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
    for value in (-60, -30, -1, 0, 1, 10):
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
]
