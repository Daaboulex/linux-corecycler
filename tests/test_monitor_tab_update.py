"""monitor_tab live-update coverage: the _do_update read/branch matrix."""

from __future__ import annotations

import sys as _sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo() -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=4, ccds=2)
    for cid in range(4):
        ccd = 0 if cid < 2 else 1
        core = PhysicalCore(core_id=cid, ccd=ccd, ccx=None, logical_cpus=(cid,))
        if ccd == 0:
            object.__setattr__(core, "has_vcache", True)
        topo.cores[cid] = core
    return topo


def _tab():
    _qapp()
    from corecycler.gui.monitor_tab import MonitorTab

    return MonitorTab(topology=_topo())


def _hwmon_data(tctl=60.0, tccd=None, vcore=1.2):
    return SimpleNamespace(
        tctl_c=tctl,
        tccd_temps=tccd if tccd is not None else {1: 61.0, 2: 59.0},
        vcore_v=vcore,
    )


def _dual(actual=5000.0, ceiling=5200.0):
    return {cid: SimpleNamespace(actual_mhz=actual, effective_max_mhz=ceiling) for cid in range(4)}


def _drive(tab, monkeypatch, *, dual=None, simple=None, watts=None, msr_ok=False, pkg=None, hwmon=None):
    import corecycler.gui.monitor_tab as mt

    monkeypatch.setattr(mt, "read_core_frequencies_dual", lambda: dual if dual is not None else {})
    monkeypatch.setattr(mt, "read_core_frequencies", lambda: simple if simple is not None else {})

    tab._hwmon = MagicMock()
    tab._hwmon.read.return_value = hwmon if hwmon is not None else _hwmon_data()
    tab._power = MagicMock()
    tab._power.read_power_watts.return_value = watts
    tab._msr = MagicMock()
    tab._msr.is_available.return_value = msr_ok
    tab._msr.read_package_power.return_value = pkg
    tab._msr.read_clock_stretch.return_value = {0: SimpleNamespace(stretch_pct=2.5)}
    tab._msr.read_core_power.return_value = {0: SimpleNamespace(watts=12.0)}
    tab._cpu_usage = MagicMock()
    tab._cpu_usage.read.return_value = {0: 50.0, 1: 40.0, 2: 30.0, 3: 20.0}
    tab._pmtable = MagicMock()
    tab._pmtable.is_available.return_value = False
    tab._update()


class TestMonitorUpdateMatrix:
    def test_sysfs_power_and_per_ccd_labels(self, monkeypatch):
        tab = _tab()
        _drive(tab, monkeypatch, dual=_dual(), watts=95.0)
        assert "95.0W" in tab._power_label.text()
        assert 0 in tab._ccd_temp_labels
        assert "VC" in tab._ccd_temp_labels[0].text()
        assert "VC" not in tab._ccd_temp_labels[1].text()

    def test_msr_power_fallback_used_when_sysfs_absent(self, monkeypatch):
        tab = _tab()
        _drive(tab, monkeypatch, dual=_dual(), watts=None, msr_ok=True, pkg=88.0)
        assert "88.0W" in tab._power_label.text()

    def test_msr_fallback_failure_marks_power_stale(self, monkeypatch):
        tab = _tab()
        tab._power_fail_count = tab._STALE_THRESHOLD
        _drive(tab, monkeypatch, dual=_dual(), watts=None, msr_ok=True, pkg=None)
        assert tab._power_label.styleSheet() == tab._STALE_STYLE

    def test_no_power_source_marks_power_stale(self, monkeypatch):
        tab = _tab()
        tab._power_fail_count = tab._STALE_THRESHOLD
        _drive(tab, monkeypatch, dual=_dual(), watts=None, msr_ok=False)
        assert tab._power_label.styleSheet() == tab._STALE_STYLE

    def test_falls_back_to_simple_frequency_read(self, monkeypatch):
        import corecycler.gui.monitor_tab as mt

        tab = _tab()
        simple = MagicMock(return_value={0: 4800.0, 1: 4700.0})
        monkeypatch.setattr(mt, "read_core_frequencies_dual", lambda: {})
        monkeypatch.setattr(mt, "read_core_frequencies", simple)
        tab._hwmon = MagicMock()
        tab._hwmon.read.return_value = _hwmon_data()
        tab._power = MagicMock()
        tab._power.read_power_watts.return_value = 50.0
        tab._msr = MagicMock()
        tab._msr.is_available.return_value = False
        tab._cpu_usage = MagicMock()
        tab._cpu_usage.read.return_value = {}
        tab._pmtable = MagicMock()
        tab._pmtable.is_available.return_value = False
        tab._update()
        simple.assert_called_once()

    def test_boost_above_ceiling_raises_max(self, monkeypatch):
        tab = _tab()
        tab._max_core_freq = 5000
        _drive(tab, monkeypatch, dual=_dual(actual=6200.0, ceiling=6300.0), watts=50.0)
        assert tab._max_core_freq == 6200.0
        assert "6200" in tab._max_freq_label.text()

    def test_missing_hwmon_values_mark_stale(self, monkeypatch):
        tab = _tab()
        tab._hwmon_fail_count = tab._STALE_THRESHOLD
        _drive(
            tab,
            monkeypatch,
            dual=_dual(),
            watts=50.0,
            hwmon=_hwmon_data(tctl=None, tccd={}, vcore=None),
        )
        assert tab._tctl_label.styleSheet() == tab._STALE_STYLE
        assert tab._vcore_label.styleSheet() == tab._STALE_STYLE

    def test_bars_update_without_topology(self, monkeypatch):
        tab = _tab()
        tab._topology = None
        _drive(tab, monkeypatch, dual=_dual(), watts=50.0)
        assert tab._per_core_bars

    def test_update_swallows_reader_oserror(self, monkeypatch):
        import corecycler.gui.monitor_tab as mt

        tab = _tab()

        def boom():
            raise OSError("sysfs gone")

        monkeypatch.setattr(mt, "read_core_frequencies_dual", boom)
        tab._update()
