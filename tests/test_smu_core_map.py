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
    CPUGeneration,
    get_commands,
)
from corecycler.smu.driver import RyzenSMU, SMUResponse, core_map_blocked, physical_ccds

VERMEER = get_commands(CPUGeneration.ZEN3_VERMEER)
ZEN5 = get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)
CEZANNE = get_commands(CPUGeneration.ZEN3_CEZANNE)
PHOENIX = get_commands(CPUGeneration.ZEN4_PHOENIX)

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

    def __init__(
        self,
        smu: RyzenSMU,
        commands,
        live: dict[int, set[int]],
        *,
        smn: bool = True,
        ccd_words: tuple[int | None, int | None] | None = None,
    ) -> None:
        self.commands = commands
        self.live = live
        self.store: dict[tuple[int, int], int] = {}
        self.writes: list[tuple[int, int, int]] = []
        self.get_calls: list[tuple[int, int]] = []
        self.fuse_reads: list[int] = []
        self.ccd_reads: list[int] = []
        present = sum(1 << ccd for ccd in live) or 1
        self.ccd_words = (present << 22, 0) if ccd_words is None else ccd_words
        smu._send_command = self._send
        smu._send_rsmu_command = self._send
        smu.check_writable = lambda: (True, "OK")
        smu.check_smn_readable = lambda: (True, "OK") if smn else (False, _SMN_DENIED)
        smu.read_smn = self._read_smn

    def _read_smn(self, address: int) -> int | None:
        ccd_fuses = self.commands.ccd_fuse_addr
        if ccd_fuses is not None and address in (ccd_fuses, ccd_fuses + 4):
            self.ccd_reads.append(address)
            return self.ccd_words[address - ccd_fuses >> 2]
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
            if self.commands.get_co_by_slot:
                ccd, slot = 0, arg
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


def _mapped_smu(
    commands,
    cores: dict[int, int],
    live: dict[int, set[int]],
    *,
    smn: bool = True,
    ccd_words: tuple[int | None, int | None] | None = None,
    **kw,
):
    smu = RyzenSMU(commands, MagicMock(), **kw)
    silicon = _FakeSilicon(smu, commands, live, smn=smn, ccd_words=ccd_words)
    smu.set_topology(_topo(cores))
    return smu, silicon


def _fuse_addr(commands, ccd: int) -> int:
    return commands.core_fuse_addr + (ccd << 25)


REPORTED_5600X_LIVE_SLOTS = {0, 1, 4, 5, 6, 7}
REPORTED_5600GE_FUSE_WORD = 0x281C70
PR23_5600X_CCD_WORDS = (0x40C00000, 0x3F)
PR23_5950X_CCD_WORDS = (0x00C00000, 0x3F)


def pr23_5600x(_commands=None) -> RyzenSMU:
    """The PR #23 5600X's driver: CCD 0 fused off, its six cores on topology CCD 0 and physical CCD 1."""
    smu, _silicon = _mapped_smu(
        VERMEER, dict.fromkeys(range(6), 0), {1: REPORTED_5600X_LIVE_SLOTS}, ccd_words=PR23_5600X_CCD_WORDS
    )
    return smu


