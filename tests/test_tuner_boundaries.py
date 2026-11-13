from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

from corecycler.history.db import HistoryDB
from corecycler.smu.driver import RyzenSMU
from corecycler.tuner.live_hw import SmuHardware

SRC = Path(__file__).resolve().parents[1] / "src" / "corecycler"
OLD_ONLY = (
    "create_tuner_session",
    "start_co_offsets",
    "stamp_session_boot",
    "set_validation_dirty",
    "update_tuner_session_status",
    "get_tuner_session",
    "get_latest_tuner_session",
    "get_active_tuner_session",
    "list_resumable_tuner_sessions",
    "list_recoverable_tuner_sessions",
    "_sessions_with_status",
    "list_tuner_sessions",
    "get_unattributed_crashes",
    "set_unattributed_crashes",
    "set_validation_position",
    "set_hunting_core",
    "get_resume_crash_streak",
    "set_resume_crash_streak",
    "_check_core_state_sane",
    "upsert_tuner_core_state",
    "get_tuner_core_states",
    "insert_tuner_test_log",
    "get_tuner_test_log",
    "get_tuner_session_offsets",
    "get_tuner_best_profile",
    "_row_to_tuner_session",
)


def _trees(root: Path) -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text())) for path in sorted(root.rglob("*.py"))]


def _attributes(root: Path, name: str) -> list[str]:
    return [
        path.relative_to(SRC).as_posix()
        for path, tree in _trees(root)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == name
    ]


@pytest.mark.req("C15", "VERDICT.5")
def test_no_finding_table_or_grade_produces_a_clock_stretch_outcome():
    assert _attributes(SRC, "CLOCK_STRETCH") == []


@pytest.mark.req("WRITER.set_all")
def test_the_tuner_writes_each_core_through_set_co_offset_and_never_calls_set_all_co():
    assert _attributes(SRC / "tuner", "set_all_co") == []
    calls = []

    class Smu:
        value = 0

        def set_co_offset(self, core, value):
            calls.append(("set_co_offset", core, value))
            self.value = value
            return True

        def get_co_offset(self, core):
            return self.value

        def set_all_co(self, value):
            calls.append(("set_all_co", value))
            return True

    hardware = SmuHardware(Smu(), True, lambda line: None, lambda: None, vcache="")
    assert hardware.write_co(3, -7) is None
    assert calls == [("set_co_offset", 3, -7)]


@pytest.mark.req("TM.I1.11")
def test_the_driver_runs_y_cruncher_and_stressapptest_and_never_mprime():
    named = {
        node.args[0].value
        for _, tree in _trees(SRC / "tuner")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "get_backend"
    }
    assert named == {"y-cruncher", "stressapptest"}
    assert [path.name for path in (SRC / "tuner").rglob("*.py") if "mprime" in path.read_text().lower()] == []


@pytest.mark.req("E7.B", "D80")
def test_the_old_engine_and_the_database_methods_only_it_used_are_gone():
    gone = [SRC / "tuner" / name for name in ("engine.py", "config.py", "state.py", "persistence.py")]
    assert [path.name for path in (*gone, SRC / "engine" / "parallel.py") if path.exists()] == []
    assert [name for name in OLD_ONLY if hasattr(HistoryDB, name)] == []


@pytest.mark.req("E7.B.import")
def test_the_driver_imports_without_loading_pyside6():
    probe = "import sys, corecycler.tuner.live_hw; print(any(name.startswith('PySide6') for name in sys.modules))"
    env = {**os.environ, "PYTHONPATH": str(SRC.parent)}
    run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=False)
    assert (run.returncode, run.stdout.strip()) == (0, "False"), run.stderr


@pytest.mark.req("L25.a")
def test_the_stage_engine_and_the_stress_runner_never_name_a_knob_writer():
    writers = {name for name in vars(RyzenSMU) if name.startswith("set_")} | {"write_co"}
    assert {"set_co_offset", "set_all_co", "set_curve_shaper"} <= writers
    runner = next(
        node
        for node in ast.parse((SRC / "tuner" / "live_hw.py").read_text()).body
        if isinstance(node, ast.ClassDef) and node.name == "StressRunner"
    )
    stages = ast.parse((SRC / "tuner" / "stages.py").read_text())
    named = {node.attr for tree in (runner, stages) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert writers & named == set()
