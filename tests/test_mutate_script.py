from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "mutate.py"
sys.path.insert(0, str(SCRIPT.parent))
_spec = importlib.util.spec_from_file_location("mutate", SCRIPT)
mutate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mutate
_spec.loader.exec_module(mutate)


def test_a_passing_selection_is_not_reported_as_failed_in_this_project_s_own_environment():
    assert mutate._failure(["tests/test_packaging.py"], 120) is None


def test_a_failing_selection_is_reported_with_pytest_s_own_reason(tmp_path):
    (tmp_path / "test_red.py").write_text("def test_red():\n    assert 1 == 2\n")
    failure = mutate._failure(["test_red.py"], 120, tmp_path)
    assert "assert 1 == 2" in failure


def test_the_copy_lets_a_test_binary_run_and_a_red_baseline_prints_why(monkeypatch, capsys):
    runnable = []

    def red(tests, timeout, root, extra=None, env=None):
        probe = root / "probe"
        probe.write_text("#!/bin/sh\n")
        probe.chmod(0o755)
        runnable.append(os.access(probe, os.X_OK))
        return False, "E   assert 1 == 2"

    diff = "diff --git a/src/x.py b/src/x.py\n+++ b/src/x.py\n@@ -0,0 +1 @@\n+x = 1\n"
    monkeypatch.setattr(mutate, "_outcome", red)
    monkeypatch.setattr(mutate, "_git", lambda *args: diff if "src/" in args else "")
    monkeypatch.setattr(mutate, "_export", lambda commit, into: None)
    assert [mutate._diff_mode(None, ["tests/test_x.py"], 10) for _ in range(2)] == [2, 2]
    assert runnable == [True, True]
    out = capsys.readouterr().out
    assert out.count("E   assert 1 == 2\nBASELINE FAILED") == 2


def test_the_diff_mode_takes_exactly_the_added_python_lines():
    diff = (
        "diff --git a/src/x.py b/src/x.py\n"
        "--- a/src/x.py\n"
        "+++ b/src/x.py\n"
        "@@ -3,0 +4,2 @@\n+a\n+b\n"
        "@@ -10 +12 @@\n-x\n+y\n"
        "@@ -20,3 +23,0 @@\n-p\n-q\n-r\n"
        "diff --git a/docs/x.md b/docs/x.md\n"
        "--- a/docs/x.md\n"
        "+++ b/docs/x.md\n"
        "@@ -1 +1 @@\n-a\n+b\n"
        "diff --git a/src/gone.py b/src/gone.py\n"
        "--- a/src/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n-a\n-b\n"
    )
    assert mutate.added_lines(diff) == {"src/x.py": {4, 5, 12}}


def test_a_line_filter_mutates_only_those_lines():
    source = "first = 1 < 2\nsecond = 3 < 4\n"
    assert mutate._count(source, {2}) == mutate._count("second = 3 < 4\n")
    assert mutate._count(source, set()) == 0
    assert mutate._count(source) == mutate._count(source, {1, 2})


def test_a_reviewed_equivalent_mutant_is_skipped_and_must_say_why():
    plain = "wait = 0\n"
    assert mutate._count("wait = 0  # equivalent-mutant: any delay defers to the event loop\n") == 0
    assert mutate._count(plain) == 1
    with pytest.raises(ValueError, match="line 1 marks an equivalent mutant without saying why"):
        mutate._count("wait = 0  # equivalent-mutant:\n")


def test_a_change_with_no_test_file_runs_the_tests_that_import_the_changed_module(tmp_path):
    (tmp_path / "test_a.py").write_text("from corecycler.tuner.live import LiveRig\n")
    (tmp_path / "test_b.py").write_text("from corecycler.tuner import live, stages\n")
    (tmp_path / "test_c.py").write_text("from corecycler.tuner.live_hw import main\n")
    (tmp_path / "helper.py").write_text("import corecycler.tuner.live\n")
    assert mutate.importers(["src/corecycler/tuner/live.py"], tmp_path) == ["tests/test_a.py", "tests/test_b.py"]


