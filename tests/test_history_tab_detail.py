"""HistoryTab context grouping, detail rendering, menus, exports and deletes.

Complements test_history_tab.py: that file covers the view-mode wiring and the
destructive-delete guards, this one drives the context table, the run and tuner
detail panes, the two context menus and every no-database refusal.
"""

from __future__ import annotations

import json
import sys as _sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.gui import history_tab as ht
from corecycler.history.db import (
    CoreResultRecord,
    EventRecord,
    HistoryDB,
    RunRecord,
    TelemetrySample,
    TuningContextRecord,
)
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _yes():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.Yes


def _no():
    from PySide6.QtWidgets import QMessageBox

    return QMessageBox.StandardButton.No


def _point():
    from PySide6.QtCore import QPoint

    return QPoint(0, 0)


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def _tab(database=None):
    _qapp()
    return ht.HistoryTab(database)


def _seed_context(db, bios, offsets, *, notes="", scalar=None, boost=None):
    payload = json.dumps(offsets)
    return db.create_context(
        TuningContextRecord(
            bios_version=bios,
            co_offsets_json=payload,
            co_hash=f"{bios}:{json.dumps(offsets, sort_keys=True)}",
            pbo_scalar=scalar,
            boost_limit_mhz=boost,
            notes=notes,
        )
    )


def _seed_run(db, started_at, *, status="completed", cores_failed=0, context_id=None):
    return db.create_run(
        RunRecord(
            started_at=started_at,
            status=status,
            backend="mprime",
            stress_mode="SSE",
            fft_preset="SMALL",
            cpu_model="Test 8C",
            seconds_per_core=600,
            cycle_count=1,
            total_cores=4,
            cores_passed=4 - cores_failed,
            cores_failed=cores_failed,
            total_seconds=600.0,
            context_id=context_id,
            bios_version="2402",
        )
    )


def _seed_session(db, status="completed"):
    sid = tp.create_session(db, TunerConfig(), bios_version="2402", cpu_model="Test 8C")
    tp.update_session_status(db, sid, status)
    return sid


