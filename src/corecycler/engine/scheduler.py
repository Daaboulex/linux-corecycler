"""Per-core cycling orchestration over the one supervised execution engine."""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING

from corecycler.config.paths import resolve_work_dir
from corecycler.engine import execution
from corecycler.engine.backends.base import LaneSignal, Outcome, OutcomeKind, StressResult
from corecycler.engine.detector import CoreMachineCheck, ErrorDetector, KernelEvent
from corecycler.engine.execution import Lane, SuperviseHooks, Supervisor, ThermalWatch

if TYPE_CHECKING:
    from pathlib import Path

    from corecycler.engine.backends.base import StressBackend, StressConfig

    from .topology import CPUTopology

log = logging.getLogger(__name__)


class TestState(Enum):
    __test__ = False
    IDLE = auto()
    RUNNING = auto()
    STOPPING = auto()
    FINISHED = auto()


@dataclass(slots=True)
class CoreTestStatus:
    core_id: int
    ccd: int | None = None
    state: str = "pending"
    iterations: int = 0
    errors: int = 0
    last_error: str | None = None
    elapsed_seconds: float = 0.0
    current_fft: int | None = None
    current_phase: str = ""


@dataclass(slots=True)
class SchedulerConfig:
    seconds_per_core: int = 360
    cores_to_test: list[int] | None = None
    stop_on_error: bool = False
    cycle_count: int = 1
    poll_interval: float = 1.0
    max_temperature: float = 95.0
    over_temp_grace_seconds: float = 3.0
    over_temp_hard_margin: float = 8.0
    stall_timeout: float = 30.0
    require_thermal_sensor: bool = False
    variable_load: bool = False
    variable_load_interval: float = 15.0
    idle_between_cores: float = 0.0
    idle_stability_test: float = 0.0


