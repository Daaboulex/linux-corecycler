from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "mutate.py"
_spec = importlib.util.spec_from_file_location("mutate", SCRIPT)
mutate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mutate
_spec.loader.exec_module(mutate)


def test_a_passing_selection_is_not_reported_as_failed_in_this_project_s_own_environment():
    assert mutate._failure(["tests/test_main_args.py"], 120) is None


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
        return "E   assert 1 == 2"

    diff = "diff --git a/src/x.py b/src/x.py\n+++ b/src/x.py\n@@ -0,0 +1 @@\n+x = 1\n"
    monkeypatch.setattr(mutate, "_failure", red)
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
    assert None not in built
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
    monkeypatch.setattr(mutate, "_baseline", lambda tests, timeout, root: {})
    stamp = tmp_path / "mutation-stamp"
    for verdict, commit, stamped in ((1, None, False), (0, "HEAD", False), (0, None, True)):
        monkeypatch.setattr(mutate, "_mutate", lambda *args, verdict=verdict: verdict)
        assert mutate._diff_mode(commit, ["tests/test_x.py"], 10) == verdict
        assert stamp.is_file() is stamped
    assert stamp.read_text() == "tree-1\n"


def test_a_mutant_knows_the_whole_statement_it_sits_in(tmp_path):
    source = "def f(a, b):\n    return (\n        a\n        < b\n    )\n"
    mutant = mutate._build(tmp_path / "m.py", source, 0)
    assert (mutant.description, mutant.lineno, mutant.span) == ("Lt -> LtE", 3, (2, 5))


def test_a_mutant_runs_only_the_tests_that_executed_its_statement_else_the_whole_selection(tmp_path):
    mutant = mutate.Mutant(tmp_path / "src" / "m.py", 3, "Lt -> LtE", "", (2, 5))
    covered = {"src/m.py": {2: frozenset({"tests/test_a.py::test_one"}), 4: frozenset({"tests/test_b.py::test_two"})}}
    everything = ["tests/test_a.py", "tests/test_b.py"]
    assert mutate._selected(mutant, covered, everything, tmp_path) == [
        "tests/test_a.py::test_one",
        "tests/test_b.py::test_two",
    ]
    at_import = {"src/m.py": {3: frozenset({"", "tests/test_a.py::test_one"})}}
    assert mutate._selected(mutant, at_import, everything, tmp_path) == everything
    assert mutate._selected(mutant, {}, everything, tmp_path) == everything


def test_a_handful_of_named_tests_runs_without_workers_and_anything_larger_with_them():
    few = [f"tests/test_a.py::test_{n}" for n in range(mutate.SERIAL_UP_TO)]
    assert "-n" not in mutate._pytest(few, [])
    assert "-n" in mutate._pytest([*few, "tests/test_a.py::test_more"], [])
    assert "-n" in mutate._pytest(["tests/test_a.py"], [])


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
        head + "from pkg.mod import one\n\n\ndef test_three():\n    assert one() == 1\n"
    )
    named = ["tests/test_one.py::test_one", "tests/test_three.py::test_three", "tests/test_two.py::test_two"]
    cover = mutate._baseline(named, 120, tmp_path)
    lines = cover["src/pkg/mod.py"]
    assert lines[2] == frozenset({"tests/test_one.py::test_one", "tests/test_three.py::test_three"})
    assert lines[6] == frozenset({"tests/test_two.py::test_two"})
    assert "" in lines[1]
