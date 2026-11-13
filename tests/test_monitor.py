"""Comprehensive tests for monitoring modules (hwmon, frequency, power)."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.monitor.frequency import read_max_frequency
from corecycler.monitor.power import PowerMonitor


class TestFrequencyReader:
    def test_read_max_frequency(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        d = cpu_dir / "cpu0" / "cpufreq"
        d.mkdir(parents=True)
        (d / "cpuinfo_max_freq").write_text("5800000")

        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", cpu_dir):
            result = read_max_frequency(0)
        assert result == 5800.0

    def test_read_max_frequency_missing(self, tmp_path):
        with patch("corecycler.monitor.frequency.CPUFREQ_BASE", tmp_path / "nonexistent"):
            result = read_max_frequency(0)
        assert result is None


class TestPowerMonitor:
    def _create_rapl_sysfs(self, tmp_path, energy_uj=0, name="package-0"):
        """Create mock RAPL sysfs tree."""
        rapl_dir = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0"
        rapl_dir.mkdir(parents=True)
        (rapl_dir / "energy_uj").write_text(str(energy_uj))
        (rapl_dir / "name").write_text(name)
        return tmp_path / "powercap" / "intel-rapl"

    def test_find_package(self, tmp_path):
        rapl_base = self._create_rapl_sysfs(tmp_path, 1000000)
        with patch("corecycler.monitor.power.RAPL_BASE", rapl_base):
            mon = PowerMonitor()
            assert mon.is_available() is True

    def test_not_available(self, tmp_path):
        with (
            patch("corecycler.monitor.power.RAPL_BASE", tmp_path / "nonexistent"),
            patch("corecycler.monitor.power.HWMON_BASE", tmp_path / "nonexistent2"),
        ):
            mon = PowerMonitor()
            assert mon.is_available() is False

    def test_first_read_returns_none(self, tmp_path):
        rapl_base = self._create_rapl_sysfs(tmp_path, 1000000)
        with patch("corecycler.monitor.power.RAPL_BASE", rapl_base):
            mon = PowerMonitor()
            result = mon.read_power_watts()
        assert result is None

    def test_second_read_returns_watts(self, tmp_path):
        rapl_dir = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0"
        rapl_dir.mkdir(parents=True)
        energy_file = rapl_dir / "energy_uj"
        energy_file.write_text("1000000")

        rapl_base = tmp_path / "powercap" / "intel-rapl"
        with patch("corecycler.monitor.power.RAPL_BASE", rapl_base):
            mon = PowerMonitor()
            mon.read_power_watts()

            energy_file.write_text("101000000")
            mon._last_time = time.monotonic() - 1.0

            watts = mon.read_power_watts()

        assert watts is not None
        assert watts > 0

    def test_power_calculation(self, tmp_path):
        rapl_dir = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0"
        rapl_dir.mkdir(parents=True)
        energy_file = rapl_dir / "energy_uj"
        energy_file.write_text("0")

        rapl_base = tmp_path / "powercap" / "intel-rapl"
        with patch("corecycler.monitor.power.RAPL_BASE", rapl_base):
            mon = PowerMonitor()
            assert mon.is_available()

            mon._last_energy_uj = 0
            mon._last_time = time.monotonic() - 1.0

            energy_file.write_text("100000000")
            watts = mon.read_power_watts()

        assert watts is not None
        assert watts == pytest.approx(100.0, abs=5.0)

    def test_a_counter_wrap_counts_the_energy_up_to_the_domain_s_range(self, tmp_path):
        rapl_dir = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0"
        rapl_dir.mkdir(parents=True)
        energy_file = rapl_dir / "energy_uj"
        energy_file.write_text("0")
        (rapl_dir / "max_energy_range_uj").write_text("65532610987\n")

        rapl_base = tmp_path / "powercap" / "intel-rapl"
        with patch("corecycler.monitor.power.RAPL_BASE", rapl_base):
            mon = PowerMonitor()
            mon._last_energy_uj = 65532610987 - 1_000_000
            mon._last_time = time.monotonic() - 1.0
            energy_file.write_text("1000000")
            watts = mon.read_power_watts()

        assert watts == pytest.approx(2.0, rel=0.05)

    def test_read_when_not_available(self, tmp_path):
        with (
            patch("corecycler.monitor.power.RAPL_BASE", tmp_path / "nonexistent"),
            patch("corecycler.monitor.power.HWMON_BASE", tmp_path / "nonexistent2"),
        ):
            mon = PowerMonitor()
            assert mon.read_power_watts() is None

    def test_invalid_energy_value(self, tmp_path):
        rapl_dir = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0"
        rapl_dir.mkdir(parents=True)
        (rapl_dir / "energy_uj").write_text("not_a_number")

        rapl_base = tmp_path / "powercap" / "intel-rapl"
        with (
            patch("corecycler.monitor.power.RAPL_BASE", rapl_base),
            patch("corecycler.monitor.power.HWMON_BASE", tmp_path / "nonexistent"),
        ):
            mon = PowerMonitor()
            result = mon.read_power_watts()
        assert result is None

    def test_hwmon_power_fallback(self, tmp_path):
        """When RAPL is unavailable, use zenpower power1_input (microwatts)."""
        hwmon_base = tmp_path / "hwmon"
        hwmon_dir = hwmon_base / "hwmon0"
        hwmon_dir.mkdir(parents=True)
        (hwmon_dir / "name").write_text("zenpower")
        (hwmon_dir / "power1_input").write_text("51000000")
        (hwmon_dir / "power1_label").write_text("RAPL_P_Package")

        with (
            patch("corecycler.monitor.power.RAPL_BASE", tmp_path / "nonexistent"),
            patch("corecycler.monitor.power.HWMON_BASE", hwmon_base),
        ):
            mon = PowerMonitor()
            assert mon.is_available() is True
            watts = mon.read_power_watts()

        assert watts == pytest.approx(51.0)

    def test_hwmon_power_not_used_when_rapl_available(self, tmp_path):
        """RAPL is preferred over hwmon power."""
        rapl_dir = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0"
        rapl_dir.mkdir(parents=True)
        (rapl_dir / "energy_uj").write_text("1000000")
        hwmon_base = tmp_path / "hwmon"
        hwmon_dir = hwmon_base / "hwmon0"
        hwmon_dir.mkdir(parents=True)
        (hwmon_dir / "name").write_text("zenpower")
        (hwmon_dir / "power1_input").write_text("51000000")

        rapl_base = tmp_path / "powercap" / "intel-rapl"
        with (
            patch("corecycler.monitor.power.RAPL_BASE", rapl_base),
            patch("corecycler.monitor.power.HWMON_BASE", hwmon_base),
        ):
            mon = PowerMonitor()
            assert mon.is_available() is True
            assert mon.read_power_watts() is None, "hwmon would read 51.0 W at once; RAPL needs a second sample"

    def test_a_read_in_the_same_instant_as_the_last_gives_no_watts_rather_than_dividing_by_zero(self, tmp_path):
        rapl_dir = tmp_path / "powercap" / "intel-rapl" / "intel-rapl:0"
        rapl_dir.mkdir(parents=True)
        (rapl_dir / "energy_uj").write_text("5000000")

        rapl_base = tmp_path / "powercap" / "intel-rapl"
        with (
            patch("corecycler.monitor.power.RAPL_BASE", rapl_base),
            patch("corecycler.monitor.power.time.monotonic", return_value=100.0),
        ):
            mon = PowerMonitor()
            mon._last_energy_uj = 1000000
            mon._last_time = 100.0
            assert mon.read_power_watts() is None