def test_every_mutant_is_built_from_the_original_text_even_after_one_was_written(tmp_path):
    path = tmp_path / "sample.py"
    original = "first = 1 < 2\nsecond = 3 < 4\n"
    path.write_text(original)
    total = mutate._count(original, {1, 2})
    path.write_text(mutate._build(path, original, 0, {1, 2}).source)
    built = [mutate._build(path, original, index, {1, 2}) for index in range(total)]
    assert len({mutant.source for mutant in built}) == total


@pytest.mark.req("T26.mutation_hook")
def test_only_a_clean_staged_run_stamps_the_tree_it_checked(monkeypatch, tmp_path):
    diff = "diff --git a/src/x.py b/src/x.py\n+++ b/src/x.py\n@@ -0,0 +1 @@\n+x = 1\n"

    def git(*args):
        if args[0] == "write-tree":
            return "tree-1\n"
        if args[0] == "rev-parse":
            return f"{tmp_path}\n"
        return diff if "src/" in args else ""

    monkeypatch.setattr(mutate, "_git", git)
    monkeypatch.setattr(mutate, "_export", lambda commit, into: None)
    monkeypatch.setattr(mutate, "_baseline", lambda tests, timeout, root: ({}, {}))
    stamp = tmp_path / "mutation-stamp"
    for verdict, commit, stamped in ((1, None, False), (0, "HEAD", False), (0, None, True)):
        monkeypatch.setattr(mutate, "_mutate", lambda *args, verdict=verdict: verdict)
        assert mutate._diff_mode(commit, ["tests/test_x.py"], 10) == verdict
        assert stamp.is_file() is stamped
    assert stamp.read_text() == "tree-1\n"


def _staged_run(monkeypatch, tmp_path, changed):
    diffs = {
        name: f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -2 +3,2 @@\n-old\n+new\n+newer\n"
        for name in changed
    }

    def git(*args):
        if args[0] == "write-tree":
            return "tree-1\n"
        if args[0] == "rev-parse":
            return f"{tmp_path}\n"
        under = [name for name in diffs if name.startswith(args[args.index("--") + 1 :])]
        return "\n".join(under) if "--name-only" in args else "".join(diffs[name] for name in under)

    mutated = []

    def clean(targets, tests, timeout, root, cap, baseline):
        mutated.extend((str(path.relative_to(root)), lines, tests) for path, lines in targets)
        return 0

    monkeypatch.setattr(mutate, "_git", git)
    monkeypatch.setattr(mutate, "_export", lambda commit, into: None)
    monkeypatch.setattr(mutate, "_baseline", lambda tests, timeout, root: ({}, {}))
    monkeypatch.setattr(mutate, "_mutate", clean)
    return mutate._diff_mode(None, None, 10), mutated


def test_a_staged_change_mutates_the_lines_it_adds_to_a_script_and_none_of_a_test_or_a_doc(monkeypatch, tmp_path):
    verdict, mutated = _staged_run(monkeypatch, tmp_path, ["docs/x.md", "scripts/tool.py", "tests/test_tool.py"])
    assert (verdict, mutated) == (0, [("scripts/tool.py", {3, 4}, ["tests/test_tool.py"])])


def test_a_clean_run_of_a_change_to_scripts_alone_stamps_the_tree_it_checked(monkeypatch, tmp_path):
    assert _staged_run(monkeypatch, tmp_path, ["scripts/tool.py", "tests/test_tool.py"])[0] == 0
    assert (tmp_path / "mutation-stamp").read_text() == "tree-1\n"


def test_a_staged_change_to_src_is_mutated_and_stamped_as_it_always_was(monkeypatch, tmp_path):
    verdict, mutated = _staged_run(monkeypatch, tmp_path, ["docs/x.md", "src/corecycler/x.py", "tests/test_x.py"])
    assert (verdict, mutated) == (0, [("src/corecycler/x.py", {3, 4}, ["tests/test_x.py"])])
    assert (tmp_path / "mutation-stamp").read_text() == "tree-1\n"


