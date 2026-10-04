"""The one supervised stress-execution loop every test path runs through."""

from __future__ import annotations

import contextlib
import logging
import math
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.engine import containment
from corecycler.engine.backends.base import StressResult

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from corecycler.engine.backends.base import StressBackend, StressConfig
    from corecycler.engine.detector import ErrorDetector, MCEEvent

log = logging.getLogger(__name__)

STALL_GRACE_SECONDS = 5.0
ERROR_POLL_INTERVAL = 5.0
WATCHDOG_INTERVAL = 2.0
STARTUP_WINDOW_SECONDS = 2.0
CONTAINMENT_GRACE_SECONDS = 5.0
LOAD_FLOOR_FRACTION = 0.5
STALL_BUSY_FRACTION = 0.05
STOP_GRACE_SECONDS = 3.0
KILL_WAIT_SECONDS = 2.0
SIGNAL_EXIT_BASE = 128


@dataclass(frozen=True, slots=True)
class Lane:
    core_id: int
    cpus: tuple[int, ...]
    work_dir: Path

    @property
    def cpu_list(self) -> str:
        return containment.cpu_list(self.cpus)


@dataclass(slots=True)
class _LaneRun:
    lane: Lane
    proc: subprocess.Popen | None = None
    verdict: StressResult | None = None
    started_at: float = 0.0
    deadline: float = math.inf
    stopped_at: float | None = None
    sent: signal.Signals | None = None
    last_active: float = 0.0
    last_watchdog: float = 0.0
    cpu_seconds: float | None = None
    cpu_sampled_at: float = 0.0
    unit: str | None = None
    cgroup: str | None = None
    stdout: str = ""
    stderr: str = ""
    drained: bool = False

    @property
    def running(self) -> bool:
        return self.verdict is None and self.proc is not None and self.proc.returncode is None

    def duration(self, now: float) -> float:
        if self.proc is None:
            return 0.0
        return (now if self.stopped_at is None else self.stopped_at) - self.started_at

    def failed(self, message: str | None, error_type: str | None) -> StressResult:
        return StressResult(
            core_id=self.lane.core_id,
            passed=False,
            duration_seconds=self.duration(time.monotonic()),
            error_message=message,
            error_type=error_type,
        )


@dataclass(slots=True)
class SuperviseHooks:
    on_status: Callable[[int, float], None] | None = None
    on_stall: Callable[[int], None] | None = None
    on_thermal: Callable[[float], None] | None = None


class ThermalWatch:
    """Debounced soft limit, instant hard ceiling, hysteresis after a trip."""

    HYSTERESIS = 5.0

    def __init__(
        self,
        *,
        max_temperature: float,
        grace_seconds: float,
        hard_margin: float,
        require_sensor: bool,
        read: Callable[[], float | None] | None = None,
    ) -> None:
        self.max_temperature = max_temperature
        self.grace_seconds = grace_seconds
        self.hard_margin = hard_margin
        self.require_sensor = require_sensor
        self._read = read or read_cpu_temperature
        self.tripped = False
        self._over_since: float | None = None
        self.last_temperature: float | None = None

    def safe(self) -> bool:
        temp = self._read()
        self.last_temperature = temp
        if temp is None:
            return not self.require_sensor
        limit = self.max_temperature
        if temp >= limit:
            if temp >= limit + self.hard_margin:
                self.tripped = True
                return False
            if self.tripped:
                return False
            now = time.monotonic()
            if self._over_since is None:
                self._over_since = now
            if now - self._over_since >= self.grace_seconds:
                self.tripped = True
                return False
            return True
        self._over_since = None
        if self.tripped:
            if temp < limit - self.HYSTERESIS:
                self.tripped = False
                return True
            return False
        return True


def read_cpu_temperature() -> float | None:
    hwmon_base = Path("/sys/class/hwmon")
    if not hwmon_base.exists():
        return None
    with contextlib.suppress(OSError):
        for hwmon_dir in hwmon_base.iterdir():
            name_file = hwmon_dir / "name"
            if not name_file.exists():
                continue
            try:
                name = name_file.read_text().strip()
            except OSError:
                continue
            if name not in ("k10temp", "coretemp", "zenpower", "zenpower3"):
                continue
            max_temp = 0.0
            for temp_input in sorted(hwmon_dir.glob("temp*_input")):
                try:
                    temp_c = int(temp_input.read_text().strip()) / 1000.0
                except (ValueError, OSError):
                    continue
                max_temp = max(max_temp, temp_c)
            if max_temp > 0:
                return max_temp
    return None


