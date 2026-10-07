"""Remaining display and lifecycle edges of the Memory, Monitor and CO tabs.

These are the branches a developer box never takes on its own: populated DIMM
tables, an SPD hwmon that answers, a missing thermal sensor, a topology-less
Curve Optimizer tab, and the memory stress lifecycle (driven with a stand-in
worker so no stress binary is ever launched).
"""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.gui import memory_tab as mem
from corecycler.gui import monitor_tab as mon
from corecycler.gui import smu_tab as st
from corecycler.monitor.memory import DIMMInfo
from tests._sensors import FakeSensors, a_tick


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _reader(available=True, **attrs):
    reader = MagicMock()
    reader.is_available.return_value = available
    for key, value in attrs.items():
        getattr(reader, key).return_value = value
    return reader


def _spd(available=True, temps=(45.0, 46.5)):
    reader = _reader(available, read_temperatures=list(temps))
    reader.spd_timings = None
    return reader


def _dimms():
    return [
        DIMMInfo(
            locator="DIMM 0",
            bank_locator="P0 CHANNEL A",
            size_gb=32,
            mem_type="DDR5",
            speed_mt=6000,
            configured_speed_mt=6000,
            manufacturer="Test",
            part_number="TEST-32G",
            serial_number="0001",
            rank=2,
            form_factor="DIMM",
            configured_voltage=1.35,
            data_width=64,
            total_width=72,
        ),
        DIMMInfo(
            locator="DIMM 1",
            size_gb=32,
            mem_type="DDR5",
            manufacturer="Test",
            part_number="TEST-32G",
        ),
    ]


class TestMemoryTabInventory:
    def _tab(self, monkeypatch, *, dimms=None, spd_available=False, pm_available=False):
        _qapp()
        spd = _spd(spd_available)
        monkeypatch.setattr(mem, "SPD5118Reader", lambda: spd)
        monkeypatch.setattr(mem, "read_dimm_info", lambda: dimms if dimms is not None else [])
        return mem.MemoryTab(pm_available)

    def test_a_populated_inventory_is_summarised(self, monkeypatch):
        tab = self._tab(monkeypatch, dimms=_dimms())
        summary = tab._summary_label.text()
        assert summary.startswith("2 DIMMs | 64 GB DDR5")
        assert "ECC" in summary
        assert "2R" in summary
        assert tab._dimm_table.rowCount() == 2
        assert tab._dimm_table.item(0, 0).text() == "DIMM 0 (P0 CHANNEL A)"
        assert tab._dimm_table.item(0, 1).text() == "32 GB"
        assert tab._dimm_table.item(1, 3).text() == "-"

    def test_an_empty_inventory_says_so(self, monkeypatch):
        tab = self._tab(monkeypatch)
        assert "No DIMM info available" in tab._summary_label.text()
        assert tab._dimm_table.rowCount() == 0

    def test_dimm_temperature_labels_are_created_and_refreshed(self, monkeypatch):
        tab = self._tab(monkeypatch, dimms=_dimms(), spd_available=True)
        assert tab._temp_group.isVisibleTo(tab)
        assert len(tab._temp_labels) == 2
        assert tab._temp_labels[0].text() == "DIMM 1: 45.0C"
        tab._spd_reader.read_temperatures.return_value = [51.0, 52.0]
        tab._update_temperatures()
        assert tab._temp_labels[1].text() == "DIMM 2: 52.0C"
        tab._load_dimm_info()
        assert len(tab._temp_labels) == 2
        assert tab._temp_labels[0].text() == "DIMM 1: 51.0C"

    def test_without_the_smu_driver_the_controller_rows_are_hidden(self, monkeypatch):
        tab = self._tab(monkeypatch)
        assert not tab._fclk_label.isVisibleTo(tab)
        assert tab._mc_missing_label.isVisibleTo(tab)

    def test_a_tab_without_the_smu_driver_ignores_a_tick_s_pm_table(self, monkeypatch):
        tab = self._tab(monkeypatch)
        before = tab._fclk_label.text()
        tab.on_tick(a_tick(pm=MagicMock(is_calibrated=True)))
        assert tab._fclk_label.text() == before

    def test_a_live_tick_refreshes_dimm_temperatures(self, monkeypatch):
        tab = self._tab(monkeypatch, dimms=_dimms(), spd_available=True)
        tab._spd_reader.read_temperatures.return_value = [60.0, 61.0]
        tab.on_tick(a_tick())
        assert tab._temp_labels[0].text() == "DIMM 1: 60.0C"


