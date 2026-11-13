"""PM (Power Monitoring) table reader for live telemetry."""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection

    from corecycler.monitor.hwmon import CoreRead

log = logging.getLogger(__name__)

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


@dataclass(frozen=True, slots=True)
class PMTableOffsets:
    """Named byte offsets for a specific PM table version.

    Offsets are in bytes (not float indices). vdd_mem is None where the
    version has no such field. ``verified`` is True only for
    offsets confirmed against real silicon; it defaults to False so an
    unconfirmed entry fails closed (surfaced as community-sourced, never as
    Verified).
    """

    table_size: int
    fclk: int
    uclk: int
    mclk: int
    vddcr_soc: int
    vddq: int
    vdd_mem: int | None
    verified: bool = False


# ZenStates-Core PowerTable.cs offsets. No 0x54 prefix fallback, 0x540208 shifts every field by 4 bytes.
PM_TABLE_OFFSETS: dict[int, PMTableOffsets] = {
    0x620205: PMTableOffsets(
        table_size=0x994,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        vddq=0xE8,
        vdd_mem=0x0A8,
        verified=True,
    ),
    0x621102: PMTableOffsets(
        table_size=0x724,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        vddq=0xE8,
        vdd_mem=None,
        verified=True,
    ),
    0x621202: PMTableOffsets(
        table_size=0x994,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        vddq=0xE8,
        vdd_mem=0x0A8,
        verified=True,
    ),
    0x620105: PMTableOffsets(
        table_size=0x724,
        fclk=0x11C,
        uclk=0x12C,
        mclk=0x13C,
        vddcr_soc=0x14C,
        vddq=0xE8,
        vdd_mem=None,
        verified=True,
    ),
    # Zen 4 offsets from ZenStates-Core PowerTable.cs, which marks them unverified.
    0x540104: PMTableOffsets(
        table_size=0x6A8,
        fclk=0x118,
        uclk=0x128,
        mclk=0x138,
        vddcr_soc=0xD0,
        vddq=0xE0,
        vdd_mem=None,
        verified=False,
    ),
}

_ZEN5_PREFIX = 0x62


def _version_prefix(version: int) -> int:
    return (version >> 16) & 0xFF


_ZEN5_GENERIC = PMTableOffsets(
    table_size=0x994,
    fclk=0x11C,
    uclk=0x12C,
    mclk=0x13C,
    vddcr_soc=0x14C,
    vddq=0xE8,
    vdd_mem=None,
    verified=False,
)

_RATIO_ROUND_MHZ = 100
_CLOCK_MIN_MHZ = 200.0
_CLOCK_MAX_MHZ = 6000.0
_VOLT_MIN_V = 0.3
_VOLT_MAX_V = 2.0
_WATT_MAX_W = 1000.0


def _plausible(value: float, lo: float, hi: float) -> bool:
    """True if value is exactly 0.0 (absent) or finite and within [lo, hi]."""
    if value == 0.0:
        return True
    return math.isfinite(value) and lo <= value <= hi


def package_watts(data: PMTableData) -> tuple[tuple[str, float | None], ...]:
    if _version_prefix(data.pm_table_version) != _ZEN5_PREFIX:
        return ()
    value = data.package_power_w
    return (("pm_table:package", value if value != 0.0 and _plausible(value, 0.0, _WATT_MAX_W) else None),)


def rail_volts(data: PMTableData) -> tuple[tuple[str, float | None], ...]:
    if _version_prefix(data.pm_table_version) != _ZEN5_PREFIX:
        return ()
    readings = (("pm_table:vdd", data.vdd_v), ("pm_table:vdd_vid", data.vdd_vid_v))
    return tuple(
        (source, value if value != 0.0 and _plausible(value, _VOLT_MIN_V, _VOLT_MAX_V) else None)
        for source, value in readings
    )


CORE_TABLE_VERSION = 0x620205
CORE_COUNT = 16
MEASURED_CORE_IDS = frozenset(range(CORE_COUNT))
CORE_ARRAYS = (
    ("clock MHz", 349, 0.0, 6.5, 1000.0),
    ("effective MHz", 365, 0.0, 6.5, 1000.0),
    ("C0 %", 381, 0.0, 100.0, 1.0),
    ("volts", 317, _VOLT_MIN_V, _VOLT_MAX_V, 1.0),
)


LOADED_C0_PCT = 95.0


def stretch_pct(clock_mhz: float | None, effective_mhz: float | None, c0_pct: float | None) -> float | None:
    """How far a loaded core's effective clock falls below the clock it runs at, in percent, or None unless the
    core sat in C0 at LOADED_C0_PCT or more, where the hour probe of design/clock-stretch.md measured it."""
    if not clock_mhz or not effective_mhz or c0_pct is None or c0_pct < LOADED_C0_PCT:
        return None
    return max(0.0, 1.0 - effective_mhz / (clock_mhz * c0_pct / 100.0)) * 100.0


