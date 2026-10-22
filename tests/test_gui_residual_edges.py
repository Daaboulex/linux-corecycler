"""Remaining display and lifecycle edges of the Memory, Monitor and Curve-Tuner tabs.

These are the branches a developer box never takes on its own: populated DIMM
tables, an SPD hwmon that answers, a missing thermal sensor, a topology-less
Curve-Tuner tab, and the memory stress lifecycle (driven with a stand-in
worker so no stress binary is ever launched).
"""

from __future__ import annotations

import io
import os
import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.gui import memory_tab as mem
from corecycler.gui import monitor_tab as mon
from corecycler.gui import smu_tab as st
from corecycler.gui.style import theme
from corecycler.monitor.memory import DIMMInfo
from tests._heavy_lock import GPU_LINE, held_by_another_tool, holder_seen_by_another_tool, said_by
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
    reader.addresses = [f"0x{0x50 + n:02x}" for n in range(len(temps))]
    reader.eeprom_count = 0
    return reader


DENIED = "dmidecode exited 1: /sys/firmware/dmi/tables/smbios_entry_point: Permission denied"


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
        monkeypatch.setattr(mem, "read_dimm_info", lambda: (dimms, "") if dimms else ([], DENIED))
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
        assert tab._summary_label.text() == f"No DIMM info: {DENIED}"
        assert tab._dimm_table.rowCount() == 0

    def test_dimm_temperature_labels_are_created_and_refreshed(self, monkeypatch):
        tab = self._tab(monkeypatch, dimms=_dimms(), spd_available=True)
        assert tab._temp_group.isVisibleTo(tab)
        assert len(tab._temp_labels) == 2
        assert tab._temp_labels[0].text() == "DIMM at 0x50: 45.0C"
        tab._spd_reader.read_temperatures.return_value = [51.0, 52.0]
        tab._update_temperatures()
        assert tab._temp_labels[1].text() == "DIMM at 0x51: 52.0C"
        tab._load_dimm_info()
        assert len(tab._temp_labels) == 2
        assert tab._temp_labels[0].text() == "DIMM at 0x50: 51.0C"

    def test_without_the_smu_driver_the_controller_rows_are_hidden(self, monkeypatch):
        tab = self._tab(monkeypatch)
        assert not tab._fclk_label.isVisibleTo(tab)
        assert tab._mc_missing_label.isVisibleTo(tab)

    def test_a_tab_without_the_smu_driver_ignores_a_tick_s_pm_table(self, monkeypatch):
        tab = self._tab(monkeypatch)
        before = tab._fclk_label.text()
        tab.on_tick(a_tick(pm=MagicMock(is_calibrated=True)))
        assert tab._fclk_label.text() == before

    @pytest.mark.req("T22.memory.dimms")
    def test_an_unread_dimm_says_unread_and_the_next_keeps_its_own_label(self, monkeypatch):
        tab = self._tab(monkeypatch, dimms=_dimms(), spd_available=True)
        tab._spd_reader.read_temperatures.return_value = [None, 39.0]
        tab._update_temperatures()
        assert [label.text() for label in tab._temp_labels] == ["DIMM at 0x50: unread", "DIMM at 0x51: 39.0C"]

    def test_a_live_tick_refreshes_dimm_temperatures(self, monkeypatch):
        tab = self._tab(monkeypatch, dimms=_dimms(), spd_available=True)
        tab._spd_reader.read_temperatures.return_value = [60.0, 61.0]
        tab.on_tick(a_tick())
        assert tab._temp_labels[0].text() == "DIMM at 0x50: 60.0C"

    @pytest.mark.req("T23.memory.facts")
    def test_the_summary_and_table_show_only_what_dmidecode_read(self, monkeypatch):
        dimms = [
            DIMMInfo(locator="A1", size_gb=32, mem_type="DDR5", rank=1),
            DIMMInfo(locator="B1", size_gb=32, rank=2),
        ]
        tab = self._tab(monkeypatch, dimms=dimms)
        assert tab._summary_label.text() == "2 DIMMs | 64 GB DDR5 ECC unread 1R/2R"
        assert [tab._dimm_table.item(1, column).text() for column in (2, 5, 6)] == ["-", "-", "-"]
        tab = self._tab(monkeypatch, dimms=[DIMMInfo(locator="A1", size_gb=32, data_width=64, total_width=64)])
        assert tab._summary_label.text() == "1 DIMMs | 32 GB type unread Non-ECC"
        tab = self._tab(monkeypatch, dimms=[DIMMInfo(locator="A1", size_gb=32, data_width=64)])
        assert tab._summary_label.text() == "1 DIMMs | 32 GB type unread ECC unread"

    @pytest.mark.req("T23.memory.facts")
    def test_a_long_reason_wraps_instead_of_widening_the_tab(self, monkeypatch):
        _qapp()
        tabs = []
        for why in (DENIED, "dmidecode could not run: " + "no such device " * 20):
            monkeypatch.setattr(mem, "read_dimm_info", lambda why=why: ([], why))
            monkeypatch.setattr(mem, "SPD5118Reader", lambda: _spd(False))
            tabs.append(mem.MemoryTab(False))
        assert tabs[1].minimumSizeHint().width() == tabs[0].minimumSizeHint().width()

    @pytest.mark.req("T23.memory.facts")
    def test_readings_that_do_not_match_the_sensors_are_refused(self, monkeypatch):
        tab = self._tab(monkeypatch, dimms=_dimms(), spd_available=True)
        tab._spd_reader.read_temperatures.return_value = [40.0]
        with pytest.raises(ValueError):
            tab._update_temperatures()
        with pytest.raises(ValueError):
            tab._load_dimm_info()

    @pytest.mark.req("T23.memory.facts")
    def test_the_spd_timings_say_which_part_could_not_be_read(self, monkeypatch):
        tab = self._tab(monkeypatch)
        assert tab._spd_unavailable_label.text().endswith("no spd5118 sensor is loaded")
        tab = self._tab(monkeypatch, spd_available=True)
        assert tab._spd_unavailable_label.text().endswith("the spd5118 sensors expose no EEPROM")
        tab._spd_reader.eeprom_count = 1
        tab._update_spd_labels()
        assert tab._spd_unavailable_label.text().endswith("no SPD EEPROM could be read as DDR5")

    @pytest.mark.req("T23.memory.facts")
    def test_the_smu_line_says_the_pm_table_was_read_at_start(self, monkeypatch):
        tab = self._tab(monkeypatch)
        assert "ryzen_smu: no PM table when the app started" in tab._deps_label.text()