class CoreScheduler:
    """Cycles through cores, one supervised contained lane per test."""

    def __init__(
        self,
        topology: CPUTopology,
        backend: StressBackend,
        stress_config: StressConfig,
        scheduler_config: SchedulerConfig,
        work_dir: Path | None = None,
    ) -> None:
        self.topology = topology
        self.backend = backend
        self.stress_config = copy.copy(stress_config)
        self._requested_threads = max(1, self.stress_config.threads)
        self.config = scheduler_config
        self.work_dir = work_dir or resolve_work_dir()
        self.detector = ErrorDetector()

        self.state = TestState.IDLE
        self.results: dict[int, list[StressResult]] = {}
        self.observed_mce: list[KernelEvent] = []
        self.core_status: dict[int, CoreTestStatus] = {}
        self._current_core: int | None = None
        self._current_cycle: int = 0
        self._stop_event = threading.Event()
        self._thermal: ThermalWatch | None = None

        self.on_core_start: list = []
        self.on_core_finish: list = []
        self.on_status_update: list = []
        self.on_cycle_complete: list = []
        self.on_test_complete: list = []
        self.on_thermal_throttle: list = []
        self.on_stall_detected: list = []
        self.on_phase_change: list = []

        self._init_core_status()

    def _init_core_status(self) -> None:
        for core_id in self._get_test_cores():
            core_info = self.topology.cores.get(core_id)
            self.core_status[core_id] = CoreTestStatus(
                core_id=core_id,
                ccd=core_info.ccd if core_info else None,
            )
            self.results[core_id] = []

    def _get_test_cores(self) -> list[int]:
        if self.config.cores_to_test is not None:
            return sorted(self.config.cores_to_test)
        return sorted(self.topology.cores.keys())

    def _new_thermal(self) -> ThermalWatch:
        return ThermalWatch(
            max_temperature=self.config.max_temperature,
            grace_seconds=self.config.over_temp_grace_seconds,
            hard_margin=self.config.over_temp_hard_margin,
            require_sensor=self.config.require_thermal_sensor,
        )

    def run(self) -> dict[int, list[StressResult]]:
        """Run the full test cycle. Blocks until complete. Use run_async() for GUI."""
        self.state = TestState.RUNNING
        self._stop_event.clear()
        self.observed_mce = []
        self.detector.reset()
        self._thermal = self._new_thermal()

        cores = self._get_test_cores()
        try:
            for cycle in range(self.config.cycle_count):
                self._current_cycle = cycle
                if self._stop_event.is_set():
                    break
                for core_id in cores:
                    if self._stop_event.is_set():
                        break
                    self._test_core(core_id, cycle)
                    if self.config.idle_between_cores > 0 and not self._stop_event.is_set():
                        self._idle_phase(core_id, self.config.idle_between_cores, "inter-core idle")
                for cb in self.on_cycle_complete:
                    cb(cycle)
        finally:
            self.detector.close()
            self.state = TestState.FINISHED
            for cb in self.on_test_complete:
                cb(self.results)
        return self.results

    def stop(self) -> None:
        self._stop_event.set()
        self.state = TestState.STOPPING

    force_stop = stop

    @property
    def _stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def _supervisor(self, phase: str, *, stall_timeout: float | None = None) -> Supervisor:
        return Supervisor(
            backend=self.backend,
            detector=self.detector,
            thermal=self._thermal or self._new_thermal(),
            stop_event=self._stop_event,
            observed=self.observed_mce,
            poll_interval=self.config.poll_interval,
            stall_timeout=stall_timeout or self.config.stall_timeout,
            phase=phase,
            hooks=SuperviseHooks(
                on_status=self._hook_status,
                on_stall=self._hook_stall,
                on_thermal=self._hook_thermal,
            ),
        )

    def _hook_status(self, core_id: int, elapsed: float) -> None:
        status = self.core_status.get(core_id)
        if status is None:
            return
        status.elapsed_seconds = elapsed
        for cb in self.on_status_update:
            cb(core_id, status)

    def _hook_stall(self, core_id: int) -> None:
        log.warning("Stall detected on core %d: near-zero usage", core_id)
        for cb in self.on_stall_detected:
            cb(core_id)

    def _hook_thermal(self, temperature: float) -> None:
        log.warning(
            "CPU temperature %.1f C exceeds safety limit %.1f C — stopping test",
            temperature,
            self.config.max_temperature,
        )
        for cb in self.on_thermal_throttle:
            cb(temperature)

    def _set_phase(self, core_id: int, phase: str) -> None:
        status = self.core_status.get(core_id)
        if status:
            status.current_phase = phase
        for cb in self.on_phase_change:
            cb(core_id, phase)

    def _lane_for(self, core_id: int) -> Lane | None:
        core_info = self.topology.cores.get(core_id)
        if not core_info or not core_info.logical_cpus:
            return None
        return Lane(
            core_id=core_id,
            cpus=tuple(core_info.logical_cpus[: self._requested_threads]),
            work_dir=self.work_dir / f"core_{core_id}",
        )

    def _test_core(self, core_id: int, cycle: int) -> None:
        self._current_core = core_id
        status = self.core_status[core_id]
        status.state = "testing"
        for cb in self.on_core_start:
            cb(core_id, cycle)

        lane = self._lane_for(core_id)
        if lane is None:
            status.state = "skipped"
            return

        start_time = time.monotonic()
        self._set_phase(core_id, "stress")
        supervisor = self._supervisor(f"stress (CPU {lane.cpu_list})")
        result = supervisor.run([lane], self.stress_config, float(self.config.seconds_per_core))[core_id]
        if result.passed and self.config.variable_load and not self._stop_event.is_set():
            result = self._run_variable_load(lane, self.config.seconds_per_core / 3.0)
        if result.outcome is Outcome.CUT_SHORT:
            status.state = "pending"
            status.current_phase = ""
            return
        if not result.passed:
            status.errors += 1
            status.last_error = result.detail
        elif self.config.idle_stability_test > 0 and not self._stop_event.is_set():
            idle = self._idle_phase(core_id, self.config.idle_stability_test, "idle stability")
            if idle is not None:
                result = StressResult(core_id=core_id, outcome=idle.outcome, detail=idle.text, duration_seconds=0.0)

        elapsed = time.monotonic() - start_time
        status.elapsed_seconds = elapsed
        status.iterations += 1
        status.state = "passed" if result.passed else "failed"
        status.current_phase = ""
        result = replace(result, duration_seconds=elapsed, iterations_completed=status.iterations)
        if not result.passed and (self.config.stop_on_error or result.outcome.kind is OutcomeKind.ENVIRONMENT):
            self._stop_event.set()

        self.results[core_id].append(result)
        for cb in self.on_core_finish:
            cb(core_id, result)

    def _run_variable_load(self, lane: Lane, total_duration: float) -> StressResult:
        self._set_phase(lane.core_id, "variable load")
        supervisor = self._supervisor("variable load")
        start = time.monotonic()
        deadline = start + total_duration
        interval = self.config.variable_load_interval
        load_on = True
        launched = False
        while time.monotonic() < deadline and not self._stop_event.is_set():
            segment = min(interval, deadline - time.monotonic())
            if load_on:
                if segment < interval:
                    break
                launched = True
                result = supervisor.run([lane], self.stress_config, segment)[lane.core_id]
                if not result.passed:
                    return result
            else:
                idle = execution.watch_idle(
                    cpus=lane.cpus,
                    duration=segment,
                    thermal=self._thermal or self._new_thermal(),
                    detector=self.detector,
                    stop_event=self._stop_event,
                    observed=self.observed_mce,
                    phase="idle transition",
                )
                if idle is not None:
                    return StressResult(
                        core_id=lane.core_id,
                        outcome=idle.outcome,
                        detail=idle.text,
                        duration_seconds=time.monotonic() - start,
                    )
            load_on = not load_on
            status = self.core_status.get(lane.core_id)
            if status:
                status.elapsed_seconds = time.monotonic() - start
        elapsed = time.monotonic() - start
        if self._stop_event.is_set():
            return StressResult(
                core_id=lane.core_id,
                outcome=Outcome.CUT_SHORT,
                detail="variable load phase stopped before its end",
                duration_seconds=elapsed,
            )
        if not launched:
            return StressResult(
                core_id=lane.core_id,
                outcome=Outcome.NO_WORK_DONE,
                detail=(
                    f"variable load phase of {total_duration:.1f}s is shorter than one {interval:.1f}s load "
                    "segment: no work done, verdict unavailable"
                ),
                duration_seconds=elapsed,
            )
        return StressResult(core_id=lane.core_id, outcome=Outcome.PASS, detail="", duration_seconds=elapsed)

    def _idle_phase(self, core_id: int, duration: float, phase_name: str) -> LaneSignal | None:
        self._set_phase(core_id, phase_name)
        core_info = self.topology.cores.get(core_id)
        cpus = tuple(core_info.logical_cpus) if core_info else ()
        found = execution.watch_idle(
            cpus=cpus,
            duration=duration,
            thermal=self._thermal or self._new_thermal(),
            detector=self.detector,
            stop_event=self._stop_event,
            observed=self.observed_mce,
            phase=phase_name,
        )
        if found is not None:
            status = self.core_status.get(core_id)
            if status:
                status.errors += 1
                status.last_error = found.text
            if self.config.stop_on_error or found.outcome.kind is OutcomeKind.ENVIRONMENT:
                self._stop_event.set()
        return found

    def run_rapid_transitions(
        self,
        cores: list[int],
        total_duration: float = 600.0,
        load_seconds: float = 10.0,
        idle_seconds: float = 5.0,
    ) -> dict[int, StressResult]:
        """Rapid load/idle cycling: one single-CPU lane per core, every lane loaded at once, a verdict per core."""
        self.state = TestState.RUNNING
        cores = sorted(set(cores))
        if self._stop_event.is_set() or not cores:
            self.state = TestState.FINISHED
            return _every_core(
                cores, Outcome.CUT_SHORT, "rapid transition run stopped before its first load segment", 0.0
            )
        if refused := execution.refuse_if_missing(self.topology, cores):
            self.state = TestState.FINISHED
            return refused
        self.observed_mce = []
        self._thermal = self._new_thermal()
        lanes = [
            Lane(
                core_id=core,
                cpus=(self.topology.cores[core].logical_cpus[0],),
                work_dir=self.work_dir / "rapid_transition" / f"core_{core}",
            )
            for core in cores
        ]
        idle_cpus = tuple(sorted(cpu for lane in lanes for cpu in lane.cpus))

        elapsed = 0.0
        cycle = 0
        self.detector.reset()
        try:
            while total_duration - elapsed >= load_seconds and not self._stop_event.is_set():
                cycle += 1
                segment_start = time.monotonic()
                supervisor = self._supervisor("rapid transition")
                results = supervisor.run(lanes, self.stress_config, load_seconds)
                elapsed += time.monotonic() - segment_start
                if not all(result.passed for result in results.values()):
                    return _segment_verdicts(results, cycle)
                if elapsed < total_duration:
                    phase = f"idle phase of rapid transition cycle {cycle}"
                    seen = len(self.observed_mce)
                    segment_start = time.monotonic()
                    idle = execution.watch_idle(
                        cpus=idle_cpus,
                        duration=min(idle_seconds, total_duration - elapsed),
                        thermal=self._thermal,
                        detector=self.detector,
                        stop_event=self._stop_event,
                        observed=self.observed_mce,
                        phase=phase,
                    )
                    elapsed += time.monotonic() - segment_start
                    if idle is not None:
                        return _idle_verdicts(lanes, idle, self.observed_mce[seen:], phase, elapsed)
        finally:
            self.detector.close()
            self.state = TestState.FINISHED
        if self._stop_event.is_set():
            return _every_core(cores, Outcome.CUT_SHORT, f"rapid transition run stopped during cycle {cycle}", elapsed)
        if cycle == 0:
            return _every_core(
                cores,
                Outcome.NO_WORK_DONE,
                f"rapid transition run of {total_duration:.1f}s is shorter than one {load_seconds:.1f}s load "
                "segment: no work done, verdict unavailable",
                elapsed,
            )
        return _every_core(cores, Outcome.PASS, "", elapsed)

    @staticmethod
    def _read_cpu_temperature() -> float | None:
        return execution.read_cpu_temperature()


