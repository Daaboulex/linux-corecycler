"""Comprehensive tests for PM table reader."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from smu.pmtable import PMTableData, PMTableReader


# ===========================================================================
# PMTableData tests
# ===========================================================================


class TestPMTableData:
    def test_defaults(self):
        data = PMTableData()
        assert data.core_frequency_mhz == {}
        assert data.core_voltage_v == {}
        assert data.core_temperature_c == {}
        assert data.core_power_w == {}
        assert data.core_c0_residency == {}
        assert data.package_power_w == 0.0
        assert data.soc_power_w == 0.0
        assert data.ppt_limit_w == 0.0
        assert data.tdc_limit_a == 0.0
        assert data.edc_limit_a == 0.0
        assert data.ppt_value_w == 0.0
        assert data.tdc_value_a == 0.0
        assert data.edc_value_a == 0.0
        assert data.tctl_c == 0.0
        assert data.tdie_c == 0.0
        assert data.raw_floats == []


# ===========================================================================
# PMTableReader tests
# ===========================================================================


class TestPMTableReader:
    def test_is_available_with_pm_table(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table").write_bytes(b"\x00" * 100)
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.is_available() is True

    def test_is_not_available(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.is_available() is False

    def test_is_not_available_missing_dir(self, tmp_path):
        reader = PMTableReader(sysfs_path=tmp_path / "nonexistent")
        assert reader.is_available() is False

    def test_read_unavailable(self, tmp_path):
        reader = PMTableReader(sysfs_path=tmp_path / "nonexistent")
        assert reader.read() is None

    def test_read_missing_pm_table(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.read() is None

    def test_read_empty_pm_table(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table").write_bytes(b"")
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.read() is None

    def test_read_too_short(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table").write_bytes(b"\x00\x00")  # < 4 bytes
        reader = PMTableReader(sysfs_path=smu_dir)
        assert reader.read() is None

    def test_read_minimal_data(self, tmp_path):
        """4 bytes = 1 float, too short for parsing but should return PMTableData."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        data = struct.pack("<f", 42.0)
        (smu_dir / "pm_table").write_bytes(data)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()
        assert result is not None
        assert len(result.raw_floats) == 1
        assert result.raw_floats[0] == pytest.approx(42.0)

    def test_read_full_pm_table(self, tmp_path):
        """Create a PM table with enough data for Granite Ridge parsing."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        # Build 420 floats (enough for 16 cores with stride 10 starting at 100)
        floats = [0.0] * 420

        # Package telemetry
        floats[0] = 200.0    # PPT limit
        floats[1] = 142.0    # PPT value
        floats[2] = 120.0    # TDC limit
        floats[3] = 95.0     # TDC value
        floats[4] = 180.0    # EDC limit
        floats[5] = 150.0    # EDC value
        floats[10] = 72.5    # Tctl
        floats[11] = 70.0    # Tdie
        floats[26] = 141.5   # package power
        floats[28] = 18.3    # SoC power

        # Per-core data (core 0 at offset 100)
        floats[100] = 5700.0  # core 0 freq
        floats[101] = 1.35    # core 0 voltage
        floats[102] = 12.5    # core 0 power
        floats[103] = 68.0    # core 0 temp
        floats[104] = 95.0    # core 0 C0 residency

        # Core 1 at offset 110
        floats[110] = 5500.0
        floats[111] = 1.30
        floats[112] = 11.0
        floats[113] = 66.0
        floats[114] = 80.0

        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(num_cores=16, sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.ppt_limit_w == pytest.approx(200.0)
        assert result.ppt_value_w == pytest.approx(142.0)
        assert result.tdc_limit_a == pytest.approx(120.0)
        assert result.tdc_value_a == pytest.approx(95.0)
        assert result.edc_limit_a == pytest.approx(180.0)
        assert result.edc_value_a == pytest.approx(150.0)
        assert result.tctl_c == pytest.approx(72.5)
        assert result.tdie_c == pytest.approx(70.0)
        assert result.package_power_w == pytest.approx(141.5)
        assert result.soc_power_w == pytest.approx(18.3)

        assert result.core_frequency_mhz[0] == pytest.approx(5700.0)
        assert result.core_voltage_v[0] == pytest.approx(1.35)
        assert result.core_power_w[0] == pytest.approx(12.5)
        assert result.core_temperature_c[0] == pytest.approx(68.0)
        assert result.core_c0_residency[0] == pytest.approx(95.0)

        assert result.core_frequency_mhz[1] == pytest.approx(5500.0)
        assert result.core_voltage_v[1] == pytest.approx(1.30)

    def test_partial_core_data(self, tmp_path):
        """PM table that's too short for all cores should parse what it can."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        # 210 floats: enough to trigger parsing (>= 200) and cover core 0
        # (offset 100) and core 1 (offset 110), but NOT core 11+ (offset 210+)
        floats = [0.0] * 210
        floats[0] = 100.0  # PPT limit
        floats[100] = 4800.0  # core 0 freq
        floats[110] = 5100.0  # core 1 freq

        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(num_cores=16, sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.ppt_limit_w == pytest.approx(100.0)
        assert result.core_frequency_mhz[0] == pytest.approx(4800.0)
        assert result.core_frequency_mhz[1] == pytest.approx(5100.0)
        # Core 11+ should not be present (offset 210+ out of range)
        assert 11 not in result.core_frequency_mhz

    def test_fewer_than_200_floats_skips_parsing(self, tmp_path):
        """PM table with < 200 floats should skip Granite Ridge parsing."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        floats = [1.0] * 50
        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert len(result.raw_floats) == 50
        # No parsed fields (< 200 floats triggers early return)
        assert result.core_frequency_mhz == {}
        assert result.ppt_limit_w == 0.0

    def test_raw_floats_always_available(self, tmp_path):
        """raw_floats should contain the full array regardless of parsing."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        floats = [float(i) for i in range(300)]
        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert len(result.raw_floats) == 300
        assert result.raw_floats[0] == pytest.approx(0.0)
        assert result.raw_floats[299] == pytest.approx(299.0)

    def test_num_cores_limits_parsing(self, tmp_path):
        """num_cores parameter should limit how many cores are parsed."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        floats = [0.0] * 420
        for i in range(16):
            floats[100 + i * 10] = 5000.0 + i * 100  # freq per core

        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        # Only parse 4 cores
        reader = PMTableReader(num_cores=4, sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert len(result.core_frequency_mhz) == 4
        assert 4 not in result.core_frequency_mhz

    def test_32_core_limit(self, tmp_path):
        """Parser should cap at 32 cores even if num_cores is higher."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        # Need enough floats for 33+ cores
        floats = [0.0] * 600
        for i in range(40):
            floats[100 + i * 10] = 4000.0 + i

        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(num_cores=40, sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        # Should cap at 32 cores
        assert len(result.core_frequency_mhz) <= 32

    def test_os_error_reading(self, tmp_path):
        """OSError on read should return None."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        pm_table = smu_dir / "pm_table"
        pm_table.write_bytes(b"\x00" * 100)
        # Make it unreadable
        pm_table.chmod(0o000)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()
        assert result is None

        # Restore permissions for cleanup
        pm_table.chmod(0o644)

    def test_non_aligned_data(self, tmp_path):
        """Data not aligned to 4 bytes should still parse what it can."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        # 201 floats + 2 extra bytes
        floats = [1.0] * 201
        raw = struct.pack(f"<{len(floats)}f", *floats) + b"\xAA\xBB"
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        # Should parse 201 floats (ignoring trailing 2 bytes)
        assert len(result.raw_floats) == 201
