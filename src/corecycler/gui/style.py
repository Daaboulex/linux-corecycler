"""Display standards -- the one source for colors, labels, fonts and value formats.

Colors resolve against the desktop's current color scheme: the semantic hues come
from the scheme table, the neutrals from the live QPalette, so the app renders in
the desktop's own colors. Read them through ``theme`` -- importing a color name by
value freezes it at import time and a scheme change is then never seen.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette

from corecycler.config.paths import child_environment
from corecycler.tuner.machine.types import Group, PauseReason, Phase, Stage

DARK = "dark"
LIGHT = "light"

_SEMANTIC: dict[str, dict[str, str]] = {
    DARK: {
        "COLOR_PASS": "#4caf50",
        "COLOR_FAIL": "#ef5350",
        "COLOR_WARN": "#ffa726",
        "COLOR_WARN_SOFT": "#ffb74d",
        "COLOR_ORANGE": "#ff9800",
        "COLOR_ACTIVE": "#4fc3f7",
        "COLOR_PASS_DARK": "#2e7d32",
        "COLOR_FAIL_DARK": "#c62828",
        "COLOR_BLUE_DEEP": "#2196f3",
        "COLOR_MEM_STRESS": "#ce93d8",
        "BTN_GREEN": "#1b5e20",
        "BTN_RED": "#b71c1c",
        "CHART_TEMP": "#ff7043",
        "CHART_POWER": "#66bb6a",
        "CHART_VOLT": "#ab47bc",
        "BG_TESTING": "#1a3a5c",
        "BG_PASS": "#1b3a1b",
        "BG_FAIL": "#2f1717",
        "BG_WARN": "#3a3a1b",
        "BG_BACKOFF": "#3a2a1a",
        "BG_MEM_STRESS": "#2a1a2e",
        "BG_ACTIVE_TINT": "#1a2a1a",
    },
    LIGHT: {
        "COLOR_PASS": "#266b2a",
        "COLOR_FAIL": "#c62828",
        "COLOR_WARN": "#8f5000",
        "COLOR_WARN_SOFT": "#9c5800",
        "COLOR_ORANGE": "#8f5000",
        "COLOR_ACTIVE": "#01579b",
        "COLOR_PASS_DARK": "#1b5e20",
        "COLOR_FAIL_DARK": "#8e0000",
        "COLOR_BLUE_DEEP": "#1565c0",
        "COLOR_MEM_STRESS": "#6a1b9a",
        "BTN_GREEN": "#2e7d32",
        "BTN_RED": "#c62828",
        "CHART_TEMP": "#c53d13",
        "CHART_POWER": "#266b2a",
        "CHART_VOLT": "#6a1b9a",
        "BG_TESTING": "#e3eef8",
        "BG_PASS": "#e4f0e5",
        "BG_FAIL": "#fbe7e7",
        "BG_WARN": "#f4f1dc",
        "BG_BACKOFF": "#f9eedd",
        "BG_MEM_STRESS": "#f1e6f6",
        "BG_ACTIVE_TINT": "#e9f3ea",
    },
}

ABSENT = "-"
PENDING_VALUE = "--"
NOT_AVAILABLE = "N/A"

TUNER_PHASE_LABELS: dict[Phase, str] = {
    Phase.NOT_STARTED: "Not started",
    Phase.COARSE: "Coarse",
    Phase.FINE: "Fine",
    Phase.PROVE: "Proving",
    Phase.PROVEN: "Proven",
}


PAUSE_LABELS: dict[PauseReason, str] = {
    PauseReason.USER: "Asked by you",
    PauseReason.RESUMING: "Reading what the last boot left",
    PauseReason.APPARATUS: "A test could not be judged",
    PauseReason.SMU_WRITE: "A Curve Optimizer write failed",
    PauseReason.SMU_READ: "A Curve Optimizer read failed",
    PauseReason.THERMAL: "Too hot",
    PauseReason.DATABASE: "The history database could not be written",
    PauseReason.STOCK_UNSTABLE: "A core fails at its stock value",
    PauseReason.INCIDENTS: "Incidents",
    PauseReason.PROFILE_APPLY: "The found values could not be applied",
    PauseReason.START_FAILURES: "Cores failed their first value",
    PauseReason.INTERNAL: "Internal error",
    PauseReason.OUTSIDE_WRITE: "Another program changed a Curve Optimizer value",
    PauseReason.HELD_KNOB: "A core outside the session failed",
    PauseReason.SETUP_CHANGED: "The setup changed",
    PauseReason.KERNEL_LOG_UNREAD: "The kernel log could not be read",
}

STAGE_LABELS: dict[Stage, str] = {
    Stage.SEARCH: "Search",
    Stage.LIVE1: "Each core alone",
    Stage.BISECT: "Narrowing",
    Stage.STEADY: "Hot all-core",
    Stage.TRANSIENT: "Changing loads",
    Stage.TIGHTEN: "Tighten",
    Stage.ENDURANCE: "Endurance",
    Stage.MEMORY: "Memory",
    Stage.SOAK: "Idle watch",
    Stage.FINALIZE: "Finishing",
}

VERDICT_LABELS: dict[str, str] = {
    Group.PASS: "Passed",
    Group.SILICON: "Failed",
    Group.THERMAL: "Too hot",
    Group.ENVIRONMENT: "Machine fault no core is blamed for",
    Group.APPARATUS: "Could not be judged",
    Group.SLEPT: "Machine slept",
    "internal_error": "Internal error",
}

POWER_LABELS = {
    "power profile": "profile {}",
    "platform profile": "platform profile {}",
    "governor": "governor {}",
    "EPP": "EPP {}",
    "boost": "boost {}",
    "scaling maximum MHz": "max MHz {}",
}
ON_OFF = {"1": "on", "0": "off"}

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

SESSION_STATUS_LABELS: dict[str, str] = {
    "completed": "Completed",
    "running": "Running",
    "interrupted": "Interrupted",
    "validating": "Validating",
    "paused": "Paused",
    "quarantined": "Quarantined",
    "aborted": "Aborted",
    "crashed": "Crashed",
    "stopped": "Stopped",
    "idle": "Idle",
}


def _mix(a: QColor, b: QColor, t: float) -> str:
    return QColor(
        round(a.red() * (1 - t) + b.red() * t),
        round(a.green() * (1 - t) + b.green() * t),
        round(a.blue() * (1 - t) + b.blue() * t),
    ).name()


_DIM_STEPS: dict[str, tuple[float, float, float, float]] = {
    DARK: (0.28, 0.42, 0.52, 0.62),
    LIGHT: (0.20, 0.30, 0.38, 0.45),
}


def _neutrals(scheme: str, palette: QPalette) -> dict[str, str]:
    """The colors taken from the desktop rather than chosen by CoreCycler."""
    role = QPalette.ColorRole
    text = palette.color(role.WindowText)
    window = palette.color(role.Window)
    dim, muted, muted_dark, muted_darker = _DIM_STEPS[scheme]
    return {
        "COLOR_TEXT_BRIGHT": text.name(),
        "COLOR_TEXT_DIM": _mix(text, window, dim),
        "COLOR_MUTED": _mix(text, window, muted),
        "COLOR_MUTED_DARK": _mix(text, window, muted_dark),
        "COLOR_MUTED_DARKER": _mix(text, window, muted_darker),
        "BG_PANEL_DARK": palette.color(role.Base).name(),
        "BG_PANEL": palette.color(role.AlternateBase).name(),
        "BG_DEFAULT": palette.color(role.AlternateBase).name(),
        "BG_SELECTED": palette.color(role.Highlight).name(),
        "COLOR_ON_SELECTED": palette.color(role.HighlightedText).name(),
        "BORDER_DIM": palette.color(role.Mid).name(),
        "BORDER_DARKER": _mix(palette.color(role.Mid), window, 0.45),
    }


def _state_colors(c: dict[str, str]) -> dict[str, tuple[str, str, str]]:
    return {
        "pending": (c["BG_DEFAULT"], c["COLOR_MUTED"], c["BORDER_DIM"]),
        "testing": (c["BG_TESTING"], c["COLOR_ACTIVE"], c["COLOR_ACTIVE"]),
        "passed": (c["BG_PASS"], c["COLOR_PASS"], c["COLOR_PASS"]),
        "failed": (c["BG_FAIL"], c["COLOR_FAIL"], c["COLOR_FAIL"]),
        "skipped": (c["BG_DEFAULT"], c["COLOR_MUTED_DARKER"], c["BORDER_DIM"]),
        "warned": (c["BG_WARN"], c["COLOR_WARN_SOFT"], c["COLOR_WARN_SOFT"]),
        "queued": (c["BG_DEFAULT"], c["COLOR_TEXT_DIM"], c["COLOR_MUTED_DARKER"]),
        "backoff": (c["BG_BACKOFF"], c["COLOR_WARN_SOFT"], c["COLOR_ORANGE"]),
        "mem_stress": (c["BG_MEM_STRESS"], c["COLOR_MEM_STRESS"], c["CHART_VOLT"]),
    }


def _status_colors(c: dict[str, str]) -> dict[str, str]:
    return {
        "completed": c["COLOR_PASS"],
        "crashed": c["COLOR_FAIL"],
        "quarantined": c["COLOR_FAIL"],
        "stopped": c["COLOR_WARN"],
        "aborted": c["COLOR_WARN"],
        "running": c["COLOR_ACTIVE"],
        "interrupted": c["COLOR_WARN"],
        "validating": c["COLOR_ACTIVE"],
        "paused": c["COLOR_MUTED"],
        "idle": c["COLOR_MUTED"],
    }


def resolve(scheme: str, palette: QPalette) -> dict[str, object]:
    """Every display color for one scheme against one desktop palette."""
    if scheme not in _SEMANTIC:
        raise ValueError(f"unknown color scheme {scheme!r}")
    colors: dict[str, str] = dict(_SEMANTIC[scheme])
    colors.update(_neutrals(scheme, palette))
    colors["CHART_FREQ"] = colors["COLOR_ACTIVE"]
    values: dict[str, object] = dict(colors)
    values["STATE_COLORS"] = _state_colors(colors)
    values["STATUS_COLORS"] = _status_colors(colors)
    values["scheme"] = scheme
    return values


class _Theme:
    """The resolved display colors; every attribute comes from :func:`resolve`."""

    scheme: str


theme = _Theme()


def scheme_for(color_scheme: Qt.ColorScheme, palette: QPalette) -> str:
    """The scheme to render in: what the desktop reports, else what its palette shows."""
    if color_scheme == Qt.ColorScheme.Dark:
        return DARK
    if color_scheme == Qt.ColorScheme.Light:
        return LIGHT
    return DARK if palette.color(QPalette.ColorRole.Window).lightness() < 128 else LIGHT


def use_scheme(scheme: str, palette: QPalette) -> None:
    resolved = resolve(scheme, palette)
    vars(theme).clear()
    vars(theme).update(resolved)


def follow(app) -> None:
    """Render in the desktop's color scheme, now and whenever the desktop changes it."""
    hints = app.styleHints()

    def _resolve() -> None:
        palette = app.palette()
        use_scheme(scheme_for(hints.colorScheme(), palette), palette)
        for widget in app.allWidgets():
            widget.update()

    hints.colorSchemeChanged.connect(lambda _scheme: _resolve())
    _resolve()


