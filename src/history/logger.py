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

from history.context import capture_system_context, find_or_create_context
from history.db import (
    CoreResultRecord,
    EventRecord,
    HistoryDB,
    RunRecord,
    TelemetrySample,
)

if TYPE_CHECKING:
    from config.settings import TestProfile
    from engine.backends.base import StressResult
    from engine.scheduler import CoreTestStatus
    from engine.topology import CPUTopology
    from smu.driver import RyzenSMU

log = logging.getLogger(__name__)


class TestRunLogger:
    """Records a single test run into the history database.

    Create one instance per test run in ``_start_test()``, wire it to the
    ``TestWorker`` signals, and discard it in ``_on_worker_finished()``.
    """

    def __init__(
        self,
        db: HistoryDB,
        topology: CPUTopology,
        profile: TestProfile,
        smu: RyzenSMU | None = None,
    ) -> None:
        self._db = db
        self._start_time = time.monotonic()
        self._active_result_ids: dict[int, int] = {}  # core_id → core_results.id

        # Capture tuning context (CO offsets, BIOS version, PBO state)
        ctx = capture_system_context(smu=smu, num_cores=topology.physical_cores)
        context_id = find_or_create_context(db, ctx)
        log.info(
            "Tuning context %d: BIOS=%s, CO hash=%s",
            context_id, ctx.bios_version, ctx.co_hash[:12] if ctx.co_hash else "none",
        )

        # snapshot settings as JSON
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

    # ------------------------------------------------------------------
    # Signal handlers (wire to TestWorker signals)
    # ------------------------------------------------------------------

    def on_core_started(self, core_id: int, cycle: int) -> None:
        rec = CoreResultRecord(
            run_id=self._run_id,
            core_id=core_id,
            cycle=cycle,
        )
        result_id = self._db.insert_core_result(rec)
        self._active_result_ids[core_id] = result_id

        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type="core_start",
                core_id=core_id,
                message=f"Core {core_id} started (cycle {cycle + 1})",
            )
        )

    def on_core_finished(self, core_id: int, result: StressResult) -> None:
        result_id = self._active_result_ids.pop(core_id, None)
        if result_id is None:
            return

        self._db.update_core_result(
            result_id,
            finished_at=HistoryDB._now_iso(),
            passed=result.passed,
            error_message=result.error_message,
            error_type=result.error_type,
            elapsed_seconds=result.duration_seconds,
            iterations_completed=result.iterations_completed,
        )

        event_type = "core_finish" if result.passed else "error"
        state = "PASS" if result.passed else "FAIL"
        msg = f"Core {core_id} {state}"
        if result.error_message:
            msg += f": {result.error_message}"

        self._db.insert_event(
            EventRecord(
                run_id=self._run_id,
                event_type=event_type,
                core_id=core_id,
                message=msg,
            )
        )

    def on_status_updated(self, core_id: int, status: CoreTestStatus) -> None:
        result_id = self._active_result_ids.get(core_id)
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

    def on_test_completed(self, results: dict) -> None:
        total = len(results)
        passed = sum(
            1
            for r_list in results.values()
            if r_list and all(r.passed for r in r_list)
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

    # ------------------------------------------------------------------
    # Telemetry recording (called from _poll_core_telemetry)
    # ------------------------------------------------------------------

    def record_telemetry_sample(
        self,
        core_id: int,
        freq_mhz: float | None,
        temp_c: float | None,
        vcore_v: float | None,
    ) -> None:
        self._db.insert_telemetry_batch(
            [
                TelemetrySample(
                    run_id=self._run_id,
                    core_id=core_id,
                    freq_mhz=freq_mhz,
                    temp_c=temp_c,
                    vcore_v=vcore_v,
                )
            ]
        )

    def update_core_telemetry_peaks(
        self,
        core_id: int,
        *,
        peak_freq_mhz: float | None = None,
        max_temp_c: float | None = None,
        min_vcore_v: float | None = None,
        max_vcore_v: float | None = None,
    ) -> None:
        result_id = self._active_result_ids.get(core_id)
        if result_id is None:
            # Core already finished — look up the last result for this core
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

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

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
