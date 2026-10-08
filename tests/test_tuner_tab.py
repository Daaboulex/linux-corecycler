from __future__ import annotations

import json
import os
import signal
import sys
import time
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

if not hasattr(sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("tuner tab tests require real PySide6", allow_module_level=True)

from PySide6.QtWidgets import QApplication

from corecycler.cli import EXIT_PAUSED
from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.gui import tuner_tab as tt
from corecycler.history.db import HistoryDB
from corecycler.smu.writer_lock import co_writer
from corecycler.tuner.live import config_from_json
from corecycler.tuner.machine.session import config_problem
from corecycler.tuner.machine.types import MachineConfig, Mode
from corecycler.tuner.orders import Method
from tests._machine import this_boot
from tests._tuner_world import save_machine_session

CO_RANGE = (-50, 10)


@pytest.fixture(autouse=True, scope="module")
def _qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(tt, "data_dir", lambda: data)
    monkeypatch.setattr(tt, "QMessageBox", MagicMock())
    monkeypatch.setattr(tt, "ensure_tool", lambda parent, key: True)
    return data


def topology(cores: int = 4) -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=cores, ccds=2)
    for core in range(cores):
        topo.cores[core] = PhysicalCore(core_id=core, ccd=core // 2, ccx=None, logical_cpus=(core,))
    return topo


def tab(db, co_range=CO_RANGE, reads=None) -> tt.TunerTab:
    return tt.TunerTab(db, topology(), co_range, lambda core: (reads or {}).get(core, 0))


class FakeChild:
    def __init__(self, code=None):
        self.code = code
        self.args = None

    def poll(self):
        return self.code


@pytest.fixture
def launched(monkeypatch):
    calls = []

    def popen(command, **kwargs):
        child = FakeChild()
        child.args = command
        calls.append((command, kwargs))
        return child

    monkeypatch.setattr(tt.subprocess, "Popen", popen)
    return calls


def test_every_machine_knob_is_offered_once(db, home):
    t = tab(db)
    names = {f.name for f in fields(MachineConfig)}
    assert set(t._knobs) == names - set(tt.SET_BY_THE_TAB)
    assert set(t._stage_spins) == {load for load, _ in MachineConfig(Mode.CO, Method.SEQUENTIAL, ()).stage_seconds}
    assert set(tt.KNOB_TEXT[tt.SHOWN]) <= set(t._knobs)


def test_a_setting_without_a_name_fails_the_tab(monkeypatch):
    texts = {group: dict(knobs) for group, knobs in tt.KNOB_TEXT.items()}
    del texts["Temperature"]["cool_min_s"]
    texts["Temperature"]["cool_minimum"] = ("x", "s", "x")
    monkeypatch.setattr(tt, "KNOB_TEXT", texts)
    with pytest.raises(AssertionError, match=r"disagree: \['cool_min_s', 'cool_minimum'\]"):
        tt._assert_every_knob_named()


def test_every_setting_shows_its_name_unit_and_what_it_does(db, home):
    t = tab(db)
    t._advanced_toggle.setChecked(True)
    shown = {label.text() for label in t.findChildren(tt.QLabel)}
    for knobs in tt.KNOB_TEXT.values():
        for label, unit, about in knobs.values():
            assert {label, about} <= shown
            assert not unit or any(text == unit or text.startswith(f"{unit} (") for text in shown)
    assert not any("_" in text for text in shown)


@pytest.mark.parametrize(
    ("seconds", "said"), [(59.0, ""), (60.0, "1 min"), (90.0, "1 min 30 s"), (5400.0, "1 h 30 min"), (7200.0, "2 h")]
)
def test_a_duration_is_also_said_in_minutes_and_hours(seconds, said):
    assert tt.spoken(seconds) == said


def test_the_unit_follows_the_box(db, home):
    t = tab(db)

    def units():
        return [label.text() for label in t.findChildren(tt.QLabel) if label.text().startswith("s (")]

    assert units().count("s (2 h)") == 1
    t._knobs["walk_budget_s"].setValue(5400.0)
    assert "s (2 h)" not in units() and units().count("s (1 h 30 min)") == 1


def test_a_refusal_names_the_setting_as_the_tab_does():
    assert tt.labelled("fine_step 6 is above coarse_step 5; soak_seconds 0 is below 1") == (
        "'Fine step' 6 is above 'Coarse step' 5; 'Idle watch length' 0 is below 1"
    )


def test_the_defaults_build_the_spec_s_configuration_on_every_core(db, home):
    t = tab(db)
    assert t.config() == MachineConfig(Mode.CO, Method.ALTERNATE, (0, 1, 2, 3))
    assert config_problem(t.config(), CO_RANGE) is None


def test_the_offset_control_starts_inside_the_cpu_s_range(db, home):
    t = tab(db, co_range=(-30, 30))
    assert (t._knobs["max_offset"].minimum(), t._knobs["max_offset"].maximum()) == (-30, -1)
    assert t.config().max_offset == -30


def test_edited_settings_reach_the_configuration(db, home):
    t = tab(db)
    t._core_boxes[3].setChecked(False)
    t._order.setCurrentText("One after another")
    t._knobs["soak"].setChecked(False)
    t._knobs["k_1t"].setValue(4)
    t._knobs["walk_budget_s"].setValue(600.0)
    t._stage_spins["idle"].setValue(30.0)
    cfg = t.config()
    assert (cfg.selected, cfg.method, cfg.soak, cfg.k_1t, cfg.walk_budget_s) == (
        (0, 1, 2),
        Method.SEQUENTIAL,
        False,
        4,
        600.0,
    )
    assert dict(cfg.stage_seconds)["idle"] == 30.0


def test_no_typed_value_falls_below_a_floor(db, home):
    t = tab(db)
    for widget in t._knobs.values():
        if not hasattr(widget, "setChecked"):
            widget.setValue(-(10**6))
    for spin in t._stage_spins.values():
        spin.setValue(-5.0)
    problem = config_problem(replace(t.config(), coarse_step=1, fine_step=1, cool_cap_s=1.0), CO_RANGE)
    assert problem is None


def test_the_advanced_section_opens_and_closes(db, home):
    t = tab(db)
    assert t._advanced.isHidden()
    t._advanced_toggle.setChecked(True)
    assert not t._advanced.isHidden()


def test_without_a_session_nothing_but_start_is_offered(db, home):
    t = tab(db)
    assert t._status.text() == "No tuning session"
    assert t._buttons["Start"].isEnabled()
    assert not any(t._buttons[name].isEnabled() for name in t._buttons if name != "Start")
    assert t.waiting_session() is None


def test_without_a_writable_co_or_a_database_start_is_off(home):
    t = tt.TunerTab(None, topology(), None, None)
    assert not t._buttons["Start"].isEnabled()
    assert t._view is None


def test_a_paused_session_shows_its_cores_and_offers_resume(db, home, tmp_path):
    t = tab(db)
    infos = []
    t.tuner_core_info.connect(lambda core, value, phase: infos.append((core, value, phase)))
    saved = save_machine_session(db, tmp_path, "paused")
    db.insert_tuner_event(saved.session_id, "user: paused by the owner", this_boot(), "paused")
    t.refresh()
    assert t._status.text() == f"Session {saved.session_id}: Paused. Asked by you: paused by the owner"
    assert t._cores.rowCount() == len(saved.cores)
    assert t._range.text() == "Power state: balanced profile"
    assert t._cores.item(0, 1).text() == str(saved.cores[0].value)
    assert infos == [(r.core, r.value, r.phase.value) for r in saved.cores]
    t.refresh()
    assert len(infos) == len(saved.cores)
    assert t._buttons["Resume"].isEnabled()
    assert not t._buttons["Validate"].isEnabled()
    assert not t._buttons["Continue"].isEnabled()
    assert t.waiting_session().session_id == saved.session_id
    assert "paused by the owner" in t._events.toPlainText()


def test_continue_and_restart_follow_a_setup_pause_only(db, home, tmp_path):
    saved = save_machine_session(db, tmp_path, "paused")
    db.insert_tuner_event(saved.session_id, "setup_changed: bios was A, is now B", this_boot(), "paused")
    t = tab(db)
    assert t._buttons["Continue"].isEnabled() and t._buttons["Restart"].isEnabled()
    db._execute_raw("UPDATE tuner_sessions SET status='running' WHERE id=?", (saved.session_id,))
    t.refresh()
    assert not t._buttons["Continue"].isEnabled() and not t._buttons["Restart"].isEnabled()
    db._execute_raw("UPDATE tuner_sessions SET status='paused' WHERE id=?", (saved.session_id,))
    db.insert_tuner_event(saved.session_id, "user: closed", this_boot(), "paused")
    t.refresh()
    assert not t._buttons["Continue"].isEnabled() and not t._buttons["Restart"].isEnabled()


def test_a_completed_session_offers_validate(db, home, tmp_path):
    save_machine_session(db, tmp_path, "completed")
    t = tab(db)
    assert t._buttons["Validate"].isEnabled()
    assert not t._buttons["Resume"].isEnabled()
    assert t.waiting_session() is None


def test_an_unreadable_session_is_named(db, home, tmp_path):
    saved = save_machine_session(db, tmp_path, "paused")
    db._execute_raw("UPDATE tuner_sessions SET machine_json='{}' WHERE id=?", (saved.session_id,))
    t = tab(db)
    assert t._status.text().startswith(f"session {saved.session_id} cannot be read: ")
    assert t._cores.rowCount() == 0


def test_a_running_driver_locks_start_and_offers_its_controls(db, home, tmp_path, monkeypatch):
    saved = save_machine_session(db, tmp_path, "running")
    db.insert_tuner_test(
        saved.session_id, this_boot(), 1, "search", "1t", "1t", saved.cores[0].core, -3, "[]", "[0]", 60.0, "{}"
    )
    t = tab(db)
    seen, grid, elapsed = [], [], []
    t.tuner_running_changed.connect(seen.append)
    t.tuner_core_testing.connect(lambda core, state: grid.append((core, state)))
    t.tuner_core_elapsed.connect(lambda core, s: elapsed.append(core))
    with co_writer(record=True):
        t.refresh()
        assert t.is_running
        assert seen == [True]
        assert not t._buttons["Start"].isEnabled() and not t._buttons["Resume"].isEnabled()
        assert all(t._buttons[name].isEnabled() for name in ("Pause", "Stop", "Abort"))
        assert t._test.text().startswith(f"Testing core {saved.cores[0].core} at -3, one thread: Search, ")
        assert (saved.cores[0].core, "testing") in grid and elapsed == [saved.cores[0].core]
        kills = []
        monkeypatch.setattr(tt.os, "kill", lambda pid, number: kills.append((pid, number)))
        t._buttons["Pause"].click()
        t._buttons["Stop"].click()
        tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.No
        t._buttons["Abort"].click()
        tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
        t._buttons["Abort"].click()
        assert kills == [(os.getpid(), signal.SIGUSR1), (os.getpid(), signal.SIGTERM), (os.getpid(), signal.SIGUSR2)]
        assert t._driver.text() == f"asked driver {os.getpid()} to abort"
    t.refresh()
    assert seen == [True, False]
    assert t._test.text() == ""
    assert t._buttons["Start"].isEnabled()


@pytest.mark.parametrize(
    ("stage", "load", "said"),
    [
        ("steady", "hot", "Testing every core: Hot all-core, "),
        ("bisect", "hot", "Testing a group of cores: Narrowing, "),
    ],
)
def test_an_all_core_test_names_its_cores_and_stage(db, home, tmp_path, stage, load, said):
    saved = save_machine_session(db, tmp_path, "running")
    db.insert_tuner_test(
        saved.session_id, this_boot(), 1, stage, load, "1t", saved.cores[0].core, -3, "[]", "[0, 1]", 510.0, "{}"
    )
    t = tab(db)
    with co_writer(record=True):
        t.refresh()
        assert t._test.text().startswith(said)
        assert t._test.text().endswith(" of 510 s")


def test_a_signal_without_a_driver_or_permission_is_said(db, home, monkeypatch):
    t = tab(db)
    t._signal(signal.SIGUSR1, "pause")
    assert t._driver.text() == "no tuner driver is running"

    def refuse(pid, number):
        raise PermissionError("not yours")

    monkeypatch.setattr(tt.os, "kill", refuse)
    with co_writer(record=True):
        t._signal(signal.SIGUSR1, "pause")
    assert t._driver.text() == f"could not ask driver {os.getpid()} to pause: not yours"


def test_start_writes_the_configuration_and_launches_the_driver(db, home, launched, monkeypatch):
    monkeypatch.setattr(db, "_db_path", Path("/data/history.db"))
    t = tab(db)
    t._knobs["k_2t"].setValue(3)
    t._buttons["Start"].click()
    ((command, kwargs),) = launched
    written = home / "tuner" / "start-config.json"
    assert command == [
        sys.executable,
        "-m",
        "corecycler.tuner.live_hw",
        "--write",
        "--start",
        "--config",
        str(written),
        "--db",
        "/data/history.db",
    ]
    assert config_from_json(json.loads(written.read_text())) == t.config()
    assert kwargs["start_new_session"] is True and kwargs["stdin"] is tt.subprocess.DEVNULL
    assert kwargs["env"]["PYTHONPATH"].split(os.pathsep) == [path for path in sys.path if path]
    assert not t._buttons["Start"].isEnabled()
    assert (home / "tuner" / "driver.log").exists()


def test_a_missing_tool_that_is_not_supplied_launches_nothing(db, home, launched, monkeypatch, tmp_path):
    save_machine_session(db, tmp_path, "paused")
    asked = []
    monkeypatch.setattr(tt, "ensure_tool", lambda parent, key: asked.append(key) or False)
    t = tab(db)
    t._on_start()
    t._on_session("resume")
    assert launched == []
    assert asked == ["y-cruncher", "y-cruncher"]


@pytest.mark.parametrize(("button", "action"), [("Resume", "resume"), ("Validate", "validate")])
def test_a_session_action_names_the_session_in_view(db, home, launched, tmp_path, button, action):
    saved = save_machine_session(db, tmp_path, "paused" if action == "resume" else "completed")
    t = tab(db, reads={0: -20})
    t._buttons[button].click()
    assert launched[0][0][4:7] == [f"--{action}", "--session", str(saved.session_id)]
    assert "--acknowledge" not in launched[0][0]


def test_start_over_set_values_asks_first_and_names_them(db, home, launched):
    t = tab(db, reads={1: -20, 3: -10, 2: None})
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Cancel
    t._buttons["Start"].click()
    assert launched == []
    assert "C1 -20, C3 -10." in tt.QMessageBox.question.call_args.args[2]
    t._core_boxes[1].setChecked(False)
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t._buttons["Start"].click()
    assert "C3 -10." in tt.QMessageBox.question.call_args.args[2]
    assert launched[0][0][-3:] == ["--acknowledge", "--db", str(db.path)]


def test_start_over_zero_values_asks_nothing(db, home, launched):
    t = tab(db)
    t._buttons["Start"].click()
    tt.QMessageBox.question.assert_not_called()
    assert "--acknowledge" not in launched[0][0]


def test_restart_asks_about_the_session_s_own_cores(db, home, launched, tmp_path):
    saved = save_machine_session(db, tmp_path, "paused")
    db.insert_tuner_event(saved.session_id, "setup_changed: bios was A, is now B", this_boot(), "paused")
    core = saved.cores[0].core
    t = tab(db, reads={core: -5})
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Cancel
    t._buttons["Restart"].click()
    assert launched == []
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t._buttons["Restart"].click()
    assert f"C{core} -5." in tt.QMessageBox.question.call_args.args[2]
    assert launched[0][0][4:8] == ["--restart", "--session", str(saved.session_id), "--acknowledge"]


def test_restart_without_a_co_reader_leaves_the_question_to_the_driver(db, home, launched, tmp_path):
    saved = save_machine_session(db, tmp_path, "paused")
    db.insert_tuner_event(saved.session_id, "setup_changed: bios was A, is now B", this_boot(), "paused")
    t = tt.TunerTab(db, topology(), None, None)
    t._buttons["Restart"].click()
    tt.QMessageBox.question.assert_not_called()
    assert launched[0][0][4:] == ["--restart", "--session", str(saved.session_id), "--db", str(db.path)]


def test_the_driver_s_end_is_said_with_its_refusal(db, home, launched):
    t = tab(db)
    t._on_start()
    (home / "tuner" / "driver.log").write_text("10:00:00 session 1\n10:00:01 refused: the BIOS sets a CO\n\n")
    t._child.code = tt.EXIT_REFUSED
    t.refresh()
    assert (
        t._driver.text()
        == "the driver ended: refused (bad config or environment): 10:00:01 refused: the BIOS sets a CO"
    )
    assert t._child is None and t._buttons["Start"].isEnabled()
    t._on_start()
    t._child.code = 0
    t.refresh()
    assert t._driver.text() == "the driver ended: completed"


def test_an_unreadable_driver_output_is_said(db, home, launched):
    t = tab(db)
    t._on_start()
    (home / "tuner" / "driver.log").unlink()
    t._child.code = tt.EXIT_LOCKED
    t.refresh()
    assert "its output cannot be read: " in t._driver.text()


def test_a_manual_test_locks_start(db, home):
    t = tab(db)
    t.set_test_running(True)
    assert not t._buttons["Start"].isEnabled()
    t.set_test_running(False)
    assert t._buttons["Start"].isEnabled()


class TestAutoResume:
    def test_without_a_database_nothing_resumes(self, home, launched):
        t = tt.TunerTab(None, topology(), CO_RANGE, None)
        t.attempt_auto_resume()
        assert t._driver.text() == "auto-resume: no history database"
        assert launched == []

    @pytest.mark.parametrize("statuses", [[], ["paused"], ["running", "running"]])
    def test_only_exactly_one_interrupted_session_resumes(self, db, home, launched, tmp_path, statuses):
        for number, status in enumerate(statuses, start=1):
            save_machine_session(db, tmp_path, status, session_id=number)
        t = tab(db)
        t.attempt_auto_resume()
        running = statuses.count("running")
        assert t._driver.text() == f"auto-resume: {running} interrupted sessions; resume one by hand"
        assert launched == []

    def test_a_running_driver_or_test_blocks_it(self, db, home, launched, tmp_path):
        save_machine_session(db, tmp_path, "running")
        t = tab(db)
        with co_writer(record=True):
            t.attempt_auto_resume()
        assert t._driver.text() == "auto-resume: a tuner driver already runs"
        t.set_test_running(True)
        t.attempt_auto_resume()
        assert t._driver.text() == "auto-resume: another test is running"
        assert launched == []

    def test_an_interrupted_session_resumes_and_a_refusal_tries_again(self, db, home, launched, tmp_path, monkeypatch):
        saved = save_machine_session(db, tmp_path, "running")
        timers = []
        monkeypatch.setattr(tt.QTimer, "singleShot", lambda ms, action: timers.append((ms, action)))
        t = tab(db)
        t.attempt_auto_resume()
        assert launched[-1][0][4:7] == ["--resume", "--session", str(saved.session_id)]
        for tries_left in (2, 1):
            t._child.code = tt.EXIT_REFUSED
            t.refresh()
            assert timers[-1][0] == tt.AUTO_RESUME_GAP_MS
            timers[-1][1]()
            assert len(launched) == 4 - tries_left
        t._child.code = tt.EXIT_REFUSED
        t.refresh()
        assert len(timers) == 2
        assert t._driver.text().startswith("the driver ended: refused")

    def test_a_driver_that_runs_is_not_retried(self, db, home, launched, tmp_path, monkeypatch):
        save_machine_session(db, tmp_path, "running")
        timers = []
        monkeypatch.setattr(tt.QTimer, "singleShot", lambda ms, action: timers.append(ms))
        t = tab(db)
        t.attempt_auto_resume()
        t._child.code = EXIT_PAUSED
        t.refresh()
        assert timers == []


def test_the_real_driver_starts_from_the_tab_and_its_refusal_comes_back(db, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(tt, "QMessageBox", MagicMock())
    monkeypatch.setattr(tt, "ensure_tool", lambda parent, key: True)
    if os.geteuid() == 0:
        pytest.skip("the child's home follows SUDO_USER under root")
    data = tmp_path / "home" / ".local" / "share" / "corecycler"
    monkeypatch.setattr(tt, "data_dir", lambda: data)
    on_disk = HistoryDB(tmp_path / "h.db")
    t = tt.TunerTab(on_disk, topology(), CO_RANGE, None)
    t._launch(["--resume", "--session", "1"])
    deadline = time.monotonic() + 60
    while t._child is not None and time.monotonic() < deadline:
        time.sleep(0.1)
        t.refresh()
    on_disk.close()
    assert t._child is None
    assert t._driver.text().startswith("the driver ended: refused (bad config or environment): ")
