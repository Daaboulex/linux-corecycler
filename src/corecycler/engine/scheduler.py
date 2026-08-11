"""Per-core cycling orchestration over the one supervised execution engine."""

from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

from corecycler.config.paths import resolve_work_dir
from corecycler.engine import execution
from corecycler.engine.backends.base import StressConfig, StressResult
from corecycler.engine.detector import ErrorDetector, MCEEvent
from corecycler.engine.execution import Lane, SuperviseHooks, Supervisor, ThermalWatch

if TYPE_CHECKING:
    from pathlib import Path

    from corecycler.engine.backends.base import StressBackend

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
        self.observed_mce: list[MCEEvent] = []
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
        self.work_dir.mkdir(parents=True, exist_ok=True)

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
            stop_on_first_failure=True,
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

        self.stress_config.threads = len(lane.cpus)
        start_time = time.monotonic()
        self._set_phase(core_id, "stress")
        supervisor = self._supervisor(f"stress (CPU {lane.cpu_list})")
        verdict = supervisor.run(
            [lane], lambda _lane: self.stress_config, float(self.config.seconds_per_core)
        )[core_id]

        passed = True
        error_msg = None
        if verdict is None:
            status.state = "pending"
            status.current_phase = ""
            self.backend.cleanup(lane.work_dir, preserve_on_error=False)
            return
        if not verdict.passed:
            passed = False
            error_msg = verdict.error_message
            status.errors += 1
            status.last_error = error_msg
            if self.config.stop_on_error:
                self._stop_event.set()

        if passed and self.config.variable_load and not self._stop_event.is_set():
            var_passed, var_error = self._run_variable_load(
                lane, self.config.seconds_per_core / 3.0
            )
            if not var_passed:
                passed = False
                error_msg = var_error
                status.errors += 1
                status.last_error = error_msg

        if passed and self.config.idle_stability_test > 0 and not self._stop_event.is_set():
            idle_error = self._idle_phase(
                core_id, self.config.idle_stability_test, "idle stability"
            )
            if idle_error:
                passed = False
                error_msg = idle_error

        elapsed = time.monotonic() - start_time
        status.elapsed_seconds = elapsed
        status.iterations += 1
        status.state = "passed" if passed else "failed"
        status.current_phase = ""

        result = StressResult(
            core_id=core_id,
            passed=passed,
            duration_seconds=elapsed,
            error_message=error_msg,
            error_type=self._classify_error(error_msg) if error_msg else None,
            iterations_completed=status.iterations,
        )
        self.results[core_id].append(result)
        for cb in self.on_core_finish:
            cb(core_id, result)
        self.backend.cleanup(lane.work_dir, preserve_on_error=not passed)

    def _run_variable_load(self, lane: Lane, total_duration: float) -> tuple[bool, str | None]:
        self._set_phase(lane.core_id, "variable load")
        supervisor = self._supervisor("variable load")
        start = time.monotonic()
        deadline = start + total_duration
        interval = self.config.variable_load_interval
        load_on = True
        while time.monotonic() < deadline and not self._stop_event.is_set():
            segment = min(interval, deadline - time.monotonic())
            if load_on:
                verdict = supervisor.run(
                    [lane], lambda _lane: self.stress_config, segment
                )[lane.core_id]
                if verdict is not None and not verdict.passed:
                    if self.config.stop_on_error:
                        self._stop_event.set()
                    return False, verdict.error_message
            else:
                idle_error = execution.watch_idle(
                    cpus=lane.cpus,
                    duration=segment,
                    thermal=self._thermal or self._new_thermal(),
                    detector=self.detector,
                    stop_event=self._stop_event,
                    observed=self.observed_mce,
                    phase="idle transition",
                )
                if idle_error:
                    if self.config.stop_on_error:
                        self._stop_event.set()
                    return False, idle_error
            load_on = not load_on
            status = self.core_status.get(lane.core_id)
            if status:
                status.elapsed_seconds = time.monotonic() - start
        return True, None

    def _idle_phase(self, core_id: int, duration: float, phase_name: str) -> str | None:
        self._set_phase(core_id, phase_name)
        core_info = self.topology.cores.get(core_id)
        cpus = tuple(core_info.logical_cpus) if core_info else ()
        error = execution.watch_idle(
            cpus=cpus,
            duration=duration,
            thermal=self._thermal or self._new_thermal(),
            detector=self.detector,
            stop_event=self._stop_event,
            observed=self.observed_mce,
            phase=phase_name,
        )
        if error:
            status = self.core_status.get(core_id)
            if status:
                status.errors += 1
                status.last_error = error
            if self.config.stop_on_error:
                self._stop_event.set()
        return error

    def run_rapid_transitions(
        self,
        cores: list[int],
        total_duration: float = 600.0,
        load_seconds: float = 10.0,
        idle_seconds: float = 5.0,
    ) -> tuple[bool, str | None]:
        """Rapid load/idle cycling across cores — catches idle-to-boost instability."""
        self.state = TestState.RUNNING
        if self._stop_event.is_set():
            self.state = TestState.FINISHED
            return True, None
        self.observed_mce = []
        self.detector.reset()
        self._thermal = self._new_thermal()
        core_work_dir = self.work_dir / "rapid_transition"
        core_work_dir.mkdir(parents=True, exist_ok=True)

        logical_ids = []
        for c in cores:
            core_info = self.topology.cores.get(c)
            if core_info and core_info.logical_cpus:
                logical_ids.append(core_info.logical_cpus[0])
        if not logical_ids:
            self.state = TestState.FINISHED
            return False, "Rapid transition harness error: no requested core is in the topology"
        lane = Lane(core_id=min(cores), cpus=tuple(sorted(logical_ids)), work_dir=core_work_dir)

        elapsed = 0.0
        cycle = 0
        try:
            while elapsed < total_duration and not self._stop_event.is_set():
                cycle += 1
                stress_cfg = StressConfig(
                    mode=self.stress_config.mode,
                    fft_preset=self.stress_config.fft_preset,
                    threads=len(cores),
                )
                segment_start = time.monotonic()
                supervisor = self._supervisor("rapid transition")
                verdict = supervisor.run(
                    [lane],
                    lambda _lane, cfg=stress_cfg: cfg,
                    min(load_seconds, total_duration - elapsed),
                )[lane.core_id]
                elapsed += time.monotonic() - segment_start
                if verdict is not None and not verdict.passed:
                    return False, (
                        f"Failure during rapid transition cycle {cycle}: {verdict.error_message}"
                    )
                if self._stop_event.is_set():
                    break
                if elapsed < total_duration:
                    segment_start = time.monotonic()
                    idle_error = execution.watch_idle(
                        cpus=lane.cpus,
                        duration=min(idle_seconds, total_duration - elapsed),
                        thermal=self._thermal,
                        detector=self.detector,
                        stop_event=self._stop_event,
                        observed=self.observed_mce,
                        phase=f"idle phase of rapid transition cycle {cycle}",
                    )
                    elapsed += time.monotonic() - segment_start
                    if idle_error:
                        return False, idle_error
        finally:
            self.backend.cleanup(core_work_dir)
            self.state = TestState.FINISHED
        return True, None

    @staticmethod
    def _classify_error(msg: str | None) -> str:
        return execution.classify_error(msg)

    @staticmethod
    def _read_cpu_temperature() -> float | None:
        return execution.read_cpu_temperature()
