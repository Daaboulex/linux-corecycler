"""HistoryDB boundary guards: corruption refusal, missing rows, merge skips.

Every one of these is a fail-closed path — a corrupt file, an absent session,
an insane persisted offset, or an orphaned row in a merged database. They must
raise or return a safe default, never quietly hand back nonsense.
"""

from __future__ import annotations

import logging
import sqlite3
import time

import pytest

from corecycler.history.db import (
    CoreResultRecord,
    CorruptSessionError,
    HistoryDB,
    RunRecord,
    SchemaTooNewError,
    TuningContextRecord,
)
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase
from tests._machine import this_boot


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestOpenGuards:
    def test_a_corrupt_file_is_refused(self, tmp_path):
        path = tmp_path / "history.db"
        path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 512)
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            HistoryDB(path)

    def test_a_newer_schema_is_refused_and_left_as_it_was(self, tmp_path):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        newer = HistoryDB.SCHEMA_VERSION + 1
        _set_schema_version(path, newer)
        with pytest.raises(SchemaTooNewError) as refused:
            HistoryDB(path)
        assert f"version {newer}" in str(refused.value)
        assert f"version {HistoryDB.SCHEMA_VERSION}" in str(refused.value)
        assert _schema_version(path) == newer

    def test_a_refused_open_closes_its_connection(self, tmp_path, monkeypatch):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        _set_schema_version(path, HistoryDB.SCHEMA_VERSION + 1)
        opened = []
        real_connect = sqlite3.connect

        def recording_connect(*args, **kwargs):
            opened.append(real_connect(*args, **kwargs))
            return opened[-1]

        monkeypatch.setattr(sqlite3, "connect", recording_connect)
        with pytest.raises(SchemaTooNewError):
            HistoryDB(path)
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")

    def test_a_version_without_a_migration_is_refused(self, tmp_path):
        path = tmp_path / "history.db"
        HistoryDB(path).close()
        conn = sqlite3.connect(path)
        conn.execute("UPDATE schema_version SET version=0")
        conn.commit()
        conn.close()
        original = dict(HistoryDB._MIGRATIONS)
        try:
            HistoryDB._MIGRATIONS = {}
            with pytest.raises(RuntimeError, match="Missing migration"):
                HistoryDB(path)
        finally:
            HistoryDB._MIGRATIONS = original


def _set_schema_version(path, version):
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version=?", (version,))
    conn.commit()
    conn.close()


def _schema_version(path):
    conn = sqlite3.connect(path)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    return version


def _reader_holding_a_snapshot(path):
    reader = sqlite3.connect(path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM tuner_sessions").fetchall()
    return reader


class _CheckpointFails:
    def __init__(self, real: sqlite3.Connection) -> None:
        self.real = real
        self.closed = False

    def execute(self, sql, *args):
        if sql.startswith("PRAGMA wal_checkpoint"):
            raise sqlite3.OperationalError("disk I/O error")
        return self.real.execute(sql, *args)

    def close(self) -> None:
        self.closed = True
        self.real.close()


class _InterruptsAt:
    def __init__(self, real: sqlite3.Connection, marker: str) -> None:
        self.real = real
        self.marker = marker

    def execute(self, sql, *args):
        if sql.lstrip().startswith(self.marker):
            raise KeyboardInterrupt
        return self.real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self.real, name)


