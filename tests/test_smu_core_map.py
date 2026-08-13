"""OS-core to SMU-slot map discovery (issue #11): renumbered harvested parts.

Some BIOS/AGESA builds renumber /proc/cpuinfo core ids contiguously on
harvested parts instead of leaving holes at the fused-off slots, so the
core_id-derived SMU address hits dead or wrong slots and CO read/write fails.
set_topology discovers the mapping: numberings that prove themselves (holes,
full CCDs) are used directly, an ambiguous CCD has its SMN core-disable fuse
read, and an undiscoverable map refuses per-core CO everywhere (driver, GUI,
tuner start/resume/validate) instead of tuning a different core than the one
under test.

The fake below models what issue #11 actually reported: the CO read answers on
EVERY in-range slot, fused-off ones included, so it can never isolate them --
only the fuse can.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from corecycler.smu import driver as drv
from corecycler.smu.commands import (
    COMMAND_SETS,
    CPUGeneration,
    get_commands,
)
from corecycler.smu.driver import RyzenSMU, SMUResponse, core_map_blocked

VERMEER = get_commands(CPUGeneration.ZEN3_VERMEER)
ZEN5 = get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)
CEZANNE = get_commands(CPUGeneration.ZEN3_CEZANNE)

_SMN_DENIED = "no write permission on /sys/kernel/ryzen_smu_drv/smn — root-only"


def _topo(cores: dict[int, int]) -> MagicMock:
    topo = MagicMock()
    topo.cores = {cid: MagicMock(ccd=ccd) for cid, ccd in cores.items()}
    return topo


class _FakeSilicon:
    """Mailbox + SMN simulator with real Zen discrimination behaviour.

    The CO mailbox answers and stores for every in-range slot regardless of
    whether that slot is fused off -- the reported 5600X behaviour that made
    the old probe useless. Which slots physically exist lives ONLY in the SMN
    core-disable fuse, built here from ``live`` (ccd -> live physical slots);
    a ccd absent from ``live`` has no readable fuse.
    """

    def __init__(self, smu: RyzenSMU, commands, live: dict[int, set[int]], *, smn: bool = True) -> None:
        self.commands = commands
        self.live = live
        self.store: dict[tuple[int, int], int] = {}
        self.writes: list[tuple[int, int, int]] = []
        self.get_calls: list[tuple[int, int]] = []
        self.fuse_reads: list[int] = []
        smu._send_command = self._send
        smu._send_rsmu_command = self._send
        smu.check_writable = lambda: (True, "OK")
        smu.check_smn_readable = lambda: (True, "OK") if smn else (False, _SMN_DENIED)
        smu.read_smn = self._read_smn

    def _read_smn(self, address: int) -> int | None:
        self.fuse_reads.append(address)
        base = self.commands.core_fuse_addr
        ccd = (address - base) >> 25
        if ccd not in self.live:
            return None
        return sum(1 << s for s in range(8) if s not in self.live[ccd])

    def _send(self, cmd, args=(0,) * 6):
        arg = args[0] if args else 0
        ccd = (arg >> 28) & 0xF
        slot = (arg >> 20) & 0xF
        low = arg & 0xFFFF
        value = low - 0x10000 if low >= 0x8000 else low
        if cmd == self.commands.get_co_cmd:
            self.get_calls.append((ccd, slot))
            stored = self.store.get((ccd, slot), 0) & 0xFFFF
            return SMUResponse(success=True, args=(stored,) + (0,) * 5, raw=b"")
        if cmd == self.commands.set_co_cmd:
            self.store[(ccd, slot)] = value
            self.writes.append((ccd, slot, value))
            return SMUResponse(success=True, args=(arg,) + (0,) * 5, raw=b"")
        if cmd == self.commands.set_all_co_cmd:
            for ccd_key, slots in self.live.items():
                for live_slot in slots:
                    self.store[(ccd_key, live_slot)] = value
            return SMUResponse(success=True, args=(0,) * 6, raw=b"")
        return SMUResponse(success=True, args=(0,) * 6, raw=b"")


def _mapped_smu(commands, cores: dict[int, int], live: dict[int, set[int]], *, smn: bool = True, **kw):
    smu = RyzenSMU(commands, MagicMock(), **kw)
    silicon = _FakeSilicon(smu, commands, live, smn=smn)
    smu.set_topology(_topo(cores))
    return smu, silicon


def _fuse_addr(commands, ccd: int) -> int:
    return commands.core_fuse_addr + (ccd << 25)


REPORTED_5600X_LIVE_SLOTS = {0, 1, 4, 5, 6, 7}


class TestRenumberedHarvested:
    def test_5600x_renumbered_maps_onto_live_slots(self):
        smu, silicon = _mapped_smu(
            VERMEER,
            {c: 0 for c in range(6)},
            {0: REPORTED_5600X_LIVE_SLOTS},
        )
        assert smu.core_map_error is None
        assert smu.core_map == {0: (0, 0), 1: (0, 1), 2: (0, 4), 3: (0, 5), 4: (0, 6), 5: (0, 7)}
        assert silicon.fuse_reads == [_fuse_addr(VERMEER, 0)]

    def test_discovery_sends_no_co_traffic_at_all(self):
        """The CO read is not a discriminator, so discovery must not use it."""
        _smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        assert silicon.get_calls == []
        assert silicon.writes == []

    def test_write_lands_on_mapped_slot_and_reads_back(self):
        smu, silicon = _mapped_smu(
            VERMEER,
            {c: 0 for c in range(6)},
            {0: REPORTED_5600X_LIVE_SLOTS},
        )
        assert smu.set_co_offset(2, -10) is True
        assert silicon.store[(0, 4)] == -10
        assert smu.get_co_offset(2) == -10
        assert all(slot not in (2, 3) for _ccd, slot, _v in silicon.writes)

    def test_renumbered_two_ccd_maps_each_ccd(self):
        cores = {c: (0 if c < 6 else 1) for c in range(12)}
        live = {0: {0, 1, 2, 3, 6, 7}, 1: {0, 1, 4, 5, 6, 7}}
        smu, silicon = _mapped_smu(ZEN5, cores, live)
        assert smu.core_map_error is None
        assert smu.core_map == {
            0: (0, 0),
            1: (0, 1),
            2: (0, 2),
            3: (0, 3),
            4: (0, 6),
            5: (0, 7),
            6: (1, 0),
            7: (1, 1),
            8: (1, 4),
            9: (1, 5),
            10: (1, 6),
            11: (1, 7),
        }
        assert silicon.fuse_reads == [_fuse_addr(ZEN5, 0), _fuse_addr(ZEN5, 1)]

    def test_full_ccd_needs_no_fuse_read(self):
        cores = {c: (0 if c < 8 else 1) for c in range(14)}
        live = {0: set(range(8)), 1: {0, 1, 2, 4, 5, 7}}
        smu, silicon = _mapped_smu(ZEN5, cores, live)
        assert smu.core_map_error is None
        assert silicon.fuse_reads == [_fuse_addr(ZEN5, 1)]
        assert smu.core_map[7] == (0, 7)
        assert smu.core_map[13] == (1, 7)

    def test_mp1_generation_maps_without_touching_its_mailbox(self):
        smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        assert smu._get_cmd_filename() == "mp1_smu_cmd"
        assert smu.core_map is not None
        assert silicon.get_calls == []


class TestFuseFailClosed:
    def test_indiscriminate_co_read_does_not_decide_the_map(self, caplog):
        """Issue #11 follow-up: every slot answering must not block discovery,
        and a fuse that then disagrees with the OS must refuse, not guess."""
        smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))})
        assert silicon.get_calls == []
        assert smu.core_map is None
        assert "disagrees with the OS" in smu.core_map_error
        assert "8 live slots" in smu.core_map_error
        assert "OS reports 6 cores" in smu.core_map_error
        with caplog.at_level(logging.ERROR):
            assert smu.get_co_offset(0) is None
            assert smu.set_co_offset(0, -5) is False
            assert smu.set_all_co(-5) is False
            assert smu.reset_all_co() is False
        assert "refused" in caplog.text
        assert silicon.writes == []

    def test_unreadable_smn_names_the_permission_fix(self):
        smu, silicon = _mapped_smu(
            VERMEER,
            {c: 0 for c in range(6)},
            {0: REPORTED_5600X_LIVE_SLOTS},
            smn=False,
        )
        assert smu.core_map is None
        assert _SMN_DENIED in smu.core_map_error
        assert "core-disable fuse" in smu.core_map_error
        assert silicon.fuse_reads == []

    def test_failed_fuse_read_refuses_instead_of_reusing_a_stale_value(self):
        smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {})
        assert smu.core_map is None
        assert "fuse read failed" in smu.core_map_error
        assert silicon.fuse_reads == [_fuse_addr(VERMEER, 0)]

    def test_generation_without_a_verified_fuse_address_refuses(self):
        smu, silicon = _mapped_smu(CEZANNE, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        assert CEZANNE.core_fuse_addr is None
        assert smu.core_map is None
        assert "ZEN3_CEZANNE" in smu.core_map_error
        assert silicon.fuse_reads == []

    def test_dry_run_refuses_an_unaddressable_write(self):
        smu, _silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))}, dry_run=True)
        assert smu.set_co_offset(0, -5) is False
        assert smu.reset_all_co() is False

    def test_unknown_core_id_refuses_instead_of_guessing(self):
        smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        silicon.writes.clear()
        assert smu.set_co_offset(17, -5) is False
        assert smu.get_co_offset(17) is None
        assert silicon.writes == []

    def test_out_of_range_value_still_raises_before_addressing(self):
        smu, _silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))})
        with pytest.raises(ValueError, match="out of range"):
            smu.set_co_offset(0, -99)


class TestSmnNodeIO:
    """The real sysfs protocol: an SMN read is a write of the address."""

    def test_read_smn_writes_the_address_and_decodes_the_reply(self, tmp_path):
        (tmp_path / "smn").write_bytes(b"\x00" * 4)
        smu = RyzenSMU(VERMEER, tmp_path)
        assert smu.check_smn_readable() == (True, "OK")
        assert smu.read_smn(0x30081D98) == 0x30081D98

    def test_missing_smn_node_is_reported_not_guessed(self, tmp_path):
        smu = RyzenSMU(VERMEER, tmp_path / "ryzen_smu_drv_absent")
        ok, msg = smu.check_smn_readable()
        assert not ok and "not found" in msg
        assert smu.read_smn(0x30081D98) is None

    def test_unwritable_smn_node_names_the_root_only_default(self, tmp_path, monkeypatch):
        (tmp_path / "smn").write_bytes(b"\x00" * 4)
        monkeypatch.setattr(drv.os, "access", lambda *_a, **_k: False)
        smu = RyzenSMU(VERMEER, tmp_path)
        ok, msg = smu.check_smn_readable()
        assert not ok
        assert "root-only" in msg and "corecycler group" in msg


class TestCoreMapBlockedHelper:
    def test_none_smu_is_not_blocked(self):
        assert core_map_blocked(None) is None

    def test_magicmock_smu_is_not_blocked(self):
        assert core_map_blocked(MagicMock()) is None

    def test_error_string_blocks(self):
        assert core_map_blocked(MagicMock(core_map_error="renumbered")) == "renumbered"

    def test_healthy_driver_is_not_blocked(self):
        smu, _silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        assert core_map_blocked(smu) is None


class TestGenerationGating:
    def test_strix_point_keeps_legacy_addressing_untouched(self):
        strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
        assert strix is not None and not strix.uniform_8core_ccds
        smu = RyzenSMU(strix, MagicMock())
        silicon = _FakeSilicon(smu, strix, {0: set(range(8)), 1: set(range(8))})
        smu.set_topology(_topo({c: (0 if c < 4 else 1) for c in range(12)}))
        assert silicon.fuse_reads == []
        assert smu.core_map is None
        assert smu.core_map_error is None
        assert smu.set_co_offset(9, -5) is True
        assert silicon.writes == [(1, 1, -5)]

    def test_zen2_has_no_co_and_no_map(self):
        matisse = get_commands(CPUGeneration.ZEN2_MATISSE)
        smu = RyzenSMU(matisse, MagicMock())
        silicon = _FakeSilicon(smu, matisse, {})
        smu.set_topology(_topo({c: 0 for c in range(6)}))
        assert silicon.fuse_reads == []
        assert smu.core_map is None
        assert smu.core_map_error is None
        assert smu.get_co_offset(0) is None

    def test_dense_l3_group_falls_back_to_legacy(self):
        smu = RyzenSMU(ZEN5, MagicMock())
        silicon = _FakeSilicon(smu, ZEN5, {0: set(range(8))})
        smu.set_topology(_topo({c: 0 for c in range(12)}))
        assert silicon.fuse_reads == []
        assert smu.core_map is None
        assert smu.core_map_error is None

    def test_the_verified_generation_set_is_deliberate(self):
        mapped = {g for g, c in COMMAND_SETS.items() if c.uniform_8core_ccds}
        assert mapped == {
            CPUGeneration.ZEN3_VERMEER,
            CPUGeneration.ZEN3_CHAGALL,
            CPUGeneration.ZEN3D_WARHOL,
            CPUGeneration.ZEN3_CEZANNE,
            CPUGeneration.ZEN3_REMBRANDT,
            CPUGeneration.ZEN4_RAPHAEL,
            CPUGeneration.ZEN4_DRAGON_RANGE,
            CPUGeneration.ZEN4_STORM_PEAK,
            CPUGeneration.ZEN4_PHOENIX,
            CPUGeneration.ZEN5_GRANITE_RIDGE,
            CPUGeneration.ZEN5_SHIMADA_PEAK,
        }

    def test_only_verified_dies_carry_a_fuse_address(self):
        """A die with no grounded fuse address must fail closed, never guess a
        neighbouring generation's address."""
        fused = {g: c.core_fuse_addr for g, c in COMMAND_SETS.items() if c.core_fuse_addr}
        assert fused == {
            CPUGeneration.ZEN3_VERMEER: 0x30081D98,
            CPUGeneration.ZEN3_CHAGALL: 0x30081D98,
            CPUGeneration.ZEN3D_WARHOL: 0x30081D98,
            CPUGeneration.ZEN4_STORM_PEAK: 0x30081D98,
            CPUGeneration.ZEN4_RAPHAEL: 0x30081CD0,
            CPUGeneration.ZEN4_DRAGON_RANGE: 0x30081CD0,
            CPUGeneration.ZEN5_GRANITE_RIDGE: 0x304A03DC,
        }