class TestViewToggles:
    def test_checked_view_toggle_selects_all_runs(self, db):
        tab = _tab(db)
        tab._tuner_toggle.setChecked(True)
        tab._view_toggle.setChecked(True)
        tab._toggle_view()
        assert tab._view_mode == tab.VIEW_ALL
        assert not tab._tuner_toggle.isChecked()

    def test_unchecked_view_toggle_returns_to_grouped(self, db):
        tab = _tab(db)
        tab._view_toggle.setChecked(True)
        tab._toggle_view()
        tab._view_toggle.setChecked(False)
        tab._toggle_view()
        assert tab._view_mode == tab.VIEW_GROUPED

    def test_checked_tuner_toggle_selects_tuner_view(self, db):
        tab = _tab(db)
        tab._view_toggle.setChecked(True)
        tab._tuner_toggle.setChecked(True)
        tab._toggle_tuner_view()
        assert tab._view_mode == tab.VIEW_TUNER
        assert not tab._view_toggle.isChecked()

    def test_unchecked_tuner_toggle_returns_to_grouped(self, db):
        tab = _tab(db)
        tab._tuner_toggle.setChecked(False)
        tab._toggle_tuner_view()
        assert tab._view_mode == tab.VIEW_GROUPED

    def test_all_view_populates_runs_table(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        _seed_run(db, "2026-07-21T10:00:00+00:00")
        tab = _tab(db)
        tab._view_toggle.setChecked(True)
        tab._toggle_view()
        assert tab._runs_table.rowCount() == 2


class TestWithoutDatabase:
    def test_refresh_is_a_noop(self):
        tab = _tab(None)
        tab.refresh()
        assert tab._runs_table.rowCount() == 0

    def test_preserving_refresh_is_a_noop(self):
        tab = _tab(None)
        tab._refresh_preserve_context()
        assert tab._contexts == []

    def test_tuner_session_load_is_empty(self):
        assert _tab(None)._load_tuner_sessions() == []

    def test_bulk_csv_export_is_a_noop(self):
        tab = _tab(None)
        with patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName") as dlg:
            tab._export_bulk_csv([0])
        assert not dlg.called

    def test_run_delete_is_a_noop(self):
        tab = _tab(None)
        with patch("corecycler.gui.history_tab.QMessageBox.question") as ask:
            tab._delete_runs([0])
        assert not ask.called

    def test_context_delete_is_a_noop(self):
        tab = _tab(None)
        with patch("corecycler.gui.history_tab.QMessageBox.question") as ask:
            tab._delete_contexts([0])
        assert not ask.called

    def test_tuner_session_delete_is_a_noop(self):
        tab = _tab(None)
        with patch("corecycler.gui.history_tab.QMessageBox.question") as ask:
            tab._delete_tuner_sessions([0])
        assert not ask.called

    def test_context_note_is_a_noop(self):
        tab = _tab(None)
        with patch("corecycler.gui.history_tab.QInputDialog.getText") as dlg:
            tab._add_context_note(TuningContextRecord(id=1))
        assert not dlg.called

    def test_context_note_without_context_id_is_a_noop(self, db):
        tab = _tab(db)
        with patch("corecycler.gui.history_tab.QInputDialog.getText") as dlg:
            tab._add_context_note(TuningContextRecord(id=None))
        assert not dlg.called

    def test_json_export_is_a_noop(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._db = None
        with patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName") as dlg:
            tab._export_json(0)
        assert not dlg.called

    def test_csv_export_is_a_noop(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._db = None
        with patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName") as dlg:
            tab._export_csv(0)
        assert not dlg.called

    def test_exports_refuse_an_out_of_range_row(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with patch("corecycler.gui.history_tab.QFileDialog.getSaveFileName") as dlg:
            tab._export_json(99)
            tab._export_csv(99)
        assert not dlg.called


def _grouped_tab(db):
    tab = _tab(db)
    tab._view_mode = tab.VIEW_GROUPED
    tab.refresh()
    return tab


class TestContextTable:
    def test_bios_change_is_marked_on_the_newer_context(self, db):
        old = _seed_context(db, "2402", {"0": -20})
        new = _seed_context(db, "2403", {"0": -25})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=old)
        _seed_run(db, "2026-07-21T10:00:00+00:00", context_id=new)
        tab = _grouped_tab(db)
        assert tab._context_table.rowCount() == 2
        assert tab._context_table.item(0, 0).text() == "2403 *"
        assert tab._context_table.item(0, 0).toolTip()
        assert tab._context_table.item(1, 0).text() == "2402"
        assert not tab._context_table.item(1, 0).toolTip()

    def test_context_row_summarises_runs_and_scalar(self, db):
        cid = _seed_context(db, "2402", {"0": -20, "1": -20}, notes="tuned", scalar=3.0)
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        _seed_run(db, "2026-07-21T10:00:00+00:00", context_id=cid, cores_failed=1)
        tab = _grouped_tab(db)
        assert tab._context_table.item(0, 1).text() == "all -20"
        assert tab._context_table.item(0, 2).text() == "3.0"
        assert tab._context_table.item(0, 3).text() == "2"
        assert tab._context_table.item(0, 4).text() == "4/4"
        assert tab._context_table.item(0, 5).text() == "tuned"

    def test_context_without_scalar_or_completed_runs(self, db):
        cid = _seed_context(db, "2402", {})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid, status="stopped")
        tab = _grouped_tab(db)
        assert tab._context_table.item(0, 1).text() == "none"
        assert tab._context_table.item(0, 2).text() == "-"
        assert tab._context_table.item(0, 4).text() == "-"

    def test_selecting_a_context_shows_only_its_runs(self, db):
        one = _seed_context(db, "2402", {"0": -20})
        two = _seed_context(db, "2403", {"0": -25})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=one)
        _seed_run(db, "2026-07-21T10:00:00+00:00", context_id=two)
        _seed_run(db, "2026-07-22T10:00:00+00:00", context_id=two)
        tab = _grouped_tab(db)
        tab._context_table.selectRow(0)
        assert tab._runs_table.rowCount() == 2
        tab._context_table.selectRow(1)
        assert tab._runs_table.rowCount() == 1

    def test_clearing_the_context_selection_empties_the_runs_table(self, db):
        cid = _seed_context(db, "2402", {"0": -20})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        tab._context_table.clearSelection()
        tab._on_context_selected()
        assert tab._runs_table.rowCount() == 0

    def test_refresh_restores_the_selected_context(self, db):
        one = _seed_context(db, "2402", {"0": -20})
        two = _seed_context(db, "2403", {"0": -25})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=one)
        _seed_run(db, "2026-07-21T10:00:00+00:00", context_id=one)
        _seed_run(db, "2026-07-22T10:00:00+00:00", context_id=two)
        tab = _grouped_tab(db)
        tab._context_table.selectRow(1)
        assert tab._runs_table.rowCount() == 2
        tab.refresh()
        rows = {i.row() for i in tab._context_table.selectionModel().selectedRows()}
        assert rows == {1}
        assert tab._runs_table.rowCount() == 2

    def test_degenerate_metrics_still_reserve_context_table_height(self, db):
        for bios in ("2402", "2403"):
            cid = _seed_context(db, bios, {"0": -20})
            _seed_run(db, f"2026-07-2{bios[-1]}T10:00:00+00:00", context_id=cid)
        _seed_run(db, "2026-07-25T10:00:00+00:00")
        tab = _grouped_tab(db)
        assert tab._context_table.rowCount() == 3
        tab._context_table.verticalHeader().setMinimumSectionSize(1)
        for row in range(3):
            tab._context_table.setRowHeight(row, 1)
        tab._context_table.horizontalHeader().setFixedHeight(5)
        tab._auto_size_context_table()
        assert tab._context_table.maximumHeight() >= 100


