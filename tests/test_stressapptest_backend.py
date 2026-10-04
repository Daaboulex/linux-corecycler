"""Tests for the stressapptest stress backend."""

from __future__ import annotations

import pytest

from corecycler.engine.backends.base import StressConfig, StressMode
from corecycler.engine.backends.stressapptest import ALLOCATION_REFUSALS, StressapptestBackend
from corecycler.engine.execution import classify_error


def _sat_accepts(size_mb: int, copy_threads: int) -> bool:
    """stressapptest's own InitializePages rule (1 MiB pages, fine-lock queue by default)."""
    pages = size_mb
    empty = pages // 5 * 2
    return empty >= copy_threads


class TestStressapptestBackend:
    def test_command_generation(self, tmp_path, on_path):
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        backend = StressapptestBackend()
        config = StressConfig(mode=StressMode.SSE)
        cmd = backend.get_command(config, tmp_path)
        assert cmd[0] == "/usr/bin/stressapptest"
        assert "-W" in cmd
        assert "-s" in cmd
        assert "86400" in cmd

    def test_no_budget_means_no_size_flag(self, tmp_path, on_path):
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        cmd = StressapptestBackend().get_command(StressConfig(memory_mb=None), tmp_path)
        assert "-M" not in cmd

    def test_the_budget_becomes_the_size_flag(self, tmp_path, on_path):
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        cmd = StressapptestBackend().get_command(StressConfig(memory_mb=2688), tmp_path)
        assert cmd[cmd.index("-M") + 1] == "2688"

    def test_the_copy_threads_match_the_lane(self, tmp_path, on_path):
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        backend = StressapptestBackend()
        cmd = backend.get_command(StressConfig(threads=2), tmp_path)
        assert cmd[cmd.index("-m") + 1] == "2"
        cmd = backend.get_command(StressConfig(threads=0), tmp_path)
        assert cmd[cmd.index("-m") + 1] == "1"

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


CLEAN_SUMMARY = (
    "Log: Thread 1 found 0 hardware incidents\n"
    "Stats: Completed: 2048.00M in 9.99s 205.00MB/s, with 0 hardware incidents, 0 errors\n"
    "Stats: Found 0 hardware incidents\n"
    "Status: PASS - please verify no corrected errors\n"
)


class TestIncidentCount:
    @pytest.mark.parametrize("returncode", [0, -15])
    def test_a_clean_summary_with_zero_incidents_passes(self, returncode):
        passed, err = StressapptestBackend().parse_output(CLEAN_SUMMARY, "", returncode)
        assert (passed, err) == (True, None)

    @pytest.mark.parametrize(
        "line",
        [
            "Stats: Found 2 hardware incidents",
            "Log: Thread 3 found 1 hardware incidents",
            "Stats: Completed: 2048.00M in 9.99s 205.00MB/s, with 12 hardware incidents, 0 errors",
        ],
    )
    def test_a_nonzero_incident_count_is_a_failure(self, line):
        passed, err = StressapptestBackend().parse_output(line + "\n", "", 0)
        assert passed is False
        assert "hardware incident" in err

    def test_a_hardware_error_line_is_still_a_failure(self):
        line = "Hardware Error: Pattern mismatch in block starting at sector 64 in DMA read\n"
        passed, _ = StressapptestBackend().parse_output(line, "", 0)
        assert passed is False


class TestAllocationRefusal:
    @pytest.mark.parametrize("refusal", ALLOCATION_REFUSALS)
    def test_a_refusal_is_an_environment_fault_not_a_memory_verdict(self, refusal):
        stdout = f"Log: Defaulting to 16 copy threads\nProcess Error: {refusal}.\nStatus: FAIL\n"
        passed, err = StressapptestBackend().parse_output(stdout, "", 1)
        assert passed is False
        assert refusal in err
        assert "memory errors" not in err
        assert classify_error(err) == "startup"

    def test_a_refusal_on_stderr_is_seen_too(self):
        passed, err = StressapptestBackend().parse_output("", "Process Error: freepages < neededpages.\n", 1)
        assert passed is False
        assert classify_error(err) == "startup"

    def test_a_real_memory_error_is_still_a_verdict(self):
        passed, err = StressapptestBackend().parse_output("Hardware Error: miscompare at 0x1000\n", "", 0)
        assert passed is False
        assert classify_error(err) != "startup"


class TestMinimumMemory:
    @pytest.mark.parametrize("copy_threads", [1, 2, 3, 16, 17, 64, 128, 255, 256])
    def test_the_minimum_is_the_smallest_size_stressapptest_accepts(self, copy_threads):
        minimum = StressapptestBackend.minimum_memory_mb(copy_threads)
        assert _sat_accepts(minimum, copy_threads)
        assert not _sat_accepts(minimum - 1, copy_threads)

    def test_zero_threads_is_refused(self):
        with pytest.raises(ValueError, match="copy thread"):
            StressapptestBackend.minimum_memory_mb(0)