def test_a_change_with_no_python_line_under_src_or_scripts_mutates_nothing_and_stamps_nothing(
    monkeypatch, tmp_path, capsys
):
    verdict, mutated = _staged_run(monkeypatch, tmp_path, ["docs/x.md", "scripts/run.sh", "tests/test_x.py"])
    assert (verdict, mutated) == (0, [])
    assert capsys.readouterr().out == "no source lines changed: nothing to mutate\n"
    assert not (tmp_path / "mutation-stamp").exists()


def test_a_change_no_test_touches_imports_or_is_named_for_is_refused_saying_how_to_name_them(
    monkeypatch, tmp_path, capsys
):
    verdict, mutated = _staged_run(monkeypatch, tmp_path, ["scripts/tool.py"])
    assert (verdict, mutated) == (2, [])
    assert "name the tests that prove it with --tests" in capsys.readouterr().out
    assert not (tmp_path / "mutation-stamp").exists()


FAKE_GIT = """import shutil
import sys
import tarfile
from pathlib import Path

command, *rest = sys.argv[1:]
if command == "rev-parse":
    print(Path.cwd())
elif command == "checkout-index" and rest[0] == "--all":
    shutil.copytree("index", rest[1].removeprefix("--prefix="), dirs_exist_ok=True)
elif command == "archive" and Path("commits", rest[0]).is_dir():
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        archive.add(Path("commits", rest[0]), arcname=".")
else:
    sys.exit(f"fatal: {command} {rest}")
"""


def test_git_runs_in_the_repository_the_copy_holds_the_index_or_the_commit_and_a_git_or_tar_failure_stops_it(
    monkeypatch, tmp_path, fake_git
):
    repo = tmp_path / "repo"
    for tree, text in (("index", "staged = 1\n"), ("commits/c0ffee", "committed = 1\n")):
        (repo / tree / "src").mkdir(parents=True)
        (repo / tree / "src" / "a.py").write_text(text)
    fake_git(FAKE_GIT)
    monkeypatch.setattr(mutate, "REPO", repo)
    assert mutate._git("rev-parse", "--show-toplevel") == f"{repo}\n"
    for commit, text in ((None, "staged = 1\n"), ("c0ffee", "committed = 1\n")):
        into = tmp_path / f"copy-{commit}"
        into.mkdir()
        mutate._export(commit, into)
        assert (into / "src" / "a.py").read_text() == text
    with pytest.raises(subprocess.CalledProcessError) as unknown_commit:
        mutate._export("deadbeef", tmp_path / "copy-None")
    with pytest.raises(subprocess.CalledProcessError) as unwritable_copy:
        mutate._export("c0ffee", tmp_path / "missing")
    with pytest.raises(subprocess.CalledProcessError) as failed_git:
        mutate._git("cat-file", "-e", "deadbeef")
    failures = (unknown_commit, unwritable_copy, failed_git)
    assert [failure.value.cmd[:2] for failure in failures] == [["git", "archive"], ["tar", "-x"], ["git", "cat-file"]]


def test_staged_and_commit_run_the_diff_mode_with_the_named_tests_and_timeout(monkeypatch):
    ran = []
    monkeypatch.setattr(mutate, "_diff_mode", lambda *args: ran.append(args) or 7)
    for argv in (
        ["--staged", "--tests", "tests/test_a.py", "--timeout", "45"],
        ["--commit", "HEAD~2", "--timeout", "30"],
    ):
        monkeypatch.setattr(sys, "argv", ["mutate.py", *argv])
        assert mutate.main() == 7
    assert ran == [(None, ["tests/test_a.py"], 45), ("HEAD~2", None, 30)]


