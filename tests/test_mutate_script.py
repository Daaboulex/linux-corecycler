from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "mutate.py"
_spec = importlib.util.spec_from_file_location("mutate", SCRIPT)
mutate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mutate
_spec.loader.exec_module(mutate)


def test_a_passing_selection_is_not_reported_as_failed_in_this_project_s_own_environment():
    assert mutate._run(["tests/test_main_args.py"], 120) is False


def test_a_failing_selection_is_reported_as_failed():
    assert mutate._run(["tests/test_main_args.py::TestParseAutoResume::test_no_such_test"], 120) is True


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
