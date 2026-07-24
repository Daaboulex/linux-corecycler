"""Error and fallback path coverage for the monitor readers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.monitor import frequency as freqmod
from corecycler.monitor.cpu_usage import CPUUsageReader
from corecycler.monitor.hwmon import HWMonReader
from corecycler.monitor.power import PowerMonitor


class TestCPUUsageReaderEdges:
    def test_proc_stat_unreadable_returns_empty(self):
        reader = CPUUsageReader()
        with patch("pathlib.Path.read_text", side_effect=OSError):
            assert reader.read() == {}


class TestPowerMonitorEdges:
    def _powercap(self, tmp_path):
        base = tmp_path / "powercap" / "intel-rapl"
        base.mkdir(parents=True)
        return base

    def test_scanned_package_domain_is_used(self, tmp_path):
        base = self._powercap(tmp_path)
        dom = base.parent / "intel-rapl:1"
        dom.mkdir()
        (dom / "name").write_text("package-1\n")
        (dom / "energy_uj").write_text("1000\n")
        with patch("corecycler.monitor.power.RAPL_BASE", base):
            mon = PowerMonitor()
        assert mon._package_path == dom / "energy_uj"
        assert mon.is_available()

    def test_rapl_name_unreadable_is_skipped(self, tmp_path):
        base = self._powercap(tmp_path)
        dom = base.parent / "intel-rapl:1"
        dom.mkdir()
        (dom / "name").mkdir()  # a directory: exists() True, read_text() raises OSError
        with (
            patch("corecycler.monitor.power.RAPL_BASE", base),
            patch("corecycler.monitor.power.HWMON_BASE", tmp_path / "nohwmon"),
        ):
            mon = PowerMonitor()
        assert mon._package_path is None

    def test_read_rapl_unreadable_returns_none(self, tmp_path):
        mon = PowerMonitor.__new__(PowerMonitor)
        mon._package_path = tmp_path / "gone" / "energy_uj"
        mon._last_energy_uj = None
        mon._last_time = None
        assert mon._read_rapl() is None

    def test_read_hwmon_power_unreadable_returns_none(self, tmp_path):
        mon = PowerMonitor.__new__(PowerMonitor)
        mon._hwmon_power_path = tmp_path / "gone" / "power1_input"
        assert mon._read_hwmon_power() is None


class TestHWMonReaderEdges:
    def test_name_unreadable_is_skipped(self, tmp_path):
        base = tmp_path / "hwmon"
        d = base / "hwmon0"
        d.mkdir(parents=True)
        (d / "name").mkdir()  # directory: exists() True, read_text() raises OSError
        with patch("corecycler.monitor.hwmon.HWMON_BASE", base):
            reader = HWMonReader()
        assert reader.is_available() is False

    def test_voltage_input_unreadable_is_skipped(self, tmp_path):
        base = tmp_path / "hwmon"
        d = base / "hwmon0"
        d.mkdir(parents=True)
        (d / "name").write_text("k10temp\n")
        (d / "in1_input").write_text("not-an-int\n")
        with patch("corecycler.monitor.hwmon.HWMON_BASE", base):
            reader = HWMonReader()
            data = reader.read()
        assert data.vcore_v is None


class TestFrequencyEdges:
    def test_read_falls_back_to_proc_when_cpufreq_absent(self, tmp_path):
        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", tmp_path / "absent"):
            out = freqmod.read_core_frequencies()
        assert isinstance(out, dict)

    def test_cpu_dir_without_freq_files_is_skipped(self, tmp_path):
        base = tmp_path / "cpu"
        (base / "cpu0").mkdir(parents=True)
        with (
            patch("corecycler.monitor.frequency.CPUFREQ_BASE", base),
            patch("corecycler.monitor.frequency._read_from_proc", return_value={7: 1.0}),
        ):
            out = freqmod.read_core_frequencies()
        assert out == {7: 1.0}

    def test_read_from_proc_malformed_processor_line(self):
        fake = "processor\t: notanum\ncpu MHz\t: 3000.0\n"
        with patch("pathlib.Path.exists", return_value=True), patch("pathlib.Path.read_text", return_value=fake):
            assert freqmod._read_from_proc() == {}

    def test_dual_returns_empty_when_cpufreq_absent(self, tmp_path):
        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", tmp_path / "absent"):
            assert freqmod.read_core_frequencies_dual() == {}

    def test_dual_actual_unreadable_is_skipped(self, tmp_path):
        base = tmp_path / "cpu"
        cf = base / "cpu0" / "cpufreq"
        cf.mkdir(parents=True)
        (cf / "cpuinfo_cur_freq").write_text("garbage\n")
        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", base):
            assert freqmod.read_core_frequencies_dual() == {}

    def test_read_max_frequency_unreadable_returns_none(self, tmp_path):
        base = tmp_path / "cpu"
        cf = base / "cpu0" / "cpufreq"
        cf.mkdir(parents=True)
        (cf / "cpuinfo_max_freq").write_text("garbage\n")
        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", base):
            assert freqmod.read_max_frequency(0) is None

    def test_read_min_frequency_unreadable_returns_none(self, tmp_path):
        base = tmp_path / "cpu"
        cf = base / "cpu0" / "cpufreq"
        cf.mkdir(parents=True)
        (cf / "cpuinfo_min_freq").write_text("garbage\n")
        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", base):
            assert freqmod.read_min_frequency(0) is None
