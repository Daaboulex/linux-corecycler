"""Comprehensive tests for PM table reader."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.smu.pmtable import (
    MEASURED_CORE_IDS,
    PM_TABLE_OFFSETS,
    PMTableData,
    PMTableOffsets,
    PMTableReader,
    compute_fclk_uclk_ratio,
    core_reads,
    package_watts,
    rail_volts,
)


class TestPMTableData:
    def test_defaults(self):
        data = PMTableData()
        assert data.package_power_w == 0.0
        assert data.ppt_limit_w == 0.0
        assert data.tdc_limit_a == 0.0
        assert data.edc_limit_a == 0.0
        assert data.ppt_value_w == 0.0
        assert data.tdc_value_a == 0.0
        assert data.edc_value_a is None
        assert data.tctl_c == 0.0
        assert data.raw_floats == []


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
        (smu_dir / "pm_table").write_bytes(b"\x00\x00")
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
        """Zen 5 header layout (evidence-based indices) plus core arrays:
        [2]=PPT limit (static), [3]=package power (moves with load),
        [8]=TDC limit, [9]=TDC value, [11]=hotspot temp, [63]=EDC limit.
        """
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table_version").write_bytes(struct.pack("<I", 0x00620205))

        floats = [0.0] * 420

        floats[2] = 225.0
        floats[3] = 142.0
        floats[8] = 190.0
        floats[9] = 95.0
        floats[11] = 70.0
        floats[63] = 230.0

        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.ppt_limit_w == pytest.approx(225.0)
        assert result.ppt_value_w == pytest.approx(142.0)
        assert result.tdc_limit_a == pytest.approx(190.0)
        assert result.tdc_value_a == pytest.approx(95.0)
        assert result.edc_limit_a == pytest.approx(230.0)
        assert result.edc_value_a is None
        assert result.tctl_c == pytest.approx(70.0)
        assert result.package_power_w == pytest.approx(142.0)

    def test_granite_ridge_reads_the_core_rail_request_and_measured_voltage(self, tmp_path):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table_version").write_bytes(struct.pack("<I", 0x00620205))
        floats = [0.0] * 420
        floats[48] = 1.296
        floats[49] = 1.291
        (smu_dir / "pm_table").write_bytes(struct.pack(f"<{len(floats)}f", *floats))

        result = PMTableReader(sysfs_path=smu_dir).read()

        assert result is not None
        assert (result.vdd_vid_v, result.vdd_v) == (pytest.approx(1.296), pytest.approx(1.291))
        assert rail_volts(result) == (
            ("pm_table:vdd", pytest.approx(1.291)),
            ("pm_table:vdd_vid", pytest.approx(1.296)),
        )

    @pytest.mark.parametrize("vid", [0.0, 0.1, 3.5, float("nan")])
    def test_a_core_rail_value_outside_physical_bounds_is_stored_as_unread(self, vid):
        data = PMTableData(vdd_vid_v=vid, vdd_v=1.2, pm_table_version=0x00620205)
        assert dict(rail_volts(data)) == {"pm_table:vdd": pytest.approx(1.2), "pm_table:vdd_vid": None}

    def test_a_table_other_than_zen_5_names_no_core_rail_source(self):
        assert rail_volts(PMTableData(vdd_vid_v=1.3, vdd_v=1.2, pm_table_version=0x00540208)) == ()

    @pytest.mark.req("L25.d.power")
    @pytest.mark.parametrize(("watts", "read"), [(142.5, 142.5), (0.0, None), (1500.0, None), (float("nan"), None)])
    def test_the_package_power_is_read_on_zen_5_and_an_impossible_value_is_unread(self, watts, read):
        data = PMTableData(package_power_w=watts, pm_table_version=0x00620205)
        assert package_watts(data) == (("pm_table:package", read),)
        assert package_watts(PMTableData(package_power_w=watts, pm_table_version=0x00540208)) == ()

    @pytest.mark.req("O19.record")
    def test_each_core_reads_its_clock_effective_clock_c0_and_voltage_from_the_measured_table(self):
        floats = [0.0] * 613
        for offset, value in ((0x574, 5.438), (0x5B4, 5.459), (0x5F4, 99.87), (0x4F4, 1.149)):
            floats[offset // 4 + 12] = value
        floats[0x4F4 // 4 + 3], floats[0x574 // 4 + 4], floats[0x5F4 // 4 + 5] = 2.5, float("nan"), 100.5
        reads = core_reads(PMTableData(pm_table_version=0x00620205, raw_floats=floats), MEASURED_CORE_IDS)
        assert [read[0] for read in reads] == list(range(16))
        assert reads[12] == (
            12,
            pytest.approx(5438.0),
            pytest.approx(5459.0),
            pytest.approx(99.87),
            pytest.approx(1.149),
        )
        assert reads[0] == (0, 0.0, 0.0, 0.0, None)
        assert (reads[3][4], reads[4][1], reads[5][3]) == (None, None, None)

    @pytest.mark.req("O19.record")
    @pytest.mark.parametrize(("version", "count"), [(0x00620105, 613), (0x00540208, 613), (0x00620205, 396)])
    def test_a_table_whose_core_layout_was_not_measured_records_no_core(self, version, count):
        assert core_reads(PMTableData(pm_table_version=version, raw_floats=[1.0] * count), MEASURED_CORE_IDS) == ()

    @pytest.mark.req("O19.record")
    def test_a_table_just_long_enough_for_the_last_core_reads_every_core(self):
        reads = core_reads(PMTableData(pm_table_version=0x00620205, raw_floats=[1.0] * 397), MEASURED_CORE_IDS)
        assert [read[0] for read in reads] == list(range(16))

    @pytest.mark.req("O19.stretch")
    @pytest.mark.parametrize("core_ids", [(*range(6), *range(8, 14)), range(8), (), range(17)])
    def test_a_machine_whose_core_ids_are_not_the_measured_0_to_15_reads_no_core(self, core_ids):
        assert core_reads(PMTableData(pm_table_version=0x00620205, raw_floats=[1.0] * 397), core_ids) == ()

    def test_fewer_than_200_floats_skips_parsing(self, tmp_path):
        """PM table with < 200 floats should skip Granite Ridge parsing."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "pm_table_version").write_bytes(struct.pack("<I", 0x00620205))

        floats = [1.0] * 50
        raw = struct.pack(f"<{len(floats)}f", *floats)
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert len(result.raw_floats) == 50
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

    def test_os_error_reading(self, tmp_path):
        """OSError on read should return None."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        pm_table = smu_dir / "pm_table"
        pm_table.write_bytes(b"\x00" * 100)
        pm_table.chmod(0o000)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()
        assert result is None

        pm_table.chmod(0o644)

    def test_non_aligned_data(self, tmp_path):
        """Data not aligned to 4 bytes should still parse what it can."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()

        floats = [1.0] * 201
        raw = struct.pack(f"<{len(floats)}f", *floats) + b"\xaa\xbb"
        (smu_dir / "pm_table").write_bytes(raw)

        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert len(result.raw_floats) == 201


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
        (smu_dir / "pm_table").write_bytes(struct.pack(f"<{num_floats}f", *([0.0] * num_floats)))

    if version_int is not None:
        (smu_dir / "pm_table_version").write_bytes(struct.pack("<I", version_int))

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
    """A zeroed table of a known version's size with each given value at that version's offset."""
    offsets = PM_TABLE_OFFSETS[version]
    raw = bytearray(offsets.table_size)
    for offset, value in (
        (offsets.fclk, fclk),
        (offsets.uclk, uclk),
        (offsets.mclk, mclk),
        (offsets.vddcr_soc, vddcr_soc),
    ):
        if value:
            struct.pack_into("<f", raw, offset, value)
    if vdd_mem:
        if offsets.vdd_mem is None:
            raise ValueError(f"version {version:#x} has no vdd_mem offset to write {vdd_mem} at")
        struct.pack_into("<f", raw, offsets.vdd_mem, vdd_mem)

    return bytes(raw)


