"""Comprehensive tests for RyzenSMU driver interface."""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.smu.commands import CPUGeneration, SMUCommandSet, get_commands
from corecycler.smu.driver import SYSFS_BASE, RyzenSMU, SMUResponse


@pytest.fixture
def zen3_cmds():
    return SMUCommandSet(
        generation=CPUGeneration.ZEN3_VERMEER,
        set_co_cmd=0x35,
        get_co_cmd=0x48,
        set_all_co_cmd=0x36,
        mailbox="mp1",
        co_range=(-30, 30),
        encoding_scheme="zen3",
        uniform_8core_ccds=True,
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
        encoding_scheme="zen4_5",
        uniform_8core_ccds=True,
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
            r.success = False


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
        assert Path("/sys/kernel/ryzen_smu_drv") == SYSFS_BASE


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
        with (
            patch.object(smu, "_send_command", side_effect=self._mock_set_readback(-30)),
            patch.object(smu, "check_writable", return_value=(True, "OK")),
        ):
            assert smu.set_co_offset(0, -30) is True

    def test_set_boundary_min(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with (
            patch.object(smu, "_send_command", side_effect=self._mock_set_readback(-60)),
            patch.object(smu, "check_writable", return_value=(True, "OK")),
        ):
            assert smu.set_co_offset(0, -60) is True

    def test_set_boundary_max(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with (
            patch.object(smu, "_send_command", side_effect=self._mock_set_readback(10)),
            patch.object(smu, "check_writable", return_value=(True, "OK")),
        ):
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
        with (
            patch.object(smu, "_send_command", return_value=fail),
            patch.object(smu, "check_writable", return_value=(True, "OK")),
        ):
            assert smu.set_co_offset(0, -10) is False

    def test_readback_mismatch(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        success = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        wrong = SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24)
        calls = [0]

        def se(cmd, args=(0, 0, 0, 0, 0, 0)):
            calls[0] += 1
            return success if calls[0] == 1 else wrong

        with (
            patch.object(smu, "_send_command", side_effect=se),
            patch.object(smu, "check_writable", return_value=(True, "OK")),
        ):
            assert smu.set_co_offset(0, -10) is False

    def test_permission_denied(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with patch.object(smu, "check_writable", return_value=(False, "No write permission")):
            assert smu.set_co_offset(0, -10) is False

    def test_an_out_of_range_value_raises(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
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


def _stateful_smu(smu, commands):
    """Wire smu to a fake SMU that stores CO per physical (ccd, slot) on SET and
    returns it on GET (so read-back verification passes). Returns the (writes,
    store) it records. Only the sysfs byte boundary is faked; encode/addressing
    is the real code."""
    store: dict[tuple[int, int], int] = {}
    writes: list[tuple[int, int, int]] = []

    def send(cmd, args=(0,) * 6):
        arg = args[0] if args else 0
        ccd = (arg >> 28) & 0xF
        slot = (arg >> 20) & 0xF
        low = arg & 0xFFFF
        if cmd == commands.set_co_cmd:
            val = low - 0x10000 if low >= 0x8000 else low
            store[(ccd, slot)] = val
            writes.append((ccd, slot, val))
            return SMUResponse(success=True, args=(arg,) + (0,) * 5, raw=b"")
        if cmd == commands.get_co_cmd:
            return SMUResponse(success=True, args=(store.get((ccd, slot), 0) & 0xFFFF,) + (0,) * 5, raw=b"")
        return SMUResponse(success=True, args=(0,) * 6, raw=b"")

    smu._send_command = send
    smu.check_writable = lambda: (True, "OK")
    return writes, store


class TestDeterministicSlotMapping:
    """A numbering that proves itself physical (holes at fused slots, or full
    8-core CCDs) is mapped deterministically from core_id -- no SMU or SMN
    traffic. These drive the REAL encode + write + read-back path and confirm a
    CO write lands on the true physical (ccd, slot) on such parts. The ambiguous
    contiguous-harvested numbering (renumbering BIOSes, issue #11) is resolved
    from the SMN core-disable fuse instead -- covered in test_smu_core_map.py."""

    @staticmethod
    def _assert_maps_without_probing(smu_dir, cmds, cores):
        smu = RyzenSMU(cmds, smu_dir)
        topo = MagicMock()
        topo.cores = cores
        calls: list[int] = []
        smu._send_command = lambda cmd, args=(0,) * 6: (
            calls.append(cmd) or SMUResponse(success=True, args=(0,) * 6, raw=b"")
        )
        smu.set_topology(topo)
        assert calls == [], "set_topology sent SMU traffic for an unambiguous topology"
        assert smu.core_map_error is None
        assert smu.core_map == {c: (c // 8, c % 8) for c in cores}
        assert smu._legacy_ccd_by_core == {c: c // 8 for c in cores}

    def test_set_topology_does_not_probe_unambiguous(self, smu_dir, zen5_cmds):
        """A provably-physical numbering must be mapped with NO SMU command."""
        full_two_ccd = {c: MagicMock(ccd=c // 8) for c in range(16)}
        sparse_harvest = {c: MagicMock(ccd=0) for c in (0, 1, 4, 5, 6, 7)}
        for cores in (full_two_ccd, sparse_harvest):
            self._assert_maps_without_probing(smu_dir, zen5_cmds, cores)

    def test_harvested_1ccd_writes_land_on_physical_slot(self, smu_dir, zen5_cmds):
        """5600X/7600X/9600X-style: 6 of 8 on one CCD. Linux core_id is physical
        with holes at the fused slots (2,3), so core_id % 8 addresses the right
        slot and the fused slots are never written."""
        smu = RyzenSMU(zen5_cmds, smu_dir)
        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=0) for c in (0, 1, 4, 5, 6, 7)}
        smu.set_topology(topo)
        writes, store = _stateful_smu(smu, zen5_cmds)
        for c in (0, 1, 4, 5, 6, 7):
            assert smu.set_co_offset(c, -20) is True
            assert store[(0, c % 8)] == -20
        assert all(slot not in (2, 3) for _ccd, slot, _v in writes), "wrote a fused slot"

    def test_harvested_2ccd_writes_land_on_physical_slot(self, smu_dir, zen5_cmds):
        """9900X-style 6+6: CCD0 physical cores 0,1,4,5,6,7; CCD1 physical cores
        8,9,10,11,14,15 (slots 0,1,2,3,6,7). Each write must hit (ccd, core_id % 8)."""
        smu = RyzenSMU(zen5_cmds, smu_dir)
        ccd0 = (0, 1, 4, 5, 6, 7)
        ccd1 = (8, 9, 10, 11, 14, 15)
        topo = MagicMock()
        topo.cores = {**{c: MagicMock(ccd=0) for c in ccd0}, **{c: MagicMock(ccd=1) for c in ccd1}}
        smu.set_topology(topo)
        writes, store = _stateful_smu(smu, zen5_cmds)
        for c in ccd0 + ccd1:
            assert smu.set_co_offset(c, -15) is True
        for c in ccd0:
            assert store[(0, c % 8)] == -15
        for c in ccd1:
            assert store[(1, c % 8)] == -15
        landed = {(ccd, slot) for ccd, slot, _v in writes}
        assert landed == {(0, c % 8) for c in ccd0} | {(1, c % 8) for c in ccd1}, (
            "a write bled across CCDs or into a fused slot"
        )

    def test_readback_of_written_core_matches(self, smu_dir, zen5_cmds):
        """get_co_offset re-derives the same physical slot, so a written value reads
        back on a harvested part."""
        smu = RyzenSMU(zen5_cmds, smu_dir)
        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=0) for c in (0, 1, 4, 5, 6, 7)}
        smu.set_topology(topo)
        _writes, _store = _stateful_smu(smu, zen5_cmds)
        smu.set_co_offset(5, -30)
        assert smu.get_co_offset(5) == -30


class TestPBOLimitValidation:
    """PBO power/current limit setters fail closed on a malformed value BEFORE any
    SMU write -- a negative would otherwise wrap to a huge unsigned limit once
    encoded (value*1000), effectively removing the cap. set_pbo_scalar is covered
    here too (its 0.0-10.0 guard existed but was untested)."""

    @pytest.fixture
    def smu(self, smu_dir):
        smu = RyzenSMU(get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE), smu_dir)
        smu._send_rsmu_command = MagicMock(return_value=SMUResponse(success=True, args=(0,) * 6, raw=b"\x00" * 24))
        return smu

    @pytest.mark.parametrize("setter", ["set_ppt_limit", "set_tdc_limit", "set_edc_limit"])
    def test_in_range_limit_accepted(self, smu, setter):
        assert getattr(smu, setter)(150) is True
        smu._send_rsmu_command.assert_called_once()

    @pytest.mark.parametrize("setter", ["set_ppt_limit", "set_tdc_limit", "set_edc_limit"])
    @pytest.mark.parametrize("bad", [0, -1, -50, 2001, 999999])
    def test_malformed_limit_raises_before_write(self, smu, setter, bad):
        with pytest.raises(ValueError):
            getattr(smu, setter)(bad)
        smu._send_rsmu_command.assert_not_called()

    @pytest.mark.parametrize("scalar", [-0.1, 10.1, 100.0])
    def test_pbo_scalar_out_of_range_raises(self, smu, scalar):
        with pytest.raises(ValueError):
            smu.set_pbo_scalar(scalar)
        smu._send_rsmu_command.assert_not_called()

    def test_pbo_scalar_in_range_accepted(self, smu):
        assert smu.set_pbo_scalar(3.0) is True
        smu._send_rsmu_command.assert_called_once()

    def test_unsupported_generation_returns_false_not_raises(self, smu_dir, zen3_cmds):
        """A generation without the PBO command returns False (capability check)
        before validation -- never raises on a missing command."""
        smu = RyzenSMU(zen3_cmds, smu_dir)
        assert smu.set_ppt_limit(150) is False
        assert smu.set_ppt_limit(-999) is False


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


class TestDriverWriteReadBranches:
    def _zen5_full(self):
        return get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)

    def _no_co(self):
        return SMUCommandSet(
            generation=CPUGeneration.ZEN2_MATISSE,
            co_range=(0, 0),
            mailbox="rsmu",
            encoding_scheme="none",
        )

    def _ok(self):
        return SMUResponse(success=True, args=(0,) * 6, raw=b"")

    def _resp(self, arg0):
        return SMUResponse(success=True, args=(arg0, 0, 0, 0, 0, 0), raw=b"")

    def test_send_rsmu_pads_args_and_fails_closed_on_write_error(self, tmp_path, zen5_cmds):
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "smu_args").mkdir()
        (smu_dir / "rsmu_cmd").write_bytes(struct.pack("<I", 1))
        smu = RyzenSMU(zen5_cmds, smu_dir)
        resp = smu._send_rsmu_command(0x70, (1,))
        assert resp.success is False

    def test_get_co_offset_none_without_co(self, smu_dir):
        assert RyzenSMU(self._no_co(), smu_dir).get_co_offset(0) is None

    def test_set_co_offset_false_without_co(self, smu_dir):
        assert RyzenSMU(self._no_co(), smu_dir).set_co_offset(0, 0) is False

    def test_set_all_co_false_without_co(self, smu_dir):
        assert RyzenSMU(self._no_co(), smu_dir).set_all_co(0) is False

    def test_set_all_co_out_of_range_raises(self, smu_dir, zen5_cmds):
        with pytest.raises(ValueError, match="out of range"):
            RyzenSMU(zen5_cmds, smu_dir).set_all_co(100)

    def test_set_all_co_readback_mismatch_fails(self, smu_dir, zen5_cmds):
        smu = RyzenSMU(zen5_cmds, smu_dir)
        with (
            patch.object(smu, "_send_command", return_value=self._ok()),
            patch.object(smu, "get_co_offset", return_value=-5),
        ):
            assert smu.set_all_co(-10) is False

    def test_set_all_co_no_set_all_cmd_returns_false(self, smu_dir):
        cmds = SMUCommandSet(
            generation=CPUGeneration.ZEN5_GRANITE_RIDGE,
            co_range=(-50, 10),
            mailbox="rsmu",
            encoding_scheme="zen4_5",
            set_co_cmd=0x06,
            get_co_cmd=0xD5,
        )
        assert RyzenSMU(cmds, smu_dir).set_all_co(-10) is False

    def test_set_ppt_limit_real_write(self, smu_dir):
        smu = RyzenSMU(self._zen5_full(), smu_dir)
        with patch.object(smu, "_send_rsmu_command", return_value=self._ok()) as m:
            assert smu.set_ppt_limit(200) is True
        m.assert_called_once()

    def test_set_tdc_limit_real_write(self, smu_dir):
        smu = RyzenSMU(self._zen5_full(), smu_dir)
        with patch.object(smu, "_send_rsmu_command", return_value=self._ok()):
            assert smu.set_tdc_limit(160) is True

    def test_set_edc_limit_real_write(self, smu_dir):
        smu = RyzenSMU(self._zen5_full(), smu_dir)
        with patch.object(smu, "_send_rsmu_command", return_value=self._ok()):
            assert smu.set_edc_limit(200) is True

    def test_get_pbo_scalar_none_without_cmd(self, smu_dir, zen3_cmds):
        assert RyzenSMU(zen3_cmds, smu_dir).get_pbo_scalar() is None

    def test_get_pbo_scalar_reads_float(self, smu_dir):
        smu = RyzenSMU(self._zen5_full(), smu_dir)
        bits = struct.unpack("<I", struct.pack("<f", 3.0))[0]
        with patch.object(smu, "_send_rsmu_command", return_value=self._resp(bits)):
            assert smu.get_pbo_scalar() == pytest.approx(3.0)

    def test_set_pbo_scalar_none_without_cmd(self, smu_dir, zen3_cmds):
        assert RyzenSMU(zen3_cmds, smu_dir).set_pbo_scalar(2.0) is False

    def test_set_pbo_scalar_real_write(self, smu_dir):
        smu = RyzenSMU(self._zen5_full(), smu_dir)
        with patch.object(smu, "_send_rsmu_command", return_value=self._ok()):
            assert smu.set_pbo_scalar(3.0) is True

    def test_get_fastest_core_none_without_cmd(self, smu_dir, zen3_cmds):
        assert RyzenSMU(zen3_cmds, smu_dir).get_fastest_core() is None

    def _vermeer_full(self):
        return get_commands(CPUGeneration.ZEN3_VERMEER)

    def test_get_fastest_core_reads_index_on_zen3(self, smu_dir):
        smu = RyzenSMU(self._vermeer_full(), smu_dir)
        with patch.object(smu, "_send_rsmu_command", return_value=self._resp(5)):
            assert smu.get_fastest_core() == 5

    def test_get_fastest_core_refused_on_zen5(self, smu_dir):
        """RSMU 0x59 on Zen 4/5 is SetTctlMax, not a fastest-core read — the
        command set carries None there so no thermal-limit write can masquerade
        as a query."""
        smu = RyzenSMU(self._zen5_full(), smu_dir)
        with patch.object(smu, "_send_rsmu_command", return_value=self._resp(5)) as send:
            assert smu.get_fastest_core() is None
        send.assert_not_called()


class TestOneMailboxAcrossProcesses:
    @pytest.mark.parametrize(("call", "anchor"), [("command", "smu_args"), ("rsmu", "smu_args"), ("smn", "smn")])
    def test_a_sequence_waits_while_another_process_holds_its_file(self, smu_dir, zen5_cmds, call, anchor):
        import fcntl
        import threading

        (smu_dir / "smn").write_bytes(struct.pack("<I", 7))
        smu = RyzenSMU(zen5_cmds, smu_dir)
        calls = {
            "command": lambda: smu._send_command(0x99),
            "rsmu": lambda: smu._send_rsmu_command(0x99),
            "smn": lambda: smu.read_smn(0x10),
        }
        done = threading.Event()
        worker = threading.Thread(target=lambda: (calls[call](), done.set()))
        with (smu_dir / anchor).open("rb") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            worker.start()
            assert not done.wait(0.3)
        assert done.wait(5)
        worker.join()

    def test_without_the_mailbox_file_a_command_fails_closed(self, smu_dir, zen5_cmds):
        (smu_dir / "smu_args").unlink()
        response = RyzenSMU(zen5_cmds, smu_dir)._send_command(0x99)
        assert response.success is False
        assert RyzenSMU(zen5_cmds, smu_dir)._send_rsmu_command(0x99).success is False


SHAPER_WORDS_READ_ON_THE_9950X3D = (0x0, 0xF4E5E201, 0xF3EEE702, 0xF6EFEB03, 0xFEFDFA04, 0x0)


@pytest.mark.req("TERMS.base_shaper", "D4")
@pytest.mark.parametrize(
    ("success", "words", "bands"),
    [
        (
            True,
            SHAPER_WORDS_READ_ON_THE_9950X3D,
            ((0, 0, 0), (-30, -27, -12), (-25, -18, -13), (-21, -17, -10), (-6, -3, -2)),
        ),
        (False, SHAPER_WORDS_READ_ON_THE_9950X3D, None),
        (True, (0,) * 6, None),
        (True, (0x0, 0xF4E5E202, 0xF3EEE701, 0xF6EFEB03, 0xFEFDFA04, 0x0), None),
    ],
)
def test_the_curve_shaper_read_gives_five_bands_only_when_every_band_names_its_own_index(success, words, bands):
    smu = RyzenSMU(get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE), MagicMock())
    smu._send_rsmu_command = lambda cmd, args=(0,) * 6: SMUResponse(success, words, b"")
    assert smu.get_curve_shaper() == bands


WRITES_FOR_THE_READ_TABLE = [0xF4E5E281, 0xF3EEE782, 0xF6EFEB83, 0xFEFDFA84]
TABLE_WITH_MIN_RAISED = ((1, 1, 1), (-30, -27, -12), (-25, -18, -13), (-21, -17, -10), (-6, -3, -2))


def shaper_smu(refuse: int | None = None, keep: bool = True):
    smu = RyzenSMU(get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE), MagicMock())
    smu.check_writable = lambda: (True, "OK")
    table = {band: band for band in range(5)}
    sent = []

    def rsmu(cmd, args=(0,) * 6):
        if cmd == smu.commands.set_curve_shaper_cmd:
            sent.append(args[0])
            band = args[0] & 0x7F
            if band == refuse:
                return SMUResponse(False, (0,) * 6, b"")
            if keep:
                table[band] = args[0] & ~0xFF | band
            return SMUResponse(True, (args[0],) + (0,) * 5, b"")
        return SMUResponse(True, (*(table[band] for band in range(5)), 0), b"")

    smu._send_rsmu_command = rsmu
    return smu, sent


@pytest.mark.req("H5.w")
def test_a_shaper_write_sends_every_band_as_measured_and_needs_the_whole_table_back():
    smu, sent = shaper_smu()
    assert smu.set_curve_shaper(TABLE_WITH_MIN_RAISED) is None
    assert sent == [0x01010180, *WRITES_FOR_THE_READ_TABLE]
    assert smu.get_curve_shaper() == TABLE_WITH_MIN_RAISED


@pytest.mark.parametrize(
    ("refuse", "keep", "says"),
    [
        (2, True, "the SMU refused band Medium"),
        (None, False, "the read-back gave ((0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0))"),
    ],
)
def test_a_shaper_write_that_is_refused_or_not_read_back_says_so(refuse, keep, says):
    smu, _ = shaper_smu(refuse, keep)
    assert smu.set_curve_shaper(TABLE_WITH_MIN_RAISED) == says


@pytest.mark.parametrize(
    "bands",
    [
        TABLE_WITH_MIN_RAISED[:4],
        ((31, 0, 0), *TABLE_WITH_MIN_RAISED[1:]),
        ((-51, 0, 0), *TABLE_WITH_MIN_RAISED[1:]),
        ((0, 0), *TABLE_WITH_MIN_RAISED[1:]),
    ],
)
def test_a_malformed_shaper_is_refused_before_anything_is_sent(bands):
    smu, sent = shaper_smu()
    with pytest.raises(ValueError, match="five bands of three margins within -50..30"):
        smu.set_curve_shaper(bands)
    assert sent == []


def test_an_unwritable_driver_sends_no_shaper_write():
    smu, sent = shaper_smu()
    smu.check_writable = lambda: (False, "smu_args is not writable")
    assert smu.set_curve_shaper(TABLE_WITH_MIN_RAISED) == "smu_args is not writable"
    assert sent == []


def test_smu_problem_names_smu_args_and_the_error_read_when_it_cannot_be_opened_for_writing(tmp_path):
    import errno
    import os

    from corecycler.smu.driver import smu_problem

    args = tmp_path / "smu_args"
    assert smu_problem(tmp_path) == f"{args}: {os.strerror(errno.ENOENT)}"
    args.write_bytes(b"")
    assert smu_problem(tmp_path) is None
    if os.geteuid() != 0:
        args.chmod(0o444)
        assert smu_problem(tmp_path) == f"{args}: {os.strerror(errno.EACCES)}"
