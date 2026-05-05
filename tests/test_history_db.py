"""Tests for history.db — HistoryDB with in-memory SQLite."""

from __future__ import annotations

import pytest

from history.db import (
    CoreResultRecord,
    EventRecord,
    HistoryDB,
    RunRecord,
    TelemetrySample,
    TuningContextRecord,
)


@pytest.fixture
def db():
    """In-memory HistoryDB for testing."""
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestSchema:
    def test_schema_created(self, db):
        tables = db._execute_raw(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in tables}
        assert "runs" in names
        assert "core_results" in names
        assert "events" in names
        assert "telemetry_samples" in names
        assert "schema_version" in names

    def test_schema_version_is_current(self, db):
        row = db._execute_raw("SELECT version FROM schema_version").fetchone()
        assert row["version"] == HistoryDB.SCHEMA_VERSION

    def test_foreign_keys_enabled(self, db):
        row = db._execute_raw("PRAGMA foreign_keys").fetchone()
        assert row[0] == 1


class TestRuns:
    def test_create_and_get_run(self, db):
        run = RunRecord(
            cpu_model="AMD Ryzen 9 9950X3D",
            physical_cores=16,
            logical_cpus=32,
            ccds=2,
            is_x3d=True,
            backend="mprime",
            stress_mode="SSE",
            fft_preset="SMALL",
            seconds_per_core=600,
        )
        run_id = db.create_run(run)
        assert run_id > 0
        assert run.id == run_id

        fetched = db.get_run(run_id)
        assert fetched is not None
        assert fetched.cpu_model == "AMD Ryzen 9 9950X3D"
        assert fetched.is_x3d is True
        assert fetched.status == "running"
        assert fetched.started_at != ""

    def test_finish_run(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        db.finish_run(
            run_id,
            status="completed",
            total_cores=16,
            cores_passed=15,
            cores_failed=1,
            total_seconds=9600.0,
        )
        fetched = db.get_run(run_id)
        assert fetched.status == "completed"
        assert fetched.finished_at is not None
        assert fetched.cores_passed == 15
        assert fetched.cores_failed == 1
        assert fetched.total_seconds == 9600.0

    def test_list_runs_ordering(self, db):
        db.create_run(RunRecord(cpu_model="first"))
        db.create_run(RunRecord(cpu_model="second"))
        db.create_run(RunRecord(cpu_model="third"))

        runs = db.list_runs()
        assert len(runs) == 3
        # newest first
        assert runs[0].cpu_model == "third"
        assert runs[2].cpu_model == "first"

    def test_list_runs_limit_offset(self, db):
        for i in range(5):
            db.create_run(RunRecord(cpu_model=f"run-{i}"))

        page = db.list_runs(limit=2, offset=1)
        assert len(page) == 2

    def test_delete_run_cascades(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=0))
        db.insert_event(EventRecord(run_id=run_id, event_type="test", message="hi"))
        db.insert_telemetry_batch(
            [TelemetrySample(run_id=run_id, core_id=0, freq_mhz=5000)]
        )

        db.delete_run(run_id)

        assert db.get_run(run_id) is None
        assert db.get_core_results(run_id) == []
        assert db.get_events(run_id) == []
        assert db.get_telemetry(run_id) == []

    def test_get_nonexistent_run(self, db):
        assert db.get_run(9999) is None