def test_src_mutates_each_python_file_under_a_named_directory_and_each_named_file_against_the_named_tests(
    monkeypatch, tmp_path
):
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    for name in ("pkg/b.py", "pkg/a.py", "pkg/sub/c.py", "pkg/notes.txt", "one.py"):
        (tmp_path / name).write_text("x = 1\n")
    ran = []
    baseline = ({}, {})
    monkeypatch.setattr(mutate, "_baseline", lambda *args: ran.append(args) or baseline)
    monkeypatch.setattr(mutate, "_mutate", lambda *args: ran.append(args) or 1)
    argv = ["--src", str(tmp_path / "pkg"), "--src", str(tmp_path / "one.py"), "--tests", "tests/test_a.py"]
    monkeypatch.setattr(sys, "argv", ["mutate.py", *argv, "--max", "4", "--timeout", "45"])
    assert mutate.main() == 1
    targets = [(tmp_path / name, None) for name in ("pkg/a.py", "pkg/b.py", "pkg/sub/c.py", "one.py")]
    tests = ["tests/test_a.py"]
    assert ran == [(tests, 45, mutate.REPO), (targets, tests, 45, mutate.REPO, 4, baseline)]


def test_src_without_max_runs_every_mutant_of_the_named_file_and_restores_it(monkeypatch, tmp_path):
    source = "def f(x):\n    return x < 2\n"
    (tmp_path / "m.py").write_text(source)
    ran = []
    monkeypatch.setattr(mutate, "REPO", tmp_path)
    monkeypatch.setattr(mutate, "_baseline", lambda *args: ({}, {}))
    monkeypatch.setattr(mutate, "_failure", lambda tests, timeout, root: ran.append(tests) or "killed")
    monkeypatch.setattr(sys, "argv", ["mutate.py", "--src", str(tmp_path / "m.py"), "--tests", "tests/test_m.py"])
    assert mutate.main() == 0
    assert len(ran) == mutate._count(source) > 1
    assert (tmp_path / "m.py").read_text() == source


