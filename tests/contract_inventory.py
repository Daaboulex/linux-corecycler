"""External-contract inventory -- the single enumerated list of every assumption
CoreCycler makes about hardware, external tools, and OS interfaces that can change
out from under it, each tied to the drift-test that re-verifies it.

Ring A pins are hermetic: they run in every gate (the nix sandbox included) and
catch an accidental EDIT to a pinned constant. Ring B live tests run the REAL
binary or hardware (marked slow + contract, run outside the sandbox on real
hardware) and catch REALITY drift -- a tool, kernel, or silicon change. A contract
that can be verified live must name a Ring B test; test_contracts.py fails on any
live-verifiable contract with no wired Ring B test, so no drift seam sits dormant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from corecycler.monitor.memory import parse_dmidecode_output
from corecycler.monitor.msr import (
    MSR_APERF,
    MSR_CORE_ENERGY,
    MSR_MPERF,
    MSR_PKG_ENERGY,
    MSR_PWR_UNIT,
)
from corecycler.smu.commands import (
    CPUGeneration,
    decode_co_arg,
    detect_generation,
    encode_co_arg,
    get_commands,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_ZEN5 = CPUGeneration.ZEN5_GRANITE_RIDGE


@dataclass(frozen=True, slots=True)
class Contract:
    """One external assumption and how its drift is detected."""

    name: str
    kind: str
    source: str
    ring_a: Callable[[], None]
    live_verifiable: bool
    ring_b_test: str | None = None


def _pin_co_bit_layout() -> None:
    assert encode_co_arg(0, 0, _ZEN5) == 0
    assert encode_co_arg(8, 0, _ZEN5) == (1 << 28)
    assert encode_co_arg(1, 0, _ZEN5) == (1 << 20)
    assert encode_co_arg(15, 0, _ZEN5) == (1 << 28) | (7 << 20)
    for value in (-50, -30, -1, 0, 1, 10):
        arg = encode_co_arg(3, value, _ZEN5)
        assert decode_co_arg(3, arg, _ZEN5) == value


def _pin_detect_generation_map() -> None:
    cases = [
        (23, 0x71, "AMD Ryzen 9 3950X 16-Core Processor", CPUGeneration.ZEN2_MATISSE),
        (23, 0x31, "AMD Ryzen Threadripper 3960X", CPUGeneration.ZEN2_CASTLE_PEAK),
        (25, 0x21, "AMD Ryzen 9 5950X 16-Core Processor", CPUGeneration.ZEN3_VERMEER),
        (25, 0x61, "AMD Ryzen 9 7950X3D 16-Core Processor", CPUGeneration.ZEN4_RAPHAEL),
        (26, 0x44, "AMD Ryzen 9 9950X3D 16-Core Processor", CPUGeneration.ZEN5_GRANITE_RIDGE),
        (26, 0x24, "AMD Ryzen AI 9 HX 370", CPUGeneration.ZEN5_STRIX_POINT),
        (6, 0xA7, "13th Gen Intel(R) Core(TM) i9-13900K", CPUGeneration.UNKNOWN),
    ]
    for family, model, name, expected in cases:
        assert detect_generation(family, model, name) == expected, (family, model, name)
    assert get_commands(_ZEN5) is not None


def _pin_msr_addresses() -> None:
    assert MSR_APERF == 0xE8
    assert MSR_MPERF == 0xE7
    assert MSR_PWR_UNIT == 0xC0010299
    assert MSR_CORE_ENERGY == 0xC001029A
    assert MSR_PKG_ENERGY == 0xC001029B


def _pin_dmidecode_dimm_parse() -> None:
    sample = (
        "Handle 0x0001, DMI type 17, 92 bytes\nMemory Device\n"
        "\tSize: 32 GB\n\tLocator: DIMM 0\n\tType: DDR5\n"
        "\tSpeed: 6000 MT/s\n\tConfigured Memory Speed: 6000 MT/s\n"
    )
    dimms = parse_dmidecode_output(sample)
    assert len(dimms) == 1
    assert dimms[0].size_gb == 32
    assert dimms[0].speed_mt == 6000
    assert dimms[0].mem_type == "DDR5"


def _pin_zen5_co_range() -> None:
    for gen in (
        CPUGeneration.ZEN5_GRANITE_RIDGE,
        CPUGeneration.ZEN5_STRIX_POINT,
        CPUGeneration.ZEN5_SHIMADA_PEAK,
    ):
        cmds = get_commands(gen)
        assert cmds is not None
        assert cmds.co_range == (-50, 10), (gen.name, cmds.co_range)


CONTRACTS: list[Contract] = [
    Contract(
        name="smu-co-bit-layout",
        kind="arch",
        source="ryzen_smu / ZenStates-Core CO arg layout: [31:28]=CCD [23:20]=core [15:0]=margin",
        ring_a=_pin_co_bit_layout,
        live_verifiable=False,
    ),
    Contract(
        name="detect-generation-cpuid-map",
        kind="arch",
        source="AMD CPUID Fn0000_0001 family/model + PPR codename ranges",
        ring_a=_pin_detect_generation_map,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_real_cpu_resolves_to_supported_generation",
    ),
    Contract(
        name="msr-register-addresses",
        kind="arch",
        source="AMD PPR Fam19h/1Ah APERF 0xE8 MPERF 0xE7 RAPL 0xC0010299/029A/029B",
        ring_a=_pin_msr_addresses,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_msr_reads_are_plausible",
    ),
    Contract(
        name="dmidecode-dimm-format",
        kind="os",
        source="dmidecode -t memory DMI type 17 field names (Size/Locator/Type/Speed)",
        ring_a=_pin_dmidecode_dimm_parse,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_dmidecode_parses_real_dimms",
    ),
    Contract(
        name="zen5-co-range",
        kind="arch",
        source="ZenStates-Core Utils.cs clamps Zen4+ CO to [-50,50]; 9950X3D read-back rejects -60",
        ring_a=_pin_zen5_co_range,
        live_verifiable=False,
    ),
]
