"""Crash-resistance fuzzing of the monitor-layer parsers.

These read /proc, sysfs, raw SPD EEPROM bytes and dmidecode text -- all of which can
be malformed (other arch, truncated read, garbage EEPROM). A parser must fail closed
(skip the bad input / return empty) rather than crash the telemetry thread.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from monitor import frequency as freq_mod  # noqa: E402
from monitor.cpu_usage import CPUUsageReader  # noqa: E402
from monitor.memory import decode_spd_timings, parse_dmidecode_output  # noqa: E402


class TestMemoryParsersRobust:
    @settings(max_examples=400, deadline=None)
    @given(data=st.binary(max_size=600))
    def test_decode_spd_never_crashes(self, data):
        r = decode_spd_timings(data)
        assert r is None or r.tCK_ps > 0

    @settings(max_examples=400, deadline=None)
    @given(text=st.text(max_size=900))
    def test_parse_dmidecode_never_crashes(self, text):
        r = parse_dmidecode_output(text)
        assert isinstance(r, list)


class TestProcStatRobust:
    @settings(max_examples=400, deadline=None)
    @given(text=st.text(max_size=600))
    def test_cpu_usage_never_crashes(self, text):
        reader = CPUUsageReader()
        with patch("pathlib.Path.read_text", return_value=text):
            result = reader.read()
        assert isinstance(result, dict)

    def test_cpu_usage_parses_real_stat(self):
        reader = CPUUsageReader()
        line = "cpu0 100 0 50 1000 10 0 5 0 0 0\ncpu1 200 0 60 900 20 0 5 0 0 0\n"
        with patch("pathlib.Path.read_text", return_value=line):
            reader.read()  # baseline
        line2 = "cpu0 150 0 70 1100 12 0 6 0 0 0\ncpu1 260 0 80 950 25 0 6 0 0 0\n"
        with patch("pathlib.Path.read_text", return_value=line2):
            usage = reader.read()
        assert set(usage) == {0, 1}
        assert all(0.0 <= v <= 100.0 for v in usage.values())

    def test_cpu_usage_skips_malformed_cpu_lines(self):
        """A malformed cpuN line (non-numeric id/values, too few fields) is skipped,
        not crashed on. The random-text fuzz rarely produces a 'cpu' line, so assert
        this explicitly."""
        reader = CPUUsageReader()
        bad = "cpu0 abc def ghi\ncpuX 1 2 3\ncpu7 5 6\ncpu1 100 0 50 1000 10 0 5 0\n"
        with patch("pathlib.Path.read_text", return_value=bad):
            result = reader.read()
        assert isinstance(result, dict)


class TestProcCpuinfoFreqRobust:
    @settings(max_examples=400, deadline=None)
    @given(text=st.text(max_size=600))
    def test_read_from_proc_never_crashes(self, text):
        with patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.read_text", return_value=text):
            result = freq_mod._read_from_proc()
        assert isinstance(result, dict)
