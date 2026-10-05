"""Database operations for the auto-tuner — sessions, core states, test log.

All functions delegate to public HistoryDB methods.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from corecycler.history.db import HistoryDB, JournalRow

    from .config import TunerConfig
    from .state import CoreState, TunerSession


@dataclass(frozen=True, slots=True)
class Resident:
    last: JournalRow
    confirmed_before: JournalRow | None


@dataclass(frozen=True, slots=True)
class LogRow:
    core_id: int
    offset: int
    phase: str
    error_msg: str
    error_type: str


@dataclass(frozen=True, slots=True)
class AttributionPlan:
    boot_id: str
    rows: tuple[LogRow, ...]
    states: tuple[CoreState, ...]
    retractions: tuple[tuple[int, int], ...]
    unattributed_crashes: int
    resume_crash_streak: int
    validation_dirty: bool
    quarantine: bool
    crashed: tuple[int, ...]
    pending_hunt: bool
    incident: bool
    messages: tuple[str, ...]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def create_session(
    db: HistoryDB,
    config: TunerConfig,
    bios_version: str,
    cpu_model: str,
    context_id: int | None = None,
    *,
    boot_id: str,
) -> int:
    """Create a new tuner session. Returns the session id."""
    return db.create_tuner_session(config.to_json(), bios_version, cpu_model, context_id, boot_id=boot_id)


def stamp_session_boot(db: HistoryDB, session_id: int, boot_id: str) -> None:
    db.stamp_session_boot(session_id, boot_id)


def commit_attribution(db: HistoryDB, session_id: int, plan: AttributionPlan, direction: int) -> None:
    with db.transaction():
        for row in plan.rows:
            db.insert_tuner_test_log(
                session_id, row.core_id, row.offset, row.phase, False, row.error_msg, row.error_type, None, None
            )
        for cs in plan.states:
            db.upsert_tuner_core_state(session_id, cs)
        for core_id, value in plan.retractions:
            db.journal_retract(session_id, core_id, value, direction)
        db.set_unattributed_crashes(session_id, plan.unattributed_crashes)
        db.set_resume_crash_streak(session_id, plan.resume_crash_streak)
        db.set_validation_dirty(session_id, plan.validation_dirty)
        db.set_hunting_core(session_id, None)
        if plan.quarantine:
            db.update_tuner_session_status(session_id, "quarantined")
        db.stamp_session_boot(session_id, plan.boot_id)


def commit_same_boot(db: HistoryDB, session_id: int, cleared: Iterable[CoreState], resume_crash_streak: int) -> None:
    with db.transaction():
        for cs in cleared:
            db.upsert_tuner_core_state(session_id, cs)
        db.set_hunting_core(session_id, None)
        db.set_resume_crash_streak(session_id, resume_crash_streak)


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
# CO write-ahead journal + resume-crash circuit breaker
# ---------------------------------------------------------------------------


def journal_co_intent(
    db: HistoryDB, session_id: int, core_id: int, value: int, survived: bool, *, boot_id: str, mono_ns: int
) -> int:
    """Durably record a CO value before it is written to the SMU."""
    return db.journal_co_intent(session_id, core_id, value, survived, boot_id=boot_id, mono_ns=mono_ns)


def journal_confirm(db: HistoryDB, row_id: int) -> None:
    db.journal_confirm(row_id)


def journal_mark_survived(
    db: HistoryDB, session_id: int, *, boot_id: str, exclude_cores: tuple[int, ...] | list[int] = ()
) -> None:
    """Mark the resident CO values survived after a test completes without a crash.

    Cores in ``exclude_cores`` stay un-survived — fresh contrary evidence (a
    corrected MCE named them during this very test) outranks the survival.
    """
    db.journal_mark_survived(session_id, boot_id=boot_id, exclude_cores=exclude_cores)


def check_journal(db: HistoryDB, session_id: int) -> None:
    db.check_journal(session_id)


def journal_rows(db: HistoryDB, session_id: int, boot_id: str) -> list[JournalRow]:
    return db.journal_rows(session_id, boot_id)


def journal_taken_over(db: HistoryDB, session_id: int, boot_id: str) -> JournalRow | None:
    return db.journal_taken_over(session_id, boot_id)


def journal_proven_values(db: HistoryDB, session_id: int, direction: int) -> dict[int, int]:
    return db.journal_proven_values(session_id, direction)


def resident_at_death(rows: Iterable[JournalRow]) -> dict[int, Resident]:
    last: dict[int, JournalRow] = {}
    landed: dict[int, JournalRow] = {}
    for row in sorted(rows, key=lambda row: row.id):
        last[row.core_id] = row
        if row.confirmed:
            landed[row.core_id] = row
    return {core_id: Resident(row, None if row.confirmed else landed.get(core_id)) for core_id, row in last.items()}


def crash_suspects(resident: dict[int, Resident], direction: int) -> dict[int, int]:
    suspects: dict[int, int] = {}
    for core_id, held in resident.items():
        unproven = [
            row.value
            for row in (held.last, held.confirmed_before)
            if row is not None and row.value != 0 and (not row.survived or row.retracted)
        ]
        if unproven:
            suspects[core_id] = min(unproven, key=lambda value: direction * value)
    return suspects


def get_resume_crash_streak(db: HistoryDB, session_id: int) -> int:
    return db.get_resume_crash_streak(session_id)


def set_resume_crash_streak(db: HistoryDB, session_id: int, value: int) -> None:
    db.set_resume_crash_streak(session_id, value)


def set_kernel_read(db: HistoryDB, session_id: int, kernel_read_ns: int) -> None:
    db.set_kernel_read(session_id, kernel_read_ns)


def get_unattributed_crashes(db: HistoryDB, session_id: int) -> int:
    return db.get_unattributed_crashes(session_id)


def set_unattributed_crashes(db: HistoryDB, session_id: int, value: int) -> None:
    db.set_unattributed_crashes(session_id, value)


def set_hunting_core(db: HistoryDB, session_id: int, core_id: int | None) -> None:
    """Durably record which core an isolated hunt slot stresses, before it runs."""
    db.set_hunting_core(session_id, core_id)


def log_event(db: HistoryDB, session_id: int, message: str, boot_id: str = "", severity: str = "info") -> None:
    db.insert_tuner_event(session_id, message, boot_id, severity)


def get_events(db: HistoryDB, session_id: int, limit: int = 200) -> list[dict]:
    return db.get_tuner_events(session_id, limit)


def pick_auto_resume_session(db: HistoryDB):
    """The session login-autostart may resume: mid-run (running/validating)
    only. A paused session is a deliberate human choice; quarantined and
    completed ones are excluded by the active query."""
    session = db.get_active_tuner_session()
    if session is not None and session.status in ("running", "validating"):
        return session
    return None


def set_validation_position(
    db: HistoryDB,
    session_id: int,
    stage: int,
    index: int,
    half: int,
    dirty: bool,
    requeue_json: str,
) -> None:
    """Persist the validation cursor so progress survives reboots/restarts."""
    db.set_validation_position(session_id, stage, index, half, dirty, requeue_json)


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
    peak_stretch_pct: float | None = None,
) -> int:
    return db.insert_tuner_test_log(
        session_id,
        core_id,
        offset,
        phase,
        passed,
        error_msg,
        error_type,
        duration,
        run_id,
        backend=backend,
        stress_mode=stress_mode,
        fft_preset=fft_preset,
        peak_stretch_pct=peak_stretch_pct,
    )


def get_test_log(db: HistoryDB, session_id: int, core_id: int | None = None) -> list[dict]:
    return db.get_tuner_test_log(session_id, core_id)


def get_best_profile(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: confirmed_offset} for all CONFIRMED cores."""
    return db.get_tuner_best_profile(session_id)


def get_session_offsets(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: best_offset} for every core with a value, any phase."""
    return db.get_tuner_session_offsets(session_id)