def _every_core(cores: list[int], outcome: Outcome, detail: str, duration: float) -> dict[int, StressResult]:
    return {
        core: StressResult(core_id=core, outcome=outcome, detail=detail, duration_seconds=duration) for core in cores
    }


def _segment_verdicts(results: dict[int, StressResult], cycle: int) -> dict[int, StressResult]:
    failed = [core for core, result in results.items() if not result.passed and result.outcome is not Outcome.CUT_SHORT]
    stopped_by = f"core {', '.join(str(core) for core in failed)}" if failed else "a stop"
    verdicts: dict[int, StressResult] = {}
    for core, result in results.items():
        if core in failed:
            verdicts[core] = replace(result, detail=f"Failure during rapid transition cycle {cycle}: {result.detail}")
        elif result.outcome is Outcome.CUT_SHORT:
            verdicts[core] = result
        else:
            verdicts[core] = replace(
                result,
                outcome=Outcome.CUT_SHORT,
                detail=f"rapid transition cycle {cycle} stopped by {stopped_by}: no verdict of its own",
            )
    return verdicts


def _idle_verdicts(
    lanes: list[Lane], found: LaneSignal, events: list[KernelEvent], phase: str, elapsed: float
) -> dict[int, StressResult]:
    blamed: dict[int, LaneSignal] = {}
    if found.outcome is Outcome.OWN_CORE_MCE:
        machine_checks = [event for event in events if isinstance(event, CoreMachineCheck)]
        for lane in lanes:
            hits = (execution.kernel_signal(event, lane.cpus, phase) for event in machine_checks)
            own = [hit for hit in hits if hit is not None]
            if own:
                blamed[lane.core_id] = own[0]
    if not blamed:
        return _every_core([lane.core_id for lane in lanes], found.outcome, found.text, elapsed)
    named = ", ".join(str(core) for core in blamed)
    cut = LaneSignal(Outcome.CUT_SHORT, f"{phase} stopped by a machine check on core {named}: no verdict of its own")
    verdicts = {lane.core_id: blamed.get(lane.core_id, cut) for lane in lanes}
    return {
        core: StressResult(core_id=core, outcome=signal.outcome, detail=signal.text, duration_seconds=elapsed)
        for core, signal in verdicts.items()
    }
