"""Comprehensive tests for SMU command encoding/decoding and generation detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from smu.commands import (
    COMMAND_SETS,
    CPUGeneration,
    SMUCommandSet,
    decode_co_arg,
    detect_generation,
    encode_co_arg,
    get_commands,
)


# ===========================================================================
# Generation detection
# ===========================================================================


class TestDetectGeneration:
    @pytest.mark.parametrize(
        "family,model,name,expected",
        [
            # Zen 3 Vermeer
            (25, 0x21, "AMD Ryzen 9 5950X", CPUGeneration.ZEN3_VERMEER),
            (25, 0x21, "AMD Ryzen 7 5800X", CPUGeneration.ZEN3_VERMEER),
            (25, 0x2F, "AMD Ryzen 5 5600X", CPUGeneration.ZEN3_VERMEER),
            # Zen 3D (X3D)
            (25, 0x21, "AMD Ryzen 7 5800X3D", CPUGeneration.ZEN3D_WARHOL),
            (25, 0x21, "AMD Ryzen 7 5800x3d", CPUGeneration.ZEN3D_WARHOL),
            # Zen 3 Cezanne (APU)
            (25, 0x44, "AMD Ryzen 7 5700G", CPUGeneration.ZEN3_CEZANNE),
            (25, 0x40, "AMD Ryzen 5 5600G", CPUGeneration.ZEN3_CEZANNE),
            # Zen 4 Raphael
            (25, 0x61, "AMD Ryzen 9 7950X", CPUGeneration.ZEN4_RAPHAEL),
            (25, 0x70, "AMD Ryzen 7 7700X", CPUGeneration.ZEN4_RAPHAEL),
            # Family 25 fallback
            (25, 0x10, "Unknown AMD CPU", CPUGeneration.ZEN3_VERMEER),
            # Zen 5 Granite Ridge
            (26, 0x44, "AMD Ryzen 9 9950X3D", CPUGeneration.ZEN5_GRANITE_RIDGE),
            (26, 0x44, "AMD Ryzen 9 9950X", CPUGeneration.ZEN5_GRANITE_RIDGE),
            (26, 0x01, "AMD Ryzen 7 9700X", CPUGeneration.ZEN5_GRANITE_RIDGE),
            # Zen 5 Strix Point (APU)
            (26, 0x44, "AMD Ryzen AI 9 HX 9050", CPUGeneration.ZEN5_STRIX_POINT),
            (26, 0x44, "Strix Point something", CPUGeneration.ZEN5_STRIX_POINT),
            # Unknown
            (6, 167, "Intel Core i9-10900K", CPUGeneration.UNKNOWN),
            (0, 0, "", CPUGeneration.UNKNOWN),
        ],
    )
    def test_detect_generation(self, family, model, name, expected):
        result = detect_generation(family, model, name)
        assert result == expected

    def test_x3d_takes_precedence_over_model_range(self):
        """X3D in name should override model range check."""
        gen = detect_generation(25, 0x21, "AMD Ryzen 7 5800X3D 8-Core")
        assert gen == CPUGeneration.ZEN3D_WARHOL


# ===========================================================================
# Command set lookups
# ===========================================================================


class TestGetCommands:
    def test_zen3_vermeer(self):
        cmds = get_commands(CPUGeneration.ZEN3_VERMEER)
        assert cmds is not None
        assert cmds.set_co_cmd == 0x35
        assert cmds.get_co_cmd == 0x48
        assert cmds.reset_co_cmd == 0x36
        assert cmds.mailbox == "mp1"
        assert cmds.co_range == (-30, 0)
        assert cmds.set_boost_limit_cmd is None
        assert cmds.get_boost_limit_cmd is None

    def test_zen3d_warhol(self):
        cmds = get_commands(CPUGeneration.ZEN3D_WARHOL)
        assert cmds is not None
        assert cmds.set_co_cmd == 0x35
        assert cmds.mailbox == "mp1"
        assert cmds.co_range == (-30, 0)

    def test_zen4_raphael(self):
        cmds = get_commands(CPUGeneration.ZEN4_RAPHAEL)
        assert cmds is not None
        assert cmds.set_co_cmd == 0x6
        assert cmds.get_co_cmd == 0xD5
        assert cmds.reset_co_cmd is None
        assert cmds.mailbox == "rsmu"
        assert cmds.co_range == (-50, 10)

    def test_zen5_granite_ridge(self):
        cmds = get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)
        assert cmds is not None
        assert cmds.set_co_cmd == 0x6
        assert cmds.get_co_cmd == 0xD5
        assert cmds.co_range == (-60, 10)
        assert cmds.mailbox == "rsmu"
        assert cmds.set_boost_limit_cmd == 0x70
        assert cmds.get_boost_limit_cmd == 0x6E

    @pytest.mark.parametrize(
        "gen",
        [
            CPUGeneration.ZEN3_CEZANNE,
            CPUGeneration.ZEN4D_PHOENIX,
            CPUGeneration.ZEN4_DRAGON_RANGE,
            CPUGeneration.ZEN5_STRIX_POINT,
            CPUGeneration.UNKNOWN,
        ],
    )
    def test_unsupported_generations_return_none(self, gen):
        assert get_commands(gen) is None

    def test_command_sets_all_valid(self):
        """Every entry in COMMAND_SETS should be a well-formed SMUCommandSet."""
        for gen, cmds in COMMAND_SETS.items():
            assert isinstance(cmds, SMUCommandSet)
            assert cmds.generation == gen
            assert cmds.set_co_cmd > 0
            assert cmds.get_co_cmd > 0
            assert cmds.mailbox in ("rsmu", "mp1")
            lo, hi = cmds.co_range
            assert lo < hi


# ===========================================================================
# Encode / decode round-trip tests
# ===========================================================================


class TestEncodeDecodeZen3:
    gen = CPUGeneration.ZEN3_VERMEER

    @pytest.mark.parametrize("core_id", range(16))
    def test_roundtrip_zero(self, core_id):
        encoded = encode_co_arg(core_id, 0, self.gen)
        decoded = decode_co_arg(core_id, encoded, self.gen)
        assert decoded == 0

    @pytest.mark.parametrize("core_id", [0, 1, 7, 8, 9, 15])
    @pytest.mark.parametrize("value", [-30, -20, -15, -10, -5, -1, 0])
    def test_roundtrip_all_valid_values(self, core_id, value):
        encoded = encode_co_arg(core_id, value, self.gen)
        decoded = decode_co_arg(core_id, encoded, self.gen)
        assert decoded == value

    def test_negative_value_encoding(self):
        """Negative values should be encoded with two's complement in lower 16 bits."""
        encoded = encode_co_arg(0, -1, self.gen)
        lower_16 = encoded & 0xFFFF
        assert lower_16 == 0xFFFF

    def test_core_id_above_7_encoding(self):
        """Zen 3 encoding uses (core_id & 8) << 5 for cores >= 8."""
        encoded_0 = encode_co_arg(0, 0, self.gen)
        encoded_8 = encode_co_arg(8, 0, self.gen)
        # core 8: ((8 & 8) << 5 | 8 & 7) << 20 = (256 | 0) << 20
        assert encoded_8 != encoded_0
        # core 8 has bit pattern 0x100 << 20 in the upper bits
        assert (encoded_8 >> 20) == 0x100


