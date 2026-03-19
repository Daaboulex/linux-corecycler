"""PM (Power Monitoring) table reader for live telemetry."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


# ===========================================================================
# Version-aware PM table offset registry
# ===========================================================================


@dataclass(frozen=True, slots=True)
class PMTableOffsets:
    """Named byte offsets for a specific PM table version.

    Offsets are in bytes (not float indices). A value of -1 means the
    field is not available for this version.
    """

    table_size: int
    fclk: int  # byte offset
    uclk: int
    mclk: int
    vddcr_soc: int
    cldo_vddp: int
    cldo_vddg_iod: int  # -1 if not available
    cldo_vddg_ccd: int  # -1 if not available
    vdd_misc: int
    vdd_mem: int  # -1 if not calibrated


# Exact version match first, then prefix fallback.
# Source: ZenStates-Core PowerTable.cs + empirical verification on 9950X3D.
PM_TABLE_OFFSETS: dict[int, PMTableOffsets] = {
    0x620205: PMTableOffsets(
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
    ),
    0x621102: PMTableOffsets(
        table_size=0x724,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        cldo_vddp=0x434,
        cldo_vddg_iod=0x40C,
        cldo_vddg_ccd=0x414,
        vdd_misc=0xE8,
        vdd_mem=-1,
    ),
    0x621202: PMTableOffsets(
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
    ),
    0x620105: PMTableOffsets(
        table_size=0x724,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        cldo_vddp=0x434,
        cldo_vddg_iod=0x40C,
        cldo_vddg_ccd=0x414,
        vdd_misc=0xE8,
        vdd_mem=-1,
    ),
}

# Zen 5 version prefix for fallback matching (0x62xxxx family).
_ZEN5_PREFIX = 0x62

# Generic Zen 5 offsets used when exact version is not in PM_TABLE_OFFSETS
# but the version prefix matches Zen 5. Conservative: vdd_mem=-1.
_ZEN5_GENERIC = PMTableOffsets(
    table_size=0x994,
    fclk=0x11C,
    uclk=0x12C,
    mclk=0x13C,
    vddcr_soc=0x14C,
    cldo_vddp=0x434,
    cldo_vddg_iod=0x40C,
    cldo_vddg_ccd=0x414,
    vdd_misc=0xE8,
    vdd_mem=-1,
)


def _find_prefix_offsets(version: int) -> PMTableOffsets | None:
    """Find offset map by version prefix family.

    For Zen 5 (0x62xxxx), clock offsets are consistent across all known
    versions, so prefix fallback is safe for clocks. Voltage offsets
    (especially vdd_mem) are conservative (-1).
    """
    prefix = (version >> 16) & 0xFF
    if prefix == _ZEN5_PREFIX:
        return _ZEN5_GENERIC
    return None


def _read_float(raw: bytes, byte_offset: int) -> float:
    """Read a single little-endian float from raw bytes at given byte offset.

    Returns 0.0 if offset is negative or out of range.
    """
    if byte_offset < 0 or byte_offset + 4 > len(raw):
        return 0.0
    return struct.unpack_from("<f", raw, byte_offset)[0]


# ===========================================================================
# PM table data
# ===========================================================================


@dataclass(slots=True)
class PMTableData:
    """Parsed PM table telemetry values."""

    # per-core data
    core_frequency_mhz: dict[int, float] = field(default_factory=dict)
    core_voltage_v: dict[int, float] = field(default_factory=dict)
    core_temperature_c: dict[int, float] = field(default_factory=dict)
    core_power_w: dict[int, float] = field(default_factory=dict)
    core_c0_residency: dict[int, float] = field(default_factory=dict)

    # package-level
    package_power_w: float = 0.0
    soc_power_w: float = 0.0
    ppt_limit_w: float = 0.0
    tdc_limit_a: float = 0.0
    edc_limit_a: float = 0.0
    ppt_value_w: float = 0.0
    tdc_value_a: float = 0.0
    edc_value_a: float = 0.0
    tctl_c: float = 0.0
    tdie_c: float = 0.0

    # memory controller clocks and voltages (version-aware parsing)
    fclk_mhz: float = 0.0
    uclk_mhz: float = 0.0
    mclk_mhz: float = 0.0
    vddcr_soc_v: float = 0.0
    vdd_mem_v: float = 0.0
    vddq_v: float = 0.0
    pm_table_version: int = 0
    is_calibrated: bool = False

    raw_floats: list[float] = field(default_factory=list)


# ===========================================================================
# FCLK:UCLK ratio computation
# ===========================================================================


def compute_fclk_uclk_ratio(
    fclk_mhz: float, uclk_mhz: float
) -> tuple[int, int] | None:
    """Compute FCLK:UCLK ratio as a simplified integer pair.

    Common AMD DDR5 ratios:
    - 1:1 — FCLK=UCLK (coupled, optimal latency)
    - 2:3 — FCLK=2000, UCLK=3000 (DDR5-6000 with FCLK capped at ~2000)
    - 1:2 — FCLK=UCLK/2 (decoupled)
    Returns None only if values are zero/negative.
    """
    if fclk_mhz <= 0 or uclk_mhz <= 0:
        return None
    from math import gcd

    # Round to nearest 100 MHz to handle measurement noise
    f = round(fclk_mhz / 100)
    u = round(uclk_mhz / 100)
    if f <= 0 or u <= 0:
        return None
    g = gcd(f, u)
    return (f // g, u // g)


# ===========================================================================
# PM table reader
# ===========================================================================


class PMTableReader:
    """Reads the SMU PM table for live telemetry data.

    Supports version-aware dispatch: reads pm_table_version from sysfs and
    selects the correct offset map for known CPU generations. Falls back to
    legacy approximate parsing for unknown versions.
    """

    def __init__(self, num_cores: int = 16, sysfs_path: Path = SYSFS_BASE) -> None:
        self.num_cores = num_cores
        self.sysfs = sysfs_path

    def is_available(self) -> bool:
        pm_path = self.sysfs / "pm_table"
        return pm_path.exists()

    def read(self) -> PMTableData | None:
        """Read and parse the PM table. Returns None if unavailable."""
        pm_path = self.sysfs / "pm_table"
        if not pm_path.exists():
            return None

        try:
            raw = pm_path.read_bytes()
        except OSError:
            return None

        if len(raw) < 4:
            return None

        # PM table is an array of 32-bit floats
        num_floats = len(raw) // 4
        floats = list(struct.unpack(f"<{num_floats}f", raw[: num_floats * 4]))

        data = PMTableData(raw_floats=floats)

        # Try version-aware dispatch
        version = self._read_pm_table_version()
        if version is not None:
            data.pm_table_version = version
            offsets = PM_TABLE_OFFSETS.get(version)
            if offsets is None:
                offsets = _find_prefix_offsets(version)
            if offsets is not None:
                self._parse_versioned(data, raw, offsets)
                data.is_calibrated = True
            # else: unknown version, is_calibrated stays False

        # Always parse legacy core-level data (per-core freq/voltage/temp/power)
        self._parse_granite_ridge(data, floats)

        return data

    def _read_pm_table_version(self) -> int | None:
        """Read PM table version from sysfs as uint32.

        Returns None if the version file does not exist or cannot be read.
        """
        version_path = self.sysfs / "pm_table_version"
        try:
            raw = version_path.read_bytes()
            if len(raw) >= 4:
                return struct.unpack("<I", raw[:4])[0]
        except OSError:
            return None
        return None

    def _parse_versioned(
        self, data: PMTableData, raw: bytes, offsets: PMTableOffsets
    ) -> None:
        """Parse memory controller clocks and voltages using version-specific offsets."""
        data.fclk_mhz = _read_float(raw, offsets.fclk)
        data.uclk_mhz = _read_float(raw, offsets.uclk)
        data.mclk_mhz = _read_float(raw, offsets.mclk)
        data.vddcr_soc_v = _read_float(raw, offsets.vddcr_soc)
        if offsets.vdd_mem >= 0:
            data.vdd_mem_v = _read_float(raw, offsets.vdd_mem)
        if offsets.vdd_misc >= 0:
            data.vddq_v = _read_float(raw, offsets.vdd_misc)

    def _parse_granite_ridge(self, data: PMTableData, floats: list[float]) -> None:
        """Parse PM table with Granite Ridge (Zen 5) approximate offsets."""
        if len(floats) < 200:
            return

        # package telemetry (approximate offsets)
        try:
            data.ppt_limit_w = floats[0]
            data.ppt_value_w = floats[1]
            data.tdc_limit_a = floats[2]
            data.tdc_value_a = floats[3]
            data.edc_limit_a = floats[4]
            data.edc_value_a = floats[5]

            data.tctl_c = floats[10] if len(floats) > 10 else 0.0
            data.tdie_c = floats[11] if len(floats) > 11 else 0.0

            data.package_power_w = floats[26] if len(floats) > 26 else 0.0
            data.soc_power_w = floats[28] if len(floats) > 28 else 0.0

            # per-core data typically starts around offset 100+
            # each core has ~10 float fields (freq, voltage, power, temp, residency, ...)
            core_base = 100
            core_stride = 10

            for core in range(min(self.num_cores, 32)):
                offset = core_base + core * core_stride
                if offset + core_stride > len(floats):
                    break
                data.core_frequency_mhz[core] = floats[offset]
                data.core_voltage_v[core] = floats[offset + 1]
                data.core_power_w[core] = floats[offset + 2]
                data.core_temperature_c[core] = floats[offset + 3]
                data.core_c0_residency[core] = floats[offset + 4]

        except IndexError:
            pass  # PM table smaller than expected, partial data is fine
