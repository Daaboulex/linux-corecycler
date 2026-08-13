"""MonitorTab + CoreFreqBar: readout formatting, view toggle, power limits, paint."""

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


def _topo(ccds=2):
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=8, ccds=ccds)
    per = 8 // ccds
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=cid // per, ccx=None, logical_cpus=(cid, cid + 8))
    return topo


def _bar(core_id=0):
    _qapp()
    from corecycler.gui.monitor_tab import CoreFreqBar

    return CoreFreqBar(core_id, f"C{core_id}", max_freq=6000)


def _tab(topo=None):
    _qapp()
    from corecycler.gui.monitor_tab import MonitorTab

    return MonitorTab(topology=topo or _topo())


class TestCoreFreqBar:
    def test_idle_readout_has_no_false_ceiling(self):
        from corecycler.gui.monitor_tab import CoreFreqBar

        assert CoreFreqBar._freq_text(0, 5000) == "  -- MHz"

    def test_live_readout_with_and_without_ceiling(self):
        from corecycler.gui.monitor_tab import CoreFreqBar

        assert CoreFreqBar._freq_text(5200, 5500) == "5200/5500MHz"
        assert CoreFreqBar._freq_text(5200, 0) == "5200MHz"

    def test_update_and_paint(self):
        bar = _bar()
        bar.resize(400, 24)
        bar.update_data(5100, temp=72, usage_pct=95, stretch_pct=1.2, core_watts=18.5, eff_max_mhz=5300)
        bar.set_active(True)
        bar.grab()

    def test_history_bounded_and_paints(self):
        from corecycler.gui.monitor_tab import MAX_FREQ_HISTORY

        bar = _bar()
        bar.resize(400, 24)
        for i in range(MAX_FREQ_HISTORY + 20):
            bar.update_data(4000 + i)
        assert len(bar._history) == MAX_FREQ_HISTORY
        bar.grab()


class TestMonitorTab:
    def test_view_toggle_switches_panels(self):
        tab = _tab()
        tab._toggle_view(True)
        assert tab._per_core_scroll.isVisible() or not tab._per_core_scroll.isHidden()
        assert tab._charts_widget.isHidden()
        assert tab._toggle_btn.text() == "Package View"
        tab._toggle_view(False)
        assert tab._charts_widget.isVisible() or not tab._charts_widget.isHidden()

    def test_set_active_core_highlights_only_that_cell(self):
        tab = _tab()
        tab.set_active_core(3)
        assert tab._per_core_bars[3]._is_active is True
        assert tab._per_core_bars[0]._is_active is False
        tab.set_active_core(None)
        assert tab._per_core_bars[3]._is_active is False

    def test_set_topology_rebuilds_bars(self):
        tab = _tab(_topo(ccds=2))
        tab.set_topology(_topo(ccds=1))
        assert sorted(tab._per_core_bars) == list(range(8))

    def test_power_limits_na_when_pmtable_absent(self):
        tab = _tab()
        tab._pmtable = MagicMock()
        tab._pmtable.is_available.return_value = False
        tab._update_power_limits()
        assert "N/A" in tab._ppt_label.text()

    def test_power_limits_formatted_when_present(self):
        tab = _tab()
        tab._pmtable = MagicMock()
        tab._pmtable.is_available.return_value = True
        tab._pmtable.read.return_value = SimpleNamespace(
            ppt_value_w=120.0,
            ppt_limit_w=200.0,
            tdc_value_a=90.0,
            tdc_limit_a=180.0,
            edc_value_a=110.0,
            edc_limit_a=230.0,
        )
        tab._update_power_limits()
        assert "120" in tab._ppt_label.text() and "200" in tab._ppt_label.text()

    def test_stop_monitoring_closes_msr(self):
        tab = _tab()
        tab._msr = MagicMock()
        tab.stop_monitoring()
        tab._msr.close.assert_called_once()