def make_preexec():
    def _preexec():
        os.setsid()
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL)

    return _preexec


def cpu_times(cpu_id: int) -> tuple[int, int] | None:
    with contextlib.suppress(OSError, ValueError, IndexError), open("/proc/stat") as f:
        prefix = f"cpu{cpu_id} "
        for line in f:
            if line.startswith(prefix):
                vals = [int(x) for x in line.split()[1:]]
                return vals[3] + vals[4], sum(vals)
    return None


def busy_fraction(prev: tuple[int, int] | None, now: tuple[int, int] | None) -> float | None:
    if prev is None or now is None:
        return None
    d_total = now[1] - prev[1]
    if d_total <= 0:
        return None
    return 1.0 - ((now[0] - prev[0]) / d_total)


def _no_work_done(run: _LaneRun) -> str | None:
    floor = LOAD_FLOOR_FRACTION * len(run.lane.cpus) * (run.deadline - run.started_at)
    if run.cpu_seconds is not None and 0 < floor <= run.cpu_seconds:
        return None
    used = "unreadable" if run.cpu_seconds is None else f"{run.cpu_seconds:.2f}s"
    return (
        f"payload CPU time {used} on CPUs {run.lane.cpu_list} did not reach the load floor of "
        f"{floor:.2f}s: no work done, verdict unavailable"
    )


def _outside_kill(returncode: int, sent: signal.Signals | None) -> str | None:
    died_of = -returncode if returncode < 0 else returncode - SIGNAL_EXIT_BASE
    if died_of not in (signal.SIGKILL, signal.SIGTERM) or died_of == sent:
        return None
    return f"Stress process killed externally (code {returncode}): possible OOM or system issue"


