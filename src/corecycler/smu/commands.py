"""SMU command IDs per AMD CPU generation.

Reference: ZenStates-Core (irusanov/ZenStates-Core), ryzen_smu driver (amkillam fork).
Command sets are derived from the SMUSettings/*.cs files in ZenStates-Core.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from enum import Enum, auto
from typing import Literal


class CPUGeneration(Enum):
    ZEN2_MATISSE = auto()
    ZEN2_CASTLE_PEAK = auto()
    ZEN3_VERMEER = auto()
    ZEN3_CHAGALL = auto()
    ZEN3_CEZANNE = auto()
    ZEN3_REMBRANDT = auto()
    ZEN3D_WARHOL = auto()
    ZEN4_RAPHAEL = auto()
    ZEN4_PHOENIX = auto()
    ZEN4_PHOENIX2 = auto()
    ZEN4_DRAGON_RANGE = auto()
    ZEN4_STORM_PEAK = auto()
    ZEN5_GRANITE_RIDGE = auto()
    ZEN5_STRIX_POINT = auto()
    ZEN5_STRIX_HALO = auto()
    ZEN5_SHIMADA_PEAK = auto()
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class SMUCommandSet:
    """SMU command opcodes for CO and PBO read/write."""

    generation: CPUGeneration
    # The range the firmware accepts, not a safe range.
    co_range: tuple[int, int]
    mailbox: Literal["rsmu", "mp1"]
    encoding_scheme: Literal["none", "zen3", "zen4_5"]
    uniform_8core_ccds: bool = False

    core_fuse_addr: int | None = None
    core_fuse_shift: int = 0
    core_fuse_die_wide: bool = False
    ccd_fuse_addr: int | None = None
    probe_co_baseline: bool = False

    set_co_cmd: int | None = None
    set_all_co_cmd: int | None = None
    get_co_cmd: int | None = None
    # APU dies set CO over MP1 but read it over RSMU (ZenStates-Core APUSettings1).
    get_co_mailbox: Literal["rsmu", "mp1"] | None = None
    # The APU CO read takes the bare slot, not the packed word (ZenStates-Core GetPsmMarginSingleCore).
    get_co_by_slot: bool = False
    get_curve_shaper_cmd: int | None = None
    set_curve_shaper_cmd: int | None = None

    # On APU dies set_ppt_cmd is RSMU SetSlowLimit, APUs have no desktop PPT id (ZenStates-Core APUSettings1).
    set_ppt_cmd: int | None = None
    set_tdc_cmd: int | None = None
    set_edc_cmd: int | None = None
    set_htc_cmd: int | None = None

    set_pbo_scalar_cmd: int | None = None
    get_pbo_scalar_cmd: int | None = None

    set_boost_limit_cmd: int | None = None
    get_boost_limit_cmd: int | None = None
    set_oc_freq_all_cmd: int | None = None
    set_oc_freq_per_core_cmd: int | None = None

    enable_oc_cmd: int | None = None
    disable_oc_cmd: int | None = None
    is_overclockable_cmd: int | None = None

    get_fastest_core_cmd: int | None = None
    get_ln2_mode_cmd: int | None = None

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


CO_MARGIN_MASK = 0xFFFF
CO_CCD_SHIFT = 28
CO_SLOT_SHIFT = 20

# Fuse addresses from ZenStates-Core Cpu.cs GetCpuTopology, Cezanne's from ryzen_monitor_ng, the Zen 3 CCD-disable
# fuses from ryzen_smu userspace/monitor_cpu.c get_fuse_topology.
_FUSE_FAMILY_BASE = 0x30081800
_FUSE_ZEN3_CLASS = _FUSE_FAMILY_BASE + 0x598
_CCD_FUSE_ZEN3 = 0x5D218
_FUSE_CEZANNE = 0x5D448
_FUSE_RAPHAEL = _FUSE_FAMILY_BASE + 0x4D0
_FUSE_GRANITE_RIDGE = 0x304A03DC

COMMAND_SETS: dict[CPUGeneration, SMUCommandSet] = {
    CPUGeneration.ZEN2_MATISSE: SMUCommandSet(
        generation=CPUGeneration.ZEN2_MATISSE,
        co_range=(0, 0),
        mailbox="rsmu",
        encoding_scheme="none",
        set_co_cmd=None,
        set_all_co_cmd=None,
        get_co_cmd=None,
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
    CPUGeneration.ZEN3_VERMEER: SMUCommandSet(
        generation=CPUGeneration.ZEN3_VERMEER,
        co_range=(-30, 30),
        mailbox="mp1",
        encoding_scheme="zen3",
        uniform_8core_ccds=True,
        core_fuse_addr=_FUSE_ZEN3_CLASS,
        ccd_fuse_addr=_CCD_FUSE_ZEN3,
        set_co_cmd=0x35,
        set_all_co_cmd=0x36,
        get_co_cmd=0x48,
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
    CPUGeneration.ZEN3D_WARHOL: SMUCommandSet(
        generation=CPUGeneration.ZEN3D_WARHOL,
        co_range=(-30, 30),
        mailbox="mp1",
        encoding_scheme="zen3",
        uniform_8core_ccds=True,
        core_fuse_addr=_FUSE_ZEN3_CLASS,
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
    CPUGeneration.ZEN3_CEZANNE: SMUCommandSet(
        generation=CPUGeneration.ZEN3_CEZANNE,
        co_range=(-30, 30),
        mailbox="mp1",
        encoding_scheme="zen3",
        uniform_8core_ccds=True,
        core_fuse_addr=_FUSE_CEZANNE,
        core_fuse_shift=11,
        core_fuse_die_wide=True,
        set_co_cmd=0x54,
        set_all_co_cmd=0x55,
        get_co_cmd=0xC3,
        get_co_mailbox="rsmu",
        get_co_by_slot=True,
        set_ppt_cmd=0x33,
        set_tdc_cmd=0x38,
        set_edc_cmd=0x3A,
        set_htc_cmd=0x37,
        set_pbo_scalar_cmd=0x3F,
        get_pbo_scalar_cmd=0xF,
        get_boost_limit_cmd=0x42,
        set_oc_freq_all_cmd=0x19,
        set_oc_freq_per_core_cmd=0x1A,
        enable_oc_cmd=0x17,
        disable_oc_cmd=0x18,
        is_overclockable_cmd=0x82,
        transfer_table_cmd=0x65,
        get_dram_base_cmd=0x66,
        get_table_version_cmd=0x06,
    ),
    CPUGeneration.ZEN4_RAPHAEL: SMUCommandSet(
        generation=CPUGeneration.ZEN4_RAPHAEL,
        co_range=(-50, 30),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
        core_fuse_addr=_FUSE_RAPHAEL,
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
        get_ln2_mode_cmd=0xDD,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
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
        set_ppt_cmd=0x33,
        set_tdc_cmd=0x38,
        set_edc_cmd=0x3A,
        set_htc_cmd=0x37,
        set_pbo_scalar_cmd=0x3E,
        get_pbo_scalar_cmd=0xF,
        set_boost_limit_cmd=0x47,
        get_boost_limit_cmd=0x42,
        set_oc_freq_all_cmd=0x19,
        set_oc_freq_per_core_cmd=0x1A,
        enable_oc_cmd=0x17,
        disable_oc_cmd=0x18,
        is_overclockable_cmd=0x82,
        get_ln2_mode_cmd=0xC4,
        transfer_table_cmd=0x65,
        get_dram_base_cmd=0x66,
        get_table_version_cmd=0x06,
    ),
    CPUGeneration.ZEN4_STORM_PEAK: SMUCommandSet(
        generation=CPUGeneration.ZEN4_STORM_PEAK,
        co_range=(-50, 30),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
        core_fuse_addr=_FUSE_ZEN3_CLASS,
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
    CPUGeneration.ZEN5_GRANITE_RIDGE: SMUCommandSet(
        generation=CPUGeneration.ZEN5_GRANITE_RIDGE,
        co_range=(-50, 10),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
        core_fuse_addr=_FUSE_GRANITE_RIDGE,
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xD5,
        get_curve_shaper_cmd=0x84,
        set_curve_shaper_cmd=0xA6,
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
        get_ln2_mode_cmd=0xDD,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
    CPUGeneration.ZEN5_STRIX_POINT: SMUCommandSet(
        generation=CPUGeneration.ZEN5_STRIX_POINT,
        co_range=(-50, 10),
        mailbox="mp1",
        encoding_scheme="zen4_5",
        probe_co_baseline=True,
        set_co_cmd=0x4B,
        set_all_co_cmd=0x4C,
        get_co_cmd=0xAF,
        get_co_mailbox="rsmu",
        set_ppt_cmd=0x33,
        set_tdc_cmd=0x38,
        set_edc_cmd=0x3A,
        set_htc_cmd=0x37,
        set_pbo_scalar_cmd=0x3E,
        get_pbo_scalar_cmd=0xF,
        set_boost_limit_cmd=0x47,
        get_boost_limit_cmd=0x42,
        set_oc_freq_all_cmd=0x19,
        set_oc_freq_per_core_cmd=0x1A,
        enable_oc_cmd=0x17,
        disable_oc_cmd=0x18,
        is_overclockable_cmd=0x82,
        get_ln2_mode_cmd=0xC4,
        transfer_table_cmd=0x65,
        get_dram_base_cmd=0x66,
        get_table_version_cmd=0x06,
    ),
    # ZenStates-Core marks the Shimada Peak fuse address uncertain, so it has none here.
    CPUGeneration.ZEN5_SHIMADA_PEAK: SMUCommandSet(
        generation=CPUGeneration.ZEN5_SHIMADA_PEAK,
        co_range=(-50, 10),
        mailbox="rsmu",
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
        set_co_cmd=0x06,
        set_all_co_cmd=0x07,
        get_co_cmd=0xA3,
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
        get_ln2_mode_cmd=0xA6,
        transfer_table_cmd=0x03,
        get_dram_base_cmd=0x04,
        get_table_version_cmd=0x05,
    ),
}


def _alias_commands(source: CPUGeneration, target: CPUGeneration, **overrides) -> SMUCommandSet:
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


COMMAND_SETS[CPUGeneration.ZEN2_CASTLE_PEAK] = _alias_commands(
    CPUGeneration.ZEN2_MATISSE, CPUGeneration.ZEN2_CASTLE_PEAK
)
COMMAND_SETS[CPUGeneration.ZEN3_CHAGALL] = _alias_commands(
    CPUGeneration.ZEN3_VERMEER, CPUGeneration.ZEN3_CHAGALL, ccd_fuse_addr=None
)
COMMAND_SETS[CPUGeneration.ZEN3_REMBRANDT] = _alias_commands(
    CPUGeneration.ZEN4_PHOENIX,
    CPUGeneration.ZEN3_REMBRANDT,
    get_co_cmd=0x2F,
    co_range=(-30, 30),
)
COMMAND_SETS[CPUGeneration.ZEN4_DRAGON_RANGE] = _alias_commands(
    CPUGeneration.ZEN4_RAPHAEL, CPUGeneration.ZEN4_DRAGON_RANGE
)
COMMAND_SETS[CPUGeneration.ZEN4_PHOENIX2] = _alias_commands(
    CPUGeneration.ZEN4_PHOENIX,
    CPUGeneration.ZEN4_PHOENIX2,
    uniform_8core_ccds=False,
)
COMMAND_SETS[CPUGeneration.ZEN5_STRIX_HALO] = _alias_commands(
    CPUGeneration.ZEN5_STRIX_POINT, CPUGeneration.ZEN5_STRIX_HALO, probe_co_baseline=False
)


# Model ranges from ryzen_smu smu.c smu_resolve_cpu_class and InstLatx64 CPUID dumps.
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
        (gen for fam, lo, hi, gen in _MODEL_TABLE if family == fam and lo <= model <= hi),
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

    margin = value & CO_MARGIN_MASK

    if scheme in ("zen3", "zen4_5"):
        detected_ccd = ccd if ccd is not None else core_id // 8
        core_in_ccd = slot if slot is not None else core_id % 8
        return (detected_ccd << CO_CCD_SHIFT) | (core_in_ccd << CO_SLOT_SHIFT) | margin

    raise ValueError(f"Zen 2 ({generation.name}) does not support Curve Optimizer")


def decode_co_arg(core_id: int, response: int, generation: CPUGeneration) -> int:
    """Decode CO value from SMU response argument.

    The response contains the CO value in the low 16 bits as two's complement.
    """
    commands = get_commands(generation)
    if commands is None:
        raise ValueError(f"Unsupported generation: {generation}")
    scheme = commands.encoding_scheme

    if scheme in ("zen3", "zen4_5"):
        raw = response & CO_MARGIN_MASK
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


TJMAX_C: dict[str, float] = {
    **dict.fromkeys(
        (
            "9950X3D2",
            "9950X3D",
            "9950X",
            "9900X3D",
            "9900X",
            "9850X3D",
            "9800X3D",
            "9700X",
            "9700F",
            "9600X",
            "9600",
            "9500F",
            "9980X",
            "9970X",
            "9960X",
            "PRO 9995WX",
            "PRO 9985WX",
            "PRO 9975WX",
            "PRO 9965WX",
            "PRO 9955WX",
            "PRO 9945WX",
            "7950X",
            "7900X",
            "7900",
            "7700X",
            "7700",
            "7600X",
            "7600",
            "7500F",
            "7400F",
            "7400",
            "8700G",
            "8700F",
            "8705G",
            "8600G",
            "8605G",
            "8500G",
            "8500GE",
            "8505G",
            "8505GE",
            "8400F",
            "8300G",
            "8300GE",
            "8305G",
            "8305GE",
            "7980X",
            "7970X",
            "7960X",
            "PRO 7995WX",
            "PRO 7985WX",
            "PRO 7975WX",
            "PRO 7965WX",
            "PRO 7955WX",
            "PRO 7945WX",
            "5600XT",
            "5600X",
            "5600T",
            "5600F",
            "5500F",
            "5700G",
            "5700GE",
            "5700",
            "5600GT",
            "5600G",
            "5605GE",
            "5500GT",
            "5300G",
            "5300GE",
            "PRO 5995WX",
            "PRO 5975WX",
            "3950X",
            "3900XT",
            "3900X",
            "3900",
            "3800XT",
            "3800X",
            "3700X",
            "3600XT",
            "3600X",
            "3600",
            "3500X",
            "3500",
            "3990X",
            "3970X",
            "3960X",
        ),
        95.0,
    ),
    **dict.fromkeys(
        (
            "5950X",
            "5900XT",
            "5900X",
            "5800XT",
            "5800X3D",
            "5800X",
            "5700X3D",
            "5700X",
            "5600X3D",
            "5600",
            "5500X3D",
            "5500",
            "PRO 3995WX",
            "PRO 3975WX",
            "PRO 3955WX",
            "PRO 3945WX",
        ),
        90.0,
    ),
    **dict.fromkeys(("7950X3D", "7900X3D", "7800X3D", "7700X3D", "7600X3D", "7500X3D"), 89.0),
}
UNKNOWN_TJMAX_C = min(TJMAX_C.values())
_SKU_RE = re.compile(r"Ryzen (?:\d+ |Threadripper )(PRO )?(\w+)")


def tjmax_c(model_name: str) -> float:
    """The model's Tjmax; a model the table does not name gets the lowest one known (D53)."""
    found = _SKU_RE.search(model_name)
    return UNKNOWN_TJMAX_C if found is None else TJMAX_C.get(f"{found.group(1) or ''}{found.group(2)}", UNKNOWN_TJMAX_C)
