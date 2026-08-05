"""Wiring conformance matrix: every generation, every SMU operation.

For EVERY generation in COMMAND_SETS, drive the real RyzenSMU against a
recording mailbox and assert the driver sends exactly the mailbox, command id
and encoded argument the command table declares — and that an operation whose
command is absent (None) returns its failure value with ZERO mailbox traffic.
A malformed value must raise before any traffic. This is the executable proof
that the single-place table in smu/commands.py is what actually reaches the
hardware, for all generations at once.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from corecycler.smu.commands import (
    COMMAND_SETS,
    encode_boost_limit_arg,
    encode_pbo_limit_arg,
    encode_pbo_scalar_arg,
)
from corecycler.smu.driver import RyzenSMU, SMUResponse

GENERATIONS = sorted(COMMAND_SETS, key=lambda gen: gen.name)


class _Recorder:
    """Answers success on both mailboxes and records (mailbox, cmd, arg0)."""

    def __init__(self, smu: RyzenSMU) -> None:
        default = smu.commands.mailbox
        self.calls: list[tuple[str, int, int]] = []
        smu._send_command = lambda cmd, args=(0,) * 6: self._log(default, cmd, args)
        smu._send_rsmu_command = lambda cmd, args=(0,) * 6: self._log("rsmu", cmd, args)
        smu.check_writable = lambda: (True, "OK")

    def _log(self, mailbox: str, cmd: int, args) -> SMUResponse:
        arg0 = args[0] if args else 0
        self.calls.append((mailbox, cmd, arg0))
        return SMUResponse(success=True, args=(arg0,) + (0,) * 5, raw=b"")


def _smu(gen):
    cmds = COMMAND_SETS[gen]
    smu = RyzenSMU(cmds, MagicMock())
    return smu, _Recorder(smu), cmds


def _get_co_mailbox(cmds) -> str:
    return cmds.get_co_mailbox or cmds.mailbox


@pytest.mark.parametrize("gen", GENERATIONS, ids=lambda g: g.name)
class TestCOWiring:
    def test_read(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.get_co_offset(0)
        if cmds.has_co:
            assert result == 0
            assert rec.calls == [(_get_co_mailbox(cmds), cmds.get_co_cmd, 0)]
        else:
            assert result is None
            assert rec.calls == []

    def test_write_and_readback(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.set_co_offset(0, 0)
        if cmds.has_co:
            assert result is True
            assert rec.calls == [
                (cmds.mailbox, cmds.set_co_cmd, 0),
                (_get_co_mailbox(cmds), cmds.get_co_cmd, 0),
            ]
        else:
            assert result is False
            assert rec.calls == []

    def test_write_all_and_readback(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.set_all_co(0)
        if cmds.has_co and cmds.set_all_co_cmd is not None:
            assert result is True
            assert rec.calls == [
                (cmds.mailbox, cmds.set_all_co_cmd, 0),
                (_get_co_mailbox(cmds), cmds.get_co_cmd, 0),
            ]
        else:
            assert result is False

    def test_out_of_range_value_raises_with_zero_traffic(self, gen):
        smu, rec, cmds = _smu(gen)
        if not cmds.has_co:
            pytest.skip("generation has no CO range to violate")
        with pytest.raises(ValueError, match="out of range"):
            smu.set_co_offset(0, cmds.co_range[0] - 1)
        assert rec.calls == []


@pytest.mark.parametrize("gen", GENERATIONS, ids=lambda g: g.name)
class TestPBOLimitWiring:
    def test_ppt(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.set_ppt_limit(200)
        if cmds.set_ppt_cmd is not None:
            assert result is True
            assert rec.calls == [("rsmu", cmds.set_ppt_cmd, encode_pbo_limit_arg(200))]
        else:
            assert result is False
            assert rec.calls == []

    def test_tdc(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.set_tdc_limit(150)
        if cmds.set_tdc_cmd is not None:
            assert result is True
            assert rec.calls == [("rsmu", cmds.set_tdc_cmd, encode_pbo_limit_arg(150))]
        else:
            assert result is False
            assert rec.calls == []

    def test_edc(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.set_edc_limit(180)
        if cmds.set_edc_cmd is not None:
            assert result is True
            assert rec.calls == [("rsmu", cmds.set_edc_cmd, encode_pbo_limit_arg(180))]
        else:
            assert result is False
            assert rec.calls == []

    def test_malformed_limit_raises_with_zero_traffic(self, gen):
        smu, rec, cmds = _smu(gen)
        if cmds.set_ppt_cmd is None:
            pytest.skip("generation has no PPT command")
        with pytest.raises(ValueError, match="out of sane range"):
            smu.set_ppt_limit(0)
        with pytest.raises(ValueError, match="out of sane range"):
            smu.set_ppt_limit(2001)
        assert rec.calls == []


@pytest.mark.parametrize("gen", GENERATIONS, ids=lambda g: g.name)
class TestScalarBoostFastestWiring:
    def test_scalar_get(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.get_pbo_scalar()
        if cmds.get_pbo_scalar_cmd is not None:
            assert result == 0.0
            assert rec.calls == [("rsmu", cmds.get_pbo_scalar_cmd, 0)]
        else:
            assert result is None
            assert rec.calls == []

    def test_scalar_set(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.set_pbo_scalar(2.0)
        if cmds.set_pbo_scalar_cmd is not None:
            assert result is True
            assert rec.calls == [("rsmu", cmds.set_pbo_scalar_cmd, encode_pbo_scalar_arg(2.0))]
        else:
            assert result is False
            assert rec.calls == []

    def test_boost_get(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.get_boost_limit()
        if cmds.get_boost_limit_cmd is not None:
            assert result == 0
            assert rec.calls == [("rsmu", cmds.get_boost_limit_cmd, 0)]
        else:
            assert result is None
            assert rec.calls == []

    def test_boost_set(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.set_boost_limit(5000)
        if cmds.set_boost_limit_cmd is not None:
            assert result is True
            assert rec.calls == [("rsmu", cmds.set_boost_limit_cmd, encode_boost_limit_arg(5000))]
        else:
            assert result is False
            assert rec.calls == []

    def test_fastest_core(self, gen):
        smu, rec, cmds = _smu(gen)
        result = smu.get_fastest_core()
        if cmds.get_fastest_core_cmd is not None:
            assert result == 0
            assert rec.calls == [("rsmu", cmds.get_fastest_core_cmd, 0)]
        else:
            assert result is None
            assert rec.calls == []
