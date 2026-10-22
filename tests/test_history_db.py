"""Tests for history.db — HistoryDB with in-memory SQLite."""

from __future__ import annotations

import json
import random
import sqlite3
from compression import zstd
from dataclasses import replace

import pytest

from corecycler.history.db import (
    CoreResultRecord,
    EventRecord,
    HistoryDB,
    RunRecord,
    TelemetrySample,
    TuningContextRecord,
)
from corecycler.tuner.live import own_peaks
from tests._machine import this_boot
from tests._old_rows import seed_old_session

AT = "2026-10-07T10:00:00+00:00"


@pytest.fixture
def db():
    """In-memory HistoryDB for testing."""
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestSchema:
    def test_schema_created(self, db):
        tables = db._execute_raw("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        names = {r["name"] for r in tables}
        assert "runs" in names
        assert "core_results" in names
        assert "events" in names
        assert "telemetry_blocks" in names
        assert "tuner_sample_blocks" in names
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

    def test_list_runs_is_newest_first(self, db):
        db.create_run(RunRecord(cpu_model="first"))
        db.create_run(RunRecord(cpu_model="second"))
        db.create_run(RunRecord(cpu_model="third"))

        runs = db.list_runs()
        assert len(runs) == 3
        assert runs[0].cpu_model == "third"
        assert runs[2].cpu_model == "first"

    @pytest.mark.req("T23.history.uncapped")
    def test_list_runs_returns_every_run(self, db):
        for i in range(101):
            db.create_run(RunRecord(cpu_model=f"run-{i}"))
        runs = db.list_runs()
        assert (len(runs), runs[0].cpu_model, runs[-1].cpu_model) == (101, "run-100", "run-0")

    def test_delete_run_cascades(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=0))
        db.insert_event(EventRecord(run_id=run_id, event_type="test", message="hi"))
        db.insert_telemetry_block([TelemetrySample(run_id=run_id, core_id=0, timestamp=AT, freq_mhz=5000)])

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
        assert results[0].passed is None

    def test_update_core_result(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        result_id = db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=0))

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

    def test_an_update_with_no_fields_changes_nothing(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        result_id = db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=0))
        before = db.get_core_results(run_id)
        db.update_core_result(result_id)
        assert db.get_core_results(run_id) == before

    def test_multiple_cores_ordered(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        for cid in [7, 3, 0, 5]:
            db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=cid, cycle=0))

        results = db.get_core_results(run_id)
        core_ids = [r.core_id for r in results]
        assert core_ids == [0, 3, 5, 7]


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
        db.insert_event(EventRecord(run_id=run_id, event_type="core_start", message="a"))
        db.insert_event(EventRecord(run_id=run_id, event_type="error", message="b"))
        db.insert_event(EventRecord(run_id=run_id, event_type="core_start", message="c"))

        errors = db.get_events(run_id, event_type="error")
        assert len(errors) == 1
        assert errors[0].message == "b"