class TestKnownCoreDomain:
    def test_get_all_iterates_the_real_id_set(self):
        cores = {c: 0 for c in (0, 1, 4, 5, 6, 7)}
        cores.update({c: 1 for c in (8, 9, 12, 13, 14, 15)})
        smu, silicon = _mapped_smu(ZEN5, cores, {})
        offsets = smu.get_all_co_offsets(12)
        assert set(offsets) == set(cores)
        assert all(v == 0 for v in offsets.values())
        assert (1, 5) in silicon.get_calls
        assert (0, 2) not in silicon.get_calls

    def test_get_all_without_topology_keeps_range_fallback(self):
        smu = RyzenSMU(ZEN5, MagicMock())
        _FakeSilicon(smu, ZEN5, {0: set(range(8))})
        offsets = smu.get_all_co_offsets(3)
        assert set(offsets) == {0, 1, 2}

    def test_set_all_readback_uses_the_first_existing_core(self):
        cores = {c: 0 for c in (1, 2, 3, 4, 5, 6, 7)}
        smu, _silicon = _mapped_smu(ZEN5, cores, {0: set(range(1, 8))})
        assert smu.set_all_co(-8) is True

    def test_backup_restore_round_trips_the_real_ids(self):
        cores = {c: 0 for c in (0, 1, 4, 5, 6, 7)}
        smu, silicon = _mapped_smu(VERMEER, cores, {})
        for cid in cores:
            assert smu.set_co_offset(cid, -7) is True
        backup = smu.backup_co_offsets(6)
        assert set(backup) == set(cores)
        assert smu.set_co_offset(4, -2) is True
        ok, failed = smu.restore_co_offsets()
        assert ok is True and failed == []
        assert silicon.store[(0, 6)] == -7


