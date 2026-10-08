from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from corecycler.config.paths import atomic_write

if TYPE_CHECKING:
    from pathlib import Path

    from corecycler.history.db import HistoryDB

FORMAT = "corecycler old tuner sessions"
VERSION = 1
PHASES = {
    "not_started": "the search had not reached the core",
    "coarse_search": "stepping toward max_offset in coarse_step steps",
    "fine_search": "stepping in fine_step steps back from the first coarse failure",
    "settled": "the search found its best value, not yet confirmed",
    "confirming": "the longer confirmation test of the best value ran",
    "confirmed": "the confirmation test passed",
    "failed_confirm": "the confirmation test failed",
    "backoff_preconfirm": "backing off after a failed confirmation, with short pre-confirm tests",
    "backoff_confirming": "confirming a backed-off value",
    "hardening_t1": "the first hardening tier ran",
    "hardening_t2": "the second hardening tier ran",
    "hardened": "every hardening tier passed: the old engine's final per-core state",
}
STATUSES = {
    "running": "a test or a stage was in progress",
    "validating": "the multi-core validation stages were in progress",
    "hunting": "the engine was isolating which core caused a crash",
    "paused": "stopped for attention, resumable",
    "completed": "every core reached its final state",
    "quarantined": "stopped as unsafe after repeated crash-resumes, every core forced to stock",
    "aborted": "stopped by the owner",
}


def archive_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.name + ".old-tuner-sessions.json")


def archive_old_sessions(db: HistoryDB) -> Path | None:
    if str(db.path) == ":memory:":
        return None
    path = archive_path(db.path)
    if path.exists():
        return None
    tables = db.old_tuner_tables()
    if not tables["tuner_sessions"]["rows"]:
        return None
    archive = {
        "format": FORMAT,
        "version": VERSION,
        "written_at": datetime.now(UTC).isoformat(),
        "database": str(db.path),
        "about": (
            "Every session the old tuner engine wrote, before its code was removed (E7, D85). The rows are copied "
            "as stored; the database keeps them too. Times are ISO 8601, offsets are Curve Optimizer steps, "
            "durations are seconds, clocks are MHz."
        ),
        "glossary": {"phases": PHASES, "statuses": STATUSES},
        "tables": tables,
    }
    atomic_write(path, json.dumps(archive, indent=1))
    return path
