from __future__ import annotations

import json
import os
import re
import signal
import sys
import time
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

if not hasattr(sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("tuner tab tests require real PySide6", allow_module_level=True)

from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QLabel,
    QWidget,
)

from corecycler.cli import EXIT_PAUSED
from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.gui import tuner_tab as tt
from corecycler.gui.style import stage_label
from corecycler.gui.widgets import spoken
from corecycler.history.db import HistoryDB
from corecycler.monitor.frequency import PowerState
from corecycler.smu.writer_lock import co_writer
from corecycler.tuner.live import Identity, Store, config_from_json
from corecycler.tuner.machine.session import config_problem
from corecycler.tuner.machine.types import MachineConfig, Mode
from corecycler.tuner.orders import Method
from tests._machine import this_boot
from tests._tuner_world import UNREADABLE_ROWS, save_machine_session

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
    monkeypatch.setattr(tt, "sched_ext_note", lambda: None)
    return data


def topology(cores: int = 4) -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=cores, ccds=2)
    for core in range(cores):
        topo.cores[core] = PhysicalCore(core_id=core, ccd=core // 2, ccx=None, logical_cpus=(core,))
    return topo


def tab(db, co_range=CO_RANGE, reads=None) -> tt.TunerTab:
    return tt.TunerTab(db, topology(), co_range, lambda core: (reads or {}).get(core, 0))


@pytest.mark.req("TM.A4.orders")
@pytest.mark.parametrize(("ccd_of", "greyed"), [(lambda core: core // 2, False), (lambda core: 0, True)])
def test_the_ccd_order_is_greyed_with_its_reason_when_every_core_sits_on_one_ccd(db, ccd_of, greyed):
    topo = topology()
    for core, info in topo.cores.items():
        topo.cores[core] = replace(info, ccd=ccd_of(core))
    t = tt.TunerTab(db, topo, CO_RANGE, lambda core: 0)
    items = [t._order.model().item(index) for index in range(t._order.count())]
    alternate = items[t._order.findData(tt.Method.ALTERNATE.value)]
    assert alternate.isEnabled() is not greyed
    assert ("one" in alternate.toolTip()) is greyed
    assert all(item.isEnabled() for item in items if item is not alternate)
    assert t._order.currentData() == (tt.Method.SEQUENTIAL if greyed else tt.Method.ALTERNATE).value


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


@pytest.mark.req("D81", "UI1.shown")
def test_every_machine_knob_is_offered_once(db, home):
    t = tab(db)
    names = {f.name for f in fields(MachineConfig)}
    assert set(t._knobs) == names - set(tt.SET_BY_THE_TAB)
    assert set(t._stage_spins) == {load for load, _ in MachineConfig(Mode.CO, Method.SEQUENTIAL, ()).stage_seconds}
    assert set(tt.KNOB_TEXT[tt.SHOWN]) <= set(t._knobs)


@pytest.mark.req("UI1.knobs")
def test_a_setting_without_a_name_fails_the_tab(monkeypatch):
    texts = {group: dict(knobs) for group, knobs in tt.KNOB_TEXT.items()}
    del texts["Temperature"]["cool_min_s"]
    texts["Temperature"]["cool_minimum"] = ("x", "s", "x")
    monkeypatch.setattr(tt, "KNOB_TEXT", texts)
    with pytest.raises(AssertionError, match=r"disagree: \['cool_min_s', 'cool_minimum'\]"):
        tt._assert_every_knob_named()


@pytest.mark.req("WARM.rest")
def test_the_warm_setting_says_where_tctl_is_held_and_that_a_warmer_reading_before_the_load_moves_the_band():
    about = tt.KNOB_TEXT["All-core stages"]["warm"][2]
    assert f"Tctl is held between 55 and 65 {tt.DEGREES}" in about
    assert f"from 2 {tt.DEGREES} above the Tctl read before its load when that is warmer" in about


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
@pytest.mark.req("UI1.minutes")
def test_a_duration_is_also_said_in_minutes_and_hours(seconds, said):
    assert spoken(seconds) == said


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


@pytest.mark.req("T22.F2")
def test_the_power_line_is_read_now_at_every_refresh_not_taken_from_the_session(db, home, tmp_path, monkeypatch):
    read = {"profile": "balanced", "state": PowerState(None, "powersave", "balance_performance", "1", 5756.452)}
    monkeypatch.setattr(tt, "power_profile", lambda: read["profile"])
    monkeypatch.setattr(tt, "read_power_state", lambda: read["state"])
    save_machine_session(db, tmp_path, "paused")
    t = tab(db)
    t.refresh()
    assert t._range.text() == (
        "Power now: profile balanced, governor powersave, EPP balance_performance, boost on, max MHz 5756"
    )
    read["profile"], read["state"] = (
        None,
        PowerState(split=(("governor", "powersave on cpus 0-1; performance on cpus 2"),)),
    )
    t.refresh()
    assert t._range.text() == "Power now: governor powersave on cpus 0-1; performance on cpus 2"
    read["state"] = PowerState()
    t.refresh()
    assert t._range.text() == "Power now: unreadable"


@pytest.mark.req("T22.F22")
def test_a_held_session_s_settings_are_shown_from_its_record_and_locked_until_it_ends(db, home, tmp_path):
    t = tab(db)
    defaults = t.config()
    saved = save_machine_session(db, tmp_path, "paused")
    assert saved.config != defaults
    t.refresh()
    assert t.config() == saved.config
    assert t._settings.title() == f"Session {saved.session_id} settings"
    assert not t._knobs["max_offset"].isEnabled() and not t._order.isEnabled()
    assert t._advanced_toggle.isEnabled()
    save_machine_session(db, tmp_path, "completed", session_id=saved.session_id)
    t.refresh()
    assert t._settings.title() == "Settings"
    assert t._knobs["max_offset"].isEnabled() and t._order.isEnabled()


@pytest.mark.req("T22.F24")
def test_session_events_read_in_local_time(db, home, tmp_path):
    from corecycler.history.timefmt import format_local

    saved = save_machine_session(db, tmp_path, "paused")
    db.insert_tuner_event(saved.session_id, "user: paused by the owner", this_boot(), "paused")
    t = tab(db)
    t.refresh()
    stamps = [event["timestamp"] for event in t._view.events]
    lines = t._events.toPlainText().splitlines()
    assert lines[-1] == f"{format_local(stamps[-1])}  paused: user: paused by the owner"
    assert all(stamp not in t._events.toPlainText() for stamp in stamps)


def test_a_paused_session_shows_its_cores_and_offers_resume(db, home, tmp_path):
    t = tab(db)
    infos = []
    t.tuner_core_info.connect(lambda core, value, phase: infos.append((core, value, phase)))
    saved = save_machine_session(db, tmp_path, "paused")
    db.insert_tuner_event(saved.session_id, "user: paused by the owner", this_boot(), "paused")
    t.refresh()
    assert t._status.text() == f"Session {saved.session_id}: Paused. Asked by you: paused by the owner"
    assert t._cores.rowCount() == len(saved.cores)
    assert t._cores.item(0, 1).text() == str(saved.cores[0].value)
    assert infos == [(r.core, r.value, r.phase.value) for r in saved.cores]
    t.refresh()
    assert len(infos) == len(saved.cores)
    assert t._buttons["Resume"].isEnabled()
    assert not t._buttons["Validate"].isEnabled()
    assert not t._buttons["Continue"].isEnabled()
    assert t.waiting_session().session_id == saved.session_id
    assert "paused by the owner" in t._events.toPlainText()


@pytest.mark.req("E6.B2.controls")
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


@pytest.mark.req("UNREAD.shown")
@pytest.mark.parametrize(("machine_json", "error"), UNREADABLE_ROWS)
def test_an_unreadable_session_is_named(db, home, tmp_path, machine_json, error):
    saved = save_machine_session(db, tmp_path, "paused")
    db._execute_raw(f"UPDATE tuner_sessions SET machine_json = {machine_json} WHERE id = ?", (saved.session_id,))
    t = tab(db)
    assert (
        t._status.text() == f"Session {saved.session_id}: Paused; session {saved.session_id} could not be read: {error}"
    )
    assert t._cores.rowCount() == 0


READABLE_ABORT = (
    "Abort the session? Every owned core goes back to its baseline and the session stops as aborted;"
    " Resume can take it up again."
)


@pytest.mark.req("UNREAD.abort")
@pytest.mark.parametrize("unread", [False, True])
def test_the_abort_dialog_of_an_unreadable_session_names_it_and_says_what_it_does_to_the_co(
    db, home, launched, tmp_path, unread
):
    saved = save_machine_session(db, tmp_path, "running")
    if unread:
        db._execute_raw(
            f"UPDATE tuner_sessions SET machine_json = {UNREADABLE_ROWS[0][0]} WHERE id = ?", (saved.session_id,)
        )
    t = tab(db)
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t._buttons["Abort"].click()
    said = tt.QMessageBox.question.call_args.args[2]
    if unread:
        assert said.startswith(
            f"Abort the session? It stops as aborted unread, as session {saved.session_id} could not be read:"
            f" {UNREADABLE_ROWS[0][1]}, and stays in History as stored."
        )
        assert "CO journal" in said and "reboot or check the CO" in said and "Resume" not in said
        assert t._status.text().startswith(f"Session {saved.session_id}: Interrupted; session {saved.session_id}")
    else:
        assert said == READABLE_ABORT
    assert [command[4:7] for command, _ in launched] == [["--abort", "--session", str(saved.session_id)]]


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


@pytest.mark.req("T22.F9")
@pytest.mark.parametrize(
    ("stage", "load", "loaded", "said"),
    [
        ("steady", "hot", (0, 1), "cpus 0-1"),
        ("bisect", "1t", (1,), "cpus 1"),
        ("soak", "soak", (), "no cpus loaded"),
    ],
)
def test_a_test_of_several_cores_shows_its_cpus_and_values_and_marks_each_loaded_core(
    db, home, tmp_path, stage, load, loaded, said
):
    saved = save_machine_session(db, tmp_path, "running")
    first, second = (record.core for record in saved.cores)
    vector = json.dumps([[first, -3], [second, -4]])
    cpus = json.dumps(list(loaded))
    db.insert_tuner_test(saved.session_id, this_boot(), 1, stage, load, "1t", first, -3, vector, cpus, 510.0, "{}")
    t = tab(db)
    grid, elapsed = [], []
    t.tuner_core_testing.connect(lambda core, state: grid.append((core, state)))
    t.tuner_core_elapsed.connect(lambda core, s: elapsed.append(core))
    with co_writer(record=True):
        t.refresh()
        assert t._test.text().startswith(f"Testing {said}, cores {first} at -3, {second} at -4: {stage_label(stage)}, ")
        assert t._test.text().endswith(" of 510 s")
    assert sorted(core for core, state in grid if state == "testing") == list(loaded)
    assert sorted(elapsed) == list(loaded)


@pytest.mark.req("L14.c.display")
def test_an_endurance_test_line_counts_the_ended_endurance_tests_and_the_failed_ones(db, home, tmp_path):
    saved = save_machine_session(db, tmp_path, "running")
    core = saved.cores[0].core
    for token, verdict in enumerate(("pass", "silicon", "pass", "thermal"), start=1):
        row = db.insert_tuner_test(
            saved.session_id, this_boot(), token, "endurance", "hot", "1t", core, -3, "[]", "[0]", 60.0, "{}"
        )
        db.end_tuner_test(row, verdict, 60.0, "[]", None, "{}", "", "[]", "")
    db.insert_tuner_test(saved.session_id, this_boot(), 9, "steady", "hot", "1t", core, -3, "[]", "[0]", 60.0, "{}")
    t = tab(db)
    with co_writer(record=True):
        t.refresh()
        assert "endurance tests" not in t._test.text()
        db.insert_tuner_test(
            saved.session_id, this_boot(), 10, "endurance", "warm", "1t", core, -3, "[]", "[0]", 60.0, "{}"
        )
        t.refresh()
        assert t._test.text().endswith(" of 60 s; 4 endurance tests ended, 1 failed")


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


@pytest.mark.req("D79", "E6.B2.launch")
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


@pytest.mark.req("T23.abort_interrupted")
@pytest.mark.parametrize(
    ("status", "offered"), [("paused", True), ("running", True), ("completed", False), ("aborted", False)]
)
def test_abort_is_offered_for_a_held_session_without_a_driver_and_launches_one(
    db, home, launched, tmp_path, status, offered
):
    saved = save_machine_session(db, tmp_path, status)
    t = tab(db, reads={0: -20})
    assert t._buttons["Abort"].isEnabled() is offered
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t._buttons["Abort"].click()
    assert [command[4:7] for command, _ in launched] == (
        [["--abort", "--session", str(saved.session_id)]] if offered else []
    )


@pytest.mark.req("L01.15.list", "Q7", "UI1.bios")
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


@pytest.mark.req("L01.15.zero")
def test_start_over_zero_values_asks_nothing(db, home, launched):
    t = tab(db)
    t._buttons["Start"].click()
    tt.QMessageBox.question.assert_not_called()
    assert "--acknowledge" not in launched[0][0]


@pytest.mark.req("C14", "L01.15.changed")
def test_start_names_a_core_whose_co_differs_from_the_newest_session_s_base_even_at_0(db, home, launched, tmp_path):
    saved = save_machine_session(db, tmp_path, "completed")
    Store(db, tmp_path / "state", Identity("bios-1", "cpu-1", None)).save(
        replace(saved, base_co=((0, -10), (1, -4), (2, 0), (3, 0))), this_boot()
    )
    t = tab(db, reads={1: None})
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t._buttons["Start"].click()
    assert tt.QMessageBox.question.call_args.args[1:3] == (
        "Curve Optimizer values at Start",
        f"The CO differs from when session {saved.session_id} started: C0 -10 to 0.\n\nStart anyway?",
    )
    assert "--acknowledge" in launched[0][0]


@pytest.mark.req("C14")
def test_start_over_a_newest_session_that_cannot_be_read_asks_first_and_names_it(db, home, launched, tmp_path):
    saved = save_machine_session(db, tmp_path, "aborted")
    db._execute_raw(
        "UPDATE tuner_sessions SET machine_json = json_remove(machine_json, '$.base_co') WHERE id = ?",
        (saved.session_id,),
    )
    t = tab(db)
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Cancel
    t._buttons["Start"].click()
    assert launched == []
    assert tt.QMessageBox.question.call_args.args[1:3] == (
        "Curve Optimizer values at Start",
        f"The newest session {saved.session_id} could not be read: KeyError('base_co'), so whether the CO moved since "
        "it started cannot be judged.\n\nStart anyway?",
    )
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t._buttons["Start"].click()
    assert "--acknowledge" in launched[0][0]


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


@pytest.mark.req("INT.4")
def test_a_running_sched_ext_scheduler_is_asked_about_before_any_test_starts(db, home, launched, tmp_path, monkeypatch):
    monkeypatch.setattr(tt, "sched_ext_note", lambda: "sched_ext scheduler lavd is running; tests run under it")
    saved = save_machine_session(db, tmp_path, "paused")
    t = tab(db)
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Cancel
    t._buttons["Start"].click()
    t._buttons["Resume"].click()
    assert launched == []
    assert tt.QMessageBox.question.call_args.args[2] == (
        "sched_ext scheduler lavd is running; tests run under it\n\n"
        "For clean results stop it first: scxctl stop, or systemctl stop scx_loader.\n\nStart anyway?"
    )
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t._buttons["Resume"].click()
    assert launched[0][0][4:7] == ["--resume", "--session", str(saved.session_id)]


@pytest.mark.parametrize(
    ("stopped", "said"),
    [
        ([], ""),
        (
            ["ananicy-cpp"],
            "The session has ended and ananicy-cpp is enabled but stopped. Start it again with:"
            " sudo systemctl start ananicy-cpp.service",
        ),
        (
            ["ananicy-cpp", "scx_loader"],
            "The session has ended and ananicy-cpp and scx_loader are enabled but stopped. Start them again with:"
            " sudo systemctl start ananicy-cpp.service && sudo systemctl start scx_loader.service",
        ),
    ],
)
def test_a_session_that_ends_names_a_stopped_scheduling_service(db, home, tmp_path, monkeypatch, stopped, said):
    saved = save_machine_session(db, tmp_path, "running")
    asked = []
    monkeypatch.setattr(tt, "stopped_services", lambda: asked.append(1) or stopped)
    t = tab(db)
    t.refresh()
    assert asked == []
    db._execute_raw("UPDATE tuner_sessions SET status='completed' WHERE id=?", (saved.session_id,))
    t.refresh()
    t.refresh()
    assert asked == [1]
    assert t._reminder.text() == said


def test_a_failed_service_check_is_said(db, home, tmp_path, monkeypatch):
    saved = save_machine_session(db, tmp_path, "paused")

    def broken():
        raise tt.subprocess.TimeoutExpired("systemctl", 5)

    monkeypatch.setattr(tt, "stopped_services", broken)
    t = tab(db)
    db._execute_raw("UPDATE tuner_sessions SET status='aborted' WHERE id=?", (saved.session_id,))
    t.refresh()
    assert t._reminder.text().startswith("The scheduling services could not be checked: ")


def test_only_an_enabled_and_stopped_service_is_named(monkeypatch):
    calls = []
    state = {("is-enabled", "ananicy-cpp"): 0, ("is-active", "ananicy-cpp"): 3, ("is-enabled", "scx_loader"): 1}

    def run(command, check, timeout):
        verb, unit = command[1], command[3].removesuffix(".service")
        calls.append((verb, unit))
        return SimpleNamespace(returncode=state.get((verb, unit), 0))

    monkeypatch.setattr(tt.tools, "resolve", lambda key: SimpleNamespace(path=Path("/run/systemctl")))
    monkeypatch.setattr(tt.subprocess, "run", run)
    assert tt.stopped_services() == ["ananicy-cpp"]
    assert ("is-active", "scx_loader") not in calls
    monkeypatch.setattr(tt.tools, "resolve", lambda key: SimpleNamespace(path=None))
    assert tt.stopped_services() == []


@pytest.mark.req("E6.B2.follow")
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


@pytest.mark.req("G11.ui")
@pytest.mark.parametrize("code", [EXIT_PAUSED, 0])
def test_a_driver_run_that_logged_a_database_failure_raises_a_dialog_naming_it(db, home, launched, code):
    t = tab(db)
    t._on_start()
    (home / "tuner" / "driver.log").write_text(
        "10:00:00 session 1\n"
        "10:00:05 database_failed: disk I/O error; every owned core is sent back to stock\n"
        "10:00:06 paused\n"
    )
    t._child.code = code
    t.refresh()
    assert [call.args[1:] for call in tt.QMessageBox.critical.call_args_list] == [
        ("The history database failed", "disk I/O error; every owned core is sent back to stock")
    ]


def test_a_driver_run_with_no_database_failure_raises_no_dialog(db, home, launched):
    t = tab(db)
    t._on_start()
    (home / "tuner" / "driver.log").write_text("10:00:00 session 1\n10:00:06 paused: user: paused by the user\n")
    t._child.code = EXIT_PAUSED
    t.refresh()
    assert t._driver.text() == "the driver ended: paused (needs attention)"
    assert tt.QMessageBox.critical.call_args_list == []


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
    t._launch(["--abort", "--session", "1"])
    deadline = time.monotonic() + 60
    while t._child is not None and time.monotonic() < deadline:
        time.sleep(0.1)
        t.refresh()
    on_disk.close()
    assert t._child is None
    assert t._driver.text().startswith("the driver ended: refused (bad config or environment): ")


@pytest.mark.req("T25.start")
def test_the_settings_list_names_each_given_start():
    cfg = MachineConfig(Mode.CO, Method.SEQUENTIAL, (0, 1), starts=((0, -18), (1, -38)))
    assert "  Starting values: core 0 at -18, core 1 at -38" in tt.settings_lines(cfg)
    assert not any("Starting values" in line for line in tt.settings_lines(replace(cfg, starts=())))


SHOWN_FACTS = (
    (r"No tuning session", "no session in the database"),
    (r"Session \d+: .+", "the session record's status and its last pause reason"),
    (r"Power now: .+", "read_power_state and power_profile, read at this refresh"),
    (r"Testing .+: .+, \d+ of \d+ s", "the open test row, its seconds counted from its start"),
    (r"(asked driver \d+ to |started the driver: |no tuner driver is running).*", "the signal or launch just made"),
    (r"The session has ended and .+", "systemctl is-enabled and is-active, read when the session ended"),
    (r"Session \d+ settings|Settings", "the held session's record, else the form"),
    (r"Core|Tuner value|Phase|Failed at", "the session record's core, value, phase and fail bound"),
)


def shown_facts(t: tt.TunerTab) -> list[str]:
    labels = [label.text() for label in t.findChildren(QLabel) if label.text() and not t._settings.isAncestorOf(label)]
    heads = [t._cores.horizontalHeaderItem(column).text() for column in range(t._cores.columnCount())]
    return [*labels, *heads, t._settings.title()]


@pytest.mark.req("T22.contract")
@pytest.mark.parametrize("held", [None, "paused", "running"])
def test_every_fact_the_tuner_tab_shows_is_listed_with_its_source(db, home, tmp_path, monkeypatch, held):
    monkeypatch.setattr(
        tt, "read_power_state", lambda: PowerState(None, "powersave", "balance_performance", "1", 5756.0)
    )
    if held is not None:
        saved = save_machine_session(db, tmp_path, held)
        db.insert_tuner_event(saved.session_id, "user: paused by the owner", this_boot(), "paused")
        db.insert_tuner_test(
            saved.session_id, this_boot(), 1, "search", "1t", "1t", saved.cores[0].core, -3, "[]", "[0]", 60.0, "{}"
        )
    t = tab(db)
    with co_writer(record=held == "running"):
        t.refresh()
        texts = shown_facts(t)
    assert texts
    assert (held == "running") == any(text.startswith("Testing ") for text in texts)
    assert [text for text in texts if not any(re.fullmatch(pattern, text) for pattern, _ in SHOWN_FACTS)] == []


TUNER_CONTROLS = {
    "Start": "launches the driver with --start and the form's configuration",
    "Pause": "sends SIGUSR1 to the running driver",
    "Stop": "sends SIGTERM to the running driver",
    "Abort": "after a yes, sends SIGUSR2 to a running driver, else launches --abort for the session in view",
    "Resume": "launches --resume for the session in view",
    "Validate": "launches --validate for the session in view",
    "Continue": "launches --continue for the session in view",
    "Restart": "launches --restart for the session in view",
    "Advanced settings": "shows or hides the advanced settings",
    "QTableCornerButton": "selects every row of the core table",
}
SETUP_PAUSE = "setup_changed: bios was A, is now B"
LAUNCHES = {
    "Start": (None, None, "--start"),
    "Resume": ("paused", None, "--resume"),
    "Validate": ("completed", None, "--validate"),
    "Continue": ("paused", SETUP_PAUSE, "--continue"),
    "Restart": ("paused", SETUP_PAUSE, "--restart"),
    "Abort": ("paused", None, "--abort"),
}
SIGNALS = {"Pause": signal.SIGUSR1, "Stop": signal.SIGTERM, "Abort": signal.SIGUSR2}


@pytest.mark.req("T23.contract.tuner")
def test_every_button_of_the_tuner_tab_is_listed_with_its_reaction(db, home):
    t = tab(db)
    buttons = {
        button.text() or button.metaObject().className()
        for button in t.findChildren(QAbstractButton)
        if not isinstance(button, QCheckBox)
    }
    assert buttons == set(TUNER_CONTROLS)


@pytest.mark.req("T23.contract.tuner")
@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("name", sorted(LAUNCHES))
def test_a_launching_button_reacts_only_through_its_connection(db, home, launched, tmp_path, name, connected):
    status, event, flag = LAUNCHES[name]
    if status is not None:
        saved = save_machine_session(db, tmp_path, status)
        if event is not None:
            db.insert_tuner_event(saved.session_id, event, this_boot(), "paused")
    t = tab(db)
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    assert t._buttons[name].isEnabled()
    if not connected:
        t._buttons[name].clicked.disconnect()
    t._buttons[name].click()
    assert any(flag in command for command, _ in launched) is connected


@pytest.mark.req("T23.contract.tuner")
@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("name", sorted(SIGNALS))
def test_a_signalling_button_reacts_only_through_its_connection(db, home, tmp_path, monkeypatch, name, connected):
    saved = save_machine_session(db, tmp_path, "running")
    db.insert_tuner_test(
        saved.session_id, this_boot(), 1, "search", "1t", "1t", saved.cores[0].core, -3, "[]", "[0]", 60.0, "{}"
    )
    kills = []
    monkeypatch.setattr(tt.os, "kill", lambda pid, number: kills.append(number))
    tt.QMessageBox.question.return_value = tt.QMessageBox.StandardButton.Yes
    t = tab(db)
    with co_writer(record=True):
        t.refresh()
        assert t._buttons[name].isEnabled()
        if not connected:
            t._buttons[name].clicked.disconnect()
        t._buttons[name].click()
    assert (SIGNALS[name] in kills) is connected


@pytest.mark.req("T23.contract.tuner")
@pytest.mark.parametrize("connected", [True, False])
def test_the_advanced_toggle_reacts_only_through_its_connection(db, home, connected):
    t = tab(db)
    if not connected:
        t._advanced_toggle.toggled.disconnect()
    t._advanced_toggle.setChecked(True)
    assert (not t._advanced.isHidden()) is connected


@pytest.mark.req("T23.contract.tuner")
def test_every_settings_input_changes_the_configuration_start_sends(db, home):
    t = tab(db)
    inputs = [
        widget
        for widget in t._settings.findChildren(QWidget)
        if isinstance(widget, (QAbstractSpinBox, QComboBox, QCheckBox))
    ]
    assert inputs
    for widget in inputs:
        before = t.config()
        if isinstance(widget, QCheckBox):
            widget.setChecked(not widget.isChecked())
        elif isinstance(widget, QComboBox):
            widget.setCurrentIndex((widget.currentIndex() + 1) % widget.count())
        else:
            widget.stepBy(1 if widget.value() < widget.maximum() else -1)
        assert t.config() != before, widget


@pytest.mark.req("T22.F8")
@pytest.mark.parametrize("driver", [True, False])
def test_a_running_session_says_interrupted_once_its_driver_is_gone(db, home, tmp_path, driver):
    saved = save_machine_session(db, tmp_path, "running")
    t = tab(db)
    with co_writer(record=driver):
        t.refresh()
        said = t._status.text()
    assert said == f"Session {saved.session_id}: {'Running' if driver else 'Interrupted'}"


@pytest.mark.req("L18.b.quarantine")
@pytest.mark.parametrize(("answer", "flags"), [("Yes", ["--resume", "--reengage"]), ("No", None)])
def test_resume_re_engages_a_quarantined_session_only_after_a_yes(db, home, launched, tmp_path, answer, flags):
    save_machine_session(db, tmp_path, "quarantined")
    t = tab(db)
    assert t._buttons["Resume"].isEnabled()
    tt.QMessageBox.question.return_value = getattr(tt.QMessageBox.StandardButton, answer)
    t._buttons["Resume"].click()
    sent = [flag for command, _ in launched for flag in command if flag in ("--resume", "--reengage")]
    assert sent == (flags or [])


def make_unreadable(db, session_id: int) -> None:
    db._execute_raw(f"UPDATE tuner_sessions SET machine_json = {UNREADABLE_ROWS[0][0]} WHERE id = ?", (session_id,))


@pytest.mark.req("UNREAD.offered")
@pytest.mark.parametrize("unread", [False, True], ids=["readable", "unread"])
def test_after_an_abort_resume_is_offered_only_for_a_session_that_can_be_read(db, home, launched, tmp_path, unread):
    saved = save_machine_session(db, tmp_path, "paused")
    if unread:
        make_unreadable(db, saved.session_id)
    db.abort_held_session(saved.session_id)
    t = tab(db)
    refusal = f"; session {saved.session_id} could not be read: {UNREADABLE_ROWS[0][1]}" if unread else ""
    assert t._status.text() == f"Session {saved.session_id}: Aborted{refusal}"
    assert t._buttons["Resume"].isEnabled() is not unread
    t._buttons["Resume"].click()
    assert [command[4:7] for command, _ in launched] == (
        [] if unread else [["--resume", "--session", str(saved.session_id)]]
    )


@pytest.mark.req("UNREAD.offered")
@pytest.mark.parametrize("unread", [False, True], ids=["readable", "unread"])
@pytest.mark.parametrize(
    ("name", "status", "event"),
    [
        ("Resume", "paused", None),
        ("Resume", "running", None),
        ("Resume", "quarantined", None),
        ("Validate", "completed", None),
        ("Continue", "paused", SETUP_PAUSE),
        ("Restart", "paused", SETUP_PAUSE),
    ],
)
def test_a_session_action_the_driver_refuses_unread_is_offered_only_for_a_session_that_can_be_read(
    db, home, tmp_path, name, status, event, unread
):
    saved = save_machine_session(db, tmp_path, status)
    if event is not None:
        db.insert_tuner_event(saved.session_id, event, this_boot(), "paused")
    if unread:
        make_unreadable(db, saved.session_id)
    t = tab(db)
    assert t._buttons[name].isEnabled() is not unread