class TestTunerRefusesUnmappedSMU:
    def _engine(self, smu):
        from corecycler.history.db import HistoryDB
        from corecycler.tuner.config import TunerConfig
        from corecycler.tuner.engine import TunerEngine

        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=0, logical_cpus=(c,)) for c in range(4)}
        topo.model_name = "Test"
        db = HistoryDB(":memory:")
        engine = TunerEngine(
            db=db,
            topology=topo,
            smu=smu,
            backend=MagicMock(name="mprime"),
            config=TunerConfig(cores_to_test=[0, 1], inherit_current=False),
        )
        return engine, db

    def test_start_refuses_with_the_map_error(self):
        smu, _silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))})
        engine, db = self._engine(smu)
        messages: list[str] = []
        engine.log_message.connect(messages.append)
        engine.start()
        db.close()
        assert engine._session_id is None
        assert any("Cannot start" in m and "disagrees with the OS" in m for m in messages)

    def test_resume_refuses_with_the_map_error(self):
        smu, _silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))})
        engine, db = self._engine(smu)
        from corecycler.tuner import persistence as tp
        from corecycler.tuner.config import TunerConfig

        session_id = tp.create_session(db, TunerConfig(), "bios-1.0", "Test", None)
        messages: list[str] = []
        engine.log_message.connect(messages.append)
        engine.resume(session_id)
        db.close()
        assert engine._status != "running"
        assert any("Cannot resume" in m for m in messages)


