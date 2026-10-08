"""The one supervised stress-execution loop every test path runs through."""

from __future__ import annotations

import contextlib
import ctypes
import functools
import logging
import math
import os
import resource
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from corecycler.config.paths import hand_back
from corecycler.engine import containment
from corecycler.engine.backends.base import LaneSignal, Outcome, StressResult, read_text
from corecycler.engine.detector import KernelTrouble, UnblamedMachineCheck

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from pathlib import Path

    from corecycler.engine.backends.base import StressBackend, StressConfig
    from corecycler.engine.detector import ErrorDetector, KernelEvent
    from corecycler.monitor.hwmon import SensorRead

log = logging.getLogger(__name__)

STALL_GRACE_SECONDS = 5.0
OUTPUT_SCAN_INTERVAL = 5.0
WATCHDOG_INTERVAL = 2.0
POLICY_NAMES = {
    os.SCHED_OTHER: "normal",
    os.SCHED_BATCH: "batch",
    os.SCHED_IDLE: "idle",
    os.SCHED_FIFO: "realtime FIFO",
    os.SCHED_RR: "realtime round-robin",
}
CONTAINMENT_GRACE_SECONDS = 5.0
LOAD_FLOOR_FRACTION = 0.5
STALL_BUSY_FRACTION = 0.05
STOP_GRACE_SECONDS = 3.0
KILL_WAIT_SECONDS = 2.0
SIGNAL_EXIT_BASE = 128
DETAIL_TAIL_LINES = 5
OUTPUT_KEPT_CHARS = 64 * 1024
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
    speeds: tuple[tuple[str, float, bool], ...] = ()
    last_active: float = 0.0
    last_watchdog: float = 0.0
    cpu_seconds: float | None = None
    cpu_sampled_at: float = 0.0
    contained: containment.Containment | None = None
    cgroup: str | None = None
    output: str = ""
    read_problem: str | None = None
    oom_kills: int = 0
    frozen: bool = False
    frozen_since: float | None = None
    frozen_total: float = 0.0
    scheduled_as: tuple[int, int] | None = None

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    @property
    def faulted(self) -> bool:
        return bool(self.signals or self.findings or self.oom_kills)

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
    ) -> None:
        self.max_temperature = max_temperature
        self.grace_seconds = grace_seconds
        self.hard_margin = hard_margin
        self.require_sensor = require_sensor
        self.tripped = False
        self._over_since: float | None = None
        self.last_temperature: float | None = None

    @classmethod
    def for_config(cls, config) -> ThermalWatch:
        return cls(
            max_temperature=config.max_temperature,
            grace_seconds=config.over_temp_grace_seconds,
            hard_margin=config.over_temp_hard_margin,
            require_sensor=config.require_thermal_sensor,
        )

    def safe(self, temp: float | None) -> bool:
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


_PR_SET_PDEATHSIG = 1


@functools.cache
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def make_preexec():
    libc = _libc()

    def _preexec():
        os.setsid()
        resource.setrlimit(resource.RLIMIT_RTPRIO, (0, 0))
        if libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL) != 0:
            raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")

    return _preexec


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


def _policy(number: int) -> str:
    return POLICY_NAMES.get(number, f"policy {number}")


def _scheduling_fault(run: _LaneRun) -> str | None:
    if run.scheduled_as is None:
        return None
    nice, policy = run.scheduled_as
    for name, tid, now_nice, now_policy in containment.tree_scheduling(run.proc.pid):
        if (now_nice, now_policy) != (nice, policy):
            return (
                f"{name} (thread {tid}) runs at nice {now_nice} with {_policy(now_policy)} scheduling instead of "
                f"nice {nice} with {_policy(policy)}: another program changed how the test is scheduled "
                f"(ananicy-cpp does this by process name); stop it or its rule for {name}, then resume: "
                "scheduling fault, not a core verdict"
            )
    return None


def _frozen_for(run: _LaneRun, now: float) -> float:
    return run.frozen_total + (now - run.frozen_since if run.frozen else 0.0)


def _no_work_done(run: _LaneRun) -> str | None:
    floor = LOAD_FLOOR_FRACTION * len(run.lane.cpus) * (run.deadline - run.started_at - run.frozen_total)
    if run.cpu_seconds is not None and 0 < floor <= run.cpu_seconds:
        return None
    used = "unreadable" if run.cpu_seconds is None else f"{run.cpu_seconds:.2f}s"
    return (
        f"payload CPU time {used} on CPUs {run.lane.cpu_list} did not reach the load floor of "
        f"{floor:.2f}s: no work done, verdict unavailable"
    )


def _tail(text: str) -> str:
    return " | ".join([line.strip() for line in text.splitlines() if line.strip()][-DETAIL_TAIL_LINES:])


def kept_output(text: str) -> str:
    if len(text) <= OUTPUT_KEPT_CHARS:
        return text
    return f"({len(text) - OUTPUT_KEPT_CHARS} earlier characters not kept)\n{text[-OUTPUT_KEPT_CHARS:]}"


