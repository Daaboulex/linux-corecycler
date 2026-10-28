from __future__ import annotations

import importlib.util
import json
import subprocess
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
        (["scripts/mutate.py"], "tree-1\n", False),
        (["scripts/mutate.py"], None, True),
        (["scripts/run_live_scenario.sh"], None, False),
    ],
)
def test_a_commit_of_source_needs_a_passing_mutation_run_of_its_exact_tree(changed, stamp, refused):
    found = check.problem(changed, "tree-1", stamp)
    assert (found is not None and "mutate.py --staged" in found) is refused


ANSWERING_GIT = (
    'import json\nimport os\nimport sys\n\nprint(json.loads(os.environ["FAKE_GIT_ANSWERS"])[" ".join(sys.argv[1:])])\n'
)


def _staged(monkeypatch, tmp_path, fake_git, changed):
    common = tmp_path / "repo" / ".git"
    own = common / "worktrees" / "wt"
    own.mkdir(parents=True)
    fake_git(ANSWERING_GIT)
    answers = {
        "rev-parse --path-format=absolute --git-common-dir": str(common),
        "rev-parse --path-format=absolute --git-dir": str(own),
        "diff --cached --name-only": "\n".join(changed),
        "write-tree": "tree-1",
    }
    monkeypatch.setenv("FAKE_GIT_ANSWERS", json.dumps(answers))
    return own


@pytest.mark.req("T26.mutation_hook")
def test_the_hook_refuses_an_unstamped_script_change_in_the_owner_s_checkout_and_passes_its_stamped_tree(
    monkeypatch, tmp_path, fake_git, capsys
):
    own = _staged(monkeypatch, tmp_path, fake_git, ["scripts/mutate.py", "tests/test_mutate_script.py"])
    (tmp_path / "repo" / "scratch").mkdir()
    assert check.main([]) == 1
    assert "commit-msg: the staged source has no passing mutation run" in capsys.readouterr().err
    (own / "mutation-stamp").write_text("tree-1\n")
    assert check.main([]) == 0


def test_the_hook_stays_out_of_a_checkout_without_the_scratch_directory(monkeypatch, tmp_path, fake_git):
    _staged(monkeypatch, tmp_path, fake_git, ["scripts/mutate.py"])
    assert check.main([]) == 0


def test_a_git_failure_stops_the_hook_rather_than_passing_the_commit(monkeypatch, fake_git):
    fake_git(ANSWERING_GIT)
    monkeypatch.setenv("FAKE_GIT_ANSWERS", "{}")
    with pytest.raises(subprocess.CalledProcessError) as failed:
        check.main([])
    assert failed.value.cmd[:2] == ["git", "rev-parse"]
