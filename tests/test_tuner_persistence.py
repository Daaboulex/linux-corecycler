"""Tests for tuner persistence layer — sessions, core states, test log."""

from __future__ import annotations

import pytest

from history.db import HistoryDB
from tuner.config import TunerConfig
from tuner.persistence import (
    create_session,
    get_active_session,
    get_best_profile,
    get_latest_session,
    get_session,
    get_test_log,
    load_core_states,
    log_test_result,
    save_core_state,
    update_session_status,
)
from tuner.state import CoreState


@pytest.fixture
def db():
    """In-memory database with v3 schema."""
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestTunerSessions:
    def test_create_and_get_session(self, db):
        cfg = TunerConfig(coarse_step=10)
        sid = create_session(db, cfg, "BIOS-1.0", "Ryzen 9 9950X3D")
        assert sid > 0

        s = get_session(db, sid)
        assert s is not None
        assert s.id == sid
        assert s.status == "running"
        assert s.bios_version == "BIOS-1.0"
        assert s.cpu_model == "Ryzen 9 9950X3D"
        assert "coarse_step" in s.config_json

    def test_update_session_status(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        update_session_status(db, sid, "paused")
        s = get_session(db, sid)
        assert s.status == "paused"

    def test_get_latest_session(self, db):
        cfg = TunerConfig()
        sid1 = create_session(db, cfg, "", "CPU1")
        sid2 = create_session(db, cfg, "", "CPU2")
        latest = get_latest_session(db)
        assert latest.id == sid2

    def test_get_active_session_running(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        active = get_active_session(db)
        assert active is not None
        assert active.id == sid

    def test_get_active_session_paused(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        update_session_status(db, sid, "paused")
        active = get_active_session(db)
        assert active is not None
        assert active.id == sid

    def test_get_active_session_none_when_completed(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        update_session_status(db, sid, "completed")
        active = get_active_session(db)
        assert active is None

    def test_get_session_not_found(self, db):
        assert get_session(db, 999) is None


class TestCoreStates:
    def test_save_and_load(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        cs0 = CoreState(core_id=0, phase="coarse_search", current_offset=-5, best_offset=0)
        cs1 = CoreState(core_id=1, phase="not_started")
        save_core_state(db, sid, cs0)
        save_core_state(db, sid, cs1)

        loaded = load_core_states(db, sid)
        assert len(loaded) == 2
        assert loaded[0].phase == "coarse_search"
        assert loaded[0].current_offset == -5
        assert loaded[0].best_offset == 0
        assert loaded[1].phase == "not_started"

    def test_upsert_updates_existing(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        cs = CoreState(core_id=0, phase="coarse_search", current_offset=-5)
        save_core_state(db, sid, cs)

        cs.phase = "fine_search"
        cs.current_offset = -8
        cs.best_offset = -5
        save_core_state(db, sid, cs)

        loaded = load_core_states(db, sid)
        assert len(loaded) == 1
        assert loaded[0].phase == "fine_search"
        assert loaded[0].current_offset == -8
        assert loaded[0].best_offset == -5

    def test_load_empty(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        loaded = load_core_states(db, sid)
        assert loaded == {}


class TestTestLog:
    def test_log_and_query(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        log_test_result(db, sid, 0, -5, "coarse", True, duration=60.0)
        log_test_result(db, sid, 0, -10, "coarse", True, duration=60.0)
        log_test_result(db, sid, 0, -15, "coarse", False, error_msg="MCE", duration=30.0)

        log_entries = get_test_log(db, sid, core_id=0)
        assert len(log_entries) == 3
        assert log_entries[0]["offset_tested"] == -5
        assert log_entries[0]["passed"] == 1
        assert log_entries[2]["passed"] == 0
        assert log_entries[2]["error_message"] == "MCE"

    def test_log_filter_by_core(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        log_test_result(db, sid, 0, -5, "coarse", True)
        log_test_result(db, sid, 1, -5, "coarse", True)

        log0 = get_test_log(db, sid, core_id=0)
        log1 = get_test_log(db, sid, core_id=1)
        log_all = get_test_log(db, sid)

        assert len(log0) == 1
        assert len(log1) == 1
        assert len(log_all) == 2

    def test_log_all_fields(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        lid = log_test_result(
            db, sid, 3, -20, "fine", False,
            error_msg="computation error", error_type="computation",
            duration=45.5, run_id=None,
        )
        assert lid > 0

        entries = get_test_log(db, sid, core_id=3)
        assert len(entries) == 1
        e = entries[0]
        assert e["core_id"] == 3
        assert e["offset_tested"] == -20
        assert e["phase"] == "fine"
        assert e["error_type"] == "computation"
        assert e["duration_seconds"] == pytest.approx(45.5)


class TestBestProfile:
    def test_confirmed_cores_only(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")

        save_core_state(db, sid, CoreState(
            core_id=0, phase="confirmed", current_offset=-30, best_offset=-30,
        ))
        save_core_state(db, sid, CoreState(
            core_id=1, phase="confirmed", current_offset=-25, best_offset=-25,
        ))
        save_core_state(db, sid, CoreState(
            core_id=2, phase="fine_search", current_offset=-20, best_offset=-15,
        ))

        profile = get_best_profile(db, sid)
        assert profile == {0: -30, 1: -25}

    def test_empty_when_no_confirmed(self, db):
        cfg = TunerConfig()
        sid = create_session(db, cfg, "", "")
        save_core_state(db, sid, CoreState(core_id=0, phase="coarse_search"))
        profile = get_best_profile(db, sid)
        assert profile == {}


class TestSchemaMigration:
    def test_fresh_db_has_tuner_tables(self, db):
        """Fresh v3 database should have all tuner tables."""
        tables = db._execute_raw(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        table_names = [t["name"] for t in tables]
        assert "tuner_sessions" in table_names
        assert "tuner_core_states" in table_names
        assert "tuner_test_log" in table_names

    def test_schema_version_is_5(self, db):
        version = db._execute_raw("SELECT version FROM schema_version").fetchone()[0]
        assert version == 5