class TestContextMenus:
    def test_context_table_menu_offers_a_note_action(self, db, monkeypatch):
        cid = _seed_context(db, "2402", {"0": -20})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        menu_cls = MagicMock()
        monkeypatch.setattr(ht, "QMenu", menu_cls)
        tab._context_table.selectRow(0)
        tab._show_context_table_menu(_point())
        labels = [c.args[0] for c in menu_cls.return_value.addAction.call_args_list]
        assert labels == ["Add Note..."]
        assert menu_cls.return_value.exec.called

    def test_context_table_menu_needs_a_real_context_row(self, db, monkeypatch):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _grouped_tab(db)
        menu_cls = MagicMock()
        monkeypatch.setattr(ht, "QMenu", menu_cls)
        tab._context_table.selectRow(0)
        tab._show_context_table_menu(_point())
        assert not menu_cls.called

    def test_accepted_note_is_stored(self, db):
        cid = _seed_context(db, "2402", {"0": -20}, notes="old")
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        with patch(
            "corecycler.gui.history_tab.QInputDialog.getText", return_value=("fresh", True)
        ):
            tab._add_context_note(tab._contexts[0])
        assert db.get_context(cid).notes == "fresh"

    def test_cancelled_note_leaves_the_context_alone(self, db):
        cid = _seed_context(db, "2402", {"0": -20}, notes="old")
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        with patch(
            "corecycler.gui.history_tab.QInputDialog.getText", return_value=("fresh", False)
        ):
            tab._add_context_note(tab._contexts[0])
        assert db.get_context(cid).notes == "old"

    def test_runs_menu_offers_single_row_exports(self, db, monkeypatch):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        menu_cls = MagicMock()
        monkeypatch.setattr(ht, "QMenu", menu_cls)
        tab._runs_table.selectRow(0)
        tab._show_context_menu(_point())
        labels = [c.args[0] for c in menu_cls.return_value.addAction.call_args_list]
        assert labels == ["Export JSON...", "Export CSV...", "Delete"]

    def test_runs_menu_offers_multi_row_actions(self, db, monkeypatch):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        _seed_run(db, "2026-07-21T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        menu_cls = MagicMock()
        monkeypatch.setattr(ht, "QMenu", menu_cls)
        tab._runs_table.selectAll()
        tab._show_context_menu(_point())
        labels = [c.args[0] for c in menu_cls.return_value.addAction.call_args_list]
        assert labels == ["Compare", "Export All as CSV...", "Delete"]

    def test_runs_menu_is_suppressed_in_tuner_view(self, db, monkeypatch):
        _seed_session(db)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        menu_cls = MagicMock()
        monkeypatch.setattr(ht, "QMenu", menu_cls)
        tab._show_context_menu(_point())
        assert not menu_cls.called

    def test_runs_menu_needs_a_selection(self, db, monkeypatch):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.clearSelection()
        menu_cls = MagicMock()
        monkeypatch.setattr(ht, "QMenu", menu_cls)
        tab._show_context_menu(_point())
        assert not menu_cls.called


def _seed_detailed_run(db, ctx_id=None, settings_json='{"backend": "mprime"}'):
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
            cycle_count=2,
            variable_load=True,
            idle_stability_test=30.0,
            total_cores=2,
            cores_passed=1,
            cores_failed=1,
            total_seconds=1200.0,
            context_id=ctx_id,
            bios_version="2402",
            settings_json=settings_json,
        )
    )
    db.insert_core_result(
        CoreResultRecord(
            run_id=rid,
            core_id=0,
            ccd=0,
            cycle=0,
            started_at="2026-07-20T10:00:00+00:00",
            passed=True,
            elapsed_seconds=600.0,
            peak_freq_mhz=5200.0,
            max_temp_c=78.0,
            min_vcore_v=1.05,
            max_vcore_v=1.25,
        )
    )
    db.insert_core_result(
        CoreResultRecord(
            run_id=rid,
            core_id=1,
            ccd=None,
            cycle=0,
            started_at="2026-07-20T10:10:00+00:00",
            passed=False,
            elapsed_seconds=100.0,
            error_message="rounding error",
            error_type="computation",
        )
    )
    return rid


