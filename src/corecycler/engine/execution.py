"""The one supervised stress-execution loop every test path runs through."""

from __future__ import annotations

import contextlib
import logging
import math
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.engine import containment
from corecycler.engine.backends.base import LaneSignal, Outcome, StressResult, read_text
from corecycler.engine.detector import KernelTrouble, UnblamedMachineCheck

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from corecycler.engine.backends.base import StressBackend, StressConfig
    from corecycler.engine.detector import ErrorDetector, KernelEvent
    from corecycler.engine.topology import CPUTopology

log = logging.getLogger(__name__)

STALL_GRACE_SECONDS = 5.0
OUTPUT_SCAN_INTERVAL = 5.0
WATCHDOG_INTERVAL = 2.0
CONTAINMENT_GRACE_SECONDS = 5.0
LOAD_FLOOR_FRACTION = 0.5
STALL_BUSY_FRACTION = 0.05
STOP_GRACE_SECONDS = 3.0
KILL_WAIT_SECONDS = 2.0
SIGNAL_EXIT_BASE = 128
DETAIL_TAIL_LINES = 5
STDOUT_LOG = "stdout.log"
STDERR_LOG = "stderr.log"
CPU_SIGNALS: frozenset[signal.Signals] = frozenset({signal.SIGILL, signal.SIGBUS, signal.SIGFPE, signal.SIGSEGV})
_SHOWS_LAST_OUTPUT: frozenset[Outcome] = frozenset({Outcome.NOT_STARTED, Outcome.UNEXPLAINED, Outcome.NO_WORK_DONE})


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
    run_dir: Path | None = None
    proc: subprocess.Popen | None = None
    started_at: float = 0.0
    deadline: float = math.inf
    stopped_at: float | None = None
    sent: signal.Signals | None = None
    signals: list[LaneSignal] = field(default_factory=list)
    findings: tuple[LaneSignal, ...] = ()
    last_active: float = 0.0
    last_watchdog: float = 0.0
    cpu_seconds: float | None = None
    cpu_sampled_at: float = 0.0
    unit: str | None = None
    cgroup: str | None = None
    output: str = ""
    kernel_problem: str | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def faulted(self) -> bool:
        return bool(self.signals or self.findings)

    def duration(self) -> float:
        if self.proc is None:
            return 0.0
        return (time.monotonic() if self.stopped_at is None else self.stopped_at) - self.started_at


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


def exit_signal(returncode: int, sent: signal.Signals | None) -> LaneSignal | None:
    if returncode == 0:
        return None
    if -returncode in CPU_SIGNALS:
        return LaneSignal(Outcome.CPU_SIGNAL, f"stress process died of {signal.Signals(-returncode).name}")
    died_of = -returncode if returncode < 0 else returncode - SIGNAL_EXIT_BASE
    if (died_of == signal.SIGTERM and sent is not None) or (died_of == signal.SIGKILL and sent == signal.SIGKILL):
        return None
    if died_of in (signal.SIGKILL, signal.SIGTERM):
        return LaneSignal(
            Outcome.OUTSIDE_KILL, f"Stress process killed externally (code {returncode}): possible OOM or system issue"
        )
    return LaneSignal(Outcome.UNEXPLAINED, f"stress exited with code {returncode}")


def kernel_signal(event: KernelEvent, own_cpus: tuple[int, ...], phase: str) -> LaneSignal | None:
    if isinstance(event, KernelTrouble):
        return LaneSignal(Outcome.KERNEL_TROUBLE, f"Kernel trouble during {phase}: {event.message}")
    if isinstance(event, UnblamedMachineCheck):
        return LaneSignal(
            Outcome.UNATTRIBUTED_MCE, f"Machine check without core attribution during {phase}: {event.message}"
        )
    if event.cpu in own_cpus:
        return LaneSignal(Outcome.OWN_CORE_MCE, f"MCE during {phase}: {event.message}")
    return None


def refuse_if_missing(topology: CPUTopology, cores: list[int]) -> dict[int, StressResult]:
    missing = [core for core in cores if not (info := topology.cores.get(core)) or not info.logical_cpus]
    if not missing:
        return {}
    named = ", ".join(str(core) for core in missing)
    refused: dict[int, StressResult] = {}
    for core in cores:
        if core in missing:
            outcome, detail = Outcome.NOT_STARTED, f"core {core} not in topology"
        else:
            outcome, detail = Outcome.CUT_SHORT, f"not run: the topology lacks core {named} of this batch"
        refused[core] = StressResult(core_id=core, outcome=outcome, detail=detail, duration_seconds=0.0)
    return refused


def _no_work_done(run: _LaneRun) -> str | None:
    floor = LOAD_FLOOR_FRACTION * len(run.lane.cpus) * (run.deadline - run.started_at)
    if run.cpu_seconds is not None and 0 < floor <= run.cpu_seconds:
        return None
    used = "unreadable" if run.cpu_seconds is None else f"{run.cpu_seconds:.2f}s"
    return (
        f"payload CPU time {used} on CPUs {run.lane.cpu_list} did not reach the load floor of "
        f"{floor:.2f}s: no work done, verdict unavailable"
    )


