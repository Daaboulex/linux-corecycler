"""Tests for history.export — JSON and CSV export."""

from __future__ import annotations

import csv
import io
import json

import pytest

from history.db import CoreResultRecord, EventRecord, HistoryDB, RunRecord, TelemetrySample
from history.export import (
    export_run_csv,
    export_run_json,
    export_runs_bulk_csv,
)


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def populated_db(db):
    """DB with one completed run, 2 cores, events, and telemetry."""
    run = RunRecord(
        cpu_model="AMD Ryzen 9 9950X3D",
        physical_cores=16,
        backend="mprime",
        stress_mode="SSE",
        fft_preset="SMALL",
        seconds_per_core=600,
    )
    run_id = db.create_run(run)

    for cid in [0, 1]:
        db.insert_core_result(
            CoreResultRecord(
                run_id=run_id,
                core_id=cid,
                ccd=0,
                cycle=0,
                passed=cid == 0,  # core 0 passes, core 1 fails
                elapsed_seconds=600.0 if cid == 0 else 120.0,
                error_message=None if cid == 0 else "MCE detected",
                error_type=None if cid == 0 else "mce",
                peak_freq_mhz=5800.0,
                max_temp_c=82.0,
                min_vcore_v=1.05,
                max_vcore_v=1.35,
            )
        )

    db.insert_event(
        EventRecord(run_id=run_id, event_type="core_start", core_id=0, message="Core 0 started")
    )
    db.insert_event(
        EventRecord(run_id=run_id, event_type="error", core_id=1, message="MCE detected")
    )

    db.insert_telemetry_batch(
        [
            TelemetrySample(run_id=run_id, core_id=0, freq_mhz=5500, temp_c=75.0, vcore_v=1.2),
            TelemetrySample(run_id=run_id, core_id=0, freq_mhz=5600, temp_c=76.0, vcore_v=1.21),
        ]
    )

    db.finish_run(
        run_id, status="completed", total_cores=2, cores_passed=1, cores_failed=1, total_seconds=720.0
    )

    return db, run_id


class TestJsonExport:
    def test_basic_structure(self, populated_db):
        db, run_id = populated_db
        text = export_run_json(db, run_id)
        data = json.loads(text)

        assert "run" in data
        assert "core_results" in data
        assert "events" in data
        assert data["run"]["cpu_model"] == "AMD Ryzen 9 9950X3D"
        assert len(data["core_results"]) == 2
        assert len(data["events"]) == 2

    def test_without_events(self, populated_db):
        db, run_id = populated_db
        text = export_run_json(db, run_id, include_events=False)
        data = json.loads(text)
        assert "events" not in data

    def test_with_telemetry(self, populated_db):
        db, run_id = populated_db
        text = export_run_json(db, run_id, include_telemetry=True)
        data = json.loads(text)
        assert "telemetry" in data
        assert len(data["telemetry"]) == 2

    def test_without_telemetry(self, populated_db):
        db, run_id = populated_db
        text = export_run_json(db, run_id, include_telemetry=False)
        data = json.loads(text)
        assert "telemetry" not in data

    def test_nonexistent_run(self, db):
        with pytest.raises(ValueError, match="not found"):
            export_run_json(db, 9999)


class TestCsvExport:
    def test_basic_structure(self, populated_db):
        db, run_id = populated_db
        text = export_run_csv(db, run_id)
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["core_id"] == "0"
        assert rows[0]["passed"] == "True"
        assert rows[1]["core_id"] == "1"
        assert rows[1]["passed"] == "False"
        assert rows[1]["error_type"] == "mce"
        assert rows[0]["cpu_model"] == "AMD Ryzen 9 9950X3D"

    def test_nonexistent_run(self, db):
        with pytest.raises(ValueError, match="not found"):
            export_run_csv(db, 9999)


class TestBulkCsvExport:
    def test_multiple_runs(self, db):
        r1 = db.create_run(RunRecord(cpu_model="CPU1", backend="mprime"))
        r2 = db.create_run(RunRecord(cpu_model="CPU2", backend="stress-ng"))

        db.insert_core_result(
            CoreResultRecord(run_id=r1, core_id=0, passed=True, elapsed_seconds=600)
        )
        db.insert_core_result(
            CoreResultRecord(run_id=r2, core_id=0, passed=False, elapsed_seconds=120)
        )

        text = export_runs_bulk_csv(db, [r1, r2])
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)

        assert len(rows) == 2
        assert rows[0]["backend"] == "mprime"
        assert rows[1]["backend"] == "stress-ng"

    def test_empty_list(self, db):
        text = export_runs_bulk_csv(db, [])
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        assert len(rows) == 0

    def test_nonexistent_run_skipped(self, db):
        r1 = db.create_run(RunRecord(cpu_model="exists"))
        db.insert_core_result(CoreResultRecord(run_id=r1, core_id=0))

        text = export_runs_bulk_csv(db, [r1, 9999])
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        assert len(rows) == 1