class TestVddqOffset:
    @pytest.mark.parametrize("version", [0x00620205, 0x00621102, 0x00621202, 0x00620105, 0x00620999])
    def test_vddq_is_read_at_0xe8_on_every_zen_5_table(self, tmp_path, version):
        raw = bytearray(0x994)
        struct.pack_into("<f", raw, 0xE8, 1.1)
        smu_dir = _make_smu_dir(tmp_path, version_int=version, raw_bytes=bytes(raw))
        assert PMTableReader(sysfs_path=smu_dir).read().vddq_v == pytest.approx(1.1)


class TestPMTableOffsets:
    def test_frozen_dataclass_with_slots(self):
        """PMTableOffsets is a frozen dataclass with slots."""
        offsets = PMTableOffsets(
            table_size=0x994,
            fclk=0x11C,
            uclk=0x12C,
            mclk=0x13C,
            vddcr_soc=0x14C,
            vddq=0xE8,
            vdd_mem=0x0A8,
        )
        assert offsets.fclk == 0x11C
        assert offsets.uclk == 0x12C
        assert offsets.mclk == 0x13C
        with pytest.raises(AttributeError):
            offsets.fclk = 0x200
        assert hasattr(offsets, "__slots__")

    def test_known_version_0x620205_exists(self):
        """PM_TABLE_OFFSETS[0x620205] exists with correct clock offsets."""
        offsets = PM_TABLE_OFFSETS[0x620205]
        assert offsets.fclk == 0x11C
        assert offsets.uclk == 0x12C
        assert offsets.mclk == 0x13C
        assert offsets.vddcr_soc == 0x14C
        assert offsets.vdd_mem == 0x0A8

    def test_known_version_0x621102_exists(self):
        """PM_TABLE_OFFSETS[0x621102] exists (Zen 5 variant)."""
        offsets = PM_TABLE_OFFSETS[0x621102]
        assert offsets.fclk == 0x11C
        assert offsets.uclk == 0x12C
        assert offsets.mclk == 0x13C
        assert offsets.vdd_mem is None

    def test_known_version_0x540104_exists(self):
        """7700X (Zen 4) version 0x540104 exists with community offsets, unverified."""
        offsets = PM_TABLE_OFFSETS[0x540104]
        assert offsets.table_size == 0x6A8
        assert offsets.fclk == 0x118
        assert offsets.uclk == 0x128
        assert offsets.mclk == 0x138
        assert offsets.vddcr_soc == 0xD0
        assert offsets.vddq == 0xE0
        assert offsets.vdd_mem is None
        assert offsets.verified is False

    def test_verified_defaults_false(self):
        """An entry that omits ``verified`` is unverified (fail-closed default)."""
        offsets = PMTableOffsets(
            table_size=0x6A8,
            fclk=0x118,
            uclk=0x128,
            mclk=0x138,
            vddcr_soc=0xD0,
            vddq=0xE0,
            vdd_mem=None,
        )
        assert offsets.verified is False

    def test_zen5_baseline_is_verified(self):
        """The empirically-confirmed table version 0x620205 is marked verified."""
        assert PM_TABLE_OFFSETS[0x620205].verified is True


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

    def test_is_verified_defaults_false(self):
        """PMTableData.offsets_verified is False until a verified version sets it."""
        assert PMTableData().offsets_verified is False


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
        assert result.offsets_verified is True

    def test_zen4_7700x_dispatch_unverified(self, tmp_path):
        """0x540104 with plausible values calibrates but is not verified."""
        raw = _build_versioned_pm_table(
            0x540104,
            fclk=2000.0,
            uclk=2400.0,
            mclk=2400.0,
            vddcr_soc=1.10,
        )
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00540104, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.offsets_verified is False
        assert result.fclk_mhz == pytest.approx(2000.0)
        assert result.uclk_mhz == pytest.approx(2400.0)
        assert result.mclk_mhz == pytest.approx(2400.0)
        assert result.vddcr_soc_v == pytest.approx(1.10)

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan"), 1e30, 50.0])
    def test_implausible_clock_fails_closed(self, tmp_path, bad):
        """A garbage FCLK (wrong offset) downgrades the table to uncalibrated."""
        raw = _build_versioned_pm_table(0x540104, fclk=bad, uclk=2400.0, mclk=2400.0, vddcr_soc=1.10)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00540104, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()

        assert result is not None
        assert result.is_calibrated is False
        assert result.offsets_verified is False
        assert result.fclk_mhz == 0.0

    def test_implausible_voltage_fails_closed(self, tmp_path):
        """A garbage VDDCR_SOC also fails closed."""
        raw = _build_versioned_pm_table(0x540104, fclk=2000.0, uclk=2400.0, mclk=2400.0, vddcr_soc=9.99e9)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00540104, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()
        assert result.is_calibrated is False

    def test_verified_version_also_gated(self, tmp_path):
        """The plausibility gate protects verified entries too (defense in depth)."""
        raw = _build_versioned_pm_table(0x620205, fclk=1e30, uclk=3000.0, mclk=3000.0, vddcr_soc=1.25)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00620205, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()
        assert result.is_calibrated is False

    def test_all_zero_stays_calibrated(self, tmp_path):
        """All-zero memory fields (absent) remain calibrated — zero is plausible."""
        raw = _build_versioned_pm_table(0x540104)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00540104, raw_bytes=raw)
        result = PMTableReader(sysfs_path=smu_dir).read()
        assert result.is_calibrated is True
        assert result.fclk_mhz == 0.0

    def test_unknown_version_uncalibrated(self, tmp_path):
        """read() with unknown version produces is_calibrated=False."""
        floats = [0.0] * 300
        raw = struct.pack(f"<{len(floats)}f", *floats)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x99999999, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is False
        assert result.pm_table_version == 0x99999999
        assert len(result.raw_floats) > 0

    def test_no_version_file_parses_no_power_header(self, tmp_path):
        """Without a version the power header is not parsed: its layout is version-family-specific, and labeling
        unknown bytes as PPT/TDC/EDC is the mislabeling this guards against."""
        floats = [0.0] * 420
        floats[2] = 225.0
        raw = struct.pack(f"<{len(floats)}f", *floats)
        smu_dir = _make_smu_dir(tmp_path, version_int=None, raw_bytes=raw)
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.ppt_limit_w == 0.0
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
        assert 0x0062FFFF not in PM_TABLE_OFFSETS
        raw = bytearray(0x994)
        struct.pack_into("<f", raw, 0x11C, 1900.0)
        struct.pack_into("<f", raw, 0x12C, 1900.0)
        struct.pack_into("<f", raw, 0x13C, 1900.0)
        struct.pack_into("<f", raw, 0x14C, 1.1)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x0062FFFF, raw_bytes=bytes(raw))
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.fclk_mhz == pytest.approx(1900.0)

    def test_a_version_without_a_vdd_mem_offset_leaves_vdd_mem_unread(self, tmp_path):
        raw = bytearray(_build_versioned_pm_table(0x621102, fclk=2000.0, uclk=3000.0, mclk=3000.0, vddcr_soc=1.25))
        struct.pack_into("<f", raw, PM_TABLE_OFFSETS[0x620205].vdd_mem, 1.4)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00621102, raw_bytes=bytes(raw))
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.vdd_mem_v == 0.0

    def test_out_of_range_offset_returns_zero(self, tmp_path):
        """Out-of-range offset (beyond raw bytes) returns 0.0, no crash."""
        small_raw = bytearray(256)
        struct.pack_into("<f", small_raw, 0x11C % 256, 2000.0)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00620205, raw_bytes=bytes(small_raw))
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert (result.fclk_mhz, result.uclk_mhz, result.mclk_mhz, result.vddcr_soc_v) == (0.0, 0.0, 0.0, 0.0)

    def test_power_header_is_parsed_with_a_known_version(self, tmp_path):
        raw = bytearray(
            _build_versioned_pm_table(
                0x620205,
                fclk=2000.0,
                uclk=3000.0,
                mclk=3000.0,
                vddcr_soc=1.25,
                vdd_mem=1.395,
            )
        )
        struct.pack_into("<f", raw, 8, 225.0)
        smu_dir = _make_smu_dir(tmp_path, version_int=0x00620205, raw_bytes=bytes(raw))
        reader = PMTableReader(sysfs_path=smu_dir)
        result = reader.read()

        assert result is not None
        assert result.is_calibrated is True
        assert result.fclk_mhz == pytest.approx(2000.0)

        assert result.ppt_limit_w == pytest.approx(225.0)