def _tail(text: str) -> str:
    return " | ".join([line.strip() for line in text.splitlines() if line.strip()][-DETAIL_TAIL_LINES:])


class Supervisor:
    def __init__(
        self,
        *,
        backend: StressBackend,
        detector: ErrorDetector,
        thermal: ThermalWatch,
        stop_event: threading.Event,
        observed: list[KernelEvent],
        poll_interval: float = 1.0,
        stall_timeout: float = 30.0,
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
        self.phase = phase
        self.hooks = hooks or SuperviseHooks()
        self._containment_for = containment_for or containment.contain

    def run(self, lanes: list[Lane], config: StressConfig, duration: float) -> dict[int, StressResult]:
        start = time.monotonic()
        runs = [_LaneRun(lane=lane, deadline=start + duration) for lane in lanes]
        try:
            for run in runs:
                if not self._launch(run, replace(config, threads=len(run.lane.cpus), cpus=run.lane.cpus)):
                    break
            self._watch(runs, start, start + duration)
        finally:
            self._stop_all(runs)
        for run in runs:
            if run.proc is not None:
                self._scan_output(run)
        self._take_kernel_events(runs, final=True)
        return {run.lane.core_id: self._grade(run) for run in runs}

    def _launch(self, run: _LaneRun, cfg: StressConfig) -> bool:
        lane = run.lane
        run_dir = lane.work_dir / f"run-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
        try:
            run_dir.mkdir(parents=True)
            run.run_dir = run_dir
            self.backend.prepare(run_dir, cfg)
            self.backend.assert_prepared(run_dir)
            contained = self._containment_for(lane.cpus)
            prefix = contained.prefix if contained is not None else []
            run.unit = contained.unit if contained is not None else None
            cmd = prefix + self.backend.get_command(cfg, run_dir)
        except (OSError, RuntimeError) as exc:
            log.error("core %d: refusing stress launch: %s", lane.core_id, exc)
            run.signals.append(LaneSignal(Outcome.NOT_STARTED, f"Failed to start stress test: {exc}"))
            return False
        try:
            with (run_dir / STDOUT_LOG).open("wb") as stdout, (run_dir / STDERR_LOG).open("wb") as stderr:
                run.proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    cwd=run_dir,
                    preexec_fn=make_preexec(),
                )
        except (OSError, RuntimeError, TypeError) as exc:
            log.error("core %d: stress process failed to start: %s", lane.core_id, exc)
            run.signals.append(LaneSignal(Outcome.NOT_STARTED, f"Failed to start stress test: {exc}"))
            return False
        run.started_at = time.monotonic()
        run.last_active = run.started_at
        return True

    def _watch(self, runs: list[_LaneRun], start: float, deadline: float) -> None:
        last_scan = start
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                return
            for run in runs:
                if run.alive:
                    self._check(run, now, start)
            if not self.thermal.safe():
                temp = self.thermal.last_temperature
                if self.hooks.on_thermal and temp is not None:
                    self.hooks.on_thermal(temp)
                tripped = LaneSignal(
                    Outcome.THERMAL,
                    f"CPU temperature exceeded {self.thermal.max_temperature} C safety limit during {self.phase}",
                )
                for run in runs:
                    run.signals.append(tripped)
            self._take_kernel_events(runs)
            if now - last_scan >= OUTPUT_SCAN_INTERVAL:
                last_scan = now
                for run in runs:
                    if run.alive:
                        self._scan_output(run)
            if any(run.faulted for run in runs) or not any(run.alive for run in runs):
                return
            for run in runs:
                if run.alive and self.hooks.on_status:
                    self.hooks.on_status(run.lane.core_id, now - start)
            self.stop_event.wait(self.poll_interval)

    def _check(self, run: _LaneRun, now: float, start: float) -> None:
        self._sample_cpu(run, now)
        if run.proc.poll() is not None:
            run.stopped_at = now
            self._note_exit(run)
            self._scan_output(run)
            return
        if run.unit is not None and now - run.last_watchdog >= WATCHDOG_INTERVAL:
            run.last_watchdog = now
            fault = self._containment_fault(run, now)
            if fault:
                run.signals.append(LaneSignal(Outcome.CONTAINMENT_BROKE, fault))
                return
        if now - start >= STALL_GRACE_SECONDS and now - run.last_active > self.stall_timeout:
            if self.hooks.on_stall:
                self.hooks.on_stall(run.lane.core_id)
            run.signals.append(
                LaneSignal(
                    Outcome.STALL,
                    f"Stress test stalled on core {run.lane.core_id} "
                    f"(payload CPU time near 0 on CPUs {run.lane.cpu_list} for "
                    f"{self.stall_timeout:.0f}s)",
                )
            )

    def _take_kernel_events(self, runs: list[_LaneRun], *, final: bool = False) -> None:
        read = self.detector.read_new(final=final)
        self.observed.extend(read.events)
        for run in runs:
            if run.kernel_problem is None:
                run.kernel_problem = read.problem
            for event in read.events:
                found = kernel_signal(event, run.lane.cpus, self.phase)
                if found is not None:
                    run.signals.append(found)

    @staticmethod
    def _note_exit(run: _LaneRun) -> None:
        if run.proc.returncode is None:
            return
        found = exit_signal(run.proc.returncode, run.sent)
        if found is not None:
            run.signals.append(found)

    def _scan_output(self, run: _LaneRun) -> None:
        unreadable: tuple[LaneSignal, ...] = ()
        try:
            run.output = read_text(run.run_dir / STDOUT_LOG) + "\n" + read_text(run.run_dir / STDERR_LOG)
        except OSError as exc:
            log.error("core %d: stress output unreadable: %s", run.lane.core_id, exc)
            unreadable = (LaneSignal(Outcome.UNVERIFIED, f"stress output unreadable ({exc}): verdict unavailable"),)
        run.findings = (*self.backend.findings(run.output, run.run_dir), *unreadable)

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
        stopping: list[_LaneRun] = []
        live: list[_LaneRun] = []
        for run in launched:
            if run.proc.returncode is not None:
                continue
            if run.unit is not None and run.cgroup is None:
                run.cgroup = containment.payload_cgroup(run.proc.pid, run.unit)
            self._sample_cpu(run, stop)
            run.stopped_at = stop
            stopping.append(run)
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
                run.signals.append(
                    LaneSignal(
                        Outcome.DID_NOT_STOP,
                        f"stress process did not stop after SIGTERM within the {STOP_GRACE_SECONDS:.1f}s grace "
                        "and was killed",
                    )
                )
            if alive or run.cgroup is None:
                self._signal_group(run, signal.SIGKILL)
            if run.cgroup is not None:
                self._kill_scope(run)
        kill_deadline = time.monotonic() + KILL_WAIT_SECONDS
        for run in launched:
            if run.sent == signal.SIGKILL:
                self._reap(run, kill_deadline)
        for run in stopping:
            self._note_exit(run)

    def _grade(self, run: _LaneRun) -> StressResult:
        signals = sorted([*run.signals, *run.findings], key=lambda found: found.outcome.rank)
        if signals:
            outcome, detail = signals[0].outcome, "; also: ".join(found.text for found in signals)
        elif run.proc is None or (run.sent is not None and run.stopped_at < run.deadline):
            outcome, detail = Outcome.CUT_SHORT, f"stopped before the end of its {self.phase}: no verdict of its own"
        elif (no_work := _no_work_done(run)) is not None:
            outcome, detail = Outcome.NO_WORK_DONE, no_work
        elif run.kernel_problem is not None:
            outcome, detail = Outcome.UNVERIFIED, f"{run.kernel_problem} during {self.phase}: verdict unavailable"
        else:
            outcome, detail = Outcome.PASS, ""
        tail = _tail(run.output)
        if outcome in _SHOWS_LAST_OUTPUT and tail:
            detail = f"{detail}; last output: {tail}"
        evidence_dir = run.run_dir
        if outcome is Outcome.PASS:
            evidence_dir = None
            try:
                shutil.rmtree(run.run_dir)
            except OSError as exc:
                log.error("core %d: removing the passed run dir %s failed: %s", run.lane.core_id, run.run_dir, exc)
        if evidence_dir is not None:
            detail = f"{detail}; evidence: {evidence_dir}"
        return StressResult(
            core_id=run.lane.core_id,
            outcome=outcome,
            detail=detail,
            duration_seconds=run.duration(),
            evidence_dir=evidence_dir,
        )


def watch_idle(
    *,
    cpus: tuple[int, ...],
    duration: float,
    thermal: ThermalWatch,
    detector: ErrorDetector,
    stop_event: threading.Event,
    observed: list[KernelEvent],
    phase: str,
    poll_interval: float = 0.5,
) -> LaneSignal | None:
    start = time.monotonic()
    found: list[LaneSignal] = []
    while True:
        final = bool(found) or time.monotonic() - start >= duration or stop_event.is_set()
        if not final and not thermal.safe():
            final = True
            found.append(
                LaneSignal(
                    Outcome.THERMAL, f"CPU temperature exceeded {thermal.max_temperature} C safety limit during {phase}"
                )
            )
        read = detector.read_new(final=final)
        observed.extend(read.events)
        found.extend(hit for hit in (kernel_signal(event, cpus, phase) for event in read.events) if hit is not None)
        if read.problem is not None:
            found.append(LaneSignal(Outcome.UNVERIFIED, f"{read.problem} during {phase}: verdict unavailable"))
        if final:
            return min(found, key=lambda hit: hit.outcome.rank, default=None)
        if not found:
            remaining = duration - (time.monotonic() - start)
            stop_event.wait(min(poll_interval, max(0.0, remaining)))
