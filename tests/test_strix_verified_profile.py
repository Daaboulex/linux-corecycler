"""Protocol regressions from FA608WV.309 firmware and live probe evidence."""
from unittest.mock import MagicMock

import pytest

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.smu.commands import CPUGeneration, get_commands
from corecycler.smu.driver import RyzenSMU, SMUResponse


def response(value=0, success=True):
    return SMUResponse(success, (value & 0xFFFFFFFF, 0, 0, 0, 0, 0), b"")


def topology():
    ids = list(range(4)) + list(range(8, 16))
    return CPUTopology(
        model_name="AMD Ryzen AI 9 HX 370 w/ Radeon 890M", family=26, model=36,
        stepping=0, cores={cid: PhysicalCore(cid, int(cid >= 8), None, (i, i + 12))
                           for i, cid in enumerate(ids)},
    )


@pytest.fixture
def hardware(monkeypatch):
    monkeypatch.setattr("corecycler.smu.driver.Path.read_text", lambda self: "FA608WV.309\n")
    smu = RyzenSMU(get_commands(CPUGeneration.ZEN5_STRIX_POINT))
    margins = {(0, s): 0 for s in (0, 2, 4, 6)} | {(1, s): 0 for s in range(8)}

    def read(cmd, args=(0,) * 6):
        if cmd == 2:
            return response(0x0B5D0B00)
        if cmd == 0x82:
            return response(0xE6)
        assert cmd == 0x5E, f"Wrong getter: {cmd:#x}"
        key = (args[0] >> 28, args[0] >> 20 & 0xFF)
        return response(margins[key]) if key in margins else response(args[0], False)

    def write(cmd, args):
        assert cmd == 0x4B
        key = (args[0] >> 28, args[0] >> 20 & 0xFF)
        raw = args[0] & 0xFFFF
        margins[key] = raw if raw < 0x8000 else raw - 0x10000
        return response()

    smu._send_rsmu_command = MagicMock(side_effect=read)
    smu._send_command = MagicMock(side_effect=write)
    smu.check_writable = MagicMock(return_value=(True, "OK"))
    return smu, margins


def test_verified_profile_maps_every_core_and_restores(hardware):
    smu, margins = hardware
    original = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
    smu.set_topology(topology())
    assert smu.core_map_error is None
    assert smu.commands.get_co_cmd == 0x5E
    assert original.get_co_cmd == 0xAF  # shared and Halo command sets stay unchanged
    for cid in topology().cores:
        smu._send_command.assert_not_called()
        assert smu.backup_co_offsets(12) == {cid: 0 for cid in topology().cores}
        assert smu.set_co_offset(cid, -1)
        assert sum(v != 0 for v in margins.values()) == 1
        group, slot = smu.core_map[cid]
        assert (group, slot) == ((0, cid * 2) if cid < 4 else (1, cid - 8))
        assert margins[group, slot] == -1
        assert smu.restore_co_offsets() == (True, [])
        assert not any(margins.values())
        smu._send_command.reset_mock()


@pytest.mark.parametrize("failure", ["old_driver", "version", "capability", "presence", "margin", "offline", "layout"])
def test_profile_mismatches_block_all_writes(hardware, failure):
    smu, margins = hardware
    topo = topology()
    original_read = smu._send_rsmu_command.side_effect

    def read(cmd, args=(0,) * 6):
        if failure == "old_driver" and cmd == 0x5E and args[0] == 1 << 20:
            return response(args[0])
        if failure == "version" and cmd == 2:
            return response(0x0B5D0400)
        if failure == "capability" and cmd == 0x82:
            return response(0)
        if failure == "presence" and cmd == 0x5E and args[0] == 6 << 20:
            return response(args[0], False)
        return original_read(cmd, args)

    if failure == "margin":
        margins[1, 7] = 0x10000  # low-16 decoding alone would incorrectly accept zero
    if failure == "offline":
        topo.cpus_all_online = False
    if failure == "layout":
        topo.cores.pop(15)
    smu._send_rsmu_command.side_effect = read
    smu.set_topology(topo)
    assert smu.core_map_error
    assert not smu.set_co_offset(0, -1)
    assert not smu.set_all_co(-1)
    smu._send_command.assert_not_called()


def test_reinitialization_drops_verified_profile_on_version_change(hardware):
    smu, _ = hardware
    smu.set_topology(topology())
    assert smu.commands.get_co_cmd == 0x5E
    smu._send_rsmu_command.side_effect = lambda cmd, args=(0,): response(0x0B5D0400)
    smu.set_topology(topology())
    assert smu.commands.get_co_cmd == 0xAF
    assert smu.core_map is None
    assert not smu.set_co_offset(0, -1)


@pytest.mark.parametrize("difference", ["bios", "cpu_model", "stepping", "name"])
def test_unverified_images_keep_old_readback_guard(hardware, monkeypatch, difference):
    smu, _ = hardware
    topo = topology()
    if difference == "bios":
        monkeypatch.setattr("corecycler.smu.driver.Path.read_text", lambda self: "FA608WV.310\n")
    elif difference == "cpu_model":
        topo.model = 0x64
    elif difference == "stepping":
        topo.stepping = 1
    else:
        topo.model_name = "AMD Ryzen AI 9 365"
    smu._send_get_co = MagicMock(return_value=response(600))
    smu.set_topology(topo)
    smu._send_rsmu_command.assert_not_called()
    assert smu.commands.get_co_cmd == 0xAF
    assert smu.core_map_error
    assert not smu.set_co_offset(0, -1)
    smu._send_command.assert_not_called()


def test_unreadable_bios_keeps_old_readback_guard(hardware, monkeypatch):
    smu, _ = hardware

    def unreadable(path):
        raise PermissionError("DMI unavailable")

    monkeypatch.setattr("corecycler.smu.driver.Path.read_text", unreadable)
    smu._send_get_co = MagicMock(return_value=response(600))
    smu.set_topology(topology())
    smu._send_rsmu_command.assert_not_called()
    assert smu.core_map_error
    assert not smu.set_all_co(-1)
    smu._send_command.assert_not_called()
