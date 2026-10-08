from __future__ import annotations

import sys
from dataclasses import replace

import pytest

if not hasattr(sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("History tab tests require real PySide6", allow_module_level=True)

from PySide6.QtWidgets import QApplication, QMessageBox

from corecycler.gui.history_tab import HistoryTab
from corecycler.gui.style import tuner_phase_label
from corecycler.history.db import HistoryDB
from corecycler.smu.writer_lock import co_writer
from corecycler.tuner.live import Identity, Store
from corecycler.tuner.machine.types import Phase
from tests._machine import this_boot
from tests._old_rows import seed_old_session
from tests._tuner_world import save_machine_session


@pytest.fixture(autouse=True, scope="module")
def _qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def dialogs(monkeypatch):
    from unittest.mock import MagicMock

    import corecycler.gui.history_tab as ht

    box = MagicMock()
    box.StandardButton = QMessageBox.StandardButton
    box.question.return_value = QMessageBox.StandardButton.No
    monkeypatch.setattr(ht, "QMessageBox", box)
    return box


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def tuner_view(db) -> HistoryTab:
    tab = HistoryTab(db)
    tab._view_mode = tab.VIEW_TUNER
    tab.refresh()
    return tab


def cell(tab: HistoryTab, row: int, column: int) -> str:
    return tab._runs_table.item(row, column).text()


def test_sessions_list_newest_first_and_leave_out_old_engine_rows(db, tmp_path):
    first = save_machine_session(db, tmp_path, "completed", session_id=1)
    old = seed_old_session(db)
    saved = save_machine_session(db, tmp_path, "paused", session_id=old + 1)
    tab = tuner_view(db)
    assert [sess.id for sess in tab._tuner_sessions] == [saved.session_id, first.session_id]
    assert (cell(tab, 0, 1), cell(tab, 0, 3), cell(tab, 0, 4)) == ("Paused", "2", "0/2")
    assert tab._total_label.text() == "Tuning sessions: 2"
    assert tab._view_select.currentText() == "Tuning sessions (2)"


def test_a_proven_session_counts_its_proven_cores(db, tmp_path):
    saved = save_machine_session(db, tmp_path, "completed")
    proven = replace(saved, cores=tuple(replace(record, phase=Phase.PROVEN) for record in saved.cores))
    Store(db, tmp_path / "state", Identity("bios-1", "cpu-1", None)).save(proven, this_boot())
    tab = tuner_view(db)
    assert cell(tab, 0, 4) == "2/2"


def test_the_detail_shows_cores_tests_events_and_boots(db, tmp_path):
    saved = save_machine_session(db, tmp_path, "paused")
    core = saved.cores[0].core
    row = db.insert_tuner_test(
        saved.session_id, this_boot(), 1, "search", "1t", "1t", core, -3, "[]", "[0]", 60.0, "{}"
    )
    db.end_tuner_test(
        row, "silicon", 12.0, "[]", None, "[]", "y-cruncher error", "{}", "", peaks_json=f'{{"{core}": 4421.0}}'
    )
    db.insert_tuner_event(saved.session_id, "user: paused by the owner", this_boot(), "paused")
    db.insert_tuner_boot(this_boot(), "2402", None, None, None, "[]", "{}")
    tab = tuner_view(db)
    assert tab._core_results_table.rowCount() == len(saved.cores)
    first = [tab._core_results_table.item(0, column).text() for column in range(7)]
    phase = tuner_phase_label(saved.cores[0].phase)
    assert first == [str(core), phase, str(saved.cores[0].value), "-", "1", "Failed", "4421"]
    assert tab._core_results_table.item(1, 6).text() == "-"
    text = tab._events_log.toPlainText()
    assert f"core {core}, Search, one thread, at -3: Failed  y-cruncher error" in text
    cfg = saved.config
    assert f"  Deepest offset to try: {cfg.max_offset} counts" in text
    assert "  Search time limit per core: 7200 s (2 h)" in text
    assert "  Endurance: off" in text and "  Burst segment: 480 s (8 min)" in text
    assert "  Fine step: 1 count\n" in text and "  Pause after first-value failures: 0 cores" in text
    assert "paused: user: paused by the owner" in text
    assert f"  {this_boot()}  first seen " in text and "BIOS 2402" in text
    assert f", deepest offset {cfg.max_offset}, {cfg.k_1t} and {cfg.k_2t} passes to prove" in tab._detail_info.text()
    assert tab._detail_info.text().endswith("power state: balanced profile")
    assert f"  core {core}: 4421 MHz with the scaling maximum at unknown, boost unknown" in text
    assert not tab._load_co_btn.isEnabled()


def test_a_completed_session_loads_its_values_into_the_co_tab(db, tmp_path):
    saved = save_machine_session(db, tmp_path, "completed")
    tab = tuner_view(db)
    assert tab._load_co_btn.isEnabled()
    loaded = []
    tab.load_profile_requested.connect(loaded.append)
    tab._on_load_co_profile()
    assert loaded == [{record.core: record.value for record in saved.cores}]


def test_a_continued_session_names_its_parent(db, tmp_path):
    parent = save_machine_session(db, tmp_path, "superseded", session_id=1)
    child = save_machine_session(db, tmp_path, "paused", session_id=2)
    db.link_session(child.session_id, parent.session_id, "continue")
    tab = tuner_view(db)
    assert "continued from session 1" in tab._detail_info.text()


def test_an_unreadable_session_is_named_in_the_list_and_the_detail(db, tmp_path):
    saved = save_machine_session(db, tmp_path, "paused")
    db._execute_raw("UPDATE tuner_sessions SET machine_json='{}' WHERE id=?", (saved.session_id,))
    tab = tuner_view(db)
    assert cell(tab, 0, 4) == "unreadable"
    assert tab._detail_info.text().startswith(f"Session #{saved.session_id} unreadable: ")
    assert tab._core_results_table.rowCount() == 0
    assert not tab._load_co_btn.isEnabled()


class TestDelete:
    def test_a_session_a_driver_runs_is_kept(self, db, tmp_path, dialogs):
        save_machine_session(db, tmp_path, "running")
        tab = tuner_view(db)
        with co_writer(record=True):
            tab._delete_tuner_sessions([0])
        assert "mid-run" in dialogs.warning.call_args.args[2]
        assert len(db.list_machine_sessions()) == 1

    def test_an_interrupted_session_without_a_driver_can_go(self, db, tmp_path, dialogs):
        save_machine_session(db, tmp_path, "running")
        tab = tuner_view(db)
        dialogs.question.return_value = QMessageBox.StandardButton.Yes
        tab._delete_tuner_sessions([0])
        assert db.list_machine_sessions() == []

    def test_a_parent_goes_only_with_or_after_its_successor(self, db, tmp_path, dialogs):
        save_machine_session(db, tmp_path, "superseded", session_id=1)
        save_machine_session(db, tmp_path, "paused", session_id=2)
        db.link_session(2, 1, "restart")
        tab = tuner_view(db)
        dialogs.question.return_value = QMessageBox.StandardButton.Yes
        tab._delete_tuner_sessions([1])
        assert "Session 1 carries on as session 2" in dialogs.warning.call_args.args[2]
        assert [sess.id for sess in db.list_machine_sessions()] == [2, 1]
        tab._delete_tuner_sessions([0, 1])
        assert db.list_machine_sessions() == []
