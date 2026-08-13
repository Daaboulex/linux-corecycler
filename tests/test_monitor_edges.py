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


class TestCpufreqSysfsReaders:
    """The cpufreq readers must be exercised against a tree we build, not the
    build host's: a machine without /sys/devices/system/cpu (the nix sandbox)
    otherwise leaves every hardware-present branch untested."""

    def _tree(self, tmp_path, monkeypatch, cpus):
        from corecycler.monitor import frequency as freq

        base = tmp_path / "cpu"
        for name, files in cpus.items():
            node = base / name / "cpufreq"
            node.mkdir(parents=True)
            for fname, text in files.items():
                (node / fname).write_text(text)
        monkeypatch.setattr(freq, "CPUFREQ_BASE", base)
        return freq

    def test_the_hardware_frequency_is_preferred_over_the_governor_target(self, tmp_path, monkeypatch):
        freq = self._tree(
            tmp_path,
            monkeypatch,
            {
                "cpu0": {"cpuinfo_cur_freq": "4200000\n", "scaling_cur_freq": "3000000\n"},
            },
        )
        assert freq.read_core_frequencies() == {0: 4200.0}

    def test_the_governor_target_is_the_fallback(self, tmp_path, monkeypatch):
        freq = self._tree(tmp_path, monkeypatch, {"cpu1": {"scaling_cur_freq": "3600000\n"}})
        assert freq.read_core_frequencies() == {1: 3600.0}

    def test_unreadable_and_non_cpu_entries_are_skipped(self, tmp_path, monkeypatch):
        freq = self._tree(
            tmp_path,
            monkeypatch,
            {
                "cpu0": {"scaling_cur_freq": "not a number\n"},
                "cpu1": {},
                "cpufreq": {"scaling_cur_freq": "3600000\n"},
            },
        )
        monkeypatch.setattr(freq, "_read_from_proc", dict)
        assert freq.read_core_frequencies() == {}

    def test_the_dual_reader_pairs_actual_with_the_boost_ceiling(self, tmp_path, monkeypatch):
        freq = self._tree(
            tmp_path,
            monkeypatch,
            {
                "cpu0": {"cpuinfo_avg_freq": "4100000\n", "scaling_max_freq": "5700000\n"},
            },
        )
        reading = freq.read_core_frequencies_dual()[0]
        assert reading.actual_mhz == 4100.0
        assert reading.effective_max_mhz == 5700.0

    def test_the_dual_reader_needs_both_halves(self, tmp_path, monkeypatch):
        freq = self._tree(
            tmp_path,
            monkeypatch,
            {
                "cpu0": {"cpuinfo_cur_freq": "garbage\n"},
                "cpu1": {"scaling_max_freq": "5700000\n"},
                "cpuidle": {"scaling_cur_freq": "3600000\n"},
            },
        )
        assert freq.read_core_frequencies_dual() == {}

    def test_the_boost_and_floor_limits_are_read(self, tmp_path, monkeypatch):
        freq = self._tree(
            tmp_path,
            monkeypatch,
            {
                "cpu0": {"cpuinfo_max_freq": "5700000\n", "cpuinfo_min_freq": "550000\n"},
            },
        )
        assert freq.read_max_frequency(0) == 5700.0
        assert freq.read_min_frequency(0) == 550.0

    def test_absent_limits_read_as_unknown(self, tmp_path, monkeypatch):
        freq = self._tree(tmp_path, monkeypatch, {"cpu0": {}})
        assert freq.read_max_frequency(0) is None
        assert freq.read_min_frequency(0) is None


class TestRaplPowerReader:
    def _rapl(self, tmp_path, monkeypatch, domains, *, sibling=False):
        from corecycler.monitor import power as pw

        base = tmp_path / "powercap" / "intel-rapl"
        base.mkdir(parents=True)
        root = base.parent if sibling else base
        for name, files in domains.items():
            node = root / name
            node.mkdir(exist_ok=True, parents=True)
            for fname, text in files.items():
                (node / fname).write_text(text)
        monkeypatch.setattr(pw, "RAPL_BASE", base)
        monkeypatch.setattr(pw, "HWMON_BASE", tmp_path / "no-hwmon")
        return pw

    def _hwmon(self, tmp_path, monkeypatch, nodes):
        from corecycler.monitor import power as pw

        base = tmp_path / "hwmon"
        for name, files in nodes.items():
            node = base / name
            node.mkdir(parents=True)
            for fname, text in files.items():
                (node / fname).write_text(text)
        monkeypatch.setattr(pw, "RAPL_BASE", tmp_path / "no-rapl")
        monkeypatch.setattr(pw, "HWMON_BASE", base)
        return pw

    def test_the_primary_package_counter_is_used(self, tmp_path, monkeypatch):
        pw = self._rapl(tmp_path, monkeypatch, {"intel-rapl:0": {"energy_uj": "1000\n"}})
        reader = pw.PowerMonitor()
        assert reader.is_available()
        assert reader._package_path.name == "energy_uj"

    def test_a_named_package_domain_is_found_by_scan(self, tmp_path, monkeypatch):
        pw = self._rapl(
            tmp_path,
            monkeypatch,
            {
                "intel-rapl:1": {"energy_uj": "2000\n", "name": "dram\n"},
                "intel-rapl:2": {"energy_uj": "3000\n", "name": "package-0\n"},
            },
            sibling=True,
        )
        reader = pw.PowerMonitor()
        assert reader.is_available()
        assert reader._package_path.parent.name == "intel-rapl:2"

    def test_power_is_the_energy_delta_over_time(self, tmp_path, monkeypatch):
        pw = self._rapl(tmp_path, monkeypatch, {"intel-rapl:0": {"energy_uj": "0\n"}})
        reader = pw.PowerMonitor()
        assert reader._read_rapl() is None
        reader._package_path.write_text("2000000\n")
        reader._last_time -= 2.0
        watts = reader._read_rapl()
        assert watts is not None
        assert 0.0 < watts < 10.0

    def test_a_wrapped_counter_stays_positive(self, tmp_path, monkeypatch):
        pw = self._rapl(tmp_path, monkeypatch, {"intel-rapl:0": {"energy_uj": "10\n"}})
        reader = pw.PowerMonitor()
        reader._read_rapl()
        reader._last_energy_uj = 2**32 - 1000
        reader._last_time -= 1.0
        assert reader._read_rapl() > 0

    def test_an_unreadable_counter_reads_as_unknown(self, tmp_path, monkeypatch):
        pw = self._rapl(tmp_path, monkeypatch, {"intel-rapl:0": {"energy_uj": "junk\n"}})
        reader = pw.PowerMonitor()
        assert reader._package_path is None
        reader._package_path = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0" / "energy_uj"
        assert reader._read_rapl() is None


class TestHwmonPowerFallback:
    def test_a_labelled_package_input_is_the_fallback(self, tmp_path, monkeypatch):
        pw = TestRaplPowerReader()._hwmon(
            tmp_path,
            monkeypatch,
            {
                "hwmon0": {},
                "hwmon1": {"name": "acpitz\n"},
                "hwmon2": {
                    "name": "zenpower\n",
                    "power1_input": "42000000\n",
                    "power1_label": "Package Power\n",
                },
            },
        )
        reader = pw.PowerMonitor()
        assert reader.is_available()
        assert reader._hwmon_power_path.name == "power1_input"