def _seed_telemetry(db, rid):
    samples = []
    for i in range(3):
        samples.append(
            TelemetrySample(
                run_id=rid,
                core_id=0,
                timestamp=f"2026-07-20T10:0{i}:00+00:00",
                freq_mhz=3000.0 + i * 1000.0,
                effective_max_mhz=5000.0,
                temp_c=70.0 + i,
                vcore_v=1.1 + i * 0.01,
            )
        )
        samples.append(
            TelemetrySample(
                run_id=rid,
                core_id=1,
                timestamp=f"2026-07-20T10:0{i}:00+00:00",
                freq_mhz=4900.0 + i * 50.0,
                effective_max_mhz=5000.0,
                temp_c=72.0 + i,
                vcore_v=1.2 + i * 0.01,
            )
        )
    db.insert_telemetry_batch(samples)


def _run_by_id(db, rid):
    return next(r for r in db.list_runs() if r.id == rid)


class TestRunDetail:
    def test_detail_renders_context_events_and_telemetry(self, db):
        cid = _seed_context(
            db, "2402", {"0": -20, "1": -25}, notes="tuned", scalar=3.0, boost=5200
        )
        rid = _seed_detailed_run(db, cid)
        db.insert_event(
            EventRecord(
                run_id=rid,
                timestamp="2026-07-20T10:05:00+00:00",
                event_type="error",
                core_id=1,
                message="boom",
            )
        )
        db.insert_event(
            EventRecord(
                run_id=rid,
                timestamp="2026-07-20T10:06:00+00:00",
                event_type="info",
                core_id=None,
                message="note",
            )
        )
        _seed_telemetry(db, rid)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._show_run_detail(_run_by_id(db, rid))

        info = tab._detail_info.text()
        assert "variable-load" in info
        assert "idle-test=30s" in info

        text = tab._events_log.toPlainText()
        assert "Tuning Context" in text
        assert "BIOS: 2402" in text
        assert "Core 0: -20" in text
        assert "PBO Scalar: 3.0" in text
        assert "Boost Limit: 5200 MHz" in text
        assert "Notes: tuned" in text
        assert "[error] boom" in text
        assert "[info] note" in text
        assert "Total samples: 6" in text
        assert "Boost ceiling: 5000 MHz" in text
        assert "Clock stretch: 40.0%" in text
        assert "Clock stretch: none" in text
        assert "Temp: 70.0-72.0 C" in text
        assert "Vcore: 1.1000-1.1200V" in text
        assert "Settings Snapshot" in text
        assert tab._core_results_table.item(0, 7).text() == "1.0500-1.2500V"
        assert tab._core_results_table.item(1, 1).text() == "-"

    def test_unparsable_settings_are_shown_verbatim(self, db):
        rid = _seed_detailed_run(db, settings_json="not json at all")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._show_run_detail(_run_by_id(db, rid))
        assert "not json at all" in tab._events_log.toPlainText()

    def test_degenerate_metrics_still_reserve_result_table_height(self, db):
        rid = _seed_detailed_run(db)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._show_run_detail(_run_by_id(db, rid))
        assert tab._core_results_table.rowCount() == 2
        tab._core_results_table.verticalHeader().setMinimumSectionSize(1)
        for row in range(2):
            tab._core_results_table.setRowHeight(row, 1)
        tab._core_results_table.horizontalHeader().setFixedHeight(5)
        tab._auto_size_core_results_table()
        assert tab._core_results_table.maximumHeight() >= 80

    def test_expanding_an_open_detail_keeps_the_manual_split(self, db):
        rid = _seed_detailed_run(db)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab.show()
        tab._show_run_detail(_run_by_id(db, rid))
        assert tab._detail_widget.isVisible()
        tab._splitter.setSizes([90, 310])
        before = tab._splitter.sizes()
        tab._expand_detail()
        assert tab._splitter.sizes() == before
        tab.hide()


