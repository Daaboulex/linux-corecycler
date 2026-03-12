"""Comprehensive tests for RyzenSMU driver interface."""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from smu.commands import CPUGeneration, SMUCommandSet, encode_co_arg
from smu.driver import SYSFS_BASE, RyzenSMU, SMUResponse


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def zen3_cmds():
    return SMUCommandSet(
        generation=CPUGeneration.ZEN3_VERMEER,
        set_co_cmd=0x35,
        get_co_cmd=0x48,
        set_all_co_cmd=0x36,
        mailbox="mp1",
        co_range=(-30, 30),
    )


@pytest.fixture
def zen5_cmds():
    return SMUCommandSet(
        generation=CPUGeneration.ZEN5_GRANITE_RIDGE,
        set_co_cmd=0x06,
        get_co_cmd=0xD5,
        set_all_co_cmd=0x07,
        mailbox="rsmu",
        co_range=(-60, 10),
        set_boost_limit_cmd=0x70,
        get_boost_limit_cmd=0x6E,
    )


@pytest.fixture
def smu_dir(tmp_path):
    smu_dir = tmp_path / "ryzen_smu_drv"
    smu_dir.mkdir()
    (smu_dir / "smu_args").write_bytes(struct.pack("<6I", 0, 0, 0, 0, 0, 0))
    (smu_dir / "rsmu_cmd").write_bytes(struct.pack("<I", 1))
    (smu_dir / "mp1_smu_cmd").write_bytes(struct.pack("<I", 1))
    return smu_dir


class TestSMUResponse:
    def test_success(self):
        r = SMUResponse(success=True, args=(1, 2, 3, 0, 0, 0), raw=b"\x00" * 24)
        assert r.success is True
        assert r.args[0] == 1

    def test_failure(self):
        r = SMUResponse(success=False, args=(0, 0, 0, 0, 0, 0), raw=b"\x00" * 24)
        assert r.success is False

    def test_frozen(self):
        r = SMUResponse(success=True, args=(0,) * 6, raw=b"")
        with pytest.raises(AttributeError):
            r.success = False  # type: ignore[misc]


class TestIsAvailable:
    def test_available_when_sysfs_exists(self, smu_dir):
        assert RyzenSMU.is_available(smu_dir) is True

    def test_not_available_missing_dir(self, tmp_path):
        assert RyzenSMU.is_available(tmp_path / "nonexistent") is False

    def test_not_available_missing_smu_args(self, tmp_path):
        d = tmp_path / "ryzen_smu_drv"
        d.mkdir()
        assert RyzenSMU.is_available(d) is False

    def test_default_path(self):
        assert SYSFS_BASE == Path("/sys/kernel/ryzen_smu_drv")


