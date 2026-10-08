"""Auto-Tuner tab: starts the tuner driver and follows its session in the history database."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from corecycler.cli import EXIT_LOCKED, EXIT_MEANINGS, EXIT_REFUSED
from corecycler.config.paths import atomic_write, data_dir, hand_back
from corecycler.engine import detector as kernel
from corecycler.gui.style import button_qss, theme
from corecycler.gui.tool_prompt import ensure_tool
from corecycler.smu.writer_lock import running_driver
from corecycler.tuner.live import config_to_json, read_stored
from corecycler.tuner.machine.types import KNOB_FLOOR, MachineConfig, Mode, PauseReason, Phase, Stored
from corecycler.tuner.orders import Method, default_method

if TYPE_CHECKING:
    from corecycler.engine.topology import CPUTopology
    from corecycler.history.db import HistoryDB

log = logging.getLogger(__name__)

CURATED = {
    "max_offset": "Max offset",
    "thermal_limit_c": "Thermal limit (C)",
    "memory": "Memory stage",
    "soak": "Soak stage",
    "endurance": "Endurance stage",
}
SET_BY_THE_TAB = ("mode", "method", "selected", "stage_seconds")
CEILING = 1_000_000_000
GRID_STATE = {
    Phase.NOT_STARTED: "pending",
    Phase.COARSE: "queued",
    Phase.FINE: "queued",
    Phase.PROVE: "queued",
    Phase.PROVEN: "passed",
}
AUTO_RESUME_TRIES = 3
AUTO_RESUME_GAP_MS = 60_000
TOOLS = ("y-cruncher", "stressapptest")


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: int
    status: str
    stored: Stored | None
    problem: str | None
    test: dict | None
    events: tuple[dict, ...]

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
    stored, problem = None, None
    try:
        stored = read_stored(db, session_id)
    except (TypeError, KeyError, ValueError) as e:
        problem = f"session {session_id} cannot be read: {e!r}"
    return SessionView(
        session_id,
        status,
        stored,
        problem,
        db.open_tuner_test(session_id, boot),
        tuple(db.get_tuner_events(session_id, limit=30)),
    )


class TunerTab(QWidget):
    tuner_running_changed = Signal(bool)
    tuner_core_testing = Signal(int, str)
    tuner_core_elapsed = Signal(int, float)
    tuner_core_info = Signal(int, int, str)

    def __init__(self, db: HistoryDB | None, topology: CPUTopology | None, co_range: tuple[int, int] | None) -> None:
        super().__init__()
        self._db = db
        self._topology = topology
        self._co_range = co_range
        self._child: subprocess.Popen | None = None
        self._child_auto = False
        self._auto_tries_left = 0
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
        layout.addLayout(self._build_live(), 2)

    def _build_settings(self) -> QWidget:
        group = QGroupBox("Settings")
        column = QVBoxLayout(group)
        cores = QGridLayout()
        domain_of = {}
        for index, (core, info) in enumerate(sorted(self._topology.cores.items()) if self._topology else ()):
            box = QCheckBox(f"C{core}")
            box.setChecked(True)
            self._core_boxes[core] = box
            domain_of[core] = info.ccd
            cores.addWidget(box, index // 8, index % 8)
        column.addWidget(QLabel("Cores"))
        column.addLayout(cores)
        form = QFormLayout()
        self._order = QComboBox()
        for method in Method:
            self._order.addItem(method.value, method.value)
        self._order.setCurrentText(default_method(tuple(self._core_boxes), domain_of).value)
        form.addRow("Order", self._order)
        defaults = {f.name: f.default for f in fields(MachineConfig) if f.name not in SET_BY_THE_TAB}
        for name, label in CURATED.items():
            form.addRow(label, self._knob(name, defaults[name]))
        self._acknowledge = QCheckBox("Start over a CO the BIOS already sets")
        form.addRow(self._acknowledge)
        column.addLayout(form)
        self._advanced_toggle = QToolButton()
        self._advanced_toggle.setText("Advanced")
        self._advanced_toggle.setCheckable(True)
        column.addWidget(self._advanced_toggle)
        self._advanced = QWidget()
        advanced = QFormLayout(self._advanced)
        for name, default in defaults.items():
            if name not in CURATED:
                advanced.addRow(name, self._knob(name, default))
        stage_default = next(f.default for f in fields(MachineConfig) if f.name == "stage_seconds")
        for load, seconds in stage_default:
            spin = QDoubleSpinBox()
            spin.setRange(1.0, CEILING)
            spin.setValue(seconds)
            self._stage_spins[load] = spin
            advanced.addRow(f"stage_seconds {load}", spin)
        self._advanced.setVisible(False)
        self._advanced_toggle.toggled.connect(self._advanced.setVisible)
        column.addWidget(self._advanced)
        column.addStretch(1)
        return group

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
            widget.setRange(KNOB_FLOOR[name], CEILING)
            widget.setValue(default)
        self._knobs[name] = widget
        return widget

    def _build_live(self) -> QVBoxLayout:
        column = QVBoxLayout()
        self._status = QLabel("No tuning session")
        self._driver = QLabel("")
        self._driver.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM};")
        self._test = QLabel("")
        column.addWidget(self._status)
        column.addWidget(self._driver)
        column.addWidget(self._test)
        buttons = QHBoxLayout()
        self._buttons: dict[str, QPushButton] = {}
        for name, action in (
            ("Start", self._on_start),
            ("Pause", lambda: self._signal(signal.SIGUSR1, "pause after the current test")),
            ("Stop", lambda: self._signal(signal.SIGTERM, "stop now; the session stays paused")),
            ("Abort", self._on_abort),
            ("Resume", lambda: self._on_session("resume")),
            ("Validate", lambda: self._on_session("validate")),
            ("Continue", lambda: self._on_session("continue")),
            ("Restart", lambda: self._on_session("restart")),
        ):
            button = QPushButton(name)
            button.clicked.connect(action)
            self._buttons[name] = button
            buttons.addWidget(button)
        self._buttons["Start"].setStyleSheet(button_qss(theme.BTN_GREEN))
        self._buttons["Abort"].setStyleSheet(button_qss(theme.BTN_RED))
        column.addLayout(buttons)
        self._cores = QTableWidget(0, 4)
        self._cores.setHorizontalHeaderLabels(["Core", "Value", "Phase", "Fail bound"])
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
            stage_seconds=tuple((load, spin.value()) for load, spin in self._stage_spins.items()),
            **values,
        )

    def set_test_running(self, running: bool) -> None:
        self._test_running = running
        self._enable_controls()

    def refresh(self) -> None:
        self._reap()
        running = self.is_running
        if running != self._running:
            self._running = running
            self.tuner_running_changed.emit(running)
        self._view = read_view(self._db, kernel.read_boot_id()) if self._db is not None else None
        self._show()
        self._enable_controls()

    def waiting_session(self) -> SessionView | None:
        view = self._view
        return view if view is not None and view.status in ("running", "paused") else None

    def _show(self) -> None:
        view = self._view
        if view is None:
            self._status.setText("No tuning session")
            return
        pause = [event for event in view.events if event["severity"] == "paused"]
        reason = f" ({pause[-1]['message']})" if view.status == "paused" and pause else ""
        self._status.setText(f"Session {view.session_id}: {view.status}{reason}")
        if view.problem is not None:
            self._status.setText(view.problem)
        self._show_test(view)
        self._show_cores(view)
        self._show_events(view)

    def _show_test(self, view: SessionView) -> None:
        test = view.test if self._running else None
        if test is None:
            self._test.setText("")
            return
        elapsed = (datetime.now(UTC) - datetime.fromisoformat(test["started_at"])).total_seconds()
        self._test.setText(
            f"Testing core {test['core']}: {test['stage']} {test['klass']} at {test['value']}, "
            f"{elapsed:.0f} of {test['seconds_asked']:.0f} s"
        )
        self.tuner_core_elapsed.emit(test["core"], elapsed)

    def _show_cores(self, view: SessionView) -> None:
        records = view.stored.cores if view.stored is not None else ()
        self._cores.setRowCount(len(records))
        testing = view.test["core"] if view.test is not None and self._running else None
        for row, record in enumerate(records):
            fail = "" if record.fb is None else str(record.fb)
            for column, text in enumerate((str(record.core), str(record.value), record.phase.value, fail)):
                self._cores.setItem(row, column, QTableWidgetItem(text))
            state = "testing" if record.core == testing else GRID_STATE[record.phase]
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
            "\n".join(f"{event['timestamp']}  {event['severity']}: {event['message']}" for event in view.events)
        )
        self._events.verticalScrollBar().setValue(self._events.verticalScrollBar().maximum())

    def _enable_controls(self) -> None:
        view = self._view
        idle = not self._running and not self._test_running and self._child is None
        status = view.status if view is not None else None
        self._buttons["Start"].setEnabled(idle and self._co_range is not None and bool(self._core_boxes))
        for name in ("Pause", "Stop", "Abort"):
            self._buttons[name].setEnabled(self._running)
        self._buttons["Resume"].setEnabled(idle and status in ("running", "paused", "aborted"))
        self._buttons["Validate"].setEnabled(idle and status == "completed")
        for name in ("Continue", "Restart"):
            self._buttons[name].setEnabled(idle and view is not None and view.setup_changed())

    def _note(self, text: str) -> None:
        log.info("tuner tab: %s", text)
        self._driver.setText(text)

    def _tools_ready(self) -> bool:
        return all(ensure_tool(self, key) for key in TOOLS)

    def _on_start(self) -> None:
        if not self._tools_ready():
            return
        state = data_dir() / "tuner"
        state.mkdir(parents=True, exist_ok=True)
        path = state / "start-config.json"
        atomic_write(path, json.dumps(config_to_json(self.config())))
        self._launch(["--start", "--config", str(path), *self._acknowledged()])

    def _acknowledged(self) -> list[str]:
        return ["--acknowledge"] if self._acknowledge.isChecked() else []

    def _on_session(self, action: str) -> None:
        if self._view is None or not self._tools_ready():
            return
        extra = self._acknowledged() if action == "restart" else []
        self._launch([f"--{action}", "--session", str(self._view.session_id), *extra])

    def _on_abort(self) -> None:
        answer = QMessageBox.question(
            self,
            "Abort Tuning",
            "Abort the session? Every owned core goes back to its baseline and the session stops as aborted;"
            " Resume can take it up again.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
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

    def _launch(self, args: list[str], auto: bool = False) -> None:
        state = data_dir() / "tuner"
        state.mkdir(parents=True, exist_ok=True)
        output = state / "driver.log"
        command = [sys.executable, "-m", "corecycler.tuner.live_hw", "--write", *args]
        if self._db is not None:
            command += ["--db", str(self._db.path)]
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)}
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
        self._child_auto = auto
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
        last = self._last_driver_line() if code in (EXIT_REFUSED, EXIT_LOCKED) else ""
        self._note(f"the driver ended: {meaning}" + (f": {last}" if last else ""))
        if self._child_auto and code in (EXIT_REFUSED, EXIT_LOCKED) and self._auto_tries_left > 0:
            self._note(f"auto-resume: trying again in {AUTO_RESUME_GAP_MS // 1000} s ({meaning})")
            QTimer.singleShot(AUTO_RESUME_GAP_MS, lambda: self.attempt_auto_resume(self._auto_tries_left))

    def _last_driver_line(self) -> str:
        try:
            lines = (data_dir() / "tuner" / "driver.log").read_text().splitlines()
        except OSError as e:
            return f"its output cannot be read: {e}"
        return next((line for line in reversed(lines) if line.strip()), "")

    def attempt_auto_resume(self, tries: int = AUTO_RESUME_TRIES) -> None:
        if self._db is None:
            self._note("auto-resume: no history database")
            return
        if self.is_running or self._child is not None:
            self._note("auto-resume: a tuner driver already runs")
            return
        interrupted = [s.id for s in self._db.list_machine_sessions() if s.status == "running"]
        if len(interrupted) != 1:
            self._note(f"auto-resume: {len(interrupted)} interrupted sessions; resume one by hand")
            return
        if self._test_running:
            self._note("auto-resume: another test is running")
            return
        self._auto_tries_left = tries - 1
        self._launch(["--resume", "--session", str(interrupted[0])], auto=True)
