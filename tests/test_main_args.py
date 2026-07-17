"""Argument parsing for the login-autostart entry point."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from main import _parse_auto_resume


class TestParseAutoResume:
    def test_absent_returns_none(self):
        assert _parse_auto_resume([]) is None
        assert _parse_auto_resume(["--other"]) is None

    def test_present_with_seconds(self):
        assert _parse_auto_resume(["--auto-resume", "300"]) == 300

    def test_present_without_value_defaults(self):
        assert _parse_auto_resume(["--auto-resume"]) == 120

    def test_malformed_value_fails_closed_to_default(self):
        assert _parse_auto_resume(["--auto-resume", "soon"]) == 120

    def test_negative_clamped_to_zero(self):
        assert _parse_auto_resume(["--auto-resume", "-5"]) == 0