class TestComputeFclkUclkRatio:
    def test_ratio_1_1(self):
        """FCLK=UCLK produces (1, 1) ratio."""
        assert compute_fclk_uclk_ratio(2000.0, 2000.0) == (1, 1)

    def test_the_clocks_are_rounded_to_100_mhz_before_the_ratio(self):
        assert compute_fclk_uclk_ratio(2150.0, 4300.0) == (22, 43)

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


class TestReadPowerLimits:
    def _tree(self, tmp_path, version: int | None, floats: list[float]):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        if version is not None:
            (smu_dir / "pm_table_version").write_bytes(struct.pack("<I", version))
        (smu_dir / "pm_table").write_bytes(struct.pack(f"<{len(floats)}f", *floats))
        return smu_dir

    def _floats(self, ppt=225.0, tdc=190.0, edc=230.0) -> list[float]:
        floats = [0.0] * 420
        floats[2] = ppt
        floats[8] = tdc
        floats[63] = edc
        return floats

    def test_reads_zen5_limits(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00620205, self._floats())
        assert read_power_limits(smu_dir) == (225.0, 190.0, 230.0)

    def test_unknown_generation_fails_closed(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00540104, self._floats())
        assert read_power_limits(smu_dir) == (None, None, None)

    def test_implausible_values_fail_closed_per_field(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00620205, self._floats(ppt=1e9, tdc=190.0, edc=5.0))
        assert read_power_limits(smu_dir) == (None, 190.0, None)

    def test_zero_reads_as_absent(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        smu_dir = self._tree(tmp_path, 0x00620205, self._floats(ppt=0.0, tdc=0.0, edc=0.0))
        assert read_power_limits(smu_dir) == (None, None, None)

    def test_missing_table_fails_closed(self, tmp_path):
        from corecycler.smu.pmtable import read_power_limits

        assert read_power_limits(tmp_path / "nope") == (None, None, None)


@pytest.mark.req("O19.stretch")
@pytest.mark.parametrize(
    ("clock", "effective", "c0", "stretch"),
    [
        (5000.0, 4900.0, 100.0, 2.0),
        (5000.0, 4655.0, 95.0, 2.0),
        (5000.0, 4655.0, 94.9, None),
        (5000.0, 5100.0, 100.0, 0.0),
        (None, 4900.0, 100.0, None),
        (5000.0, None, 100.0, None),
        (5000.0, 4900.0, None, None),
        (0.0, 4900.0, 100.0, None),
        (5000.0, 0.0, 100.0, None),
    ],
)
def test_stretch_is_the_effective_clock_below_the_clock_for_a_core_at_least_95_percent_in_c0(
    clock, effective, c0, stretch
):
    from corecycler.smu.pmtable import stretch_pct

    assert stretch_pct(clock, effective, c0) == (None if stretch is None else pytest.approx(stretch))
