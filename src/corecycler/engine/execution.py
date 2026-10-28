"""The one supervised stress-execution loop every test path runs through."""

from __future__ import annotations

import contextlib
import ctypes
import functools
import json
import logging
import math
import os
import resource
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum, auto
from typing import TYPE_CHECKING

from corecycler.config.paths import atomic_write, hand_back
from corecycler.engine import containment
from corecycler.engine.backends.base import LaneSignal, Outcome, StressResult, read_text
from corecycler.engine.detector import CoreMachineCheck, KernelTrouble, UnblamedMachineCheck
from corecycler.smu.writer_lock import start_time

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from corecycler.engine.backends.base import StressBackend, StressConfig
    from corecycler.engine.detector import ErrorDetector, KernelEvent
    from corecycler.monitor.hwmon import SensorRead

log = logging.getLogger(__name__)

STALL_GRACE_SECONDS = 5.0
OUTPUT_SCAN_INTERVAL = 5.0
WATCHDOG_INTERVAL = 2.0
HEARTBEAT_EVERY_S = 5.0
POLICY_NAMES = {
    os.SCHED_OTHER: "normal",
    os.SCHED_BATCH: "batch",
    os.SCHED_IDLE: "idle",
    os.SCHED_FIFO: "realtime FIFO",
    os.SCHED_RR: "realtime round-robin",
}
CONTAINMENT_GRACE_SECONDS = 5.0
LOAD_FLOOR_FRACTION = 0.5
WALL_TIME_RUN_FLOOR = 0.05
WALL_TIME_UNREAD_LIMIT = 0.5
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
class _Held:
    since: float | None = None
    total: float = 0.0

    def mark(self, held: bool, now: float) -> None:
        if (self.since is not None) == held:
            return
        if held:
            self.since = now
        else:
            self.total += now - self.since
            self.since = None

    def unbroken(self, now: float) -> float:
        return 0.0 if self.since is None else now - self.since

    def summed(self, now: float) -> float:
        return self.total + self.unbroken(now)


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
    cpu_last_read_failed: bool = False
    contained: containment.Containment | None = None
    cgroup: str | None = None
    output: str = ""
    read_problem: str | None = None
    oom_kills: int = 0
    frozen: bool = False
    frozen_since: float | None = None
    frozen_total: float = 0.0
    frozen_in_all: float = 0.0
    heat: _Held = field(default_factory=_Held)
    unread: _Held = field(default_factory=_Held)
    holds: _Held = field(default_factory=_Held)
    scheduled_as: tuple[int, int] | None = None
    progress_window: float | None = None
    last_progress: float = 0.0
    progress_clock_at_freeze: float = 0.0

    def progress_clock(self, now: float) -> float:
        """The clock the hang window reads: wall time, planned rests included, heat and unread holds left out."""
        return now - self.holds.summed(now)

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


class Frozen(Enum):
    NO = auto()
    PLANNED = auto()
    HEAT = auto()
    UNREAD = auto()


@dataclass(frozen=True, slots=True)
class HoldCap:
    seconds: float
    describe: Callable[[], str]
    outcome: Outcome
    in_wall_time: bool


@dataclass(slots=True)
class SuperviseHooks:
    on_status: Callable[[int, float], None] | None = None
    on_stall: Callable[[int], None] | None = None
    on_thermal: Callable[[float], None] | None = None


class ThermalWatch:
    """Debounced soft limit, instant hard ceiling, hysteresis after a trip."""

    HYSTERESIS = 5.0
    GRACE_WINDOW_MIN_S = 10.0

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
        self._readings: deque[tuple[float, bool]] = deque()
        self.last_temperature: float | None = None

    @classmethod
    def for_config(cls, config) -> ThermalWatch:
        return cls(
            max_temperature=config.max_temperature,
            grace_seconds=config.over_temp_grace_seconds,
            hard_margin=config.over_temp_hard_margin,
            require_sensor=config.require_thermal_sensor,
        )

    def stop_signal(self, phase: str) -> LaneSignal:
        if self.last_temperature is None:
            return LaneSignal(Outcome.UNVERIFIED, f"the CPU temperature could not be read during {phase}")
        return LaneSignal(
            Outcome.THERMAL,
            f"CPU temperature {self.last_temperature:.1f} C at the {self.max_temperature} C safety limit "
            f"during {phase}",
        )

    def safe(self, temp: float | None) -> bool:
        self.last_temperature = temp
        if temp is None:
            return not self.require_sensor
        limit = self.max_temperature
        now = time.monotonic()
        window = max(self.GRACE_WINDOW_MIN_S, 2.0 * self.grace_seconds)
        self._readings.append((now, temp >= limit))
        start = now - window
        while len(self._readings) > 1 and self._readings[1][0] <= start:  # equivalent-mutant: the sum clips to start
            self._readings.popleft()
        if temp >= limit + self.hard_margin:
            self.tripped = True
            return False
        if self.tripped:
            if temp < limit - self.HYSTERESIS:
                self.tripped = False
                self._readings.clear()
                return True
            return False
        if temp >= limit and self._over_seconds(start) >= self.grace_seconds:
            self.tripped = True
            return False
        return True

    def _over_seconds(self, since: float) -> float:
        readings = list(self._readings)
        return sum(
            max(0.0, end - max(start, since))
            for (start, over), (end, _) in zip(readings, readings[1:], strict=False)
            if over
        )


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
    if isinstance(event, CoreMachineCheck) and event.cpu in own_cpus:
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


