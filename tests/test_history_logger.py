"""Tests for history.logger — TestRunLogger with mock-backed HistoryDB."""

from __future__ import annotations

import json

import pytest

from config.settings import TestProfile
from engine.backends.base import StressResult
from engine.scheduler import CoreTestStatus
from engine.topology import CPUTopology, PhysicalCore
from history.db import HistoryDB
from history.logger import TestRunLogger


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def topology():
    """Minimal topology for logger tests."""
    topo = CPUTopology()
    topo.model_name = "AMD Ryzen 9 9950X3D"
    topo.vendor = "AuthenticAMD"
    topo.physical_cores = 8
    topo.logical_cpus_count = 16
    topo.ccds = 2
    topo.is_x3d = True
    for i in range(8):
        topo.cores[i] = PhysicalCore(
            core_id=i, ccd=i // 4, ccx=None, logical_cpus=(i, i + 8)
        )
    return topo


@pytest.fixture
def profile():
    return TestProfile(
        name="Test",
        backend="mprime",
        stress_mode="SSE",
        fft_preset="SMALL",
        seconds_per_core=600,
        cycle_count=2,
    )


@pytest.fixture
def logger(db, topology, profile):
    return TestRunLogger(db, topology, profile)


class TestLoggerInit:
    def test_creates_run_record(self, db, logger):
        run = db.get_run(logger.run_id)
        assert run is not None
        assert run.status == "running"
        assert run.backend == "mprime"
        assert run.stress_mode == "SSE"

    def test_settings_snapshot(self, db, logger):
        run = db.get_run(logger.run_id)
        settings = json.loads(run.settings_json)
        assert settings["backend"] == "mprime"
        assert settings["seconds_per_core"] == 600


class TestCoreLifecycle:
    def test_core_started_creates_result_and_event(self, db, logger):
        logger.on_core_started(3, 0)

        results = db.get_core_results(logger.run_id)
        assert len(results) == 1
        assert results[0].core_id == 3
        assert results[0].cycle == 0
        assert results[0].passed is None

        events = db.get_events(logger.run_id, event_type="core_start")
        assert len(events) == 1
        assert "Core 3" in events[0].message

    def test_core_finished_updates_result(self, db, logger):
        logger.on_core_started(5, 0)

        result = StressResult(
            core_id=5,
            passed=True,
            duration_seconds=600.0,
            iterations_completed=1,
        )
        logger.on_core_finished(5, result)

        results = db.get_core_results(logger.run_id)
        assert len(results) == 1
        assert results[0].passed is True
        assert results[0].elapsed_seconds == 600.0
        assert results[0].finished_at is not None

    def test_core_finished_with_error(self, db, logger):
        logger.on_core_started(2, 0)

        result = StressResult(
            core_id=2,
            passed=False,
            duration_seconds=120.0,
            error_message="MCE detected on CPU 4",
            error_type="mce",
        )
        logger.on_core_finished(2, result)

        results = db.get_core_results(logger.run_id)
        assert results[0].passed is False
        assert results[0].error_message == "MCE detected on CPU 4"
        assert results[0].error_type == "mce"

        events = db.get_events(logger.run_id, event_type="error")
        assert len(events) == 1
        assert "MCE" in events[0].message

    def test_core_finished_without_start_is_noop(self, db, logger):
        """If on_core_finished is called without on_core_started, no crash."""
        result = StressResult(core_id=99, passed=True, duration_seconds=1.0)
        logger.on_core_finished(99, result)
        assert db.get_core_results(logger.run_id) == []


class TestStatusAndCycle:
    def test_status_updated(self, db, logger):
        logger.on_core_started(0, 0)
        status = CoreTestStatus(core_id=0, elapsed_seconds=30.0)
        logger.on_status_updated(0, status)

        results = db.get_core_results(logger.run_id)
        assert results[0].elapsed_seconds == 30.0

    def test_cycle_completed(self, db, logger):
        logger.on_cycle_completed(0)

        events = db.get_events(logger.run_id, event_type="cycle")
        assert len(events) == 1
        assert "Cycle 1" in events[0].message


class TestTestCompletion:
    def test_on_test_completed(self, db, logger):
        # simulate results dict as TestWorker sends it
        results = {
            0: [StressResult(core_id=0, passed=True, duration_seconds=600)],
            1: [StressResult(core_id=1, passed=False, duration_seconds=300, error_message="fail")],
            2: [StressResult(core_id=2, passed=True, duration_seconds=600)],
        }
        logger.on_test_completed(results)

        run = db.get_run(logger.run_id)
        assert run.status == "completed"
        assert run.cores_passed == 2
        assert run.cores_failed == 1
        assert run.total_cores == 3
        assert run.finished_at is not None

    def test_on_test_stopped(self, db, logger):
        logger.on_test_stopped()

        run = db.get_run(logger.run_id)
        assert run.status == "stopped"

        events = db.get_events(logger.run_id, event_type="info")
        assert any("stopped" in e.message.lower() for e in events)


class TestTelemetry:
    def test_record_sample(self, db, logger):
        logger.record_telemetry_sample(0, 5500.0, 75.0, 1.2)
        logger.record_telemetry_sample(0, 5600.0, 76.0, 1.21)

        samples = db.get_telemetry(logger.run_id, core_id=0)
        assert len(samples) == 2
        assert samples[0].freq_mhz == 5500.0
        assert samples[1].temp_c == 76.0

    def test_update_peaks(self, db, logger):
        logger.on_core_started(0, 0)
        logger.update_core_telemetry_peaks(
            0,
            peak_freq_mhz=5800.0,
            max_temp_c=82.0,
            min_vcore_v=1.05,
            max_vcore_v=1.35,
        )

        results = db.get_core_results(logger.run_id)
        assert results[0].peak_freq_mhz == 5800.0
        assert results[0].max_temp_c == 82.0


class TestEventHelpers:
    def test_record_phase_change(self, db, logger):
        logger.record_phase_change(3, "variable load")
        events = db.get_events(logger.run_id, event_type="phase_change")
        assert len(events) == 1
        assert "variable load" in events[0].message

    def test_record_thermal_event(self, db, logger):
        logger.record_thermal_event(95.5)
        events = db.get_events(logger.run_id, event_type="thermal")
        assert len(events) == 1
        assert "95.5" in events[0].message

    def test_record_stall_event(self, db, logger):
        logger.record_stall_event(7)
        events = db.get_events(logger.run_id, event_type="stall")
        assert len(events) == 1
        assert "core 7" in events[0].message.lower()


class TestTuningContext:
    def test_logger_without_smu_creates_context(self, db, topology, profile):
        """Logger without SMU still creates a run with a tuning context."""
        logger = TestRunLogger(db, topology, profile, smu=None)
        run = db.get_run(logger.run_id)
        assert run.context_id is not None

        ctx = db.get_context(run.context_id)
        assert ctx is not None
        # Without SMU, CO offsets should be empty
        assert ctx.co_offsets_json == "{}"
        assert ctx.co_hash == ""

    def test_two_runs_same_context(self, db, topology, profile):
        """Two runs without SMU share the same tuning context."""
        logger1 = TestRunLogger(db, topology, profile, smu=None)
        logger2 = TestRunLogger(db, topology, profile, smu=None)

        run1 = db.get_run(logger1.run_id)
        run2 = db.get_run(logger2.run_id)
        assert run1.context_id == run2.context_id
