"""Database operations for the auto-tuner — sessions, core states, test log.

All functions delegate to public HistoryDB methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .state import CoreState, TunerSession

if TYPE_CHECKING:
    from history.db import HistoryDB
    from .config import TunerConfig


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(
    db: HistoryDB,
    config: TunerConfig,
    bios_version: str,
    cpu_model: str,
    context_id: int | None = None,
) -> int:
    """Create a new tuner session. Returns the session id."""
    return db.create_tuner_session(config.to_json(), bios_version, cpu_model, context_id)


def update_session_status(db: HistoryDB, session_id: int, status: str) -> None:
    db.update_tuner_session_status(session_id, status)


def get_session(db: HistoryDB, session_id: int) -> TunerSession | None:
    return db.get_tuner_session(session_id)


def get_latest_session(db: HistoryDB) -> TunerSession | None:
    return db.get_latest_tuner_session()


def get_active_session(db: HistoryDB) -> TunerSession | None:
    """Return session with status 'running' or 'paused', if any."""
    return db.get_active_tuner_session()


# ---------------------------------------------------------------------------
# Core states
# ---------------------------------------------------------------------------


def save_core_state(db: HistoryDB, session_id: int, cs: CoreState) -> None:
    """Upsert a core state row."""
    db.upsert_tuner_core_state(session_id, cs)


def load_core_states(db: HistoryDB, session_id: int) -> dict[int, CoreState]:
    return db.get_tuner_core_states(session_id)


# ---------------------------------------------------------------------------
# Test log
# ---------------------------------------------------------------------------


def log_test_result(
    db: HistoryDB,
    session_id: int,
    core_id: int,
    offset: int,
    phase: str,
    passed: bool,
    error_msg: str | None = None,
    error_type: str | None = None,
    duration: float | None = None,
    run_id: int | None = None,
    backend: str | None = None,
    stress_mode: str | None = None,
    fft_preset: str | None = None,
) -> int:
    return db.insert_tuner_test_log(
        session_id, core_id, offset, phase, passed,
        error_msg, error_type, duration, run_id,
        backend=backend, stress_mode=stress_mode, fft_preset=fft_preset,
    )


def get_test_log(
    db: HistoryDB, session_id: int, core_id: int | None = None
) -> list[dict]:
    return db.get_tuner_test_log(session_id, core_id)


def get_best_profile(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: confirmed_offset} for all CONFIRMED cores."""
    return db.get_tuner_best_profile(session_id)
