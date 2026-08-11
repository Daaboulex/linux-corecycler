"""Ring B: does tool resolution match what THIS distro actually installed?

Every tool the running system has on PATH must resolve through config.tools,
and at least one stress backend must be present. Marked slow + contract; the
nix sandbox deselects `slow`, so it runs on real machines, by hand:

    CORECYCLER_HW_CONTRACTS=1 pytest -m contract

The distro half of the same contract runs `corecycler doctor` inside each
distro's container (.github/workflows/distro-matrix.yml) -- the same
``tools.report()``, through the CLI, so no test framework has to be installed
in the image.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.config import tools

sys.path.insert(0, str(Path(__file__).parent))

from _contract_hw import require

pytestmark = [pytest.mark.slow, pytest.mark.contract]


def _on_path() -> dict[str, str]:
    found = {}
    for key, tool in tools.TOOLS.items():
        for name in tool.names:
            where = shutil.which(name)
            if where:
                found[key] = where
                break
    return found


def test_every_tool_present_on_path_resolves_through_the_registry():
    present = _on_path()
    require(bool(present), "no CoreCycler external tool is on PATH at all")
    mismatched = {
        key: (where, tools.resolve(key).path)
        for key, where in present.items()
        if tools.resolve(key).path != Path(where)
    }
    assert not mismatched, f"resolution disagrees with PATH: {mismatched}"


def test_a_stress_backend_is_installed():
    present = _on_path()
    backends = [k for k in present if tools.TOOLS[k].kind == tools.BACKEND]
    require(bool(backends), "no stress backend installed (mprime/y-cruncher/stress-ng/stressapptest)")
    assert tools.unmet_requirements(tools.report()) == []


def test_core_tools_are_installed():
    require("taskset" in _on_path(), "taskset (util-linux) is not installed")
    assert tools.resolve("taskset").path is not None
