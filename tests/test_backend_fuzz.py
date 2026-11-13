"""Property-based fuzzing of the backend findings and the exit grading.

findings() decides which signals a stress tool's own text names, and the
Supervisor grades every exit status. A false STABLE (reporting pass when the
run actually failed/crashed) is a safety bug: the tuner would confirm a
crashing offset.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from corecycler.engine.backends.base import LaneSignal, Outcome, OutcomeKind
from corecycler.engine.backends.mprime import MprimeBackend
from corecycler.engine.backends.stress_ng import StressNgBackend
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.engine.backends.ycruncher import YCruncherBackend
from corecycler.engine.execution import CPU_SIGNALS, exit_signal

_BACKENDS = [MprimeBackend, YCruncherBackend, StressNgBackend, StressapptestBackend]
_NO_RESULTS_DIR = Path("/nonexistent-corecycler-fuzz")


def _instances():
    return [B() for B in _BACKENDS]


class TestBackendFindingsRobust:
    @settings(max_examples=300, deadline=None)
    @given(output=st.text(max_size=600))
    def test_findings_never_raise_and_return_signals(self, output):
        for b in _instances():
            found = b.findings(output, _NO_RESULTS_DIR)
            assert isinstance(found, tuple)
            assert all(isinstance(hit, LaneSignal) for hit in found)

    @settings(max_examples=100, deadline=None)
    @given(prefix=st.text(max_size=100), suffix=st.text(max_size=100))
    def test_stressapptest_detects_midrun_memory_errors_whatever_surrounds_them(self, prefix, suffix):
        """A stressapptest run that logged a memory error mid-run must fail,
        whatever came before or after it, the final 'Status:' line included."""
        found = StressapptestBackend().findings(
            f"{prefix}\nHardware Error: miscompare on CPU 3\n{suffix}", _NO_RESULTS_DIR
        )
        assert Outcome.COMPUTATION in {hit.outcome for hit in found}


class TestCpuSignalDeaths:
    @pytest.mark.parametrize("sig", sorted(CPU_SIGNALS))
    @pytest.mark.parametrize("sent", [None, signal.SIGTERM, signal.SIGKILL])
    def test_a_cpu_signal_death_is_always_silicon(self, sig, sent):
        found = exit_signal(-sig, sent)
        assert found is not None and found.outcome.kind is OutcomeKind.SILICON

    @pytest.mark.parametrize("sig", sorted(CPU_SIGNALS))
    @pytest.mark.parametrize(
        ("backend", "passing_output"),
        [
            (MprimeBackend, "Self-test 5 passed\nSelf-test 6 passed\n"),
            (StressapptestBackend, "Status: PASS - please verify no corrected errors\n"),
        ],
    )
    def test_no_passing_line_can_mask_a_crash(self, sig, backend, passing_output):
        """A pass line yields no signal, so nothing can outrank the crash the
        exit status reports: the graded lane is silicon, never a pass."""
        assert backend().findings(passing_output, _NO_RESULTS_DIR) == ()
        assert exit_signal(-sig, None).outcome is Outcome.CPU_SIGNAL

    @pytest.mark.parametrize("sig", [signal.SIGABRT, signal.SIGTRAP])
    def test_an_abort_or_trap_with_no_message_is_unexplained(self, sig):
        assert exit_signal(-sig, None).outcome is Outcome.UNEXPLAINED
