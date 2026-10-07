from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.monitor.tick import Tick
from tests._sensors import FakeSensors


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


def _tab(sensors=None):
    _qapp()
    from corecycler.gui.monitor_tab import MonitorTab

    return MonitorTab(sensors or FakeSensors(), True, topology=_topo())


def _tick(*, actual=5000.0, ceiling=5200.0, watts=None, sensors=None, pm=None) -> Tick:
    return Tick(
        clocks=dict.fromkeys(range(4), actual),
        ceilings=dict.fromkeys(range(4), ceiling),
        sensed=(sensors or FakeSensors()).read(),
        package_watts=watts,
        usage={0: 50.0, 1: 40.0, 2: 30.0, 3: 20.0},
        stretch={0: 2.5},
        core_watts={0: 12.0},
        pm=pm,
    )


class TestMonitorUpdateMatrix:
    def test_package_power_and_per_ccd_labels(self):
        tab = _tab()
        tab.on_tick(_tick(watts=95.0))
        assert "95.0W" in tab._power_label.text()
        assert 0 in tab._ccd_temp_labels
        assert "VC" in tab._ccd_temp_labels[0].text()
        assert "VC" not in tab._ccd_temp_labels[1].text()

    def test_a_ccd_whose_read_failed_gets_no_label(self):
        sensors = FakeSensors(tccd=((0, None), (1, 59.0)))
        tab = _tab(sensors)
        tab.on_tick(_tick(watts=95.0, sensors=sensors))
        assert sorted(tab._ccd_temp_labels) == [1]
        assert "59.0" in tab._ccd_temp_labels[1].text()

    def test_no_package_power_marks_power_stale(self):
        tab = _tab()
        tab._power_fail_count = tab._STALE_THRESHOLD
        tab.on_tick(_tick(watts=None))
        assert tab._power_label.styleSheet() == tab._stale_style()

    def test_boost_above_ceiling_raises_max(self):
        tab = _tab()
        tab._max_core_freq = 5000
        tab.on_tick(_tick(actual=6200.0, ceiling=6300.0, watts=50.0))
        assert tab._max_core_freq == 6200.0
        assert "6200" in tab._max_freq_label.text()

    def test_missing_hwmon_values_mark_stale(self):
        sensors = FakeSensors(tctl=None, tccd=(), vcore=None)
        tab = _tab(sensors)
        tab._hwmon_fail_count = tab._STALE_THRESHOLD
        tab.on_tick(_tick(watts=50.0, sensors=sensors))
        assert tab._tctl_label.styleSheet() == tab._stale_style()
        assert tab._vcore_label.styleSheet() == tab._stale_style()

    def test_the_bars_show_the_tick_s_stretch_and_core_power(self):
        tab = _tab()
        bar = tab._per_core_bars[0]
        seen = []
        bar.update_data = lambda *args, **kwargs: seen.append(kwargs)
        tab.on_tick(_tick(watts=50.0))
        assert (seen[0]["stretch_pct"], seen[0]["core_watts"]) == (2.5, 12.0)

    def test_bars_update_without_topology(self):
        tab = _tab()
        tab._topology = None
        tab.on_tick(_tick(watts=50.0))
        assert tab._per_core_bars
