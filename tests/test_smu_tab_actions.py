"""smu_tab CO action coverage: read/apply/reset/backup/restore branch matrix."""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(cores: int = 4) -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=cores, ccds=1)
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
    return topo


@pytest.fixture
def tab(monkeypatch):
    import corecycler.gui.smu_tab as st

    monkeypatch.setattr(st, "QMessageBox", MagicMock())
    _qapp()
    t = st.SMUTab(topology=_topo())
    t._tuner_active = False
    monkeypatch.setattr(t, "_confirm_co_write", lambda _detail: True)
    return t


def _smu(**kw):
    smu = MagicMock()
    smu.get_co_offset.return_value = kw.get("get", -10)
    smu.set_co_offset.return_value = kw.get("set", True)
    smu.reset_all_co.return_value = kw.get("reset", True)
    smu.has_backup.return_value = kw.get("has_backup", True)
    smu.backup_co_offsets.return_value = kw.get("backup", {0: -10, 1: -5})
    smu.restore_co_offsets.return_value = kw.get("restore", (True, []))
    return smu


class TestReadAllCo:
    def test_no_smu_is_a_noop(self, tab):
        tab._smu = None
        tab._read_all_co()

    def test_reads_and_fills_spinboxes(self, tab):
        tab._smu = _smu(get=-20)
        tab._read_all_co()
        assert tab._spinboxes[0].value() == -20

    def test_retries_then_succeeds(self, tab):
        smu = _smu()
        smu.get_co_offset.side_effect = [None, -7] + [-7] * 32
        tab._smu = smu
        tab._read_all_co()
        assert tab._spinboxes[0].value() == -7

    def test_persistent_failure_marks_err(self, tab):
        smu = _smu()
        smu.get_co_offset.return_value = None
        tab._smu = smu
        tab._read_all_co()
        assert tab._table.item(0, 2).text() == "ERR"


class TestApplySingle:
    def test_without_driver_warns(self, tab):
        tab._smu = None
        tab._apply_single(0)

    def test_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._apply_single(0)
        tab._smu.set_co_offset.assert_not_called()

    def test_unknown_core_is_a_noop(self, tab):
        tab._smu = _smu()
        tab._apply_single(999)
        tab._smu.set_co_offset.assert_not_called()

    def test_declined_confirmation_does_not_write(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._apply_single(0)
        tab._smu.set_co_offset.assert_not_called()

    def test_successful_write_updates_table(self, tab):
        tab._smu = _smu(set=True)
        tab._spinboxes[0].setValue(-15)
        tab._apply_single(0)
        assert tab._table.item(0, 2).text() == "-15"

    def test_failed_write_warns(self, tab):
        tab._smu = _smu(set=False)
        tab._apply_single(0)
        tab._smu.set_co_offset.assert_called_once()


class TestApplyAllAndReset:
    def test_apply_all_without_driver(self, tab):
        tab._smu = None
        tab._apply_all_co()

    def test_apply_all_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._apply_all_co()
        tab._smu.set_co_offset.assert_not_called()

    def test_apply_all_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._apply_all_co()
        tab._smu.set_co_offset.assert_not_called()

    def test_apply_all_success_hides_banner_and_reenables(self, tab):
        tab._smu = _smu(set=True)
        tab._apply_all_co()
        assert tab._profile_banner.isHidden()
        assert tab._apply_all_btn.isEnabled()

    def test_apply_all_partial_failure_reenables(self, tab):
        tab._smu = _smu(set=False)
        tab._apply_all_co()
        assert tab._apply_all_btn.isEnabled()
        assert tab._reset_btn.isEnabled()

    def test_reset_without_driver(self, tab):
        tab._smu = None
        tab._reset_all_co()

    def test_reset_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._reset_all_co()
        tab._smu.reset_all_co.assert_not_called()

    def test_reset_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._reset_all_co()
        tab._smu.reset_all_co.assert_not_called()

    def test_reset_uses_bulk_command(self, tab):
        tab._smu = _smu(reset=True)
        tab._reset_all_co()
        tab._smu.reset_all_co.assert_called_once()
        tab._smu.set_co_offset.assert_not_called()

    def test_reset_falls_back_to_per_core_writes(self, tab):
        tab._smu = _smu(reset=False)
        tab._reset_all_co()
        assert tab._smu.set_co_offset.call_count == len(tab._spinboxes)


class TestBackupRestore:
    def test_backup_without_driver(self, tab):
        tab._smu = None
        tab._backup_co()

    def test_backup_enables_restore(self, tab):
        tab._smu = _smu()
        tab._backup_co()
        assert tab._restore_btn.isEnabled()

    def test_restore_without_backup_warns(self, tab):
        tab._smu = _smu(has_backup=False)
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_not_called()

    def test_restore_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_co_write", lambda _d: False)
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_not_called()

    def test_restore_success_rereads(self, tab):
        tab._smu = _smu(restore=(True, []))
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_called_once()

    def test_restore_partial_failure_still_rereads(self, tab):
        tab._smu = _smu(restore=(False, [1]))
        tab._restore_co()
        tab._smu.get_co_offset.assert_called()
