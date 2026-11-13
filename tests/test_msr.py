"""Tests for MSR (Model-Specific Register) reader module."""

from __future__ import annotations

import errno
import os
import struct
from unittest.mock import patch

import pytest

from corecycler.monitor.msr import (
    MSR_MPERF,
    MSRReader,
    _EnergySnapshot,
    msr_problem,
)


class TestMsrProblem:
    def test_a_readable_device_has_no_problem(self, tmp_path):
        device = tmp_path / "msr"
        device.write_bytes(bytes(16))
        assert msr_problem(str(device)) is None

    def test_the_probe_reads_whole_registers_as_the_msr_driver_requires(self, tmp_path, monkeypatch):
        device = tmp_path / "msr"
        device.write_bytes(bytes(256))
        real_pread = os.pread

        def msr_driver_pread(fd, count, offset):
            if count % 8:
                raise OSError(errno.EINVAL, os.strerror(errno.EINVAL))
            return real_pread(fd, count, offset)

        monkeypatch.setattr(os, "pread", msr_driver_pread)
        assert msr_problem(str(device)) is None

    def test_a_missing_device_names_its_path_and_the_error_read(self, tmp_path):
        device = tmp_path / "absent"
        assert msr_problem(str(device)) == f"{device}: {os.strerror(errno.ENOENT)}"

    def test_an_unreadable_device_names_its_path_and_the_error_read(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root reads a file whatever its mode")
        device = tmp_path / "msr"
        device.write_bytes(bytes(16))
        device.chmod(0)
        assert msr_problem(str(device)) == f"{device}: {os.strerror(errno.EACCES)}"


class TestMSRReaderAvailability:
    def test_not_available_when_no_device(self):
        reader = MSRReader()
        with patch("os.open", side_effect=OSError("No such file")):
            assert reader.is_available() is False

    def test_not_available_when_permission_denied(self):
        reader = MSRReader()
        with patch("os.open", side_effect=PermissionError("Permission denied")):
            assert reader.is_available() is False

    def test_available_when_device_readable(self):
        reader = MSRReader()
        mock_data = struct.pack("Q", 12345)
        with (
            patch("os.open", return_value=99),
            patch("os.pread", return_value=mock_data),
            patch("os.close"),
        ):
            assert reader.is_available() is True

    def test_availability_cached(self):
        reader = MSRReader()
        with patch("os.open", side_effect=OSError):
            reader.is_available()
        with patch("os.open") as mock_open:
            result = reader.is_available()
            assert result is False
            mock_open.assert_not_called()


class TestCorePower:
    def _make_reader(self):
        reader = MSRReader()
        reader._available = True
        reader._joules_per_tick = 1.0 / (1 << 14)
        return reader

    def test_first_read_returns_empty(self):
        reader = self._make_reader()
        with patch.object(reader, "_read_msr", return_value=1000000):
            result = reader.read_core_power([0])
        assert result == {}

    def test_second_read_returns_watts(self):
        reader = self._make_reader()
        reader._core_energy_prev[0] = _EnergySnapshot(energy_raw=1000000, timestamp=100.0)

        with (
            patch.object(reader, "_read_msr", return_value=1000000 + 10 * (1 << 14)),
            patch("time.monotonic", return_value=101.0),
        ):
            result = reader.read_core_power([0])

        assert 0 in result
        assert abs(result[0].watts - 10.0) < 0.1

    def test_energy_unit_not_available(self):
        reader = MSRReader()
        reader._available = True
        reader._joules_per_tick = None
        with patch.object(reader, "_read_msr", return_value=None):
            result = reader.read_core_power([0])
        assert result == {}


class TestMSRClose:
    def test_close_clears_state(self):
        reader = MSRReader()
        reader._fd_by_cpu = {0: 10, 1: 11}
        reader._core_energy_prev = {0: _EnergySnapshot(energy_raw=1, timestamp=1.0)}
        reader._pkg_energy_prev = _EnergySnapshot(energy_raw=1, timestamp=1.0)

        with patch("os.close"):
            reader.close()

        assert reader._fd_by_cpu == {}
        assert reader._core_energy_prev == {}
        assert reader._pkg_energy_prev is None


class TestReadMSR:
    def test_read_msr_returns_uint64(self):
        reader = MSRReader()
        val = 0xDEADBEEFCAFEBABE
        mock_data = struct.pack("Q", val)

        with (
            patch("os.open", return_value=99),
            patch("os.pread", return_value=mock_data),
        ):
            result = reader._read_msr(0, MSR_MPERF)

        assert result == val

    def test_read_msr_failure_returns_none(self):
        reader = MSRReader()
        with patch("os.open", side_effect=OSError):
            result = reader._read_msr(0, MSR_MPERF)
        assert result is None

    def test_fd_cached(self):
        reader = MSRReader()
        mock_data = struct.pack("Q", 42)

        with (
            patch("os.open", return_value=99) as mock_open,
            patch("os.pread", return_value=mock_data),
        ):
            reader._read_msr(0, MSR_MPERF)
            reader._read_msr(0, MSR_MPERF)

        mock_open.assert_called_once()


class TestReadPackagePower:
    def _make_reader(self):
        reader = MSRReader()
        reader._available = True
        reader._joules_per_tick = 1.0 / (1 << 14)
        return reader

    def test_not_available_returns_none(self):
        reader = MSRReader()
        reader._available = False
        assert reader.read_package_power() is None

    def test_energy_unit_unavailable_returns_none(self):
        reader = MSRReader()
        reader._available = True
        reader._joules_per_tick = None
        with patch.object(reader, "_read_msr", return_value=None):
            assert reader.read_package_power() is None

    def test_raw_read_failure_returns_none(self):
        reader = self._make_reader()
        with patch.object(reader, "_read_msr", return_value=None):
            assert reader.read_package_power() is None

    def test_first_read_establishes_baseline(self):
        reader = self._make_reader()
        with (
            patch.object(reader, "_read_msr", return_value=1_000_000),
            patch("time.monotonic", return_value=100.0),
        ):
            assert reader.read_package_power() is None
        assert reader._pkg_energy_prev.energy_raw == 1_000_000

    def test_second_read_returns_watts(self):
        reader = self._make_reader()
        reader._pkg_energy_prev = _EnergySnapshot(energy_raw=1_000_000, timestamp=100.0)
        with (
            patch.object(reader, "_read_msr", return_value=1_000_000 + 163840),
            patch("time.monotonic", return_value=101.0),
        ):
            watts = reader.read_package_power()
        assert watts is not None
        assert abs(watts - 10.0) < 0.1

    def test_counter_wraparound(self):
        reader = self._make_reader()
        reader._pkg_energy_prev = _EnergySnapshot(energy_raw=(1 << 32) - 100, timestamp=100.0)
        with (
            patch.object(reader, "_read_msr", return_value=60),
            patch("time.monotonic", return_value=101.0),
        ):
            watts = reader.read_package_power()
        assert watts is not None
        assert watts > 0


class TestMSRExtraBranches:
    def test_read_core_power_unavailable_returns_empty(self):
        reader = MSRReader()
        reader._available = False
        assert reader.read_core_power([0]) == {}

    def test_core_power_raw_read_failure_skips_cpu(self):
        reader = MSRReader()
        reader._available = True
        reader._joules_per_tick = 1.0 / (1 << 14)
        with patch.object(reader, "_read_msr", return_value=None):
            assert reader.read_core_power([0]) == {}

    def test_core_power_counter_wraparound(self):
        reader = MSRReader()
        reader._available = True
        reader._joules_per_tick = 1.0 / (1 << 14)
        reader._core_energy_prev[0] = _EnergySnapshot(energy_raw=(1 << 32) - 100, timestamp=100.0)
        with (
            patch.object(reader, "_read_msr", return_value=60),
            patch("time.monotonic", return_value=101.0),
        ):
            result = reader.read_core_power([0])
        assert result[0].watts == pytest.approx(160 / (1 << 14))

    def test_each_reading_asks_the_register_amd_documents_for_it(self):
        reader = MSRReader()
        reader._available = True
        asked = []

        def read(cpu_id, register):
            asked.append(register)
            return 0x0A0E03

        with patch.object(reader, "_read_msr", side_effect=read), patch("time.monotonic", return_value=1.0):
            reader.read_core_power([0])
            reader.read_package_power()
        assert asked == [0xC0010299, 0xC001029A, 0xC001029B]

    def test_read_msr_pread_failure_returns_none(self):
        reader = MSRReader()
        with (
            patch("os.open", return_value=99),
            patch("os.pread", side_effect=OSError),
            patch("os.close"),
        ):
            assert reader._read_msr(0, MSR_MPERF) is None

    def test_get_energy_unit_computes_and_caches(self):
        reader = MSRReader()
        raw = 14 << 8
        with patch.object(reader, "_read_msr", return_value=raw):
            unit = reader._get_energy_unit()
        assert unit == 1.0 / (1 << 14)
        with patch.object(reader, "_read_msr") as m2:
            assert reader._get_energy_unit() == unit
            m2.assert_not_called()

    def test_get_energy_unit_read_failure_returns_none(self):
        reader = MSRReader()
        with patch.object(reader, "_read_msr", return_value=None):
            assert reader._get_energy_unit() is None

    def test_del_swallows_close_error(self):
        reader = MSRReader()
        with patch.object(reader, "close", side_effect=RuntimeError("shutdown")):
            reader.__del__()
        assert reader._fd_by_cpu == {}
