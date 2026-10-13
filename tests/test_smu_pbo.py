"""SMU PBO-limit write guards: every setter range-checks and RAISES before any
hardware write and returns False on a generation that lacks the command. These are
the last line before a bad watt/amp/scalar reaches the SMU.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from corecycler.smu.commands import CPUGeneration, SMUCommandSet, get_commands
from corecycler.smu.driver import RyzenSMU, SMUResponse


def _smu(gen=CPUGeneration.ZEN5_GRANITE_RIDGE):
    smu = RyzenSMU(get_commands(gen))
    smu._send_rsmu_command = MagicMock(return_value=SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24))
    return smu


LIMIT_SETTERS = ["set_ppt_limit", "set_tdc_limit", "set_edc_limit"]


class TestPboLimitGuards:
    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    def test_a_valid_limit_is_sent(self, setter):
        smu = _smu()
        assert getattr(smu, setter)(200) is True
        smu._send_rsmu_command.assert_called_once()

    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    @pytest.mark.parametrize("bad", [0, -1, -500, 2001, 100000])
    def test_out_of_range_raises(self, setter, bad):
        smu = _smu()
        with pytest.raises(ValueError):
            getattr(smu, setter)(bad)
        smu._send_rsmu_command.assert_not_called()

    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    @pytest.mark.parametrize("edge", [1, 2000])
    def test_boundaries_ok(self, setter, edge):
        assert getattr(_smu(), setter)(edge) is True

    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    def test_missing_command_returns_false_not_raises(self, setter):
        cmds = SMUCommandSet(
            generation=CPUGeneration.UNKNOWN,
            co_range=(0, 0),
            mailbox="rsmu",
            encoding_scheme="none",
        )
        smu = RyzenSMU(cmds)
        assert getattr(smu, setter)(200) is False


class TestPboScalar:
    def test_a_valid_scalar_is_sent(self):
        smu = _smu()
        assert smu.set_pbo_scalar(5.0) is True
        smu._send_rsmu_command.assert_called_once()

    @pytest.mark.parametrize("edge", [0.0, 10.0])
    def test_boundaries(self, edge):
        assert _smu().set_pbo_scalar(edge) is True

    @pytest.mark.parametrize("bad", [-0.1, 10.1, 100.0, -50.0])
    def test_out_of_range_raises(self, bad):
        with pytest.raises(ValueError):
            _smu().set_pbo_scalar(bad)


class TestBoostLimit:
    def test_a_valid_limit_is_sent(self):
        smu = _smu()
        assert smu.set_boost_limit(5500) is True
        smu._send_rsmu_command.assert_called_once()

    def test_generation_without_command_returns_false(self):
        assert _smu(CPUGeneration.ZEN3_VERMEER).set_boost_limit(5000) is False


class TestSystemStateReads:
    def _drv(self, tmp_path):
        return RyzenSMU(get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE), tmp_path)

    def test_get_pbo_scalar_none_on_failed_read(self, tmp_path):
        assert self._drv(tmp_path).get_pbo_scalar() is None

    def test_get_fastest_core_none_on_failed_read(self, tmp_path):
        vermeer = RyzenSMU(get_commands(CPUGeneration.ZEN3_VERMEER), tmp_path)
        assert vermeer.get_fastest_core() is None

    def test_get_fastest_core_none_without_command(self, tmp_path):
        assert self._drv(tmp_path).get_fastest_core() is None

    def test_get_boost_limit_none_on_failed_read(self, tmp_path):
        assert self._drv(tmp_path).get_boost_limit() is None