class TestNoL3Grouping:
    def test_window_grouping_reads_the_window_ccd_fuse(self):
        smu = RyzenSMU(VERMEER, MagicMock())
        silicon = _FakeSilicon(smu, VERMEER, {0: REPORTED_5600X_LIVE_SLOTS})
        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=None) for c in range(6)}
        smu.set_topology(topo)
        assert smu.core_map_error is None
        assert smu.core_map == {0: (0, 0), 1: (0, 1), 2: (0, 4), 3: (0, 5), 4: (0, 6), 5: (0, 7)}
        assert silicon.fuse_reads == [_fuse_addr(VERMEER, 0)]

    def test_empty_topology_keeps_legacy_behaviour(self):
        smu = RyzenSMU(VERMEER, MagicMock())
        _FakeSilicon(smu, VERMEER, {0: set(range(8))})
        topo = MagicMock()
        topo.cores = {}
        smu.set_topology(topo)
        assert smu.core_map is None
        assert smu.core_map_error is None
        assert set(smu.get_all_co_offsets(2)) == {0, 1}


class TestCliRefusal:
    def test_build_smu_prints_reason_and_returns_none(self, monkeypatch, capsys):
        from corecycler import cli
        from corecycler.engine.topology import CPUTopology, PhysicalCore

        monkeypatch.setattr(drv.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True))
        monkeypatch.setattr(
            drv.RyzenSMU,
            "check_smn_readable",
            lambda self: (False, _SMN_DENIED),
        )
        topo = CPUTopology(model_name="AMD Ryzen 5 5600X 6-Core Processor", family=25, model=0x21)
        for cid in range(6):
            topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
        assert cli._build_smu(topo) is None
        assert "per-core CO disabled" in capsys.readouterr().err


