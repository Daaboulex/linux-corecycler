"""SMU command IDs per AMD CPU generation.

Reference: ZenStates-Core (irusanov/ZenStates-Core), ryzen_smu driver (amkillam fork).
Command sets are derived from the SMUSettings/*.cs files in ZenStates-Core.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from enum import Enum, auto


class CPUGeneration(Enum):
    ZEN2_MATISSE = auto()       # 3000 series desktop (family 0x17, model 0x71)
    ZEN2_CASTLE_PEAK = auto()   # 3000 TR (family 0x17, model 0x31) — same SMU as Matisse
    ZEN3_VERMEER = auto()       # 5000 series desktop (family 0x19, model 0x20-0x2F)
    ZEN3_CHAGALL = auto()       # 5000 TR + Milan (family 0x19, model 0x00-0x0F) — Vermeer SMU
    ZEN3_CEZANNE = auto()       # 5000 APU (family 0x19, model 0x50-0x5F)
    ZEN3_REMBRANDT = auto()     # 6000 APU (family 0x19, model 0x40-0x4F) —
                                # Phoenix-class CO commands, get_co=0x2F
    ZEN3D_WARHOL = auto()       # 5800X3D (family 0x19, model 0x20-0x21 + X3D name)
    ZEN4_RAPHAEL = auto()       # 7000 desktop + Dragon Range (family 0x19, model 0x60-0x6F)
    ZEN4_PHOENIX = auto()       # 7040/8040 APU (family 0x19, model 0x74 Phoenix,
                                # 0x75 Hawk Point — classic monolithic 8-core CCX)
    ZEN4_PHOENIX2 = auto()      # Small het APU (family 0x19, 0x78 Phoenix2 2+4c,
                                # 0x7C Hawk Point 2) — Phoenix commands, no slot map
    ZEN4_DRAGON_RANGE = auto()  # 7045 mobile — uses Raphael commands
    ZEN4_STORM_PEAK = auto()    # Zen 4 TR (family 0x19, model 0x10-0x1F)
    ZEN5_GRANITE_RIDGE = auto() # 9000 desktop + Fire Range (family 0x1A, model 0x40-0x4F)
    ZEN5_STRIX_POINT = auto()   # Ryzen AI APU (family 0x1A, model 0x20-0x2F het 4+8c;
                                # 0x60-0x6F Krackan Point 4+4c / 1+3c het routes here too)
    ZEN5_STRIX_HALO = auto()    # Ryzen AI Max APU (family 0x1A, model 0x70-0x7F)
    ZEN5_SHIMADA_PEAK = auto()  # Zen 5 TR (family 0x1A, model 0x00-0x0F, 9980X dump
                                # B00F81; different RSMU addresses, get_co=0xA3)
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class SMUCommandSet:
    """SMU command opcodes for CO and PBO read/write."""

    generation: CPUGeneration
    co_range: tuple[int, int]  # (min, max) CO values this generation supports
    mailbox: str  # "rsmu" or "mp1"
    encoding_scheme: str  # "none" | "zen3" | "zen4_5"
    # True where the CO core address space is verified to be uniform 8-slot
    # CCDs (one 8-core CCX per CCD): classic desktop/TR dies and monolithic
    # 8-core-CCX APUs. Gates RyzenSMU's core-map discovery (slot probing and
    # its fail-closed refusals); False keeps the legacy core_id-derived
    # addressing untouched. Deliberately False for heterogeneous Zen4c/5c
    # parts (Phoenix2/Hawk Point 2, Strix Point, Krackan), for Strix Halo
    # (classic CCDs, but per-core CO tuning there is unverified with known
    # tool failures), and for any future generation until verified.
    uniform_8core_ccds: bool = False

    # CO (Curve Optimizer / DldoPsmMargin) commands — None if generation lacks CO.
    # get_co_mailbox overrides the mailbox for the GET command only: the APU
    # classes set CO via MP1 but read it back via RSMU (ZenStates-Core
    # APUSettings1*); None means the default mailbox.
    set_co_cmd: int | None = None
    set_all_co_cmd: int | None = None
    get_co_cmd: int | None = None
    get_co_mailbox: str | None = None

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
# - Zen 5: -50 to +10 (firmware clamps at -50; -60 is a BIOS input ceiling only)
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
        uniform_8core_ccds=True,
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
        uniform_8core_ccds=True,
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
        uniform_8core_ccds=True,
        set_co_cmd=0x54,
        set_all_co_cmd=0x55,
        get_co_cmd=0xC3,
        get_co_mailbox="rsmu",
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
        uniform_8core_ccds=True,
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
        mailbox="mp1",
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
        set_co_cmd=0x4B,
        set_all_co_cmd=0x4C,
        get_co_cmd=0xE1,
        get_co_mailbox="rsmu",
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
        uniform_8core_ccds=True,
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
        co_range=(-50, 10),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
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
        co_range=(-50, 10),
        mailbox="mp1",
        encoding_scheme="zen4_5",
        set_co_cmd=0x4B,
        set_all_co_cmd=0x4C,
        get_co_cmd=0xAF,
        get_co_mailbox="rsmu",
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
        co_range=(-50, 10),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
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


def _alias_commands(
    source: CPUGeneration, target: CPUGeneration, **overrides
) -> SMUCommandSet:
    """Create a command set for target that shares source's commands.

    All fields are copied from source except ``generation`` (set to target) and
    any explicit ``overrides`` (e.g. a die that shares the mailbox commands but
    not the classic CCX layout). Only use when the SMU commands truly match.
    """
    base = COMMAND_SETS[source]
    fields = {f.name: getattr(base, f.name) for f in dataclass_fields(base)}
    fields["generation"] = target
    fields.update(overrides)
    return SMUCommandSet(**fields)


# -----------------------------------------------------------------------
# Aliased command sets — identical SMU commands, different generation enum
# -----------------------------------------------------------------------
COMMAND_SETS[CPUGeneration.ZEN2_CASTLE_PEAK] = _alias_commands(
    CPUGeneration.ZEN2_MATISSE, CPUGeneration.ZEN2_CASTLE_PEAK
)
COMMAND_SETS[CPUGeneration.ZEN3_CHAGALL] = _alias_commands(
    CPUGeneration.ZEN3_VERMEER, CPUGeneration.ZEN3_CHAGALL
)
COMMAND_SETS[CPUGeneration.ZEN3_REMBRANDT] = _alias_commands(
    CPUGeneration.ZEN4_PHOENIX, CPUGeneration.ZEN3_REMBRANDT, get_co_cmd=0x2F
)
COMMAND_SETS[CPUGeneration.ZEN4_DRAGON_RANGE] = _alias_commands(
    CPUGeneration.ZEN4_RAPHAEL, CPUGeneration.ZEN4_DRAGON_RANGE
)
# Phoenix2 (2x Zen4 + 4x Zen4c in one shared CCX) and Hawk Point 2 share
# Phoenix's mailbox commands but not its classic 8-core CCX layout, so the
# slot-map discovery stays off for them.
COMMAND_SETS[CPUGeneration.ZEN4_PHOENIX2] = _alias_commands(
    CPUGeneration.ZEN4_PHOENIX,
    CPUGeneration.ZEN4_PHOENIX2,
    uniform_8core_ccds=False,
)
# Strix Halo shares Strix Point's SMU interface. The completeness test pins
# "every known generation has a set", so a routed generation can never build
# RyzenSMU(commands=None).
COMMAND_SETS[CPUGeneration.ZEN5_STRIX_HALO] = _alias_commands(
    CPUGeneration.ZEN5_STRIX_POINT, CPUGeneration.ZEN5_STRIX_HALO
)


# The single model-routing table: (family, model_lo, model_hi, generation),
# first match wins; an unmatched family/model is UNKNOWN (no SMU commands —
# fail closed for future dies rather than inheriting a probing generation:
# Zen 6 CPUID sightings already sit at family 0x1A model 0x80+). Grounded in
# the ryzen_smu driver's own CPUID table (smu.c smu_resolve_cpu_class,
# amkillam fork rev 21c1e2c — the exact source pinned in nix/ryzen-smu.nix,
# and the transport every SMU feature here rides on), the documented block
# scheme from Zen 3 on (AMD reserves 16 model ids per die: Chagall 0x00-0x0F,
# Storm Peak 0x10-0x1F, Vermeer 0x20-0x2F, ..., which Ryzen Master keys on),
# and InstLatx64 CPUID dumps (Chagall A00F82, Storm Peak A10F81, Phoenix2
# A70F80-class, Hawk Point 2 A70FC0, Granite Ridge B40F40, Strix Point
# B20F40, Krackan B60F00/B60F80, Strix Halo B70F00, Shimada Peak B00F81).
# Heterogeneous Zen4c/5c blocks (Phoenix2/HP2 0x76-0x7F, Strix 0x20-0x2F,
# Krackan 0x60-0x6F) route to generations declaring uniform_8core_ccds=False.
# Family 0x17 (Zen1/+/2) has no CO, so the whole family safely shares the
# Matisse PBO-only set, Castle Peak excepted for its own display identity.
# Name-based adjustments (X3D, Zen 5 Threadripper) live in detect_generation.
_MODEL_TABLE: tuple[tuple[int, int, int, CPUGeneration], ...] = (
    (23, 0x31, 0x31, CPUGeneration.ZEN2_CASTLE_PEAK),
    (23, 0x00, 0xFF, CPUGeneration.ZEN2_MATISSE),
    (25, 0x00, 0x0F, CPUGeneration.ZEN3_CHAGALL),
    (25, 0x10, 0x1F, CPUGeneration.ZEN4_STORM_PEAK),
    (25, 0x20, 0x2F, CPUGeneration.ZEN3_VERMEER),
    (25, 0x40, 0x4F, CPUGeneration.ZEN3_REMBRANDT),
    (25, 0x50, 0x5F, CPUGeneration.ZEN3_CEZANNE),
    (25, 0x60, 0x6F, CPUGeneration.ZEN4_RAPHAEL),
    (25, 0x74, 0x75, CPUGeneration.ZEN4_PHOENIX),
    (25, 0x70, 0x7F, CPUGeneration.ZEN4_PHOENIX2),
    (26, 0x00, 0x0F, CPUGeneration.ZEN5_SHIMADA_PEAK),
    (26, 0x20, 0x2F, CPUGeneration.ZEN5_STRIX_POINT),
    (26, 0x40, 0x4F, CPUGeneration.ZEN5_GRANITE_RIDGE),
    (26, 0x60, 0x6F, CPUGeneration.ZEN5_STRIX_POINT),
    (26, 0x70, 0x7F, CPUGeneration.ZEN5_STRIX_HALO),
)


def detect_generation(family: int, model: int, model_name: str) -> CPUGeneration:
    """Detect CPU generation from CPUID family/model and model name.

    All family/model knowledge lives in _MODEL_TABLE (first match wins); this
    function only applies the name-based adjustments the table cannot express:
    the 5800X3D (Warhol shares Vermeer's model block) and a belt-and-braces
    Zen 5 Threadripper name check alongside Shimada Peak's model row.
    """
    name_lower = model_name.lower()

    if family == 26 and ("threadripper" in name_lower or "shimada" in name_lower):
        return CPUGeneration.ZEN5_SHIMADA_PEAK

    generation = next(
        (
            gen
            for fam, lo, hi, gen in _MODEL_TABLE
            if family == fam and lo <= model <= hi
        ),
        CPUGeneration.UNKNOWN,
    )

    if generation is CPUGeneration.ZEN3_VERMEER and "x3d" in name_lower:
        return CPUGeneration.ZEN3D_WARHOL

    return generation


def get_commands(generation: CPUGeneration) -> SMUCommandSet | None:
    """Get SMU commands for a CPU generation. Returns None if unsupported."""
    return COMMAND_SETS.get(generation)


def encode_co_arg(
    core_id: int,
    value: int,
    generation: CPUGeneration,
    *,
    ccd: int | None = None,
    slot: int | None = None,
) -> int:
    """Encode core ID and CO value into SMU command argument.

    Pure bit-packer: ``value`` is encoded as-is into the low 16 bits (two's
    complement) with NO range check -- that is deliberate so the encoder can be
    round-trip-tested over the full int16 range. Enforcing the generation's valid
    CO range is the WRITER's job: every real write goes through
    ``RyzenSMU.set_co_offset`` / ``set_all_co``, which range-check against
    ``co_range`` and raise before calling this. A direct caller must do the same.

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
        slot: Physical slot index (0-7) within the CCD, overriding the default
              ``core_id % 8``. The default is exact only when the kernel's
              ``core id`` carries the physical, gap-preserving numbering (a
              fused-off core leaves a hole). Some BIOS/AGESA builds renumber
              core ids contiguously on harvested parts instead (issue #11,
              5600X), so ``RyzenSMU.set_topology`` discovers the true mapping
              and passes both ``ccd`` and ``slot`` explicitly.
    """
    commands = get_commands(generation)
    if commands is None:
        raise ValueError(f"Unsupported generation: {generation}")
    scheme = commands.encoding_scheme

    # Encode the CO value: negative values use two's complement in 16 bits.
    # ZenStates uses: offset = 0x100000 if margin < 0 else 0; (offset + margin) & 0xFFFF
    # This is equivalent to standard 16-bit two's complement.
    margin = value & 0xFFFF

    if scheme in ("zen3", "zen4_5"):
        # Zen 3/4/5 per-core layout: CCD in bits [31:28], physical core-in-CCD
        # in bits [23:20]. The core_id-derived defaults hold only for the
        # gap-preserving physical numbering; RyzenSMU.set_topology discovers
        # each core's true (ccd, slot) and passes both explicitly.
        detected_ccd = ccd if ccd is not None else core_id // 8
        core_in_ccd = slot if slot is not None else core_id % 8
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
