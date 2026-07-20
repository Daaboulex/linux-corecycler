"""Display standards — the one source for colors, labels, fonts and value formats."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtGui import QFont

from corecycler.tuner.state import TunerPhase

COLOR_PASS = "#4caf50"
COLOR_FAIL = "#f44336"
COLOR_WARN = "#ffa726"
COLOR_WARN_SOFT = "#ffb74d"
COLOR_ORANGE = "#ff9800"
COLOR_ACTIVE = "#4fc3f7"
COLOR_MUTED = "#888888"
COLOR_MUTED_DARK = "#666666"
COLOR_MUTED_DARKER = "#555555"
COLOR_TEXT_DIM = "#aaaaaa"

CHART_FREQ = COLOR_ACTIVE
CHART_TEMP = "#ff7043"
CHART_POWER = "#66bb6a"
CHART_VOLT = "#ab47bc"

BG_DEFAULT = "#2d2d2d"
BG_TESTING = "#1a3a5c"
BG_PASS = "#1b3a1b"
BG_FAIL = "#3a1b1b"
BG_WARN = "#3a3a1b"
BG_BACKOFF = "#3a2a1a"
BG_MEM_STRESS = "#2a1a2e"
BORDER_DIM = "#444444"

COLOR_PASS_DARK = "#2e7d32"
COLOR_FAIL_DARK = "#c62828"
COLOR_BLUE_DEEP = "#2196f3"
COLOR_TEXT_BRIGHT = "#cccccc"
BG_PANEL = "#2a2a2a"
BG_PANEL_DARK = "#1e1e1e"
BG_ACTIVE_TINT = "#1a2a1a"
BORDER_DARKER = "#333333"
BTN_GREEN = "#1b5e20"
BTN_RED = "#b71c1c"
BG_SELECTED = BG_TESTING

ABSENT = "-"
PENDING_VALUE = "--"
NOT_AVAILABLE = "N/A"


def font_mono(size: int = 9, bold: bool = False) -> QFont:
    return QFont("monospace", size, QFont.Weight.Bold if bold else QFont.Weight.Normal)


def button_qss(bg: str) -> str:
    return (
        f"QPushButton {{ background: {bg}; color: white; padding: 6px 14px; "
        f"border-radius: 4px; font-weight: bold; }}"
        f"QPushButton:disabled {{ background: {COLOR_MUTED_DARKER}; "
        f"color: {COLOR_MUTED}; }}"
    )


def scheduler_phase_label(phase: str) -> str:
    return phase.capitalize() if phase else ""


PHASE_LABELS: dict[TunerPhase, str] = {
    TunerPhase.NOT_STARTED: "Not started",
    TunerPhase.COARSE_SEARCH: "Coarse search",
    TunerPhase.FINE_SEARCH: "Fine search",
    TunerPhase.SETTLED: "Settled",
    TunerPhase.CONFIRMING: "Confirming",
    TunerPhase.CONFIRMED: "Confirmed",
    TunerPhase.FAILED_CONFIRM: "Failed confirm",
    TunerPhase.BACKOFF_PRECONFIRM: "Backing off",
    TunerPhase.BACKOFF_CONFIRMING: "Backoff confirm",
    TunerPhase.HARDENING_T1: "Hardening 1",
    TunerPhase.HARDENING_T2: "Hardening 2",
    TunerPhase.HARDENED: "Hardened",
}

PHASE_TO_GRID: dict[TunerPhase, str] = {
    TunerPhase.NOT_STARTED: "pending",
    TunerPhase.COARSE_SEARCH: "queued",
    TunerPhase.FINE_SEARCH: "queued",
    TunerPhase.SETTLED: "queued",
    TunerPhase.CONFIRMING: "queued",
    TunerPhase.CONFIRMED: "passed",
    TunerPhase.FAILED_CONFIRM: "backoff",
    TunerPhase.BACKOFF_PRECONFIRM: "backoff",
    TunerPhase.BACKOFF_CONFIRMING: "backoff",
    TunerPhase.HARDENING_T1: "queued",
    TunerPhase.HARDENING_T2: "queued",
    TunerPhase.HARDENED: "passed",
}

PHASE_COLORS: dict[TunerPhase, str] = {
    TunerPhase.NOT_STARTED: COLOR_MUTED_DARK,
    TunerPhase.COARSE_SEARCH: "#b4b432",
    TunerPhase.FINE_SEARCH: "#c8c832",
    TunerPhase.SETTLED: "#c89632",
    TunerPhase.CONFIRMING: "#3296c8",
    TunerPhase.CONFIRMED: "#32b432",
    TunerPhase.FAILED_CONFIRM: "#c86432",
    TunerPhase.BACKOFF_PRECONFIRM: COLOR_ORANGE,
    TunerPhase.BACKOFF_CONFIRMING: COLOR_WARN_SOFT,
    TunerPhase.HARDENING_T1: "#9575cd",
    TunerPhase.HARDENING_T2: "#7e57c2",
    TunerPhase.HARDENED: "#66bb6a",
}

GRID_STATE_LABELS: dict[str, str] = {
    "pending": "Pending",
    "queued": "Queued",
    "testing": "Testing",
    "passed": "Passed",
    "failed": "Failed",
    "skipped": "Skipped",
    "warned": "Warned",
    "backoff": "Backoff",
    "mem_stress": "Mem stress",
}

STATE_COLORS: dict[str, tuple[str, str, str]] = {
    "pending": (BG_DEFAULT, COLOR_MUTED, BORDER_DIM),
    "testing": (BG_TESTING, COLOR_ACTIVE, COLOR_ACTIVE),
    "passed": (BG_PASS, COLOR_PASS, COLOR_PASS),
    "failed": (BG_FAIL, COLOR_FAIL, COLOR_FAIL),
    "skipped": (BG_DEFAULT, COLOR_MUTED_DARKER, BORDER_DIM),
    "warned": (BG_WARN, COLOR_WARN_SOFT, COLOR_WARN_SOFT),
    "queued": (BG_DEFAULT, COLOR_TEXT_DIM, COLOR_MUTED_DARKER),
    "backoff": (BG_BACKOFF, COLOR_WARN_SOFT, COLOR_ORANGE),
    "mem_stress": (BG_MEM_STRESS, "#ce93d8", CHART_VOLT),
}

SESSION_STATUS_LABELS: dict[str, str] = {
    "completed": "Completed",
    "running": "Running",
    "validating": "Validating",
    "paused": "Paused",
    "quarantined": "Quarantined",
    "aborted": "Aborted",
    "crashed": "Crashed",
    "stopped": "Stopped",
    "idle": "Idle",
}

STATUS_COLORS: dict[str, str] = {
    "completed": COLOR_PASS,
    "crashed": COLOR_FAIL,
    "quarantined": COLOR_FAIL,
    "stopped": COLOR_WARN,
    "aborted": COLOR_WARN,
    "running": COLOR_ACTIVE,
    "validating": COLOR_ACTIVE,
    "paused": COLOR_MUTED,
    "idle": COLOR_MUTED,
}


def phase_label(phase: TunerPhase | str) -> str:
    try:
        return PHASE_LABELS[TunerPhase(phase)]
    except ValueError:
        return str(phase)


def state_label(state: str) -> str:
    return GRID_STATE_LABELS.get(state, state)


def status_label(status: str) -> str:
    return SESSION_STATUS_LABELS.get(status, status)


def duration_str(seconds: float | None) -> str:
    if seconds is None or seconds <= 0:
        return ABSENT
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600:02d}h"


def span_str(start_iso: str | None, end_iso: str | None) -> str:
    if not start_iso or not end_iso:
        return ABSENT
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except (ValueError, TypeError):
        return ABSENT
    return duration_str((end - start).total_seconds())


def _assert_complete() -> None:
    for name, mapping in (
        ("PHASE_LABELS", PHASE_LABELS),
        ("PHASE_TO_GRID", PHASE_TO_GRID),
        ("PHASE_COLORS", PHASE_COLORS),
    ):
        missing = set(TunerPhase) - set(mapping)
        if missing:
            raise AssertionError(f"{name} is missing phases: {sorted(missing)}")
    if set(STATE_COLORS) != set(GRID_STATE_LABELS):
        raise AssertionError("STATE_COLORS and GRID_STATE_LABELS disagree on states")
    unmapped = {s for s in PHASE_TO_GRID.values() if s not in STATE_COLORS}
    if unmapped:
        raise AssertionError(f"PHASE_TO_GRID targets unknown grid states: {unmapped}")
    unstyled = set(SESSION_STATUS_LABELS) - set(STATUS_COLORS)
    if unstyled:
        raise AssertionError(f"STATUS_COLORS is missing statuses: {sorted(unstyled)}")


_assert_complete()