class TestTunerEndToEndOnRenumbered:
    def test_engine_traffic_lands_on_mapped_slots(self, monkeypatch, tmp_path):
        from corecycler.history.db import HistoryDB
        from corecycler.tuner import engine as eng_mod
        from corecycler.tuner.config import TunerConfig
        from corecycler.tuner.engine import TunerEngine

        smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=0, logical_cpus=(c,)) for c in range(6)}
        topo.model_name = "AMD Ryzen 5 5600X 6-Core Processor"
        db = HistoryDB(":memory:")
        backend = MagicMock()
        backend.name = "mprime"
        engine = TunerEngine(
            db=db,
            topology=topo,
            smu=smu,
            backend=backend,
            config=TunerConfig(cores_to_test=[2, 3], inherit_current=True),
            work_dir=tmp_path,
        )
        monkeypatch.setattr(engine, "_start_worker", MagicMock())
        monkeypatch.setattr(eng_mod.QTimer, "singleShot", lambda _ms, fn: None)
        engine.start()
        assert engine._session_id is not None
        assert (0, 4) in silicon.get_calls
        assert (0, 5) in silicon.get_calls
        assert engine._apply_co(2, -5) is True
        assert silicon.writes[-1] == (0, 4, -5)
        assert engine._apply_co(3, -5) is True
        assert silicon.writes[-1] == (0, 5, -5)
        engine.abort()
        db.close()
        assert all(slot not in (2, 3) for _ccd, slot, _v in silicon.writes)


