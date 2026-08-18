"""Display standards -- the one source for colors, labels, fonts and value formats.

Colors resolve against the desktop's current color scheme: the semantic hues come
from the scheme table, the neutrals from the live QPalette, so the app renders in
the desktop's own colors. Read them through ``theme`` -- importing a color name by
value freezes it at import time and a scheme change is then never seen.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette

from corecycler.tuner.state import TunerPhase

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
        "PHASE_COARSE": "#b4b432",
        "PHASE_FINE": "#c8c832",
        "PHASE_SETTLED": "#c89632",
        "PHASE_CONFIRMING": "#3296c8",
        "PHASE_CONFIRMED": "#32b432",
        "PHASE_FAILED_CONFIRM": "#dc8050",
        "PHASE_HARDENING_T1": "#b39ddb",
        "PHASE_HARDENING_T2": "#a084d6",
        "PHASE_HARDENED": "#66bb6a",
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
        "PHASE_COARSE": "#6b6b1e",
        "PHASE_FINE": "#5f5f1a",
        "PHASE_SETTLED": "#7a5216",
        "PHASE_CONFIRMING": "#1f5f80",
        "PHASE_CONFIRMED": "#1f7a1f",
        "PHASE_FAILED_CONFIRM": "#8f3a10",
        "PHASE_HARDENING_T1": "#5e35b1",
        "PHASE_HARDENING_T2": "#4527a0",
        "PHASE_HARDENED": "#266b2a",
    },
}

ABSENT = "-"
PENDING_VALUE = "--"
NOT_AVAILABLE = "N/A"

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


def _phase_colors(c: dict[str, str]) -> dict[TunerPhase, str]:
    return {
        TunerPhase.NOT_STARTED: c["COLOR_MUTED_DARK"],
        TunerPhase.COARSE_SEARCH: c["PHASE_COARSE"],
        TunerPhase.FINE_SEARCH: c["PHASE_FINE"],
        TunerPhase.SETTLED: c["PHASE_SETTLED"],
        TunerPhase.CONFIRMING: c["PHASE_CONFIRMING"],
        TunerPhase.CONFIRMED: c["PHASE_CONFIRMED"],
        TunerPhase.FAILED_CONFIRM: c["PHASE_FAILED_CONFIRM"],
        TunerPhase.BACKOFF_PRECONFIRM: c["COLOR_ORANGE"],
        TunerPhase.BACKOFF_CONFIRMING: c["COLOR_WARN_SOFT"],
        TunerPhase.HARDENING_T1: c["PHASE_HARDENING_T1"],
        TunerPhase.HARDENING_T2: c["PHASE_HARDENING_T2"],
        TunerPhase.HARDENED: c["PHASE_HARDENED"],
    }


def _status_colors(c: dict[str, str]) -> dict[str, str]:
    return {
        "completed": c["COLOR_PASS"],
        "crashed": c["COLOR_FAIL"],
        "quarantined": c["COLOR_FAIL"],
        "stopped": c["COLOR_WARN"],
        "aborted": c["COLOR_WARN"],
        "running": c["COLOR_ACTIVE"],
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
    values["PHASE_COLORS"] = _phase_colors(colors)
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
    for scheme in _SEMANTIC:
        resolved = resolve(scheme, QPalette())
        for name, mapping in (
            ("PHASE_LABELS", PHASE_LABELS),
            ("PHASE_TO_GRID", PHASE_TO_GRID),
            ("PHASE_COLORS", resolved["PHASE_COLORS"]),
        ):
            missing = set(TunerPhase) - set(mapping)
            if missing:
                raise AssertionError(f"{name} is missing phases: {sorted(missing)}")
        if set(resolved["STATE_COLORS"]) != set(GRID_STATE_LABELS):
            raise AssertionError("STATE_COLORS and GRID_STATE_LABELS disagree on states")
        unmapped = {s for s in PHASE_TO_GRID.values() if s not in resolved["STATE_COLORS"]}
        if unmapped:
            raise AssertionError(f"PHASE_TO_GRID targets unknown grid states: {unmapped}")
        unstyled = set(SESSION_STATUS_LABELS) - set(resolved["STATUS_COLORS"])
        if unstyled:
            raise AssertionError(f"STATUS_COLORS is missing statuses: {sorted(unstyled)}")


_assert_complete()
