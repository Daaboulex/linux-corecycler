"""Comprehensive tests for SMU command encoding/decoding and generation detection."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.smu.commands import (
    COMMAND_SETS,
    CPUGeneration,
    SMUCommandSet,
    decode_co_arg,
    detect_generation,
    encode_co_arg,
    encode_pbo_limit_arg,
    get_commands,
)

# ===========================================================================
# Command-set completeness
# ===========================================================================


class TestCommandSetCompleteness:
    """Every known CPU generation must have an SMU command set. A generation in the
    enum but absent from COMMAND_SETS makes get_commands() return None, and
    RyzenSMU(commands=None) then crashes — this pins the invariant so a newly added
    generation (or routing model 0x70 to ZEN5_STRIX_HALO) can't ship that gap."""

    def test_every_non_unknown_generation_has_a_command_set(self):
        missing = [
            g.name for g in CPUGeneration
            if g is not CPUGeneration.UNKNOWN and get_commands(g) is None
        ]
        assert missing == [], f"generations with no SMU command set: {missing}"

    def test_unknown_has_no_command_set(self):
        assert get_commands(CPUGeneration.UNKNOWN) is None


# ===========================================================================
# Generation detection
# ===========================================================================


class TestDetectGeneration:
    @pytest.mark.parametrize(
        "family,model,name,expected",
        [
            # Zen 2
            (23, 0x71, "AMD Ryzen 9 3950X", CPUGeneration.ZEN2_MATISSE),
            # Zen 3 Vermeer
            (25, 0x21, "AMD Ryzen 9 5950X", CPUGeneration.ZEN3_VERMEER),
            (25, 0x21, "AMD Ryzen 7 5800X", CPUGeneration.ZEN3_VERMEER),
            (25, 0x2F, "AMD Ryzen 5 5600X", CPUGeneration.ZEN3_VERMEER),
            # Zen 3D (X3D)
            (25, 0x21, "AMD Ryzen 7 5800X3D", CPUGeneration.ZEN3D_WARHOL),
            (25, 0x21, "AMD Ryzen 7 5800x3d", CPUGeneration.ZEN3D_WARHOL),
            # Zen 3 Cezanne (APU)
            (25, 0x50, "AMD Ryzen 7 5700G", CPUGeneration.ZEN3_CEZANNE),
            # Zen 4 Raphael
            (25, 0x61, "AMD Ryzen 9 7950X", CPUGeneration.ZEN4_RAPHAEL),
            # Zen 4 Phoenix
            (25, 0x74, "AMD Ryzen 7 7840U", CPUGeneration.ZEN4_PHOENIX),
            (25, 0x75, "AMD Ryzen 7 8845HS", CPUGeneration.ZEN4_PHOENIX),
            # Zen 4 Storm Peak (TR)
            (25, 0x18, "AMD Ryzen Threadripper 7980X", CPUGeneration.ZEN4_STORM_PEAK),
            # Rembrandt -> Cezanne fallback
            (25, 0x44, "AMD Ryzen 7 6800H", CPUGeneration.ZEN3_CEZANNE),
            # Family 25 fallback
            (25, 0x10, "Unknown AMD CPU", CPUGeneration.ZEN3_VERMEER),
            # Zen 5 Granite Ridge
            (26, 0x44, "AMD Ryzen 9 9950X3D", CPUGeneration.ZEN5_GRANITE_RIDGE),
            (26, 0x44, "AMD Ryzen 9 9950X", CPUGeneration.ZEN5_GRANITE_RIDGE),
            (26, 0x01, "AMD Ryzen 7 9700X", CPUGeneration.ZEN5_GRANITE_RIDGE),
            # Zen 5 Strix Point (APU)
            (26, 0x24, "AMD Ryzen AI 9 HX 370", CPUGeneration.ZEN5_STRIX_POINT),
            # Zen 5 ThreadRipper
            (26, 0x44, "AMD Ryzen Threadripper something", CPUGeneration.ZEN5_SHIMADA_PEAK),
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
    def test_zen2_matisse(self):
        cmds = get_commands(CPUGeneration.ZEN2_MATISSE)
        assert cmds is not None
        assert cmds.has_co is False  # Zen 2 has no CO
        assert cmds.has_pbo_limits is True
        assert cmds.set_ppt_cmd == 0x53
        assert cmds.mailbox == "rsmu"

    def test_zen3_vermeer(self):
        cmds = get_commands(CPUGeneration.ZEN3_VERMEER)
        assert cmds is not None
        assert cmds.has_co is True
        assert cmds.set_co_cmd == 0x35
        assert cmds.get_co_cmd == 0x48
        assert cmds.set_all_co_cmd == 0x36
        assert cmds.mailbox == "mp1"
        assert cmds.co_range == (-30, 30)

    def test_zen3d_warhol(self):
        cmds = get_commands(CPUGeneration.ZEN3D_WARHOL)
        assert cmds is not None
        assert cmds.has_co is True
        assert cmds.set_co_cmd == 0x35
        assert cmds.mailbox == "mp1"
        assert cmds.co_range == (-30, 30)

    def test_zen4_raphael(self):
        cmds = get_commands(CPUGeneration.ZEN4_RAPHAEL)
        assert cmds is not None
        assert cmds.has_co is True
        assert cmds.set_co_cmd == 0x06
        assert cmds.get_co_cmd == 0xD5
        assert cmds.mailbox == "rsmu"
        assert cmds.co_range == (-50, 30)
        assert cmds.set_boost_limit_cmd == 0x70
        assert cmds.get_boost_limit_cmd == 0x6E

    def test_zen5_granite_ridge(self):
        cmds = get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)
        assert cmds is not None
        assert cmds.has_co is True
        assert cmds.set_co_cmd == 0x06
        assert cmds.get_co_cmd == 0xD5
        assert cmds.co_range == (-50, 10)
        assert cmds.mailbox == "rsmu"
        assert cmds.set_boost_limit_cmd == 0x70
        assert cmds.get_boost_limit_cmd == 0x6E

    def test_zen5_shimada_peak_different_get_co(self):
        """Shimada Peak uses a different get CO command (0xA3 vs 0xD5)."""
        cmds = get_commands(CPUGeneration.ZEN5_SHIMADA_PEAK)
        assert cmds is not None
        assert cmds.get_co_cmd == 0xA3
        assert cmds.get_ln2_mode_cmd == 0xA6

    @pytest.mark.parametrize(
        "gen",
        [
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
            assert cmds.mailbox in ("rsmu", "mp1")
            lo, hi = cmds.co_range
            assert lo <= hi


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
    @pytest.mark.parametrize("value", [-30, -20, -15, -10, -5, -1, 0, 10, 30])
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
        """Cores >= 8 derive CCD1 from core_id when topology is not supplied."""
        encoded_0 = encode_co_arg(0, 0, self.gen)
        encoded_8 = encode_co_arg(8, 0, self.gen)
        assert encoded_8 != encoded_0
        # core 8 -> CCD 1 (bits [31:28]), core_in_ccd 0 (bits [23:20])
        assert (encoded_8 >> 28) & 0xF == 1
        assert (encoded_8 >> 20) & 0xF == 0

    def test_harvested_slot_and_ccd_override_core_id(self):
        """Zen 3 must honor the topology-detected CCD and probed slot, not derive
        them from core_id. On a harvested/2-CCD part (5900X 6+6, 5600X 6-of-8) the
        kernel renumbers cores contiguously, so deriving the slot from core_id alone
        targets the WRONG physical SMU slot — a silent wrong-core write."""
        # Logical core 6 is physically CCD1, slot 0 (5900X). Must encode CCD1/slot0,
        # NOT the core_id-derived CCD0/slot6.
        encoded = encode_co_arg(6, -30, self.gen, ccd=1, slot=0)
        assert (encoded >> 28) & 0xF == 1   # CCD1, not CCD0
        assert (encoded >> 20) & 0xF == 0   # slot 0, not slot 6
        assert (encoded & 0xFFFF) == (-30 & 0xFFFF)
        # And it must match the Zen 4/5 encoding for the same authoritative topology.
        assert encoded == encode_co_arg(6, -30, CPUGeneration.ZEN4_RAPHAEL, ccd=1, slot=0)


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
    @pytest.mark.parametrize("value", [-50, -30, -10, -1, 0, 5, 10, 30])
    def test_roundtrip(self, core_id, value):
        encoded = encode_co_arg(core_id, value, self.gen)
        decoded = decode_co_arg(core_id, encoded, self.gen)
        assert decoded == value

    def test_encoding_format(self):
        """Zen 4 uses CCD-aware encoding: (ccd << 28) | (core_in_ccd << 20) | margin."""
        encoded = encode_co_arg(5, -10, self.gen)
        # core 5 is in CCD 0, core_in_ccd = 5
        assert (encoded >> 20) & 0xF == 5
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

    def test_ccd_encoding(self):
        """Core 8+ should encode with CCD=1."""
        encoded = encode_co_arg(8, -10, self.gen)
        ccd = (encoded >> 28) & 0xF
        core_in_ccd = (encoded >> 20) & 0xF
        assert ccd == 1
        assert core_in_ccd == 0

    def test_core_15_ccd_encoding(self):
        """Core 15 should be CCD1, core_in_ccd=7."""
        encoded = encode_co_arg(15, 0, self.gen)
        ccd = (encoded >> 28) & 0xF
        core_in_ccd = (encoded >> 20) & 0xF
        assert ccd == 1
        assert core_in_ccd == 7


class TestEncodeDecodeHarvested:
    """Test slot parameter for harvested Zen 4/5 chips."""

    gen = CPUGeneration.ZEN5_GRANITE_RIDGE

    def test_slot_overrides_core_mod_8(self):
        """Harvested CCD: core 2 maps to physical slot 4, not slot 2."""
        encoded = encode_co_arg(2, -10, self.gen, ccd=0, slot=4)
        core_in_ccd = (encoded >> 20) & 0xF
        ccd = (encoded >> 28) & 0xF
        assert core_in_ccd == 4
        assert ccd == 0

    def test_slot_none_falls_back_to_mod_8(self):
        encoded_default = encode_co_arg(5, -10, self.gen, ccd=0)
        encoded_explicit = encode_co_arg(5, -10, self.gen, ccd=0, slot=5)
        assert encoded_default == encoded_explicit

    def test_9900x_ccd0_mapping(self):
        """Simulate 9900X CCD0: slots 2,3 harvested → cores 0-5 map to 0,1,4,5,6,7."""
        slot_map = {0: 0, 1: 1, 2: 4, 3: 5, 4: 6, 5: 7}
        for kernel_core, phys_slot in slot_map.items():
            encoded = encode_co_arg(kernel_core, -10, self.gen, ccd=0, slot=phys_slot)
            assert (encoded >> 20) & 0xF == phys_slot
            assert (encoded >> 28) & 0xF == 0

    def test_9900x_ccd1_mapping(self):
        """Simulate 9900X CCD1: slots 4,5 harvested → cores 8-13 map to 0,1,2,3,6,7."""
        slot_map = {8: 0, 9: 1, 10: 2, 11: 3, 12: 6, 13: 7}
        for kernel_core, phys_slot in slot_map.items():
            encoded = encode_co_arg(kernel_core, -10, self.gen, ccd=1, slot=phys_slot)
            assert (encoded >> 20) & 0xF == phys_slot
            assert (encoded >> 28) & 0xF == 1

    def test_roundtrip_with_slot(self):
        for slot in range(8):
            encoded = encode_co_arg(0, -30, self.gen, ccd=0, slot=slot)
            decoded = decode_co_arg(0, encoded, self.gen)
            assert decoded == -30

    def test_slot_honored_for_zen3(self):
        """Zen 3 honors the probed physical slot (was a bug: it ignored it, so a
        harvested 5600X/5900X wrote the wrong core)."""
        gen = CPUGeneration.ZEN3_VERMEER
        enc_default = encode_co_arg(3, -10, gen)
        enc_with_slot = encode_co_arg(3, -10, gen, slot=5)
        assert enc_default != enc_with_slot
        assert (enc_with_slot >> 20) & 0xF == 5

    def test_slot_with_zen4(self):
        gen = CPUGeneration.ZEN4_RAPHAEL
        encoded = encode_co_arg(2, -10, gen, ccd=0, slot=6)
        assert (encoded >> 20) & 0xF == 6


class TestEncodeDecodeEdgeCases:
    def test_unsupported_generation_encode(self):
        with pytest.raises(ValueError, match="Unsupported generation"):
            encode_co_arg(0, 0, CPUGeneration.UNKNOWN)

    def test_unsupported_generation_decode(self):
        with pytest.raises(ValueError, match="Unsupported generation"):
            decode_co_arg(0, 0, CPUGeneration.UNKNOWN)

    def test_zen2_encode_raises(self):
        """Zen 2 doesn't support CO, encoding should raise."""
        with pytest.raises(ValueError, match="does not support Curve Optimizer"):
            encode_co_arg(0, 0, CPUGeneration.ZEN2_MATISSE)

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

    def test_has_co_property(self):
        """has_co should be True for gens with CO, False for Zen 2."""
        assert get_commands(CPUGeneration.ZEN2_MATISSE).has_co is False
        assert get_commands(CPUGeneration.ZEN3_VERMEER).has_co is True
        assert get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE).has_co is True

    def test_has_pbo_limits_property(self):
        """All generations should have PBO limit support."""
        for gen, cmds in COMMAND_SETS.items():
            assert cmds.has_pbo_limits is True, f"{gen} should have PBO limits"


# ===========================================================================
# CPUGeneration enum tests
# ===========================================================================


class TestCPUGeneration:
    def test_all_generations_defined(self):
        expected = {
            "ZEN2_MATISSE",
            "ZEN2_CASTLE_PEAK",
            "ZEN3_VERMEER",
            "ZEN3_CEZANNE",
            "ZEN3D_WARHOL",
            "ZEN4_RAPHAEL",
            "ZEN4_PHOENIX",
            "ZEN4_DRAGON_RANGE",
            "ZEN4_STORM_PEAK",
            "ZEN5_GRANITE_RIDGE",
            "ZEN5_STRIX_POINT",
            "ZEN5_STRIX_HALO",
            "ZEN5_SHIMADA_PEAK",
            "UNKNOWN",
        }
        actual = {g.name for g in CPUGeneration}
        assert actual == expected


class TestDetectGenerationDriftEdges:
    def test_zen2_castle_peak_model_0x31(self):
        assert detect_generation(23, 0x31, "AMD Ryzen Threadripper 3960X") == CPUGeneration.ZEN2_CASTLE_PEAK

    def test_family23_unknown_model_falls_back_to_matisse(self):
        assert detect_generation(23, 0x01, "AMD Ryzen 7 1700 Eight-Core") == CPUGeneration.ZEN2_MATISSE

    def test_zen4_x3d_model_in_raphael_range(self):
        assert detect_generation(25, 0x61, "AMD Ryzen 9 7950X3D 16-Core Processor") == CPUGeneration.ZEN4_RAPHAEL

    def test_zen5_strix_halo_model_0x70(self):
        assert detect_generation(26, 0x70, "AMD Ryzen AI Max+ 395") == CPUGeneration.ZEN5_STRIX_POINT

    def test_decode_co_arg_rejects_zen2(self):
        with pytest.raises(ValueError, match="Unsupported generation"):
            decode_co_arg(0, 0x0, CPUGeneration.ZEN2_MATISSE)

    def test_encode_pbo_limit_arg_watts_to_milliwatts(self):
        assert encode_pbo_limit_arg(225) == 225000
        assert encode_pbo_limit_arg(0) == 0
