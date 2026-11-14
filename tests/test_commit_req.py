from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.requirement_inventory import REQUIREMENTS

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


def _run(tmp_path, message):
    file = tmp_path / "message"
    file.write_text(message)
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(file)],
        cwd=SCRIPT.parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_script_passes_a_message_naming_a_listed_requirement(tmp_path):
    listed = next(iter(REQUIREMENTS)).id
    done = _run(tmp_path, f"fix: x\n\nReq: {listed}\n")
    assert (done.returncode, done.stderr) == (0, "")


def test_the_script_refuses_and_names_an_unlisted_requirement(tmp_path):
    done = _run(tmp_path, "fix: x\n\nReq: L99.1\n")
    assert done.returncode == 1
    assert done.stderr == "commit-msg: Req: L99.1 is not in tests/requirement_inventory.py\n"
