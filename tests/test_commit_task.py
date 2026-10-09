from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_commit_task.py"
_spec = importlib.util.spec_from_file_location("check_commit_task", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

PLAN = "| T26 | No drift | in progress |\n- S0, read only, no write\n| H8 | the GUI | done |\n"
IDS = {"L01.12", "U18"}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("fix: x\n\nbody\nTest: none\n", ["the message names no Task: or Req: line"]),
        ("fix: x\n\nTask: T99\n", ["Task: T99 is not a task in scratch/PLAN.md"]),
        ("fix: x\n\nReq: L99.1\n", ["Req: L99.1 is not in tests/requirement_inventory.py"]),
        ("fix: x\n\nTask: T2\n", ["Task: T2 is not a task in scratch/PLAN.md"]),
        ("fix: x\n\nTask: T26\n", []),
        ("fix: x\n\nTask: S0, H8\nReq: L01.12, U18\n", []),
    ],
)
def test_a_commit_names_a_listed_task_or_requirement(message, expected):
    assert check.problems(message, PLAN, IDS) == expected
