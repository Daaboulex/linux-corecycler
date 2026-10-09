from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from corecycler.tuner.live import LiveRig
from corecycler.tuner.machine.types import World

TUNER = Path(__file__).parent.parent / "src" / "corecycler" / "tuner"


def keywords(path: Path, callees: set[str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Call):
            callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            if callee in callees:
                found |= {keyword.arg for keyword in node.keywords if keyword.arg is not None}
    return found


def test_every_input_the_machine_reads_has_a_producer_in_the_live_rig():
    fields = {field.name for field in dataclasses.fields(World)}
    assert fields - keywords(TUNER / "live.py", {"World", "replace"}) == set()


def test_the_driver_gives_the_rig_every_input_it_has():
    inputs = {field.name for field in dataclasses.fields(LiveRig) if field.init}
    assert inputs - keywords(TUNER / "live_hw.py", {"LiveRig"}) == set()


def test_the_wiring_checks_see_a_missing_producer(tmp_path):
    rig = tmp_path / "live.py"
    rig.write_text("world = World(now=1.0)\nworld = replace(world, temperature_c=40.0)\n")
    assert keywords(rig, {"World", "replace"}) == {"now", "temperature_c"}
