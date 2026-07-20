"""PM (Power Monitoring) table reader for live telemetry."""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


# ===========================================================================
# Version-aware PM table offset registry
# ===========================================================================


@dataclass(frozen=True, slots=True)
class PMTableOffsets:
    """Named byte offsets for a specific PM table version.

    Offsets are in bytes (not float indices). A value of -1 means the
    field is not available for this version. ``verified`` is True only for
    offsets confirmed against real silicon; it defaults to False so an
    unconfirmed entry fails closed (surfaced as community-sourced, never as
    Verified).
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
    verified: bool = False  # True only if confirmed on real hardware


# Exact version match only. No Zen 4 prefix fallback: version 0x540208 uses a
# +4-byte-shifted layout, so guessing by the 0x54 family would misread every
# field. Source: ZenStates-Core PowerTable.cs. ``verified`` marks offsets
# confirmed on real silicon; an unverified entry still parses but is gated by
# the runtime plausibility check in PMTableReader.read() and never labelled
# "Verified".
# VDD_MEM at 0x0A8; VDDQ at 0x0E8 (per-channel pair at 0x0E8/0x0EC).
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
        vdd_mem=0x0A8,
        verified=True,
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
        verified=True,
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
        vdd_mem=0x0A8,
        verified=True,
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
        verified=True,
    ),
    # Zen 4 (Raphael, e.g. 7700X). Offsets from ZenStates-Core PowerTable.cs,
    # whose Zen4 block is commented "offsets are not verified yet" — a single
    # source with no independent corroboration (ryzen_smu / ryzen_monitor /
    # RyzenAdj do not implement Raphael desktop). Shipped unverified; read()'s
    # plausibility gate fails closed if these offsets decode nonsensical values.
    0x540104: PMTableOffsets(
        table_size=0x6A8,
        fclk=0x118,
        uclk=0x128,
        mclk=0x138,
        vddcr_soc=0xD0,
        cldo_vddp=0x430,
        cldo_vddg_iod=-1,
        cldo_vddg_ccd=-1,
        vdd_misc=0xE0,
        vdd_mem=-1,
        verified=False,
    ),
}

# Zen 5 version prefix for fallback matching (0x62xxxx family).
_ZEN5_PREFIX = 0x62

# Generic Zen 5 offsets used when exact version is not in PM_TABLE_OFFSETS
# but the version prefix matches Zen 5. A family guess, so verified=False.
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
    verified=False,
)

# Generous physical bounds for the runtime plausibility gate. A field is
# accepted when it is zero (absent for this version/state) or finite and within
# range; anything else means the offset map decoded non-telemetry bytes.
_CLOCK_MIN_MHZ = 200.0
_CLOCK_MAX_MHZ = 6000.0
_VOLT_MIN_V = 0.3
_VOLT_MAX_V = 2.0


def _plausible(value: float, lo: float, hi: float) -> bool:
    """True if value is exactly 0.0 (absent) or finite and within [lo, hi]."""
    if value == 0.0:
        return True
    return math.isfinite(value) and lo <= value <= hi


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
    is_verified: bool = False  # offsets confirmed on real silicon (not just sourced)

    raw_floats: list[float] = field(default_factory=list)


# Physical bounds for PBO limit plausibility (fail closed to None on garbage).
_PPT_MIN_W, _PPT_MAX_W = 30.0, 1000.0
_AMP_MIN_A, _AMP_MAX_A = 30.0, 2000.0


def read_power_limits(
    num_cores: int = 16, sysfs_path: Path | None = None
) -> tuple[float | None, float | None, float | None]:
    """Read the live PBO power limits (PPT W, TDC A, EDC A) from the PM table.

    Fails closed: unsupported generation, unreadable table, or an implausible
    decoded value yields None for that field — a wrong number stored as a
    limit would poison every context comparison built on it.
    """
    reader = (
        PMTableReader(num_cores, sysfs_path) if sysfs_path is not None
        else PMTableReader(num_cores)
    )
    data = reader.read()
    if data is None:
        return None, None, None

    def _gate(value: float, lo: float, hi: float) -> float | None:
        if not math.isfinite(value) or not lo <= value <= hi:
            return None
        return value

    return (
        _gate(data.ppt_limit_w, _PPT_MIN_W, _PPT_MAX_W),
        _gate(data.tdc_limit_a, _AMP_MIN_A, _AMP_MAX_A),
        _gate(data.edc_limit_a, _AMP_MIN_A, _AMP_MAX_A),
    )


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
    Returns None if values are zero/negative or non-finite (NaN/inf would crash
    round()).
    """
    if not (math.isfinite(fclk_mhz) and math.isfinite(uclk_mhz)):
        return None
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
                if self._memory_values_plausible(data):
                    data.is_calibrated = True
                    data.is_verified = offsets.verified
                else:
                    # Fail closed: a wrong offset map decoded garbage. Don't
                    # present it as calibrated; blank the suspect fields so the
                    # GUI shows "--" rather than nonsense.
                    log.warning(
                        "PM table v%#010x decoded implausible memory values "
                        "(fclk=%.1f uclk=%.1f mclk=%.1f vddcr_soc=%.3f) — "
                        "treating as uncalibrated",
                        version,
                        data.fclk_mhz,
                        data.uclk_mhz,
                        data.mclk_mhz,
                        data.vddcr_soc_v,
                    )
                    self._blank_memory_values(data)
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

    @staticmethod
    def _memory_values_plausible(data: PMTableData) -> bool:
        """Sanity-gate decoded memory-controller values (fail closed on garbage).

        A wrong offset map decodes arbitrary bytes as floats, typically NaN/inf
        or absurd magnitudes. Accept a field only if it is zero (absent) or
        finite and within generous physical bounds. This protects every version
        and lets unverified community offsets ship safely: if they are wrong,
        the values are implausible and the table is downgraded to uncalibrated.
        """
        for mhz in (data.fclk_mhz, data.uclk_mhz, data.mclk_mhz):
            if not _plausible(mhz, _CLOCK_MIN_MHZ, _CLOCK_MAX_MHZ):
                return False
        for volt in (data.vddcr_soc_v, data.vdd_mem_v, data.vddq_v):
            if not _plausible(volt, _VOLT_MIN_V, _VOLT_MAX_V):
                return False
        return True

    @staticmethod
    def _blank_memory_values(data: PMTableData) -> None:
        """Reset memory-controller fields and clear calibration (fail-closed)."""
        data.fclk_mhz = 0.0
        data.uclk_mhz = 0.0
        data.mclk_mhz = 0.0
        data.vddcr_soc_v = 0.0
        data.vdd_mem_v = 0.0
        data.vddq_v = 0.0
        data.is_calibrated = False
        data.is_verified = False

    def _parse_granite_ridge(self, data: PMTableData, floats: list[float]) -> None:
        """Parse PM table core arrays plus the Zen 5 power/thermal header.

        Header layout (0x62xxxx family only): no open-source project publishes
        an authoritative label map — ZenStates-Core's PowerTable.cs carries NO
        power fields for any generation — so these indices are evidence-based:
        [0..1]=0, [2]=PPT limit W (static), [3]=package power W (moves with
        load), [4..7]=0, [8]=TDC limit A, [9]=TDC current A (moves with load),
        [10]=thermal throttle limit C (static), [11]=hotspot temperature C
        (moves), [63]=EDC limit A.
        """
        if len(floats) < 200:
            return

        try:
            if (data.pm_table_version >> 16) & 0xFF == _ZEN5_PREFIX:
                data.ppt_limit_w = floats[2]
                data.ppt_value_w = floats[3]
                data.tdc_limit_a = floats[8]
                data.tdc_value_a = floats[9]
                data.edc_limit_a = floats[63] if len(floats) > 63 else 0.0
                data.edc_value_a = 0.0  # value slot not located — absent, not a guess
                data.tctl_c = floats[11] if len(floats) > 11 else 0.0
                data.tdie_c = 0.0  # not located — absent, not a guess
                # PPT value IS the package power the limit governs.
                data.package_power_w = floats[3]
                data.soc_power_w = 0.0  # not located — absent, not a guess

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
