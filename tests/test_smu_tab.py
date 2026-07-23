"""SMUTab CO write UI: profile clamping, tuner lock, live updates, round-trip.

The reported crash lived here; these exercise the write-path guards and the
clamp/adjust warnings that keep a hostile profile from silently writing.
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


def _topo():
    topo = CPUTopology(model_name="Test 8C", family=26, model=0x44, physical_cores=8, ccds=2)
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0 if cid < 4 else 1, ccx=None, logical_cpus=(cid, cid + 8))
    return topo


def _tab():
    _qapp()
    from corecycler.gui.smu_tab import SMUTab

    return SMUTab(_topo())


class TestProfileLoad:
    def test_valid_profile_populates_spinboxes(self):
        tab = _tab()
        tab.set_co_profile({0: -20, 1: -30, 7: -10})
        assert tab._spinboxes[0].value() == -20
        assert tab._spinboxes[7].value() == -10

    def test_out_of_range_offset_is_clamped_and_warns(self):
        tab = _tab()
        with patch("corecycler.gui.smu_tab.QMessageBox.warning") as warn:
            tab.set_co_profile({0: -200})
        assert tab._spinboxes[0].value() == tab._commands.co_range[0]
        assert warn.called

    def test_offset_for_absent_core_warns(self):
        tab = _tab()
        with patch("corecycler.gui.smu_tab.QMessageBox.warning") as warn:
            tab.set_co_profile({0: -10, 999: -10})
        assert warn.called
        assert tab._spinboxes[0].value() == -10


class TestTunerLock:
    def test_tuner_running_disables_writes(self):
        tab = _tab()
        tab._smu = MagicMock()
        tab._smu.is_available.return_value = True
        tab.set_tuner_running(True)
        assert tab._tuner_active is True
        assert not tab._apply_all_btn.isEnabled()
        assert not tab._reset_btn.isEnabled()
        for spin in tab._spinboxes.values():
            assert not spin.isEnabled()

    def test_tuner_stopped_reenables_writes(self):
        tab = _tab()
        tab._smu = MagicMock()
        tab._smu.is_available.return_value = True
        tab._smu.has_backup.return_value = False
        tab._smu.get_co_offset.return_value = 0
        tab.set_tuner_running(True)
        tab.set_tuner_running(False)
        assert tab._tuner_active is False
        assert tab._apply_all_btn.isEnabled()
        for spin in tab._spinboxes.values():
            assert spin.isEnabled()


class TestLiveUpdate:
    def test_update_current_co_writes_the_cell(self):
        tab = _tab()
        tab.update_current_co(3, -37)
        row = tab._core_row_map()[3]
        assert tab._table.item(row, 2).text() == "-37"

    def test_core_row_map_matches_sorted_cores(self):
        tab = _tab()
        assert tab._core_row_map() == {cid: cid for cid in range(8)}


class TestReadBack:
    def test_read_all_shows_err_on_failed_read(self):
        tab = _tab()
        tab._smu = MagicMock()
        tab._smu.get_co_offset.return_value = None
        tab._read_all_co()
        assert tab._table.item(0, 2).text() == "ERR"

    def test_dry_run_toggle_propagates_to_driver(self):
        tab = _tab()
        tab._smu = MagicMock()
        tab._dry_run_cb.setChecked(True)
        assert tab._smu.dry_run is True
        assert "[DRY]" in tab._apply_all_btn.text()