class Supervisor:
    """Runs one batch of contained lanes to per-lane honest verdicts.

    A lane with no earned verdict when the batch stops early stays None:
    an invented pass would enter the evidence record as a proven offset.
    """

    def __init__(
        self,
        *,
        backend: StressBackend,
        detector: ErrorDetector,
        thermal: ThermalWatch,
        stop_event: threading.Event,
        observed: list[MCEEvent],
        poll_interval: float = 1.0,
        stall_timeout: float = 30.0,
        stop_on_first_failure: bool = True,
        phase: str = "stress",
        hooks: SuperviseHooks | None = None,
        containment_for: Callable[[tuple[int, ...]], containment.Containment | None] | None = None,
    ) -> None:
        self.backend = backend
        self.detector = detector
        self.thermal = thermal
        self.stop_event = stop_event
        self.observed = observed
        self.poll_interval = poll_interval
        self.stall_timeout = stall_timeout
        self.stop_on_first_failure = stop_on_first_failure
        self.phase = phase
        self.hooks = hooks or SuperviseHooks()
        self._containment_for = containment_for or containment.contain

    def run(
        self,
        lanes: list[Lane],
        config_for: Callable[[Lane], StressConfig],
        duration: float,
    ) -> dict[int, StressResult | None]:
        start = time.monotonic()
        runs = [_LaneRun(lane=lane, deadline=start + duration) for lane in lanes]
        try:
            for run in runs:
                if not self._launch(run, config_for(run.lane)):
                    break
            if any(run.running for run in runs):
                self._poll_until_done(runs, start, duration)
        finally:
            self._finish(runs)
        return {run.lane.core_id: run.verdict for run in runs}

    def _launch(self, run: _LaneRun, cfg: StressConfig) -> bool:
        lane = run.lane
        try:
            self.backend.prepare(lane.work_dir, cfg)
            self.backend.assert_prepared(lane.work_dir)
            contained = self._containment_for(lane.cpus)
            prefix = contained.prefix if contained is not None else []
            run.unit = contained.unit if contained is not None else None
            cmd = prefix + self.backend.get_command(cfg, lane.work_dir)
        except (OSError, RuntimeError) as exc:
            log.error("core %d: refusing stress launch: %s", lane.core_id, exc)
            self._fail(run, f"Failed to start stress test: {exc}", error_type="startup")
            return False
        try:
            run.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(lane.work_dir),
                preexec_fn=make_preexec(),
            )
        except (OSError, RuntimeError, TypeError) as exc:
            log.error("core %d: stress process failed to start: %s", lane.core_id, exc)
            self._fail(run, f"Failed to start stress test: {exc}", error_type="startup")
            return False
        run.started_at = time.monotonic()
        run.last_active = run.started_at
        return True

    def _poll_until_done(self, runs: list[_LaneRun], start: float, duration: float) -> None:
        deadline = start + duration
        last_error_poll = start
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            if self._poll_exits_stalls_watchdog(runs, start):
                break
            if not any(run.running for run in runs):
                break
            if not self.thermal.safe():
                temp = self.thermal.last_temperature
                if self.hooks.on_thermal and temp is not None:
                    self.hooks.on_thermal(temp)
                first = min((r for r in runs if r.verdict is None), default=None, key=lambda r: r.lane.core_id)
                if first is not None:
                    self._fail(
                        first,
                        f"CPU temperature exceeded {self.thermal.max_temperature} C safety limit during {self.phase}",
                    )
                self.stop_event.set()
                break
            if self._apply_mce_events(runs):
                break
            now = time.monotonic()
            if now - last_error_poll >= ERROR_POLL_INTERVAL:
                last_error_poll = now
                if self._poll_backend_errors(runs):
                    break
            for run in runs:
                if run.running and self.hooks.on_status:
                    self.hooks.on_status(run.lane.core_id, now - start)
            self.stop_event.wait(self.poll_interval)

    def _apply_mce_events(self, runs: list[_LaneRun]) -> bool:
        events = self.detector.check_mce()
        if not events:
            return False
        self.observed.extend(events)
        cpu_to_run = {cpu: run for run in runs for cpu in run.lane.cpus}
        hit = False
        for event in events:
            if event.cpu == -1:
                anchor = min((r for r in runs if r.verdict is None), default=None, key=lambda r: r.lane.core_id)
                if anchor is not None:
                    anchor.verdict = anchor.failed(
                        f"Machine check without core attribution during {self.phase}: {event.message}",
                        "mce_unattributed",
                    )
                    self.stop_event.set()
                    hit = True
                continue
            run = cpu_to_run.get(event.cpu)
            if run is not None and run.verdict is None:
                self._fail(run, f"MCE during {self.phase}: {event.message}", error_type="mce")
                hit = True
        return hit

    def _poll_backend_errors(self, runs: list[_LaneRun]) -> bool:
        for run in runs:
            if run.verdict is not None:
                continue
            err = self.backend.poll_errors(run.lane.work_dir)
            if err:
                self._fail(run, err)
                return self.stop_on_first_failure
        return False

    def _poll_exits_stalls_watchdog(self, runs: list[_LaneRun], start: float) -> bool:
        now = time.monotonic()
        for run in runs:
            if run.verdict is not None or run.proc is None:
                continue
            self._sample_cpu(run, now)
            rc = run.proc.poll()
            if rc is not None:
                run.stopped_at = now
                self._drain(run)
                outside = _outside_kill(rc, run.sent)
                if outside is not None:
                    self._fail(run, outside, error_type="killed")
                    return self.stop_on_first_failure
                if now - run.started_at < STARTUP_WINDOW_SECONDS:
                    log.warning(
                        "Stress process for core %d exited in <%.0fs (code %d) — "
                        "binary may be missing or misconfigured",
                        run.lane.core_id,
                        STARTUP_WINDOW_SECONDS,
                        rc,
                    )
                    self._fail(
                        run,
                        self._startup_message(
                            run, rc, f"stress exited at startup (code {rc}) with no work done — verdict unavailable"
                        ),
                        error_type="startup",
                    )
                    return True
                passed, msg = self.backend.parse_output(run.stdout, run.stderr, rc)
                if passed:
                    no_work = _no_work_done(run)
                    if no_work is None:
                        run.verdict = StressResult(
                            core_id=run.lane.core_id,
                            passed=True,
                            duration_seconds=run.duration(now),
                        )
                        continue
                    self._fail(run, no_work, error_type="startup")
                    return True
                self._fail(run, msg or f"stress exited with code {rc}")
                return self.stop_on_first_failure
            if run.unit is not None and now - run.last_watchdog >= WATCHDOG_INTERVAL:
                run.last_watchdog = now
                fault = self._containment_fault(run, now)
                if fault:
                    self._fail(run, fault, error_type="startup")
                    return True
            if now - start >= STALL_GRACE_SECONDS and now - run.last_active > self.stall_timeout:
                if self.hooks.on_stall:
                    self.hooks.on_stall(run.lane.core_id)
                self._fail(
                    run,
                    f"Stress test stalled on core {run.lane.core_id} "
                    f"(payload CPU time near 0 on CPUs {run.lane.cpu_list} for "
                    f"{self.stall_timeout:.0f}s)",
                    error_type="stall",
                )
                return self.stop_on_first_failure
        return False

    def _containment_fault(self, run: _LaneRun, now: float) -> str | None:
        if run.proc is None or run.unit is None:
            return None
        if run.cgroup is None:
            run.cgroup = containment.payload_cgroup(run.proc.pid, run.unit)
        if run.cgroup is None:
            if now - run.started_at > CONTAINMENT_GRACE_SECONDS:
                return (
                    f"scope {run.unit} never adopted the stress payload within "
                    f"{CONTAINMENT_GRACE_SECONDS:.0f}s — containment fault, not a core verdict"
                )
            return None
        effective = containment.scope_effective_cpus(run.cgroup)
        if effective is None:
            return (
                f"the kernel record for scope {run.unit} vanished while the payload ran "
                "— containment fault, not a core verdict"
            )
        if effective != set(run.lane.cpus):
            return (
                f"scope {run.unit} runs on CPUs {containment.cpu_list(effective)} "
                f"instead of {run.lane.cpu_list} — containment fault, not a core verdict"
            )
        return None

    def _sample_cpu(self, run: _LaneRun, now: float) -> None:
        if run.cgroup is not None:
            seconds = containment.scope_cpu_seconds(run.cgroup)
        else:
            seconds = containment.process_group_cpu_seconds(run.proc.pid)
        if seconds is None:
            return
        busy_floor = STALL_BUSY_FRACTION * len(run.lane.cpus) * (now - run.cpu_sampled_at)
        if run.cpu_seconds is not None and seconds - run.cpu_seconds >= busy_floor:
            run.last_active = now
        run.cpu_seconds = seconds
        run.cpu_sampled_at = now

    def _startup_message(self, run: _LaneRun, rc: int, fallback: str) -> str:
        """A backend that names the environment fault in its output outranks the generic line."""
        passed, msg = self.backend.parse_output(run.stdout or "", run.stderr or "", rc)
        if not passed and msg and classify_error(msg) == "startup":
            return msg
        return fallback

    def _drain(self, run: _LaneRun) -> None:
        if run.drained or run.proc is None:
            return
        try:
            run.stdout, run.stderr = run.proc.communicate(timeout=2)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            run.stdout, run.stderr = run.stdout or "", run.stderr or ""
        run.drained = True

    def _fail(self, run: _LaneRun, message: str, *, error_type: str | None = None) -> None:
        run.verdict = run.failed(message, error_type or classify_error(message))
        if self.stop_on_first_failure:
            self.stop_event.set()

    def _signal_group(self, run: _LaneRun, sig: signal.Signals) -> None:
        try:
            os.killpg(run.proc.pid, sig)
        except ProcessLookupError:
            return
        except OSError as exc:
            log.error("core %d: %s to process group %d failed: %s", run.lane.core_id, sig.name, run.proc.pid, exc)

    def _kill_scope(self, run: _LaneRun) -> None:
        try:
            containment.kill_scope(run.cgroup)
        except OSError as exc:
            log.error("core %d: killing scope %s failed: %s", run.lane.core_id, run.cgroup, exc)

    @staticmethod
    def _reap(run: _LaneRun, deadline: float) -> None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            run.proc.wait(timeout=max(0.0, deadline - time.monotonic()))

    def _stop_all(self, runs: list[_LaneRun]) -> None:
        launched = [run for run in runs if run.proc is not None]
        stop = time.monotonic()
        live: list[_LaneRun] = []
        for run in launched:
            if run.proc.returncode is not None:
                continue
            self._sample_cpu(run, stop)
            run.stopped_at = stop
            if run.proc.poll() is None:
                live.append(run)
        for run in live:
            run.sent = signal.SIGTERM
            self._signal_group(run, signal.SIGTERM)
        for run in live:
            self._reap(run, stop + STOP_GRACE_SECONDS)
        for run in launched:
            alive = run.proc.poll() is None
            if alive:
                run.sent = signal.SIGKILL
            if alive or run.cgroup is None:
                self._signal_group(run, signal.SIGKILL)
            if run.cgroup is not None:
                self._kill_scope(run)
        kill_deadline = time.monotonic() + KILL_WAIT_SECONDS
        for run in launched:
            if run.sent == signal.SIGKILL:
                self._reap(run, kill_deadline)
        for run in launched:
            if run.proc.returncode is not None:
                self._drain(run)

    def _finish(self, runs: list[_LaneRun]) -> None:
        self._stop_all(runs)
        drained = self.detector.check_mce()
        if drained:
            self.observed.extend(drained)
            cpu_to_run = {cpu: r for r in runs for cpu in r.lane.cpus}
            for event in drained:
                target = None
                if event.cpu == -1:
                    target = min(
                        (r for r in runs if r.verdict is None or r.verdict.passed),
                        default=None,
                        key=lambda r: r.lane.core_id,
                    )
                else:
                    target = cpu_to_run.get(event.cpu)
                if target is not None and (target.verdict is None or target.verdict.passed):
                    target.verdict = target.failed(
                        f"MCE during {self.phase}: {event.message}",
                        "mce" if event.cpu != -1 else "mce_unattributed",
                    )
        for run in runs:
            if run.verdict is None and run.proc is not None:
                run.verdict = self._final_verdict(run)

    def _backend_failure(self, run: _LaneRun, rc: int) -> StressResult | None:
        live_err = self.backend.poll_errors(run.lane.work_dir)
        if live_err:
            return run.failed(live_err, classify_error(live_err))
        passed, msg = self.backend.parse_output(run.stdout, run.stderr, rc)
        return None if passed else run.failed(msg, classify_error(msg) if msg else None)

    def _final_verdict(self, run: _LaneRun) -> StressResult | None:
        rc = run.proc.returncode
        if run.sent == signal.SIGKILL:
            failure = None if rc is None else self._backend_failure(run, rc)
            return failure or run.failed(
                f"stress process did not stop after SIGTERM within the {STOP_GRACE_SECONDS:.1f}s grace and was killed",
                "killed",
            )
        outside = _outside_kill(rc, run.sent)
        if outside is not None:
            return run.failed(outside, "killed")
        if run.sent is None and rc != 0 and run.duration(time.monotonic()) < STARTUP_WINDOW_SECONDS:
            return run.failed(self._startup_message(run, rc, f"stress exited with code {rc} at startup"), "startup")
        failure = self._backend_failure(run, rc)
        if failure is not None:
            return failure
        if run.sent is not None and run.stopped_at < run.deadline:
            return None
        no_work = _no_work_done(run)
        if no_work is not None:
            return run.failed(no_work, "startup")
        return StressResult(core_id=run.lane.core_id, passed=True, duration_seconds=run.duration(time.monotonic()))