class TestTelemetry:
    def test_blocks_read_back_in_order_and_by_core(self, db):
        run_id = db.create_run(RunRecord(cpu_model="test"))
        samples = [
            TelemetrySample(run_id=run_id, core_id=0, timestamp=AT, freq_mhz=5500, temp_c=75.0, vcore_v=1.2),
            TelemetrySample(run_id=run_id, core_id=0, timestamp=AT, freq_mhz=5600, temp_c=76.0, vcore_v=1.21),
            TelemetrySample(run_id=run_id, core_id=1, timestamp=AT, freq_mhz=5400, temp_c=74.0, vcore_v=1.19),
        ]
        db.insert_telemetry_block(samples[:2])
        db.insert_telemetry_block(samples[2:])

        assert db.get_telemetry(run_id) == samples
        assert db.get_telemetry(run_id, core_id=0) == samples[:2]
        assert db.get_telemetry(run_id, core_id=1) == samples[2:]

    def test_a_block_holds_one_run_and_only_stamped_samples(self, db):
        first, second = (db.create_run(RunRecord(cpu_model="test")) for _ in range(2))
        with pytest.raises(ValueError, match="exactly one run"):
            db.insert_telemetry_block([])
        with pytest.raises(ValueError, match="exactly one run"):
            db.insert_telemetry_block([TelemetrySample(run_id=run, core_id=0, timestamp=AT) for run in (first, second)])
        with pytest.raises(ValueError, match="no timestamp"):
            db.insert_telemetry_block([TelemetrySample(run_id=first, core_id=0)])
        assert db.get_telemetry(first) == db.get_telemetry(second) == []


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
        for _rid, started_at in recovered:
            assert isinstance(started_at, str)
            assert len(started_at) > 0

        runs = db.list_runs()
        statuses = {r.cpu_model: r.status for r in runs}
        assert statuses["running1"] == "crashed"
        assert statuses["running2"] == "crashed"
        assert statuses["completed"] == "completed"

        assert db.recover_incomplete_runs() == []

    def test_purge_before(self, db):
        db.create_run(RunRecord(cpu_model="old", started_at="2020-01-01T00:00:00+00:00"))
        db.create_run(RunRecord(cpu_model="new", started_at="2099-01-01T00:00:00+00:00"))

        count = db.purge_before("2025-01-01T00:00:00+00:00")
        assert count == 1

        runs = db.list_runs()
        assert len(runs) == 1
        assert runs[0].cpu_model == "new"

    def test_an_older_database_moves_to_incremental_vacuum_and_gives_back_its_free_pages(self, tmp_path):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        conn = sqlite3.connect(path, autocommit=True)
        conn.execute("PRAGMA auto_vacuum=NONE")
        conn.execute("VACUUM")
        conn.execute("CREATE TABLE filler (x BLOB)")
        conn.executemany("INSERT INTO filler VALUES (zeroblob(4000))", [()] * 50)
        conn.execute("DROP TABLE filler")
        assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0
        conn.close()
        db = HistoryDB(path)
        try:
            assert db._execute_raw("PRAGMA auto_vacuum").fetchone()[0] == 2
            assert db._execute_raw("PRAGMA freelist_count").fetchone()[0] == 0
        finally:
            db.close()

    @pytest.mark.req("D72")
    def test_only_the_samples_of_tests_that_passed_before_the_cutoff_go(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "2401", "cpu_model": "9950X3D", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        noise = random.Random(7)
        rows = {}
        for name, verdict, ended in (
            ("old pass", "pass", "2026-08-01T00:00:00+00:00"),
            ("new pass", "pass", "2026-10-01T00:00:00+00:00"),
            ("old fail", "silicon", "2026-08-01T00:00:00+00:00"),
            ("open", None, None),
        ):
            row = db.insert_tuner_test(sid, "a", len(rows) + 1, "steady", "hot", "1t", 0, -5, "[]", "[0]", 60.0, "{}")
            filler = [
                {
                    "mono_ns": n,
                    "running": True,
                    "tctl": 60.0,
                    "clocks": [[c, noise.random()] for c in range(16)],
                    "tccd": [],
                    "volts": [],
                    "watts": [],
                    "cores": [],
                }
                for n in range(500)
            ]
            db.insert_tuner_block(row, filler)
            db._execute_raw("UPDATE tuner_tests SET verdict=?, ended_at=? WHERE id=?", (verdict, ended, row))
            rows[name] = row
        assert db._execute_raw("PRAGMA page_count").fetchone()[0] > 20
        db.prune_tuner_samples("pass", "2026-09-07T00:00:00+00:00")
        kept = {name for name, row in rows.items() if db.tuner_test_samples(row)}
        assert kept == {"new pass", "old fail", "open"}
        assert db._execute_raw("SELECT count(*) FROM tuner_tests").fetchone()[0] == 4
        assert db._execute_raw("PRAGMA freelist_count").fetchone()[0] == 0


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


class TestMachineSessionMethods:
    """Verify public tuner methods that both Grouped and Tuner views use."""

    @pytest.mark.req("T23.history.uncapped")
    def test_list_machine_sessions_returns_every_session_or_the_newest_asked_for(self, db):
        for number in range(1, 52):
            db.save_machine_session(
                number,
                status="paused",
                config_json="{}",
                machine_json="{}",
                boot_id="b",
                bios_version="bios-1",
                cpu_model="cpu-1",
                context_id=None,
            )
        assert [s.id for s in db.list_machine_sessions()] == list(range(51, 0, -1))
        assert [s.id for s in db.list_machine_sessions(limit=2)] == [51, 50]

    def test_machine_readers_never_see_an_old_engine_row(self, db):
        old = seed_old_session(db)
        machine = db.next_tuner_session_id()
        db.save_machine_session(
            machine,
            status="running",
            config_json="{}",
            machine_json='{"m": 1}',
            boot_id=this_boot(),
            bios_version="BIOS-1",
            cpu_model="CPU1",
            context_id=None,
        )
        assert machine == old + 1
        assert db.load_machine_session(machine) == ('{"m": 1}', this_boot(), None, None)
        assert db.load_machine_session(old) is None
        assert [s.id for s in db.list_machine_sessions()] == [machine]

    def test_a_machine_session_never_overwrites_an_old_engine_row(self, db):
        old = seed_old_session(db)
        with pytest.raises(sqlite3.IntegrityError, match="belongs to the old engine"):
            db.save_machine_session(
                old,
                status="running",
                config_json="{}",
                machine_json="{}",
                boot_id=this_boot(),
                bios_version="B",
                cpu_model="C",
                context_id=None,
            )
        kept = db._execute_raw("SELECT status, bios_version, machine_json FROM tuner_sessions WHERE id=?", (old,))
        assert tuple(kept.fetchone()) == ("completed", "2402", None)

    def test_a_test_row_ends_once(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        row = db.insert_tuner_test(sid, "a", 1, "search", None, "1t", 0, -5, "[]", "[0]", 60.0, "{}")
        db.end_tuner_test(row, "pass", 60.0, "[]", None, "{}", "", "[]", "")
        with pytest.raises(sqlite3.IntegrityError, match="already ended"):
            db.end_tuner_test(row, "silicon", 1.0, "[]", None, "{}", "", "[]", "")
        with pytest.raises(sqlite3.IntegrityError, match="missing"):
            db.end_tuner_test(row + 1, "pass", 1.0, "[]", None, "{}", "", "[]", "")

    @pytest.mark.req("D77", "SC.3")
    def test_a_block_takes_its_test_s_tail_and_samples_read_back_in_order(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        row, other = (
            db.insert_tuner_test(sid, "a", n, "steady", "hot", "1t", 0, -5, "[]", "[0]", 60.0, "{}") for n in (1, 2)
        )
        taken = [
            {
                "mono_ns": n,
                "running": n % 2 == 0,
                "tctl": None,
                "clocks": [[0, 5000.0]],
                "tccd": [],
                "volts": [],
                "watts": [],
                "cores": [],
            }
            for n in range(4)
        ]
        db.insert_tuner_samples(row, taken[:2])
        db.insert_tuner_samples(other, taken[:1])
        db.insert_tuner_block(row, taken[:2])
        db.insert_tuner_samples(row, taken[2:])
        assert db.tuner_test_samples(row) == taken
        assert db.tuner_test_samples(other) == taken[:1]

    def test_a_boot_is_recorded_once_and_its_first_record_stands(self, db):
        db.insert_tuner_boot(
            "b1", "2401", 2033.0, 3000.0, 3000.0, '{"lines": [], "problem": null}', '{"present": false}'
        )
        db.insert_tuner_boot("b1", "2402", None, None, None, '{"lines": [], "problem": "x"}', '{"present": true}')
        rows = [dict(r) for r in db._execute_raw("SELECT * FROM tuner_boots").fetchall()]
        assert [(r["boot_id"], r["bios_version"], r["fclk_mhz"], r["igpu_json"]) for r in rows] == [
            ("b1", "2401", 2033.0, '{"present": false}')
        ]
        assert rows[0]["first_seen"]

    def test_a_child_session_links_to_its_parent_and_inherits_proven_values(self, db):
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        for sid in (1, 2):
            db.save_machine_session(sid, status="paused", machine_json="1", boot_id="a", **fields)
        db.link_session(2, 1, "continue")
        db.journal_inherit(2, {0: -12, 1: -6})
        linked = db._execute_raw("SELECT parent_session_id, origin FROM tuner_sessions WHERE id=2").fetchone()
        assert tuple(linked) == (1, "continue")
        assert db.journal_proven_values(2, -1) == {0: -12, 1: -6}
        assert db.journal_rows(2, "a") == []
        with pytest.raises(sqlite3.IntegrityError, match="session 3 is missing"):
            db.link_session(3, 1, "continue")

    def test_a_damaged_block_is_refused_rather_than_read(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        row = db.insert_tuner_test(sid, "a", 1, "steady", "hot", "1t", 0, -5, "[]", "[0]", 60.0, "{}")
        db.insert_tuner_block(
            row,
            [
                {
                    "mono_ns": 1,
                    "running": True,
                    "tctl": 60.0,
                    "clocks": [],
                    "tccd": [],
                    "volts": [],
                    "watts": [],
                    "cores": [],
                }
            ],
        )
        data = bytearray(db._execute_raw("SELECT data FROM tuner_sample_blocks").fetchone()[0])
        data[-1] ^= 1
        db._execute_raw("UPDATE tuner_sample_blocks SET data=?", (bytes(data),))
        with pytest.raises(zstd.ZstdError, match="checksum"):
            db.tuner_test_samples(row)

    def test_a_machine_session_row_follows_its_later_commits(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        db.save_machine_session(sid, status="paused", machine_json="2", boot_id="b", **fields)
        row = db._execute_raw("SELECT status, machine_json, boot_id FROM tuner_sessions WHERE id=?", (sid,))
        assert dict(row.fetchone()) == {"status": "paused", "machine_json": "2", "boot_id": "b"}
        assert db.next_tuner_session_id() == sid + 1

    def test_only_a_core_s_latest_landed_write_is_marked_survived(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        landed = db.journal_co_intent(sid, 0, -3, False, boot_id="a", mono_ns=1)
        db.journal_confirm(landed)
        refused = db.journal_co_intent(sid, 0, -5, False, boot_id="a", mono_ns=2)
        other = db.journal_co_intent(sid, 1, -2, False, boot_id="a", mono_ns=3)
        db.journal_confirm(other)
        db.journal_mark_survived(sid, boot_id="a")
        survived = dict(db._execute_raw("SELECT id, survived FROM tuner_co_journal").fetchall())
        assert survived == {landed: 0, refused: 0, other: 1}

    def test_a_kernel_read_point_lives_only_as_long_as_its_boot(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        db.set_kernel_read(sid, 5_000)
        db.save_machine_session(sid, status="running", machine_json="2", boot_id="a", **fields)
        assert db.load_machine_session(sid) == ("2", "a", 5_000, None)
        db.save_machine_session(sid, status="running", machine_json="3", boot_id="b", **fields)
        assert db.load_machine_session(sid) == ("3", "b", None, None)

    @pytest.mark.req("L18.d.3.once")
    def test_a_kernel_read_point_only_moves_forward_within_its_boot(self, db):
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        db.save_machine_session(sid, status="running", machine_json="1", boot_id="a", **fields)
        db.set_kernel_read(sid, 5_000)
        db.set_kernel_read(sid, 4_000)
        assert db.load_machine_session(sid)[2] == 5_000
        db.set_kernel_read(sid, 6_000)
        assert db.load_machine_session(sid)[2] == 6_000
        db.save_machine_session(sid, status="running", machine_json="2", boot_id="b", **fields)
        db.set_kernel_read(sid, 1_000)
        assert db.load_machine_session(sid)[2] == 1_000

    def test_delete_context_cascade(self, db):
        ctx_id = db.create_context(TuningContextRecord(bios_version="v1"))
        db.create_run(RunRecord(cpu_model="test", context_id=ctx_id))

        db.delete_context_cascade(ctx_id)

        assert db.list_runs_for_context(ctx_id) == []
        assert db.get_context(ctx_id) is None

    @pytest.mark.req("T23.history.delete")
    def test_a_setup_that_tuner_sessions_name_is_never_deleted_with_them(self, db):
        ctx_id = db.create_context(TuningContextRecord(bios_version="v1"))
        db.create_run(RunRecord(cpu_model="test", context_id=ctx_id))
        sid = seed_old_session(db)
        db._execute_raw("UPDATE tuner_sessions SET context_id=? WHERE id=?", (ctx_id, sid))

        with pytest.raises(ValueError, match=rf"setup {ctx_id} holds tuner sessions \[{sid}\]"):
            db.delete_context_cascade(ctx_id)

        assert db.context_sessions(ctx_id) == [sid]
        assert len(db.list_runs_for_context(ctx_id)) == 1
        assert db.get_context(ctx_id) is not None


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
        db.insert_core_result(CoreResultRecord(run_id=run_id, core_id=0))
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
        db.create_context(TuningContextRecord(bios_version="2101", co_hash="hash1"))
        db.create_context(TuningContextRecord(bios_version="2201", co_hash="hash2"))

        found = db.get_context_by_hash("hash1", "2101")
        assert found is not None
        assert found.co_hash == "hash1"

        assert db.get_context_by_hash("hash1", "2201") is None
        assert db.get_context_by_hash("missing", "2101") is None

    def test_list_contexts_is_newest_first(self, db):
        db.create_context(TuningContextRecord(bios_version="first"))
        db.create_context(TuningContextRecord(bios_version="second"))
        db.create_context(TuningContextRecord(bios_version="third"))

        contexts = db.list_contexts()
        assert len(contexts) == 3
        assert contexts[0].bios_version == "third"
        assert contexts[2].bios_version == "first"

    @pytest.mark.req("T23.history.uncapped")
    def test_list_contexts_returns_every_context_or_the_newest_asked_for(self, db):
        for i in range(101):
            db.create_context(TuningContextRecord(bios_version=f"b{i}"))
        assert len(db.list_contexts()) == 101
        assert [c.bios_version for c in db.list_contexts(limit=2)] == ["b100", "b99"]

    def test_update_notes(self, db):
        ctx_id = db.create_context(TuningContextRecord(bios_version="2101"))
        db.update_context_notes(ctx_id, "trying aggressive CO")

        fetched = db.get_context(ctx_id)
        assert fetched.notes == "trying aggressive CO"

    def test_run_with_context(self, db):
        ctx_id = db.create_context(TuningContextRecord(bios_version="2101"))
        run_id = db.create_run(RunRecord(cpu_model="test", context_id=ctx_id, bios_version="2101"))

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
        tables = db._execute_raw("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        names = {r["name"] for r in tables}
        assert "tuning_contexts" in names


_V1_SCHEMA = """\
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
"""


class TestMigrationV1ToV2:
    def test_a_v1_database_migrates_to_the_current_schema_with_its_runs_kept(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "v1.db"
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_V1_SCHEMA)
        conn.execute(
            "INSERT INTO runs (started_at, cpu_model, backend) VALUES (?, ?, ?)",
            ("2025-01-01T00:00:00+00:00", "old-cpu", "mprime"),
        )
        conn.close()

        db = HistoryDB(db_path)

        version = db._execute_raw("SELECT version FROM schema_version").fetchone()[0]
        assert version == HistoryDB.SCHEMA_VERSION

        tables = db._execute_raw("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert "tuning_contexts" in {r["name"] for r in tables}

        runs = db.list_runs()
        assert len(runs) == 1
        assert runs[0].cpu_model == "old-cpu"
        assert runs[0].context_id is None
        assert runs[0].bios_version == ""

        ctx_id = db.create_context(TuningContextRecord(bios_version="2101"))
        run_id = db.create_run(RunRecord(cpu_model="new-cpu", context_id=ctx_id, bios_version="2101"))
        fetched = db.get_run(run_id)
        assert fetched.context_id == ctx_id

        db.close()


class TestFreshEqualsMigrated:
    """Future-proofing invariant: a database created fresh at the current
    schema version must be COLUMN-IDENTICAL to a v1 database walked through
    every migration. If a schema change touches _DDL_FRESH but not a
    migration (or vice versa), sudo/non-sudo or old/new installs would
    diverge structurally — this test makes that impossible to ship."""

    @staticmethod
    def _schema_map(db) -> dict[str, list[tuple]]:
        tables = db._execute_raw(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        out = {}
        for t in tables:
            cols = db._execute_raw(f"PRAGMA table_info({t['name']})").fetchall()
            out[t["name"]] = sorted((c["name"], c["type"].upper(), c["notnull"]) for c in cols)
        return out

    def test_fresh_schema_equals_v1_plus_migrations(self, tmp_path):
        import sqlite3

        migrated_path = tmp_path / "migrated.db"
        conn = sqlite3.connect(str(migrated_path), isolation_level=None)
        conn.executescript(_V1_SCHEMA)
        conn.close()

        migrated = HistoryDB(migrated_path)
        fresh = HistoryDB(tmp_path / "fresh.db")
        try:
            assert self._schema_map(fresh) == self._schema_map(migrated)
        finally:
            fresh.close()
            migrated.close()


class TestMergeFrom:
    """merge_from is the one-database guarantee: it must import EVERYTHING,
    remap every reference, deduplicate contexts, and never modify source rows."""

    def test_merge_remaps_ids_and_dedups_contexts(self, tmp_path):
        src = HistoryDB(tmp_path / "src.db")
        dst = HistoryDB(tmp_path / "dst.db")

        src_ctx = src.create_context(TuningContextRecord(bios_version="2401", co_hash="h1"))
        dst_ctx = dst.create_context(TuningContextRecord(bios_version="2401", co_hash="h1"))
        rid = src.create_run(
            RunRecord(
                started_at="2026-07-07T09:00:00+00:00",
                status="completed",
                cpu_model="9950X3D",
                backend="mprime",
                context_id=src_ctx,
                bios_version="2401",
            )
        )
        src.insert_core_result(CoreResultRecord(run_id=rid, core_id=3, passed=True))
        src.insert_event(EventRecord(run_id=rid, event_type="core_start", core_id=3))
        src.insert_telemetry_block([TelemetrySample(run_id=rid, core_id=3, timestamp=AT, freq_mhz=5300.0)])
        sid = seed_old_session(src)
        src._execute_raw("UPDATE tuner_sessions SET context_id=? WHERE id=?", (src_ctx, sid))
        src._execute_raw("UPDATE tuner_test_log SET run_id=? WHERE session_id=?", (rid, sid))
        src.close()

        counts = dst.merge_from(tmp_path / "src.db")
        assert counts == {"contexts": 0, "runs": 1, "tuner_sessions": 1}

        runs = dst.list_runs()
        assert len(runs) == 1
        merged_run = runs[0]
        assert merged_run.cpu_model == "9950X3D"
        assert merged_run.context_id == dst_ctx
        results = dst.get_core_results(merged_run.id)
        assert [r.core_id for r in results] == [3]
        assert [e.event_type for e in dst.get_events(merged_run.id)] == ["core_start"]
        assert len(dst.get_telemetry(merged_run.id)) == 1

        def rows(sql):
            return [tuple(row) for row in dst._execute_raw(sql).fetchall()]

        [(merged, bios, context)] = rows("SELECT id, bios_version, context_id FROM tuner_sessions")
        assert (bios, context) == ("2402", dst_ctx)
        assert rows("SELECT session_id, core_id, best_offset FROM tuner_core_states") == [(merged, 0, -12)]
        assert rows("SELECT session_id, run_id FROM tuner_test_log") == [(merged, merged_run.id)]
        assert rows("SELECT session_id, message FROM tuner_events") == [(merged, "confirmed")]
        assert rows("SELECT session_id, core_id, value FROM tuner_co_journal") == [(merged, 0, -12)]
        dst.close()

    def test_merge_copies_every_journal_column_in_write_order(self, tmp_path):
        dead = "a" * 32
        src = HistoryDB(tmp_path / "src.db")
        sid = src.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "2401", "cpu_model": "9950X3D", "context_id": None}
        src.save_machine_session(sid, status="paused", machine_json="1", boot_id=dead, **fields)
        crashed = src.journal_co_intent(sid, 3, -30, False, boot_id=dead, mono_ns=5_000)
        src.journal_confirm(crashed)
        src.journal_co_intent(sid, 1, 0, True, boot_id=dead, mono_ns=9_000)
        src.journal_mark_survived(sid, boot_id=dead)
        src._execute_raw("UPDATE tuner_co_journal SET retracted=1 WHERE id=?", (crashed,))
        src._execute_raw("UPDATE tuner_sessions SET kernel_read_ns=7000 WHERE id=?", (sid,))
        written = src.journal_rows(sid, dead)
        src.close()
        dst = HistoryDB(tmp_path / "dst.db")
        own = seed_old_session(dst)
        dst.journal_co_intent(own, 0, -5, False, boot_id=dead, mono_ns=1_000)

        dst.merge_from(tmp_path / "src.db")

        merged = own + 1
        copied = dst.journal_rows(merged, dead)
        kernel_read = dst._execute_raw("SELECT kernel_read_ns FROM tuner_sessions WHERE id=?", (merged,)).fetchone()[0]
        dst.close()
        assert [replace(row, id=0, session_id=0) for row in copied] == [
            replace(row, id=0, session_id=0) for row in written
        ]
        assert [row.core_id for row in copied] == [3, 1]
        assert kernel_read == 7000

    def test_merge_keeps_a_machine_session_and_its_tests_under_the_new_id(self, tmp_path):
        boot = "b" * 32
        src = HistoryDB(tmp_path / "src.db")
        sid = src.next_tuner_session_id()
        src.save_machine_session(
            sid,
            status="paused",
            config_json="{}",
            machine_json='{"m": 1}',
            boot_id=boot,
            bios_version="2401",
            cpu_model="9950X3D",
            context_id=None,
        )
        row = src.insert_tuner_test(sid, boot, 3, "search", None, "1t", 8, -5, "[[8, -5]]", "[8]", 60.0, "{}")
        src.end_tuner_test(
            row, "silicon", 12.5, "[8]", None, "[]", "error on core 8", "[]", "Error(s) encountered on logical core 8."
        )
        sample = {
            "mono_ns": 7,
            "running": True,
            "tctl": 61.5,
            "clocks": [[8, 5000.0]],
            "tccd": [],
            "volts": [],
            "watts": [],
            "cores": [],
        }
        src.insert_tuner_block(row, [sample])
        src.insert_tuner_samples(row, [sample | {"mono_ns": 8}])
        src.close()
        dst = HistoryDB(tmp_path / "dst.db")
        own = seed_old_session(dst)

        dst.merge_from(tmp_path / "src.db")

        merged = own + 1
        tests = [
            dict(r) for r in dst._execute_raw("SELECT * FROM tuner_tests WHERE session_id=?", (merged,)).fetchall()
        ]
        loaded = dst.load_machine_session(merged)
        samples = dst.tuner_test_samples(tests[0]["id"])
        dst.close()
        assert loaded == ('{"m": 1}', boot, None, None)
        assert [(t["token"], t["core"], t["value"], t["verdict"], t["seconds_run"]) for t in tests] == [
            (3, 8, -5, "silicon", 12.5)
        ]
        assert samples == [sample, sample | {"mono_ns": 8}]

    @pytest.mark.req("L24.l24b.schema")
    def test_merge_keeps_a_child_session_linked_to_its_parent_under_the_new_ids(self, tmp_path):
        src = HistoryDB(tmp_path / "src.db")
        fields = {"config_json": "{}", "bios_version": "B", "cpu_model": "C", "context_id": None}
        for sid in (1, 2):
            src.save_machine_session(sid, status="paused", machine_json="1", boot_id="a", **fields)
        src.link_session(2, 1, "restart")
        src.close()
        dst = HistoryDB(tmp_path / "dst.db")
        own = seed_old_session(dst)
        dst.merge_from(tmp_path / "src.db")
        rows = dst._execute_raw("SELECT id, parent_session_id, origin FROM tuner_sessions ORDER BY id").fetchall()
        dst.close()
        assert [tuple(r) for r in rows] == [(own, None, None), (own + 1, None, None), (own + 2, own + 1, "restart")]

    def test_merge_adopts_new_boots_and_keeps_the_first_record_of_a_known_one(self, tmp_path):
        src = HistoryDB(tmp_path / "src.db")
        src.insert_tuner_boot("b1", "2401", 2033.0, 3000.0, 3000.0, "{}", '{"from": "src"}')
        src.insert_tuner_boot("b2", "2401", None, None, None, "{}", '{"from": "src"}')
        src.close()
        dst = HistoryDB(tmp_path / "dst.db")
        dst.insert_tuner_boot("b1", "2401", 2033.0, 3000.0, 3000.0, "{}", '{"from": "dst"}')
        dst.merge_from(tmp_path / "src.db")
        boots = {r["boot_id"]: r["igpu_json"] for r in dst._execute_raw("SELECT * FROM tuner_boots").fetchall()}
        dst.close()
        assert boots == {"b1": '{"from": "dst"}', "b2": '{"from": "src"}'}

    @pytest.mark.req("CR.2.merge")
    def test_merge_carries_every_column_of_every_table(self, tmp_path):
        src_path = tmp_path / "src.db"
        HistoryDB(src_path).close()
        raw = sqlite3.connect(src_path)
        raw.row_factory = sqlite3.Row
        tables = [
            row["name"]
            for row in raw.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            if row["name"] not in {"schema_version", "sqlite_sequence"}
        ]
        links = {"run_id", "context_id", "session_id", "test_id", "parent_session_id"}
        written = {}
        for table in tables:
            cols = [c["name"] for c in raw.execute(f"PRAGMA table_info({table})") if c["name"] != "id"]
            row = {c: 1 if c in links else f"{table}.{c}" for c in cols}
            raw.execute(
                f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values())
            )
            written[table] = row
        raw.commit()
        raw.close()
        dst = HistoryDB(tmp_path / "dst.db")
        dst.merge_from(src_path)
        copied = {t: dict(dst._execute_raw(f"SELECT * FROM {t} ORDER BY rowid DESC").fetchone()) for t in tables}
        dst.close()
        for table, row in written.items():
            assert {c: v for c, v in copied[table].items() if c not in links | {"id"}} == {
                c: v for c, v in row.items() if c not in links
            }, table

    def test_merge_keeps_rows_whose_optional_link_is_gone(self, tmp_path):
        src_path = tmp_path / "src.db"
        src = HistoryDB(src_path)
        sid = seed_old_session(src)
        src.close()
        raw = sqlite3.connect(src_path)
        raw.execute(
            "INSERT INTO runs (started_at, cpu_model, backend, status, context_id) VALUES (?,?,?,?,?)",
            ("2025-01-01T00:00:00+00:00", "old-cpu", "mprime", "completed", 99),
        )
        raw.execute(
            "INSERT INTO tuner_test_log (session_id, core_id, offset_tested, phase, passed, run_id, tested_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sid, 3, -5, "search", 1, 99, "2025-01-01T00:00:00+00:00"),
        )
        raw.commit()
        raw.close()
        dst = HistoryDB(tmp_path / "dst.db")
        assert dst.merge_from(src_path)["runs"] == 1
        assert dst.list_runs()[0].context_id is None
        logged = dst._execute_raw("SELECT core_id, run_id FROM tuner_test_log ORDER BY id").fetchall()
        dst.close()
        assert [tuple(row) for row in logged] == [(0, None), (3, None)]

    def test_merge_migrates_old_schema_source_first(self, tmp_path):
        import sqlite3

        src_path = tmp_path / "old.db"
        conn = sqlite3.connect(str(src_path), isolation_level=None)
        conn.executescript(_V1_SCHEMA)
        conn.execute(
            "INSERT INTO runs (started_at, cpu_model, backend, status) VALUES (?,?,?,?)",
            ("2025-01-01T00:00:00+00:00", "old-cpu", "mprime", "completed"),
        )
        conn.close()

        dst = HistoryDB(tmp_path / "dst.db")
        counts = dst.merge_from(src_path)
        assert counts["runs"] == 1
        assert dst.list_runs()[0].cpu_model == "old-cpu"
        dst.close()


def test_a_schema_32_database_gains_the_co_reads_table_and_keeps_each_read_with_its_moment(tmp_path):
    path = tmp_path / "history.db"
    HistoryDB(path).close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE tuner_co_reads")
    conn.execute("UPDATE schema_version SET version=32")
    conn.commit()
    conn.close()
    db = HistoryDB(path)
    try:
        db.save_machine_session(
            7,
            status="paused",
            config_json="{}",
            machine_json="{}",
            boot_id="b",
            bios_version="",
            cpu_model="",
            context_id=None,
        )
        db.insert_co_reads(7, "b", 1_000, ((0, -3), (1, None)))
        assert [(row.core_id, row.value, row.mono_ns, row.confirmed) for row in db.co_reads("b")] == [
            (0, -3, 1_000, True)
        ]
    finally:
        db.close()


class TestMigrationCrashSafety:
    def test_interrupted_migration_reruns_idempotently(self, tmp_path):
        import sqlite3

        path = tmp_path / "history.db"
        HistoryDB(str(path)).close()

        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE schema_version SET version = 8")
        conn.commit()
        conn.close()

        db = HistoryDB(str(path))
        try:
            version = db._execute_raw("SELECT version FROM schema_version").fetchone()[0]
            assert version == HistoryDB.SCHEMA_VERSION
        finally:
            db.close()


def _half_done_migration(conn):
    conn.execute("CREATE TABLE half_done (x INTEGER)")
    conn.execute("SELECT no_such_function()")


class TestMigrationAtomicity:
    @pytest.mark.parametrize(
        "migration",
        [_half_done_migration, "CREATE TABLE half_done (x INTEGER);\nSELECT no_such_function();"],
        ids=["callable", "script"],
    )
    def test_a_failed_migration_leaves_the_database_as_it_was(self, tmp_path, monkeypatch, migration):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        previous = HistoryDB.SCHEMA_VERSION - 1
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version=?", (previous,))
        conn.commit()
        conn.close()
        monkeypatch.setitem(HistoryDB._MIGRATIONS, HistoryDB.SCHEMA_VERSION, migration)

        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            HistoryDB(path)

        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        half_done = conn.execute("SELECT count(*) FROM sqlite_master WHERE name='half_done'").fetchone()[0]
        conn.close()
        assert version == previous
        assert half_done == 0

    def test_a_failed_first_creation_leaves_no_tables_and_the_next_open_builds_them_all(self, tmp_path, monkeypatch):
        path = tmp_path / "history.db"
        complete = HistoryDB._DDL_FRESH
        monkeypatch.setattr(HistoryDB, "_DDL_FRESH", complete + "\nSELECT no_such_function();")

        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            HistoryDB(path)

        conn = sqlite3.connect(path)
        left = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        conn.close()
        assert left == []

        monkeypatch.setattr(HistoryDB, "_DDL_FRESH", complete)
        HistoryDB(path).close()
        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        sessions = conn.execute("SELECT count(*) FROM tuner_sessions").fetchone()[0]
        conn.close()
        assert version == HistoryDB.SCHEMA_VERSION
        assert sessions == 0

    def test_a_migration_that_commits_on_its_own_does_not_move_the_version(self, tmp_path, monkeypatch):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        previous = HistoryDB.SCHEMA_VERSION - 1
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version=?", (previous,))
        conn.commit()
        conn.close()
        own_commit = "CREATE TABLE IF NOT EXISTS early (x INTEGER);\nCOMMIT;"
        monkeypatch.setitem(HistoryDB._MIGRATIONS, HistoryDB.SCHEMA_VERSION, own_commit)

        with pytest.raises(RuntimeError, match=f"version {HistoryDB.SCHEMA_VERSION} ended its own transaction"):
            HistoryDB(path)

        conn = sqlite3.connect(path)
        version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        conn.close()
        assert version == previous


class TestTransaction:
    def test_a_nested_transaction_is_refused_and_the_outer_one_rolls_back(self, tmp_path):
        db = HistoryDB(tmp_path / "history.db")
        try:
            with pytest.raises(RuntimeError, match="does not nest"), db.transaction() as conn:
                conn.execute("CREATE TABLE outer_work (x INTEGER)")
                with db.transaction():
                    pass
            with db.transaction() as conn:
                left = conn.execute("SELECT count(*) FROM sqlite_master WHERE name='outer_work'").fetchone()[0]
            assert left == 0
        finally:
            db.close()

    def test_a_body_that_ends_the_transaction_is_refused(self, tmp_path):
        db = HistoryDB(tmp_path / "history.db")
        try:
            with pytest.raises(RuntimeError, match="ended before its commit"), db.transaction() as conn:
                conn.execute("COMMIT")
            with db.transaction() as conn:
                assert conn.in_transaction
        finally:
            db.close()


class TestMigrationV24:
    def test_every_stored_sample_row_moves_into_one_block_per_test(self, tmp_path):
        path = tmp_path / "v23.db"
        db = HistoryDB(path)
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "2401", "cpu_model": "9950X3D", "context_id": None}
        db.save_machine_session(sid, status="paused", machine_json="1", boot_id="a", **fields)
        rows = [db.insert_tuner_test(sid, "a", n, "search", None, "1t", 0, -5, "[]", "[0]", 60.0, "{}") for n in (1, 2)]
        db.close()
        conn = sqlite3.connect(path)
        conn.executescript(_V20_SAMPLES)
        stored = [(rows[0], 1, 1, 60.5), (rows[1], 2, 0, None), (rows[0], 3, 1, 61.0)]
        conn.executemany(
            "INSERT INTO tuner_samples (test_id, mono_ns, running, tctl, clocks_json, tccd_json, volts_json)"
            " VALUES (?,?,?,?,'[[0, 5000.0]]','[[0, 55.0]]','[[\"vcore\", 1.1]]')",
            stored,
        )
        conn.commit()
        conn.close()
        db = HistoryDB(path)
        try:
            read = {row: db.tuner_test_samples(row) for row in rows}
            blocks = db._execute_raw("SELECT test_id FROM tuner_sample_blocks ORDER BY id").fetchall()
            tail = db._execute_raw("SELECT count(*) FROM tuner_samples").fetchone()[0]
        finally:
            db.close()

        def sample(mono_ns, running, tctl):
            return {
                "mono_ns": mono_ns,
                "running": bool(running),
                "tctl": tctl,
                "clocks": [[0, 5000.0]],
                "tccd": [[0, 55.0]],
                "volts": [["vcore", 1.1]],
                "watts": [],
                "cores": [],
            }

        assert read == {rows[0]: [sample(1, 1, 60.5), sample(3, 1, 61.0)], rows[1]: [sample(2, 0, None)]}
        assert [b[0] for b in blocks] == rows
        assert tail == 0

    def test_a_rerun_keeps_the_tail_rows_it_finds(self, tmp_path):
        path = tmp_path / "v24.db"
        db = HistoryDB(path)
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "2401", "cpu_model": "9950X3D", "context_id": None}
        db.save_machine_session(sid, status="paused", machine_json="1", boot_id="a", **fields)
        row = db.insert_tuner_test(sid, "a", 1, "steady", "hot", "1t", 0, -5, "[]", "[0]", 60.0, "{}")
        sample = {
            "mono_ns": 1,
            "running": True,
            "tctl": 60.5,
            "clocks": [[0, 5000.0]],
            "tccd": [],
            "volts": [],
            "watts": [],
            "cores": [],
        }
        db.insert_tuner_samples(row, [sample])
        db.close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version = 23")
        conn.commit()
        conn.close()
        db = HistoryDB(path)
        try:
            assert db.tuner_test_samples(row) == [sample]
            assert db._execute_raw("SELECT count(*) FROM tuner_samples").fetchone()[0] == 1
        finally:
            db.close()


_V20_SAMPLES = """\
UPDATE schema_version SET version = 23;
DROP TABLE tuner_sample_blocks;
DROP TABLE tuner_samples;
CREATE TABLE tuner_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    test_id         INTEGER NOT NULL REFERENCES tuner_tests(id) ON DELETE CASCADE,
    mono_ns         INTEGER NOT NULL,
    running         INTEGER NOT NULL,
    tctl            REAL,
    clocks_json     TEXT    NOT NULL,
    tccd_json       TEXT    NOT NULL,
    volts_json      TEXT    NOT NULL
);
"""


class TestMigrationV31:
    @pytest.mark.req("O19.record")
    def test_a_sample_stored_before_the_core_list_reads_back_without_cores_and_a_new_one_keeps_them(self, tmp_path):
        path = tmp_path / "v30.db"
        db = HistoryDB(path)
        sid = db.next_tuner_session_id()
        fields = {"config_json": "{}", "bios_version": "2401", "cpu_model": "9950X3D", "context_id": None}
        db.save_machine_session(sid, status="paused", machine_json="1", boot_id="a", **fields)
        row = db.insert_tuner_test(sid, "a", 1, "steady", "hot", "1t", 0, -5, "[]", "[0]", 60.0, "{}")
        db.close()
        conn = sqlite3.connect(path)
        conn.executescript("UPDATE schema_version SET version = 30; ALTER TABLE tuner_samples DROP COLUMN cores;")
        conn.execute(
            "INSERT INTO tuner_samples (test_id, mono_ns, running, tctl, clocks, tccd, volts, watts)"
            " VALUES (?, 1, 1, 60.0, '[]', '[]', '[]', '[]')",
            (row,),
        )
        conn.commit()
        conn.close()
        db = HistoryDB(path)
        try:
            core = [12, 5438.0, 5459.0, 99.87, 1.149]
            empty = {"clocks": [], "tccd": [], "volts": [], "watts": []}
            db.insert_tuner_samples(row, [{"mono_ns": 2, "running": True, "tctl": 61.0, **empty, "cores": [core]}])
            assert [sample["cores"] for sample in db.tuner_test_samples(row)] == [[], [core]]
        finally:
            db.close()


class TestMigrationV25:
    def test_every_stored_telemetry_row_moves_into_one_block_per_run(self, tmp_path):
        path = tmp_path / "v24.db"
        db = HistoryDB(path)
        runs = [db.create_run(RunRecord(cpu_model="test")) for _ in range(2)]
        db.close()
        conn = sqlite3.connect(path)
        conn.executescript(_V24_TELEMETRY)
        stored = [(runs[1], 2, 5300.0, None), (runs[0], 0, 5500.0, 75.0), (runs[1], 3, None, 70.5)]
        conn.executemany(
            "INSERT INTO telemetry_samples (run_id, core_id, timestamp, freq_mhz, effective_max_mhz, temp_c, vcore_v)"
            " VALUES (?,?,?,?,5700.0,?,1.2)",
            [(run, core, AT, freq, temp) for run, core, freq, temp in stored],
        )
        conn.commit()
        conn.close()
        db = HistoryDB(path)
        try:
            read = {run: db.get_telemetry(run) for run in runs}
            gone = db._execute_raw("SELECT count(*) FROM sqlite_master WHERE name='telemetry_samples'").fetchone()[0]
        finally:
            db.close()

        def sample(run, core, freq, temp):
            return TelemetrySample(run, core, AT, freq, 5700.0, temp, 1.2)

        assert read == {runs[0]: [sample(*stored[1])], runs[1]: [sample(*stored[0]), sample(*stored[2])]}
        assert gone == 0


_V24_TELEMETRY = """\
UPDATE schema_version SET version = 24;
DROP TABLE telemetry_blocks;
CREATE TABLE telemetry_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    core_id         INTEGER NOT NULL,
    timestamp       TEXT    NOT NULL,
    freq_mhz        REAL,
    effective_max_mhz REAL,
    temp_c          REAL,
    vcore_v         REAL
);
"""


class TestMigrationV5:
    def test_a_duplicate_context_still_referenced_moves_onto_the_kept_one(self, tmp_path, monkeypatch):
        path = tmp_path / "v4.db"
        conn = sqlite3.connect(path)
        conn.executescript(_V1_SCHEMA)
        conn.close()
        with monkeypatch.context() as at_v4:
            at_v4.setattr(HistoryDB, "SCHEMA_VERSION", 4)
            HistoryDB(path).close()

        conn = sqlite3.connect(path)

        def context(co_hash):
            return conn.execute(
                "INSERT INTO tuning_contexts (created_at, bios_version, co_hash) VALUES (?,?,?)",
                ("2025-01-01T00:00:00+00:00", "2401", co_hash),
            ).lastrowid

        def run(context_id):
            return conn.execute(
                "INSERT INTO runs (started_at, context_id) VALUES (?,?)",
                ("2025-01-02T00:00:00+00:00", context_id),
            ).lastrowid

        kept = context("same")
        duplicate = context("same")
        other = context("other")
        run_on_duplicate = run(duplicate)
        run_on_other = run(other)
        session_on_duplicate = conn.execute(
            "INSERT INTO tuner_sessions (created_at, updated_at, context_id) VALUES (?,?,?)",
            ("2025-01-03T00:00:00+00:00", "2025-01-03T00:00:00+00:00", duplicate),
        ).lastrowid
        conn.commit()
        conn.close()

        db = HistoryDB(path)
        try:
            version = db._execute_raw("SELECT version FROM schema_version").fetchone()[0]
            contexts = sorted(c.id for c in db.list_contexts())
            runs = {r.id: r.context_id for r in db.list_runs()}
            session = db._execute_raw("SELECT context_id FROM tuner_sessions WHERE id=?", (session_on_duplicate,))
            session_context = session.fetchone()[0]
        finally:
            db.close()
        assert version == HistoryDB.SCHEMA_VERSION
        assert contexts == sorted([kept, other])
        assert runs == {run_on_duplicate: kept, run_on_other: other}
        assert session_context == kept


class TestAdoptLegacyRootDb:
    def _adopt(self, dest, root_path, euid=0):
        from unittest.mock import patch

        from corecycler.history.db import adopt_legacy_root_db

        with patch("corecycler.history.db.os.geteuid", return_value=euid):
            return adopt_legacy_root_db(dest, root_path)

    def test_non_root_returns_none(self, tmp_path):
        db = HistoryDB(":memory:")
        try:
            assert self._adopt(db, tmp_path / "root.db", euid=1000) is None
        finally:
            db.close()

    def test_missing_root_db_returns_none(self, tmp_path):
        db = HistoryDB(":memory:")
        try:
            assert self._adopt(db, tmp_path / "nope.db") is None
        finally:
            db.close()

    def test_same_file_returns_none(self, tmp_path):
        path = tmp_path / "shared.db"
        db = HistoryDB(path)
        try:
            assert self._adopt(db, path) is None
        finally:
            db.close()

    def test_resolve_error_returns_none(self, tmp_path):
        from unittest.mock import patch

        root_path = tmp_path / "root.db"
        root_path.write_bytes(b"")
        db = HistoryDB(":memory:")
        try:
            with patch("pathlib.Path.resolve", side_effect=OSError):
                assert self._adopt(db, root_path) is None
        finally:
            db.close()

    def test_adopts_merges_renames_and_clears_sidecars(self, tmp_path):
        root_path = tmp_path / "root.db"
        src = HistoryDB(root_path)
        src.create_run(RunRecord(cpu_model="legacy-root", backend="mprime"))
        src.close()
        for sidecar in ("-wal", "-shm"):
            root_path.with_name(root_path.name + sidecar).write_bytes(b"")

        from unittest.mock import patch

        dest = HistoryDB(tmp_path / "user.db")
        merged: list = []
        try:
            with patch.object(HistoryDB, "merge_from", lambda self, p: merged.append(p) or {"runs": 1}):
                counts = self._adopt(dest, root_path)
            assert counts == {"runs": 1}
            assert merged == [root_path]
            assert not root_path.exists()
            assert root_path.with_name("root.db.adopted").exists()
            assert not root_path.with_name("root.db-wal").exists()
            assert not root_path.with_name("root.db-shm").exists()
        finally:
            dest.close()


class TestPeakClocks:
    def _test(self, db, session_id, peaks_json, power, core=0):
        row = db.insert_tuner_test(
            session_id, this_boot(), 1, "search", None, "1t", core, -3, "[]", f"[{core}]", 60.0, json.dumps(power)
        )
        db.end_tuner_test(row, "pass", 60.0, "[]", None, "{}", "", "[]", "", peaks_json=peaks_json)

    def _session(self, db, session_id):
        db.save_machine_session(
            session_id,
            status="paused",
            config_json="{}",
            machine_json="{}",
            boot_id="b",
            bios_version="b",
            cpu_model="c",
            context_id=None,
        )

    @pytest.mark.req("CR.3", "T22.F10")
    def test_a_core_s_peaks_come_from_its_own_tests_per_session_and_per_range(self, db):
        capped = {"boost": "1", "ceilings": [[0, 4300.0], [1, 4300.0]]}
        self._session(db, 1)
        self._session(db, 2)
        self._test(db, 1, '{"0": 4300.0, "1": 4280.0}', capped)
        self._test(db, 1, '{"0": 4420.0}', capped)
        self._test(db, 1, None, capped)
        self._test(db, 1, '{"0": 4410.0, "1": 4290.0}', capped, core=1)
        self._test(db, 2, '{"0": 5700.0}', {"boost": "1", "ceilings": [[0, 5756.0]]})
        self._test(db, 2, '{"0": 4000.0}', {"boost": "0", "ceilings": [[0, 4300.0]]})
        assert own_peaks(db.list_tuner_tests(1)) == [
            {"core": 0, "cap_mhz": 4300.0, "boost": "1", "mhz": 4420.0},
            {"core": 1, "cap_mhz": 4300.0, "boost": "1", "mhz": 4290.0},
        ]
        assert own_peaks(db.list_every_tuner_test()) == [
            {"core": 0, "cap_mhz": 4300.0, "boost": "1", "mhz": 4420.0},
            {"core": 0, "cap_mhz": 5756.0, "boost": "1", "mhz": 5700.0},
            {"core": 0, "cap_mhz": 4300.0, "boost": "0", "mhz": 4000.0},
            {"core": 1, "cap_mhz": 4300.0, "boost": "1", "mhz": 4290.0},
        ]

    def test_v28_adds_the_peak_column_to_a_v27_database(self, tmp_path):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        conn = sqlite3.connect(path)
        conn.execute("ALTER TABLE tuner_tests DROP COLUMN peaks_json")
        conn.execute("UPDATE schema_version SET version=27")
        conn.commit()
        conn.close()
        db = HistoryDB(path)
        columns = {row[1] for row in db._execute_raw("PRAGMA table_info(tuner_tests)").fetchall()}
        db.close()
        assert "peaks_json" in columns
