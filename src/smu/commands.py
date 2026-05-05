"""SMU command IDs per AMD CPU generation.

Reference: ZenStates-Core (irusanov/ZenStates-Core), ryzen_smu driver (amkillam fork).
Command sets are derived from the SMUSettings/*.cs files in ZenStates-Core.
"""

from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields
from enum import Enum, auto


class CPUGeneration(Enum):
    ZEN2_MATISSE = auto()       # 3000 series desktop (family 0x17, model 0x71)
    ZEN2_CASTLE_PEAK = auto()   # 3000 TR (family 0x17, model 0x31) — same SMU as Matisse
    ZEN3_VERMEER = auto()       # 5000 series desktop (family 0x19, model 0x20-0x2F)
    ZEN3_CEZANNE = auto()       # 5000 APU (family 0x19, model 0x50)
    ZEN3D_WARHOL = auto()       # 5800X3D (family 0x19, model 0x20-0x21 + X3D name)
    ZEN4_RAPHAEL = auto()       # 7000 series desktop (family 0x19, model 0x60-0x7F)
    ZEN4_PHOENIX = auto()       # 7040/8040 APU (family 0x19, model 0x74-0x75)
    ZEN4_DRAGON_RANGE = auto()  # 7045 mobile — uses Raphael commands
    ZEN4_STORM_PEAK = auto()    # Zen 4 TR (family 0x19, model 0x18)
    ZEN5_GRANITE_RIDGE = auto() # 9000 series desktop (family 0x1A, model 0x44)
    ZEN5_STRIX_POINT = auto()   # 9050 APU (family 0x1A, model 0x24)
    ZEN5_STRIX_HALO = auto()    # Zen 5 APU (family 0x1A, model 0x70)
    ZEN5_SHIMADA_PEAK = auto()  # Zen 5 TR (different RSMU addresses)
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class SMUCommandSet:
    """SMU command opcodes for CO and PBO read/write."""

    generation: CPUGeneration
    co_range: tuple[int, int]  # (min, max) CO values this generation supports
    mailbox: str  # "rsmu" or "mp1"
    encoding_scheme: str  # "none" | "zen3" | "zen4_5"

    # CO (Curve Optimizer / DldoPsmMargin) commands — None if generation lacks CO
    set_co_cmd: int | None = None
    set_all_co_cmd: int | None = None
    get_co_cmd: int | None = None

    # PBO power limits
    set_ppt_cmd: int | None = None
    set_tdc_cmd: int | None = None
    set_edc_cmd: int | None = None
    set_htc_cmd: int | None = None

    # PBO scalar
    set_pbo_scalar_cmd: int | None = None
    get_pbo_scalar_cmd: int | None = None

    # Boost frequency
    set_boost_limit_cmd: int | None = None
    get_boost_limit_cmd: int | None = None
    set_oc_freq_all_cmd: int | None = None
    set_oc_freq_per_core_cmd: int | None = None

    # OC mode
    enable_oc_cmd: int | None = None
    disable_oc_cmd: int | None = None
    is_overclockable_cmd: int | None = None

    # Info queries
    get_fastest_core_cmd: int | None = None
    get_ln2_mode_cmd: int | None = None

    # PM table
    transfer_table_cmd: int | None = None
    get_dram_base_cmd: int | None = None
    get_table_version_cmd: int | None = None

    @property
    def has_co(self) -> bool:
        """Whether this generation supports Curve Optimizer."""
        return self.set_co_cmd is not None and self.get_co_cmd is not None

    @property
    def has_pbo_limits(self) -> bool:
        """Whether PPT/TDC/EDC can be set."""
        return self.set_ppt_cmd is not None


# ===========================================================================
# Known command sets per generation
# ===========================================================================
#
# CO ranges: These are the hardware-supported ranges, NOT "safe" ranges.
# The hardware will accept values outside typical recommendations.
# - Zen 3: -30 to +30 (positive increases voltage, rarely useful)
# - Zen 3D: -30 to +30 (V-Cache sensitive, be conservative)
# - Zen 4: -50 to +30 (extended negative range confirmed by community)
# - Zen 5: -60 to +10
#
# Zen 2 has NO Curve Optimizer but does have PBO scalar/limits.
# ===========================================================================

