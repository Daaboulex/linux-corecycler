from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.history.db import HistoryDB

WHEN = "2026-09-01T10:00:00+00:00"


def seed_old_session(db: HistoryDB, status: str = "completed", phase: str = "hardened") -> int:
    session = db._execute_raw(
        "INSERT INTO tuner_sessions (created_at, updated_at, status, config_json, bios_version, cpu_model)"
        " VALUES (?, ?, ?, '{}', '2402', 'Test 8C')",
        (WHEN, WHEN, status),
    ).lastrowid
    db._execute_raw(
        "INSERT INTO tuner_core_states (session_id, core_id, phase, current_offset, best_offset, updated_at)"
        " VALUES (?, 0, ?, -12, -12, ?)",
        (session, phase, WHEN),
    )
    db._execute_raw(
        "INSERT INTO tuner_test_log (session_id, core_id, offset_tested, phase, passed, tested_at)"
        " VALUES (?, 0, -12, ?, 1, ?)",
        (session, phase, WHEN),
    )
    db._execute_raw(
        "INSERT INTO tuner_events (session_id, timestamp, message) VALUES (?, ?, 'confirmed')", (session, WHEN)
    )
    db._execute_raw(
        "INSERT INTO tuner_co_journal (session_id, core_id, value, boot_id, written_at) VALUES (?, 0, -12, 'b', ?)",
        (session, WHEN),
    )
    return session
