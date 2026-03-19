"""Comprehensive tests for PM table reader."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from smu.pmtable import (
    PMTableData,
    PMTableOffsets,
    PMTableReader,
    PM_TABLE_OFFSETS,
    compute_fclk_uclk_ratio,
)


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


# ===========================================================================
# Helper for version-aware sysfs mocking
# ===========================================================================


def _make_smu_dir(
    tmp_path: Path,
    *,
    version_int: int | None = None,
    raw_bytes: bytes | None = None,
    num_floats: int = 0,
) -> Path:
    """Create a mock sysfs smu directory with optional version and pm_table data.

    If raw_bytes is provided, it is written directly as pm_table.
    Otherwise, num_floats zero-floats are packed as pm_table.
    If version_int is provided, pm_table_version is written as 4-byte LE uint32.
    """
    smu_dir = tmp_path / "ryzen_smu_drv"
    smu_dir.mkdir(exist_ok=True)

    if raw_bytes is not None:
        (smu_dir / "pm_table").write_bytes(raw_bytes)
    elif num_floats > 0:
        (smu_dir / "pm_table").write_bytes(
            struct.pack(f"<{num_floats}f", *([0.0] * num_floats))
        )

    if version_int is not None:
        (smu_dir / "pm_table_version").write_bytes(
            struct.pack("<I", version_int)
        )

    return smu_dir


def _build_versioned_pm_table(
    version: int,
    *,
    fclk: float = 0.0,
    uclk: float = 0.0,
    mclk: float = 0.0,
    vddcr_soc: float = 0.0,
    vdd_mem: float = 0.0,
) -> bytes:
    """Build a raw PM table with values at the correct byte offsets for a version.

    Creates a zeroed byte array of the appropriate table_size and inserts
    float values at the known offsets. Also populates enough data for
    legacy _parse_granite_ridge (200+ floats).
    """
    offsets = PM_TABLE_OFFSETS.get(version)
    if offsets is None:
        # Use a generic size for unknown versions
        table_size = 0x994
    else:
        table_size = offsets.table_size

    # Ensure table is large enough for legacy parsing (>= 200 floats = 800 bytes)
    table_size = max(table_size, 800)
    raw = bytearray(table_size)

    if offsets is not None:
        if fclk != 0.0:
            struct.pack_into("<f", raw, offsets.fclk, fclk)
        if uclk != 0.0:
            struct.pack_into("<f", raw, offsets.uclk, uclk)
        if mclk != 0.0:
            struct.pack_into("<f", raw, offsets.mclk, mclk)
        if vddcr_soc != 0.0:
            struct.pack_into("<f", raw, offsets.vddcr_soc, vddcr_soc)
        if vdd_mem != 0.0 and offsets.vdd_mem >= 0:
            struct.pack_into("<f", raw, offsets.vdd_mem, vdd_mem)

    return bytes(raw)


# ===========================================================================
# PMTableOffsets tests
# ===========================================================================


class TestPMTableOffsets:
    def test_frozen_dataclass_with_slots(self):
        """PMTableOffsets is a frozen dataclass with slots."""
        offsets = PMTableOffsets(
            table_size=0x994,
            fclk=0x11C,
            uclk=0x12C,
            mclk=0x13C,
            vddcr_soc=0x14C,
            cldo_vddp=0x434,
            cldo_vddg_iod=0x40C,
            cldo_vddg_ccd=0x414,
            vdd_misc=0xE8,
            vdd_mem=0x43C,
        )
        assert offsets.fclk == 0x11C
        assert offsets.uclk == 0x12C
        assert offsets.mclk == 0x13C
        # Verify frozen
        with pytest.raises(AttributeError):
            offsets.fclk = 0x200  # type: ignore[misc]
        # Verify slots
        assert hasattr(offsets, "__slots__")

    def test_known_version_0x620205_exists(self):
        """PM_TABLE_OFFSETS[0x620205] exists with correct clock offsets."""
        offsets = PM_TABLE_OFFSETS[0x620205]
        assert offsets.fclk == 0x11C
        assert offsets.uclk == 0x12C
        assert offsets.mclk == 0x13C
        assert offsets.vddcr_soc == 0x14C
        assert offsets.vdd_mem == 0x43C

    def test_known_version_0x621102_exists(self):
        """PM_TABLE_OFFSETS[0x621102] exists (Zen 5 variant)."""
        offsets = PM_TABLE_OFFSETS[0x621102]
        assert offsets.fclk == 0x11C
        assert offsets.uclk == 0x12C
        assert offsets.mclk == 0x13C
        assert offsets.vdd_mem == -1  # not available on this version


# ===========================================================================
# PMTableData new fields tests
# ===========================================================================


class TestPMTableDataNewFields:
    def test_new_fields_defaults(self):
        """PMTableData has new memory controller fields with correct defaults."""
        data = PMTableData()
        assert data.fclk_mhz == 0.0
        assert data.uclk_mhz == 0.0
        assert data.mclk_mhz == 0.0
        assert data.vddcr_soc_v == 0.0
        assert data.vdd_mem_v == 0.0
        assert data.pm_table_version == 0
        assert data.is_calibrated is False


# ===========================================================================
# Version-dispatch tests
# ===========================================================================


class TestVersionDispatch:
    def test_known_version_dispatch(self, tmp_path):
        """read() with version 0x00620205 produces correct clock/voltage values."""
        raw = _build_versioned_pm_table(
            0x620205,
            fclk=2000.0,
            uclk=3000.0,
            mclk=3000.0,
            vddcr_soc=1.25,
            vdd_mem=1.395,
        )
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00620205, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.pm_table_version == 0x00620205
        assert result.fclk_mhz == pytest.approx(2000.0)
        assert result.uclk_mhz == pytest.approx(3000.0)
        assert result.mclk_mhz == pytest.approx(3000.0)
        assert result.vddcr_soc_v == pytest.approx(1.25)
        assert result.vdd_mem_v == pytest.approx(1.395)

    def test_unknown_version_uncalibrated(self, tmp_path):
        """read() with unknown version produces is_calibrated=False."""
        # Use a table big enough for legacy parsing
        floats = [0.0] * 300
        raw = struct.pack(f"<{len(floats)}f", *floats)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x99999999, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is False
        assert result.pm_table_version == 0x99999999
        assert len(result.raw_floats) > 0

    def test_no_version_file_falls_back_to_legacy(self, tmp_path):
        """read() without pm_table_version file uses legacy _parse_granite_ridge."""
        floats = [0.0] * 420
        floats[0] = 200.0  # PPT limit
        floats[100] = 5700.0  # core 0 freq
        raw = struct.pack(f"<{len(floats)}f", *floats)
        smu_dir = _make_smu_dir(tmp_path, version_int=None, raw_bytes=raw)
        reader = PMTableReader(num_cores=16, sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        # Legacy behavior: core data parsed, no version info
        assert result.ppt_limit_w == pytest.approx(200.0)
        assert result.core_frequency_mhz[0] == pytest.approx(5700.0)
        # No version dispatch happened
        assert result.pm_table_version == 0
        assert result.is_calibrated is False

    def test_zen5_prefix_match(self, tmp_path):
        """read() with Zen 5 prefix match (0x621102) uses Zen 5 offsets."""
        raw = _build_versioned_pm_table(
            0x621102,
            fclk=1800.0,
            uclk=3600.0,
            mclk=3600.0,
            vddcr_soc=1.15,
        )
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00621102, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.fclk_mhz == pytest.approx(1800.0)
        assert result.uclk_mhz == pytest.approx(3600.0)
        assert result.mclk_mhz == pytest.approx(3600.0)

    def test_zen5_unknown_exact_prefix_fallback(self, tmp_path):
        """read() with unknown 0x62xxxx version falls back to Zen 5 prefix offsets."""
        # 0x62FFFF is not in PM_TABLE_OFFSETS but matches Zen 5 prefix 0x62
        # Build raw bytes large enough with values at Zen 5 clock offsets
        raw = bytearray(0x994)
        struct.pack_into("<f", raw, 0x11C, 1900.0)  # fclk
        struct.pack_into("<f", raw, 0x12C, 1900.0)  # uclk
        struct.pack_into("<f", raw, 0x13C, 1900.0)  # mclk
        struct.pack_into("<f", raw, 0x14C, 1.1)      # vddcr_soc
        smu_dir = _make_smu_dir(tmp_path, version_int=0x0062FFFF, raw_bytes=bytes(raw))
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.fclk_mhz == pytest.approx(1900.0)

    def test_vdd_mem_negative_offset_stays_zero(self, tmp_path):
        """offset -1 for vdd_mem means field stays at 0.0 (not read)."""
        # 0x621102 has vdd_mem=-1
        raw = _build_versioned_pm_table(
            0x621102,
            fclk=2000.0,
            uclk=3000.0,
            mclk=3000.0,
            vddcr_soc=1.25,
            vdd_mem=1.4,  # this should NOT be written since offset is -1
        )
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00621102, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.vdd_mem_v == 0.0  # not read because offset is -1

    def test_out_of_range_offset_returns_zero(self, tmp_path):
        """Out-of-range offset (beyond raw bytes) returns 0.0, no crash."""
        # Create a PM table that is smaller than the expected table_size
        # so some offsets will be out of range
        small_raw = bytearray(256)  # much smaller than 0x994
        struct.pack_into("<f", small_raw, 0x11C % 256, 2000.0)  # may or may not work
        smu_dir = _make_smu_dir(
            tmp_path, version_int=0x00620205, raw_bytes=bytes(small_raw)
        )
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        # Should not crash -- out-of-range offsets produce 0.0
        assert result is not None
        # vdd_mem at 0x43C is way beyond 256 bytes
        assert result.vdd_mem_v == 0.0

    def test_legacy_core_data_still_parsed_with_version(self, tmp_path):
        """Legacy _parse_granite_ridge core data is still parsed when version is known."""
        raw = bytearray(_build_versioned_pm_table(
            0x620205,
            fclk=2000.0,
            uclk=3000.0,
            mclk=3000.0,
            vddcr_soc=1.25,
            vdd_mem=1.395,
        ))
        # Insert legacy core data
        # PPT limit at float index 0 (byte offset 0)
        struct.pack_into("<f", raw, 0, 200.0)
        # Core 0 freq at float index 100 (byte offset 400)
        struct.pack_into("<f", raw, 400, 5700.0)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00620205, raw_bytes=bytes(raw))
        reader = PMTableReader(num_cores=16, sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        # Versioned data
        assert result.is_calibrated is True
        assert result.fclk_mhz == pytest.approx(2000.0)
        # Legacy core data also parsed
        assert result.ppt_limit_w == pytest.approx(200.0)
        assert result.core_frequency_mhz[0] == pytest.approx(5700.0)


# ===========================================================================
# compute_fclk_uclk_ratio tests
# ===========================================================================


class TestComputeFclkUclkRatio:
    def test_ratio_1_1(self):
        """FCLK=UCLK produces (1, 1) ratio."""
        assert compute_fclk_uclk_ratio(2000.0, 2000.0) == (1, 1)

    def test_ratio_1_2(self):
        """UCLK=2*FCLK produces (1, 2) ratio."""
        assert compute_fclk_uclk_ratio(1000.0, 2000.0) == (1, 2)

    def test_zero_fclk_returns_none(self):
        assert compute_fclk_uclk_ratio(0.0, 2000.0) is None

    def test_zero_uclk_returns_none(self):
        assert compute_fclk_uclk_ratio(2000.0, 0.0) is None

    def test_negative_returns_none(self):
        assert compute_fclk_uclk_ratio(-100.0, 2000.0) is None

    def test_ratio_2_3(self):
        """DDR5-6000 with FCLK capped: FCLK=2000, UCLK=3000 → 2:3."""
        assert compute_fclk_uclk_ratio(2000.0, 3000.0) == (2, 3)

    def test_ratio_1_3(self):
        """FCLK=1000, UCLK=3000 → 1:3."""
        assert compute_fclk_uclk_ratio(1000.0, 3000.0) == (1, 3)

    def test_near_1_1_ratio(self):
        """Slightly off ratio should still round to 1:1."""
        assert compute_fclk_uclk_ratio(2000.0, 2001.0) == (1, 1)

    def test_near_1_2_ratio(self):
        """Slightly off ratio should still round to 1:2."""
        assert compute_fclk_uclk_ratio(1000.0, 1999.0) == (1, 2)