class TestGetCmdPath:
    def test_mp1_mailbox(self, smu_dir, zen3_cmds):
        smu = RyzenSMU(zen3_cmds, smu_dir)
        assert smu._get_cmd_path() == smu_dir / "mp1_smu_cmd"

    def test_rsmu_mailbox(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        assert smu._get_cmd_path() == smu_dir / "rsmu_cmd"

    def test_get_cmd_filename(self, smu_dir, zen3_cmds, zen5_cmds):
        assert RyzenSMU(zen3_cmds, smu_dir)._get_cmd_filename() == "mp1_smu_cmd"
        assert RyzenSMU(zen5_cmds, smu_dir)._get_cmd_filename() == "rsmu_cmd"


class TestSendCommand:
    @staticmethod
    def _patch_write(monkeypatch, smu_dir, cmd_name, status=1):
        _orig = Path.write_bytes
        def _sim(self_path, data):
            _orig(self_path, data)
            if self_path.name == cmd_name and self_path.parent == smu_dir:
                _orig(self_path, struct.pack("<I", status))
        monkeypatch.setattr(Path, "write_bytes", _sim)

    def test_basic_send_success(self, smu_dir, zen5_cmds, monkeypatch):
        self._patch_write(monkeypatch, smu_dir, "rsmu_cmd", 1)
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = smu._send_command(0x6, (0xDEAD,))
        assert resp.success is True
        assert len(resp.args) == 6

    def test_args_padded(self, smu_dir, zen5_cmds, monkeypatch):
        self._patch_write(monkeypatch, smu_dir, "rsmu_cmd")
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = smu._send_command(0x6, (42,))
        assert resp.success is True

    def test_args_truncated(self, smu_dir, zen5_cmds, monkeypatch):
        self._patch_write(monkeypatch, smu_dir, "rsmu_cmd")
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = smu._send_command(0x6, (1, 2, 3, 4, 5, 6, 7, 8))
        assert resp.success is True

    def test_failure_response(self, smu_dir, zen5_cmds, monkeypatch):
        self._patch_write(monkeypatch, smu_dir, "rsmu_cmd", 0)
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = smu._send_command(0x6)
        assert resp.success is False

    def test_mp1_path(self, smu_dir, zen3_cmds, monkeypatch):
        self._patch_write(monkeypatch, smu_dir, "mp1_smu_cmd")
        smu = RyzenSMU(zen3_cmds, smu_dir)
        resp = smu._send_command(0x35, (0,))
        assert resp.success is True


class TestGetCOOffset:
    def test_read_zero(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=resp):
            assert smu.get_co_offset(0) == 0

    def test_read_negative(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=True, args=(0xFFF6, 0, 0, 0, 0, 0), raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=resp):
            assert smu.get_co_offset(0) == -10

    def test_read_positive(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=True, args=(5, 0, 0, 0, 0, 0), raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=resp):
            assert smu.get_co_offset(0) == 5

    def test_returns_none_on_failure(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=False, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=resp):
            assert smu.get_co_offset(0) is None

    def test_different_core_ids(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        for cid in [0, 1, 7, 15]:
            with patch.object(smu, "_send_command", return_value=resp):
                assert smu.get_co_offset(cid) is not None

    def test_read_max_negative_zen5(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=True, args=(0xFFC4, 0, 0, 0, 0, 0), raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=resp):
            assert smu.get_co_offset(0) == -60


class TestSetCOOffset:
    @staticmethod
    def _mock_set_readback(value):
        success = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        raw_rb = value & 0xFFFF
        readback = SMUResponse(success=True, args=(raw_rb, 0, 0, 0, 0, 0), raw=b"\x00" * 24)
        calls = [0]
        def side_effect(cmd, args=(0, 0, 0, 0, 0, 0)):
            calls[0] += 1
            return success if calls[0] == 1 else readback
        return side_effect

    def test_set_valid(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "_send_command", side_effect=self._mock_set_readback(-30)), \
             patch.object(smu, "check_writable", return_value=(True, "OK")):
            assert smu.set_co_offset(0, -30) is True

    def test_set_boundary_min(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "_send_command", side_effect=self._mock_set_readback(-60)), \
             patch.object(smu, "check_writable", return_value=(True, "OK")):
            assert smu.set_co_offset(0, -60) is True

    def test_set_boundary_max(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "_send_command", side_effect=self._mock_set_readback(10)), \
             patch.object(smu, "check_writable", return_value=(True, "OK")):
            assert smu.set_co_offset(0, 10) is True

    def test_out_of_range_low(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with pytest.raises(ValueError, match="CO value -61 out of range"):
            smu.set_co_offset(0, -61)

    def test_out_of_range_high(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with pytest.raises(ValueError, match="CO value 11 out of range"):
            smu.set_co_offset(0, 11)

    def test_out_of_range_zen3(self, smu_dir, zen3_cmds):
        smu = RyzenSMU(zen3_cmds, smu_dir)
        with pytest.raises(ValueError, match="CO value -31 out of range"):
            smu.set_co_offset(0, -31)

    def test_above_max_rejected_zen3(self, smu_dir, zen3_cmds):
        smu = RyzenSMU(zen3_cmds, smu_dir)
        with pytest.raises(ValueError, match="CO value 31 out of range"):
            smu.set_co_offset(0, 31)

    def test_smu_rejection(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        fail = SMUResponse(success=False, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=fail), \
             patch.object(smu, "check_writable", return_value=(True, "OK")):
            assert smu.set_co_offset(0, -10) is False

    def test_readback_mismatch(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        success = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        wrong = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        calls = [0]
        def se(cmd, args=(0, 0, 0, 0, 0, 0)):
            calls[0] += 1
            return success if calls[0] == 1 else wrong
        with patch.object(smu, "_send_command", side_effect=se), \
             patch.object(smu, "check_writable", return_value=(True, "OK")):
            assert smu.set_co_offset(0, -10) is False

    def test_permission_denied(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "check_writable", return_value=(False, "No write permission")):
            assert smu.set_co_offset(0, -10) is False

    def test_dry_run(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir, dry_run=True)
        assert smu.set_co_offset(0, -30) is True

    def test_dry_run_validates_range(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir, dry_run=True)
        with pytest.raises(ValueError):
            smu.set_co_offset(0, -100)


class TestResetAllCO:
    def test_reset_zen3(self, smu_dir, zen3_cmds):
        smu = RyzenSMU(zen3_cmds, smu_dir)
        success = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=success):
            assert smu.reset_all_co() is True

    def test_reset_zen5(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        success = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=success):
            assert smu.reset_all_co() is True

    def test_reset_failure(self, smu_dir, zen3_cmds):
        smu = RyzenSMU(zen3_cmds, smu_dir)
        fail = SMUResponse(success=False, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_command", return_value=fail):
            assert smu.reset_all_co() is False

    def test_reset_dry_run(self, smu_dir, zen3_cmds):
        assert RyzenSMU(zen3_cmds, smu_dir, dry_run=True).reset_all_co() is True


class TestGetAllCOOffsets:
    def test_reads_all(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "get_co_offset", return_value=0):
            offsets = smu.get_all_co_offsets(4)
        assert len(offsets) == 4 and all(v == 0 for v in offsets.values())

    def test_zero_cores(self, smu_dir, zen5_cmds):
        assert RyzenSMU(zen5_cmds, smu_dir).get_all_co_offsets(0) == {}

    def test_handles_failure(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "get_co_offset", return_value=None):
            assert all(v is None for v in smu.get_all_co_offsets(2).values())

    def test_mixed(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "get_co_offset", side_effect=[-10, None, -5, 0]):
            assert smu.get_all_co_offsets(4) == {0: -10, 1: None, 2: -5, 3: 0}


class TestBoostLimit:
    def test_get(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=True, args=(5700, 0, 0, 0, 0, 0), raw=b"\x00" * 24)
        with patch.object(smu, "_send_rsmu_command", return_value=resp):
            assert smu.get_boost_limit() == 5700

    def test_get_unsupported(self, smu_dir, zen3_cmds):
        assert RyzenSMU(zen3_cmds, smu_dir).get_boost_limit() is None

    def test_get_failure(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=False, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_rsmu_command", return_value=resp):
            assert smu.get_boost_limit() is None

    def test_set(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_rsmu_command", return_value=resp):
            assert smu.set_boost_limit(5500) is True

    def test_set_unsupported(self, smu_dir, zen3_cmds):
        assert RyzenSMU(zen3_cmds, smu_dir).set_boost_limit(5500) is False

    def test_set_failure(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = SMUResponse(success=False, args=(0,) * 6, raw=b"\x00" * 24)
        with patch.object(smu, "_send_rsmu_command", return_value=resp):
            assert smu.set_boost_limit(5500) is False

    def test_set_dry_run(self, smu_dir, zen5_cmds):
        assert RyzenSMU(zen5_cmds, smu_dir, dry_run=True).set_boost_limit(5500) is True


class TestCheckWritable:
    def test_writable(self, smu_dir, zen5_cmds):
        ok, _ = RyzenSMU(zen5_cmds, smu_dir).check_writable()
        assert ok is True

    def test_missing_file(self, tmp_path, zen5_cmds):
        d = tmp_path / "ryzen_smu_drv"
        d.mkdir()
        (d / "smu_args").write_bytes(b"\x00" * 24)
        ok, msg = RyzenSMU(zen5_cmds, d).check_writable()
        assert ok is False and "not found" in msg

    def test_no_write_permission(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch("os.access", return_value=False):
            ok, msg = smu.check_writable()
        assert ok is False and "permission" in msg.lower()


class TestBackupRestore:
    def test_backup(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "get_co_offset", side_effect=[-10, -5, 0, -15]):
            backup = smu.backup_co_offsets(4)
        assert backup == {0: -10, 1: -5, 2: 0, 3: -15}
        assert smu.has_backup()

    def test_backup_excludes_none(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "get_co_offset", side_effect=[-10, None, 0, None]):
            assert smu.backup_co_offsets(4) == {0: -10, 2: 0}

    def test_restore_no_backup(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        ok, _ = smu.restore_co_offsets()
        assert ok is False

    def test_restore_success(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        smu._backup = {0: -10, 1: -5}
        with patch.object(smu, "set_co_offset", return_value=True):
            ok, failed = smu.restore_co_offsets()
        assert ok is True and failed == []

    def test_restore_partial_failure(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        smu._backup = {0: -10, 1: -5, 2: 0}
        with patch.object(smu, "set_co_offset", side_effect=[True, False, True]):
            ok, failed = smu.restore_co_offsets()
        assert ok is False and failed == [1]