class TestMemoryTabLabels:
    def _tab(self, monkeypatch):
        _qapp()
        monkeypatch.setattr(mem, "SPD5118Reader", lambda: _spd(False))
        monkeypatch.setattr(mem, "read_dimm_info", list)
        return mem.MemoryTab(False)

    def test_an_unknown_clock_ratio_reads_as_blank(self, monkeypatch):
        tab = self._tab(monkeypatch)
        pm = MagicMock(fclk_mhz=0.0, uclk_mhz=0.0, mclk_mhz=0.0)
        tab._update_clock_labels(pm)
        assert tab._ratio_label.text() == "FCLK:UCLK --"

    def test_a_matched_clock_ratio_reads_as_one_to_one(self, monkeypatch):
        tab = self._tab(monkeypatch)
        pm = MagicMock(fclk_mhz=2000.0, uclk_mhz=2000.0, mclk_mhz=4000.0)
        tab._update_clock_labels(pm)
        assert tab._ratio_label.text() == "FCLK:UCLK 1:1"

    def test_present_voltages_are_shown(self, monkeypatch):
        tab = self._tab(monkeypatch)
        tab._update_voltage_labels(MagicMock(vdd_mem_v=1.35, vddq_v=1.30))
        assert tab._vdd_label.text() == "VDD: 1.350V"
        assert tab._vddq_label.text() == "VDDQ: 1.300V"

    def test_absent_voltages_read_as_blank(self, monkeypatch):
        tab = self._tab(monkeypatch)
        tab._update_voltage_labels(MagicMock(vdd_mem_v=0.0, vddq_v=0.0))
        assert tab._vdd_label.text() == "VDD: --"
        assert tab._vddq_label.text() == "VDDQ: --"


class TestMemoryStressLifecycle:
    @pytest.fixture
    def tab(self, monkeypatch):
        _qapp()
        monkeypatch.setattr(mem, "SPD5118Reader", lambda: _spd(False))
        monkeypatch.setattr(mem, "read_dimm_info", list)
        monkeypatch.setattr(mem.tools.shutil, "which", lambda name: "/usr/bin/" + name)
        widget = mem.MemoryTab(False)
        widget._stress_tool.clear()
        widget._stress_tool.addItem("stressapptest")
        return widget

    def test_starting_a_run_locks_the_controls_and_announces_it(self, tab, monkeypatch):
        worker = MagicMock()
        monkeypatch.setattr(mem, "_StressWorker", MagicMock(return_value=worker))
        started = []
        tab.memory_stress_started.connect(lambda: started.append(True))
        tab._run_memory_stress()
        assert worker.start.called
        assert started == [True]
        assert not tab._stress_btn.isEnabled()
        assert tab._stop_btn.isEnabled()
        assert not tab._stress_duration.isEnabled()
        assert "Running stressapptest" in tab._stress_status.text()

    def test_stopping_a_run_asks_the_worker_to_stop(self, tab):
        worker = MagicMock()
        worker.isRunning.return_value = True
        tab._stress_worker = worker
        tab._stop_memory_stress()
        assert worker.stop.called
        assert tab._stress_status.text() == "Stopping..."
        assert not tab._stop_btn.isEnabled()

    def test_stopping_without_a_run_is_a_noop(self, tab):
        tab._stop_memory_stress()
        assert not tab._stop_btn.isEnabled()

    def test_force_stop_escalates_to_terminate(self, tab):
        worker = MagicMock()
        worker.isRunning.return_value = True
        tab._stress_worker = worker
        tab.force_stop()
        assert worker.stop.called
        assert worker.wait.call_args.args == (3000,)
        assert worker.terminate.called

    def test_the_child_isolates_itself_and_dies_with_its_parent(self, tab, monkeypatch):
        import os
        import resource
        import signal
        import subprocess

        from corecycler.engine import execution

        libc = MagicMock()
        libc.prctl.return_value = 0
        monkeypatch.setattr(execution, "_libc", lambda: libc)
        proc = MagicMock()
        proc.communicate.return_value = ("ok", "")
        proc.returncode = 0
        popen = MagicMock(return_value=proc)
        monkeypatch.setattr(subprocess, "Popen", popen)
        worker = mem._StressWorker("stressapptest", 1)
        worker.run()

        preexec = popen.call_args.kwargs["preexec_fn"]
        sessions = []
        limits = []
        monkeypatch.setattr(os, "setsid", lambda: sessions.append(True))
        monkeypatch.setattr(resource, "setrlimit", lambda which, value: limits.append((which, value)))
        preexec()
        assert limits == [(resource.RLIMIT_RTPRIO, (0, 0))]
        assert sessions == [True]
        assert libc.prctl.call_args.args == (1, signal.SIGKILL)

    def test_force_stop_leaves_a_finished_worker_alone(self, tab):
        worker = MagicMock()
        worker.isRunning.return_value = False
        tab._stress_worker = worker
        tab.force_stop()
        assert not worker.terminate.called