use_scheme(LIGHT, QPalette())


def font_mono(size: int = 9, bold: bool = False) -> QFont:
    return QFont("monospace", size, QFont.Weight.Bold if bold else QFont.Weight.Normal)


def button_qss(bg: str) -> str:
    return (
        f"QPushButton {{ background: {bg}; color: white; padding: 6px 14px; "
        f"border-radius: 4px; font-weight: bold; }}"
        f"QPushButton:disabled {{ background: {theme.COLOR_MUTED_DARKER}; "
        f"color: {theme.COLOR_MUTED}; }}"
    )


def scheduler_phase_label(phase: str) -> str:
    return phase.capitalize() if phase else ""


def tuner_phase_label(value: str) -> str:
    return TUNER_PHASE_LABELS.get(value, value)


def stage_label(value: str) -> str:
    return STAGE_LABELS.get(value, value)


def verdict_text(value: str) -> str:
    return VERDICT_LABELS.get(value, value)


def pause_text(message: str) -> str:
    reason, colon, detail = message.partition(": ")
    label = PAUSE_LABELS.get(reason)
    return f"{label}: {detail}" if label is not None and colon else message


def power_text(pairs: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(
        POWER_LABELS[name].format(ON_OFF.get(value, value) if name == "boost" else value)
        if name in POWER_LABELS
        else f"{name} {value}"
        for name, value in pairs
    )


def shown_status(status: str, driver_running: bool) -> str:
    return "interrupted" if status == "running" and not driver_running else status


def state_label(state: str) -> str:
    return GRID_STATE_LABELS.get(state, state)


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
    except ValueError, TypeError:
        return ABSENT
    return duration_str((end - start).total_seconds())


def _assert_complete() -> None:
    for table, members in (
        (TUNER_PHASE_LABELS, Phase),
        (PAUSE_LABELS, PauseReason),
        (STAGE_LABELS, Stage),
        (VERDICT_LABELS, Group),
    ):
        unlabelled = set(members) - set(table)
        if unlabelled:
            raise AssertionError(f"{members.__name__} has no label for {sorted(unlabelled)}")
    for scheme in _SEMANTIC:
        resolved = resolve(scheme, QPalette())
        if set(resolved["STATE_COLORS"]) != set(GRID_STATE_LABELS):
            raise AssertionError("STATE_COLORS and GRID_STATE_LABELS disagree on states")
        unstyled = set(SESSION_STATUS_LABELS) - set(resolved["STATUS_COLORS"])
        if unstyled:
            raise AssertionError(f"STATUS_COLORS is missing statuses: {sorted(unstyled)}")


_assert_complete()


STYLE_PROBE = (
    "import sys\nfrom PySide6.QtWidgets import QApplication\nprint(QApplication(sys.argv[:1]).style().name())\n"
)
STYLE_PROBE_S = 15


def style_problem(run=subprocess.run) -> str | None:
    if os.environ.get("QT_STYLE_OVERRIDE"):
        return None
    try:
        probe = run(
            [sys.executable, "-c", STYLE_PROBE],
            capture_output=True,
            text=True,
            timeout=STYLE_PROBE_S,
            env=child_environment(),
        )
    except subprocess.TimeoutExpired:
        return f"system Qt style probe did not finish in {STYLE_PROBE_S} s"
    except OSError as e:
        return f"system Qt style probe could not run: {e}"
    if probe.returncode < 0:
        try:
            name = signal.Signals(-probe.returncode).name
        except ValueError:
            name = f"signal {-probe.returncode}"
        return f"system Qt style crashed in a probe ({name})"
    if probe.returncode != 0:
        said = [line for line in probe.stderr.splitlines() if line.strip()]
        return f"system Qt style probe exited {probe.returncode}: {said[-1] if said else 'no output'}"
    return None


def fall_back_to_fusion(run=subprocess.run) -> str | None:
    problem = style_problem(run)
    if problem is not None:
        os.environ["QT_STYLE_OVERRIDE"] = "Fusion"
        logging.getLogger(__name__).warning("%s: running in Fusion", problem)
    return problem
