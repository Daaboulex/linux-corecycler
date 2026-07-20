"""Property-based fuzzing of the backend output parsers.

parse_output decides "is this offset stable?" from raw stress-tool output. A
false STABLE (reporting pass when the run actually failed/crashed) is a safety
bug: the tuner would confirm a crashing offset.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends.base import CRASH_SIGNALS  # noqa: E402
from corecycler.engine.backends.mprime import MprimeBackend  # noqa: E402
from corecycler.engine.backends.stress_ng import StressNgBackend  # noqa: E402
from corecycler.engine.backends.stressapptest import StressapptestBackend  # noqa: E402
from corecycler.engine.backends.ycruncher import YCruncherBackend  # noqa: E402

_BACKENDS = [MprimeBackend, YCruncherBackend, StressNgBackend, StressapptestBackend]


def _instances():
    return [B() for B in _BACKENDS]


class TestBackendParseRobust:
    @settings(max_examples=300, deadline=None)
    @given(stdout=st.text(max_size=300), stderr=st.text(max_size=300),
           rc=st.integers(min_value=-30, max_value=400))
    def test_parse_never_raises_and_returns_bool(self, stdout, stderr, rc):
        for b in _instances():
            passed, msg = b.parse_output(stdout, stderr, rc)
            assert isinstance(passed, bool)
            assert msg is None or isinstance(msg, str)

    @settings(max_examples=200, deadline=None)
    @given(stdout=st.text(max_size=200), stderr=st.text(max_size=200),
           sig=st.sampled_from(sorted(CRASH_SIGNALS)))
    def test_crash_signal_is_never_reported_as_passed(self, stdout, stderr, sig):
        """A SIGSEGV/SIGABRT/SIGBUS exit is CO instability and must never read as
        pass, no matter what the surrounding output contains."""
        for b in _instances():
            passed, _ = b.parse_output(stdout, stderr, sig)
            assert passed is False, f"{b.name} reported a crash signal {sig} as passed"

    @settings(max_examples=100, deadline=None)
    @given(prefix=st.text(max_size=100), suffix=st.text(max_size=100),
           rc=st.sampled_from([-15, -9, 143, 137, 0]))
    def test_stressapptest_detects_midrun_memory_errors_even_when_killed(
        self, prefix, suffix, rc
    ):
        """A killed stressapptest run that logged a memory error mid-run must fail,
        not pass — the final 'Status:' line is never reached."""
        b = StressapptestBackend()
        out = f"{prefix}\nHardware Error: miscompare on CPU 3\n{suffix}"
        passed, _ = b.parse_output(out, "", rc)
        assert passed is False

    @pytest.mark.parametrize("sig", sorted(CRASH_SIGNALS))
    def test_mprime_crash_after_passing_iteration_is_not_passed(self, sig):
        """EVERY crash signal (incl. SIGILL/SIGFPE) must override an earlier
        'Self-test N passed' line. Random-output fuzz won't reproduce this exact
        trigger, so assert it explicitly per signal."""
        b = MprimeBackend()
        out = "Self-test 5 passed\nSelf-test 6 passed\n"
        passed, _ = b.parse_output(out, "", sig)
        assert passed is False, (
            f"mprime passed a {CRASH_SIGNALS[sig]} crash after a passing iteration"
        )

    @pytest.mark.parametrize("sig", sorted(CRASH_SIGNALS))
    def test_stressapptest_crash_overrides_status_pass(self, sig):
        """A crash signal must override a printed 'Status: PASS' — a clean
        stressapptest run exits 0 or is killed by us, never with a crash code, so a
        PASS line followed by a crash-on-teardown is still instability, not a pass."""
        b = StressapptestBackend()
        passed, _ = b.parse_output("Status: PASS\n", "", sig)
        assert passed is False, (
            f"stressapptest passed a {CRASH_SIGNALS[sig]} crash despite Status: PASS"
        )