def _frozen_in_all(run: _LaneRun) -> float:
    still = 0.0 if run.frozen_since is None else run.stopped_at - run.frozen_since
    return run.frozen_in_all + still


def _no_work_done(run: _LaneRun) -> str | None:
    floor = LOAD_FLOOR_FRACTION * len(run.lane.cpus) * (run.deadline - run.started_at - run.frozen_total)
    if run.cpu_seconds is not None and 0 < floor <= run.cpu_seconds:
        return None
    if run.cpu_seconds is not None and not run.cpu_last_read_failed:
        return (
            f"payload CPU time {run.cpu_seconds:.2f}s on CPUs {run.lane.cpu_list} did not reach the load floor of "
            f"{floor:.2f}s: no work done, verdict unavailable"
        )
    unread = (
        "every read"
        if run.cpu_seconds is None
        else f"the final read; the last read that succeeded gave {run.cpu_seconds:.2f}s at "
        f"{run.cpu_sampled_at - run.started_at:.1f}s into the run"
    )
    return (
        f"payload CPU time on CPUs {run.lane.cpu_list} unreadable at {unread}: the load floor of {floor:.2f}s is "
        "unproven, verdict unavailable"
    )


def _tail(text: str) -> str:
    return " | ".join([line.strip() for line in text.splitlines() if line.strip()][-DETAIL_TAIL_LINES:])


def kept_output(text: str) -> str:
    if len(text) <= OUTPUT_KEPT_CHARS:
        return text
    return f"({len(text) - OUTPUT_KEPT_CHARS} earlier characters not kept)\n{text[-OUTPUT_KEPT_CHARS:]}"


