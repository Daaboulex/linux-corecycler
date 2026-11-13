"""Cold-widget control regression tests.

Guards the defect class where a control handler assumes a lifecycle method
(set_tuner_running, start, ...) already ran: on a fresh app, before any test or
tuner session, clicking a button must never raise. The reported crash was
SMUTab._reset_all_co reading self._tuner_active when __init__ never set it.

These run only under real PySide6 (offscreen); the conftest stub is skipped.
"""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo() -> CPUTopology:
    topo = CPUTopology(
        model_name="Test 8C/16T",
        family=26,
        model=0x44,
        physical_cores=8,
        logical_cpus_count=16,
        ccds=2,
    )
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(
            core_id=cid,
            ccd=0 if cid < 4 else 1,
            ccx=None,
            logical_cpus=(cid, cid + 8),
        )
    return topo


def _available_smu() -> MagicMock:
    smu = MagicMock()
    smu.commands.get_curve_shaper_cmd = None
    smu.get_co_offset.return_value = 0
    smu.reset_all_co.return_value = True
    smu.set_co_offset.return_value = True
    smu.backup_co_offsets.return_value = {0: 0}
    smu.has_backup.return_value = True
    smu.restore_co_offsets.return_value = (True, [])
    smu.is_available.return_value = True
    return smu


class TestSMUColdTab:
    def test_tuner_active_initialized_at_construction(self):
        _qapp()
        from corecycler.gui.smu_tab import SMUTab

        tab = SMUTab(_topo())
        assert tab._tuner_active is False

    def test_write_handlers_do_not_crash_before_tuner_lifecycle(self):
        _qapp()
        from PySide6.QtWidgets import QMessageBox

        from corecycler.gui.smu_tab import SMUTab

        tab = SMUTab(_topo())
        tab._smu = _available_smu()
        with patch.object(QMessageBox, "warning", return_value=QMessageBox.StandardButton.Yes):
            tab._reset_all_co()
            next(iter(tab._spinboxes.values())).setValue(-1)
            tab._apply_changed()
        assert tab._smu.set_co_offset.called
        assert tab._smu.reset_all_co.called


class TestHistorySortMapping:
    def test_a_sorted_row_resolves_to_the_record_it_shows(self):
        _qapp()
        from PySide6.QtCore import Qt

        from corecycler.gui.history_tab import HistoryTab
        from corecycler.history.db import RunRecord
        from corecycler.history.timefmt import format_local

        tab = HistoryTab(None)
        runs = [
            RunRecord(id=10, started_at="2026-07-20T10:00:00+00:00", status="completed", backend="mprime"),
            RunRecord(id=11, started_at="2026-07-21T10:00:00+00:00", status="completed", backend="ycruncher"),
            RunRecord(id=12, started_at="2026-07-22T10:00:00+00:00", status="completed", backend="stress-ng"),
        ]
        tab._populate_runs_table(runs)
        tab._runs_table.sortItems(0, Qt.SortOrder.DescendingOrder)

        for vis_row in range(tab._runs_table.rowCount()):
            tab._runs_table.clearSelection()
            tab._runs_table.selectRow(vis_row)
            selected = tab._selected_run_rows()
            assert len(selected) == 1
            logical = selected[0]
            shown_date = tab._runs_table.item(vis_row, 0).text()
            assert format_local(runs[logical].started_at) == shown_date, (
                "a sorted row resolved to another record's list index: a delete or export would act on the wrong run"
            )