class TestPhysicalCcd:
    """PR #23 (iam4x): the topology numbers CCDs by the rank of their L3 id, so on a Vermeer whose physical CCD 0
    is fused off the surviving CCD 1 is group 0; the CCD-disable fuses name the physical CCD. Words read on the
    PR's 5600X."""

    def test_the_reported_5600x_maps_its_group_onto_physical_ccd_1(self):
        smu, silicon = _mapped_smu(
            VERMEER, dict.fromkeys(range(6), 0), {1: REPORTED_5600X_LIVE_SLOTS}, ccd_words=PR23_5600X_CCD_WORDS
        )
        assert smu.core_map_error is None
        assert smu.core_map == {0: (1, 0), 1: (1, 1), 2: (1, 4), 3: (1, 5), 4: (1, 6), 5: (1, 7)}
        assert silicon.fuse_reads == [_fuse_addr(VERMEER, 1)]

    def test_a_write_lands_on_the_surviving_physical_ccd(self):
        smu, silicon = _mapped_smu(
            VERMEER, dict.fromkeys(range(6), 0), {1: REPORTED_5600X_LIVE_SLOTS}, ccd_words=PR23_5600X_CCD_WORDS
        )
        assert smu.set_co_offset(2, -1) is True
        assert silicon.writes == [(1, 4, -1)]
        assert smu.get_co_offset(2) == -1

    def test_a_full_surviving_ccd_is_written_on_its_physical_number(self):
        smu, silicon = _mapped_smu(
            VERMEER, dict.fromkeys(range(8), 0), {1: set(range(8))}, ccd_words=PR23_5600X_CCD_WORDS
        )
        assert smu.core_map == {core: (1, core) for core in range(8)}
        assert smu.set_co_offset(3, -2) is True
        assert silicon.writes == [(1, 3, -2)]

    def test_ccds_2_to_7_count_only_when_the_second_word_leaves_them_enabled(self):
        every_ccd_enabled_ccd_0_off = (0xFF << 22) | (1 << 30)
        smu, _silicon = _mapped_smu(
            VERMEER,
            dict.fromkeys(range(6), 0),
            {1: REPORTED_5600X_LIVE_SLOTS},
            ccd_words=(every_ccd_enabled_ccd_0_off, 0x3F),
        )
        assert smu.core_map == {0: (1, 0), 1: (1, 1), 2: (1, 4), 3: (1, 5), 4: (1, 6), 5: (1, 7)}
        smu, _silicon = _mapped_smu(
            VERMEER,
            dict.fromkeys(range(6), 0),
            {1: REPORTED_5600X_LIVE_SLOTS},
            ccd_words=(every_ccd_enabled_ccd_0_off, 0x3E),
        )
        assert smu.core_map is None
        assert "active CCDs [1, 2]" in smu.core_map_error

    def test_ccd_1_fused_off_by_bit_31_leaves_the_group_on_ccd_0(self):
        """The PR's words mirrored, not measured: CCD 0 alive, CCD 1 disabled, as a single-CCD 5600X or 5800X."""
        smu, silicon = _mapped_smu(
            VERMEER, dict.fromkeys(range(6), 0), {0: REPORTED_5600X_LIVE_SLOTS}, ccd_words=(0x80C00000, 0x3F)
        )
        assert smu.core_map == {0: (0, 0), 1: (0, 1), 2: (0, 4), 3: (0, 5), 4: (0, 6), 5: (0, 7)}
        assert silicon.fuse_reads == [_fuse_addr(VERMEER, 0)]

    def test_a_5950x_keeps_both_ccds_in_place(self):
        smu, silicon = _mapped_smu(
            VERMEER,
            {core: core // 8 for core in range(16)},
            {0: set(range(8)), 1: set(range(8))},
            ccd_words=PR23_5950X_CCD_WORDS,
        )
        assert smu.core_map == {core: (core // 8, core % 8) for core in range(16)}
        assert silicon.fuse_reads == []

    def test_fuses_naming_more_ccds_than_the_os_shows_refuse(self):
        smu, silicon = _mapped_smu(
            VERMEER, dict.fromkeys(range(6), 0), {0: REPORTED_5600X_LIVE_SLOTS}, ccd_words=PR23_5950X_CCD_WORDS
        )
        assert smu.core_map is None
        assert "active CCDs [0, 1]" in smu.core_map_error
        assert smu.set_co_offset(0, -1) is False
        assert silicon.writes == []

    @pytest.mark.parametrize("words", [(None, 0x3F), (0x40C00000, None)], ids=["present", "down"])
    def test_an_unreadable_ccd_fuse_refuses(self, words):
        smu, _silicon = _mapped_smu(VERMEER, dict.fromkeys(range(8), 0), {0: set(range(8))}, ccd_words=words)
        assert smu.core_map is None
        assert "CCD-disable fuse read failed" in smu.core_map_error

    def test_a_vermeer_without_smn_access_refuses_even_with_full_ccds(self):
        smu, silicon = _mapped_smu(VERMEER, dict.fromkeys(range(8), 0), {0: set(range(8))}, smn=False)
        assert smu.core_map is None
        assert _SMN_DENIED in smu.core_map_error
        assert silicon.ccd_reads == []

    def test_granite_ridge_reads_no_fuse_at_all(self):
        smu, silicon = _mapped_smu(
            ZEN5, {core: core // 8 for core in range(16)}, {0: set(range(8)), 1: set(range(8))}, smn=False
        )
        assert smu.core_map == {core: (core // 8, core % 8) for core in range(16)}
        assert silicon.fuse_reads == []

    def test_the_5600x_s_cores_read_the_temperature_of_physical_ccd_1(self):
        """hwmon labels physical CCD i as Tccd(i+1) (k10temp and zenpower5 alike), so these cores read Tccd2."""
        assert physical_ccds(pr23_5600x(), _topo(dict.fromkeys(range(6), 0))) == dict.fromkeys(range(6), 1)

    def test_a_refused_vermeer_map_keeps_the_topology_s_ccd_so_a_5950x_still_reads_both(self):
        cores = {core: core // 8 for core in range(16)}
        smu, _silicon = _mapped_smu(VERMEER, cores, {0: set(range(8)), 1: set(range(8))}, smn=False)
        assert smu.core_map is None
        assert physical_ccds(smu, _topo(cores)) == cores

    def test_granite_ridge_keeps_the_topology_s_ccd_even_while_per_core_co_is_refused(self):
        cores = dict.fromkeys(range(6), 1)
        smu, _silicon = _mapped_smu(ZEN5, cores, {1: REPORTED_5600X_LIVE_SLOTS}, smn=False)
        assert smu.core_map is None
        assert physical_ccds(smu, _topo(cores)) == cores

    def test_without_a_driver_the_topology_s_ccd_stands(self):
        assert physical_ccds(None, _topo({0: 0, 8: 1, 9: None})) == {0: 0, 8: 1, 9: None}


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
        assert f"CO read refused for core 0: {smu.core_map_error}" in caplog.text
        assert f"CO write refused for core 0: {smu.core_map_error}" in caplog.text
        assert silicon.writes == []

    def test_a_core_outside_a_good_map_is_refused_naming_why(self, caplog):
        smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        assert smu.core_map_error is None
        with caplog.at_level(logging.ERROR):
            assert smu.get_co_offset(9) is None
            assert smu.set_co_offset(9, -5) is False
        assert "CO read refused for core 9: core is not in the discovered core map" in caplog.text
        assert "CO write refused for core 9: core is not in the discovered core map" in caplog.text
        assert silicon.writes == []

    def test_unreadable_smn_names_the_permission_fix(self):
        smu, silicon = _mapped_smu(
            ZEN5,
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
        smu, silicon = _mapped_smu(PHOENIX, {c: 0 for c in range(6)}, {0: REPORTED_5600X_LIVE_SLOTS})
        assert PHOENIX.core_fuse_addr is None
        assert smu.core_map is None
        assert "ZEN4_PHOENIX" in smu.core_map_error
        assert silicon.fuse_reads == []

    def test_cezanne_5600ge_reported_fuse_word_puts_cores_on_slots_2_to_7(self):
        smu = RyzenSMU(CEZANNE, MagicMock())
        silicon = _FakeSilicon(smu, CEZANNE, {0: set(range(2, 8))})
        reads: list[int] = []

        def read_reported_word(address: int) -> int:
            reads.append(address)
            return REPORTED_5600GE_FUSE_WORD

        smu.read_smn = read_reported_word
        smu.set_topology(_topo({c: 0 for c in range(6)}))
        assert smu.core_map_error is None
        assert smu.core_map == {0: (0, 2), 1: (0, 3), 2: (0, 4), 3: (0, 5), 4: (0, 6), 5: (0, 7)}
        assert reads == [_fuse_addr(CEZANNE, 0)]
        assert smu.set_co_offset(0, -5) is True
        assert silicon.writes == [(0, 2, -5)]
        assert smu.get_co_offset(0) == -5
        assert silicon.get_calls[-1] == (0, 2)

    def test_cezanne_bare_slot_read_refuses_a_ccd_it_cannot_address(self):
        smu = RyzenSMU(CEZANNE, MagicMock())
        silicon = _FakeSilicon(smu, CEZANNE, {0: set(range(8))})
        assert smu.get_co_offset(8) is None
        assert silicon.get_calls == []
        assert smu.get_co_offset(7) == 0
        assert silicon.get_calls == [(0, 7)]

    def test_cezanne_die_wide_fuse_refuses_a_second_ccd(self):
        smu = RyzenSMU(CEZANNE, MagicMock())
        _FakeSilicon(smu, CEZANNE, {0: set(range(2, 8))})
        reads: list[int] = []

        def read_reported_word(address: int) -> int:
            reads.append(address)
            return REPORTED_5600GE_FUSE_WORD

        smu.read_smn = read_reported_word
        smu.set_topology(_topo({c: c // 6 for c in range(12)}))
        assert smu.core_map is None
        assert "CCD 1" in smu.core_map_error
        assert reads == [_fuse_addr(CEZANNE, 0)]

    def test_an_unaddressable_write_is_refused(self):
        smu, silicon = _mapped_smu(VERMEER, {c: 0 for c in range(6)}, {0: set(range(8))})
        silicon.writes.clear()
        assert smu.set_co_offset(0, -5) is False
        assert smu.reset_all_co() is False
        assert silicon.writes == []

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
        smu.set_topology(_topo({**dict.fromkeys(range(4), 0), **dict.fromkeys(range(8, 16), 1)}))
        assert silicon.fuse_reads == []
        assert smu.core_map is None
        assert smu.core_map_error is None
        assert smu.set_co_offset(9, -5) is True
        assert silicon.writes == [(1, 1, -5)]

    def test_a_strix_point_co_read_out_of_range_refuses_per_core_co_without_a_write(self):
        """PR #21 (chevydan6): an HX 370 on BIOS FA608WV.309 acknowledges the CO read and returns 600."""
        strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
        smu = RyzenSMU(strix, MagicMock())
        silicon = _FakeSilicon(smu, strix, {0: set(range(8)), 1: set(range(8))})
        silicon.store[(0, 0)] = 600
        smu.set_topology(_topo({**dict.fromkeys(range(4), 0), **dict.fromkeys(range(8, 16), 1)}))
        assert "returned 600, outside -50..10" in smu.core_map_error
        assert smu.get_co_offset(1) is None
        assert smu.set_co_offset(1, -1) is False
        assert smu.set_all_co(-1) is False
        assert silicon.writes == []
        assert silicon.get_calls == [(0, 0)]

    def test_a_failed_strix_point_co_read_refuses_per_core_co(self):
        strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
        smu = RyzenSMU(strix, MagicMock())
        silicon = _FakeSilicon(smu, strix, {0: set(range(8))})
        sent = []

        def refused(cmd, args=(0,) * 6):
            sent.append((cmd, args))
            return SMUResponse(success=False, args=(0,) * 6, raw=b"")

        smu._send_rsmu_command = refused
        smu.set_topology(_topo({c: 0 for c in range(4)}))
        assert sent == [(strix.get_co_cmd, (0,))]
        assert "CO read failed" in smu.core_map_error
        assert smu.set_co_offset(1, -1) is False
        assert silicon.writes == []

    def test_a_strix_point_without_mailbox_access_says_so(self):
        strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
        smu = RyzenSMU(strix, MagicMock())
        silicon = _FakeSilicon(smu, strix, {0: set(range(8))})
        smu.check_writable = lambda: (False, "no write permission on mp1_smu_cmd")
        smu.set_topology(_topo({c: 0 for c in range(4)}))
        assert "no write permission on mp1_smu_cmd" in smu.core_map_error
        assert silicon.get_calls == []

    def test_the_strix_point_probe_reads_the_lowest_known_core_where_get_co_offset_would(self):
        strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
        smu = RyzenSMU(strix, MagicMock())
        silicon = _FakeSilicon(smu, strix, {1: set(range(8))})
        silicon.store[(1, 0)] = 600
        smu.set_topology(_topo({8: 1, 9: 1, 10: 1}))
        assert silicon.get_calls == [(1, 0)]
        assert "returned 600" in smu.core_map_error

    @pytest.mark.parametrize("generation", [CPUGeneration.ZEN5_STRIX_HALO, CPUGeneration.ZEN5_GRANITE_RIDGE])
    def test_only_strix_point_reads_a_co_at_topology_time(self, generation):
        commands = get_commands(generation)
        smu = RyzenSMU(commands, MagicMock())
        silicon = _FakeSilicon(smu, commands, {0: set(range(8)), 1: set(range(8))})
        silicon.store[(0, 0)] = 600
        smu.set_topology(_topo({c: c // 8 for c in range(16)}))
        assert silicon.get_calls == []
        assert smu.core_map_error is None

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
        from corecycler.engine.topology import CPUTopology, PhysicalCore
        from corecycler.tuner import live_hw

        monkeypatch.setattr(drv.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True))
        monkeypatch.setattr(
            drv.RyzenSMU,
            "check_smn_readable",
            lambda self: (False, _SMN_DENIED),
        )
        topo = CPUTopology(model_name="AMD Ryzen 5 5600X 6-Core Processor", family=25, model=0x21)
        for cid in range(6):
            topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
        assert live_hw.build_smu(topo) is None
        assert "per-core CO disabled" in capsys.readouterr().err


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
