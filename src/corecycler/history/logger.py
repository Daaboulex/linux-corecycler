"""TestRunLogger — connects TestWorker Qt signals to HistoryDB writes.

All signal handlers run on the GUI thread (Qt signal delivery), so there
are no threading concerns.  Each handler does a single auto-commit INSERT
or UPDATE against the WAL-mode SQLite database.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

from corecycler.history.context import capture_system_context, find_or_create_context
from corecycler.history.db import (
    SAMPLE_BLOCK_S,
    CoreResultRecord,
    EventRecord,
    HistoryDB,
    RunRecord,
    TelemetrySample,
)

if TYPE_CHECKING:
    from corecycler.config.settings import TestProfile
    from corecycler.engine.backends.base import StressResult
    from corecycler.engine.scheduler import CoreTestStatus
    from corecycler.engine.topology import CPUTopology
    from corecycler.monitor.hwmon import CoreRead
    from corecycler.smu.driver import RyzenSMU

log = logging.getLogger(__name__)


class TestRunLogger:
    """Records a single test run into the history database.

    Create one instance per test run in ``_start_test()``, wire it to the
    ``TestWorker`` signals, and discard it in ``_on_worker_finished()``.
    """

    __test__ = False

    def __init__(
        self,
        db: HistoryDB,
        topology: CPUTopology,
        profile: TestProfile,
        smu: RyzenSMU | None = None,
    ) -> None:
        self._db = db
        self._start_time = time.monotonic()
        self._unwritten: list[TelemetrySample] = []
        self._unwritten_since = self._start_time
        self._result_id_by_core: dict[int, int] = {}

        ctx = capture_system_context(smu=smu, num_cores=topology.physical_cores)
        context_id = find_or_create_context(db, ctx)
        log.info(
            "Tuning context %s: BIOS=%s, CO hash=%s",
            "none" if context_id is None else context_id,
            ctx.bios_version,
            ctx.co_hash[:12] if ctx.co_hash else "none",
        )

        settings_json = json.dumps(asdict(profile), default=str)

        run = RunRecord(
            status="running",
            cpu_model=topology.model_name,
            physical_cores=topology.physical_cores,
            logical_cpus=topology.logical_cpus_count,
            ccds=topology.ccds,
            is_x3d=topology.is_x3d,
            backend=profile.backend,
            stress_mode=profile.stress_mode,
            fft_preset=profile.fft_preset,
            seconds_per_core=profile.seconds_per_core,
            cycle_count=profile.cycle_count,
            stop_on_error=profile.stop_on_error,
            variable_load=profile.variable_load,
            idle_stability_test=profile.idle_stability_test,
            max_temperature=profile.max_temperature,
            settings_json=settings_json,
            total_cores=topology.physical_cores,
            context_id=context_id,
            bios_version=ctx.bios_version,
        )
        self._run_id = db.create_run(run)

    @property
    def run_id(self) -> int:
        return self._run_id

    def on_core_started(self, core_id: int, cycle: int) -> None:
        rec = CoreResultRecord(
            run_id=self._run_id,
            core_id=core_id,
            cycle=cycle,
        )
        result_id = self._db.insert_core_result(rec)
        self._result_id_by_core[core_id] = result_id

        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type="core_start",
                core_id=core_id,
                message=f"Core {core_id} started (cycle {cycle + 1})",
            )
        )

    def on_core_finished(self, core_id: int, result: StressResult) -> None:
        result_id = self._result_id_by_core.pop(core_id, None)
        if result_id is None:
            return

        self._db.update_core_result(
            result_id,
            finished_at=HistoryDB._now_iso(),
            passed=result.passed,
            error_message=None if result.passed else result.detail,
            error_type=None if result.passed else result.outcome.value,
            elapsed_seconds=result.duration_seconds,
            iterations_completed=result.iterations_completed,
            output=result.output,
        )

        event_type = "core_finish" if result.passed else "error"
        state = "PASS" if result.passed else "FAIL"
        msg = f"Core {core_id} {state}"
        if not result.passed:
            msg += f": {result.detail}"

        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type=event_type,
                core_id=core_id,
                message=msg,
            )
        )

    def on_status_updated(self, core_id: int, status: CoreTestStatus) -> None:
        result_id = self._result_id_by_core.get(core_id)
        if result_id is None:
            return
        self._db.update_core_result(
            result_id,
            elapsed_seconds=status.elapsed_seconds,
        )

    def on_cycle_completed(self, cycle: int) -> None:
        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type="cycle",
                message=f"Cycle {cycle + 1} completed",
            )
        )

    def on_test_completed(self, results_json: str) -> None:
        self.write_telemetry()
        try:
            results = json.loads(results_json)
        except json.JSONDecodeError, TypeError:
            return
        if not isinstance(results, dict):
            return
        tested = {k: v for k, v in results.items() if v}
        total = len(tested)
        passed = sum(
            1
            for r_list in tested.values()
            if isinstance(r_list, list) and r_list and all(isinstance(r, dict) and r.get("passed") for r in r_list)
        )
        failed = total - passed
        elapsed = time.monotonic() - self._start_time

        self._db.finish_run(
            self._run_id,
            status="completed",
            total_cores=total,
            cores_passed=passed,
            cores_failed=failed,
            total_seconds=elapsed,
        )

    def on_test_stopped(self) -> None:
        self.write_telemetry()
        elapsed = time.monotonic() - self._start_time
        self._db.finish_run(
            self._run_id,
            status="stopped",
            total_seconds=elapsed,
        )
        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type="info",
                message="Test stopped by user",
            )
        )

    def record_telemetry_sample(
        self,
        core_id: int,
        freq_mhz: float | None,
        temp_c: float | None,
        vcore_v: float | None,
        effective_max_mhz: float | None = None,
        core_read: CoreRead | None = None,
    ) -> None:
        _, clock, effective, c0, volts = core_read if core_read is not None else (None, None, None, None, None)
        now = time.monotonic()
        if not self._unwritten:
            self._unwritten_since = now
        self._unwritten.append(
            TelemetrySample(
                run_id=self._run_id,
                core_id=core_id,
                timestamp=HistoryDB._now_iso(),
                freq_mhz=freq_mhz,
                effective_max_mhz=effective_max_mhz,
                temp_c=temp_c,
                vcore_v=vcore_v,
                pm_clock_mhz=clock,
                pm_effective_mhz=effective,
                pm_c0_pct=c0,
                pm_volts=volts,
            )
        )
        if now - self._unwritten_since >= SAMPLE_BLOCK_S:
            self.write_telemetry()

    def write_telemetry(self) -> None:
        if self._unwritten:
            self._db.insert_telemetry_block(self._unwritten)
            self._unwritten = []

    def update_core_telemetry_peaks(
        self,
        core_id: int,
        *,
        peak_freq_mhz: float | None = None,
        max_temp_c: float | None = None,
        min_vcore_v: float | None = None,
        max_vcore_v: float | None = None,
    ) -> None:
        result_id = self._result_id_by_core.get(core_id)
        if result_id is None:
            return
        kwargs = {}
        if peak_freq_mhz is not None:
            kwargs["peak_freq_mhz"] = peak_freq_mhz
        if max_temp_c is not None:
            kwargs["max_temp_c"] = max_temp_c
        if min_vcore_v is not None:
            kwargs["min_vcore_v"] = min_vcore_v
        if max_vcore_v is not None:
            kwargs["max_vcore_v"] = max_vcore_v
        if kwargs:
            self._db.update_core_result(result_id, **kwargs)

    def record_phase_change(self, core_id: int, phase: str) -> None:
        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type="phase_change",
                core_id=core_id,
                message=f"Core {core_id} phase: {phase}",
            )
        )

    def record_thermal_event(self, temperature: float) -> None:
        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type="thermal",
                message=f"Thermal limit reached: {temperature:.1f}°C",
            )
        )

    def record_stall_event(self, core_id: int) -> None:
        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type="stall",
                core_id=core_id,
                message=f"Stall detected on core {core_id}",
            )
        )
