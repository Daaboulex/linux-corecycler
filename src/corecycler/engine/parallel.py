"""Simultaneous per-core stress: every lane at once through the one engine."""

from __future__ import annotations

import copy
import logging
import threading
from typing import TYPE_CHECKING

from corecycler.config.paths import resolve_work_dir
from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.detector import ErrorDetector, KernelEvent
from corecycler.engine.execution import Lane, Supervisor, ThermalWatch

if TYPE_CHECKING:
    from pathlib import Path

    from corecycler.engine.backends.base import StressBackend
    from corecycler.engine.scheduler import SchedulerConfig
    from corecycler.engine.topology import CPUTopology

log = logging.getLogger(__name__)


class ParallelStress:
    """Runs every configured core's stress process at the same time.

    A failure on any lane stops the whole batch and is attributed to exactly
    that core; every lane reaches the results, and a lane the stop cut before
    it had a verdict of its own is CUT_SHORT, never a pass.
    """

    def __init__(
        self,
        topology: CPUTopology,
        backend: StressBackend,
        stress_config,
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
        self.observed_mce: list[KernelEvent] = []
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    force_stop = stop

    def run(self) -> dict[int, StressResult]:
        self._stop_event.clear()
        self.observed_mce = []
        self.work_dir.mkdir(parents=True, exist_ok=True)

        cores = sorted(self.config.cores_to_test or self.topology.cores.keys())
        missing = [core for core in cores if not (info := self.topology.cores.get(core)) or not info.logical_cpus]
        if missing:
            return {core: _refused(core, missing) for core in cores}
        lanes = [
            Lane(
                core_id=core_id,
                cpus=tuple(sorted(self.topology.cores[core_id].logical_cpus)[: self._requested_threads]),
                work_dir=self.work_dir / f"core_{core_id}",
            )
            for core_id in cores
        ]
        if not lanes:
            return {}

        supervisor = Supervisor(
            backend=self.backend,
            detector=self.detector,
            thermal=ThermalWatch(
                max_temperature=self.config.max_temperature,
                grace_seconds=self.config.over_temp_grace_seconds,
                hard_margin=self.config.over_temp_hard_margin,
                require_sensor=self.config.require_thermal_sensor,
            ),
            stop_event=self._stop_event,
            observed=self.observed_mce,
            poll_interval=self.config.poll_interval,
            stall_timeout=self.config.stall_timeout,
            phase="parallel stress",
        )
        self.detector.reset()
        try:
            return supervisor.run(lanes, self.stress_config, float(self.config.seconds_per_core))
        finally:
            self.detector.close()


def _refused(core_id: int, missing: list[int]) -> StressResult:
    named = ", ".join(str(core) for core in missing)
    if core_id in missing:
        outcome, detail = Outcome.NOT_STARTED, f"core {core_id} not in topology"
    else:
        outcome, detail = Outcome.CUT_SHORT, f"not run: the topology lacks core {named} of this batch"
    return StressResult(core_id=core_id, outcome=outcome, detail=detail, duration_seconds=0.0)
