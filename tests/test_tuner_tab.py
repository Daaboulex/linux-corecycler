"""Tests for TunerTab GUI widget."""

from __future__ import annotations

import pytest

from history.db import HistoryDB
from tuner.config import TunerConfig


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestTunerTabCreation:
    """Basic construction tests that don't require a running Qt app."""

    def test_config_defaults(self):
        """TunerConfig defaults are usable for GUI initialization."""
        cfg = TunerConfig()
        assert cfg.coarse_step > 0
        assert cfg.fine_step > 0
        assert cfg.search_duration_seconds > 0
        assert cfg.confirm_duration_seconds > 0

    def test_config_fields_have_gui_ranges(self):
        """All config fields that map to spinboxes are within reasonable ranges."""
        cfg = TunerConfig()
        assert -60 <= cfg.start_offset <= 30
        assert 1 <= cfg.coarse_step <= 20
        assert 1 <= cfg.fine_step <= 10
        assert -60 <= cfg.max_offset <= 60
        assert 10 <= cfg.search_duration_seconds <= 3600
        assert 10 <= cfg.confirm_duration_seconds <= 7200
        assert 0 <= cfg.max_confirm_retries <= 10

    def test_db_schema_has_tuner_tables(self, db):
        """The DB fixture should have tuner tables from v3 schema."""
        tables = db._execute_raw(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = [t["name"] for t in tables]
        assert "tuner_sessions" in names
        assert "tuner_core_states" in names
        assert "tuner_test_log" in names


class TestSelfPauseRecoverable:
    """Every engine self-pause says 'fix the cause, then Resume' — the buttons
    must follow the engine status or the GUI is a dead end (Resume greyed out
    after an apparatus/SMU/startup pause)."""

    def test_status_paused_enables_resume(self):
        from PySide6.QtWidgets import QApplication

        from gui.tuner_tab import TunerTab

        app = QApplication.instance() or QApplication([])
        assert app is not None
        tab = TunerTab(db=None, topology=None, smu=None)
        tab._set_running_state(True)          # engine started: Resume disabled
        assert not tab._resume_btn.isEnabled()

        tab._on_status_changed("paused")      # engine paused ITSELF
        assert tab._resume_btn.isEnabled()
        assert not tab._pause_btn.isEnabled()

        tab._on_status_changed("running")     # resumed again
        assert not tab._resume_btn.isEnabled()
        assert tab._pause_btn.isEnabled()
