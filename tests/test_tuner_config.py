"""Tests for TunerConfig dataclass."""

from __future__ import annotations

import json

from tuner.config import TunerConfig


class TestTunerConfigDefaults:
    def test_defaults_are_sensible(self):
        cfg = TunerConfig()
        assert cfg.start_offset == 0
        assert cfg.coarse_step == 5
        assert cfg.fine_step == 1
        assert cfg.direction == -1
        assert cfg.search_duration_seconds == 60
        assert cfg.confirm_duration_seconds == 300
        assert cfg.max_offset == -50
        assert cfg.max_confirm_retries == 2
        assert cfg.cores_to_test is None
        assert cfg.test_order == "sequential"
        assert cfg.backend == "mprime"
        assert cfg.abort_on_consecutive_failures == 0

    def test_json_roundtrip(self):
        cfg = TunerConfig(coarse_step=10, max_offset=-40, cores_to_test=[0, 1, 2])
        json_str = cfg.to_json()
        restored = TunerConfig.from_json(json_str)
        assert restored.coarse_step == 10
        assert restored.max_offset == -40
        assert restored.cores_to_test == [0, 1, 2]
        assert restored.start_offset == cfg.start_offset

    def test_json_roundtrip_defaults(self):
        cfg = TunerConfig()
        restored = TunerConfig.from_json(cfg.to_json())
        assert restored.coarse_step == cfg.coarse_step
        assert restored.direction == cfg.direction
        assert restored.cores_to_test == cfg.cores_to_test

    def test_from_json_ignores_unknown_fields(self):
        data = json.dumps({"coarse_step": 3, "unknown_field": 42})
        cfg = TunerConfig.from_json(data)
        assert cfg.coarse_step == 3

    def test_from_json_fails_closed_on_wrong_typed_fields(self):
        """A corrupted/hand-edited config_json with wrong-typed fields must fall
        back to defaults, not raise later in validate()/the engine. Blind audit
        found from_json passed None/str/int straight into the dataclass, then
        validate() crashed with TypeError (and resume() never validates)."""
        defaults = TunerConfig()
        cases = {
            "hardening_tiers": None,      # was: TypeError 'NoneType' not iterable
            "cores_to_test": 42,          # was: TypeError int has no len()
            "coarse_step": "abc",         # was: TypeError str < int
            "auto_validate": 1,           # int for a bool field
            "max_temperature_c": "hot",   # str for a float field
        }
        for field, bad in cases.items():
            cfg = TunerConfig.from_json(json.dumps({field: bad}))
            assert getattr(cfg, field) == getattr(defaults, field), (
                f"{field}={bad!r} should have reverted to the default"
            )
            cfg.validate()  # must not raise

    def test_from_json_keeps_valid_typed_fields(self):
        """The type guard must not reject legitimate values."""
        cfg = TunerConfig.from_json(json.dumps({
            "coarse_step": 7, "cores_to_test": [0, 2], "auto_validate": False,
            "max_temperature_c": 90.0, "hardening_tiers": [],
        }))
        assert cfg.coarse_step == 7
        assert cfg.cores_to_test == [0, 2]
        assert cfg.auto_validate is False
        assert cfg.max_temperature_c == 90.0
        assert cfg.hardening_tiers == []

    def test_from_json_accepts_json_int_for_float_field(self):
        """JSON has no float/int distinction — a bare int for a float field is
        valid (60 for max_temperature_c), not a type error."""
        cfg = TunerConfig.from_json(json.dumps({"max_temperature_c": 90}))
        assert cfg.max_temperature_c == 90

    def test_clamp_max_offset_negative_direction(self):
        cfg = TunerConfig(max_offset=-100, direction=-1)
        cfg.clamp_max_offset((-60, 10))  # Zen 5
        assert cfg.max_offset == -60

    def test_clamp_max_offset_within_range(self):
        cfg = TunerConfig(max_offset=-40, direction=-1)
        cfg.clamp_max_offset((-60, 10))
        assert cfg.max_offset == -40  # already within range

    def test_clamp_max_offset_positive_direction(self):
        cfg = TunerConfig(max_offset=50, direction=1)
        cfg.clamp_max_offset((-30, 30))  # Zen 3
        assert cfg.max_offset == 30

    def test_clamp_max_offset_zen3_range(self):
        cfg = TunerConfig(max_offset=-50, direction=-1)
        cfg.clamp_max_offset((-30, 30))  # Zen 3
        assert cfg.max_offset == -30


class TestNewConfigOptions:
    def test_hardening_tiers_default(self):
        cfg = TunerConfig()
        assert cfg.hardening_tiers == [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]

    def test_max_core_time_default(self):
        cfg = TunerConfig()
        assert cfg.max_core_time_seconds == 7200

    def test_crash_penalty_steps_default(self):
        cfg = TunerConfig()
        assert cfg.crash_penalty_steps == 3

    def test_validate_transitions_default(self):
        cfg = TunerConfig()
        assert cfg.validate_transitions is True

    def test_hardening_tiers_json_roundtrip(self):
        cfg = TunerConfig()
        restored = TunerConfig.from_json(cfg.to_json())
        assert restored.hardening_tiers == cfg.hardening_tiers
        assert restored.max_core_time_seconds == cfg.max_core_time_seconds
        assert restored.crash_penalty_steps == cfg.crash_penalty_steps
        assert restored.validate_transitions == cfg.validate_transitions

    def test_empty_hardening_tiers_valid(self):
        cfg = TunerConfig(hardening_tiers=[])
        errors = cfg.validate()
        assert not any("hardening" in e.lower() for e in errors)

    def test_validate_crash_penalty_range(self):
        cfg = TunerConfig(crash_penalty_steps=0)
        errors = cfg.validate()
        assert any("crash_penalty" in e.lower() for e in errors)

    def test_validate_max_core_time_range(self):
        cfg = TunerConfig(max_core_time_seconds=100)
        errors = cfg.validate()
        assert any("max_core_time" in e.lower() for e in errors)


class TestConfigValidationFailsClosed:
    """Invalid configs must be rejected (fail closed). A step size < 1 would make
    the search advance by 0 and loop forever, so it must never validate."""

    def _cfg(self, **kw):
        return TunerConfig(cores_to_test=[0], **kw)

    def test_default_config_is_valid(self):
        assert self._cfg().validate() == []

    def test_zero_coarse_step_rejected(self):
        errors = self._cfg(coarse_step=0, fine_step=0).validate()
        assert any("coarse_step" in e for e in errors)

    def test_zero_fine_step_rejected(self):
        errors = self._cfg(fine_step=0).validate()
        assert any("fine_step" in e for e in errors)

    def test_each_invalid_numeric_field_is_rejected(self):
        cases = {
            "validate_duration_seconds": 0,
            "max_confirm_retries": -1,
            "midpoint_jump_threshold": 0,
            "abort_on_consecutive_failures": -1,
            "backoff_preconfirm_multiplier": 0.0,
            "stretch_threshold_pct": -1.0,
            "resume_crash_quarantine_threshold": 0,
            "hunt_slot_seconds": 10,
            "max_unattributed_crash_hunts": 0,
        }
        for field, bad in cases.items():
            errors = self._cfg(**{field: bad}).validate()
            assert any(field in e for e in errors), f"{field}={bad} not rejected: {errors}"
