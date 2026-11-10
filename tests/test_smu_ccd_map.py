"""Vermeer can expose physical CCD 1 as Linux's only L3 group, numbered 0."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from corecycler.smu.commands import CPUGeneration, get_commands
from corecycler.smu.driver import RyzenSMU

VERMEER = get_commands(CPUGeneration.ZEN3_VERMEER)


def _discover(cores: dict[int, int], words: dict[int, int]) -> RyzenSMU:
    smu = RyzenSMU(VERMEER, MagicMock())
    smu.check_smn_readable = lambda: (True, "OK")
    smu.read_smn = words.get
    smu.set_topology(SimpleNamespace(cores={cid: SimpleNamespace(ccd=ccd) for cid, ccd in cores.items()}))
    return smu


def test_reported_5600x_uses_the_surviving_second_ccd() -> None:
    smu = _discover(
        dict.fromkeys(range(6), 0),
        {0x5D218: 0x40C00000, 0x5D21C: 0x3F, 0x30081D98: 0x3FF, 0x32081D98: 0x30C},
    )
    assert smu.core_map_error is None
    assert smu.core_map == {0: (1, 0), 1: (1, 1), 2: (1, 4), 3: (1, 5), 4: (1, 6), 5: (1, 7)}


def test_full_single_ccd_still_needs_its_physical_ccd_number() -> None:
    smu = _discover(
        dict.fromkeys(range(8), 0),
        {0x5D218: 0x40C00000, 0x5D21C: 0x3F, 0x30081D98: 0x3FF, 0x32081D98: 0x300},
    )
    assert smu.core_map_error is None
    assert smu.core_map == {cid: (1, cid) for cid in range(8)}


def test_5950x_preserves_both_full_ccds() -> None:
    smu = _discover(
        {cid: cid // 8 for cid in range(16)},
        {0x5D218: 0x00C00000, 0x5D21C: 0x3F, 0x30081D98: 0x300, 0x32081D98: 0x300},
    )
    assert smu.core_map_error is None
    assert smu.core_map == {cid: (cid // 8, cid % 8) for cid in range(16)}


def test_ccd_count_disagreement_refuses_to_guess() -> None:
    smu = _discover(dict.fromkeys(range(6), 0), {0x5D218: 0x00C00000, 0x5D21C: 0x3F})
    assert smu.core_map is None
    assert "active physical CCDs [0, 1]" in smu.core_map_error


def test_unreadable_ccd_fuses_refuse_to_guess() -> None:
    smu = _discover(dict.fromkeys(range(8), 0), {})
    assert smu.core_map is None
    assert "CCD fuse read failed" in smu.core_map_error