class TestEncodeDecodeZen3D:
    gen = CPUGeneration.ZEN3D_WARHOL

    @pytest.mark.parametrize("core_id", [0, 4, 7])
    @pytest.mark.parametrize("value", [-30, -15, 0])
    def test_roundtrip(self, core_id, value):
        encoded = encode_co_arg(core_id, value, self.gen)
        decoded = decode_co_arg(core_id, encoded, self.gen)
        assert decoded == value


class TestEncodeDecodeZen4:
    gen = CPUGeneration.ZEN4_RAPHAEL

    @pytest.mark.parametrize("core_id", [0, 1, 7, 8, 15, 31])
    @pytest.mark.parametrize("value", [-50, -30, -10, -1, 0, 5, 10])
    def test_roundtrip(self, core_id, value):
        encoded = encode_co_arg(core_id, value, self.gen)
        decoded = decode_co_arg(core_id, encoded, self.gen)
        assert decoded == value

    def test_encoding_format(self):
        """Zen 4 uses (core_id << 20) | (value & 0xFFFF)."""
        encoded = encode_co_arg(5, -10, self.gen)
        assert (encoded >> 20) == 5
        assert (encoded & 0xFFFF) == (-10 & 0xFFFF)


class TestEncodeDecodeZen5:
    gen = CPUGeneration.ZEN5_GRANITE_RIDGE

    @pytest.mark.parametrize("core_id", [0, 1, 7, 8, 15, 16, 31])
    @pytest.mark.parametrize("value", [-60, -50, -30, -10, -1, 0, 5, 10])
    def test_roundtrip(self, core_id, value):
        encoded = encode_co_arg(core_id, value, self.gen)
        decoded = decode_co_arg(core_id, encoded, self.gen)
        assert decoded == value

    def test_positive_value(self):
        encoded = encode_co_arg(0, 10, self.gen)
        decoded = decode_co_arg(0, encoded, self.gen)
        assert decoded == 10

    def test_max_negative(self):
        encoded = encode_co_arg(0, -60, self.gen)
        decoded = decode_co_arg(0, encoded, self.gen)
        assert decoded == -60

    def test_boundary_values(self):
        """Test exact boundary of CO range for Zen 5."""
        for val in [-60, 10]:
            encoded = encode_co_arg(0, val, self.gen)
            decoded = decode_co_arg(0, encoded, self.gen)
            assert decoded == val