COMMAND_SETS: dict[CPUGeneration, SMUCommandSet] = {
    # -----------------------------------------------------------------------
    # Zen 2 — NO Curve Optimizer, PBO limits only
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN2_MATISSE: SMUCommandSet(
        generation=CPUGeneration.ZEN2_MATISSE,
        co_range=(0, 0),  # no CO support
        mailbox="rsmu",
        encoding_scheme="none",
        # no CO commands
        set_co_cmd=None,
        set_all_co_cmd=None,
        get_co_cmd=None,
        # PBO limits (RSMU)
        set_ppt_cmd=0x53,
        set_tdc_cmd=0x54,
        set_edc_cmd=0x55,
        set_htc_cmd=0x56,
        set_pbo_scalar_cmd=0x58,
        get_pbo_scalar_cmd=0x6C,
        set_oc_freq_all_cmd=0x5C,
        set_oc_freq_per_core_cmd=0x5D,
        enable_oc_cmd=0x5A,
        disable_oc_cmd=0x5B,
        is_overclockable_cmd=0x6F,
        get_fastest_core_cmd=0x59,
        get_boost_limit_cmd=0x6E,
        transfer_table_cmd=0x05,
        get_dram_base_cmd=0x06,
        get_table_version_cmd=0x08,
    ),
    # Castle Peak (Zen 2 TR) — aliased from Matisse after dict definition
    # -----------------------------------------------------------------------
    # Zen 3 Vermeer — first generation with CO (MP1 mailbox)
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN3_VERMEER: SMUCommandSet(
        generation=CPUGeneration.ZEN3_VERMEER,
        co_range=(-30, 30),
        mailbox="mp1",
        encoding_scheme="zen3",
        set_co_cmd=0x35,
        set_all_co_cmd=0x36,
        get_co_cmd=0x48,
        # PBO limits (RSMU — Zen 3 uses RSMU for limits, MP1 for CO)
        set_ppt_cmd=0x53,
        set_tdc_cmd=0x54,
        set_edc_cmd=0x55,
        set_htc_cmd=0x56,
        set_pbo_scalar_cmd=0x58,
        get_pbo_scalar_cmd=0x6C,
        set_oc_freq_all_cmd=0x5C,
        set_oc_freq_per_core_cmd=0x5D,
        enable_oc_cmd=0x5A,
        disable_oc_cmd=0x5B,
        is_overclockable_cmd=0x6F,
        get_fastest_core_cmd=0x59,
        get_boost_limit_cmd=0x6E,
        transfer_table_cmd=0x05,
        get_dram_base_cmd=0x06,
        get_table_version_cmd=0x08,
    ),
    # -----------------------------------------------------------------------
    # Zen 3D (5800X3D) — CO officially locked by AMD, but accessible via SMU.
    # V-Cache has strict voltage limits. Conservative tuning recommended.
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN3D_WARHOL: SMUCommandSet(
        generation=CPUGeneration.ZEN3D_WARHOL,
        co_range=(-30, 30),  # hardware accepts this; V-Cache makes >-25 risky
        mailbox="mp1",
        encoding_scheme="zen3",
        set_co_cmd=0x35,
        set_all_co_cmd=0x36,
        get_co_cmd=0x48,
        set_ppt_cmd=0x53,
        set_tdc_cmd=0x54,
        set_edc_cmd=0x55,
        set_htc_cmd=0x56,
        set_pbo_scalar_cmd=0x58,
        get_pbo_scalar_cmd=0x6C,
        enable_oc_cmd=0x5A,
        disable_oc_cmd=0x5B,
        is_overclockable_cmd=0x6F,
        get_boost_limit_cmd=0x6E,
        transfer_table_cmd=0x05,
        get_dram_base_cmd=0x06,
        get_table_version_cmd=0x08,
    ),
    # -----------------------------------------------------------------------
    # Zen 3 Cezanne (APU) — same CO commands as Vermeer
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN3_CEZANNE: SMUCommandSet(
        generation=CPUGeneration.ZEN3_CEZANNE,
        co_range=(-30, 30),
        mailbox="mp1",
        encoding_scheme="zen3",
        set_co_cmd=0x35,
        set_all_co_cmd=0x36,
        get_co_cmd=0x48,
        set_ppt_cmd=0x53,
        set_tdc_cmd=0x54,
        set_edc_cmd=0x55,
        set_htc_cmd=0x56,
        set_pbo_scalar_cmd=0x58,
        get_pbo_scalar_cmd=0x6C,
        enable_oc_cmd=0x5A,
        disable_oc_cmd=0x5B,
        is_overclockable_cmd=0x6F,
        transfer_table_cmd=0x05,
        get_dram_base_cmd=0x06,
        get_table_version_cmd=0x08,
    ),
    # -----------------------------------------------------------------------
    # Zen 4 Raphael — RSMU mailbox, extended negative CO range
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN4_RAPHAEL: SMUCommandSet(
        generation=CPUGeneration.ZEN4_RAPHAEL,
        co_range=(-50, 30),  # -40 confirmed working, allow -50 for headroom
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xD5,
        set_ppt_cmd=0x56,
        set_tdc_cmd=0x57,
        set_edc_cmd=0x58,
        set_htc_cmd=0x59,
        set_pbo_scalar_cmd=0x5B,
        get_pbo_scalar_cmd=0x6D,
        set_boost_limit_cmd=0x70,
        get_boost_limit_cmd=0x6E,
        set_oc_freq_all_cmd=0x5F,
        set_oc_freq_per_core_cmd=0x60,
        enable_oc_cmd=0x5D,
        disable_oc_cmd=0x5E,
        is_overclockable_cmd=0x6F,
        get_fastest_core_cmd=0x59,
        get_ln2_mode_cmd=0xDD,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
    # -----------------------------------------------------------------------
    # Zen 4 Phoenix/Hawk Point APU
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN4_PHOENIX: SMUCommandSet(
        generation=CPUGeneration.ZEN4_PHOENIX,
        co_range=(-50, 30),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xD5,
        set_ppt_cmd=0x56,
        set_tdc_cmd=0x57,
        set_edc_cmd=0x58,
        set_htc_cmd=0x59,
        set_pbo_scalar_cmd=0x5B,
        get_pbo_scalar_cmd=0x6D,
        enable_oc_cmd=0x5D,
        disable_oc_cmd=0x5E,
        is_overclockable_cmd=0x6F,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
    # -----------------------------------------------------------------------
    # Zen 4 Storm Peak (ThreadRipper)
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN4_STORM_PEAK: SMUCommandSet(
        generation=CPUGeneration.ZEN4_STORM_PEAK,
        co_range=(-50, 30),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xD5,
        set_ppt_cmd=0x56,
        set_tdc_cmd=0x57,
        set_edc_cmd=0x58,
        set_htc_cmd=0x59,
        set_pbo_scalar_cmd=0x5B,
        get_pbo_scalar_cmd=0x6D,
        set_boost_limit_cmd=0x70,
        get_boost_limit_cmd=0x6E,
        enable_oc_cmd=0x5D,
        disable_oc_cmd=0x5E,
        is_overclockable_cmd=0x6F,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
    # Dragon Range (Zen 4 mobile) — aliased from Raphael after dict definition
    # -----------------------------------------------------------------------
    # Zen 5 Granite Ridge — same RSMU cmd IDs as Zen 4, wider CO range
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN5_GRANITE_RIDGE: SMUCommandSet(
        generation=CPUGeneration.ZEN5_GRANITE_RIDGE,
        co_range=(-60, 10),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xD5,
        set_ppt_cmd=0x56,
        set_tdc_cmd=0x57,
        set_edc_cmd=0x58,
        set_htc_cmd=0x59,
        set_pbo_scalar_cmd=0x5B,
        get_pbo_scalar_cmd=0x6D,
        set_boost_limit_cmd=0x70,
        get_boost_limit_cmd=0x6E,
        set_oc_freq_all_cmd=0x5F,
        set_oc_freq_per_core_cmd=0x60,
        enable_oc_cmd=0x5D,
        disable_oc_cmd=0x5E,
        is_overclockable_cmd=0x6F,
        get_fastest_core_cmd=0x59,
        get_ln2_mode_cmd=0xDD,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
    # -----------------------------------------------------------------------
    # Zen 5 Strix Point (APU)
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN5_STRIX_POINT: SMUCommandSet(
        generation=CPUGeneration.ZEN5_STRIX_POINT,
        co_range=(-60, 10),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xD5,
        set_ppt_cmd=0x56,
        set_tdc_cmd=0x57,
        set_edc_cmd=0x58,
        set_htc_cmd=0x59,
        set_pbo_scalar_cmd=0x5B,
        get_pbo_scalar_cmd=0x6D,
        enable_oc_cmd=0x5D,
        disable_oc_cmd=0x5E,
        is_overclockable_cmd=0x6F,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
    # -----------------------------------------------------------------------
    # Zen 5 Shimada Peak (ThreadRipper) — different RSMU base addresses!
    # GetDldoPsmMargin = 0xA3 (NOT 0xD5), GetLN2Mode = 0xA6 (NOT 0xDD)
    # SMU_ADDR_MSG = 0x03B10924 (vs 0x03B10524 for desktop)
    # -----------------------------------------------------------------------
    CPUGeneration.ZEN5_SHIMADA_PEAK: SMUCommandSet(
        generation=CPUGeneration.ZEN5_SHIMADA_PEAK,
        co_range=(-60, 10),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xA3,  # different from desktop!
        set_ppt_cmd=0x56,
        set_tdc_cmd=0x57,
        set_edc_cmd=0x58,
        set_htc_cmd=0x59,
        set_pbo_scalar_cmd=0x5B,
        get_pbo_scalar_cmd=0x6D,
        set_boost_limit_cmd=0x70,
        get_boost_limit_cmd=0x6E,
        enable_oc_cmd=0x5D,
        disable_oc_cmd=0x5E,
        is_overclockable_cmd=0x6F,
        get_ln2_mode_cmd=0xA6,  # different from desktop!
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
}


def _alias_commands(source: CPUGeneration, target: CPUGeneration) -> SMUCommandSet:
    """Create a command set for target that shares source's commands.

    All fields are copied from source except ``generation``, which is set to target.
    Only use when two generations have truly identical SMU commands (same silicon).
    """
    base = COMMAND_SETS[source]
    fields = {f.name: getattr(base, f.name) for f in dataclass_fields(base)}
    fields["generation"] = target
    return SMUCommandSet(**fields)


# -----------------------------------------------------------------------
# Aliased command sets — identical SMU commands, different generation enum
# -----------------------------------------------------------------------
COMMAND_SETS[CPUGeneration.ZEN2_CASTLE_PEAK] = _alias_commands(
    CPUGeneration.ZEN2_MATISSE, CPUGeneration.ZEN2_CASTLE_PEAK
)
COMMAND_SETS[CPUGeneration.ZEN4_DRAGON_RANGE] = _alias_commands(
    CPUGeneration.ZEN4_RAPHAEL, CPUGeneration.ZEN4_DRAGON_RANGE
)


def detect_generation(family: int, model: int, model_name: str) -> CPUGeneration:
    """Detect CPU generation from CPUID family/model and model name.

    Uses family (from /proc/cpuinfo 'cpu family') and model to identify
    the processor codename, which determines the SMU command set.
    """
    name_lower = model_name.lower()

    # AMD family 0x17 (23) = Zen / Zen+ / Zen 2
    if family == 23:
        if model in (0x71,):  # Matisse
            return CPUGeneration.ZEN2_MATISSE
        if model in (0x31,):  # Castle Peak (TR)
            return CPUGeneration.ZEN2_CASTLE_PEAK
        # Zen 1 (0x01, 0x11) and Zen+ (0x08, 0x18) — same PBO limits as Matisse
        return CPUGeneration.ZEN2_MATISSE  # fallback for family 23

    # AMD family 0x19 (25) = Zen 3 / Zen 4
    if family == 25:
        # X3D detection first (overrides model-based detection)
        # Zen 3 X3D (5800X3D): model 0x20-0x2F + X3D name
        if "5800x3d" in name_lower or ("x3d" in name_lower and model in range(0x20, 0x30)):
            return CPUGeneration.ZEN3D_WARHOL
        # Zen 4 X3D (7800X3D, 7900X3D, 7950X3D): model 0x60-0x7F + X3D name
        # Same Raphael commands — X3D CO tuning works but V-Cache is sensitive
        if "x3d" in name_lower and model in range(0x60, 0x80):
            return CPUGeneration.ZEN4_RAPHAEL

        if model in range(0x20, 0x30):  # Vermeer
            return CPUGeneration.ZEN3_VERMEER
        if model in range(0x50, 0x60):  # Cezanne
            return CPUGeneration.ZEN3_CEZANNE
        if model in (0x18,):  # Storm Peak (Zen 4 TR)
            return CPUGeneration.ZEN4_STORM_PEAK
        if model in (0x74, 0x75):  # Phoenix / Hawk Point (must be before Raphael range)
            return CPUGeneration.ZEN4_PHOENIX
        if model in range(0x60, 0x80):  # Raphael + Dragon Range (same silicon)
            return CPUGeneration.ZEN4_RAPHAEL
        if model in range(0x40, 0x50):  # Rembrandt (Zen 3+ APU)
            return CPUGeneration.ZEN3_CEZANNE  # same SMU commands

        return CPUGeneration.ZEN3_VERMEER  # fallback for family 25

    # AMD family 0x1A (26) = Zen 5
    if family == 26:
        if model in (0x24,):  # Strix Point
            return CPUGeneration.ZEN5_STRIX_POINT
        if model in (0x70,):  # Strix Halo
            return CPUGeneration.ZEN5_STRIX_POINT  # similar commands
        if "threadripper" in name_lower or "shimada" in name_lower:
            return CPUGeneration.ZEN5_SHIMADA_PEAK
        return CPUGeneration.ZEN5_GRANITE_RIDGE

    return CPUGeneration.UNKNOWN


def get_commands(generation: CPUGeneration) -> SMUCommandSet | None:
    """Get SMU commands for a CPU generation. Returns None if unsupported."""
    return COMMAND_SETS.get(generation)


def encode_co_arg(
    core_id: int,
    value: int,
    generation: CPUGeneration,
    *,
    ccd: int | None = None,
) -> int:
    """Encode core ID and CO value into SMU command argument.

    Bit layout (Zen 3+ per-core set):
      [31:28] = CCD index
      [27:24] = CCX index (always 0 for Zen 3+, each CCD has 1 CCX)
      [23:20] = Core index within CCX
      [19:16] = Reserved
      [15:0]  = CO margin value (16-bit two's complement)

    For Zen 2 (family 0x17): CCD << 28 | CCX << 24 | (core % 4) << 20
    For Zen 3+: CCD << 28 | (core % 8) << 20

    Args:
        ccd: Topology-detected CCD index. If provided, used instead of
             deriving CCD from ``core_id // 8``. Always prefer passing the
             L3-detected CCD from topology when available.
    """
    commands = get_commands(generation)
    if commands is None:
        raise ValueError(f"Unsupported generation: {generation}")
    scheme = commands.encoding_scheme

    # Encode the CO value: negative values use two's complement in 16 bits.
    # ZenStates uses: offset = 0x100000 if margin < 0 else 0; (offset + margin) & 0xFFFF
    # This is equivalent to standard 16-bit two's complement.
    margin = value & 0xFFFF

    if scheme == "zen3":
        # Zen 3: ((core_id & 8) << 5 | core_id & 7) << 20 | margin
        # This encodes CCD in bit 28 (core_id >= 8 means CCD1)
        return (((core_id & 8) << 5 | core_id & 7) << 20) | margin

    if scheme == "zen4_5":
        # Zen 4/5: CCD in bits [31:28], core within CCD in bits [23:20]
        # Prefer topology-detected CCD; fall back to core_id // 8
        detected_ccd = ccd if ccd is not None else core_id // 8
        core_in_ccd = core_id % 8
        return (detected_ccd << 28) | (core_in_ccd << 20) | margin

    raise ValueError(
        f"Zen 2 ({generation.name}) does not support Curve Optimizer"
    )


def decode_co_arg(core_id: int, response: int, generation: CPUGeneration) -> int:
    """Decode CO value from SMU response argument.

    The response contains the CO value in the low 16 bits as two's complement.
    """
    commands = get_commands(generation)
    if commands is None:
        raise ValueError(f"Unsupported generation: {generation}")
    scheme = commands.encoding_scheme

    if scheme in ("zen3", "zen4_5"):
        raw = response & 0xFFFF
        return raw if raw < 0x8000 else raw - 0x10000

    raise ValueError(f"Unsupported generation: {generation}")


def encode_pbo_limit_arg(value_w_or_a: int) -> int:
    """Encode a PBO power/current limit for SMU.

    PPT in watts, TDC/EDC in amps. Converted to milliwatts/milliamps for SMU.
    """
    return value_w_or_a * 1000


def encode_pbo_scalar_arg(scalar: float) -> int:
    """Encode PBO scalar (1.0-10.0) for SMU. ZenStates: arg0 = scalar * 100."""
    return int(scalar * 100)


def encode_boost_limit_arg(freq_mhz: int) -> int:
    """Encode boost frequency limit (MHz) for SMU. 20-bit value."""
    return freq_mhz & 0xFFFFF