class TestDurability:
    def test_a_failing_close_checkpoint_still_closes_and_says_why(self, tmp_path):
        db = HistoryDB(tmp_path / "history.db")
        failing = _CheckpointFails(db._HistoryDB__conn)
        db._HistoryDB__conn = failing
        with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
            db.close()
        assert failing.closed is True
        db.close()

    def test_every_commit_is_synced(self, tmp_path):
        db = HistoryDB(tmp_path / "history.db")
        synchronous = db._HistoryDB__conn.execute("PRAGMA synchronous").fetchone()[0]
        db.close()
        assert synchronous == 2

    def test_a_reader_holding_a_snapshot_does_not_stall_a_co_write(self, tmp_path):
        path = tmp_path / "history.db"
        db = HistoryDB(path)
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        reader = _reader_holding_a_snapshot(path)
        started = time.monotonic()
        tp.journal_co_intent(db, sid, 0, -5, False)
        elapsed = time.monotonic() - started
        reader.execute("ROLLBACK")
        reader.close()
        fresh = sqlite3.connect(path)
        rows = fresh.execute("SELECT core_id, value FROM tuner_co_journal WHERE session_id=?", (sid,)).fetchall()
        fresh.close()
        db.close()
        assert elapsed < 1.0
        assert rows == [(0, -5)]

    def test_closing_logs_the_checkpoint_result(self, tmp_path, caplog):
        path = tmp_path / "history.db"
        db = HistoryDB(path)
        reader = _reader_holding_a_snapshot(path)
        tp.create_session(db, TunerConfig(), "", "", boot_id=this_boot())
        caplog.set_level(logging.INFO, logger="corecycler.history.db")
        started = time.monotonic()
        db.close()
        elapsed = time.monotonic() - started
        reader.close()
        closing = [r for r in caplog.records if r.name == "corecycler.history.db" and r.funcName == "close"]
        assert elapsed < 1.0
        assert len(closing) == 1
        busy, wal_frames, checkpointed = closing[0].args
        assert busy == 0
        assert 0 <= checkpointed < wal_frames

    def test_a_second_close_is_a_no_op(self, tmp_path):
        db = HistoryDB(tmp_path / "history.db")
        db.close()
        db.close()


class TestMissingRows:
    def test_an_absent_session_has_no_latest(self, db):
        assert db.get_latest_tuner_session() is None

    def test_an_absent_session_reports_no_unattributed_crashes(self, db):
        assert db.get_unattributed_crashes(999) == 0

    def test_an_absent_session_reports_no_crash_streak(self, db):
        assert db.get_resume_crash_streak(999) == 0


class TestCoreStateSanity:
    def _session(self, db):
        return tp.create_session(db, TunerConfig(), bios_version="2402", cpu_model="Test", boot_id=this_boot())

    def test_an_offset_outside_the_sane_range_is_refused(self, db):
        sid = self._session(db)
        with pytest.raises(ValueError, match="outside sane CO range"):
            db.upsert_tuner_core_state(sid, CoreState(core_id=0, current_offset=-9999))

    def test_a_negative_counter_is_refused(self, db):
        sid = self._session(db)
        with pytest.raises(ValueError, match="negative"):
            db.upsert_tuner_core_state(sid, CoreState(core_id=0, crash_count=-1))

    def test_negative_accumulated_time_is_refused(self, db):
        sid = self._session(db)
        with pytest.raises(ValueError, match="cumulative_test_time"):
            db.upsert_tuner_core_state(sid, CoreState(core_id=0, cumulative_test_time=-1.0))

    @pytest.mark.parametrize(
        ("column", "value", "cause"),
        [("phase", "future_phase", "future_phase"), ("current_offset", -9999, "current_offset=-9999")],
    )
    def test_an_unreadable_row_names_its_session_and_its_cause(self, db, column, value, cause):
        sid = self._session(db)
        tp.save_core_state(db, sid, CoreState(core_id=3))
        db._execute_raw(f"UPDATE tuner_core_states SET {column}=? WHERE session_id=?", (value, sid))
        with pytest.raises(CorruptSessionError) as refused:
            db.get_tuner_core_states(sid)
        assert cause in refused.value.detail
        assert "core 3" in refused.value.detail
        assert f"session {sid}" in str(refused.value)
        assert refused.value.detail in str(refused.value)


def _seed(path, *, runs=1, sessions=1):
    db = HistoryDB(path)
    for i in range(runs):
        rid = db.create_run(
            RunRecord(
                started_at=f"2026-07-2{i}T10:00:00+00:00",
                status="completed",
                backend="mprime",
                total_cores=1,
                cores_passed=1,
            )
        )
        db.insert_core_result(
            CoreResultRecord(run_id=rid, core_id=0, started_at="2026-07-20T10:00:00+00:00", passed=True)
        )
    for _ in range(sessions):
        sid = tp.create_session(db, TunerConfig(), bios_version="2402", cpu_model="Test", boot_id=this_boot())
        db.create_context(TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash=f"seed{sid}"))
        tp.save_core_state(db, sid, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, best_offset=-20))
        tp.journal_co_intent(db, sid, 0, -20, False)
        tp.log_test_result(db, sid, 0, -20, "confirm", True, duration=300.0)
    db.close()
    return db


