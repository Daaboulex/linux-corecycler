"""Crash-resistance fuzzing of the text/binary parsers that read untrusted-ish
kernel data: /proc/cpuinfo, the SMU PM table bytes, and the FCLK:UCLK ratio math.

A malformed or non-x86 /proc/cpuinfo, garbage PM-table bytes from a wrong offset
map, or a NaN clock value must never crash the parser -- they must fail closed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import engine.topology as topo_mod  # noqa: E402
from engine.topology import CPUTopology, _field_int, _parse_cpuinfo  # noqa: E402
from smu.pmtable import PMTableReader, compute_fclk_uclk_ratio  # noqa: E402


class TestCpuinfoParserRobust:
    @settings(max_examples=400, deadline=None)
    @given(text=st.text(max_size=600))
    def test_parse_cpuinfo_never_crashes(self, text):
        mock = MagicMock()
        mock.exists.return_value = True
        mock.read_text.return_value = text
        orig = topo_mod.CPUINFO
        topo_mod.CPUINFO = mock
        try:
            _parse_cpuinfo(CPUTopology())  # must not raise on any input
        finally:
            topo_mod.CPUINFO = orig

    @settings(max_examples=300, deadline=None)
    @given(line=st.text(max_size=60))
    def test_field_int_returns_int_or_none(self, line):
        v = _field_int(line)
        assert v is None or isinstance(v, int)

    def test_parse_cpuinfo_real_data_still_works(self):
        """A guard against the fuzz hardening breaking normal parsing."""
        mock = MagicMock()
        mock.exists.return_value = True
        mock.read_text.return_value = (
            "processor\t: 0\ncore id\t\t: 0\nphysical id\t: 0\n"
            "model name\t: AMD Ryzen 9 9950X\ncpu family\t: 26\nmodel\t\t: 68\n\n"
            "processor\t: 1\ncore id\t\t: 1\nphysical id\t: 0\n\n"
        )
        orig = topo_mod.CPUINFO
        topo_mod.CPUINFO = mock
        try:
            topo = CPUTopology()
            _parse_cpuinfo(topo)
            assert topo.physical_cores == 2
            assert topo.family == 26
            assert "9950X" in topo.model_name
        finally:
            topo_mod.CPUINFO = orig

    def test_colon_less_field_lines_do_not_crash(self):
        """A field line with no colon -- 'model name', 'vendor_id', 'processor' (a
        truncated read or other arch) -- must not crash. Fuzz found this; pin it."""
        mock = MagicMock()
        mock.exists.return_value = True
        mock.read_text.return_value = "model name\nvendor_id\nprocessor\ncore id\n\n"
        orig = topo_mod.CPUINFO
        topo_mod.CPUINFO = mock
        try:
            _parse_cpuinfo(CPUTopology())  # must not raise IndexError
        finally:
            topo_mod.CPUINFO = orig


class TestPmTableParserRobust:
    @settings(max_examples=300, deadline=None,
              suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(raw=st.binary(max_size=3000), ver=st.binary(max_size=8))
    def test_read_never_crashes_on_garbage_bytes(self, raw, ver, tmp_path):
        (tmp_path / "pm_table").write_bytes(raw)
        if ver:
            (tmp_path / "pm_table_version").write_bytes(ver)
        result = PMTableReader(num_cores=16, sysfs_path=tmp_path).read()
        assert result is None or hasattr(result, "raw_floats")


class TestRatioMathRobust:
    @settings(max_examples=400, deadline=None)
    @given(f=st.floats(allow_nan=True, allow_infinity=True),
           u=st.floats(allow_nan=True, allow_infinity=True))
    def test_ratio_never_crashes(self, f, u):
        r = compute_fclk_uclk_ratio(f, u)
        assert r is None or (isinstance(r, tuple) and len(r) == 2)
