"""Error and fallback path coverage for the monitor readers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.monitor import frequency as freqmod
from corecycler.monitor.cpu_usage import CPUUsageReader
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
        directory_in_place_of_file = dom / "name"
        directory_in_place_of_file.mkdir()
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
        assert mon._read_rapl(mon._package_path) is None

    def test_read_hwmon_power_unreadable_returns_none(self, tmp_path):
        mon = PowerMonitor.__new__(PowerMonitor)
        mon._hwmon_power_path = tmp_path / "gone" / "power1_input"
        assert mon._read_hwmon_power(mon._hwmon_power_path) is None


class TestFrequencyEdges:
    def test_read_max_frequency_unreadable_returns_none(self, tmp_path):
        base = tmp_path / "cpu"
        cf = base / "cpu0" / "cpufreq"
        cf.mkdir(parents=True)
        (cf / "cpuinfo_max_freq").write_text("garbage\n")
        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", base):
            assert freqmod.read_max_frequency(0) is None


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

    def test_the_boost_limit_is_read(self, tmp_path, monkeypatch):
        freq = self._tree(
            tmp_path,
            monkeypatch,
            {
                "cpu0": {"cpuinfo_max_freq": "5700000\n"},
            },
        )
        assert freq.read_max_frequency(0) == 5700.0

    def test_an_absent_boost_limit_reads_as_unknown(self, tmp_path, monkeypatch):
        freq = self._tree(tmp_path, monkeypatch, {"cpu0": {}})
        assert freq.read_max_frequency(0) is None


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
        assert reader._read_rapl(reader._package_path) is None
        reader._package_path.write_text("2000000\n")
        reader._last_time -= 2.0
        watts = reader._read_rapl(reader._package_path)
        assert watts is not None
        assert 0.0 < watts < 10.0

    def test_a_wrap_without_a_known_range_drops_that_one_reading(self, tmp_path, monkeypatch, caplog):
        pw = self._rapl(tmp_path, monkeypatch, {"intel-rapl:0": {"energy_uj": "10\n"}})
        with caplog.at_level("WARNING", logger="corecycler.monitor.power"):
            reader = pw.PowerMonitor()
        assert "max_energy_range_uj" in caplog.text
        reader._read_rapl(reader._package_path)
        reader._last_energy_uj = 5_000_000
        reader._last_time -= 1.0
        assert reader._read_rapl(reader._package_path) is None
        reader._package_path.write_text("1000010\n")
        reader._last_time -= 1.0
        assert reader._read_rapl(reader._package_path) == pytest.approx(1.0, rel=0.05)

    def test_an_unreadable_counter_reads_as_unknown(self, tmp_path, monkeypatch):
        pw = self._rapl(tmp_path, monkeypatch, {"intel-rapl:0": {"energy_uj": "junk\n"}})
        reader = pw.PowerMonitor()
        assert reader._package_path is None
        reader._package_path = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0" / "energy_uj"
        assert reader._read_rapl(reader._package_path) is None


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