class TestTunerSessionDetail:
    def test_detail_renders_states_test_log_and_profile(self, db):
        sid = _seed_session(db, "completed")
        tp.save_core_state(
            db,
            sid,
            CoreState(core_id=0, phase=TunerPhase.HARDENED, current_offset=-30, best_offset=-30),
        )
        tp.save_core_state(
            db,
            sid,
            CoreState(core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-18, best_offset=None),
        )
        tp.log_test_result(db, sid, 0, -30, "confirm", True, duration=300.0)
        tp.log_test_result(
            db, sid, 1, -18, "coarse", False, error_msg="rounding error", duration=12.5
        )
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        sess = db.list_tuner_sessions()[0]
        tab._show_tuner_session_detail(sess)

        assert tab._core_results_table.item(0, 5).text() == "PASS"
        assert tab._core_results_table.item(1, 5).text() == "FAIL"
        assert tab._core_results_table.item(0, 4).text() == "1"
        assert tab._core_results_table.item(1, 3).text() == "-"

        text = tab._events_log.toPlainText()
        assert "Test Log" in text
        assert "offset -30" in text
        assert "[confirm] PASS" in text
        assert "rounding error" in text
        assert "300.0s" in text
        assert "Confirmed CO Profile" in text

    def test_unparsable_config_falls_back_to_empty(self, db):
        sid = _seed_session(db, "completed")
        tp.save_core_state(db, sid, CoreState(core_id=0, phase=TunerPhase.NOT_STARTED))
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        sess = db.get_tuner_session(sid)
        sess.config_json = "{not json"
        tab._show_tuner_session_detail(sess)
        assert "coarse=?" in tab._detail_info.text()

    def test_rows_are_skipped_without_a_database(self, db):
        _seed_session(db)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        tab._db = None
        tab._runs_table.clearContents()
        tab._populate_tuner_sessions()
        assert tab._runs_table.rowCount() == 1
        assert tab._runs_table.item(0, 0) is None


