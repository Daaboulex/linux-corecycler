"""Crash-resistance fuzzing of the IO-boundary parsers: the SMU sysfs command
primitive, the kernel record parser and reader, and the timestamp formatter. Each must fail
closed on a truncated/garbage response rather than raise into its caller.
"""

from __future__ import annotations

from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corecycler.engine.detector import ErrorDetector, parse_kernel_records
from corecycler.history.timefmt import format_local
from corecycler.smu.commands import CPUGeneration, SMUCommandSet
from corecycler.smu.driver import RyzenSMU

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
            if len(resp) < 24:
                assert r.success is False
            assert smu.get_co_offset(0) is None or isinstance(smu.get_co_offset(0), int)


class TestDetectorParsersRobust:
    @settings(max_examples=300, deadline=None)
    @given(lines=st.lists(st.text(max_size=200), max_size=12), final=st.booleans())
    def test_the_record_parser_never_crashes(self, lines, final):
        events, carry = parse_kernel_records([(1.0, line) for line in lines], final=final)
        assert isinstance(events, list)
        assert carry == [] or not final

    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(raw=st.binary(max_size=800))
    def test_the_kmsg_reader_never_crashes(self, raw, tmp_path):
        kmsg = tmp_path / "kmsg"
        kmsg.write_bytes(b"")
        det = ErrorDetector(kmsg)
        det.reset()
        with kmsg.open("ab") as log:
            log.write(raw)
        read = det.read_new(final=True)
        det.close()
        assert isinstance(read.events, tuple)
        assert read.problem is None or isinstance(read.problem, str)


class TestTimefmtRobust:
    @settings(max_examples=400, deadline=None)
    @given(s=st.text(max_size=60))
    def test_format_local_never_crashes(self, s):
        assert isinstance(format_local(s), str)
        assert isinstance(format_local(s, date_only=True), str)