class TestCoreResults:
    def test_insert_and_get(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        rec = CoreResultRecord(
            run_id=run_id,
            core_id=3,
            ccd=0,
            cycle=0,
        )
        result_id = db.insert_core_result(rec)
        assert result_id > 0

        results = db.get_core_results(run_id)
        assert len(results) == 1
        assert results[0].core_id == 3
        assert results[0].passed is None  # not yet finished

    def test_update_core_result(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        result_id = db.insert_core_result(
            CoreResultRecord(run_id=run_id, core_id=0)
        )

        db.update_core_result(
            result_id,
            passed=True,
            elapsed_seconds=600.5,
            peak_freq_mhz=5800.0,
            max_temp_c=82.3,
            min_vcore_v=1.05,
            max_vcore_v=1.35,
        )

        results = db.get_core_results(run_id)
        r = results[0]
        assert r.passed is True
        assert r.elapsed_seconds == 600.5
        assert r.peak_freq_mhz == 5800.0
        assert r.max_temp_c == 82.3
        assert r.min_vcore_v == 1.05
        assert r.max_vcore_v == 1.35

    def test_update_noop(self, db):
        """Updating with no kwargs should not error."""
        run_id = db.create_run(RunRecord(cpu_model="test"))
        result_id = db.insert_core_result(
            CoreResultRecord(run_id=run_id, core_id=0)
        )
        db.update_core_result(result_id)  # no-op

    def test_multiple_cores_ordered(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        for cid in [7, 3, 0, 5]:
            db.insert_core_result(
                CoreResultRecord(run_id=run_id, core_id=cid, cycle=0)
            )

        results = db.get_core_results(run_id)
        core_ids = [r.core_id for r in results]
        assert core_ids == [0, 3, 5, 7]  # sorted by core_id


class TestEvents:
    def test_insert_and_get(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        db.insert_event(
            EventRecord(
                run_id=run_id,
                event_type="core_start",
                core_id=5,
                message="Core 5 started",
            )
        )
        db.insert_event(
            EventRecord(
                run_id=run_id,
                event_type="error",
                core_id=5,
                message="MCE detected",
            )
        )

        events = db.get_events(run_id)
        assert len(events) == 2
        assert events[0].event_type == "core_start"
        assert events[1].event_type == "error"

    def test_filter_by_type(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        db.insert_event(
            EventRecord(run_id=run_id, event_type="core_start", message="a")
        )
        db.insert_event(
            EventRecord(run_id=run_id, event_type="error", message="b")
        )
        db.insert_event(
            EventRecord(run_id=run_id, event_type="core_start", message="c")
        )

        errors = db.get_events(run_id, event_type="error")
        assert len(errors) == 1
        assert errors[0].message == "b"


class TestTelemetry:
    def test_batch_insert_and_get(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        samples = [
            TelemetrySample(run_id=run_id, core_id=0, freq_mhz=5500, temp_c=75.0, vcore_v=1.2),
            TelemetrySample(run_id=run_id, core_id=0, freq_mhz=5600, temp_c=76.0, vcore_v=1.21),
            TelemetrySample(run_id=run_id, core_id=1, freq_mhz=5400, temp_c=74.0, vcore_v=1.19),
        ]
        db.insert_telemetry_batch(samples)

        all_samples = db.get_telemetry(run_id)
        assert len(all_samples) == 3

        core0 = db.get_telemetry(run_id, core_id=0)
        assert len(core0) == 2

        core1 = db.get_telemetry(run_id, core_id=1)
        assert len(core1) == 1

    def test_empty_batch(self, db):
        db.insert_telemetry_batch([])  # should not error


class TestMaintenance:
    def test_recover_incomplete_runs(self, db):
        r1 = db.create_run(RunRecord(cpu_model="running1", status="running"))
        r2 = db.create_run(RunRecord(cpu_model="running2", status="running"))
        db.create_run(RunRecord(cpu_model="completed", status="completed"))

        recovered = db.recover_incomplete_runs()
        assert len(recovered) == 2
        recovered_ids = {r[0] for r in recovered}
        assert r1 in recovered_ids
        assert r2 in recovered_ids
        # Each tuple has (id, started_at)
        for rid, started_at in recovered:
            assert isinstance(started_at, str)
            assert len(started_at) > 0

        runs = db.list_runs()
        statuses = {r.cpu_model: r.status for r in runs}
        assert statuses["running1"] == "crashed"
        assert statuses["running2"] == "crashed"
        assert statuses["completed"] == "completed"

        # second call recovers nothing
        assert db.recover_incomplete_runs() == []

    def test_purge_before(self, db):
        db.create_run(RunRecord(cpu_model="old", started_at="2020-01-01T00:00:00+00:00"))
        db.create_run(RunRecord(cpu_model="new", started_at="2099-01-01T00:00:00+00:00"))

        count = db.purge_before("2025-01-01T00:00:00+00:00")
        assert count == 1

        runs = db.list_runs()
        assert len(runs) == 1
        assert runs[0].cpu_model == "new"

    def test_vacuum(self, db):
        db.vacuum()  # should not error


class TestStatusCounts:
    def test_basic_counts(self, db):
        for _ in range(3):
            rid = db.create_run(RunRecord(cpu_model="test"))
            db.finish_run(rid, status="completed")
        for _ in range(2):
            rid = db.create_run(RunRecord(cpu_model="test"))
            db.finish_run(rid, status="crashed")
        rid = db.create_run(RunRecord(cpu_model="test"))
        db.finish_run(rid, status="stopped")

        counts = db.get_status_counts()
        assert counts["completed"] == 3
        assert counts["crashed"] == 2
        assert counts["stopped"] == 1

    def test_empty_db(self, db):
        counts = db.get_status_counts()
        assert counts == {}

    def test_single_status(self, db):
        for _ in range(5):
            rid = db.create_run(RunRecord(cpu_model="test"))
            db.finish_run(rid, status="completed")
        counts = db.get_status_counts()
        assert counts == {"completed": 5}


class TestTunerSessionMethods:
    """Verify public tuner methods that both Grouped and Tuner views use."""

    def test_list_tuner_sessions(self, db):
        sid1 = db.create_tuner_session("{}", "BIOS-1", "CPU1")
        sid2 = db.create_tuner_session("{}", "BIOS-1", "CPU1")
        sessions = db.list_tuner_sessions()
        assert len(sessions) == 2
        assert sessions[0].id == sid2  # newest first
        assert sessions[1].id == sid1

    def test_list_tuner_sessions_limit(self, db):
        for _ in range(5):
            db.create_tuner_session("{}", "", "")
        sessions = db.list_tuner_sessions(limit=3)
        assert len(sessions) == 3

    def test_delete_context_cascade(self, db):
        ctx_id = db.create_context(TuningContextRecord(bios_version="v1"))
        db.create_run(RunRecord(cpu_model="test", context_id=ctx_id))
        sid = db.create_tuner_session("{}", "v1", "test", context_id=ctx_id)

        db.delete_context_cascade(ctx_id)

        runs = db.list_runs_for_context(ctx_id)
        assert len(runs) == 0
        assert db.get_tuner_session(sid) is None
        assert db.get_context(ctx_id) is None


class TestBooleanConversion:
    """Verify bool fields survive the SQLite INTEGER round-trip."""

    def test_run_booleans(self, db):
        run_id = db.create_run(
            RunRecord(
                cpu_model="test",
                is_x3d=True,
                stop_on_error=True,
                variable_load=True,
            )
        )
        fetched = db.get_run(run_id)
        assert fetched.is_x3d is True
        assert fetched.stop_on_error is True
        assert fetched.variable_load is True

    def test_core_result_passed_none(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        rid = db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=0))
        results = db.get_core_results(run_id)
        assert results[0].passed is None

    def test_core_result_passed_true_false(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        r1 = db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=0))
        r2 = db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=1))
        db.update_core_result(r1, passed=True)
        db.update_core_result(r2, passed=False)

        results = db.get_core_results(run_id)
        by_core = {r.core_id: r for r in results}
        assert by_core[0].passed is True
        assert by_core[1].passed is False


class TestTuningContexts:
    def test_create_and_get(self, db):
        ctx = TuningContextRecord(
            bios_version="2101",
            co_offsets_json='{"0":-30,"1":-20}',
            co_hash="abc123",
            pbo_scalar=1.0,
            boost_limit_mhz=5700,
        )
        ctx_id = db.create_context(ctx)
        assert ctx_id > 0
        assert ctx.id == ctx_id

        fetched = db.get_context(ctx_id)
        assert fetched is not None
        assert fetched.bios_version == "2101"
        assert fetched.co_hash == "abc123"
        assert fetched.pbo_scalar == 1.0
        assert fetched.boost_limit_mhz == 5700
        assert fetched.created_at != ""

    def test_get_nonexistent(self, db):
        assert db.get_context(9999) is None

    def test_get_by_hash(self, db):
        db.create_context(
            TuningContextRecord(bios_version="2101", co_hash="hash1")
        )
        db.create_context(
            TuningContextRecord(bios_version="2201", co_hash="hash2")
        )

        found = db.get_context_by_hash("hash1", "2101")
        assert found is not None
        assert found.co_hash == "hash1"

        assert db.get_context_by_hash("hash1", "2201") is None
        assert db.get_context_by_hash("missing", "2101") is None

    def test_list_contexts_ordering(self, db):
        db.create_context(TuningContextRecord(bios_version="first"))
        db.create_context(TuningContextRecord(bios_version="second"))
        db.create_context(TuningContextRecord(bios_version="third"))

        contexts = db.list_contexts()
        assert len(contexts) == 3
        assert contexts[0].bios_version == "third"  # newest first
        assert contexts[2].bios_version == "first"

    def test_update_notes(self, db):
        ctx_id = db.create_context(TuningContextRecord(bios_version="2101"))
        db.update_context_notes(ctx_id, "trying aggressive CO")

        fetched = db.get_context(ctx_id)
        assert fetched.notes == "trying aggressive CO"

    def test_run_with_context(self, db):
        ctx_id = db.create_context(TuningContextRecord(bios_version="2101"))
        run_id = db.create_run(
            RunRecord(cpu_model="test", context_id=ctx_id, bios_version="2101")
        )

        fetched = db.get_run(run_id)
        assert fetched.context_id == ctx_id
        assert fetched.bios_version == "2101"

    def test_list_runs_for_context(self, db):
        ctx1 = db.create_context(TuningContextRecord(bios_version="2101"))
        ctx2 = db.create_context(TuningContextRecord(bios_version="2201"))

        db.create_run(RunRecord(cpu_model="a", context_id=ctx1))
        db.create_run(RunRecord(cpu_model="b", context_id=ctx1))
        db.create_run(RunRecord(cpu_model="c", context_id=ctx2))

        runs_ctx1 = db.list_runs_for_context(ctx1)
        assert len(runs_ctx1) == 2

        runs_ctx2 = db.list_runs_for_context(ctx2)
        assert len(runs_ctx2) == 1
        assert runs_ctx2[0].cpu_model == "c"

    def test_run_without_context(self, db):
        """Runs without a context (legacy/no SMU) still work."""
        run_id = db.create_run(RunRecord(cpu_model="test"))
        fetched = db.get_run(run_id)
        assert fetched.context_id is None
        assert fetched.bios_version == ""


class TestSchemaV2:
    def test_schema_version_is_current(self, db):
        row = db._execute_raw("SELECT version FROM schema_version").fetchone()
        assert row["version"] == HistoryDB.SCHEMA_VERSION

    def test_tuning_contexts_table_exists(self, db):
        tables = db._execute_raw(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = {r["name"] for r in tables}
        assert "tuning_contexts" in names


class TestMigrationV1ToV2:
    def test_migration(self):
        """Create a v1 database, then open with v2 code — migration should run."""
        import sqlite3

        db_path = ":memory:"
        # We can't use :memory: across connections, so use a temp file
        import tempfile
        import os

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            # Create v1 schema manually
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript("""\
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version (version) VALUES (1);
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    cpu_model TEXT NOT NULL DEFAULT '',
    physical_cores INTEGER NOT NULL DEFAULT 0,
    logical_cpus INTEGER NOT NULL DEFAULT 0,
    ccds INTEGER NOT NULL DEFAULT 0,
    is_x3d INTEGER NOT NULL DEFAULT 0,
    backend TEXT NOT NULL DEFAULT '',
    stress_mode TEXT NOT NULL DEFAULT '',
    fft_preset TEXT NOT NULL DEFAULT '',
    seconds_per_core INTEGER NOT NULL DEFAULT 0,
    cycle_count INTEGER NOT NULL DEFAULT 1,
    stop_on_error INTEGER NOT NULL DEFAULT 0,
    variable_load INTEGER NOT NULL DEFAULT 0,
    idle_stability_test REAL NOT NULL DEFAULT 0.0,
    max_temperature REAL NOT NULL DEFAULT 95.0,
    settings_json TEXT NOT NULL DEFAULT '{}',
    total_cores INTEGER NOT NULL DEFAULT 0,
    cores_passed INTEGER NOT NULL DEFAULT 0,
    cores_failed INTEGER NOT NULL DEFAULT 0,
    total_seconds REAL NOT NULL DEFAULT 0.0
);
CREATE TABLE core_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    core_id INTEGER NOT NULL,
    ccd INTEGER,
    cycle INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    passed INTEGER,
    error_message TEXT,
    error_type TEXT,
    elapsed_seconds REAL NOT NULL DEFAULT 0.0,
    iterations_completed INTEGER NOT NULL DEFAULT 0,
    peak_freq_mhz REAL,
    max_temp_c REAL,
    min_vcore_v REAL,
    max_vcore_v REAL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    core_id INTEGER,
    message TEXT NOT NULL DEFAULT '',
    details_json TEXT
);
CREATE TABLE telemetry_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    core_id INTEGER NOT NULL,
    timestamp TEXT NOT NULL,
    freq_mhz REAL,
    temp_c REAL,
    vcore_v REAL
);
""")
            # Insert a v1 run
            conn.execute(
                "INSERT INTO runs (started_at, cpu_model, backend) VALUES (?, ?, ?)",
                ("2025-01-01T00:00:00+00:00", "old-cpu", "mprime"),
            )
            conn.close()

            # Open with HistoryDB (should migrate to v2)
            db = HistoryDB(db_path)

            # Verify migration
            version = db._execute_raw("SELECT version FROM schema_version").fetchone()[0]
            assert version == HistoryDB.SCHEMA_VERSION

            # tuning_contexts table exists
            tables = db._execute_raw(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            assert "tuning_contexts" in {r["name"] for r in tables}

            # Old run is still accessible with new fields defaulted
            runs = db.list_runs()
            assert len(runs) == 1
            assert runs[0].cpu_model == "old-cpu"
            assert runs[0].context_id is None
            assert runs[0].bios_version == ""

            # Can create new runs with context
            ctx_id = db.create_context(TuningContextRecord(bios_version="2101"))
            run_id = db.create_run(
                RunRecord(cpu_model="new-cpu", context_id=ctx_id, bios_version="2101")
            )
            fetched = db.get_run(run_id)
            assert fetched.context_id == ctx_id

            db.close()
        finally:
            os.unlink(db_path)
