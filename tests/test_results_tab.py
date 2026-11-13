"""ResultsTab dashboard: population, per-core updates, log, summary, injection."""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from PySide6.QtWidgets import QLabel

from corecycler.engine.scheduler import CoreTestStatus


def _tab():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from corecycler.gui.results_tab import ResultsTab

    return ResultsTab()


def _statuses(n=4):
    return {i: CoreTestStatus(core_id=i, ccd=0 if i < 2 else 1, state="pending") for i in range(n)}


class TestResultsTab:
    def test_init_cores_populates_rows(self):
        tab = _tab()
        tab.init_cores(_statuses(4))
        assert tab._table.rowCount() == 4

    def test_update_known_core_sets_row(self):
        tab = _tab()
        tab.init_cores(_statuses(4))
        tab.update_core(2, CoreTestStatus(core_id=2, state="passed", iterations=9))
        assert tab._table.item(tab._row_by_core[2], 4).text() == "9"

    def test_update_unknown_core_is_noop(self):
        tab = _tab()
        tab.init_cores(_statuses(2))
        tab.update_core(99, CoreTestStatus(core_id=99, state="failed"))

    @pytest.mark.parametrize("state", ["pending", "testing", "passed", "failed", "skipped"])
    def test_all_states_render_without_crash(self, state):
        tab = _tab()
        tab.init_cores(_statuses(1))
        tab.update_core(0, CoreTestStatus(core_id=0, state=state, errors=1 if state == "failed" else 0))

    def test_error_row_shows_count_and_last_error(self):
        tab = _tab()
        tab.init_cores(_statuses(1))
        tab.update_core(0, CoreTestStatus(core_id=0, state="failed", errors=3, last_error="rounding"))
        row = tab._row_by_core[0]
        assert tab._table.item(row, 3).text() == "3"
        assert tab._table.item(row, 6).toolTip() == "rounding"

    def test_none_ccd_renders_dash(self):
        tab = _tab()
        tab.init_cores({0: CoreTestStatus(core_id=0, ccd=None, state="pending")})
        assert tab._table.item(0, 1).text() == "-"

    def test_log_and_error_entries(self):
        tab = _tab()
        tab.add_error(1, "boom")
        tab.add_log(2, "note")
        text = tab._log.toPlainText()
        assert "boom" in text and "note" in text

    @pytest.mark.req("T23.results.fresh")
    def test_a_new_run_starts_with_no_summary_rows_or_log_from_the_last(self):
        tab = _tab()
        fresh = [label.text() for label in tab.findChildren(QLabel)]
        tab.init_cores(_statuses(3))
        tab.update_summary(total=3, passed=2, failed=1, elapsed=61.0, cycle=1, total_cycles=2)
        tab.add_error(1, "boom")
        assert tab._passed_label.text() == "Passed: 2"
        tab.init_cores(_statuses(2))
        assert [label.text() for label in tab.findChildren(QLabel)] == fresh
        assert (tab._table.rowCount(), tab._log.toPlainText()) == (2, "")


RESULTS_CONTROLS = {"QTableCornerButton": "selects every cell of the core table"}


@pytest.mark.req("T23.contract.results")
def test_every_control_of_the_results_tab_is_listed_with_its_reaction():
    from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit

    tab = _tab()
    tab.init_cores(_statuses(2))
    buttons = {button.text() or button.metaObject().className() for button in tab.findChildren(QAbstractButton)}
    assert buttons == set(RESULTS_CONTROLS)
    assert [*tab.findChildren(QAbstractSpinBox), *tab.findChildren(QComboBox), *tab.findChildren(QLineEdit)] == []
    tab.findChild(QAbstractButton).click()
    assert len(tab._table.selectedItems()) == 2 * tab._table.columnCount()
