"""Regression for HX 370 firmware acknowledging an invalid CO readback."""

from unittest.mock import MagicMock

import pytest

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.smu.commands import CPUGeneration, get_commands
from corecycler.smu.driver import RyzenSMU, SMUResponse


@pytest.mark.parametrize("value,success", [(600, True), (0, False)])
def test_invalid_strix_baseline_blocks_tuning_without_a_write(value, success):
    commands = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
    smu = RyzenSMU(commands)
    smu._send_get_co = MagicMock(return_value=SMUResponse(success, (value, 0, 0, 0, 0, 0), b""))
    smu._send_command = MagicMock()
    topology = CPUTopology(cores={0: PhysicalCore(0, 0, None, (0, 12))})

    smu.set_topology(topology)

    assert "Strix Point CO readback" in smu.core_map_error
    assert smu.get_co_offset(0) is None
    assert not smu.set_co_offset(0, -1)
    assert not smu.set_all_co(-1)
    smu._send_command.assert_not_called()
    smu._send_get_co.assert_called_once_with(0)


def test_plausible_strix_readback_preserves_existing_behavior():
    commands = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
    smu = RyzenSMU(commands)
    smu._send_get_co = MagicMock(return_value=SMUResponse(True, (0, 0, 0, 0, 0, 0), b""))

    smu.set_topology(CPUTopology(cores={0: PhysicalCore(0, 0, None, (0, 12))}))

    assert smu.core_map_error is None
