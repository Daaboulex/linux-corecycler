"""Display-correctness tests for the monitor/tuner readouts.

Covers the per-core frequency string (an idle core must not render a false
boost ceiling) and the "Tests Run" tally (synthetic crash-on-resume rows must
not be counted as stress tests).
"""

from __future__ import annotations

from gui.monitor_tab import CoreFreqBar
from gui.tuner_tab import TunerTab


class TestPerCoreFreqText:
    def test_idle_core_shows_no_false_ceiling(self):
        # "--/<max>" reads as a live boost maximum on a parked core — wrong.
        assert CoreFreqBar._freq_text(0.0, 5750.0) == "  -- MHz"

    def test_negative_freq_treated_as_idle(self):
        assert CoreFreqBar._freq_text(-1.0, 5750.0) == "  -- MHz"

    def test_live_core_has_unit_and_ceiling(self):
        assert CoreFreqBar._freq_text(4321.0, 5750.0) == "4321/5750MHz"

    def test_live_core_without_known_ceiling_has_unit(self):
        assert CoreFreqBar._freq_text(4321.0, 0.0) == "4321MHz"


class TestRealTestCount:
    def test_excludes_synthetic_crash_rows(self):
        entries = [
            {"passed": True, "duration_seconds": 60.0},
            {"passed": False, "duration_seconds": None, "error_type": "crash"},
            {"passed": True, "duration_seconds": 300.0},
        ]
        assert TunerTab._real_test_count(entries) == 2

    def test_all_real_counted(self):
        entries = [
            {"passed": True, "duration_seconds": 60.0},
            {"passed": False, "duration_seconds": 1.0},
        ]
        assert TunerTab._real_test_count(entries) == 2

    def test_empty(self):
        assert TunerTab._real_test_count([]) == 0

    def test_only_synthetic_counts_zero(self):
        assert TunerTab._real_test_count([{"passed": False, "duration_seconds": None}]) == 0
