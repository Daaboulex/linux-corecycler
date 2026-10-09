from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "mutate.py"
_spec = importlib.util.spec_from_file_location("mutate", SCRIPT)
mutate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mutate
_spec.loader.exec_module(mutate)


def test_a_passing_selection_is_not_reported_as_failed_in_this_project_s_own_environment():
    assert mutate._run(["tests/test_main_args.py"], 120) is False


def test_a_failing_selection_is_reported_as_failed():
    assert mutate._run(["tests/test_main_args.py::TestParseAutoResume::test_no_such_test"], 120) is True
