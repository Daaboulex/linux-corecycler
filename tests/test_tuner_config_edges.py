"""Validation-error and helper coverage for TunerConfig."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.tuner.config import TunerConfig, _json_value_ok


def _errors(**kw) -> list[str]:
    return TunerConfig(**kw).validate()


class TestTunerConfigValidation:
    def test_direction_invalid(self):
        assert any("direction must be" in e for e in _errors(direction=0))

    def test_cores_to_test_empty(self):
        assert any("cores_to_test is empty" in e for e in _errors(cores_to_test=[]))

    def test_search_duration_too_low(self):
        assert any("search_duration_seconds" in e for e in _errors(search_duration_seconds=0))

    def test_confirm_duration_too_low(self):
        assert any("confirm_duration_seconds" in e for e in _errors(confirm_duration_seconds=0))

    def test_apparatus_streak_out_of_range(self):
        assert any("apparatus_failure_streak must be 0-100" in e for e in _errors(apparatus_failure_streak=101))

    def test_apparatus_streak_not_above_confirm_retries(self):
        errs = _errors(apparatus_failure_streak=2, max_confirm_retries=2)
        assert any("must exceed max_confirm_retries" in e for e in errs)

    def test_hardening_tier_not_a_dict(self):
        assert any("must be a dict" in e for e in _errors(hardening_tiers=["nope"]))

    def test_hardening_tier_missing_keys(self):
        assert any("missing required keys" in e for e in _errors(hardening_tiers=[{"backend": "mprime"}]))

    def test_over_temp_grace_negative(self):
        assert any("over_temp_grace_seconds" in e for e in _errors(over_temp_grace_seconds=-1.0))

    def test_over_temp_hard_margin_negative(self):
        assert any("over_temp_hard_margin_c" in e for e in _errors(over_temp_hard_margin_c=-1.0))


class TestJsonValueOk:
    def test_unknown_default_type_accepts_any(self):
        assert _json_value_ok(object(), 5) is True
