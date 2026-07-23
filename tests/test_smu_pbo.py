"""SMU PBO-limit write guards: every setter range-checks and RAISES before any
hardware write, returns False on a generation that lacks the command, and honors
dry-run. These are the last line before a bad watt/amp/scalar reaches the SMU.
"""

from __future__ import annotations

import pytest

from corecycler.smu.commands import CPUGeneration, SMUCommandSet, get_commands
from corecycler.smu.driver import RyzenSMU


def _smu(gen=CPUGeneration.ZEN5_GRANITE_RIDGE, dry_run=True):
    return RyzenSMU(get_commands(gen), dry_run=dry_run)


LIMIT_SETTERS = ["set_ppt_limit", "set_tdc_limit", "set_edc_limit"]


class TestPboLimitGuards:
    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    def test_valid_dry_run_ok(self, setter):
        assert getattr(_smu(), setter)(200) is True

    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    @pytest.mark.parametrize("bad", [0, -1, -500, 2001, 100000])
    def test_out_of_range_raises(self, setter, bad):
        with pytest.raises(ValueError):
            getattr(_smu(), setter)(bad)

    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    @pytest.mark.parametrize("edge", [1, 2000])
    def test_boundaries_ok(self, setter, edge):
        assert getattr(_smu(), setter)(edge) is True

    @pytest.mark.parametrize("setter", LIMIT_SETTERS)
    def test_missing_command_returns_false_not_raises(self, setter):
        cmds = SMUCommandSet(
            generation=CPUGeneration.UNKNOWN, co_range=(0, 0),
            mailbox="rsmu", encoding_scheme="none",
        )
        smu = RyzenSMU(cmds, dry_run=True)
        assert getattr(smu, setter)(200) is False


class TestPboScalar:
    def test_valid_dry_run(self):
        assert _smu().set_pbo_scalar(5.0) is True

    @pytest.mark.parametrize("edge", [0.0, 10.0])
    def test_boundaries(self, edge):
        assert _smu().set_pbo_scalar(edge) is True

    @pytest.mark.parametrize("bad", [-0.1, 10.1, 100.0, -50.0])
    def test_out_of_range_raises(self, bad):
        with pytest.raises(ValueError):
            _smu().set_pbo_scalar(bad)


class TestBoostLimit:
    def test_dry_run_ok(self):
        assert _smu().set_boost_limit(5500) is True

    def test_generation_without_command_returns_false(self):
        assert _smu(CPUGeneration.ZEN3_VERMEER).set_boost_limit(5000) is False
