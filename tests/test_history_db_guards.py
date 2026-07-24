"""HistoryDB boundary guards: corruption refusal, missing rows, merge skips.

Every one of these is a fail-closed path — a corrupt file, an absent session,
an insane persisted offset, or an orphaned row in a merged database. They must
raise or return a safe default, never quietly hand back nonsense.
"""

from __future__ import annotations

import sqlite3

import pytest

from corecycler.history.db import (
    CoreResultRecord,
    HistoryDB,
    RunRecord,
    TuningContextRecord,
)
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase


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

    def test_a_future_schema_version_is_refused(self, tmp_path):
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


class TestMissingRows:
    def test_an_absent_session_has_no_latest(self, db):
        assert db.get_latest_tuner_session() is None

    def test_an_absent_session_reports_no_unattributed_crashes(self, db):
        assert db.get_unattributed_crashes(999) == 0

    def test_an_absent_session_reports_no_crash_streak(self, db):
        assert db.get_resume_crash_streak(999) == 0


class TestCoreStateSanity:
    def _session(self, db):
        return tp.create_session(db, TunerConfig(), bios_version="2402", cpu_model="Test")

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
            db.upsert_tuner_core_state(
                sid, CoreState(core_id=0, cumulative_test_time=-1.0)
            )


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
        sid = tp.create_session(db, TunerConfig(), bios_version="2402", cpu_model="Test")
        db.create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash=f"seed{sid}")
        )
        tp.save_core_state(
            db, sid, CoreState(core_id=0, phase=TunerPhase.CONFIRMED, best_offset=-20)
        )
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
        other.create_run(
            RunRecord(started_at="2026-07-20T10:00:00+00:00", status="completed", context_id=cid)
        )
        other.close()

        target = HistoryDB(tmp_path / "target.db")
        target.create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash="same")
        )
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
        first = db.create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash="abc")
        )
        second = db.create_context(
            TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash="abc")
        )
        assert first == second
        assert len(db.list_contexts()) == 1


class TestFailClosedOnBadInput:
    def test_a_context_that_cannot_be_stored_is_refused_loudly(self, db):
        with pytest.raises(RuntimeError, match="database inconsistent"):
            db.create_context(
                TuningContextRecord(bios_version="2402", co_offsets_json="{}", co_hash=None)
            )

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

    def test_a_corrupted_page_is_refused_at_open(self, tmp_path):
        path = tmp_path / "history.db"
        seeded = HistoryDB(path)
        for i in range(400):
            seeded.create_run(
                RunRecord(started_at=f"2026-07-20T10:00:{i % 60:02d}+00:00", status="completed")
            )
        seeded.close()

        raw = bytearray(path.read_bytes())
        assert len(raw) > 16384
        raw[-4096:] = b"\xde\xad\xbe\xef" * 1024
        path.write_bytes(bytes(raw))

        with pytest.raises(RuntimeError, match="failed integrity check"):
            HistoryDB(path)


def _seed_run_only(db):
    return db.create_run(
        RunRecord(started_at="2026-07-20T10:00:00+00:00", status="completed")
    )