class TestMerge:
    def test_rows_from_another_database_are_adopted(self, tmp_path):
        other = tmp_path / "other.db"
        _seed(other)
        target = HistoryDB(tmp_path / "target.db")
        counts = target.merge_from(other)
        assert counts["runs"] == 1
        assert counts["tuner_sessions"] == 1
        assert counts["contexts"] == 1
        assert len(target.list_runs()) == 1
        assert len(target.list_tuner_sessions()) == 1
        target.close()

    def test_a_duplicate_context_is_deduplicated(self, tmp_path):
        other_path = tmp_path / "other.db"
        other = HistoryDB(other_path)
        ctx = TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash="same")
        cid = other.create_context(ctx)
        other.create_run(RunRecord(started_at="2026-07-20T10:00:00+00:00", status="completed", context_id=cid))
        other.close()

        target = HistoryDB(tmp_path / "target.db")
        target.create_context(TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash="same"))
        counts = target.merge_from(other_path)
        assert counts["contexts"] == 0
        assert len(target.list_contexts()) == 1
        assert len(target.list_runs()) == 1
        target.close()

    def test_an_orphaned_journal_row_is_skipped(self, tmp_path):
        other_path = tmp_path / "other.db"
        _seed(other_path, runs=0)
        raw = sqlite3.connect(other_path)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DELETE FROM tuner_sessions")
        raw.commit()
        raw.close()

        target = HistoryDB(tmp_path / "target.db")
        counts = target.merge_from(other_path)
        assert counts["tuner_sessions"] == 0
        assert target.list_tuner_sessions() == []
        target.close()


class TestContextIdentity:
    def test_the_same_profile_reuses_its_context(self, db):
        first = db.create_context(TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash="abc"))
        second = db.create_context(TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash="abc"))
        assert first == second
        assert len(db.list_contexts()) == 1


class TestFailClosedOnBadInput:
    def test_a_context_that_cannot_be_stored_is_refused_loudly(self, db):
        with pytest.raises(RuntimeError, match="database inconsistent"):
            db.create_context(TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash=None))

    def test_a_merge_that_cannot_complete_leaves_nothing_behind(self, tmp_path):
        """All-or-nothing: a source holding a child row whose parent is gone
        must roll the whole merge back, not half-import it."""
        source = tmp_path / "orphaned.db"
        _seed(source)
        raw = sqlite3.connect(source)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("DELETE FROM runs")
        raw.commit()
        raw.close()

        target = HistoryDB(tmp_path / "target.db")
        _seed_run_only(target)
        before = len(target.list_runs())
        with pytest.raises(sqlite3.Error):
            target.merge_from(source)
        assert len(target.list_runs()) == before
        assert target.list_tuner_sessions() == []
        target.close()

    def test_an_interrupted_merge_leaves_the_target_as_it_was_and_usable(self, tmp_path):
        source = tmp_path / "other.db"
        _seed(source)
        target = HistoryDB(tmp_path / "target.db")
        _seed_run_only(target)
        real = target._HistoryDB__conn
        target._HistoryDB__conn = _InterruptsAt(real, "INSERT INTO tuner_sessions")
        with pytest.raises(KeyboardInterrupt):
            target.merge_from(source)
        target._HistoryDB__conn = real
        attached = [row[1] for row in real.execute("PRAGMA database_list").fetchall()]
        assert (real.in_transaction, "src" in attached) == (False, False)
        assert (len(target.list_runs()), target.list_contexts()) == (1, [])
        target.close()

    def test_a_corrupted_page_is_refused_at_open(self, tmp_path):
        path = tmp_path / "history.db"
        seeded = HistoryDB(path)
        for i in range(400):
            seeded.create_run(RunRecord(started_at=f"2026-07-20T10:00:{i % 60:02d}+00:00", status="completed"))
        seeded.close()

        raw = bytearray(path.read_bytes())
        assert len(raw) > 16384
        raw[-4096:] = b"\xde\xad\xbe\xef" * 1024
        path.write_bytes(bytes(raw))

        with pytest.raises(RuntimeError, match="failed integrity check"):
            HistoryDB(path)


def _seed_run_only(db):
    return db.create_run(RunRecord(started_at="2026-07-20T10:00:00+00:00", status="completed"))
