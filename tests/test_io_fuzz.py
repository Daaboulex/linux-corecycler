"""Crash-resistance fuzzing of the IO-boundary parsers: the SMU sysfs command
primitive, the MCE/dmesg detector, and the timestamp formatter. Each must fail
closed on a truncated/garbage response rather than raise into its caller.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.detector import (  # noqa: E402
    ErrorDetector,
    _is_kernel_error_line,
    _is_mce_error_line,
)
from corecycler.history.timefmt import format_local  # noqa: E402
from corecycler.smu.commands import CPUGeneration, SMUCommandSet  # noqa: E402
from corecycler.smu.driver import RyzenSMU  # noqa: E402

_ZEN5 = SMUCommandSet(
    generation=CPUGeneration.ZEN5_GRANITE_RIDGE,
    set_co_cmd=0x06,
    get_co_cmd=0xD5,
    set_all_co_cmd=0x07,
    mailbox="rsmu",
    co_range=(-60, 10),
    encoding_scheme="zen4_5",
)


class TestSmuPrimitiveFailsClosed:
    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(resp=st.binary(max_size=40))
    def test_send_command_never_crashes_on_any_response(self, resp, tmp_path):
        for name in ("smu_args", "rsmu_cmd", "mp1_smu_cmd"):
            (tmp_path / name).write_bytes(b"\x00" * 24)
        smu = RyzenSMU(commands=_ZEN5, sysfs_path=tmp_path)
        with patch("pathlib.Path.read_bytes", return_value=resp):
            r = smu._send_command(0x06, (0, 1, 2))
            assert isinstance(r.success, bool)
            # A truncated response (< 24 bytes) must fail closed, never raise.
            if len(resp) < 24:
                assert r.success is False
            # And the higher-level CO read must not raise either.
            assert smu.get_co_offset(0) is None or isinstance(smu.get_co_offset(0), int)


class TestDetectorParsersRobust:
    @settings(max_examples=300, deadline=None)
    @given(line=st.text(max_size=200))
    def test_classifiers_never_crash(self, line):
        assert isinstance(_is_mce_error_line(line.lower()), bool)
        assert isinstance(_is_kernel_error_line(line.lower()), bool)

    @settings(max_examples=300, deadline=None)
    @given(dmesg_out=st.text(max_size=800))
    def test_check_mce_never_crashes(self, dmesg_out):
        det = ErrorDetector()
        det._dmesg_baseline_ts = 1.0  # so the timestamp filter runs the parse path
        det._last_dmesg_time = 0.0
        fake = MagicMock(returncode=0, stdout=dmesg_out)
        with patch("subprocess.run", return_value=fake):
            events = det.check_mce()
        assert isinstance(events, list)

    def test_real_mce_line_is_classified(self):
        line = "mce: [hardware error]: cpu 3 bank 5: status: ...".lower()
        assert _is_mce_error_line(line) is True


class TestTimefmtRobust:
    @settings(max_examples=400, deadline=None)
    @given(s=st.text(max_size=60))
    def test_format_local_never_crashes(self, s):
        assert isinstance(format_local(s), str)
        assert isinstance(format_local(s, date_only=True), str)
