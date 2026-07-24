"""Ring B live contract tests -- run the REAL hardware or tools and assert our
readers/parsers still match reality. Marked slow + contract; the nix sandbox
deselects `slow` so it never runs them here. They run on real hardware via the
ryzen hardware-contract CI step (pytest -m contract, CORECYCLER_HW_CONTRACTS=1),
where an absent resource fails loud instead of skipping green.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.smu.commands import CPUGeneration, detect_generation, get_commands

sys.path.insert(0, str(Path(__file__).parent))

from _contract_hw import require

pytestmark = [pytest.mark.slow, pytest.mark.contract]


def _read_cpuinfo() -> tuple[int, int, str] | None:
    p = Path("/proc/cpuinfo")
    if not p.exists():
        return None
    family: int | None = None
    model: int | None = None
    name = ""
    for line in p.read_text().splitlines():
        if line.startswith("cpu family") and family is None:
            family = int(line.split(":")[1])
        elif line.startswith("model name"):
            if not name:
                name = line.split(":", 1)[1].strip()
        elif line.startswith("model") and model is None:
            model = int(line.split(":")[1])
    if family is None or model is None:
        return None
    return family, model, name


def _is_amd_zen() -> bool:
    info = _read_cpuinfo()
    return info is not None and info[0] in (23, 25, 26)


def test_real_cpu_resolves_to_supported_generation():
    require(_is_amd_zen(), "requires a real AMD Zen CPU")
    info = _read_cpuinfo()
    assert info is not None
    family, model, name = info
    gen = detect_generation(family, model, name)
    assert gen is not CPUGeneration.UNKNOWN, f"unmapped CPU family={family} model={model} name={name!r}"
    assert get_commands(gen) is not None