class TestOfflineCpusDisableGapProof:
    def test_offline_with_vanished_core_reads_the_fuse_and_refuses(self):
        smu = RyzenSMU(ZEN5, MagicMock())
        silicon = _FakeSilicon(smu, ZEN5, {0: set(range(8))})
        topo = _topo({c: 0 for c in (0, 1, 2, 4, 5, 6, 7)})
        topo.cpus_all_online = False
        smu.set_topology(topo)
        assert smu.core_map_error is not None
        assert "offline" in smu.core_map_error
        assert silicon.fuse_reads == [_fuse_addr(ZEN5, 0)]

    def test_offline_but_fuse_matches_still_maps(self):
        smu = RyzenSMU(VERMEER, MagicMock())
        _FakeSilicon(smu, VERMEER, {0: REPORTED_5600X_LIVE_SLOTS})
        topo = _topo({c: 0 for c in (0, 1, 4, 5, 6, 7)})
        topo.cpus_all_online = False
        smu.set_topology(topo)
        assert smu.core_map_error is None
        assert smu.core_map == {0: (0, 0), 1: (0, 1), 4: (0, 4), 5: (0, 5), 6: (0, 6), 7: (0, 7)}

    def test_fully_online_gap_machine_still_skips_the_fuse(self):
        smu = RyzenSMU(ZEN5, MagicMock())
        silicon = _FakeSilicon(smu, ZEN5, {})
        topo = _topo({c: 0 for c in (0, 1, 2, 4, 5, 6, 7)})
        topo.cpus_all_online = True
        smu.set_topology(topo)
        assert smu.core_map_error is None
        assert silicon.fuse_reads == []
        assert smu.core_map == {c: (0, c) for c in (0, 1, 2, 4, 5, 6, 7)}


class TestPerCcdCompactionIsResolvedByFuse:
    def test_stride_preserving_compaction_cannot_bypass_the_fuse(self):
        cores = {c: 0 for c in range(6)}
        cores.update({c: 1 for c in range(8, 14)})
        live = {0: {0, 1, 4, 5, 6, 7}, 1: {0, 1, 4, 5, 6, 7}}
        smu, silicon = _mapped_smu(ZEN5, cores, live)
        assert smu.core_map_error is None
        assert silicon.fuse_reads == [_fuse_addr(ZEN5, 0), _fuse_addr(ZEN5, 1)]
        assert smu.core_map[4] == (0, 6)
        assert smu.core_map[5] == (0, 7)
        assert smu.core_map[12] == (1, 6)
        assert smu.core_map[13] == (1, 7)

    def test_internal_hole_still_maps_without_the_fuse(self):
        cores = {c: 0 for c in (0, 1, 4, 5, 6, 7)}
        cores.update({c: 1 for c in (8, 9, 12, 13, 14, 15)})
        smu, silicon = _mapped_smu(ZEN5, cores, {})
        assert smu.core_map_error is None
        assert silicon.fuse_reads == []
        assert smu.core_map[15] == (1, 7)


class TestValidateProfileRefusesUnmappedSMU:
    def test_validate_refuses_with_the_map_error(self):
        from corecycler.history.db import HistoryDB
        from corecycler.tuner.config import TunerConfig
        from corecycler.tuner.engine import TunerEngine

        smu, _silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))})
        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=0, logical_cpus=(c,)) for c in range(4)}
        topo.model_name = "Test"
        db = HistoryDB(":memory:")
        engine = TunerEngine(
            db=db,
            topology=topo,
            smu=smu,
            backend=MagicMock(name="mprime"),
            config=TunerConfig(cores_to_test=[0, 1], inherit_current=False),
        )
        messages: list[str] = []
        engine.log_message.connect(messages.append)
        engine.validate_profile(1)
        db.close()
        assert engine._status != "running"
        assert any("Cannot validate" in m for m in messages)