class TestMemoryTabLabels:
    def _tab(self, monkeypatch):
        _qapp()
        monkeypatch.setattr(mem, "SPD5118Reader", lambda: _spd(False))
        monkeypatch.setattr(mem, "read_dimm_info", lambda: ([], DENIED))
        return mem.MemoryTab(False)

    @pytest.mark.req("T23.memory.facts")
    def test_an_unread_clock_reads_as_blank_not_zero(self, monkeypatch):
        tab = self._tab(monkeypatch)
        pm = MagicMock(fclk_mhz=0.0, uclk_mhz=2000.0, mclk_mhz=0.0)
        tab._update_clock_labels(pm)
        labels = (tab._fclk_label, tab._uclk_label, tab._mclk_label, tab._ratio_label)
        assert [label.text() for label in labels] == ["FCLK: --", "UCLK: 2000 MHz", "MCLK: --", "FCLK:UCLK --"]
        assert theme.COLOR_MUTED in tab._fclk_label.styleSheet()
        assert tab._uclk_label.styleSheet() == ""
        tab._update_clock_labels(MagicMock(fclk_mhz=1.0, uclk_mhz=2000.0, mclk_mhz=4000.0))
        assert tab._fclk_label.text() == "FCLK: 1 MHz"

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
        monkeypatch.setattr(mem, "read_dimm_info", lambda: ([], DENIED))
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

    @pytest.mark.req("HEAVY.gui")
    def test_a_run_refuses_while_another_tool_holds_the_shared_lock_naming_it(self, tab, monkeypatch, heavy_lock):
        shown = MagicMock()
        monkeypatch.setattr(mem, "QMessageBox", shown)
        monkeypatch.setattr(mem._StressWorker, "start", lambda self: None)
        started = []
        tab.memory_stress_started.connect(lambda: started.append(True))
        with held_by_another_tool(heavy_lock, f"{GPU_LINE}\n".encode()):
            tab._run_memory_stress()
        assert (tab._stress_worker, started) == (None, [])
        assert shown.warning.call_args.args[1:] == (
            "Cannot Start",
            f"Cannot start: another heavy test holds the shared lock: {GPU_LINE}",
        )
        assert tab._stress_btn.isEnabled() and not tab._stop_btn.isEnabled()

    @pytest.mark.req("HEAVY.gui")
    @pytest.mark.parametrize("ends", ["finishes", "is stopped", "crashes"])
    def test_a_run_holds_the_shared_lock_and_its_worker_lets_go_however_the_run_ends(
        self, tab, monkeypatch, heavy_lock, ends
    ):
        seen = []

        def command_name(key):
            seen.append(holder_seen_by_another_tool(heavy_lock))
            if ends == "crashes":
                raise RuntimeError("boom")
            if ends == "is stopped":
                tab._stress_worker.stop()
            return _sys.executable

        monkeypatch.setattr(mem, "QMessageBox", MagicMock())
        monkeypatch.setattr(mem.tools, "command_name", command_name)
        monkeypatch.setattr(mem._StressWorker, "start", lambda self: None)
        tab._stress_tool.clear()
        tab._stress_tool.addItem("stress-ng --vm")
        tab._run_memory_stress()
        tab._stress_worker.run()
        [line] = seen
        assert line is not None and said_by(line)[:2] == ("corecycler-gui", os.getpid())
        assert holder_seen_by_another_tool(heavy_lock) is None

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
        worker = mem._StressWorker("stressapptest", 1, io.BytesIO())
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

    @pytest.mark.req("T23.memory.stop")
    def test_a_stop_before_the_tool_starts_ends_the_run_stopped_without_starting_it(self, tab, monkeypatch):
        import subprocess

        popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", popen)
        worker = mem._StressWorker("stress-ng --vm", 1, io.BytesIO())
        seen = []
        worker.done.connect(lambda verdict, output: seen.append(verdict))
        worker.stop()
        worker.run()
        assert (seen, popen.called) == ([mem.STOPPED], False)

    @pytest.mark.req("T23.memory.stop")
    def test_a_stop_while_the_tool_starts_kills_it_and_ends_the_run_stopped(self, tab, monkeypatch):
        import os
        import signal
        import subprocess

        from corecycler.engine import execution

        monkeypatch.setattr(execution, "make_preexec", lambda: None)
        worker = mem._StressWorker("stress-ng --vm", 1, io.BytesIO())
        proc = MagicMock(pid=4242, returncode=-9)
        proc.communicate.return_value = ("", "")

        def started(*_args, **_kwargs):
            worker.stop()
            return proc

        monkeypatch.setattr(subprocess, "Popen", started)
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
        killed = []
        monkeypatch.setattr(os, "killpg", lambda group, how: killed.append((group, how)))
        seen = []
        worker.done.connect(lambda verdict, output: seen.append(verdict))
        worker.run()
        assert (killed, seen) == ([(4242, signal.SIGKILL)], [mem.STOPPED])

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


