"""OS-core to SMU-slot map discovery (issue #11): renumbered harvested parts.

Some BIOS/AGESA builds renumber /proc/cpuinfo core ids contiguously on
harvested parts instead of leaving holes at the fused-off slots, so the
core_id-derived SMU address hits dead or wrong slots and CO read/write fails.
set_topology now discovers the mapping: numberings that prove themselves
(holes, full CCDs) are used directly, ambiguous CCDs are probed with the
read-only CO query, and an undiscoverable map refuses per-core CO everywhere
(driver, GUI, tuner start/resume) instead of tuning a different core than the
one under test.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from corecycler.smu.commands import (
    COMMAND_SETS,
    CPUGeneration,
    get_commands,
)
from corecycler.smu.driver import RyzenSMU, SMUResponse, core_map_blocked

VERMEER = get_commands(CPUGeneration.ZEN3_VERMEER)
ZEN5 = get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)


def _topo(cores: dict[int, int]) -> MagicMock:
    topo = MagicMock()
    topo.cores = {cid: MagicMock(ccd=ccd) for cid, ccd in cores.items()}
    return topo


class _FakeMailbox:
    """Mailbox simulator: only the physical slots in ``present`` answer.

    ``present`` maps ccd -> set of live slot indices. GET fails for absent
    slots (the discrimination the probe relies on), SET stores per (ccd, slot)
    so read-back verification runs against real addressing, SET-ALL fills
    every live slot.
    """

    def __init__(self, smu: RyzenSMU, commands, present: dict[int, set[int]]) -> None:
        self.commands = commands
        self.present = present
        self.store: dict[tuple[int, int], int] = {}
        self.writes: list[tuple[int, int, int]] = []
        self.get_calls: list[tuple[int, int]] = []
        smu._send_command = self._send
        smu._send_rsmu_command = self._send
        smu.check_writable = lambda: (True, "OK")

    def _send(self, cmd, args=(0,) * 6):
        arg = args[0] if args else 0
        ccd = (arg >> 28) & 0xF
        slot = (arg >> 20) & 0xF
        low = arg & 0xFFFF
        value = low - 0x10000 if low >= 0x8000 else low
        if cmd == self.commands.get_co_cmd:
            self.get_calls.append((ccd, slot))
            if slot not in self.present.get(ccd, set()):
                return SMUResponse(success=False, args=(0,) * 6, raw=b"")
            stored = self.store.get((ccd, slot), 0) & 0xFFFF
            return SMUResponse(success=True, args=(stored,) + (0,) * 5, raw=b"")
        if cmd == self.commands.set_co_cmd:
            if slot not in self.present.get(ccd, set()):
                return SMUResponse(success=False, args=(0,) * 6, raw=b"")
            self.store[(ccd, slot)] = value
            self.writes.append((ccd, slot, value))
            return SMUResponse(success=True, args=(arg,) + (0,) * 5, raw=b"")
        if cmd == self.commands.set_all_co_cmd:
            for ccd_key, slots in self.present.items():
                for live_slot in slots:
                    self.store[(ccd_key, live_slot)] = value
            return SMUResponse(success=True, args=(0,) * 6, raw=b"")
        return SMUResponse(success=True, args=(0,) * 6, raw=b"")


def _mapped_smu(commands, cores: dict[int, int], present: dict[int, set[int]], **kw):
    smu = RyzenSMU(commands, MagicMock(), **kw)
    mailbox = _FakeMailbox(smu, commands, present)
    smu.set_topology(_topo(cores))
    return smu, mailbox


REPORTED_5600X_LIVE_SLOTS = {0, 1, 4, 5, 6, 7}


class TestRenumberedHarvested:
    def test_5600x_renumbered_maps_onto_live_slots(self):
        smu, mailbox = _mapped_smu(
            VERMEER,
            {c: 0 for c in range(6)},
            {0: REPORTED_5600X_LIVE_SLOTS},
        )
        assert smu.core_map_error is None
        assert smu.core_map == {0: (0, 0), 1: (0, 1), 2: (0, 4), 3: (0, 5), 4: (0, 6), 5: (0, 7)}
        assert mailbox.get_calls == [(0, s) for s in range(8)]

    def test_write_lands_on_mapped_slot_and_reads_back(self):
        smu, mailbox = _mapped_smu(
            VERMEER,
            {c: 0 for c in range(6)},
            {0: REPORTED_5600X_LIVE_SLOTS},
        )
        assert smu.set_co_offset(2, -10) is True
        assert mailbox.store[(0, 4)] == -10
        assert smu.get_co_offset(2) == -10
        assert all(slot not in (2, 3) for _ccd, slot, _v in mailbox.writes)

    def test_renumbered_two_ccd_maps_each_ccd(self):
        cores = {c: (0 if c < 6 else 1) for c in range(12)}
        present = {0: {0, 1, 2, 3, 6, 7}, 1: {0, 1, 4, 5, 6, 7}}
        smu, _mailbox = _mapped_smu(ZEN5, cores, present)
        assert smu.core_map_error is None
        assert smu.core_map == {
            0: (0, 0), 1: (0, 1), 2: (0, 2), 3: (0, 3), 4: (0, 6), 5: (0, 7),
            6: (1, 0), 7: (1, 1), 8: (1, 4), 9: (1, 5), 10: (1, 6), 11: (1, 7),
        }

    def test_full_ccd_in_contiguous_space_is_not_probed(self):
        cores = {c: (0 if c < 8 else 1) for c in range(14)}
        present = {0: set(range(8)), 1: {0, 1, 2, 4, 5, 7}}
        smu, mailbox = _mapped_smu(ZEN5, cores, present)
        assert smu.core_map_error is None
        assert all(ccd == 1 for ccd, _slot in mailbox.get_calls)
        assert smu.core_map[7] == (0, 7)
        assert smu.core_map[13] == (1, 7)

    def test_mp1_generation_probes_through_its_own_mailbox(self):
        smu, _mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS}
        )
        assert smu._get_cmd_filename() == "mp1_smu_cmd"
        assert smu.core_map is not None


class TestProbeFailClosed:
    def test_indiscriminate_probe_refuses_per_core_co(self, caplog):
        smu, mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))}
        )
        assert smu.core_map is None
        assert smu.core_map_error is not None
        assert "could not isolate" in smu.core_map_error
        with caplog.at_level(logging.ERROR):
            assert smu.get_co_offset(0) is None
            assert smu.set_co_offset(0, -5) is False
            assert smu.set_all_co(-5) is False
            assert smu.reset_all_co() is False
        assert "refused" in caplog.text
        assert mailbox.writes == []

    def test_dead_mailbox_probe_names_the_access_problem(self):
        smu, _mailbox = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {})
        assert smu.core_map_error is not None
        assert "no CCD-0 slot answered" in smu.core_map_error

    def test_dry_run_refuses_an_unaddressable_write(self):
        smu, _mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))}, dry_run=True
        )
        assert smu.set_co_offset(0, -5) is False

    def test_unknown_core_id_refuses_instead_of_guessing(self):
        smu, mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS}
        )
        mailbox.writes.clear()
        assert smu.set_co_offset(17, -5) is False
        assert smu.get_co_offset(17) is None
        assert mailbox.writes == []

    def test_out_of_range_value_still_raises_before_addressing(self):
        smu, _mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))}
        )
        with pytest.raises(ValueError, match="out of range"):
            smu.set_co_offset(0, -99)


class TestCoreMapBlockedHelper:
    def test_none_smu_is_not_blocked(self):
        assert core_map_blocked(None) is None

    def test_magicmock_smu_is_not_blocked(self):
        assert core_map_blocked(MagicMock()) is None

    def test_error_string_blocks(self):
        assert core_map_blocked(MagicMock(core_map_error="renumbered")) == "renumbered"

    def test_healthy_driver_is_not_blocked(self):
        smu, _mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS}
        )
        assert core_map_blocked(smu) is None


class TestGenerationGating:
    def test_strix_point_keeps_legacy_addressing_untouched(self):
        strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
        assert strix is not None and not strix.uniform_8core_ccds
        smu = RyzenSMU(strix, MagicMock())
        mailbox = _FakeMailbox(smu, strix, {0: set(range(8)), 1: set(range(8))})
        smu.set_topology(_topo({c: (0 if c < 4 else 1) for c in range(12)}))
        assert mailbox.get_calls == []
        assert smu.core_map is None
        assert smu.core_map_error is None
        assert smu.set_co_offset(9, -5) is True
        assert mailbox.writes == [(1, 1, -5)]

    def test_zen2_has_no_co_and_no_map(self):
        matisse = get_commands(CPUGeneration.ZEN2_MATISSE)
        smu = RyzenSMU(matisse, MagicMock())
        mailbox = _FakeMailbox(smu, matisse, {})
        smu.set_topology(_topo({c: 0 for c in range(6)}))
        assert mailbox.get_calls == []
        assert smu.core_map is None
        assert smu.core_map_error is None
        assert smu.get_co_offset(0) is None

    def test_dense_l3_group_falls_back_to_legacy(self):
        smu = RyzenSMU(ZEN5, MagicMock())
        mailbox = _FakeMailbox(smu, ZEN5, {0: set(range(8))})
        smu.set_topology(_topo({c: 0 for c in range(12)}))
        assert mailbox.get_calls == []
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


class TestKnownCoreDomain:
    def test_get_all_iterates_the_real_id_set(self):
        cores = {c: 0 for c in (0, 1, 4, 5, 6, 7)}
        cores.update({c: 1 for c in (8, 9, 12, 13, 14, 15)})
        present = {0: {0, 1, 4, 5, 6, 7}, 1: {0, 1, 4, 5, 6, 7}}
        smu, mailbox = _mapped_smu(ZEN5, cores, present)
        offsets = smu.get_all_co_offsets(12)
        assert set(offsets) == set(cores)
        assert all(v == 0 for v in offsets.values())
        assert (1, 5) in mailbox.get_calls
        assert (0, 2) not in mailbox.get_calls

    def test_get_all_without_topology_keeps_range_fallback(self):
        smu = RyzenSMU(ZEN5, MagicMock())
        _FakeMailbox(smu, ZEN5, {0: set(range(8))})
        offsets = smu.get_all_co_offsets(3)
        assert set(offsets) == {0, 1, 2}

    def test_set_all_readback_uses_the_first_existing_core(self):
        cores = {c: 0 for c in (1, 2, 3, 4, 5, 6, 7)}
        smu, _mailbox = _mapped_smu(ZEN5, cores, {0: set(range(1, 8))})
        assert smu.set_all_co(-8) is True

    def test_backup_restore_round_trips_the_real_ids(self):
        cores = {c: 0 for c in (0, 1, 4, 5, 6, 7)}
        smu, mailbox = _mapped_smu(VERMEER, cores, {0: REPORTED_5600X_LIVE_SLOTS})
        for cid in cores:
            assert smu.set_co_offset(cid, -7) is True
        backup = smu.backup_co_offsets(6)
        assert set(backup) == set(cores)
        assert smu.set_co_offset(4, -2) is True
        ok, failed = smu.restore_co_offsets()
        assert ok is True and failed == []
        assert mailbox.store[(0, 6)] == -7


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
        smu, _mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))}
        )
        engine, db = self._engine(smu)
        messages: list[str] = []
        engine.log_message.connect(messages.append)
        engine.start()
        db.close()
        assert engine._session_id is None
        assert any("Cannot start" in m and "could not isolate" in m for m in messages)

    def test_resume_refuses_with_the_map_error(self):
        smu, _mailbox = _mapped_smu(
            VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))}
        )
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
    def test_window_grouping_probes_with_window_ccd(self):
        smu = RyzenSMU(VERMEER, MagicMock())
        mailbox = _FakeMailbox(smu, VERMEER, {0: REPORTED_5600X_LIVE_SLOTS})
        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=None) for c in range(6)}
        smu.set_topology(topo)
        assert smu.core_map_error is None
        assert smu.core_map == {0: (0, 0), 1: (0, 1), 2: (0, 4), 3: (0, 5), 4: (0, 6), 5: (0, 7)}
        assert all(ccd == 0 for ccd, _slot in mailbox.get_calls)

    def test_empty_topology_keeps_legacy_behaviour(self):
        smu = RyzenSMU(VERMEER, MagicMock())
        _FakeMailbox(smu, VERMEER, {0: set(range(8))})
        topo = MagicMock()
        topo.cores = {}
        smu.set_topology(topo)
        assert smu.core_map is None
        assert smu.core_map_error is None
        assert set(smu.get_all_co_offsets(2)) == {0, 1}


class TestProbePermissionHint:
    def test_unwritable_mailbox_hint_reaches_the_error(self):
        smu = RyzenSMU(VERMEER, MagicMock())
        mailbox = _FakeMailbox(smu, VERMEER, {})
        del mailbox
        smu.check_writable = lambda: (False, "No write permission on rsmu_cmd")
        topo = MagicMock()
        topo.cores = {c: MagicMock(ccd=0) for c in range(6)}
        smu.set_topology(topo)
        assert smu.core_map_error is not None
        assert "No write permission" in smu.core_map_error


class TestCliRefusal:
    def test_build_smu_prints_reason_and_returns_none(self, monkeypatch, capsys):
        from corecycler import cli
        from corecycler.engine.topology import CPUTopology, PhysicalCore
        from corecycler.smu import driver as drv

        monkeypatch.setattr(
            drv.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True)
        )
        monkeypatch.setattr(
            drv.RyzenSMU,
            "_send_command",
            lambda self, cmd, args=(0,) * 6: SMUResponse(
                success=False, args=(0,) * 6, raw=b""
            ),
        )
        topo = CPUTopology(model_name="AMD Ryzen 5 5600X 6-Core Processor", family=25, model=0x21)
        for cid in range(6):
            topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
        assert cli._build_smu(topo) is None
        assert "per-core CO disabled" in capsys.readouterr().err