class TestEncodeDecodeEdgeCases:
    def test_unsupported_generation_encode(self):
        with pytest.raises(ValueError, match="Unsupported generation"):
            encode_co_arg(0, 0, CPUGeneration.UNKNOWN)

    def test_unsupported_generation_decode(self):
        with pytest.raises(ValueError, match="Unsupported generation"):
            decode_co_arg(0, 0, CPUGeneration.UNKNOWN)

    def test_unsupported_generation_cezanne_encode(self):
        with pytest.raises(ValueError):
            encode_co_arg(0, 0, CPUGeneration.ZEN3_CEZANNE)

    def test_large_core_id_zen5(self):
        """Core IDs up to 31 should work for Zen 5 (16-core with SMT)."""
        gen = CPUGeneration.ZEN5_GRANITE_RIDGE
        for core_id in range(32):
            encoded = encode_co_arg(core_id, -10, gen)
            decoded = decode_co_arg(core_id, encoded, gen)
            assert decoded == -10

    def test_twos_complement_boundary(self):
        """Value 0x7FFF should decode as positive, 0x8000 as negative."""
        gen = CPUGeneration.ZEN5_GRANITE_RIDGE
        # 0x7FFF = 32767 (positive)
        assert decode_co_arg(0, 0x7FFF, gen) == 32767
        # 0x8000 = -32768 (negative, two's complement)
        assert decode_co_arg(0, 0x8000, gen) == -32768


# ===========================================================================
# SMUCommandSet dataclass tests
# ===========================================================================


class TestSMUCommandSet:
    def test_frozen(self):
        cmds = get_commands(CPUGeneration.ZEN3_VERMEER)
        with pytest.raises(AttributeError):
            cmds.set_co_cmd = 0xFF  # type: ignore[misc]

    def test_co_range_semantics(self):
        """co_range[0] is min (most negative), co_range[1] is max."""
        for gen, cmds in COMMAND_SETS.items():
            lo, hi = cmds.co_range
            assert lo <= 0, f"{gen}: min CO should be <= 0"
            assert hi >= 0, f"{gen}: max CO should be >= 0"


# ===========================================================================
# CPUGeneration enum tests
# ===========================================================================


class TestCPUGeneration:
    def test_all_generations_defined(self):
        expected = {
            "ZEN3_VERMEER",
            "ZEN3_CEZANNE",
            "ZEN3D_WARHOL",
            "ZEN4_RAPHAEL",
            "ZEN4D_PHOENIX",
            "ZEN4_DRAGON_RANGE",
            "ZEN5_GRANITE_RIDGE",
            "ZEN5_STRIX_POINT",
            "UNKNOWN",
        }
        actual = {g.name for g in CPUGeneration}
        assert actual == expected
