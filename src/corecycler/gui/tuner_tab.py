"""Auto-Tuner tab: starts the tuner driver and follows its session in the history database."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from corecycler.cli import EXIT_LOCKED, EXIT_MEANINGS, EXIT_REFUSED
from corecycler.config import tools
from corecycler.config.paths import atomic_write, child_environment, data_dir, hand_back
from corecycler.engine import detector as kernel
from corecycler.gui.style import (
    SESSION_STATUS_LABELS,
    button_qss,
    pause_text,
    power_text,
    shown_status,
    stage_label,
    theme,
    tuner_phase_label,
)
from corecycler.gui.tool_prompt import ensure_tool
from corecycler.gui.widgets import add_setting, note, setting_grid, spoken
from corecycler.history.timefmt import format_local
from corecycler.monitor.frequency import cpu_ranges, read_power_state
from corecycler.monitor.power_profile import power_profile
from corecycler.smu.writer_lock import running_driver
from corecycler.tuner.live import ONE_CORE_STAGES, config_to_json, read_stored
from corecycler.tuner.live_hw import sched_ext_note
from corecycler.tuner.machine.session import co_changed_since
from corecycler.tuner.machine.types import (
    HELD_STATUSES,
    KNOB_FLOOR,
    MachineConfig,
    Mode,
    PauseReason,
    Phase,
    Stage,
    Stored,
)
from corecycler.tuner.orders import Method, default_method, order_problem
from corecycler.tuner.stages import REST_MARGIN_C, WARM_HOLD

if TYPE_CHECKING:
    from collections.abc import Callable

    from corecycler.engine.topology import CPUTopology
    from corecycler.history.db import HistoryDB

log = logging.getLogger(__name__)

DEGREES = "°C"
SHOWN = "Shown"
KNOB_TEXT: dict[str, dict[str, tuple[str, str, str]]] = {
    SHOWN: {
        "max_offset": (
            "Deepest offset to try",
            "counts",
            "The most negative Curve Optimizer value the search may reach. Each core settles at its own"
            " stable value on the way.",
        ),
        "thermal_limit_c": (
            "Temperature limit",
            DEGREES,
            "A test stops when the CPU reaches this, or its own maximum if that is lower, cools down and"
            " repeats the same value.",
        ),
        "memory": (
            "Memory test",
            "",
            "After the CPU stages, each core runs stressapptest. Skipped, with the reason, when memory is short.",
        ),
        "soak": ("Idle watch", "", "At the end the machine sits without load while the kernel log is watched."),
        "endurance": ("Endurance", "", "A long extra run of mixed all-core loads after the main stages."),
    },
    "Search, one core at a time": {
        "coarse_step": ("Coarse step", "counts", "How far each step of the first, quick search moves."),
        "fine_step": ("Fine step", "counts", "How far each step moves once the quick search has found the edge."),
        "k_1t": (
            "Passes to prove, one thread",
            "passes",
            "Clean tests a value needs with one thread busy before it counts as stable.",
        ),
        "k_2t": ("Passes to prove, two threads", "passes", "The same with both threads of the core busy."),
        "test_seconds_1t": ("Test length, one thread", "s", "How long each one-thread test runs."),
        "test_seconds_2t": ("Test length, two threads", "s", "How long each two-thread test runs."),
        "walk_budget_s": (
            "Search time limit per core",
            "s",
            "When a core has searched this long, the search stops and proves the best value found so far.",
        ),
        "wait_between_cores_s": (
            "Pause between cores",
            "s",
            "Rest before the next core's test, so the last test's heat does not carry over.",
        ),
    },
    "All-core stages": {
        "hot_seconds": ("Hot test length", "s", "Every core at once under the heaviest load."),
        "bisect_seconds": (
            "Narrowing test length",
            "s",
            "When the hot test reports a failure under steady heat without naming a core, groups of cores are"
            " tested this long to find it.",
        ),
        "burst": (
            "Burst segment",
            "s",
            "Groups of cores switch between load and rest every few seconds, which swings the voltage hardest.",
        ),
        "warm": (
            "Warm segment",
            "s",
            f"Every core runs each test in turn while Tctl is held between {WARM_HOLD[1]:.0f} and"
            f" {WARM_HOLD[0]:.0f} {DEGREES}, or from {REST_MARGIN_C:.0f} {DEGREES} above the Tctl read before its"
            " load when that is warmer, over the same span and below the hot loads' hold.",
        ),
        "idle": ("Idle segment", "s", "A few cores wake briefly between long rests, where the clocks boost highest."),
        "transient_window_s": (
            "Clean time needed, changing loads",
            "s",
            "Burst, warm and idle repeat until this much time passes without a failure. Tighten uses the same.",
        ),
        "endurance_window_s": (
            "Clean time needed, endurance",
            "s",
            "The endurance loads repeat until this much time passes without a failure.",
        ),
        "memory_seconds": ("Memory test length per core", "s", "How long stressapptest runs on each core."),
        "soak_seconds": ("Idle watch length", "s", "How long the machine sits without load at the end."),
    },
    "Temperature": {
        "cool_margin_c": (
            "Cool below the limit by",
            DEGREES,
            "After a heat stop, wait until the CPU is this far under the limit.",
        ),
        "cool_min_s": ("Shortest cool-down", "s", "Always rest at least this long after a heat stop."),
        "cool_cap_s": ("Longest cool-down", "s", "If the CPU is still too hot after this long, the session pauses."),
        "thermal_retries": (
            "Heat stops before pausing",
            "times",
            "Heat stops in a row on the same value before the session pauses.",
        ),
    },
    "When a test goes wrong": {
        "apparatus_retries": (
            "Retries for a test that cannot be judged",
            "times",
            "A test that stalls, does not start, or is killed by something else runs again this many times,"
            " then the session pauses.",
        ),
        "start_failure_threshold": (
            "Pause after first-value failures",
            "cores",
            "Pause when this many cores in a row fail the first value they try, a sign the setup is wrong."
            " 0 turns it off.",
        ),
        "stop_bound_s": (
            "Time allowed to stop a test",
            "s",
            "How long a test may take to end when asked before it is left behind as hung.",
        ),
    },
}
ORDER_LABELS = {
    Method.ALTERNATE: "Alternate across CCDs",
    Method.SEQUENTIAL: "One after another",
    Method.RANDOM: "Random",
}
THREADS = {"1t": "one thread", "2t": "two threads"}
SINGULAR = {"counts": "count", "passes": "pass", "times": "time", "cores": "core"}
SET_BY_THE_TAB = ("mode", "method", "selected", "stage_seconds", "starts")
STAGE_DEFAULT = next(f.default for f in fields(MachineConfig) if f.name == "stage_seconds")
STAGE_ORDER = tuple(load for load, _ in STAGE_DEFAULT)
CEILING = 1_000_000_000
DATABASE_FAILED = "database_failed: "
BOX_WIDTH = 110
CORES_PER_ROW = 4  # equivalent-mutant: five boxes a row are no wider than the setting rows, six are
GRID_STATE = {
    Phase.NOT_STARTED: "pending",
    Phase.COARSE: "queued",
    Phase.FINE: "queued",
    Phase.PROVE: "queued",
    Phase.PROVEN: "passed",
}
SCHEDULING_SERVICES = ("ananicy-cpp", "scx_loader", "scx")
ENDED = frozenset({"completed", "aborted", "quarantined"})
TOOLS = ("y-cruncher", "stressapptest")


def _assert_every_knob_named() -> None:
    named = [name for knobs in KNOB_TEXT.values() for name in knobs]
    offered = [f.name for f in fields(MachineConfig) if f.name not in SET_BY_THE_TAB] + list(STAGE_ORDER)
    if sorted(named) != sorted(offered):
        raise AssertionError(f"KNOB_TEXT and the settings disagree: {sorted(set(named) ^ set(offered))}")


_assert_every_knob_named()


def settings_lines(cfg: MachineConfig) -> list[str]:
    stage_seconds = dict(cfg.stage_seconds)
    lines = [f"  Cores to tune: {', '.join(map(str, cfg.selected))}", f"  Core order: {ORDER_LABELS[cfg.method]}"]
    if cfg.starts:
        lines.append(f"  Starting values: {', '.join(f'core {core} at {value}' for core, value in cfg.starts)}")
    for knobs in KNOB_TEXT.values():
        for name, (label, unit, _) in knobs.items():
            value = stage_seconds[name] if name in STAGE_ORDER else getattr(cfg, name)
            if isinstance(value, bool):
                lines.append(f"  {label}: {'on' if value else 'off'}")
                continue
            said = spoken(value) if unit == "s" else ""
            shown = SINGULAR.get(unit, unit) if value == 1 else unit
            lines.append(f"  {label}: {value:g} {shown}".rstrip() + (f" ({said})" if said else ""))
    return lines


def scope_text(test: dict) -> str:
    cpus = json.loads(test["cpus_json"])
    vector = json.loads(test["vector_json"])
    load = f"cpus {cpu_ranges(cpus)}" if cpus else "no cpus loaded"
    values = ", ".join(f"{core} at {value}" for core, value in vector) if vector else "none recorded"
    return f"{load}, cores {values}"


def power_now() -> tuple[tuple[str, str], ...]:
    profile = power_profile()
    return ((("power profile", profile),) if profile is not None else ()) + read_power_state().named()


def stopped_services() -> list[str]:
    systemctl = tools.resolve("systemctl").path
    if systemctl is None:
        return []

    def answers(verb: str, unit: str) -> bool:
        command = [str(systemctl), verb, "--quiet", f"{unit}.service"]
        return subprocess.run(command, check=False, timeout=5).returncode == 0

    return [unit for unit in SCHEDULING_SERVICES if answers("is-enabled", unit) and not answers("is-active", unit)]


def labelled(text: str) -> str:
    for knobs in KNOB_TEXT.values():
        for name, (label, _, _) in knobs.items():
            text = re.sub(rf"\b{name}\b", f"'{label}'", text)
    return text


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: int
    status: str
    stored: Stored | None
    problem: str | None
    test: dict | None
    events: tuple[dict, ...]
    endurance: tuple[int, int] | None = None

    def setup_changed(self) -> bool:
        paused = [event for event in self.events if event["severity"] == "paused"]
        return (
            self.status == "paused"
            and bool(paused)
            and paused[-1]["message"].startswith(f"{PauseReason.SETUP_CHANGED.value}:")
        )


def read_view(db: HistoryDB, boot: str) -> SessionView | None:
    newest = db.list_machine_sessions(limit=1)
    if not newest:
        return None
    session_id, status = newest[0].id, newest[0].status
    stored, problem = read_stored(db, session_id)
    test = db.open_tuner_test(session_id, boot)
    enduring = test is not None and test["stage"] == Stage.ENDURANCE
    return SessionView(
        session_id,
        status,
        stored,
        problem,
        test,
        tuple(db.get_tuner_events(session_id, limit=30)),
        db.endurance_tally(session_id) if enduring else None,
    )


class TunerTab(QWidget):
    tuner_running_changed = Signal(bool)
    tuner_core_testing = Signal(int, str)
    tuner_core_elapsed = Signal(int, float)
    tuner_core_info = Signal(int, int, str)

    def __init__(
        self,
        db: HistoryDB | None,
        topology: CPUTopology | None,
        co_range: tuple[int, int] | None,
        read_co: Callable[[int], int | None] | None,
    ) -> None:
        super().__init__()
        self._db = db
        self._topology = topology
        self._co_range = co_range
        self._read_co = read_co
        self._child: subprocess.Popen | None = None
        self._running = False
        self._test_running = False
        self._view: SessionView | None = None
        self._shown: dict[int, tuple[str, int, str]] = {}
        self._newest_event: int | None = None
        self._knobs: dict[str, QWidget] = {}
        self._stage_spins: dict[str, QDoubleSpinBox] = {}
        self._core_boxes: dict[int, QCheckBox] = {}
        self._build()
        self.refresh()

    @property
    def is_running(self) -> bool:
        return running_driver() is not None

    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.addWidget(self._build_settings(), 1)
        layout.addLayout(self._build_live(), 1)

    def _build_settings(self) -> QWidget:
        group = QGroupBox("Settings")
        self._settings = group
        column = QVBoxLayout(group)
        cores = QGridLayout()
        domain_of = {}
        for index, (core, info) in enumerate(sorted(self._topology.cores.items()) if self._topology else ()):
            box = QCheckBox(f"C{core}")
            box.setChecked(True)
            self._core_boxes[core] = box
            domain_of[core] = info.ccd
            cores.addWidget(box, index // CORES_PER_ROW, index % CORES_PER_ROW)
        column.addWidget(QLabel("Cores to tune"))
        column.addLayout(cores)
        column.addWidget(note("An unticked core keeps its value and is not tested."))
        shown = setting_grid()
        self._order = QComboBox()
        for method in Method:
            self._order.addItem(ORDER_LABELS[method], method.value)
            problem = order_problem(method, tuple(self._core_boxes), domain_of)
            if problem is not None:
                item = self._order.model().item(self._order.count() - 1)
                item.setEnabled(False)
                item.setToolTip(problem)
        self._order.setCurrentIndex(self._order.findData(default_method(tuple(self._core_boxes), domain_of).value))
        add_setting(
            shown,
            0,
            "Core order",
            self._order,
            "",
            "Which core is tested next. Alternating across CCDs lets one cool while the other works.",
        )
        defaults = {f.name: f.default for f in fields(MachineConfig) if f.name not in SET_BY_THE_TAB}
        defaults |= STAGE_DEFAULT
        for row, (name, (label, unit, about)) in enumerate(KNOB_TEXT[SHOWN].items(), start=1):
            add_setting(shown, row, label, self._knob(name, defaults[name]), unit, about)
        column.addLayout(shown)
        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setText("Advanced settings")
        self._advanced_toggle.setCheckable(True)
        column.addWidget(self._advanced_toggle)
        self._advanced = QWidget()
        advanced = QVBoxLayout(self._advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        for title, knobs in KNOB_TEXT.items():
            if title == SHOWN:
                continue
            box = QGroupBox(title)
            rows = setting_grid()
            for row, (name, (label, unit, about)) in enumerate(knobs.items()):
                add_setting(rows, row, label, self._knob(name, defaults[name]), unit, about)
            box.setLayout(rows)
            advanced.addWidget(box)
        column.addWidget(self._advanced)
        column.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumWidth(group.minimumSizeHint().width() + scroll.verticalScrollBar().sizeHint().width())
        scroll.setWidget(group)
        self._advanced.setVisible(False)
        self._advanced_toggle.toggled.connect(self._advanced.setVisible)
        return scroll

    def _knob(self, name: str, default) -> QWidget:
        if isinstance(default, bool):
            widget = QCheckBox()
            widget.setChecked(default)
        elif isinstance(default, int):
            widget = QSpinBox()
            if name == "max_offset":
                low = self._co_range[0] if self._co_range is not None else default
                widget.setRange(low, -1)
                widget.setValue(max(default, low))
            else:
                widget.setRange(int(KNOB_FLOOR[name]), CEILING)
                widget.setValue(default)
        else:
            widget = QDoubleSpinBox()
            widget.setDecimals(0)
            widget.setRange(1.0 if name in STAGE_ORDER else KNOB_FLOOR[name], CEILING)
            widget.setValue(default)
        if not isinstance(widget, QCheckBox):
            widget.setFixedWidth(BOX_WIDTH)
        if name in STAGE_ORDER:
            self._stage_spins[name] = widget
        else:
            self._knobs[name] = widget
        return widget

    def _build_live(self) -> QVBoxLayout:
        column = QVBoxLayout()
        self._status = QLabel("No tuning session")
        self._range = QLabel("")
        self._range.setWordWrap(True)
        self._driver = QLabel("")
        self._driver.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM};")
        self._test = QLabel("")
        self._test.setWordWrap(True)
        self._reminder = QLabel("")
        self._reminder.setWordWrap(True)
        column.addWidget(self._status)
        column.addWidget(self._range)
        column.addWidget(self._driver)
        column.addWidget(self._test)
        column.addWidget(self._reminder)
        buttons = QGridLayout()
        self._buttons: dict[str, QPushButton] = {}
        for name, action in (
            ("Start", self._on_start),
            ("Pause", lambda: self._signal(signal.SIGUSR1, "pause: a one-core test finishes, an all-core test stops")),
            ("Stop", lambda: self._signal(signal.SIGTERM, "stop now; the session stays paused")),
            ("Abort", self._on_abort),
            ("Resume", lambda: self._on_session("resume")),
            ("Validate", lambda: self._on_session("validate")),
            ("Continue", lambda: self._on_session("continue")),
            ("Restart", lambda: self._on_session("restart")),
        ):
            button = QPushButton(name)
            button.clicked.connect(action)
            buttons.addWidget(button, *divmod(len(self._buttons), 4))
            self._buttons[name] = button
        self._buttons["Start"].setStyleSheet(button_qss(theme.BTN_GREEN))
        self._buttons["Abort"].setStyleSheet(button_qss(theme.BTN_RED))
        column.addLayout(buttons)
        self._cores = QTableWidget(0, 4)
        self._cores.setHorizontalHeaderLabels(["Core", "Tuner value", "Phase", "Failed at"])
        self._cores.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._cores.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        column.addWidget(self._cores, 1)
        self._events = QPlainTextEdit()
        self._events.setReadOnly(True)
        column.addWidget(self._events, 1)
        return column

    def config(self) -> MachineConfig:
        values = {
            name: widget.isChecked() if isinstance(widget, QCheckBox) else widget.value()
            for name, widget in self._knobs.items()
        }
        return MachineConfig(
            Mode.CO,
            Method(self._order.currentData()),
            tuple(core for core, box in self._core_boxes.items() if box.isChecked()),
            stage_seconds=tuple((load, self._stage_spins[load].value()) for load in STAGE_ORDER),
            **values,
        )

    def _show_settings(self, view: SessionView | None) -> None:
        held = view.stored if view is not None and view.status in HELD_STATUSES else None
        if held is not None:
            cfg = held.config
            for name, widget in self._knobs.items():
                if isinstance(widget, QCheckBox):
                    widget.setChecked(getattr(cfg, name))
                else:
                    widget.setValue(getattr(cfg, name))
            for load, seconds in cfg.stage_seconds:
                self._stage_spins[load].setValue(seconds)
            self._order.setCurrentIndex(self._order.findData(cfg.method.value))
            for core, box in self._core_boxes.items():
                box.setChecked(core in cfg.selected)
        self._settings.setTitle("Settings" if held is None else f"Session {held.session_id} settings")
        for widget in (*self._knobs.values(), *self._stage_spins.values(), self._order, *self._core_boxes.values()):
            widget.setEnabled(held is None)

    def set_test_running(self, running: bool) -> None:
        self._test_running = running
        self._enable_controls()

    def refresh(self) -> None:
        self._reap()
        running = self.is_running
        if running != self._running:
            self._running = running
            self.tuner_running_changed.emit(running)
        seen = self._view
        self._view = read_view(self._db, kernel.read_boot_id()) if self._db is not None else None
        self._show_settings(self._view)
        if seen is not None and self._view is not None and seen.status not in ENDED and self._view.status in ENDED:
            self._remind()
        self._range.setText("Power now: " + (power_text(power_now()) or "unreadable"))
        self._show()
        self._enable_controls()

    def _remind(self) -> None:
        try:
            stopped = stopped_services()
        except (OSError, subprocess.SubprocessError) as e:
            self._reminder.setText(f"The scheduling services could not be checked: {e}")
            return
        commands = " && ".join(f"sudo systemctl start {unit}.service" for unit in stopped)
        self._reminder.setText(
            f"The session has ended and {' and '.join(stopped)} {'is' if len(stopped) == 1 else 'are'} enabled"
            f" but stopped. Start {'it' if len(stopped) == 1 else 'them'} again with: {commands}"
            if stopped
            else ""
        )

    def waiting_session(self) -> SessionView | None:
        view = self._view
        return view if view is not None and view.status in HELD_STATUSES else None

    def _show(self) -> None:
        view = self._view
        if view is None:
            self._status.setText("No tuning session")
            return
        pause = [event for event in view.events if event["severity"] == "paused"]
        reason = f". {pause_text(pause[-1]['message'])}" if view.status == "paused" and pause else ""
        shown = shown_status(view.status, self._running)
        status = SESSION_STATUS_LABELS.get(shown, shown)
        unread = "" if view.problem is None else f"; {view.problem}"
        self._status.setText(f"Session {view.session_id}: {status}{reason}{unread}")
        self._show_test(view)
        self._show_cores(view)
        self._show_events(view)

    def _show_test(self, view: SessionView) -> None:
        test = view.test if self._running else None
        if test is None:
            self._test.setText("")
            return
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(test["started_at"])).total_seconds()
        if test["stage"] in ONE_CORE_STAGES:
            what = f"core {test['core']} at {test['value']}, {THREADS.get(test['klass'], test['klass'])}"
        else:
            what = scope_text(test)
        tally = "" if view.endurance is None else "; {} endurance tests ended, {} failed".format(*view.endurance)
        self._test.setText(
            f"Testing {what}: {stage_label(test['stage'])}, {elapsed:.0f} of {test['seconds_asked']:.0f} s{tally}"
        )
        for core in self._loaded_cores(test):
            self.tuner_core_elapsed.emit(core, elapsed)

    def _loaded_cores(self, test: dict) -> tuple[int, ...]:
        if test["stage"] in ONE_CORE_STAGES:
            return (test["core"],)
        cpus = set(json.loads(test["cpus_json"]))
        cores = sorted(self._topology.cores.items()) if self._topology is not None else ()
        return tuple(core for core, info in cores if cpus.intersection(info.logical_cpus))

    def _show_cores(self, view: SessionView) -> None:
        records = view.stored.cores if view.stored is not None else ()
        self._cores.setRowCount(len(records))
        testing = self._loaded_cores(view.test) if view.test is not None and self._running else ()
        for row, record in enumerate(records):
            fail = "" if record.fb is None else str(record.fb)
            texts = (str(record.core), str(record.value), tuner_phase_label(record.phase), fail)
            for column, text in enumerate(texts):
                self._cores.setItem(row, column, QTableWidgetItem(text))
            state = "testing" if record.core in testing else GRID_STATE[record.phase]
            shown = (state, record.value, record.phase.value)
            if self._shown.get(record.core) != shown:
                self._shown[record.core] = shown
                self.tuner_core_testing.emit(record.core, state)
                self.tuner_core_info.emit(record.core, record.value, record.phase.value)

    def _show_events(self, view: SessionView) -> None:
        newest = view.events[-1]["id"] if view.events else None
        if newest == self._newest_event:
            return
        self._newest_event = newest
        self._events.setPlainText(
            "\n".join(
                f"{format_local(event['timestamp'])}  {event['severity']}: {event['message']}" for event in view.events
            )
        )
        self._events.verticalScrollBar().setValue(self._events.verticalScrollBar().maximum())

    def _enable_controls(self) -> None:
        view = self._view
        idle = not self._running and not self._test_running and self._child is None
        status = view.status if view is not None else None
        readable = view is not None and view.stored is not None
        self._buttons["Start"].setEnabled(idle and self._co_range is not None and bool(self._core_boxes))
        for name in ("Pause", "Stop"):
            self._buttons[name].setEnabled(self._running)
        self._buttons["Abort"].setEnabled(self._running or (idle and status in ("running", "paused")))
        self._buttons["Resume"].setEnabled(
            idle and readable and status in ("running", "paused", "aborted", "quarantined")
        )
        self._buttons["Validate"].setEnabled(idle and readable and status == "completed")
        for name in ("Continue", "Restart"):
            self._buttons[name].setEnabled(idle and readable and view.setup_changed())

    def _note(self, text: str) -> None:
        log.info("tuner tab: %s", text)
        self._driver.setText(text)

    def _tools_ready(self) -> bool:
        return all(ensure_tool(self, key) for key in TOOLS)

    def _scheduler_allows(self) -> bool:
        problem = sched_ext_note()
        if problem is None:
            return True
        answer = QMessageBox.question(
            self,
            "Another scheduler is running",
            f"{problem}\n\nFor clean results stop it first: scxctl stop, or systemctl stop scx_loader."
            "\n\nStart anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_start(self) -> None:
        if not self._tools_ready() or not self._scheduler_allows():
            return
        state = data_dir() / "tuner"
        state.mkdir(parents=True, exist_ok=True)
        path = state / "start-config.json"
        config = self.config()
        over = self._over_set_values(config.selected)
        if over is None:
            return
        atomic_write(path, json.dumps(config_to_json(config)))
        self._launch(["--start", "--config", str(path), *over])

    def _over_set_values(self, cores: tuple[int, ...]) -> list[str] | None:
        if self._read_co is None:
            return []
        values = {core: self._read_co(core) for core in cores}
        already = ", ".join(f"C{core} {value}" for core, value in sorted(values.items()) if value not in (0, None))
        newest = self._view
        changed = (
            [] if newest is None or newest.stored is None else co_changed_since(newest.stored.base_co, values, cores)
        )
        unread = None if newest is None else newest.problem
        if not already and not changed and unread is None:
            return []
        said = []
        if already:
            said.append(
                f"These cores already run a Curve Optimizer value, set by the BIOS or written since boot: {already}."
                "\n\nThe tuner searches each core from 0, or from where the newest completed session with this setup"
                " and power state ended, and replaces these values while it runs. A reboot brings the BIOS values back."
            )
        if changed:
            said.append(f"The CO differs from when session {newest.session_id} started: {', '.join(changed)}.")
        if unread is not None:
            said.append(f"The newest {unread}, so whether the CO moved since it started cannot be judged.")
        answer = QMessageBox.question(
            self,
            "Curve Optimizer values at Start",
            "\n\n".join([*said, "Start anyway?"]),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return ["--acknowledge"] if answer == QMessageBox.StandardButton.Yes else None

    def _on_session(self, action: str) -> None:
        view = self._view
        if view is None or not self._tools_ready() or not self._scheduler_allows():
            return
        extra: list[str] | None = []
        if action == "restart":
            extra = self._over_set_values(view.stored.config.selected)
        if action == "resume" and view.status == "quarantined":
            extra = self._reengage(view.session_id)
        if extra is None:
            return
        self._launch([f"--{action}", "--session", str(view.session_id), *extra])

    def _reengage(self, session_id: int) -> list[str] | None:
        answer = QMessageBox.question(
            self,
            "Re-engage Tuning",
            f"Session {session_id} is quarantined: it crashed three times in a row with no test surviving."
            " Re-engage it on proven ground? Each core starts again from its last proven value, or from stock"
            " where none is proven.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return ["--reengage"] if answer == QMessageBox.StandardButton.Yes else None

    def _on_abort(self) -> None:
        view = self._view
        said = (
            "Abort the session? Every owned core goes back to its baseline and the session stops as aborted;"
            " Resume can take it up again."
        )
        if view is not None and view.stored is None:
            said = (
                f"Abort the session? It stops as aborted unread, as {view.problem}, and stays in History as stored."
                " Every core its CO journal shows it wrote in this boot goes back to stock; when the journal cannot"
                " be read or another session wrote after it, no CO is written: reboot or check the CO before tuning"
                " again."
            )
        answer = QMessageBox.question(
            self,
            "Abort Tuning",
            said,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if running_driver() is None and self._view is not None:
            self._launch(["--abort", "--session", str(self._view.session_id)])
        else:
            self._signal(signal.SIGUSR2, "abort")

    def _signal(self, number: signal.Signals, meaning: str) -> None:
        pid = running_driver()
        if pid is None:
            self._note("no tuner driver is running")
            return
        try:
            os.kill(pid, number)
        except OSError as e:
            self._note(f"could not ask driver {pid} to {meaning}: {e}")
            return
        self._note(f"asked driver {pid} to {meaning}")

    def _launch(self, args: list[str]) -> None:
        state = data_dir() / "tuner"
        state.mkdir(parents=True, exist_ok=True)
        output = state / "driver.log"
        command = [sys.executable, "-m", "corecycler.tuner.live_hw", "--write", *args]
        if self._db is not None:
            command += ["--db", str(self._db.path)]
        environment = child_environment()
        with output.open("w") as out:
            self._child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
        hand_back(output)
        self._note(f"started the driver: {' '.join(args)}")
        self._enable_controls()

    def _reap(self) -> None:
        if self._child is None:
            return
        code = self._child.poll()
        if code is None:
            return
        self._child = None
        meaning = EXIT_MEANINGS.get(code, f"exit {code}")
        lines = self._driver_output()
        last = (
            next((line for line in reversed(lines) if line.strip()), "") if code in (EXIT_REFUSED, EXIT_LOCKED) else ""
        )
        self._note(f"the driver ended: {meaning}" + (f": {labelled(last)}" if last else ""))
        failure = next((line.partition(DATABASE_FAILED)[2] for line in reversed(lines) if DATABASE_FAILED in line), "")
        if failure:
            QMessageBox.critical(self, "The history database failed", failure)

    def _driver_output(self) -> list[str]:
        try:
            return (data_dir() / "tuner" / "driver.log").read_text().splitlines()
        except OSError as e:
            return [f"its output cannot be read: {e}"]
