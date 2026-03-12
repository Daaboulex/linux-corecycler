"""SMU command IDs per AMD CPU generation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CPUGeneration(Enum):
    ZEN3_VERMEER = auto()       # 5000 series desktop
    ZEN3_CEZANNE = auto()       # 5000 APU
    ZEN3D_WARHOL = auto()       # 5800X3D
    ZEN4_RAPHAEL = auto()       # 7000 series desktop
    ZEN4D_PHOENIX = auto()      # 7040/8040 APU
    ZEN4_DRAGON_RANGE = auto()  # 7045 mobile
    ZEN5_GRANITE_RIDGE = auto() # 9000 series desktop
    ZEN5_STRIX_POINT = auto()   # 9050 APU
    UNKNOWN = auto()


@dataclass(frozen=True, slots=True)
class SMUCommandSet:
    """SMU command opcodes for CO read/write."""
    generation: CPUGeneration
    set_co_cmd: int          # command to set per-core CO offset
    get_co_cmd: int          # command to read per-core CO offset
    reset_co_cmd: int | None # command to reset all CO offsets
    mailbox: str             # "rsmu" or "mp1"
    co_range: tuple[int, int]  # (min, max) CO values
    set_boost_limit_cmd: int | None = None
    get_boost_limit_cmd: int | None = None


# known command sets per generation
COMMAND_SETS: dict[CPUGeneration, SMUCommandSet] = {
    CPUGeneration.ZEN3_VERMEER: SMUCommandSet(
        generation=CPUGeneration.ZEN3_VERMEER,
        set_co_cmd=0x35,
        get_co_cmd=0x48,
        reset_co_cmd=0x36,
        mailbox="mp1",
        co_range=(-30, 0),
    ),
    CPUGeneration.ZEN3D_WARHOL: SMUCommandSet(
        generation=CPUGeneration.ZEN3D_WARHOL,
        set_co_cmd=0x35,
        get_co_cmd=0x48,
        reset_co_cmd=0x36,
        mailbox="mp1",
        co_range=(-30, 0),
    ),
    CPUGeneration.ZEN4_RAPHAEL: SMUCommandSet(
        generation=CPUGeneration.ZEN4_RAPHAEL,
        set_co_cmd=0x6,
        get_co_cmd=0xD5,
        reset_co_cmd=None,
        mailbox="rsmu",
        co_range=(-50, 10),
    ),
    CPUGeneration.ZEN5_GRANITE_RIDGE: SMUCommandSet(
        generation=CPUGeneration.ZEN5_GRANITE_RIDGE,
        set_co_cmd=0x6,
        get_co_cmd=0xD5,
        reset_co_cmd=None,
        mailbox="rsmu",
        co_range=(-60, 10),
        set_boost_limit_cmd=0x70,
        get_boost_limit_cmd=0x6E,
    ),
}


def detect_generation(family: int, model: int, model_name: str) -> CPUGeneration:
    """Detect CPU generation from CPUID family/model and model name."""
    name_lower = model_name.lower()

    # AMD family 0x19 (25) = Zen 3/4
    if family == 25:
        if "x3d" in name_lower or "5800x3d" in name_lower:
            return CPUGeneration.ZEN3D_WARHOL
        if model in range(0x20, 0x30):  # Vermeer
            return CPUGeneration.ZEN3_VERMEER
        if model in range(0x40, 0x50):  # Cezanne
            return CPUGeneration.ZEN3_CEZANNE
        if model in range(0x60, 0x80):  # Raphael/Zen 4
            return CPUGeneration.ZEN4_RAPHAEL
        return CPUGeneration.ZEN3_VERMEER  # fallback for family 25

    # AMD family 0x1A (26) = Zen 5
    if family == 26:
        if "strix" in name_lower or "9050" in name_lower:
            return CPUGeneration.ZEN5_STRIX_POINT
        return CPUGeneration.ZEN5_GRANITE_RIDGE

    return CPUGeneration.UNKNOWN


def get_commands(generation: CPUGeneration) -> SMUCommandSet | None:
    """Get SMU commands for a CPU generation."""
    return COMMAND_SETS.get(generation)


def encode_co_arg(core_id: int, value: int, generation: CPUGeneration) -> int:
    """Encode core ID and CO value into SMU command argument."""
    match generation:
        case CPUGeneration.ZEN3_VERMEER | CPUGeneration.ZEN3D_WARHOL:
            # Zen 3: ((core_id & 8) << 5 | core_id & 7) << 20 | value & 0xFFFF
            return (((core_id & 8) << 5 | core_id & 7) << 20) | (value & 0xFFFF)
        case CPUGeneration.ZEN4_RAPHAEL | CPUGeneration.ZEN5_GRANITE_RIDGE:
            # Zen 4/5 uses same encoding via RSMU
            return (core_id << 20) | (value & 0xFFFF)
        case _:
            raise ValueError(f"Unsupported generation: {generation}")


def decode_co_arg(core_id: int, response: int, generation: CPUGeneration) -> int:
    """Decode CO value from SMU response argument."""
    match generation:
        case CPUGeneration.ZEN3_VERMEER | CPUGeneration.ZEN3D_WARHOL:
            raw = response & 0xFFFF
            return raw if raw < 0x8000 else raw - 0x10000
        case CPUGeneration.ZEN4_RAPHAEL | CPUGeneration.ZEN5_GRANITE_RIDGE:
            raw = response & 0xFFFF
            return raw if raw < 0x8000 else raw - 0x10000
        case _:
            raise ValueError(f"Unsupported generation: {generation}")
