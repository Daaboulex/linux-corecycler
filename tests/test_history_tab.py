"""HistoryTab with a seeded DB: view modes, run/session deletion."""

from __future__ import annotations

import sys as _sys
from unittest.mock import patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.history.db import HistoryDB, RunRecord
from tests._tuner_world import save_machine_session


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
            started_at=started_at,
            status=status,
            backend="mprime",
            stress_mode="SSE",
            cpu_model="Test 8C",
            total_cores=4,
            cores_passed=4 - cores_failed,
            cores_failed=cores_failed,
        )
    )


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

    def test_tuner_view_shows_sessions(self, db, tmp_path):
        save_machine_session(db, tmp_path, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        assert len(tab._tuner_sessions) == 1

    def test_empty_db_does_not_crash(self, db):
        tab = _tab(db)
        tab.refresh()
        assert tab._runs_table.rowCount() == 0


class TestDeleteGuards:
    def test_completed_session_deletes(self, db, tmp_path):
        save_machine_session(db, tmp_path, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_tuner_sessions([0])
        assert db.list_machine_sessions() == []

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
        warning = "BIOS changed: 2401 -> 2402"
        tab.set_bios_warning(warning)
        assert (tab._bios_warning, tab._bios_label.text()) == (warning, warning)


def _seed_run_with_results(db, cores=2, failed=0):
    from corecycler.history.db import CoreResultRecord

    rid = db.create_run(
        RunRecord(
            started_at="2026-07-20T10:00:00+00:00",
            finished_at="2026-07-20T11:00:00+00:00",
            status="completed",
            backend="mprime",
            stress_mode="SSE",
            fft_preset="SMALL",
            cpu_model="Test 8C",
            seconds_per_core=600,
            cycle_count=1,
            total_cores=cores,
            cores_passed=cores - failed,
            cores_failed=failed,
            bios_version="2402",
        )
    )
    for c in range(cores):
        db.insert_core_result(
            CoreResultRecord(
                run_id=rid,
                core_id=c,
                ccd=0,
                cycle=0,
                started_at="2026-07-20T10:00:00+00:00",
                passed=(c >= failed),
                elapsed_seconds=600.0,
                iterations_completed=5,
                peak_freq_mhz=5200.0,
                max_temp_c=78.0,
                error_message=None if c >= failed else "rounding error",
                error_type=None if c >= failed else "computation",
            )
        )
    return rid


def _run_by_id(db, rid):
    return next(r for r in db.list_runs() if r.id == rid)


class TestRunDetail:
    def test_show_run_detail_renders(self, db):
        rid = _seed_run_with_results(db, cores=3, failed=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._show_run_detail(_run_by_id(db, rid))
        assert tab._detail_info.text()

    def test_selecting_row_opens_detail(self, db):
        _seed_run_with_results(db, cores=2)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.selectRow(0)
        tab._on_run_selection_changed()
        assert tab._detail_info.text()


class TestViewChoice:
    def test_the_tuner_view_and_back_keeps_the_runs(self, db):
        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_select.setCurrentIndex(tab._view_select.findData(tab.VIEW_TUNER))
        assert tab._total_label.text() == "Tuning sessions: 0"
        tab._view_select.setCurrentIndex(tab._view_select.findData(tab.VIEW_ALL))
        assert tab._runs_table.rowCount() == 1
        assert tab._total_label.text() == "Stress test runs: 1"


class TestExport:
    def test_export_json(self, db, tmp_path):
        from PySide6.QtWidgets import QDialog

        _seed_run_with_results(db, cores=2)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        out = tmp_path / "run.json"
        with (
            patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(out), "")),
            patch("corecycler.gui.history_tab._ExportOptionsDialog.exec", return_value=QDialog.DialogCode.Accepted),
        ):
            tab._export_json(0)
        assert out.exists()

    def test_export_csv(self, db, tmp_path):
        _seed_run_with_results(db, cores=2)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        out = tmp_path / "run.csv"
        with patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(out), "")):
            tab._export_csv(0)
        assert out.exists()

    def test_export_bulk_csv(self, db, tmp_path):
        _seed_run_with_results(db, cores=1)
        _seed_run_with_results(db, cores=1)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        out = tmp_path / "bulk.csv"
        with patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName", return_value=(str(out), "")):
            tab._export_bulk_csv([0, 1])
        assert out.exists()


class TestCompare:
    def test_compare_two_runs(self, db):
        _seed_run_with_results(db, cores=2)
        _seed_run_with_results(db, cores=2)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._compare_selected_rows = [0, 1]
        tab._runs_table.selectAll()
        tab._compare_selected()
        assert "Run Comparison" in tab._events_log.toPlainText()