class TestLoadCoProfile:
    def test_without_a_selected_session_nothing_is_emitted(self, db):
        tab = _tab(db)
        tab._selected_tuner_session = None
        emitted = []
        tab.load_profile_requested.connect(emitted.append)
        tab._on_load_co_profile()
        assert emitted == []

    def test_a_session_without_offsets_informs_the_user(self, db):
        sid = _seed_session(db)
        tp.save_core_state(
            db, sid, CoreState(core_id=0, phase=TunerPhase.NOT_STARTED, best_offset=None)
        )
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        tab._selected_tuner_session = db.get_tuner_session(sid)
        emitted = []
        tab.load_profile_requested.connect(emitted.append)
        with patch("corecycler.gui.history_tab.QMessageBox.information") as info:
            tab._on_load_co_profile()
        assert info.called
        assert emitted == []

    def test_offsets_are_emitted(self, db):
        sid = _seed_session(db)
        tp.save_core_state(
            db,
            sid,
            CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30, best_offset=-30),
        )
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        tab._selected_tuner_session = db.get_tuner_session(sid)
        emitted = []
        tab.load_profile_requested.connect(emitted.append)
        tab._on_load_co_profile()
        assert emitted == [{0: -30}]


class TestDeleteSelected:
    def test_tuner_view_deletes_the_selected_session(self, db):
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_selected()
        assert db.list_tuner_sessions() == []

    def test_declined_session_delete_keeps_it(self, db):
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_no()):
            tab._delete_selected()
        assert len(db.list_tuner_sessions()) == 1

    def test_grouped_view_deletes_the_selected_context_and_its_runs(self, db):
        cid = _seed_context(db, "2402", {"0": -20})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        _seed_run(db, "2026-07-21T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        tab._context_table.selectRow(0)
        tab._runs_table.clearSelection()
        with patch(
            "corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()
        ) as ask:
            tab._delete_selected()
        assert "2 associated test run(s)" in ask.call_args.args[2]
        assert db.list_contexts() == []
        assert db.list_runs() == []

    def test_declined_context_delete_keeps_everything(self, db):
        cid = _seed_context(db, "2402", {"0": -20})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        tab._context_table.selectRow(0)
        tab._runs_table.clearSelection()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_no()):
            tab._delete_selected()
        assert len(db.list_contexts()) == 1

    def test_context_delete_refuses_an_out_of_range_row(self, db):
        cid = _seed_context(db, "2402", {"0": -20})
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        with patch("corecycler.gui.history_tab.QMessageBox.question") as ask:
            tab._delete_contexts([99])
        assert not ask.called
        assert len(db.list_contexts()) == 1

    def test_flat_view_deletes_the_selected_run(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.selectRow(0)
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_yes()):
            tab._delete_selected()
        assert db.list_runs() == []

    def test_run_delete_refuses_an_out_of_range_row(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question") as ask:
            tab._delete_runs([99])
        assert not ask.called
        assert len(db.list_runs()) == 1

    def test_declined_tuner_session_delete_keeps_it(self, db):
        _seed_session(db, "completed")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_TUNER
        tab.refresh()
        with patch("corecycler.gui.history_tab.QMessageBox.question", return_value=_no()):
            tab._delete_tuner_sessions([0])
        assert len(db.list_tuner_sessions()) == 1


class TestCompare:
    def test_comparison_needs_two_runs(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.selectRow(0)
        tab._compare_selected()
        assert "Comparing" not in tab._detail_info.text()

    def test_rows_beyond_the_displayed_list_are_skipped(self, db):
        _seed_run(db, "2026-07-20T10:00:00+00:00")
        _seed_run(db, "2026-07-21T10:00:00+00:00")
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.selectAll()
        tab._displayed_runs = []
        tab._compare_selected()
        assert "Comparing" not in tab._detail_info.text()

    def test_a_core_missing_from_one_run_reads_as_absent(self, db):
        first = _seed_detailed_run(db)
        second = db.create_run(
            RunRecord(
                started_at="2026-07-21T10:00:00+00:00",
                status="completed",
                backend="mprime",
                stress_mode="SSE",
                cpu_model="Test 8C",
                total_cores=1,
                cores_passed=1,
                total_seconds=600.0,
            )
        )
        db.insert_core_result(
            CoreResultRecord(
                run_id=second,
                core_id=0,
                cycle=0,
                started_at="2026-07-21T10:00:00+00:00",
                passed=True,
                elapsed_seconds=600.0,
            )
        )
        assert first != second
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._runs_table.selectAll()
        tab._compare_selected()
        assert "Comparing 2 runs across 2 cores" in tab._detail_info.text()
        order = [r.id for r in tab._displayed_runs]
        both = 1 + order.index(first) * 2
        one = 1 + order.index(second) * 2
        assert tab._core_results_table.item(1, both).text() == "FAIL"
        assert tab._core_results_table.item(1, one).text() == "-"
        assert tab._core_results_table.item(1, one + 1).text() == "-"
        assert "Run Comparison" in tab._events_log.toPlainText()


class TestHelpers:
    def test_co_summary_rejects_unparsable_json(self):
        assert ht._co_summary("not json") == "none"

    def test_co_summary_rejects_a_missing_value(self):
        assert ht._co_summary(None) == "none"

    def test_co_summary_reads_an_empty_map_as_none(self):
        assert ht._co_summary("{}") == "none"

    def test_co_summary_collapses_a_uniform_profile(self):
        assert ht._co_summary('{"0": -20, "1": -20}') == "all -20"

    def test_co_summary_spans_a_mixed_profile(self):
        assert ht._co_summary('{"0": -20, "1": -30, "2": -25}') == "mixed [-30..-20]"

    def test_best_result_without_completed_runs(self):
        runs = [
            RunRecord(status="stopped", total_cores=4, cores_passed=4),
            RunRecord(status="completed", total_cores=0),
        ]
        assert ht._best_result(runs) == "-"

    def test_best_result_picks_the_highest_pass_rate(self):
        runs = [
            RunRecord(status="completed", total_cores=4, cores_passed=2),
            RunRecord(status="completed", total_cores=4, cores_passed=3),
        ]
        assert ht._best_result(runs) == "3/4"


class TestMalformedContextData:
    """A stored CO profile is DB content: a hand-edited row or a database merged
    from elsewhere can hold anything, and a display slot must never raise."""

    def test_a_non_object_profile_reads_as_none(self):
        assert ht._co_summary("[1, 2, 3]") == "none"
        assert ht._co_summary('"abc"') == "none"
        assert ht._co_summary("5") == "none"

    def test_mixed_value_types_still_summarise(self):
        assert ht._co_summary('{"0": -20, "1": "x"}') == "mixed"

    def test_a_malformed_profile_does_not_break_the_context_table(self, db):
        cid = db.create_context(
            TuningContextRecord(
                bios_version="2402", co_offsets_json="[1, 2, 3]", co_hash="broken"
            )
        )
        _seed_run(db, "2026-07-20T10:00:00+00:00", context_id=cid)
        tab = _grouped_tab(db)
        assert tab._context_table.rowCount() == 1
        assert tab._context_table.item(0, 1).text() == "none"

    def test_non_numeric_core_ids_still_render_in_the_detail(self, db):
        cid = db.create_context(
            TuningContextRecord(
                bios_version="2402",
                co_offsets_json='{"a": -20, "b": -25}',
                co_hash="letters",
            )
        )
        rid = _seed_detailed_run(db, cid)
        tab = _tab(db)
        tab._view_mode = tab.VIEW_ALL
        tab.refresh()
        tab._show_run_detail(_run_by_id(db, rid))
        text = tab._events_log.toPlainText()
        assert "Core a: -20" in text
        assert "Core b: -25" in text

    @settings(deadline=None, max_examples=250)
    @given(st.text())
    def test_no_stored_text_can_break_the_summary(self, payload):
        assert isinstance(ht._co_summary(payload), str)

    @settings(deadline=None, max_examples=250)
    @given(
        st.recursive(
            st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
            lambda children: st.lists(children) | st.dictionaries(st.text(), children),
            max_leaves=8,
        )
    )
    def test_no_stored_json_value_can_break_the_summary(self, value):
        assert isinstance(ht._co_summary(json.dumps(value)), str)
