from __future__ import annotations

import pytest

from corecycler.history.db import HistoryDB
from corecycler.tuner.live import crash_suspects, resident_at_death
from tests._machine import journaled, this_boot
from tests._old_rows import seed_old_session


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def _session(db) -> int:
    session = db.next_tuner_session_id()
    db.save_machine_session(
        session,
        status="running",
        config_json="{}",
        machine_json="{}",
        boot_id=this_boot(),
        bios_version="1.0",
        cpu_model="TestCPU",
        context_id=None,
    )
    return session


def _suspects(db, sid) -> dict[int, int]:
    return crash_suspects(resident_at_death(db.journal_rows(sid, this_boot())), -1)


class TestSchemaMigration:
    def test_fresh_db_has_tuner_tables(self, db):
        """Fresh v3 database should have all tuner tables."""
        tables = db._execute_raw("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        table_names = [t["name"] for t in tables]
        assert "tuner_sessions" in table_names
        assert "tuner_core_states" in table_names
        assert "tuner_test_log" in table_names

    def test_schema_version_is_current(self, db):
        version = db._execute_raw("SELECT version FROM schema_version").fetchone()[0]
        assert version == HistoryDB.SCHEMA_VERSION


class TestSchemaV11:
    def test_schema_version_is_current(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            row = db._execute_raw("SELECT version FROM schema_version").fetchone()
            assert row[0] == HistoryDB.SCHEMA_VERSION
        finally:
            db.close()

    def test_the_co_journal_round_trips(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = _session(db)
            db.journal_confirm(journaled(db, sid, 0, -30))
            db.journal_confirm(journaled(db, sid, 1, 0, survived=True))
            assert _suspects(db, sid) == {0: -30}
            db.journal_mark_survived(sid, boot_id=this_boot())
            assert _suspects(db, sid) == {}
            assert db.journal_proven_values(sid, -1) == {0: -30, 1: 0}
        finally:
            db.close()

    def test_v10_database_migrates_to_v11(self, tmp_path):
        """An older (v10) on-disk database gains the CO journal table when
        re-opened — the migration path, not just the fresh-schema path."""
        path = tmp_path / "mig.db"
        db = HistoryDB(path)
        sid = seed_old_session(db)
        db._execute_raw("UPDATE schema_version SET version=10")
        db._execute_raw("DROP TABLE tuner_co_journal")
        for table, column in (
            ("tuning_contexts", "ppt_limit_w"),
            ("tuning_contexts", "tdc_limit_a"),
            ("tuning_contexts", "edc_limit_a"),
            ("tuner_sessions", "unattributed_crashes"),
            ("tuner_sessions", "hunting_core"),
            ("tuner_sessions", "validation_stage"),
            ("tuner_sessions", "validation_index"),
            ("tuner_sessions", "validation_half"),
            ("tuner_sessions", "validation_dirty"),
            ("tuner_sessions", "validation_requeue"),
            ("tuner_sessions", "boot_id"),
            ("tuner_sessions", "kernel_read_ns"),
            ("tuner_test_log", "peak_stretch_pct"),
        ):
            db._execute_raw(f"ALTER TABLE {table} DROP COLUMN {column}")
        db.close()

        db2 = HistoryDB(path)
        try:
            assert db2._execute_raw("SELECT version FROM schema_version").fetchone()[0] == HistoryDB.SCHEMA_VERSION
            journaled(db2, sid, 0, -25)
            assert _suspects(db2, sid) == {0: -25}
        finally:
            db2.close()


class TestSchemaV13:
    def test_the_last_write_per_core_is_read_survived_or_not(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = _session(db)
            journaled(db, sid, 0, -30)
            journaled(db, sid, 1, -30)
            journaled(db, sid, 0, -41, survived=True)
            resident = resident_at_death(db.journal_rows(sid, this_boot()))
            assert {core_id: held.last.value for core_id, held in resident.items()} == {0: -41, 1: -30}
        finally:
            db.close()

    def test_context_power_limits_round_trip(self, tmp_path):
        from corecycler.history.db import TuningContextRecord

        db = HistoryDB(tmp_path / "test.db")
        try:
            ctx = TuningContextRecord(
                bios_version="2101",
                co_hash="abc",
                ppt_limit_w=225.0,
                tdc_limit_a=190.0,
                edc_limit_a=None,
            )
            cid = db.create_context(ctx)
            loaded = db.get_context(cid)
            assert loaded.ppt_limit_w == pytest.approx(225.0)
            assert loaded.tdc_limit_a == pytest.approx(190.0)
            assert loaded.edc_limit_a is None
        finally:
            db.close()


class TestSchemaV15Narrative:
    def test_events_round_trip_newest_last(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = _session(db)
            for i in range(5):
                db.insert_tuner_event(sid, f"line {i}", boot_id="boot-a")
            events = db.get_tuner_events(sid, limit=3)
            assert [e["message"] for e in events] == ["line 2", "line 3", "line 4"]
            assert events[0]["boot_id"] == "boot-a"
            assert events[0]["severity"] == "info"
        finally:
            db.close()


class TestSchemaV16SessionBoot:
    LAST_STATE_WRITE = "2026-10-01T11:30:00+00:00"
    EVENTS = (
        ("0a1b2c3d-0000-4000-8000-00000000000a", "2026-10-01T10:00:00+00:00"),
        ("0a1b2c3d-0000-4000-8000-00000000000b", "2026-10-01T11:00:00+00:00"),
        ("0a1b2c3d-0000-4000-8000-00000000000c", "2026-10-02T09:00:00+00:00"),
    )

    def _v15_file(self, path):
        db = HistoryDB(path)
        first = seed_old_session(db, "paused", "fine_search")
        second = seed_old_session(db)
        for boot, stamp in self.EVENTS:
            db.insert_tuner_event(first, f"a line written in boot {boot}", boot_id=boot)
            db._execute_raw("UPDATE tuner_events SET timestamp=? WHERE id=(SELECT MAX(id) FROM tuner_events)", (stamp,))
        db._execute_raw("UPDATE tuner_sessions SET updated_at=?", (self.LAST_STATE_WRITE,))
        db._execute_raw("UPDATE tuner_core_states SET updated_at=?", (self.LAST_STATE_WRITE,))
        _with_the_v15_journal(db)
        db._execute_raw("UPDATE schema_version SET version=15")
        db._execute_raw("ALTER TABLE tuner_sessions DROP COLUMN boot_id")
        db.close()
        return first, second

    def test_v15_sessions_take_the_newest_event_boot_or_none(self, tmp_path):
        path = tmp_path / "v15.db"
        first, second = self._v15_file(path)

        db = HistoryDB(path)
        try:
            boots = dict(db._execute_raw("SELECT id, boot_id FROM tuner_sessions").fetchall())
        finally:
            db.close()
        assert boots == {first: "0a1b2c3d00004000800000000000000b", second: ""}


_V15_JOURNAL = """\
CREATE TABLE tuner_co_journal (
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id     INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    survived    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
)"""


def _with_the_v15_journal(db) -> None:
    db._execute_raw("DROP TABLE tuner_co_journal")
    db._execute_raw(_V15_JOURNAL)
    db._execute_raw("ALTER TABLE tuner_sessions DROP COLUMN kernel_read_ns")


def _shape(db, table: str) -> tuple[list[tuple], list[str]]:
    columns = db._execute_raw(f"PRAGMA table_info({table})").fetchall()
    indexes = db._execute_raw(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)
    ).fetchall()
    return (
        sorted((c["name"], c["type"], c["notnull"], c["dflt_value"], c["pk"]) for c in columns),
        sorted(row["name"] for row in indexes),
    )


class TestSchemaV17Journal:
    BOOT = "0a1b2c3d00004000800000000000000a"
    V15_ROWS = (
        (1, -15, 1, "2026-10-01T10:00:00+00:00"),
        (0, -20, 1, "2026-10-01T10:05:00+00:00"),
        (2, -10, 0, "2026-10-01T10:10:00+00:00"),
    )
    TESTS = (
        (0, -20, "confirm", 1, 300.0),
        (1, -15, "fine", 0, None),
        (2, -10, "fine", 1, 60.0),
    )

    def _v16_file(self, path) -> int:
        db = HistoryDB(path)
        sid = seed_old_session(db)
        db._execute_raw("UPDATE tuner_sessions SET boot_id=? WHERE id=?", (self.BOOT, sid))
        for core_id, value, phase, passed, duration in self.TESTS:
            db._execute_raw(
                "INSERT INTO tuner_test_log"
                " (session_id, core_id, offset_tested, phase, passed, duration_seconds, tested_at)"
                " VALUES (?,?,?,?,?,?,'2026-10-01T09:00:00+00:00')",
                (sid, core_id, value, phase, passed, duration),
            )
        _with_the_v15_journal(db)
        for core_id, value, survived, stamp in self.V15_ROWS:
            db._execute_raw(
                "INSERT INTO tuner_co_journal (session_id, core_id, value, survived, updated_at) VALUES (?,?,?,?,?)",
                (sid, core_id, value, survived, stamp),
            )
        db._execute_raw("UPDATE schema_version SET version=16")
        db.close()
        return sid

    def test_v15_journal_rows_migrate_with_the_sessions_boot(self, tmp_path):
        sid = self._v16_file(tmp_path / "v16.db")

        db = HistoryDB(tmp_path / "v16.db")
        fresh = HistoryDB(tmp_path / "fresh.db")
        try:
            rows = [
                (r.core_id, r.value, r.mono_ns, r.confirmed, r.survived, r.retracted, r.written_at)
                for r in db.journal_rows(sid, self.BOOT)
            ]
            assert rows == [
                (1, -15, None, False, False, False, "2026-10-01T10:00:00+00:00"),
                (0, -20, None, False, True, False, "2026-10-01T10:05:00+00:00"),
                (2, -10, None, False, False, False, "2026-10-01T10:10:00+00:00"),
            ]
            for table in ("tuner_co_journal", "tuner_sessions"):
                assert _shape(db, table) == _shape(fresh, table)
        finally:
            fresh.close()
            db.close()


class TestJournalProof:
    def test_a_retracted_row_is_unproven(self, db):
        sid = _session(db)
        row = journaled(db, sid, 0, -20, survived=True)
        db.journal_confirm(row)
        db._execute_raw("UPDATE tuner_co_journal SET retracted=1 WHERE id=?", (row,))

        assert _suspects(db, sid) == {0: -20}
        assert db.journal_proven_values(sid, -1) == {}

    def test_a_positive_direction_proves_and_anchors_by_its_own_aggressiveness(self, db):
        sid = _session(db)
        for value in (5, 8, 6):
            journaled(db, sid, 0, value, survived=True)
        db.journal_confirm(journaled(db, sid, 1, 3))
        journaled(db, sid, 1, 6)

        resident = resident_at_death(db.journal_rows(sid, this_boot()))
        assert crash_suspects(resident, 1) == {1: 3}
        assert db.journal_proven_values(sid, 1) == {0: 8}

    @pytest.mark.parametrize("direction", [-1, 1])
    def test_a_retraction_takes_the_value_and_everything_beyond_it_in_the_sessions_direction(self, db, direction):
        sid = _session(db)
        for value in (3, 5, 8):
            journaled(db, sid, 0, direction * value, survived=True)
        journaled(db, sid, 1, direction * 8, survived=True)

        db.journal_retract(sid, 0, direction * 5, direction)

        assert db.journal_proven_values(sid, direction) == {0: direction * 3, 1: direction * 8}
