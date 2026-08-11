"""Simultaneous per-core stress: every lane at once through the one engine."""

from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.engine.backends.base import StressResult
from corecycler.engine.detector import ErrorDetector, MCEEvent
from corecycler.engine.execution import Lane, Supervisor, ThermalWatch

if TYPE_CHECKING:
    from corecycler.engine.backends.base import StressBackend
    from corecycler.engine.scheduler import SchedulerConfig
    from corecycler.engine.topology import CPUTopology

log = logging.getLogger(__name__)


class ParallelStress:
    """Runs every configured core's stress process at the same time.

    A failure on any lane stops the whole batch and is attributed to exactly
    that core; lanes without a verdict when the batch stops stay absent from
    the results rather than being invented.
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
        self.config = scheduler_config
        self.work_dir = work_dir or Path("/tmp/corecycler")
        self.detector = ErrorDetector()
        self.observed_mce: list[MCEEvent] = []
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    force_stop = stop

    def run(self) -> dict[int, StressResult]:
        self._stop_event.clear()
        self.observed_mce = []
        self.detector.reset()
        self.work_dir.mkdir(parents=True, exist_ok=True)

        cores = sorted(self.config.cores_to_test or self.topology.cores.keys())
        lanes: list[Lane] = []
        for core_id in cores:
            info = self.topology.cores.get(core_id)
            if not info or not info.logical_cpus:
                return {
                    core_id: StressResult(
                        core_id=core_id,
                        passed=False,
                        duration_seconds=0.0,
                        error_message=f"core {core_id} not in topology",
                        error_type="startup",
                    )
                }
            lanes.append(
                Lane(
                    core_id=core_id,
                    cpus=tuple(sorted(info.logical_cpus)),
                    work_dir=self.work_dir / f"core_{core_id}",
                )
            )
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
            stop_on_first_failure=True,
            phase="parallel stress",
        )

        def config_for(lane: Lane):
            cfg = copy.copy(self.stress_config)
            cfg.threads = len(lane.cpus)
            return cfg

        verdicts = supervisor.run(lanes, config_for, float(self.config.seconds_per_core))

        results: dict[int, StressResult] = {}
        for lane in lanes:
            verdict = verdicts.get(lane.core_id)
            if verdict is not None:
                results[lane.core_id] = verdict
            self.backend.cleanup(
                lane.work_dir,
                preserve_on_error=verdict is not None and not verdict.passed,
            )
        return results