def watch_idle(
    *,
    cpus: tuple[int, ...],
    duration: float,
    thermal: ThermalWatch,
    detector: ErrorDetector,
    stop_event: threading.Event,
    observed: list[MCEEvent],
    phase: str,
    poll_interval: float = 0.5,
) -> str | None:
    own = set(cpus)
    start = time.monotonic()
    while time.monotonic() - start < duration and not stop_event.is_set():
        if not thermal.safe():
            stop_event.set()
            return f"CPU temperature exceeded {thermal.max_temperature} C safety limit during {phase}"
        events = detector.check_mce()
        if events:
            observed.extend(events)
            for event in events:
                if event.cpu == -1 or event.cpu in own:
                    return f"MCE during {phase}: {event.message}"
        remaining = duration - (time.monotonic() - start)
        stop_event.wait(min(poll_interval, max(0.0, remaining)))
    return None


def classify_error(msg: str | None) -> str:
    if not msg:
        return "unknown"
    msg_lower = msg.lower()
    if (
        "failed to start" in msg_lower
        or "verdict unavailable" in msg_lower
        or "harness error" in msg_lower
        or "containment fault" in msg_lower
    ):
        return "startup"
    if "machine check without core attribution" in msg_lower:
        return "mce_unattributed"
    if "mce" in msg_lower or "machine check" in msg_lower:
        return "mce"
    if "temperature" in msg_lower or "thermal" in msg_lower:
        return "thermal"
    if "stall" in msg_lower:
        return "stall"
    if any(
        w in msg_lower
        for w in (
            "rounding",
            "fatal",
            "illegal",
            "sumout",
            "mismatch",
            "jacobi",
            "verification",
            "computation",
        )
    ):
        return "computation"
    if "timeout" in msg_lower:
        return "timeout"
    if "killed externally" in msg_lower or "did not stop" in msg_lower:
        return "killed"
    if "crash" in msg_lower or "signal" in msg_lower:
        return "crash"
    if re.search(r"exited with code -\d+", msg_lower):
        return "crash"
    if "idle" in msg_lower:
        return "idle_instability"
    if "variable" in msg_lower or "transition" in msg_lower:
        return "load_transition"
    return "unknown"