class Supervisor:
    def __init__(
        self,
        *,
        backend: StressBackend,
        detector: ErrorDetector,
        thermal: ThermalWatch,
        sensors: Callable[[], SensorRead],
        stop_event: threading.Event,
        observed: list[KernelEvent],
        poll_interval: float = 1.0,
        stall_timeout: float = 30.0,
        phase: str = "stress",
        hooks: SuperviseHooks | None = None,
        containment_for: Callable[[tuple[int, ...]], containment.Containment | None] | None = None,
        freeze: Callable[[float, SensorRead], bool] | None = None,
        hold_cap_s: float | None = None,
    ) -> None:
        self.backend = backend
        self.detector = detector
        self.thermal = thermal
        self.sensors = sensors
        self.stop_event = stop_event
        self.observed = observed
        self.poll_interval = poll_interval
        self.stall_timeout = stall_timeout
        self.phase = phase
        self.hooks = hooks or SuperviseHooks()
        self._containment_for = containment_for or containment.contain
        self.freeze = freeze
        self.hold_cap_s = hold_cap_s

    def run(self, lanes: list[Lane], config: StressConfig, duration: float) -> dict[int, StressResult]:
        start = time.monotonic()
        runs = [_LaneRun(lane=lane, deadline=start + duration) for lane in lanes]
        try:
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
        finally:
            for run in runs:
                self._remove_run_dir(run)

    def _launch(self, run: _LaneRun, cfg: StressConfig) -> bool:
        lane = run.lane
        run_dir = lane.work_dir / f"run-{datetime.now(UTC):%Y%m%dT%H%M%S%f}"
        try:
            run_dir.mkdir(parents=True)
            run.run_dir = run_dir
            hand_back(run_dir)
            self.backend.prepare(run_dir, cfg)
            self.backend.assert_prepared(run_dir)
            run.contained = self._containment_for(lane.cpus)
            prefix = run.contained.prefix if run.contained is not None else []
            cmd = prefix + self.backend.get_command(cfg, run_dir)
        except (OSError, RuntimeError) as exc:
            log.error("core %d: refusing stress launch: %s", lane.core_id, exc)
            run.signals.append(LaneSignal(Outcome.NOT_STARTED, f"Failed to start stress test: {exc}"))
            return False
        run.scheduled_as = containment.own_scheduling()
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
        except (OSError, RuntimeError, TypeError, subprocess.SubprocessError) as exc:
            log.error("core %d: stress process failed to start: %s", lane.core_id, exc)
            run.signals.append(LaneSignal(Outcome.NOT_STARTED, f"Failed to start stress test: {exc}"))
            return False
        run.started_at = time.monotonic()
        run.last_active = run.started_at
        return True

    def _watch(self, runs: list[_LaneRun], start: float, deadline: float) -> None:
        last_scan = start
        duration = deadline - start
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                return
            reading = self.sensors()
            if self.freeze is not None:
                self._set_frozen(runs, self.freeze(now - start, reading), now)
            if self.hold_cap_s is not None:
                held = [run.frozen_since for run in runs if run.frozen]
                if held and now - min(held) > self.hold_cap_s:
                    self._hold_expired(runs)
                    return
                deadline = start + duration + max(_frozen_for(run, now) for run in runs)
                for run in runs:
                    run.deadline = deadline
            for run in runs:
                if run.alive:
                    self._check(run, now, start)
            if not self.thermal.safe(reading.hottest):
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
        if now - run.last_watchdog >= WATCHDOG_INTERVAL:
            run.last_watchdog = now
            fault = _scheduling_fault(run)
            if run.contained is not None:
                self._read_oom_kills(run)
                fault = fault or self._containment_fault(run, now)
            if fault:
                run.signals.append(LaneSignal(Outcome.CONTAINMENT_BROKE, fault))
                return
        if not run.frozen and now - start >= STALL_GRACE_SECONDS and now - run.last_active > self.stall_timeout:
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

    def _hold_expired(self, runs: list[_LaneRun]) -> None:
        reading = self.thermal.last_temperature
        last = "" if reading is None else f", last reading {reading:.1f} C"
        text = f"the load stayed held more than {self.hold_cap_s:g} s waiting for the CPU to cool{last}"
        for run in runs:
            run.signals.append(LaneSignal(Outcome.THERMAL, text))

    def _set_frozen(self, runs: list[_LaneRun], frozen: bool, now: float) -> None:
        for run in runs:
            if not run.alive or run.frozen == frozen:
                continue
            if frozen:
                self._sample_cpu(run, now)
                self._signal_group(run, signal.SIGSTOP)
                run.frozen_since = now
            else:
                self._signal_group(run, signal.SIGCONT)
                run.frozen_total += max(0.0, min(now, run.deadline) - run.frozen_since)
                run.frozen_since = None
                run.last_active = now
            run.frozen = frozen

    def _take_kernel_events(self, runs: list[_LaneRun], *, final: bool = False) -> None:
        read = self.detector.read_new(final=final)
        self.observed.extend(read.events)
        for run in runs:
            if run.read_problem is None:
                run.read_problem = read.problem
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
        run.speeds = self.backend.speeds(run.output)

    @staticmethod
    def _read_oom_kills(run: _LaneRun) -> None:
        kills = containment.scope_oom_kills(run.contained.scope)
        if kills is not None:
            run.oom_kills = max(run.oom_kills, kills)
        elif run.read_problem is None and containment.scope_exists(run.contained.scope):
            run.read_problem = f"the memory.events of live scope {run.contained.unit} cannot be read for its OOM kills"

    def _containment_fault(self, run: _LaneRun, now: float) -> str | None:
        if run.proc is None or run.contained is None:
            return None
        if run.cgroup is None and (moved := self._learn_scope(run)) is not None:
            return moved
        if run.cgroup is None:
            if now - run.started_at > CONTAINMENT_GRACE_SECONDS:
                return (
                    f"scope {run.contained.unit} never adopted the stress payload within "
                    f"{CONTAINMENT_GRACE_SECONDS:.0f}s: containment fault, not a core verdict"
                )
            return None
        effective = containment.scope_effective_cpus(run.cgroup)
        if effective is None:
            return (
                f"the kernel record for scope {run.contained.unit} vanished while the payload ran: "
                "containment fault, not a core verdict"
            )
        if effective != set(run.lane.cpus):
            return (
                f"scope {run.contained.unit} runs on CPUs {containment.cpu_list(effective)} "
                f"instead of {run.lane.cpu_list}: containment fault, not a core verdict"
            )
        return None

    @staticmethod
    def _learn_scope(run: _LaneRun) -> str | None:
        learned = containment.payload_cgroup(run.proc.pid, run.contained.unit)
        if learned is not None and learned != run.contained.scope:
            return (
                f"the payload of scope {run.contained.unit} sits in {learned}, not in {run.contained.scope} "
                "where it was launched: containment fault, not a core verdict"
            )
        run.cgroup = learned
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
            containment.kill_scope(run.contained.scope)
        except OSError as exc:
            log.error("core %d: killing scope %s failed: %s", run.lane.core_id, run.contained.scope, exc)

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
            if run.contained is not None:
                self._read_oom_kills(run)
            if run.proc.returncode is not None:
                continue
            if run.contained is not None and run.cgroup is None and not run.faulted:
                moved = self._learn_scope(run)
                if moved is not None:
                    run.signals.append(LaneSignal(Outcome.CONTAINMENT_BROKE, moved))
            self._sample_cpu(run, stop)
            run.stopped_at = stop
            stopping.append(run)
            if run.proc.poll() is None:
                live.append(run)
        self._set_frozen(live, False, stop)
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
            if alive or run.contained is None:
                self._signal_group(run, signal.SIGKILL)
            if run.contained is not None:
                self._kill_scope(run)
        kill_deadline = time.monotonic() + KILL_WAIT_SECONDS
        for run in launched:
            if run.sent == signal.SIGKILL:
                self._reap(run, kill_deadline)
        for run in stopping:
            self._note_exit(run)

    def _grade(self, run: _LaneRun) -> StressResult:
        killed = f"the kernel's OOM killer killed {run.oom_kills} process(es) in this lane's scope"
        oom = [LaneSignal(Outcome.OUTSIDE_KILL, killed)] if run.oom_kills else []
        signals = sorted([*run.signals, *run.findings, *oom], key=lambda found: found.outcome.rank)
        if signals:
            outcome, detail = signals[0].outcome, "; also: ".join(found.text for found in signals)
        elif run.proc is None or (run.sent is not None and run.stopped_at < run.deadline):
            outcome, detail = Outcome.CUT_SHORT, f"stopped before the end of its {self.phase}: no verdict of its own"
        elif (no_work := _no_work_done(run)) is not None:
            outcome, detail = Outcome.NO_WORK_DONE, no_work
        elif run.read_problem is not None:
            outcome, detail = Outcome.UNVERIFIED, f"{run.read_problem} during {self.phase}: verdict unavailable"
        else:
            outcome, detail = Outcome.PASS, ""
        tail = _tail(run.output)
        if outcome in _SHOWS_LAST_OUTPUT and tail:
            detail = f"{detail}; last output: {tail}"
        return StressResult(
            core_id=run.lane.core_id,
            outcome=outcome,
            detail=detail,
            duration_seconds=run.duration(),
            output="" if outcome is Outcome.PASS else kept_output(run.output),
            speeds=run.speeds,
        )

    @staticmethod
    def _remove_run_dir(run: _LaneRun) -> None:
        if run.run_dir is None:
            return
        try:
            shutil.rmtree(run.run_dir)
        except OSError as exc:
            log.error("core %d: removing the run dir %s failed: %s", run.lane.core_id, run.run_dir, exc)


def watch_idle(
    *,
    cpus: tuple[int, ...],
    duration: float,
    thermal: ThermalWatch,
    sensors: Callable[[], SensorRead],
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
        if not final and not thermal.safe(sensors().hottest):
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