MEMORY_CONTROLS = {
    "Refresh": "reads the DIMMs, their temperatures and the tools again",
    "Run": "starts the chosen tool for the chosen minutes and locks Run, Duration and Tool",
    "Stop": "asks the running tool to stop and says Stopping...",
    "QTableCornerButton": "selects every cell of the DIMM table",
}
MEMORY_INPUTS = {"_stress_duration": "the minutes Run gives the tool", "_stress_tool": "the tool Run starts"}


class TestMemoryContract:
    @pytest.fixture
    def tab(self, monkeypatch):
        _qapp()
        monkeypatch.setattr(mem, "SPD5118Reader", lambda: _spd(False))
        monkeypatch.setattr(mem, "read_dimm_info", lambda: ([], DENIED))
        monkeypatch.setattr(mem.tools.shutil, "which", lambda name: "/usr/bin/" + name)
        return mem.MemoryTab(False)

    @pytest.mark.req("T23.contract.memory")
    def test_every_control_of_the_memory_tab_is_listed_with_its_reaction(self, tab):
        from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox

        assert {b.text() or b.metaObject().className() for b in tab.findChildren(QAbstractButton)} == set(
            MEMORY_CONTROLS
        )
        inputs = {*tab.findChildren(QAbstractSpinBox), *tab.findChildren(QComboBox)}
        assert inputs == {getattr(tab, name) for name in MEMORY_INPUTS}

    @pytest.mark.req("T23.contract.memory")
    @pytest.mark.parametrize("connected", [True, False])
    def test_refresh_reads_the_dimms_only_through_its_connection(self, tab, monkeypatch, connected):
        from PySide6.QtWidgets import QPushButton

        refresh = next(b for b in tab.findChildren(QPushButton) if b.text() == "Refresh")
        if not connected:
            refresh.clicked.disconnect()
        monkeypatch.setattr(mem, "read_dimm_info", lambda: (_dimms(), ""))
        refresh.click()
        assert (tab._dimm_table.rowCount() == 2) is connected

    @pytest.mark.req("T23.contract.memory")
    @pytest.mark.parametrize("connected", [True, False])
    def test_run_starts_the_chosen_tool_for_the_chosen_minutes_only_through_its_connection(
        self, tab, monkeypatch, connected
    ):
        made = MagicMock()
        monkeypatch.setattr(mem, "_StressWorker", made)
        tab._stress_tool.setCurrentText("stress-ng --vm")
        tab._stress_duration.setValue(7)
        if not connected:
            tab._stress_btn.clicked.disconnect()
        tab._stress_btn.click()
        assert made.called is connected
        if connected:
            assert made.call_args.args[:2] == ("stress-ng --vm", 7)

    @pytest.mark.req("T23.contract.memory")
    @pytest.mark.parametrize("connected", [True, False])
    def test_stop_asks_the_tool_to_stop_only_through_its_connection(self, tab, connected):
        worker = MagicMock()
        worker.isRunning.return_value = True
        tab._stress_worker = worker
        tab._stop_btn.setEnabled(True)
        if not connected:
            tab._stop_btn.clicked.disconnect()
        tab._stop_btn.click()
        assert worker.stop.called is connected
