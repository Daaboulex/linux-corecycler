"""Tests for the stressapptest stress backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from corecycler.engine import execution
from corecycler.engine.backends.base import Outcome, StressConfig, StressMode
from corecycler.engine.backends.stressapptest import ALLOCATION_REFUSALS, StressapptestBackend
from tests.test_execution import (
    holds_the_deadline_until,
    lane,
    make_supervisor,
    run_one,
    scripted,
    stop_once,
)

if TYPE_CHECKING:
    from pathlib import Path


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

    def test_a_passing_status_finds_nothing(self, tmp_path):
        assert StressapptestBackend().findings("Status: PASS - please verify no corrected errors\n", tmp_path) == ()

    def test_a_hardware_problem_status_is_a_computation_error(self, tmp_path):
        found = StressapptestBackend().findings("Status: FAIL - test discovered HW problems\n", tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.COMPUTATION]

    def test_a_procedural_error_status_is_unexplained_not_a_memory_verdict(self, tmp_path):
        found = StressapptestBackend().findings("Status: FAIL - test encountered procedural errors\n", tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.UNEXPLAINED]

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
    def test_a_clean_summary_with_zero_incidents_finds_nothing(self, tmp_path):
        assert StressapptestBackend().findings(CLEAN_SUMMARY, tmp_path) == ()

    @pytest.mark.parametrize(
        "line",
        [
            "Stats: Found 2 hardware incidents",
            "Log: Thread 3 found 1 hardware incidents",
            "Stats: Completed: 2048.00M in 9.99s 205.00MB/s, with 12 hardware incidents, 0 errors",
        ],
    )
    def test_a_nonzero_incident_count_is_a_computation_error(self, line, tmp_path):
        found = StressapptestBackend().findings(line + "\n", tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.COMPUTATION]
        assert found[0].text == line

    def test_a_hardware_error_line_is_still_a_computation_error(self, tmp_path):
        line = "Hardware Error: Pattern mismatch in block starting at sector 64 in DMA read\n"
        found = StressapptestBackend().findings(line, tmp_path)
        assert {signal.outcome for signal in found} == {Outcome.COMPUTATION}


class TestAllocationRefusal:
    @pytest.mark.parametrize("refusal", ALLOCATION_REFUSALS)
    def test_a_refusal_is_not_started_with_the_tools_text(self, refusal, tmp_path):
        stdout = (
            f"Log: Defaulting to 16 copy threads\nProcess Error: {refusal}.\n"
            "Status: FAIL - test encountered procedural errors\n"
        )
        found = StressapptestBackend().findings(stdout, tmp_path)
        strongest = min(found, key=lambda signal: signal.outcome.rank)
        assert strongest.outcome is Outcome.NOT_STARTED
        assert refusal in strongest.text.lower()

    def test_a_refusal_on_stderr_is_seen_too(self, tmp_path):
        found = StressapptestBackend().findings("\nProcess Error: freepages < neededpages.\n", tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.NOT_STARTED]

    def test_a_real_memory_error_outranks_a_refusal(self, tmp_path):
        stdout = "Hardware Error: miscompare at 0x1000\nProcess Error: failed to allocate memory\n"
        found = StressapptestBackend().findings(stdout, tmp_path)
        assert min(found, key=lambda signal: signal.outcome.rank).outcome is Outcome.COMPUTATION


def _prints_on_sigterm(summary: str, code: int, marker: Path) -> str:
    return (
        "import signal, sys, time\n"
        "def stop(*_):\n"
        f"    sys.stdout.write({summary!r})\n"
        "    sys.stdout.flush()\n"
        f"    open({str(marker)!r}, 'w').close()\n"
        f"    sys.exit({code})\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "open('ready', 'w').close()\n"
        "while True:\n"
        "    time.sleep(0.01)\n"
    )


class TestSummaryAfterTheStop:
    @pytest.fixture(autouse=True)
    def a_grace_a_loaded_host_cannot_exhaust(self, monkeypatch):
        monkeypatch.setattr(execution, "STOP_GRACE_SECONDS", 30.0)

    def test_the_summary_printed_after_our_stop_is_read(self, tmp_path):
        marker = tmp_path / "summary-written"
        summary = "Report Error: miscompare : DIMM0 : 1 : 3s\nStatus: FAIL - test discovered HW problems\n"
        one = lane(tmp_path)
        supervisor, stop, _ = make_supervisor(scripted(StressapptestBackend, _prints_on_sigterm(summary, 1, marker)))
        ready = stop_once(stop, lambda: any(one.work_dir.glob("run-*/ready")))
        verdict = run_one(supervisor, one, 30.0)
        assert ready.is_set(), f"the stub was not ready within 10 s; {verdict.outcome}: {verdict.detail}"
        assert marker.exists(), f"the stub's SIGTERM handler wrote nothing; {verdict.outcome}: {verdict.detail}"
        assert verdict.outcome is Outcome.COMPUTATION
        assert "miscompare" in verdict.detail

    def test_a_clean_summary_after_our_stop_passes(self, tmp_path):
        marker = tmp_path / "summary-written"
        one = lane(tmp_path)
        supervisor, _, _ = make_supervisor(
            scripted(StressapptestBackend, _prints_on_sigterm(CLEAN_SUMMARY, 0, marker)),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/ready"))),
        )
        verdict = run_one(supervisor, one, 2.0)
        assert marker.exists(), f"the stub's SIGTERM handler wrote nothing; {verdict.outcome}: {verdict.detail}"
        assert verdict.outcome is Outcome.PASS


class TestMinimumMemory:
    @pytest.mark.parametrize("copy_threads", [1, 2, 3, 16, 17, 64, 128, 255, 256])
    def test_the_minimum_is_the_smallest_size_stressapptest_accepts(self, copy_threads):
        minimum = StressapptestBackend.minimum_memory_mb(copy_threads)
        assert _sat_accepts(minimum, copy_threads)
        assert not _sat_accepts(minimum - 1, copy_threads)

    def test_zero_threads_is_refused(self):
        with pytest.raises(ValueError, match="copy thread"):
            StressapptestBackend.minimum_memory_mb(0)