def core_reads(data: PMTableData, core_ids: Collection[int]) -> tuple[CoreRead, ...]:
    """Each core's clock, effective clock, C0 and voltage on table 0x620205 only, each array at its first float,
    and only on a machine whose core ids are 0 to 15, the layout whose index-to-core mapping was measured."""
    if set(core_ids) != MEASURED_CORE_IDS:
        return ()
    floats = data.raw_floats
    if data.pm_table_version != CORE_TABLE_VERSION or len(floats) < max(i for _, i, *_ in CORE_ARRAYS) + CORE_COUNT:
        return ()

    def read(first: int, lo: float, hi: float, scale: float, core: int) -> float | None:
        value = floats[first + core]
        return value * scale if math.isfinite(value) and lo <= value <= hi else None

    return tuple(
        (core, *(read(first, lo, hi, scale, core) for _, first, lo, hi, scale in CORE_ARRAYS))
        for core in range(CORE_COUNT)
    )


def _find_prefix_offsets(version: int) -> PMTableOffsets | None:
    """Find offset map by version prefix family.

    For Zen 5 (0x62xxxx), clock offsets are consistent across all known
    versions, so prefix fallback is safe for clocks; vdd_mem, which the
    versions do not share, is None.
    """
    prefix = _version_prefix(version)
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


@dataclass(slots=True)
class PMTableData:
    """Parsed PM table telemetry values."""

    package_power_w: float = 0.0
    ppt_limit_w: float = 0.0
    tdc_limit_a: float = 0.0
    edc_limit_a: float = 0.0
    ppt_value_w: float = 0.0
    tdc_value_a: float = 0.0
    edc_value_a: float | None = None
    tctl_c: float = 0.0

    fclk_mhz: float = 0.0
    uclk_mhz: float = 0.0
    mclk_mhz: float = 0.0
    vddcr_soc_v: float = 0.0
    vdd_mem_v: float = 0.0
    vddq_v: float = 0.0
    vdd_vid_v: float = 0.0
    vdd_v: float = 0.0
    pm_table_version: int = 0
    is_calibrated: bool = False
    offsets_verified: bool = False

    raw_floats: list[float] = field(default_factory=list)


_PPT_MIN_W, _PPT_MAX_W = 30.0, 1000.0
_AMP_MIN_A, _AMP_MAX_A = 30.0, 2000.0


def read_power_limits(sysfs_path: Path | None = None) -> tuple[float | None, float | None, float | None]:
    """Read the live PBO power limits (PPT W, TDC A, EDC A) from the PM table.

    Fails closed: unsupported generation, unreadable table, or an implausible
    decoded value yields None for that field — a wrong number stored as a
    limit would poison every context comparison built on it.
    """
    reader = PMTableReader(sysfs_path) if sysfs_path is not None else PMTableReader()
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


def compute_fclk_uclk_ratio(fclk_mhz: float, uclk_mhz: float) -> tuple[int, int] | None:
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

    f = round(fclk_mhz / _RATIO_ROUND_MHZ)
    u = round(uclk_mhz / _RATIO_ROUND_MHZ)
    if f <= 0 or u <= 0:
        return None
    g = gcd(f, u)
    return (f // g, u // g)


class PMTableReader:
    """Reads the SMU PM table for live telemetry data.

    Supports version-aware dispatch: reads pm_table_version from sysfs and
    selects the correct offset map for known CPU generations. A version
    outside every known family parses nothing but the raw floats.
    """

    def __init__(self, sysfs_path: Path = SYSFS_BASE) -> None:
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

        num_floats = len(raw) // 4
        floats = list(struct.unpack(f"<{num_floats}f", raw[: num_floats * 4]))

        data = PMTableData(raw_floats=floats)

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
                    data.offsets_verified = offsets.verified
                else:
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

    def _parse_versioned(self, data: PMTableData, raw: bytes, offsets: PMTableOffsets) -> None:
        """Parse memory controller clocks and voltages using version-specific offsets."""
        data.fclk_mhz = _read_float(raw, offsets.fclk)
        data.uclk_mhz = _read_float(raw, offsets.uclk)
        data.mclk_mhz = _read_float(raw, offsets.mclk)
        data.vddcr_soc_v = _read_float(raw, offsets.vddcr_soc)
        if offsets.vdd_mem is not None:
            data.vdd_mem_v = _read_float(raw, offsets.vdd_mem)
        data.vddq_v = _read_float(raw, offsets.vddq)

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
        data.offsets_verified = False

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

        if _version_prefix(data.pm_table_version) == _ZEN5_PREFIX:
            data.ppt_limit_w = floats[2]
            data.ppt_value_w = floats[3]
            data.tdc_limit_a = floats[8]
            data.tdc_value_a = floats[9]
            data.edc_limit_a = floats[63]
            data.tctl_c = floats[11]
            data.package_power_w = data.ppt_value_w
            data.vdd_vid_v = floats[48]
            data.vdd_v = floats[49]
