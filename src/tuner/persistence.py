"""Database operations for the auto-tuner — sessions, core states, test log."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .state import CoreState, TunerSession

if TYPE_CHECKING:
    from history.db import HistoryDB
    from .config import TunerConfig


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    now = _now_iso()
    cur = db._conn.execute(
        """\
        INSERT INTO tuner_sessions
            (created_at, updated_at, status, bios_version, cpu_model,
             config_json, context_id, notes)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (now, now, "running", bios_version, cpu_model, config.to_json(), context_id, ""),
    )
    return cur.lastrowid


def update_session_status(db: HistoryDB, session_id: int, status: str) -> None:
    db._conn.execute(
        "UPDATE tuner_sessions SET status=?, updated_at=? WHERE id=?",
        (status, _now_iso(), session_id),
    )


def get_session(db: HistoryDB, session_id: int) -> TunerSession | None:
    row = db._conn.execute(
        "SELECT * FROM tuner_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_session(row)


def get_latest_session(db: HistoryDB) -> TunerSession | None:
    row = db._conn.execute(
        "SELECT * FROM tuner_sessions ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return _row_to_session(row)


def get_active_session(db: HistoryDB) -> TunerSession | None:
    """Return session with status 'running' or 'paused', if any."""
    row = db._conn.execute(
        "SELECT * FROM tuner_sessions WHERE status IN ('running','paused') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return _row_to_session(row)


def _row_to_session(row) -> TunerSession:
    return TunerSession(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        bios_version=row["bios_version"],
        cpu_model=row["cpu_model"],
        config_json=row["config_json"],
        context_id=row["context_id"],
        notes=row["notes"],
    )


# ---------------------------------------------------------------------------
# Core states
# ---------------------------------------------------------------------------


def save_core_state(db: HistoryDB, session_id: int, cs: CoreState) -> None:
    """Upsert a core state row."""
    now = _now_iso()
    db._conn.execute(
        """\
        INSERT INTO tuner_core_states
            (session_id, core_id, phase, current_offset, best_offset,
             coarse_fail_offset, confirm_attempts, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id, core_id) DO UPDATE SET
            phase=excluded.phase,
            current_offset=excluded.current_offset,
            best_offset=excluded.best_offset,
            coarse_fail_offset=excluded.coarse_fail_offset,
            confirm_attempts=excluded.confirm_attempts,
            updated_at=excluded.updated_at
        """,
        (
            session_id,
            cs.core_id,
            cs.phase,
            cs.current_offset,
            cs.best_offset,
            cs.coarse_fail_offset,
            cs.confirm_attempts,
            now,
        ),
    )


def load_core_states(db: HistoryDB, session_id: int) -> dict[int, CoreState]:
    rows = db._conn.execute(
        "SELECT * FROM tuner_core_states WHERE session_id=? ORDER BY core_id",
        (session_id,),
    ).fetchall()
    result: dict[int, CoreState] = {}
    for r in rows:
        result[r["core_id"]] = CoreState(
            core_id=r["core_id"],
            phase=r["phase"],
            current_offset=r["current_offset"],
            best_offset=r["best_offset"],
            coarse_fail_offset=r["coarse_fail_offset"],
            confirm_attempts=r["confirm_attempts"],
        )
    return result


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
) -> int:
    cur = db._conn.execute(
        """\
        INSERT INTO tuner_test_log
            (session_id, core_id, offset_tested, phase, passed,
             error_message, error_type, duration_seconds, run_id, tested_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            session_id,
            core_id,
            offset,
            phase,
            int(passed),
            error_msg,
            error_type,
            duration,
            run_id,
            _now_iso(),
        ),
    )
    return cur.lastrowid


def get_test_log(
    db: HistoryDB, session_id: int, core_id: int | None = None
) -> list[dict]:
    if core_id is not None:
        rows = db._conn.execute(
            "SELECT * FROM tuner_test_log WHERE session_id=? AND core_id=? ORDER BY id",
            (session_id, core_id),
        ).fetchall()
    else:
        rows = db._conn.execute(
            "SELECT * FROM tuner_test_log WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_best_profile(db: HistoryDB, session_id: int) -> dict[int, int]:
    """Return {core_id: confirmed_offset} for all CONFIRMED cores."""
    rows = db._conn.execute(
        "SELECT core_id, best_offset FROM tuner_core_states "
        "WHERE session_id=? AND phase='confirmed' AND best_offset IS NOT NULL",
        (session_id,),
    ).fetchall()
    return {r["core_id"]: r["best_offset"] for r in rows}
