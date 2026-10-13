from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "check_mutation_stamp.py"
_spec = importlib.util.spec_from_file_location("check_mutation_stamp", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


@pytest.mark.req("T26.mutation_hook")
@pytest.mark.parametrize(
    ("changed", "stamp", "refused"),
    [
        (["tests/test_a.py", "tests/requirements.toml"], None, False),
        (["src/corecycler/a.py"], "tree-1\n", False),
        (["src/corecycler/a.py"], "tree-0\n", True),
        (["src/corecycler/a.py"], None, True),
    ],
)
def test_a_commit_of_source_needs_a_passing_mutation_run_of_its_exact_tree(changed, stamp, refused):
    found = check.problem(changed, "tree-1", stamp)
    assert (found is not None and "mutate.py --staged" in found) is refused
