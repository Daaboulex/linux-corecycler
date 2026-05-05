"""Tests for MSR (Model-Specific Register) reader module."""

from __future__ import annotations

import os
import struct
from unittest.mock import MagicMock, patch

import pytest

from monitor.msr import (
    MSR_APERF,
    MSR_CORE_ENERGY,
    MSR_MPERF,
    MSR_PWR_UNIT,
    ClockStretchReading,
    CorePowerReading,
    MSRReader,
    _EnergySnapshot,
    _PerfSnapshot,
)


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
        # Second call should not try os.open again
        with patch("os.open") as mock_open:
            result = reader.is_available()
            assert result is False
            mock_open.assert_not_called()


class TestClockStretch:
    def _make_reader(self):
        reader = MSRReader()
        reader._available = True
        return reader

    def test_first_read_returns_empty_baseline(self):
        reader = self._make_reader()
        # First call establishes baseline
        with patch.object(reader, "_read_msr", side_effect=[1000, 1000]):
            result = reader.read_clock_stretch([0])
        assert result == {}

    def test_second_read_returns_stretch(self):
        reader = self._make_reader()
        # Simulate: APERF grew by 900, MPERF grew by 1000 → 10% stretch
        reader._perf_prev[0] = _PerfSnapshot(aperf=1000, mperf=1000)

        with patch.object(reader, "_read_msr", side_effect=[1900, 2000]):
            result = reader.read_clock_stretch([0])

        assert 0 in result
        assert abs(result[0].ratio - 0.9) < 0.01
        assert abs(result[0].stretch_pct - 10.0) < 0.1

    def test_no_stretch_when_ratio_is_one(self):
        reader = self._make_reader()
        reader._perf_prev[0] = _PerfSnapshot(aperf=1000, mperf=1000)

        with patch.object(reader, "_read_msr", side_effect=[2000, 2000]):
            result = reader.read_clock_stretch([0])

        assert result[0].stretch_pct == 0.0
        assert abs(result[0].ratio - 1.0) < 0.01

    def test_turbo_ratio_above_one_clamps_stretch_to_zero(self):
        reader = self._make_reader()
        reader._perf_prev[0] = _PerfSnapshot(aperf=1000, mperf=1000)

        # APERF > MPERF = turbo boost beyond reference
        with patch.object(reader, "_read_msr", side_effect=[2100, 2000]):
            result = reader.read_clock_stretch([0])

        assert result[0].stretch_pct == 0.0
        assert result[0].ratio > 1.0

    def test_msr_read_failure_skips_cpu(self):
        reader = self._make_reader()
        with patch.object(reader, "_read_msr", return_value=None):
            result = reader.read_clock_stretch([0])
        assert result == {}

    def test_multiple_cpus(self):
        reader = self._make_reader()
        reader._perf_prev[0] = _PerfSnapshot(aperf=100, mperf=100)
        reader._perf_prev[1] = _PerfSnapshot(aperf=100, mperf=100)

        def mock_read(cpu_id, msr_addr):
            if cpu_id == 0:
                return 200 if msr_addr == MSR_APERF else 200
            return 180 if msr_addr == MSR_APERF else 200

        with patch.object(reader, "_read_msr", side_effect=mock_read):
            result = reader.read_clock_stretch([0, 1])

        assert 0 in result
        assert 1 in result
        assert result[0].stretch_pct == 0.0  # no stretch
        assert result[1].stretch_pct > 0  # 10% stretch

    def test_zero_mperf_delta_skipped(self):
        reader = self._make_reader()
        reader._perf_prev[0] = _PerfSnapshot(aperf=1000, mperf=1000)

        # Same MPERF = 0 delta
        with patch.object(reader, "_read_msr", side_effect=[1100, 1000]):
            result = reader.read_clock_stretch([0])
        assert result == {}


class TestCorePower:
    def _make_reader(self):
        reader = MSRReader()
        reader._available = True
        reader._energy_unit = 1.0 / (1 << 14)  # ~61 µJ per tick
        return reader

    def test_first_read_returns_empty(self):
        reader = self._make_reader()
        with patch.object(reader, "_read_msr", return_value=1000000):
            result = reader.read_core_power([0])
        assert result == {}

    def test_second_read_returns_watts(self):
        reader = self._make_reader()
        # Seed with initial reading
        reader._energy_prev[0] = _EnergySnapshot(energy_raw=1000000, timestamp=100.0)

        with (
            patch.object(reader, "_read_msr", return_value=1000000 + 163840),
            patch("time.monotonic", return_value=101.0),
        ):
            result = reader.read_core_power([0])

        assert 0 in result
        # 163840 ticks * (1/16384) J/tick / 1.0 s = 10.0 W
        assert abs(result[0].watts - 10.0) < 0.1

    def test_energy_unit_not_available(self):
        reader = MSRReader()
        reader._available = True
        reader._energy_unit = None
        with patch.object(reader, "_read_msr", return_value=None):
            result = reader.read_core_power([0])
        assert result == {}

    def test_clock_stretch_does_not_affect_power(self):
        """Verify separated snapshot state — clock stretch reads don't corrupt power reads."""
        reader = self._make_reader()

        # Seed perf snapshot (clock stretch baseline)
        reader._perf_prev[0] = _PerfSnapshot(aperf=1000, mperf=1000)

        # Seed energy snapshot (power baseline)
        reader._energy_prev[0] = _EnergySnapshot(energy_raw=1000000, timestamp=100.0)

        # Read clock stretch — should NOT touch energy state
        with patch.object(reader, "_read_msr", side_effect=[2000, 2000]):
            stretch = reader.read_clock_stretch([0])

        # Energy snapshot should be untouched
        assert reader._energy_prev[0].energy_raw == 1000000

        # Read power — should compute correct watts from energy snapshot
        with (
            patch.object(reader, "_read_msr", return_value=1000000 + 163840),
            patch("time.monotonic", return_value=101.0),
        ):
            power = reader.read_core_power([0])

        assert 0 in power
        assert abs(power[0].watts - 10.0) < 0.1


class TestMSRClose:
    def test_close_clears_state(self):
        reader = MSRReader()
        reader._fds = {0: 10, 1: 11}
        reader._perf_prev = {0: _PerfSnapshot(aperf=1, mperf=1)}
        reader._energy_prev = {0: _EnergySnapshot(energy_raw=1, timestamp=1.0)}
        reader._pkg_energy_prev = _EnergySnapshot(energy_raw=1, timestamp=1.0)

        with patch("os.close"):
            reader.close()

        assert reader._fds == {}
        assert reader._perf_prev == {}
        assert reader._energy_prev == {}
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
            result = reader._read_msr(0, MSR_APERF)

        assert result == val

    def test_read_msr_failure_returns_none(self):
        reader = MSRReader()
        with patch("os.open", side_effect=OSError):
            result = reader._read_msr(0, MSR_APERF)
        assert result is None

    def test_fd_cached(self):
        reader = MSRReader()
        mock_data = struct.pack("Q", 42)

        with (
            patch("os.open", return_value=99) as mock_open,
            patch("os.pread", return_value=mock_data),
        ):
            reader._read_msr(0, MSR_APERF)
            reader._read_msr(0, MSR_MPERF)

        # os.open should only be called once (fd cached)
        mock_open.assert_called_once()
