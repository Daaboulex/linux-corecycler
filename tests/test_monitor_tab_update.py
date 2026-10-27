from __future__ import annotations

import sys as _sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.monitor.tick import Tick
from corecycler.smu.commands import CPUGeneration, get_commands
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

    def test_the_v_cache_tag_goes_on_the_ccd_its_cores_sit_on_physically(self):
        _qapp()
        from corecycler.gui.monitor_tab import MonitorTab

        topo = CPUTopology(model_name="Test", family=25, model=0x21, physical_cores=2, ccds=1, is_x3d=True)
        for cid in range(2):
            topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,), has_vcache=True)
        driver = SimpleNamespace(commands=get_commands(CPUGeneration.ZEN3_VERMEER), core_map={0: (1, 0), 1: (1, 1)})
        sensors = FakeSensors(tccd=((1, 59.0),))
        tab = MonitorTab(sensors, True, topology=topo, smu=driver)
        tab.on_tick(_tick(sensors=sensors))
        assert tab._ccd_temp_labels[1].text() == "CCD1 VC: 59.0°C"

    def test_a_ccd_whose_read_failed_gets_no_label(self):
        sensors = FakeSensors(tccd=((0, None), (1, 59.0)))
        tab = _tab(sensors)
        tab.on_tick(_tick(watts=95.0, sensors=sensors))
        assert sorted(tab._ccd_temp_labels) == [1]
        assert "59.0" in tab._ccd_temp_labels[1].text()

    @pytest.mark.req("T22.monitor.stale")
    def test_a_missed_package_read_marks_the_last_one_stale_at_once(self):
        tab = _tab()
        tab.on_tick(_tick(watts=95.0))
        tab.on_tick(_tick(watts=None))
        assert tab._power_label.styleSheet() == tab._stale_style()
        assert tab._power_label.text() == "Package: 95.0W (stale)"

    @pytest.mark.req("T22.monitor.charts")
    def test_a_clock_above_the_read_maximum_widens_the_chart_and_leaves_max_boost_as_read(self):
        tab = _tab()
        tab._max_core_freq = 5000
        before = tab._max_freq_label.text()
        tab.on_tick(_tick(actual=6200.0, ceiling=6300.0, watts=50.0))
        assert tab._freq_chart.max_val == pytest.approx(6200.0 * 1.1)
        assert tab._max_freq_label.text() == before

    @pytest.mark.req("T22.monitor.stale")
    def test_missed_sensor_reads_mark_each_last_reading_stale_on_its_own(self):
        sensors = FakeSensors(tctl=60.0, tccd=((0, 61.0), (1, 59.0)), vcore=1.2)
        tab = _tab(sensors)
        tab.on_tick(_tick(watts=50.0, sensors=sensors))
        sensors.give(tctl=62.0, tccd=((0, None), (1, 58.0)), vcore=None)
        tab.on_tick(_tick(watts=50.0, sensors=sensors))
        assert tab._tctl_label.text() == "Tctl: 62.0\u00b0C"
        assert tab._vcore_label.text() == "Vcore: 1.2000V (stale)"
        assert tab._ccd_temp_labels[0].text().endswith("61.0\u00b0C (stale)")
        assert tab._ccd_temp_labels[1].text().endswith("58.0\u00b0C")

    def test_the_bars_show_the_tick_s_stretch_and_core_power(self):
        tab = _tab()
        bar = tab._per_core_bars[0]
        seen = []
        bar.update_data = lambda *args, **kwargs: seen.append(kwargs)
        tab.on_tick(_tick(watts=50.0))
        assert (seen[0]["stretch_pct"], seen[0]["core_watts"]) == (2.5, 12.0)

    @pytest.mark.req("O19.stretch")
    def test_a_bar_takes_its_core_s_stretch_not_its_first_cpu_s(self):
        tab = _tab()
        core = tab._topology.cores[1]
        tab._topology.cores[1] = type(core)(core_id=1, ccd=core.ccd, ccx=None, logical_cpus=(5,))
        seen = []
        tab._per_core_bars[1].update_data = lambda *args, **kwargs: seen.append(kwargs)
        tab.on_tick(replace(_tick(watts=50.0), stretch={1: 3.0, 5: 9.0}))
        assert seen[0]["stretch_pct"] == 3.0

    def test_bars_update_without_topology(self):
        tab = _tab()
        tab._topology = None
        tab.on_tick(_tick(watts=50.0))
        assert tab._per_core_bars


@pytest.mark.req("T22.monitor.bars")
def test_a_bar_shows_its_own_ccd_temperature_or_none_never_tctl_in_its_place():
    sensors = FakeSensors(tctl=65.0, tccd=((0, None), (1, 59.0)))
    tab = _tab(sensors)
    tab.on_tick(_tick(sensors=sensors))
    assert (tab._per_core_bars[0]._temp, tab._per_core_bars[2]._temp) == (None, 59.0)


@pytest.mark.req("T22.monitor.bars")
def test_a_bar_with_no_usage_or_clock_read_shows_none_not_zero_or_its_old_values():
    from dataclasses import replace

    tab = _tab()
    tab.on_tick(_tick())
    assert tab._per_core_bars[0]._freq == 5000.0
    tab.on_tick(replace(_tick(), clocks={}, usage={}))
    bar = tab._per_core_bars[0]
    assert (bar._freq, bar._usage_pct) == (None, None)
    assert bar._freq_text(bar._freq, bar._boost_ceiling_mhz) == "  -- MHz"
