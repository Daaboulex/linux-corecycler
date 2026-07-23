"""HistoryTab with a seeded DB: view modes, delete guards, run/session deletion.

Deletion is destructive, so the guards (a running/validating tuner session must
refuse deletion) and the view-mode wiring get first-class tests here.
"""

from __future__ import annotations

import sys as _sys
from unittest.mock import patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.history.db import HistoryDB, RunRecord
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def _seed_run(db, started_at, status="completed", cores_failed=0):
    return db.create_run(
        RunRecord(
            started_at=started_at, status=status, backend="mprime", stress_mode="SSE",
            cpu_model="Test 8C", total_cores=4, cores_passed=4 - cores_failed, cores_failed=cores_failed,
        )
    )


def _seed_session(db, status="completed"):
    sid = tp.create_session(db, TunerConfig(), bios_version="2402", cpu_model="Test 8C")
    tp.update_session_status(db, sid, status)
    return sid


def _tab(db):
    _qapp()
    from corecycler.gui.history_tab import HistoryTab

    return HistoryTab(db)


def _yes():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


class TestViews:
    def test_all_view_shows_seeded_runs(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        _seed_run(db, "2026-07-21T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        assert tab._runs_table.rowCount() == 2
        assert len(tab._displayed_runs) == 2

    def test_tuner_view_shows_sessions(self, db):
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        assert len(tab._tuner_sessions) == 1

    def test_empty_db_does_not_crash(self, db):
        tab = _tab(db)
        tab.refresh()
        assert tab._runs_table.rowCount() == 0


class TestDeleteGuards:
    def test_running_session_refuses_deletion(self, db):
        _seed_session(db, "running")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.warning") as warn:
            tab._delete_tuner_sessions([0])
        assert warn.called
        assert len(db.list_tuner_sessions()) == 1

    def test_validating_session_refuses_deletion(self, db):
        _seed_session(db, "validating")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.warning"):
            tab._delete_tuner_sessions([0])
        assert len(db.list_tuner_sessions()) == 1

    def test_completed_session_deletes(self, db):
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_tuner_sessions([0])
        assert db.list_tuner_sessions() == []

    def test_delete_run_removes_it(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_runs([0])
        assert db.list_runs() == []

    def test_delete_run_cancelled_keeps_it(self, db):
        from PySide6.QtWidgets import QMessageBox

        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with patch(
            "corecycler.gui.history_tab.QMessageBox.question",
            return_value=QMessageBox.StandardButton.No,
        ):
            tab._delete_runs([0])
        assert len(db.list_runs()) == 1


class TestBiosWarning:
    def test_set_bios_warning_is_recorded(self, db):
        tab = _tab(db)
        tab.set_bios_warning("2401", "2402")
        assert tab._bios_warning
