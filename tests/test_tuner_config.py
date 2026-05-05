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
