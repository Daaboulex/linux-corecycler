"""Tests for the stressapptest stress backend."""

from __future__ import annotations

from corecycler.engine.backends.base import StressConfig, StressMode
from corecycler.engine.backends.stressapptest import StressapptestBackend


class TestStressapptestBackend:
    def test_command_generation(self, tmp_path):
        backend = StressapptestBackend()
        config = StressConfig(mode=StressMode.SSE)
        cmd = backend.get_command(config, tmp_path)
        assert cmd[0] == "stressapptest"
        assert "-W" in cmd
        assert "-s" in cmd
        assert "86400" in cmd

    def test_parse_pass(self):
        backend = StressapptestBackend()
        stdout = "Status: PASS - please pass all stress tests."
        passed, err = backend.parse_output(stdout, "", 0)
        assert passed is True
        assert err is None

    def test_parse_fail(self):
        backend = StressapptestBackend()
        stdout = "Status: FAIL - memory errors detected."
        passed, err = backend.parse_output(stdout, "", 1)
        assert passed is False
        assert "fail" in err.lower()

    def test_parse_killed_by_scheduler(self):
        backend = StressapptestBackend()
        passed, err = backend.parse_output("", "", -15)
        assert passed is True

    def test_supported_modes(self):
        backend = StressapptestBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
