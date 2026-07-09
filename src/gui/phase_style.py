"""Single source of truth for how a TunerPhase is presented in the UI.

Every TunerPhase MUST have an entry in both maps — enforced at import time, so
a new phase can never ship with an undefined UI state (an unmapped phase used
to silently render as 'pending'/grey: cores mid-hardening looked idle).
"""

from __future__ import annotations

from tuner.state import TunerPhase

# phase -> core-grid visual state (keys of gui.widgets.core_grid.STATE_COLORS)
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

# phase -> display color (hex), used for phase text in tables
PHASE_COLORS: dict[TunerPhase, str] = {
    TunerPhase.NOT_STARTED: "#666666",
    TunerPhase.COARSE_SEARCH: "#b4b432",
    TunerPhase.FINE_SEARCH: "#c8c832",
    TunerPhase.SETTLED: "#c89632",
    TunerPhase.CONFIRMING: "#3296c8",
    TunerPhase.CONFIRMED: "#32b432",
    TunerPhase.FAILED_CONFIRM: "#c86432",
    TunerPhase.BACKOFF_PRECONFIRM: "#ff9800",
    TunerPhase.BACKOFF_CONFIRMING: "#ffb74d",
    TunerPhase.HARDENING_T1: "#9575cd",
    TunerPhase.HARDENING_T2: "#7e57c2",
    TunerPhase.HARDENED: "#2e7d32",
}


def _check_exhaustive() -> None:
    for name, mapping in (
        ("PHASE_TO_GRID", PHASE_TO_GRID),
        ("PHASE_COLORS", PHASE_COLORS),
    ):
        missing = set(TunerPhase) - set(mapping)
        if missing:
            raise RuntimeError(
                f"{name} is missing TunerPhase entries: "
                f"{sorted(m.value for m in missing)}"
            )


_check_exhaustive()
