"""Property-based fuzzing of the SMU encode/decode and generation detection.

These are the most safety-critical pure functions in the codebase: encode_co_arg
turns (core, value) into the exact SMU argument that writes a voltage offset to a
specific physical core. A bit-packing bug here is catastrophic and NOT caught by
the driver's read-back check (the read re-uses the same encoding, so a consistently
wrong core target reads back "correct" and silently undervolts the wrong core).
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.smu.commands import (  # noqa: E402
    COMMAND_SETS,
    CPUGeneration,
    decode_co_arg,
    detect_generation,
    encode_boost_limit_arg,
    encode_co_arg,
    encode_pbo_scalar_arg,
    get_commands,
)

# Generations that support Curve Optimizer (have an encoding scheme != "none").
_CO_GENS = [
    g for g, cs in COMMAND_SETS.items()
    if cs.encoding_scheme in ("zen3", "zen4_5")
]
_ALL_GENS = list(CPUGeneration)


class TestCoArgRoundTrip:
    @settings(max_examples=400, deadline=None)
    @given(gen=st.sampled_from(_CO_GENS),
           core_id=st.integers(min_value=0, max_value=63),
           value=st.data())
    def test_decode_inverts_encode_over_full_co_range(self, gen, core_id, value):
        """decode(encode(v)) == v for every value the generation supports."""
        co_min, co_max = COMMAND_SETS[gen].co_range
        v = value.draw(st.integers(min_value=co_min, max_value=co_max))
        arg = encode_co_arg(core_id, v, gen)
        assert decode_co_arg(core_id, arg, gen) == v

    @settings(max_examples=400, deadline=None)
    @given(gen=st.sampled_from(_CO_GENS),
           core_id=st.integers(min_value=0, max_value=63),
           v=st.integers(min_value=-32768, max_value=32767))
    def test_decode_inverts_encode_over_full_int16(self, gen, core_id, v):
        """Round-trip holds across the entire 16-bit signed space, not just the
        documented CO range (defends against an out-of-range value leaking through)."""
        arg = encode_co_arg(core_id, v, gen)
        assert decode_co_arg(core_id, arg, gen) == v


class TestCoArgTargeting:
    """The CCD/core bits must address the intended physical core. A silent
    wrong-core write is the worst failure mode and read-back cannot catch it."""

    @settings(max_examples=400, deadline=None)
    @given(core_id=st.integers(min_value=0, max_value=63),
           v=st.integers(min_value=-60, max_value=30),
           ccd=st.one_of(st.none(), st.integers(min_value=0, max_value=7)),
           slot=st.one_of(st.none(), st.integers(min_value=0, max_value=7)))
    def test_zen4_5_bits_address_the_right_core(self, core_id, v, ccd, slot):
        gen = CPUGeneration.ZEN5_GRANITE_RIDGE
        arg = encode_co_arg(core_id, v, gen, ccd=ccd, slot=slot)
        exp_ccd = ccd if ccd is not None else core_id // 8
        exp_core = slot if slot is not None else core_id % 8
        assert (arg >> 28) & 0xF == exp_ccd, f"CCD bits wrong: {arg:#x}"
        assert (arg >> 20) & 0xF == exp_core, f"core bits wrong: {arg:#x}"
        assert arg & 0xFFFF == (v & 0xFFFF)
        # The CCD/core bits must not collide with the 16-bit value field.
        assert arg & 0xFFFF == (v & 0xFFFF)
        # Write-target and read-target encode identically (same core addressed).
        read_arg = encode_co_arg(core_id, 0, gen, ccd=ccd, slot=slot)
        assert (read_arg >> 20) == (arg >> 20), "read targets a different core than write"

    @settings(max_examples=200, deadline=None)
    @given(core_id=st.integers(min_value=0, max_value=15),
           v=st.integers(min_value=-30, max_value=30))
    def test_zen3_bits_address_the_right_core(self, core_id, v):
        gen = CPUGeneration.ZEN3_VERMEER
        arg = encode_co_arg(core_id, v, gen)
        assert (arg >> 28) & 1 == (1 if core_id >= 8 else 0), f"CCD bit wrong: {arg:#x}"
        assert (arg >> 20) & 7 == core_id & 7, f"core bits wrong: {arg:#x}"
        assert arg & 0xFFFF == (v & 0xFFFF)


class TestGenerationDetectionRobust:
    @settings(max_examples=500, deadline=None)
    @given(family=st.integers(min_value=0, max_value=255),
           model=st.integers(min_value=0, max_value=255),
           name=st.text(max_size=40))
    def test_detect_never_crashes_and_returns_valid_gen(self, family, model, name):
        gen = detect_generation(family, model, name)
        assert isinstance(gen, CPUGeneration)
        # Anything detected as a real generation must have a command set.
        if gen is not CPUGeneration.UNKNOWN:
            assert get_commands(gen) is not None


class TestNonCoGenerationsRejected:
    def test_zen2_encode_raises(self):
        import pytest
        with pytest.raises(ValueError):
            encode_co_arg(0, -10, CPUGeneration.ZEN2_MATISSE)

    def test_unknown_generation_encode_raises(self):
        import pytest
        with pytest.raises(ValueError):
            encode_co_arg(0, -10, CPUGeneration.UNKNOWN)


class TestOtherEncoders:
    @settings(max_examples=200, deadline=None)
    @given(mhz=st.integers(min_value=0, max_value=1 << 24))
    def test_boost_limit_fits_20_bits(self, mhz):
        arg = encode_boost_limit_arg(mhz)
        assert 0 <= arg <= 0xFFFFF

    @settings(max_examples=200, deadline=None)
    @given(scalar=st.floats(min_value=0.0, max_value=10.0,
                            allow_nan=False, allow_infinity=False))
    def test_pbo_scalar_is_int(self, scalar):
        arg = encode_pbo_scalar_arg(scalar)
        assert isinstance(arg, int)
        assert 0 <= arg <= 1000
