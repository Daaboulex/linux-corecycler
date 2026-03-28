"""Crash-safe test history database using SQLite WAL mode.

Every write is an auto-commit transaction.  WAL + synchronous=NORMAL gives
process-crash safety with good performance — data survives kill -9 and OOM.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tuner.state import CoreState, TunerSession

SCHEMA_VERSION = 7

DATA_DIR = Path.home() / ".local" / "share" / "corecycler" / "history"
DEFAULT_DB_PATH = DATA_DIR / "history.db"


# ---------------------------------------------------------------------------
# Record dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RunRecord:
    id: int | None = None
    started_at: str = ""  # ISO 8601 UTC
    finished_at: str | None = None
    status: str = "running"  # running, completed, stopped, crashed
    cpu_model: str = ""
    physical_cores: int = 0
    logical_cpus: int = 0
    ccds: int = 0
    is_x3d: bool = False
    # test settings snapshot (JSON blob)
    backend: str = ""
    stress_mode: str = ""
    fft_preset: str = ""
    seconds_per_core: int = 0
    cycle_count: int = 1
    stop_on_error: bool = False
    variable_load: bool = False
    idle_stability_test: float = 0.0
    max_temperature: float = 95.0
    settings_json: str = "{}"
    # tuning context (v2)
    context_id: int | None = None
    bios_version: str = ""
    # summary (filled on finish)
    total_cores: int = 0
    cores_passed: int = 0
    cores_failed: int = 0
    total_seconds: float = 0.0


@dataclass(slots=True)
class CoreResultRecord:
    id: int | None = None
    run_id: int = 0
    core_id: int = 0
    ccd: int | None = None
    cycle: int = 0
    started_at: str = ""
    finished_at: str | None = None
    passed: bool | None = None  # None while running
    error_message: str | None = None
    error_type: str | None = None
    elapsed_seconds: float = 0.0
    iterations_completed: int = 0
    peak_freq_mhz: float | None = None
    max_temp_c: float | None = None
    min_vcore_v: float | None = None
    max_vcore_v: float | None = None


@dataclass(slots=True)
class EventRecord:
    id: int | None = None
    run_id: int = 0
    timestamp: str = ""  # ISO 8601 UTC
    event_type: str = ""  # core_start, core_finish, error, phase_change, thermal, stall, cycle, info
    core_id: int | None = None
    message: str = ""
    details_json: str | None = None


@dataclass(slots=True)
class TuningContextRecord:
    id: int | None = None
    created_at: str = ""
    bios_version: str = ""
    co_offsets_json: str = "{}"
    co_hash: str = ""
    pbo_scalar: float | None = None
    boost_limit_mhz: int | None = None
    notes: str = ""


@dataclass(slots=True)
class TelemetrySample:
    id: int | None = None
    run_id: int = 0
    core_id: int = 0
    timestamp: str = ""
    freq_mhz: float | None = None
    effective_max_mhz: float | None = None  # scaling_max_freq — boost ceiling for clock stretch detection
    temp_c: float | None = None
    vcore_v: float | None = None


# ---------------------------------------------------------------------------
# HistoryDB
# ---------------------------------------------------------------------------


class HistoryDB:
    """Crash-safe SQLite database for test run history."""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self.__conn = sqlite3.connect(
            str(self._db_path),
            isolation_level=None,  # autocommit
        )
        self.__conn.row_factory = sqlite3.Row
        self.__conn.execute("PRAGMA journal_mode=WAL")
        self.__conn.execute("PRAGMA synchronous=NORMAL")
        self.__conn.execute("PRAGMA foreign_keys=ON")
        self._create_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _create_schema(self) -> None:
        cur = self.__conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if cur.fetchone() is None:
            # Fresh database — create everything at current version
            self.__conn.executescript(self._DDL_V7)
            return

        # Existing database — check version and migrate
        version = self.__conn.execute("SELECT version FROM schema_version").fetchone()[0]
        if version < 2:
            self.__conn.executescript(self._DDL_MIGRATE_V2)
            self.__conn.execute("UPDATE schema_version SET version=2")
            version = 2
        if version < 3:
            self.__conn.executescript(self._DDL_MIGRATE_V3)
            self.__conn.execute("UPDATE schema_version SET version=3")
            version = 3
        if version < 4:
            self.__conn.executescript(self._DDL_MIGRATE_V4)
            self.__conn.execute("UPDATE schema_version SET version=4")
            version = 4
        if version < 5:
            self.__conn.executescript(self._DDL_MIGRATE_V5)
            self.__conn.execute("UPDATE schema_version SET version=5")
            version = 5
        if version < 6:
            self.__conn.executescript(self._DDL_MIGRATE_V6)
            self.__conn.execute("UPDATE schema_version SET version=6")
            version = 6
        if version < 7:
            for col_name, col_def in self._DDL_MIGRATE_V7_COLUMNS:
                try:
                    self.__conn.execute(
                        f"ALTER TABLE tuner_core_states ADD COLUMN {col_name} {col_def}"
                    )
                except Exception:
                    pass  # Column already exists from partial migration
            self.__conn.execute("UPDATE schema_version SET version=7")
            version = 7
        if version < 8:
            try:
                self.__conn.execute(
                    "ALTER TABLE tuner_core_states ADD COLUMN in_test INTEGER NOT NULL DEFAULT 0"
                )
            except Exception:
                pass  # Column already exists
            self.__conn.execute("UPDATE schema_version SET version=8")
            version = 8

    # Full schema for fresh databases (v8)
    _DDL_V7 = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (version) VALUES (8);

CREATE TABLE IF NOT EXISTS tuning_contexts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    bios_version    TEXT    NOT NULL DEFAULT '',
    co_offsets_json TEXT    NOT NULL DEFAULT '{}',
    co_hash         TEXT    NOT NULL DEFAULT '',
    pbo_scalar      REAL,
    boost_limit_mhz INTEGER,
    notes           TEXT    NOT NULL DEFAULT '',
    UNIQUE(co_hash, bios_version)
);
CREATE INDEX IF NOT EXISTS idx_context_hash ON tuning_contexts(co_hash, bios_version);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running',
    cpu_model       TEXT    NOT NULL DEFAULT '',
    physical_cores  INTEGER NOT NULL DEFAULT 0,
    logical_cpus    INTEGER NOT NULL DEFAULT 0,
    ccds            INTEGER NOT NULL DEFAULT 0,
    is_x3d          INTEGER NOT NULL DEFAULT 0,
    backend         TEXT    NOT NULL DEFAULT '',
    stress_mode     TEXT    NOT NULL DEFAULT '',
    fft_preset      TEXT    NOT NULL DEFAULT '',
    seconds_per_core INTEGER NOT NULL DEFAULT 0,
    cycle_count     INTEGER NOT NULL DEFAULT 1,
    stop_on_error   INTEGER NOT NULL DEFAULT 0,
    variable_load   INTEGER NOT NULL DEFAULT 0,
    idle_stability_test REAL NOT NULL DEFAULT 0.0,
    max_temperature REAL    NOT NULL DEFAULT 95.0,
    settings_json   TEXT    NOT NULL DEFAULT '{}',
    context_id      INTEGER REFERENCES tuning_contexts(id),
    bios_version    TEXT    NOT NULL DEFAULT '',
    total_cores     INTEGER NOT NULL DEFAULT 0,
    cores_passed    INTEGER NOT NULL DEFAULT 0,
    cores_failed    INTEGER NOT NULL DEFAULT 0,
    total_seconds   REAL    NOT NULL DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);

CREATE TABLE IF NOT EXISTS core_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    core_id         INTEGER NOT NULL,
    ccd             INTEGER,
    cycle           INTEGER NOT NULL DEFAULT 0,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    passed          INTEGER,
    error_message   TEXT,
    error_type      TEXT,
    elapsed_seconds REAL    NOT NULL DEFAULT 0.0,
    iterations_completed INTEGER NOT NULL DEFAULT 0,
    peak_freq_mhz   REAL,
    max_temp_c       REAL,
    min_vcore_v      REAL,
    max_vcore_v      REAL
);
CREATE INDEX IF NOT EXISTS idx_core_results_run ON core_results(run_id);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    timestamp       TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    core_id         INTEGER,
    message         TEXT    NOT NULL DEFAULT '',
    details_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);

CREATE TABLE IF NOT EXISTS telemetry_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    core_id         INTEGER NOT NULL,
    timestamp       TEXT    NOT NULL,
    freq_mhz       REAL,
    effective_max_mhz REAL,
    temp_c          REAL,
    vcore_v         REAL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_run_core ON telemetry_samples(run_id, core_id);

CREATE TABLE IF NOT EXISTS tuner_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'running',
    bios_version        TEXT    NOT NULL DEFAULT '',
    cpu_model           TEXT    NOT NULL DEFAULT '',
    config_json         TEXT    NOT NULL DEFAULT '{}',
    context_id          INTEGER REFERENCES tuning_contexts(id),
    notes               TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tuner_core_states (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'not_started',
    current_offset      INTEGER NOT NULL DEFAULT 0,
    best_offset         INTEGER,
    coarse_fail_offset  INTEGER,
    confirm_attempts    INTEGER NOT NULL DEFAULT 0,
    baseline_offset     INTEGER NOT NULL DEFAULT 0,
    backoff_mode        INTEGER NOT NULL DEFAULT 0,
    consecutive_backoff_fails INTEGER NOT NULL DEFAULT 0,
    backoff_fail_bound  INTEGER,
    backoff_pass_bound  INTEGER,
    in_test             INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);

CREATE TABLE IF NOT EXISTS tuner_test_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    offset_tested       INTEGER NOT NULL,
    phase               TEXT    NOT NULL,
    passed              INTEGER NOT NULL,
    error_message       TEXT,
    error_type          TEXT,
    duration_seconds    REAL,
    run_id              INTEGER REFERENCES runs(id),
    tested_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_log_session ON tuner_test_log(session_id, core_id);
"""

    # Migration from v1 to v2
    _DDL_MIGRATE_V2 = """\
CREATE TABLE IF NOT EXISTS tuning_contexts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    bios_version    TEXT    NOT NULL DEFAULT '',
    co_offsets_json TEXT    NOT NULL DEFAULT '{}',
    co_hash         TEXT    NOT NULL DEFAULT '',
    pbo_scalar      REAL,
    boost_limit_mhz INTEGER,
    notes           TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_context_hash ON tuning_contexts(co_hash, bios_version);

ALTER TABLE runs ADD COLUMN context_id INTEGER REFERENCES tuning_contexts(id);
ALTER TABLE runs ADD COLUMN bios_version TEXT NOT NULL DEFAULT '';
"""

    # Migration from v2 to v3 — add tuner tables
    _DDL_MIGRATE_V3 = """\
CREATE TABLE IF NOT EXISTS tuner_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'running',
    bios_version        TEXT    NOT NULL DEFAULT '',
    cpu_model           TEXT    NOT NULL DEFAULT '',
    config_json         TEXT    NOT NULL DEFAULT '{}',
    context_id          INTEGER REFERENCES tuning_contexts(id),
    notes               TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tuner_core_states (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    phase               TEXT    NOT NULL DEFAULT 'not_started',
    current_offset      INTEGER NOT NULL DEFAULT 0,
    best_offset         INTEGER,
    coarse_fail_offset  INTEGER,
    confirm_attempts    INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);

CREATE TABLE IF NOT EXISTS tuner_test_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id             INTEGER NOT NULL,
    offset_tested       INTEGER NOT NULL,
    phase               TEXT    NOT NULL,
    passed              INTEGER NOT NULL,
    error_message       TEXT,
    error_type          TEXT,
    duration_seconds    REAL,
    run_id              INTEGER REFERENCES runs(id),
    tested_at           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_log_session ON tuner_test_log(session_id, core_id);

-- Performance index for time-based queries on runs
CREATE INDEX IF NOT EXISTS idx_runs_started_at ON runs(started_at DESC);
"""

    # Migration from v3 to v4 — add effective_max_mhz for clock stretch detection
    _DDL_MIGRATE_V4 = """\
ALTER TABLE telemetry_samples ADD COLUMN effective_max_mhz REAL;
"""

    # Migration from v4 to v5 — deduplicate tuning contexts, add UNIQUE constraint
    _DDL_MIGRATE_V5 = """\
-- Deduplicate existing rows: keep the oldest (smallest id) for each (co_hash, bios_version)
DELETE FROM tuning_contexts
WHERE id NOT IN (
    SELECT MIN(id) FROM tuning_contexts GROUP BY co_hash, bios_version
);
-- Add UNIQUE constraint via index (SQLite cannot ALTER TABLE ADD CONSTRAINT)
CREATE UNIQUE INDEX IF NOT EXISTS idx_context_unique_hash ON tuning_contexts(co_hash, bios_version);
"""

    # Migration from v5 to v6 — add baseline_offset for CO isolation during tuning
    _DDL_MIGRATE_V6 = """\
ALTER TABLE tuner_core_states ADD COLUMN baseline_offset INTEGER NOT NULL DEFAULT 0;
"""

    # Migration from v6 to v7 — add backoff algorithm columns
    _DDL_MIGRATE_V7_COLUMNS = [
        ("backoff_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("consecutive_backoff_fails", "INTEGER NOT NULL DEFAULT 0"),
        ("backoff_fail_bound", "INTEGER"),
        ("backoff_pass_bound", "INTEGER"),
    ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Runs
    # ------------------------------------------------------------------

    def create_run(self, run: RunRecord) -> int:
        """Insert a new run record. Returns the run id."""
        if not run.started_at:
            run.started_at = self._now_iso()
        cur = self.__conn.execute(
            """\
            INSERT INTO runs (
                started_at, status, cpu_model, physical_cores, logical_cpus,
                ccds, is_x3d, backend, stress_mode, fft_preset,
                seconds_per_core, cycle_count, stop_on_error, variable_load,
                idle_stability_test, max_temperature, settings_json,
                context_id, bios_version,
                total_cores, cores_passed, cores_failed, total_seconds
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run.started_at,
                run.status,
                run.cpu_model,
                run.physical_cores,
                run.logical_cpus,
                run.ccds,
                int(run.is_x3d),
                run.backend,
                run.stress_mode,
                run.fft_preset,
                run.seconds_per_core,
                run.cycle_count,
                int(run.stop_on_error),
                int(run.variable_load),
                run.idle_stability_test,
                run.max_temperature,
                run.settings_json,
                run.context_id,
                run.bios_version,
                run.total_cores,
                run.cores_passed,
                run.cores_failed,
                run.total_seconds,
            ),
        )
        run.id = cur.lastrowid
        return run.id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str = "completed",
        total_cores: int = 0,
        cores_passed: int = 0,
        cores_failed: int = 0,
        total_seconds: float = 0.0,
    ) -> None:
        self.__conn.execute(
            """\
            UPDATE runs SET finished_at=?, status=?,
                total_cores=?, cores_passed=?, cores_failed=?, total_seconds=?
            WHERE id=?
            """,
            (
                self._now_iso(),
                status,
                total_cores,
                cores_passed,
                cores_failed,
                total_seconds,
                run_id,
            ),
        )

    def get_run(self, run_id: int) -> RunRecord | None:
        row = self.__conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_run(row)

    def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[RunRecord]:
        rows = self.__conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    def delete_run(self, run_id: int) -> None:
        """Delete a run and all related records (CASCADE)."""
        self.__conn.execute("DELETE FROM runs WHERE id=?", (run_id,))

    def list_runs_for_context(self, context_id: int) -> list[RunRecord]:
        """Return all runs belonging to a specific tuning context."""
        rows = self.__conn.execute(
            "SELECT * FROM runs WHERE context_id=? ORDER BY id DESC",
            (context_id,),
        ).fetchall()
        return [self._row_to_run(r) for r in rows]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            id=row["id"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            status=row["status"],
            cpu_model=row["cpu_model"],
            physical_cores=row["physical_cores"],
            logical_cpus=row["logical_cpus"],
            ccds=row["ccds"],
            is_x3d=bool(row["is_x3d"]),
            backend=row["backend"],
            stress_mode=row["stress_mode"],
            fft_preset=row["fft_preset"],
            seconds_per_core=row["seconds_per_core"],
            cycle_count=row["cycle_count"],
            stop_on_error=bool(row["stop_on_error"]),
            variable_load=bool(row["variable_load"]),
            idle_stability_test=row["idle_stability_test"],
            max_temperature=row["max_temperature"],
            settings_json=row["settings_json"],
            context_id=row["context_id"],
            bios_version=row["bios_version"],
            total_cores=row["total_cores"],
            cores_passed=row["cores_passed"],
            cores_failed=row["cores_failed"],
            total_seconds=row["total_seconds"],
        )

    # ------------------------------------------------------------------
    # Core results
    # ------------------------------------------------------------------

    def insert_core_result(self, rec: CoreResultRecord) -> int:
        if not rec.started_at:
            rec.started_at = self._now_iso()
        cur = self.__conn.execute(
            """\
            INSERT INTO core_results (
                run_id, core_id, ccd, cycle, started_at, finished_at,
                passed, error_message, error_type, elapsed_seconds,
                iterations_completed, peak_freq_mhz, max_temp_c,
                min_vcore_v, max_vcore_v
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rec.run_id,
                rec.core_id,
                rec.ccd,
                rec.cycle,
                rec.started_at,
                rec.finished_at,
                None if rec.passed is None else int(rec.passed),
                rec.error_message,
                rec.error_type,
                rec.elapsed_seconds,
                rec.iterations_completed,
                rec.peak_freq_mhz,
                rec.max_temp_c,
                rec.min_vcore_v,
                rec.max_vcore_v,
            ),
        )
        rec.id = cur.lastrowid
        return rec.id

    def update_core_result(
        self,
        result_id: int,
        *,
        finished_at: str | None = None,
        passed: bool | None = None,
        error_message: str | None = None,
        error_type: str | None = None,
        elapsed_seconds: float | None = None,
        iterations_completed: int | None = None,
        peak_freq_mhz: float | None = None,
        max_temp_c: float | None = None,
        min_vcore_v: float | None = None,
        max_vcore_v: float | None = None,
    ) -> None:
        sets: list[str] = []
        vals: list = []
        if finished_at is not None:
            sets.append("finished_at=?")
            vals.append(finished_at)
        if passed is not None:
            sets.append("passed=?")
            vals.append(int(passed))
        if error_message is not None:
            sets.append("error_message=?")
            vals.append(error_message)
        if error_type is not None:
            sets.append("error_type=?")
            vals.append(error_type)
        if elapsed_seconds is not None:
            sets.append("elapsed_seconds=?")
            vals.append(elapsed_seconds)
        if iterations_completed is not None:
            sets.append("iterations_completed=?")
            vals.append(iterations_completed)
        if peak_freq_mhz is not None:
            sets.append("peak_freq_mhz=?")
            vals.append(peak_freq_mhz)
        if max_temp_c is not None:
            sets.append("max_temp_c=?")
            vals.append(max_temp_c)
        if min_vcore_v is not None:
            sets.append("min_vcore_v=?")
            vals.append(min_vcore_v)
        if max_vcore_v is not None:
            sets.append("max_vcore_v=?")
            vals.append(max_vcore_v)
        if not sets:
            return
        vals.append(result_id)
        self.__conn.execute(
            f"UPDATE core_results SET {', '.join(sets)} WHERE id=?",
            vals,
        )

    def get_core_results(self, run_id: int) -> list[CoreResultRecord]:
        rows = self.__conn.execute(
            "SELECT * FROM core_results WHERE run_id=? ORDER BY cycle, core_id",
            (run_id,),
        ).fetchall()
        return [self._row_to_core_result(r) for r in rows]

    @staticmethod
    def _row_to_core_result(row: sqlite3.Row) -> CoreResultRecord:
        return CoreResultRecord(
            id=row["id"],
            run_id=row["run_id"],
            core_id=row["core_id"],
            ccd=row["ccd"],
            cycle=row["cycle"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            passed=None if row["passed"] is None else bool(row["passed"]),
            error_message=row["error_message"],
            error_type=row["error_type"],
            elapsed_seconds=row["elapsed_seconds"],
            iterations_completed=row["iterations_completed"],
            peak_freq_mhz=row["peak_freq_mhz"],
            max_temp_c=row["max_temp_c"],
            min_vcore_v=row["min_vcore_v"],
            max_vcore_v=row["max_vcore_v"],
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def insert_event(self, event: EventRecord) -> int:
        if not event.timestamp:
            event.timestamp = self._now_iso()
        cur = self.__conn.execute(
            """\
            INSERT INTO events (run_id, timestamp, event_type, core_id, message, details_json)
            VALUES (?,?,?,?,?,?)
            """,
            (
                event.run_id,
                event.timestamp,
                event.event_type,
                event.core_id,
                event.message,
                event.details_json,
            ),
        )
        event.id = cur.lastrowid
        return event.id

    def get_events(self, run_id: int, *, event_type: str | None = None) -> list[EventRecord]:
        if event_type:
            rows = self.__conn.execute(
                "SELECT * FROM events WHERE run_id=? AND event_type=? ORDER BY id",
                (run_id, event_type),
            ).fetchall()
        else:
            rows = self.__conn.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            id=row["id"],
            run_id=row["run_id"],
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            core_id=row["core_id"],
            message=row["message"],
            details_json=row["details_json"],
        )

    # ------------------------------------------------------------------
    # Telemetry
    # ------------------------------------------------------------------

    def insert_telemetry_batch(self, samples: list[TelemetrySample]) -> None:
        if not samples:
            return
        self.__conn.executemany(
            """\
            INSERT INTO telemetry_samples (run_id, core_id, timestamp, freq_mhz, effective_max_mhz, temp_c, vcore_v)
            VALUES (?,?,?,?,?,?,?)
            """,
            [
                (s.run_id, s.core_id, s.timestamp or self._now_iso(), s.freq_mhz, s.effective_max_mhz, s.temp_c, s.vcore_v)
                for s in samples
            ],
        )

    def get_telemetry(
        self, run_id: int, *, core_id: int | None = None
    ) -> list[TelemetrySample]:
        if core_id is not None:
            rows = self.__conn.execute(
                "SELECT * FROM telemetry_samples WHERE run_id=? AND core_id=? ORDER BY id",
                (run_id, core_id),
            ).fetchall()
        else:
            rows = self.__conn.execute(
                "SELECT * FROM telemetry_samples WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [
            TelemetrySample(
                id=r["id"],
                run_id=r["run_id"],
                core_id=r["core_id"],
                timestamp=r["timestamp"],
                freq_mhz=r["freq_mhz"],
                effective_max_mhz=r["effective_max_mhz"],
                temp_c=r["temp_c"],
                vcore_v=r["vcore_v"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Tuning contexts
    # ------------------------------------------------------------------

    def create_context(self, ctx: TuningContextRecord) -> int:
        """Insert a new tuning context. Returns the context id.

        Uses INSERT OR IGNORE to handle races with concurrent instances
        that may create the same (co_hash, bios_version) pair.
        """
        if not ctx.created_at:
            ctx.created_at = self._now_iso()
        cur = self.__conn.execute(
            """\
            INSERT OR IGNORE INTO tuning_contexts (
                created_at, bios_version, co_offsets_json, co_hash,
                pbo_scalar, boost_limit_mhz, notes
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                ctx.created_at,
                ctx.bios_version,
                ctx.co_offsets_json,
                ctx.co_hash,
                ctx.pbo_scalar,
                ctx.boost_limit_mhz,
                ctx.notes,
            ),
        )
        if cur.lastrowid and cur.rowcount > 0:
            ctx.id = cur.lastrowid
            return ctx.id
        # Row already existed (concurrent insert) — fetch it
        existing = self.get_context_by_hash(ctx.co_hash, ctx.bios_version)
        if existing:
            ctx.id = existing.id
            return existing.id
        # Fallback (should not happen)
        return cur.lastrowid

    def get_context(self, context_id: int) -> TuningContextRecord | None:
        row = self.__conn.execute(
            "SELECT * FROM tuning_contexts WHERE id=?", (context_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_context(row)

    def get_context_by_hash(
        self, co_hash: str, bios_version: str
    ) -> TuningContextRecord | None:
        """Find an existing context matching the given CO hash and BIOS version."""
        row = self.__conn.execute(
            "SELECT * FROM tuning_contexts WHERE co_hash=? AND bios_version=? LIMIT 1",
            (co_hash, bios_version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_context(row)

    def list_contexts(self, *, limit: int = 100) -> list[TuningContextRecord]:
        """List tuning contexts, newest first."""
        rows = self.__conn.execute(
            "SELECT * FROM tuning_contexts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_context(r) for r in rows]

    def update_context_notes(self, context_id: int, notes: str) -> None:
        self.__conn.execute(
            "UPDATE tuning_contexts SET notes=? WHERE id=?", (notes, context_id)
        )

    @staticmethod
    def _row_to_context(row: sqlite3.Row) -> TuningContextRecord:
        return TuningContextRecord(
            id=row["id"],
            created_at=row["created_at"],
            bios_version=row["bios_version"],
            co_offsets_json=row["co_offsets_json"],
            co_hash=row["co_hash"],
            pbo_scalar=row["pbo_scalar"],
            boost_limit_mhz=row["boost_limit_mhz"],
            notes=row["notes"],
        )

    # ------------------------------------------------------------------
    # Tuner sessions
    # ------------------------------------------------------------------

    def create_tuner_session(
        self,
        config_json: str,
        bios_version: str,
        cpu_model: str,
        context_id: int | None = None,
    ) -> int:
        """Create a new tuner session. Returns the session id."""
        now = self._now_iso()
        cur = self.__conn.execute(
            """\
            INSERT INTO tuner_sessions
                (created_at, updated_at, status, bios_version, cpu_model,
                 config_json, context_id, notes)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (now, now, "running", bios_version, cpu_model, config_json, context_id, ""),
        )
        return cur.lastrowid

    def update_tuner_session_status(self, session_id: int, status: str) -> None:
        self.__conn.execute(
            "UPDATE tuner_sessions SET status=?, updated_at=? WHERE id=?",
            (status, self._now_iso(), session_id),
        )

    def get_tuner_session(self, session_id: int) -> TunerSession | None:
        row = self.__conn.execute(
            "SELECT * FROM tuner_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tuner_session(row)

    def get_latest_tuner_session(self) -> TunerSession | None:
        row = self.__conn.execute(
            "SELECT * FROM tuner_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tuner_session(row)

    def get_active_tuner_session(self) -> TunerSession | None:
        row = self.__conn.execute(
            "SELECT * FROM tuner_sessions WHERE status IN ('running','paused','validating') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return self._row_to_tuner_session(row)

    def list_resumable_tuner_sessions(self) -> list[TunerSession]:
        """Return all tuner sessions that can be resumed (paused, running, or validating)."""
        rows = self.__conn.execute(
            "SELECT * FROM tuner_sessions WHERE status IN ('running','paused','validating') "
            "ORDER BY id DESC"
        ).fetchall()
        return [self._row_to_tuner_session(r) for r in rows]

    def list_tuner_sessions(self, *, limit: int = 100) -> list[TunerSession]:
        rows = self.__conn.execute(
            "SELECT * FROM tuner_sessions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._row_to_tuner_session(r) for r in rows]

    def upsert_tuner_core_state(self, session_id: int, cs: CoreState) -> None:
        now = self._now_iso()
        self.__conn.execute(
            """\
            INSERT INTO tuner_core_states
                (session_id, core_id, phase, current_offset, best_offset,
                 coarse_fail_offset, confirm_attempts, baseline_offset,
                 backoff_mode, consecutive_backoff_fails,
                 backoff_fail_bound, backoff_pass_bound, in_test, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(session_id, core_id) DO UPDATE SET
                phase=excluded.phase,
                current_offset=excluded.current_offset,
                best_offset=excluded.best_offset,
                coarse_fail_offset=excluded.coarse_fail_offset,
                confirm_attempts=excluded.confirm_attempts,
                baseline_offset=excluded.baseline_offset,
                backoff_mode=excluded.backoff_mode,
                consecutive_backoff_fails=excluded.consecutive_backoff_fails,
                backoff_fail_bound=excluded.backoff_fail_bound,
                backoff_pass_bound=excluded.backoff_pass_bound,
                in_test=excluded.in_test,
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
                cs.baseline_offset,
                int(cs.backoff_mode),
                cs.consecutive_backoff_fails,
                cs.backoff_fail_bound,
                cs.backoff_pass_bound,
                int(cs.in_test),
                now,
            ),
        )

    def get_tuner_core_states(self, session_id: int) -> dict[int, CoreState]:
        from tuner.state import CoreState as _CoreState

        rows = self.__conn.execute(
            "SELECT * FROM tuner_core_states WHERE session_id=? ORDER BY core_id",
            (session_id,),
        ).fetchall()
        result: dict[int, _CoreState] = {}
        for r in rows:
            result[r["core_id"]] = _CoreState(
                core_id=r["core_id"],
                phase=r["phase"],
                current_offset=r["current_offset"],
                best_offset=r["best_offset"],
                coarse_fail_offset=r["coarse_fail_offset"],
                confirm_attempts=r["confirm_attempts"],
                baseline_offset=r["baseline_offset"],
                backoff_mode=bool(r["backoff_mode"]),
                consecutive_backoff_fails=r["consecutive_backoff_fails"],
                backoff_fail_bound=r["backoff_fail_bound"],
                backoff_pass_bound=r["backoff_pass_bound"],
                in_test=bool(r["in_test"]) if "in_test" in r.keys() else False,
            )
        return result

    def insert_tuner_test_log(
        self,
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
        cur = self.__conn.execute(
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
                self._now_iso(),
            ),
        )
        return cur.lastrowid

    def get_tuner_test_log(
        self, session_id: int, core_id: int | None = None
    ) -> list[dict]:
        if core_id is not None:
            rows = self.__conn.execute(
                "SELECT * FROM tuner_test_log WHERE session_id=? AND core_id=? ORDER BY id",
                (session_id, core_id),
            ).fetchall()
        else:
            rows = self.__conn.execute(
                "SELECT * FROM tuner_test_log WHERE session_id=? ORDER BY id",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_tuner_best_profile(self, session_id: int) -> dict[int, int]:
        rows = self.__conn.execute(
            "SELECT core_id, best_offset FROM tuner_core_states "
            "WHERE session_id=? AND phase='confirmed' AND best_offset IS NOT NULL",
            (session_id,),
        ).fetchall()
        return {r["core_id"]: r["best_offset"] for r in rows}

    def delete_context_cascade(self, context_id: int) -> None:
        """Delete a tuning context and all associated runs and tuner sessions."""
        self.__conn.execute("DELETE FROM runs WHERE context_id=?", (context_id,))
        self.__conn.execute("DELETE FROM tuner_sessions WHERE context_id=?", (context_id,))
        self.__conn.execute("DELETE FROM tuning_contexts WHERE id=?", (context_id,))

    def get_status_counts(self) -> dict[str, int]:
        rows = self.__conn.execute(
            "SELECT status, COUNT(*) as cnt FROM runs GROUP BY status"
        ).fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    @staticmethod
    def _row_to_tuner_session(row: sqlite3.Row) -> TunerSession:
        from tuner.state import TunerSession as _TunerSession

        return _TunerSession(
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

    def _execute_raw(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Internal: raw SQL access for testing. Not for application code."""
        return self.__conn.execute(sql, params)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def delete_orphaned_contexts(self) -> int:
        """Delete tuning contexts that have no associated runs or tuner sessions."""
        cursor = self.__conn.execute(
            "DELETE FROM tuning_contexts WHERE id NOT IN "
            "(SELECT DISTINCT context_id FROM runs WHERE context_id IS NOT NULL) "
            "AND id NOT IN "
            "(SELECT DISTINCT context_id FROM tuner_sessions WHERE context_id IS NOT NULL)"
        )
        return cursor.rowcount

    def delete_tuner_session(self, session_id: int) -> None:
        """Delete a tuner session and all related records (CASCADE)."""
        self.__conn.execute("DELETE FROM tuner_sessions WHERE id=?", (session_id,))

    def recover_incomplete_runs(self) -> list[tuple[int, str]]:
        """Mark any 'running' runs as 'crashed'. Returns list of (id, started_at) recovered."""
        stale = self.__conn.execute(
            "SELECT id, started_at FROM runs WHERE status='running'"
        ).fetchall()
        if stale:
            self.__conn.execute(
                "UPDATE runs SET status='crashed', finished_at=? WHERE status='running'",
                (self._now_iso(),),
            )
        return [(r["id"], r["started_at"]) for r in stale]

    def purge_before(self, iso_date: str) -> int:
        """Delete all runs started before the given ISO date. Returns count deleted."""
        cur = self.__conn.execute(
            "DELETE FROM runs WHERE started_at < ?",
            (iso_date,),
        )
        return cur.rowcount

    def vacuum(self) -> None:
        """Reclaim space after bulk deletes."""
        self.__conn.execute("VACUUM")

    def close(self) -> None:
        self.__conn.close()