def _bar(**over):
    from corecycler.gui.monitor_tab import CoreFreqBar

    bar = CoreFreqBar(0, "C0", max_freq=5000)
    bar.resize(600, 20)
    for key, value in over.items():
        setattr(bar, key, value)
    return bar


class TestCoreFreqBarPaint:
    def test_a_low_clock_paints_without_error(self):
        _qapp()
        bar = _bar(_freq=1000.0, _usage_pct=2.0, _temp=0.0)
        bar.grab()

    def test_a_heavy_stretch_and_hot_core_paint(self):
        _qapp()
        bar = _bar(_freq=4800.0, _usage_pct=90.0, _stretch_pct=6.0, _temp=90.0)
        bar.grab()

    def test_a_clean_stretch_and_cool_core_paint(self):
        _qapp()
        bar = _bar(_freq=3000.0, _usage_pct=60.0, _stretch_pct=0.4, _temp=45.0)
        bar.grab()


class TestMonitorTabFallbacks:
    def _tab(self, monkeypatch, *, hwmon_available=True, max_freq=5000.0, topology=None):
        _qapp()
        monkeypatch.setattr(mon, "read_max_frequency", lambda: max_freq)
        sensors = FakeSensors() if hwmon_available else FakeSensors(tctl=None, tccd=(), vcore=None)
        return mon.MonitorTab(sensors, True, topology=topology)

    def test_a_missing_thermal_sensor_reads_as_unavailable(self, monkeypatch):
        tab = self._tab(monkeypatch, hwmon_available=False)
        assert tab._tctl_label.text() == "Tctl: N/A"

    def test_an_unknown_max_boost_falls_back_to_a_ceiling(self, monkeypatch):
        tab = self._tab(monkeypatch, max_freq=None)
        assert tab._max_core_freq == 6000

    def test_without_topology_the_first_tick_makes_a_bar_per_live_cpu(self, monkeypatch):
        tab = self._tab(monkeypatch)
        assert tab._per_core_bars == {}
        tab.on_tick(a_tick(clocks={0: 4000.0, 1: 3800.0}))
        assert sorted(tab._per_core_bars) == [0, 1]

    def test_an_unknown_power_limit_reads_as_unavailable(self, monkeypatch):
        tab = self._tab(monkeypatch)
        tab._show_power_limits(
            MagicMock(
                ppt_value_w=0.0,
                ppt_limit_w=0.0,
                tdc_value_a=0.0,
                tdc_limit_a=0.0,
                edc_value_a=0.0,
                edc_limit_a=0.0,
            )
        )
        assert tab._ppt_label.text() == "PPT: N/A"
        assert tab._edc_label.text() == "EDC: N/A"


class TestSmuTabWithoutTopology:
    def test_the_table_stays_empty(self, monkeypatch):
        _qapp()
        monkeypatch.setattr(st, "QMessageBox", MagicMock())
        tab = st.SMUTab(topology=None)
        tab._populate_table()
        assert tab._table.rowCount() == 0

    def test_the_row_map_is_empty(self, monkeypatch):
        _qapp()
        monkeypatch.setattr(st, "QMessageBox", MagicMock())
        tab = st.SMUTab(topology=None)
        assert tab._core_row_map() == {}