def test_no_mode_or_src_without_tests_is_a_usage_error_and_a_red_baseline_mutates_nothing(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(mutate, "_diff_mode", lambda *args: pytest.fail("no mode must not run a diff"))
    monkeypatch.setattr(mutate, "_baseline", lambda *args: None)
    monkeypatch.setattr(mutate, "_mutate", lambda *args: pytest.fail("a red baseline must not start the mutants"))
    monkeypatch.setattr(sys, "argv", ["mutate.py", "--src", str(tmp_path), "--tests", "tests/test_a.py"])
    assert mutate.main() == 2
    for argv, reason in (
        (["--tests", "tests/test_a.py"], "one of the arguments --src --staged --commit is required"),
        (["--src", str(tmp_path)], "--src needs --tests"),
    ):
        monkeypatch.setattr(sys, "argv", ["mutate.py", *argv])
        with pytest.raises(SystemExit) as refused:
            mutate.main()
        assert refused.value.code == 2
        assert reason in capsys.readouterr().err


def test_each_and_each_or_and_each_boolean_constant_is_flipped_alone_and_their_operands_are_mutated_too(tmp_path):
    source = "both = a < 1 and b\neither = a or b\nflag = True\n"
    built = [mutate._build(tmp_path / "m.py", source, index) for index in range(mutate._count(source))]
    assert [(mutant.lineno, mutant.description, mutant.source) for mutant in built] == [
        (1, "1 -> 2", "both = a < 2 and b\neither = a or b\nflag = True"),
        (1, "Lt -> LtE", "both = a <= 1 and b\neither = a or b\nflag = True"),
        (1, "And -> Or", "both = a < 1 or b\neither = a or b\nflag = True"),
        (2, "Or -> And", "both = a < 1 and b\neither = a and b\nflag = True"),
        (3, "True -> False", "both = a < 1 and b\neither = a or b\nflag = False"),
    ]


def test_a_decorator_s_arguments_and_a_dunder_setting_make_no_mutant():
    body = "def f(x):\n    return x < 2\n"
    assert mutate._count("@cache(size=3, typed=True)\n" + body) == mutate._count(body) > 0
    assert mutate._count("__test__ = False\n") == 0 < mutate._count("test = False\n")


def test_a_mutant_knows_the_whole_statement_it_sits_in(tmp_path):
    source = "def f(a, b):\n    return (\n        a\n        < b\n    )\n"
    mutant = mutate._build(tmp_path / "m.py", source, 0)
    assert (mutant.description, mutant.lineno, mutant.span) == ("Lt -> LtE", 3, (2, 5))


DELETIONS = (
    '"""Module docstring."""\n'
    "import os\n"
    "LIMIT = 5\n"
    "print(LIMIT)\n"
    "\n"
    "\n"
    "class C:\n"
    "    STEP = 2\n"
    "\n"
    "    def f(self, beat, items):\n"
    '        """Docstring."""\n'
    "        import json\n"
    "        global LIMIT\n"
    "        seen: int\n"
    "        beat.between(\n"
    "            items,\n"
    "        )\n"
    "        total = 0\n"
    "        total += len(items)\n"
    "        self.seen: int = total\n"
    "        if not items:\n"
    "            raise ValueError(total)\n"
    "        del items[0]\n"
    "        for item in items:\n"
    "            if item:\n"
    "                continue\n"
    "            break\n"
    "        ...\n"
    "        pass\n"
    "        return json.dumps(total)\n"
    "\n"
    "\n"
    "async def g(lock):\n"
    "    await lock.acquire()\n"
)


def test_each_simple_statement_inside_a_function_is_deleted_alone_as_pass_and_nothing_else_is(tmp_path):
    built = [mutate._build(tmp_path / "src" / "m.py", DELETIONS, index) for index in range(mutate._count(DELETIONS))]
    deleted = [mutant for mutant in built if mutant.description == "delete statement"]
    spans = [(15, 17), (18, 18), (19, 19), (20, 20), (22, 22), (23, 23), (26, 26), (27, 27), (30, 30), (34, 34)]
    assert [mutant.span for mutant in deleted] == spans
    assert {(mutant.lineno, mutant.at_import) for mutant in deleted} == {(first, False) for first, _ in spans}
    lines = DELETIONS.splitlines()
    for mutant, (first, last) in zip(deleted, spans, strict=True):
        indent = lines[first - 1][: len(lines[first - 1]) - len(lines[first - 1].lstrip())]
        expected = "\n".join([*lines[: first - 1], f"{indent}pass", *lines[last:]])
        assert mutant.source == ast.unparse(ast.parse(expected))


def test_a_deletion_obeys_the_changed_lines_and_a_reviewed_equivalent_mutant():
    source = "def f(beat):\n    beat.between()\n    beat.idle()\n"
    assert mutate._count(source) == 2
    assert mutate._count(source, {3}) == 1
    reviewed = source.replace("idle()", "idle()  # equivalent-mutant: nothing reads the idle beat here")
    assert mutate._count(reviewed) == 1


def test_a_return_of_none_that_ends_a_function_is_never_deleted_but_an_early_one_is(tmp_path):
    source = (
        "def f(x):\n    if x:\n        return None\n    x.go()\n    return None\n\n\n"
        "def g(x):\n    x.stop()\n    return\n\n\ndef h(x):\n    return x\n"
    )
    built = [mutate._build(tmp_path / "m.py", source, index) for index in range(mutate._count(source))]
    assert [mutant.lineno for mutant in built if mutant.description == "delete statement"] == [3, 4, 9, 14]


def test_a_mutant_runs_only_the_tests_that_executed_its_statement_short_ones_first_else_the_whole_selection(
    tmp_path,
):
    mutant = mutate.Mutant(tmp_path / "src" / "m.py", 3, "Lt -> LtE", "", (2, 5))
    one, two, slow = "tests/test_a.py::test_one", "tests/test_b.py::test_two", "tests/test_b.py::test_slow"
    covered = {"src/m.py": {2: frozenset({one, slow}), 4: frozenset({two}), 9: frozenset({"tests/test_c.py::x"})}}
    durations = {one: 0.1, slow: mutate.SHORT_S + 0.1}
    everything = ["tests/test_a.py", "tests/test_b.py"]
    assert mutate._tiers(mutant, covered, durations, everything, tmp_path) == [[one, two], [slow]]
    assert mutate._tiers(mutant, covered, {}, everything, tmp_path) == [[one, slow, two]]
    at_import = {"src/m.py": {3: frozenset({"", one})}}
    assert mutate._tiers(mutant, at_import, durations, everything, tmp_path) == [everything]
    assert mutate._tiers(mutant, {}, durations, everything, tmp_path) == [everything]


def test_a_mutant_in_code_that_runs_at_import_runs_the_whole_selection(tmp_path):
    source = (
        "LIMIT = 5\n\n\nclass C:\n    STEP = 2\n\n    def f(self, x=3):\n        return x > LIMIT\n\n\n"
        "next_one = lambda y: y + 7\n\n\ndef g(z=11): return z\n"
    )
    built = [mutate._build(tmp_path / "src" / "m.py", source, index) for index in range(mutate._count(source))]
    assert {(mutant.description, mutant.at_import) for mutant in built} == {
        ("5 -> 6", True),
        ("2 -> 3", True),
        ("3 -> 4", True),
        ("Gt -> GtE", False),
        ("7 -> 8", False),
        ("Add -> Sub", False),
        ("11 -> 12", True),
        ("delete statement", False),
    }
    limit = next(mutant for mutant in built if mutant.description == "5 -> 6")
    first_importer = {"src/m.py": {1: frozenset({"tests/test_a.py::test_one"})}}
    everything = ["tests/test_a.py", "tests/test_b.py"]
    assert mutate._tiers(limit, first_importer, {}, everything, tmp_path) == [everything]
    compare = next(mutant for mutant in built if mutant.description == "Gt -> GtE")
    one, two = "tests/test_a.py::test_one", "tests/test_b.py::test_two"
    called_at_import = {"src/m.py": {1: frozenset({one}), compare.lineno: frozenset({one})}}
    assert mutate._tiers(compare, called_at_import, {}, everything, tmp_path) == [everything]
    called_by_two = {"src/m.py": {1: frozenset({one}), compare.lineno: frozenset({one, two})}}
    assert mutate._tiers(compare, called_by_two, {}, everything, tmp_path) == [[one, two]]


def test_a_mutant_killed_by_a_short_test_never_starts_the_long_ones_and_a_survivor_runs_both(monkeypatch, tmp_path):
    source = tmp_path / "src" / "m.py"
    source.parent.mkdir()
    source.write_text("def flag():\n    return 1 < 2\n")
    short, long = "tests/test_m.py::test_short", "tests/test_m.py::test_long"
    baseline = ({"src/m.py": {1: frozenset({short}), 2: frozenset({short, long})}}, {long: mutate.SHORT_S * 10})
    for killing, verdict, runs in ((short, 0, [[short]]), (None, 1, [[short], [long]])):
        ran = []

        def failure(tests, timeout, root, killing=killing, ran=ran):
            ran.append(tests)
            return "killed" if killing in tests else None

        monkeypatch.setattr(mutate, "_failure", failure)
        assert mutate._mutate([(source, None)], [], 10, tmp_path, 0, baseline) == verdict
        assert ran == runs * mutate._count("def flag():\n    return 1 < 2\n")
    assert source.read_text() == "def flag():\n    return 1 < 2\n"


def test_a_tier_waits_a_multiple_of_its_tests_serial_baseline_seconds_at_least_the_floor_at_most_the_timeout():
    quick, first, second = "tests/test_a.py::test_quick", "tests/test_a.py::test_first", "tests/test_b.py::test_second"
    durations = {quick: 0.1, first: 20.0, second: 30.0}
    assert mutate._limit([quick], durations, 600) == mutate.LIMIT_FLOOR_S < 600
    assert mutate.LIMIT_FACTOR * 50.0 <= mutate._limit([first, second], durations, 600) < 600
    assert mutate._limit([first, second], durations, 100) == 100


def test_a_tier_holding_a_test_the_baseline_did_not_time_waits_the_whole_timeout():
    known = "tests/test_a.py::test_known"
    for tier in ([known, "tests/test_a.py::test_new"], ["tests/test_a.py"], []):
        assert mutate._limit(tier, {known: 0.1}, 600) == 600


def test_a_mutant_that_hangs_its_tests_is_killed_at_its_tier_s_limit_not_at_the_whole_timeout(
    monkeypatch, tmp_path, capsys
):
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    source = tmp_path / "src" / "m.py"
    source.write_text("def wait(ready):\n    while not ready():\n        pass\n    return True\n")
    (tmp_path / "tests" / "test_m.py").write_text(
        'import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent.parent / "src"))\n'
        "from m import wait\n\n\ndef test_wait():\n    assert wait(lambda: True)\n"
    )
    test = "tests/test_m.py::test_wait"
    baseline = ({"src/m.py": {1: frozenset({""}), 2: frozenset({test})}}, {test: 0.01})
    monkeypatch.setattr(mutate, "LIMIT_FLOOR_S", 2)
    timeout = 60
    started = time.monotonic()
    assert mutate._mutate([(source, {2})], [test], timeout, tmp_path, 0, baseline) == 0
    assert time.monotonic() - started < timeout / 2
    out = capsys.readouterr().out
    assert "mutants 1  killed 1  survived 0" in out
    assert "\nT\n" in out and "  TIMED OUT src/m.py:2" in out and "(limit 2 s)" in out


def test_a_handful_of_named_tests_runs_on_one_worker_and_anything_larger_as_the_machine_allows(monkeypatch):
    asked = []
    monkeypatch.setattr(mutate.fit, "run", lambda args, timeout, root, env, most: asked.append(most) or (True, ""))
    few = [f"tests/test_a.py::test_{n}" for n in range(mutate.SERIAL_UP_TO)]
    for tests in (few, [*few, "tests/test_a.py::test_more"], ["tests/test_a.py"]):
        mutate._outcome(tests, 10)
    assert asked == [1, None, None]


def test_the_baseline_maps_each_line_to_the_tests_that_executed_it(tmp_path):
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "mod.py").write_text("def one():\n    return 1\n\n\ndef two():\n    return 2\n")
    (tmp_path / "tests").mkdir()
    head = 'import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent.parent / "src"))\n'
    (tmp_path / "tests" / "test_one.py").write_text(
        head + "from pkg.mod import one\n\n\ndef test_one():\n    assert one() == 1\n"
    )
    (tmp_path / "tests" / "test_two.py").write_text(
        head + "from pkg.mod import two\n\n\ndef test_two():\n    assert two() == 2\n"
    )
    (tmp_path / "tests" / "test_three.py").write_text(
        head
        + "import time\n\nfrom pkg.mod import one\n\n\ndef test_three():\n    time.sleep(0.3)\n    assert one() == 1\n"
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tool.py").write_text("def four():\n    return 4\n")
    (tmp_path / "tests" / "test_tool.py").write_text(
        head.replace('"src"', '"scripts"') + "from tool import four\n\n\ndef test_tool():\n    assert four() == 4\n"
    )
    named = [
        "tests/test_one.py::test_one",
        "tests/test_three.py::test_three",
        "tests/test_tool.py::test_tool",
        "tests/test_two.py::test_two",
    ]
    cover, durations = mutate._baseline(named, 120, tmp_path)
    assert set(durations) == set(named)
    assert durations["tests/test_three.py::test_three"] >= 0.3 > durations["tests/test_one.py::test_one"]
    lines = cover["src/pkg/mod.py"]
    assert lines[2] == frozenset({"tests/test_one.py::test_one", "tests/test_three.py::test_three"})
    assert lines[6] == frozenset({"tests/test_two.py::test_two"})
    assert "" in lines[1]
    assert cover["scripts/tool.py"][2] == frozenset({"tests/test_tool.py::test_tool"})
