"""smu_tab topology states and CO profile save/load coverage."""

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


def _topo(model_name="Test", family=26, model=0x44, cores=8) -> CPUTopology:
    topo = CPUTopology(model_name=model_name, family=family, model=model, physical_cores=cores, ccds=1)
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
    return t


def _file_dialog(monkeypatch, *, save=None, load=None):
    import corecycler.gui.smu_tab as st

    fd = MagicMock()
    fd.getSaveFileName.return_value = (save if save is not None else "", "")
    fd.getOpenFileName.return_value = (load if load is not None else "", "")
    monkeypatch.setattr(st, "QFileDialog", fd)
    return fd


class TestSetTopologyStates:
    def test_connected_with_co_enables_writes(self, tab, monkeypatch):
        import corecycler.gui.smu_tab as st

        monkeypatch.setattr(st.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True))
        tab.set_topology(_topo())
        assert "Connected" in tab._status_label.text()
        assert tab._apply_all_btn.isEnabled()
        assert "CO Range" in tab._range_label.text()

    def test_connected_without_co_support(self, tab, monkeypatch):
        import corecycler.gui.smu_tab as st

        monkeypatch.setattr(st.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True))
        tab.set_topology(_topo(model_name="AMD Ryzen 9 3950X", family=23, model=0x71))
        assert "no CO support" in tab._status_label.text()
        assert tab._apply_all_btn.isEnabled() is False

    def test_driver_not_loaded(self, tab, monkeypatch):
        import corecycler.gui.smu_tab as st

        monkeypatch.setattr(st.RyzenSMU, "is_available", staticmethod(lambda *a, **k: False))
        tab.set_topology(_topo())
        assert "Driver not loaded" in tab._status_label.text()

    def test_unsupported_generation(self, tab):
        tab.set_topology(_topo(model_name="Intel Core i9", family=6, model=0xA7))
        assert "Unsupported CPU generation" in tab._status_label.text()


class TestSaveCoProfile:
    def test_cancelled_dialog_writes_nothing(self, tab, monkeypatch):
        from corecycler.config import settings as settings_mod

        _file_dialog(monkeypatch, save="")
        called = []
        monkeypatch.setattr(settings_mod, "save_co_profile", lambda *a, **k: called.append(a))
        tab._save_co_profile()
        assert called == []

    def test_saves_current_spinbox_values(self, tab, monkeypatch, tmp_path):
        from corecycler.config import settings as settings_mod

        _file_dialog(monkeypatch, save=str(tmp_path / "co.json"))
        tab._spinboxes[0].setValue(-21)
        seen: dict = {}
        monkeypatch.setattr(
            settings_mod,
            "save_co_profile",
            lambda offsets, path, **kw: seen.update(offsets=offsets, kw=kw),
        )
        tab._save_co_profile()
        assert seen["offsets"][0] == -21
        assert seen["kw"]["source"] == "manual"

    def test_save_failure_is_surfaced(self, tab, monkeypatch, tmp_path):
        import corecycler.gui.smu_tab as st
        from corecycler.config import settings as settings_mod

        _file_dialog(monkeypatch, save=str(tmp_path / "co.json"))

        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(settings_mod, "save_co_profile", boom)
        tab._save_co_profile()
        st.QMessageBox.warning.assert_called()


class TestLoadCoProfile:
    def test_cancelled_dialog_loads_nothing(self, tab, monkeypatch):
        from corecycler.config import settings as settings_mod

        _file_dialog(monkeypatch, load="")
        called = []
        monkeypatch.setattr(settings_mod, "load_co_profile", lambda p: called.append(p) or {})
        tab._load_co_profile()
        assert called == []

    def test_load_failure_is_surfaced(self, tab, monkeypatch, tmp_path):
        import corecycler.gui.smu_tab as st
        from corecycler.config import settings as settings_mod

        _file_dialog(monkeypatch, load=str(tmp_path / "co.json"))

        def boom(_p):
            raise ValueError("bad json")

        monkeypatch.setattr(settings_mod, "load_co_profile", boom)
        tab._load_co_profile()
        st.QMessageBox.warning.assert_called()

    def test_empty_profile_is_surfaced(self, tab, monkeypatch, tmp_path):
        import corecycler.gui.smu_tab as st
        from corecycler.config import settings as settings_mod

        _file_dialog(monkeypatch, load=str(tmp_path / "co.json"))
        monkeypatch.setattr(settings_mod, "load_co_profile", lambda _p: {})
        tab._load_co_profile()
        st.QMessageBox.warning.assert_called()

    def test_loaded_profile_fills_spinboxes_and_banner(self, tab, monkeypatch, tmp_path):
        from corecycler.config import settings as settings_mod

        _file_dialog(monkeypatch, load=str(tmp_path / "mine.json"))
        monkeypatch.setattr(settings_mod, "load_co_profile", lambda _p: {0: -18, 1: -12})
        tab._load_co_profile()
        assert tab._spinboxes[0].value() == -18
        assert "mine.json" in tab._profile_banner.text()


class TestTunerLockAndAccessors:
    def test_smu_property_reflects_driver(self, tab):
        tab._smu = None
        assert tab.smu is None
        driver = MagicMock()
        tab._smu = driver
        assert tab.smu is driver

    def test_set_tuner_running_toggles_lock(self, tab):
        tab.set_tuner_running(True)
        assert tab._tuner_active is True
        tab.set_tuner_running(False)
        assert tab._tuner_active is False

    def test_core_row_map_covers_every_core(self, tab):
        assert set(tab._core_row_map()) == set(tab._spinboxes)