class Heartbeat:
    """The beat the hang watch reads: running while a test makes progress, between while the driver works between
    tests, idle once nothing drives; what it says is written at once when it changes and otherwise at most every
    HEARTBEAT_EVERY_S. It names ``probe_dir``, where the watch writes to prove the disk answers. A beat that cannot be
    written leaves no older one behind, so a stale beat never looks like a hang. One lock orders every write, so a
    test's end on its own thread and the driver's rest never cross."""

    def __init__(self, path: Path | None = None, probe_dir: Path | None = None) -> None:
        self.path = path
        self.probe_dir = probe_dir
        self.said: dict[str, str] = {}
        self.due = -math.inf
        self.failed = False
        self.driving = False
        self.lock = threading.RLock()

    def running(self, test: str, now: float) -> None:
        self._renew({"state": "running", "test": test}, now)

    def between(self, now: float) -> None:
        self._renew({"state": "between"}, now)

    def ended(self, now: float) -> None:
        """A test's last beat, once it has cleaned up after itself: between while its driver drives, idle otherwise."""
        with self.lock:
            if self.driving:
                self.between(now)
            else:
                self._write({"state": "idle"})

    @contextlib.contextmanager
    def driven(self) -> Iterator[None]:
        """The driver drives for the length of the block: the beat says between from its start unless a test says
        running; however the block ends, the beat then says idle, unless a test still says running, whose end then
        does."""
        with self.lock:
            self.driving = True
            if self.said.get("state") != "running":
                self._write({"state": "between"})
        try:
            yield
        finally:
            with self.lock:
                self.driving = False
                if self.said.get("state") != "running":
                    self._write({"state": "idle"})

    def _renew(self, said: dict[str, str], now: float) -> None:
        with self.lock:
            if said == self.said and now < self.due:
                return
            self.due = now + HEARTBEAT_EVERY_S
            self._write(said)

    def _write(self, said: dict[str, str]) -> None:
        self.said = said
        if self.path is None:
            return
        beat = {
            **said,
            "probe_dir": None if self.probe_dir is None else str(self.probe_dir),
            "pid": os.getpid(),
            "started": start_time(os.getpid()),
            "at": time.clock_gettime(time.CLOCK_MONOTONIC),
        }
        try:
            self.path.parent.mkdir(exist_ok=True)
            hand_back(self.path.parent)
            atomic_write(self.path, json.dumps(beat))
        except OSError as exc:
            with contextlib.suppress(OSError):
                self.path.unlink()
            if not self.failed:
                log.error(
                    "the heartbeat %s could not be written, so no hang watch sees this session: %s", self.path, exc
                )
            self.failed = True
        else:
            self.failed = False


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
        settle_s: float,
        poll_interval: float = 1.0,
        stall_timeout: float = 30.0,
        phase: str = "stress",
        hooks: SuperviseHooks | None = None,
        containment_for: Callable[[tuple[int, ...]], containment.Containment | None] | None = None,
        freeze: Callable[[float, SensorRead], Frozen] | None = None,
        hold_cap: HoldCap | None = None,
        must_finish_a_test: bool = False,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        self.backend = backend
        self.detector = detector
        self.thermal = thermal
        self.sensors = sensors
        self.stop_event = stop_event
        self.observed = observed
        self.settle_s = settle_s
        self.poll_interval = poll_interval
        self.stall_timeout = stall_timeout
        self.phase = phase
        self.hooks = hooks or SuperviseHooks()
        self._containment_for = containment_for or containment.contain
        self.freeze = freeze
        self.hold_cap = hold_cap
        self.must_finish_a_test = must_finish_a_test
        self.heartbeat = heartbeat or Heartbeat()

    def run(self, lanes: list[Lane], config: StressConfig, duration: float) -> dict[int, StressResult]:
        start = time.monotonic()
        runs = [_LaneRun(lane=lane, deadline=start + duration) for lane in lanes]
        beats = self.heartbeat
        try:
            try:
                for run in runs:
                    if not self._launch(run, replace(config, threads=len(run.lane.cpus), cpus=run.lane.cpus)):
                        break
                self._watch(runs, start, start + duration, beats)
            finally:
                self._stop_all(runs)
            for run in runs:
                if run.proc is not None:
                    self._scan_output(run)
            self.stop_event.wait(self.settle_s)
            self._take_kernel_events(runs, final=True)
            return {run.lane.core_id: self._grade(run) for run in runs}
        finally:
            for run in runs:
                self._remove_run_dir(run)
            beats.ended(time.monotonic())

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
        run.last_active = run.cpu_sampled_at = run.started_at
        run.last_progress = run.started_at
        run.progress_window = self.backend.progress_window(cfg)
        return True

    def _watch(self, runs: list[_LaneRun], start: float, deadline: float, beats: Heartbeat) -> None:
        last_scan = start
        test = f"{self.phase} on CPUs {containment.cpu_list([cpu for run in runs for cpu in run.lane.cpus])}"
        duration = deadline - start
        while not self.stop_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                return
            beats.running(test, now)
            reading = self.sensors()
            if self.freeze is not None:
                frozen = self.freeze(now - start, reading)
                self._set_frozen(runs, frozen is not Frozen.NO, now)
                self._set_held(runs, frozen, now)
            if self.hold_cap is not None:
                in_wall_time = self.hold_cap.in_wall_time
                held = max(run.heat.unbroken(now) if in_wall_time else run.heat.summed(now) for run in runs)
                if held > self.hold_cap.seconds:
                    self._hold_expired(runs)
                    return
                if in_wall_time:
                    deadline = math.inf if any(run.heat.since is not None for run in runs) else start + duration
                else:
                    deadline = start + duration + held
                    for run in runs:
                        run.deadline = deadline
            for run in runs:
                if run.alive:
                    self._check(run, now, start)
            if not self.thermal.safe(reading.hottest):
                temp = self.thermal.last_temperature
                if self.hooks.on_thermal and temp is not None:
                    self.hooks.on_thermal(temp)
                tripped = self.thermal.stop_signal(self.phase)
                for run in runs:
                    run.signals.append(tripped)
            self._take_kernel_events(runs)
            if now - last_scan >= OUTPUT_SCAN_INTERVAL:
                last_scan = now
                for run in runs:
                    if run.alive:
                        finished = self.backend.finished(run.output)
                        self._scan_output(run)
                        self._check_progress(run, now, finished)
            if any(run.faulted for run in runs) or not any(run.alive for run in runs):
                return
            for run in runs:
                if run.alive and self.hooks.on_status:
                    self.hooks.on_status(run.lane.core_id, now - start)
            self.stop_event.wait(self.poll_interval)

    def _check_progress(self, run: _LaneRun, now: float, finished_before: int) -> None:
        if run.progress_window is None:
            return
        clock = run.progress_clock(now)
        if self.backend.finished(run.output) > finished_before:
            run.last_progress = run.progress_clock_at_freeze if run.frozen else clock
        waited = clock - run.last_progress
        if waited > run.progress_window:  # equivalent-mutant: two float clock reads are never equal
            run.signals.append(
                LaneSignal(
                    Outcome.STALL,
                    f"Stress test hung on core {run.lane.core_id}: no test finished for {waited:.0f}s, "
                    f"heat and unread holds not counted, past its {run.progress_window:.0f}s window",
                )
            )

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
                fault = fault or self._containment_fault(run, now)
                if run.cgroup is not None:
                    self._read_oom_kills(run)
            if fault:
                run.signals.append(LaneSignal(Outcome.CONTAINMENT_BROKE, fault))
                return
        if run.frozen or now - start < STALL_GRACE_SECONDS:
            return
        if now - run.cpu_sampled_at > self.stall_timeout:
            run.signals.append(
                LaneSignal(
                    Outcome.UNVERIFIED,
                    f"payload CPU time on CPUs {run.lane.cpu_list} unreadable for "
                    f"{now - run.cpu_sampled_at:.0f}s: verdict unavailable",
                )
            )
        elif run.cpu_sampled_at - run.last_active > self.stall_timeout:
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
        counted = "in one hold" if self.hold_cap.in_wall_time else "in all"
        text = f"the load stayed held more than {self.hold_cap.seconds:g} s {counted}; {self.hold_cap.describe()}"
        for run in runs:
            run.signals.append(LaneSignal(self.hold_cap.outcome, text))

    def _set_frozen(self, runs: list[_LaneRun], frozen: bool, now: float) -> None:
        for run in runs:
            if not run.alive or run.frozen == frozen:
                continue
            if frozen:
                self._sample_cpu(run, now)
                self._signal_group(run, signal.SIGSTOP)
                run.frozen_since = now
                run.progress_clock_at_freeze = run.progress_clock(now)
            else:
                self._signal_group(run, signal.SIGCONT)
                run.frozen_total += max(0.0, min(now, run.deadline) - run.frozen_since)
                run.frozen_in_all += now - run.frozen_since
                run.frozen_since = None
                run.last_active = now
            run.frozen = frozen

    @staticmethod
    def _set_held(runs: list[_LaneRun], frozen: Frozen, now: float) -> None:
        for run in runs:
            if run.alive:
                run.heat.mark(frozen is Frozen.HEAT or (frozen is Frozen.UNREAD and run.heat.since is not None), now)
                run.unread.mark(frozen is Frozen.UNREAD, now)
                run.holds.mark(frozen in (Frozen.HEAT, Frozen.UNREAD), now)

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
        kills = None if run.cgroup is None else containment.scope_oom_kills(run.cgroup)
        if kills is not None:
            run.oom_kills = max(run.oom_kills, kills)
        elif run.read_problem is None:
            run.read_problem = (
                f"the memory.events of scope {run.contained.unit} cannot be read for its OOM kills"
                if run.cgroup
                else f"the payload was never seen in scope {run.contained.unit}, so its OOM kills cannot be read"
            )

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
        run.cpu_last_read_failed = seconds is None
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
            run.signals.append(
                LaneSignal(
                    Outcome.CONTAINMENT_BROKE,
                    f"killing scope {run.contained.unit} failed ({exc}), so a process left in it could load the "
                    "next test: containment fault, not a core verdict",
                )
            )

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
            if run.contained is not None and run.cgroup is None:
                moved = self._learn_scope(run)
                if moved is not None and not run.faulted:
                    run.signals.append(LaneSignal(Outcome.CONTAINMENT_BROKE, moved))
            if run.contained is not None:
                self._read_oom_kills(run)
            if run.proc.returncode is not None:
                continue
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
        elif (short := self._held_too_long_in_wall_time(run)) is not None:
            outcome, detail = Outcome.HELD_TOO_LONG, short
        elif (no_work := _no_work_done(run)) is not None:
            outcome, detail = Outcome.NO_WORK_DONE, no_work
        elif self.must_finish_a_test and self.backend.finished(run.output) == 0:
            outcome, detail = (
                Outcome.STALL,
                f"Stress test hung on core {run.lane.core_id}: no test finished in its {run.duration():.0f}s",
            )
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
            frozen_seconds=_frozen_in_all(run),
        )

    def _held_too_long_in_wall_time(self, run: _LaneRun) -> str | None:
        if self.hold_cap is None or not self.hold_cap.in_wall_time:
            return None
        wall = run.duration()
        unread = run.unread.summed(run.stopped_at)
        if unread > WALL_TIME_UNREAD_LIMIT * wall:
            return (
                f"the hold's reading went unread for {unread:.1f} of {wall:.1f} s, more than the"
                f" {WALL_TIME_UNREAD_LIMIT:.0%} limit for a load held in wall time; {self.hold_cap.describe()}"
            )
        ran = wall - _frozen_in_all(run)
        if ran >= WALL_TIME_RUN_FLOOR * wall:
            return None
        return (
            f"the load ran only {ran:.1f} of {wall:.1f} s, under the {WALL_TIME_RUN_FLOOR:.0%} floor for a load held"
            f" in wall time; {self.hold_cap.describe()}"
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
            found.append(thermal.stop_signal(phase))
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
