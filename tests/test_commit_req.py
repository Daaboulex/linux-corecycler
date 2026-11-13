from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_commit_req.py"
_spec = importlib.util.spec_from_file_location("check_commit_req", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

IDS = {"L01.12", "U18"}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("docs: x\n\nbody\nTest: none\n", []),
        ("fix: x\n\nReq: L01.12\n", []),
        ("fix: x\n\nReq: L01.12, U18\n", []),
        ("fix: x\n\nReq: L99.1\n", ["Req: L99.1 is not in tests/requirement_inventory.py"]),
        ("fix: x\n\nReq: L01.12, L99.1\n", ["Req: L99.1 is not in tests/requirement_inventory.py"]),
        ("fix: x\n\nTask: T26\n", []),
    ],
)
def test_a_named_requirement_is_a_listed_one(message, expected):
    assert check.problems(message, IDS) == expected
