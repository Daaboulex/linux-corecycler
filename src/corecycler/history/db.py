"""Crash-safe test history database using SQLite WAL mode.

WAL + synchronous=FULL syncs every commit before it returns, so a committed
row survives a power loss or a freeze, not only kill -9 and OOM. SQLite's
automatic PASSIVE checkpoint bounds the WAL; Python cannot read its result, so
close() runs one more PASSIVE checkpoint and logs that.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
from compression import zstd
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from corecycler.config.paths import data_dir, hand_back

if TYPE_CHECKING:
    from collections.abc import Iterator

DEFAULT_DB_PATH = data_dir() / "history" / "history.db"

LEGACY_ROOT_DB = Path("/root/.local/share/corecycler/history/history.db")

log = logging.getLogger(__name__)

SAMPLE_BLOCK_S = 10.0
_SECOND_WRITER_WAIT_S = 5.0
INCREMENTAL_VACUUM = 2
SAMPLE_BLOCK_OPTIONS = {zstd.CompressionParameter.compression_level: 19, zstd.CompressionParameter.checksum_flag: 1}
SAMPLE_LISTS = ("clocks", "tccd", "volts", "watts", "cores")
SAMPLE_COLUMNS = tuple((name, name) for name in SAMPLE_LISTS)
OLD_TUNER_TABLES = ("tuner_core_states", "tuner_test_log", "tuner_events", "tuner_co_journal")


def _pack_samples(samples: list[dict]) -> bytes:
    lines = "\n".join(json.dumps(sample, separators=(",", ":")) for sample in samples)
    return zstd.compress(lines.encode(), options=SAMPLE_BLOCK_OPTIONS)


def _unpack_samples(data: bytes) -> list[dict]:
    return [json.loads(line) for line in zstd.decompress(data).decode().splitlines()]


def _tail_sample(row: sqlite3.Row, columns: tuple[tuple[str, str], ...] = SAMPLE_COLUMNS) -> dict:
    sample = {"mono_ns": row["mono_ns"], "running": bool(row["running"]), "tctl": row["tctl"]}
    return sample | {name: json.loads(row[column]) for name, column in columns}


class SchemaTooNewError(RuntimeError):
    def __init__(self, path: Path, found: int, supported: int) -> None:
        super().__init__(
            f"History database {path} has schema version {found}; this build reads up to version {supported}. "
            "A newer corecycler wrote it: run that build again, or move the file aside to start a new history."
        )


class CorruptSessionError(ValueError):
    def __init__(self, session_id: int, detail: str) -> None:
        super().__init__(f"session {session_id} is unreadable: {detail}")
        self.detail = detail


def _newest(limit: int | None) -> tuple[str, tuple[int, ...]]:
    return ("", ()) if limit is None else (" LIMIT ?", (limit,))


def adopt_legacy_root_db(db: HistoryDB, root_db: Path = LEGACY_ROOT_DB) -> dict[str, int] | None:
    """One-time adoption of a root-owned history database.

    Sudo runs may have left history under /root; this merges that data into
    the user's (single) database and renames the source ``*.adopted`` so it
    can never be merged twice or silently diverge again. Only possible when
    running as root — the file is unreadable otherwise. Returns the merge
    counts, or None when there was nothing to adopt.
    """
    if os.geteuid() != 0:
        return None
    try:
        if not root_db.exists():
            return None
        if root_db.resolve() == db._db_path.resolve():
            return None
    except OSError:
        return None
    counts = db.merge_from(root_db)
    root_db.replace(root_db.with_name(root_db.name + ".adopted"))
    for sidecar in ("-wal", "-shm"):
        leftover = root_db.with_name(root_db.name + sidecar)
        if leftover.exists():
            leftover.unlink()
    log.info("Adopted legacy root history database %s: %s", root_db, counts)
    return counts


@dataclass(slots=True)
class RunRecord:
    id: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    status: Literal["running", "completed", "stopped", "crashed"] = "running"
    cpu_model: str = ""
    physical_cores: int = 0
    logical_cpus: int = 0
    ccds: int = 0
    is_x3d: bool = False
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
    context_id: int | None = None
    bios_version: str = ""
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
    passed: bool | None = None
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
    timestamp: str = ""
    event_type: Literal[
        "", "core_start", "core_finish", "error", "phase_change", "thermal", "stall", "cycle", "info"
    ] = ""
    core_id: int | None = None
    message: str = ""
    details_json: str | None = None


@dataclass(frozen=True, slots=True)
class MachineSessionRecord:
    id: int
    status: str
    created_at: str
    updated_at: str
    cpu_model: str
    bios_version: str
    parent_session_id: int | None
    origin: str | None
    last_event: str | None


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
    ppt_limit_w: float | None = None
    tdc_limit_a: float | None = None
    edc_limit_a: float | None = None


@dataclass(slots=True)
class TelemetrySample:
    run_id: int = 0
    core_id: int = 0
    timestamp: str = ""
    freq_mhz: float | None = None
    effective_max_mhz: float | None = None
    temp_c: float | None = None
    vcore_v: float | None = None
    pm_clock_mhz: float | None = None
    pm_effective_mhz: float | None = None
    pm_c0_pct: float | None = None
    pm_volts: float | None = None


@dataclass(frozen=True, slots=True)
class JournalRow:
    id: int
    session_id: int
    core_id: int
    value: int
    boot_id: str
    mono_ns: int | None
    confirmed: bool
    survived: bool
    retracted: bool
    written_at: str


@dataclass(frozen=True, slots=True)
class CoRead:
    core_id: int
    value: int
    mono_ns: int
    confirmed: bool = True


class HistoryDB:
    """Crash-safe SQLite database for test run history."""

    SCHEMA_VERSION = 33

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._db_path = Path(db_path)
        self._closed = False
        if str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self.__conn = sqlite3.connect(str(self._db_path), autocommit=True, timeout=_SECOND_WRITER_WAIT_S)
        try:
            self.__conn.row_factory = sqlite3.Row
            mode = self.__conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode not in ("wal", "memory"):
                raise RuntimeError(
                    f"History database {self._db_path} cannot use write-ahead logging (journal mode {mode}), "
                    f"so a commit is not durable before the hardware write it guards"
                )
            self.__conn.execute("PRAGMA synchronous=FULL")
            self.__conn.execute("PRAGMA foreign_keys=ON")
            self.__conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            check = self.__conn.execute("PRAGMA quick_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError(
                    f"History database failed integrity check ({check}): {self._db_path}. "
                    f"Move the file aside and restart to rebuild."
                )
            self._create_schema()
            self._shrink()
        except BaseException:
            self.__conn.close()
            raise
        if str(self._db_path) != ":memory:":
            self._hand_back_files()

    def _hand_back_files(self) -> None:
        for path in (self._db_path, *(self._db_path.with_name(self._db_path.name + end) for end in ("-wal", "-shm"))):
            hand_back(path)

    def _shrink(self) -> None:
        conn = self.__conn
        if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != INCREMENTAL_VACUUM:
            log.info("History database %s moves to incremental vacuum with one full VACUUM", self._db_path)
            conn.execute("VACUUM")
        conn.execute("PRAGMA incremental_vacuum").fetchall()

    def _create_schema(self) -> None:
        cur = self.__conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
        if cur.fetchone() is None:
            with self.transaction() as conn:
                conn.executescript(self._DDL_FRESH)
            return

        version = self.__conn.execute("SELECT version FROM schema_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise SchemaTooNewError(self._db_path, version, self.SCHEMA_VERSION)
        for target_version in range(version + 1, self.SCHEMA_VERSION + 1):
            migration = self._MIGRATIONS.get(target_version)
            if migration is None:
                raise RuntimeError(f"Missing migration for version {target_version}")
            with self.transaction() as conn:
                if callable(migration):
                    migration(conn)
                else:
                    conn.executescript(migration)
                if not conn.in_transaction:
                    raise RuntimeError(f"Migration to version {target_version} ended its own transaction")
                conn.execute("UPDATE schema_version SET version=?", (target_version,))

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.__conn
        if conn.in_transaction:
            raise RuntimeError("HistoryDB.transaction() does not nest")
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if not conn.in_transaction:
                raise RuntimeError("The transaction ended before its commit")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    _DDL_TUNER_TESTS = """\
CREATE TABLE IF NOT EXISTS tuner_tests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    boot_id         TEXT    NOT NULL,
    token           INTEGER NOT NULL,
    started_at      TEXT    NOT NULL,
    stage           TEXT    NOT NULL,
    load            TEXT,
    klass           TEXT    NOT NULL,
    core            INTEGER NOT NULL,
    value           INTEGER NOT NULL,
    vector_json     TEXT    NOT NULL,
    cpus_json       TEXT    NOT NULL,
    seconds_asked   REAL    NOT NULL,
    ended_at        TEXT,
    verdict         TEXT,
    seconds_run     REAL,
    named_json      TEXT,
    context_json    TEXT,
    evidence_json   TEXT,
    detail          TEXT,
    speeds_json     TEXT,
    power_json      TEXT,
    output          TEXT,
    peaks_json      TEXT,
    coverage_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tuner_tests_session ON tuner_tests(session_id, core);
"""

    _DDL_TUNER_SAMPLES = """\
CREATE TABLE IF NOT EXISTS tuner_samples (
    id              INTEGER PRIMARY KEY,
    test_id         INTEGER NOT NULL REFERENCES tuner_tests(id) ON DELETE CASCADE,
    mono_ns         INTEGER NOT NULL,
    running         INTEGER NOT NULL,
    tctl            REAL,
    clocks          TEXT    NOT NULL,
    tccd            TEXT    NOT NULL,
    volts           TEXT    NOT NULL,
    watts           TEXT    NOT NULL DEFAULT '[]',
    cores           TEXT    NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS tuner_sample_blocks (
    id              INTEGER PRIMARY KEY,
    test_id         INTEGER NOT NULL REFERENCES tuner_tests(id) ON DELETE CASCADE,
    data            BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_sample_blocks_test ON tuner_sample_blocks(test_id, id);
"""

    _DDL_TUNER_BOOTS = """\
CREATE TABLE IF NOT EXISTS tuner_boots (
    boot_id            TEXT    PRIMARY KEY,
    first_seen         TEXT    NOT NULL,
    bios_version       TEXT    NOT NULL,
    fclk_mhz           REAL,
    uclk_mhz           REAL,
    mclk_mhz           REAL,
    reset_reasons_json TEXT    NOT NULL,
    igpu_json          TEXT    NOT NULL
);
"""

    _DDL_TUNER_CO_READS = """\
CREATE TABLE IF NOT EXISTS tuner_co_reads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id     INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    boot_id     TEXT    NOT NULL,
    mono_ns     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_co_reads_boot ON tuner_co_reads(boot_id);
"""

    _DDL_TELEMETRY_BLOCKS = """\
CREATE TABLE IF NOT EXISTS telemetry_blocks (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    data            BLOB    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_telemetry_blocks_run ON telemetry_blocks(run_id, id);
"""

    _DDL_FRESH = (
        """\
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version (version) VALUES (__SCHEMA_VERSION__);

CREATE TABLE IF NOT EXISTS tuning_contexts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL,
    bios_version    TEXT    NOT NULL DEFAULT '',
    co_offsets_json TEXT    NOT NULL DEFAULT '{}',
    co_hash         TEXT    NOT NULL DEFAULT '',
    pbo_scalar      REAL,
    boost_limit_mhz INTEGER,
    notes           TEXT    NOT NULL DEFAULT '',
    ppt_limit_w     REAL,
    tdc_limit_a     REAL,
    edc_limit_a     REAL,
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
    max_vcore_v      REAL,
    output           TEXT
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

CREATE TABLE IF NOT EXISTS tuner_sessions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'running',
    bios_version        TEXT    NOT NULL DEFAULT '',
    cpu_model           TEXT    NOT NULL DEFAULT '',
    config_json         TEXT    NOT NULL DEFAULT '{}',
    context_id          INTEGER REFERENCES tuning_contexts(id),
    resume_crash_streak INTEGER NOT NULL DEFAULT 0,
    notes               TEXT    NOT NULL DEFAULT '',
    unattributed_crashes INTEGER NOT NULL DEFAULT 0,
    hunting_core        INTEGER,
    validation_stage    INTEGER NOT NULL DEFAULT 0,
    validation_index    INTEGER NOT NULL DEFAULT 0,
    validation_half     INTEGER NOT NULL DEFAULT 0,
    validation_dirty    INTEGER NOT NULL DEFAULT 0,
    validation_requeue  TEXT    NOT NULL DEFAULT '[]',
    boot_id             TEXT    NOT NULL DEFAULT '',
    kernel_read_ns      INTEGER,
    machine_json        TEXT,
    parent_session_id   INTEGER REFERENCES tuner_sessions(id),
    origin              TEXT
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
    crash_count         INTEGER NOT NULL DEFAULT 0,
    crash_cooldown      INTEGER NOT NULL DEFAULT 0,
    thermal_aborts      INTEGER NOT NULL DEFAULT 0,
    cumulative_test_time REAL   NOT NULL DEFAULT 0.0,
    hardening_tier_index INTEGER NOT NULL DEFAULT 0,
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
    backend             TEXT,
    stress_mode         TEXT,
    fft_preset          TEXT,
    tested_at           TEXT    NOT NULL,
    peak_stretch_pct    REAL
);
CREATE INDEX IF NOT EXISTS idx_tuner_log_session ON tuner_test_log(session_id, core_id);

CREATE TABLE IF NOT EXISTS tuner_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    timestamp   TEXT    NOT NULL,
    boot_id     TEXT    NOT NULL DEFAULT '',
    severity    TEXT    NOT NULL DEFAULT 'info',
    message     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_events_session ON tuner_events(session_id);

CREATE TABLE IF NOT EXISTS tuner_co_journal (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id     INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    boot_id     TEXT    NOT NULL,
    mono_ns     INTEGER,
    confirmed   INTEGER NOT NULL DEFAULT 0,
    survived    INTEGER NOT NULL DEFAULT 0,
    retracted   INTEGER NOT NULL DEFAULT 0,
    written_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_co_journal_boot ON tuner_co_journal(session_id, boot_id, core_id, id);
CREATE INDEX IF NOT EXISTS idx_co_journal_proven ON tuner_co_journal(session_id, core_id, survived, retracted, value);
"""
        + _DDL_TUNER_TESTS
        + _DDL_TUNER_SAMPLES
        + _DDL_TELEMETRY_BLOCKS
        + _DDL_TUNER_BOOTS
        + _DDL_TUNER_CO_READS
    ).replace("__SCHEMA_VERSION__", str(SCHEMA_VERSION))

    _DDL_MIGRATE_V2_TABLES = """\
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
"""

    @staticmethod
    def _migrate_v2(conn: sqlite3.Connection) -> None:
        conn.executescript(HistoryDB._DDL_MIGRATE_V2_TABLES)
        HistoryDB._add_columns(
            conn,
            "runs",
            [
                ("context_id", "INTEGER REFERENCES tuning_contexts(id)"),
                ("bios_version", "TEXT NOT NULL DEFAULT ''"),
            ],
        )

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

    @staticmethod
    def _migrate_v4(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "telemetry_samples", [("effective_max_mhz", "REAL")])

    @staticmethod
    def _migrate_v5(conn: sqlite3.Connection) -> None:
        duplicates = (
            "SELECT id FROM tuning_contexts WHERE id NOT IN "
            "(SELECT MIN(id) FROM tuning_contexts GROUP BY co_hash, bios_version)"
        )
        for table in ("runs", "tuner_sessions"):
            conn.execute(
                f"UPDATE {table} SET context_id = (SELECT MIN(kept.id) FROM tuning_contexts kept "
                "JOIN tuning_contexts dup ON kept.co_hash = dup.co_hash AND kept.bios_version = dup.bios_version "
                f"WHERE dup.id = {table}.context_id) WHERE context_id IN ({duplicates})"
            )
        conn.execute(f"DELETE FROM tuning_contexts WHERE id IN ({duplicates})")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_context_unique_hash ON tuning_contexts(co_hash, bios_version)"
        )

    @staticmethod
    def _migrate_v6(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_core_states", [("baseline_offset", "INTEGER NOT NULL DEFAULT 0")])

    _DDL_MIGRATE_V7_COLUMNS = [
        ("backoff_mode", "INTEGER NOT NULL DEFAULT 0"),
        ("consecutive_backoff_fails", "INTEGER NOT NULL DEFAULT 0"),
        ("backoff_fail_bound", "INTEGER"),
        ("backoff_pass_bound", "INTEGER"),
    ]

    @staticmethod
    def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))

    @staticmethod
    def _add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
        for name, coldef in columns:
            if not HistoryDB._column_exists(conn, table, name):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coldef}")

    @staticmethod
    def _migrate_v7(conn: sqlite3.Connection) -> None:
        for col_name, col_def in HistoryDB._DDL_MIGRATE_V7_COLUMNS:
            if not HistoryDB._column_exists(conn, "tuner_core_states", col_name):
                conn.execute(f"ALTER TABLE tuner_core_states ADD COLUMN {col_name} {col_def}")

    @staticmethod
    def _migrate_v8(conn: sqlite3.Connection) -> None:
        if not HistoryDB._column_exists(conn, "tuner_core_states", "in_test"):
            conn.execute("ALTER TABLE tuner_core_states ADD COLUMN in_test INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _migrate_v9(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuner_core_states",
            [
                ("crash_count", "INTEGER DEFAULT 0"),
                ("crash_cooldown", "INTEGER DEFAULT 0"),
                ("cumulative_test_time", "REAL DEFAULT 0.0"),
                ("hardening_tier_index", "INTEGER DEFAULT 0"),
            ],
        )
        HistoryDB._add_columns(
            conn,
            "tuner_test_log",
            [("backend", "TEXT"), ("stress_mode", "TEXT"), ("fft_preset", "TEXT")],
        )

    @staticmethod
    def _migrate_v10(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_core_states", [("thermal_aborts", "INTEGER DEFAULT 0")])

    @staticmethod
    def _migrate_v11(conn: sqlite3.Connection) -> None:
        if not HistoryDB._column_exists(conn, "tuner_sessions", "resume_crash_streak"):
            conn.execute("ALTER TABLE tuner_sessions ADD COLUMN resume_crash_streak INTEGER NOT NULL DEFAULT 0")
        conn.executescript(
            """\
CREATE TABLE IF NOT EXISTS tuner_co_journal (
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id     INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    survived    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);
"""
        )

    _DDL_MIGRATE_V12 = """\
CREATE TABLE tuner_core_states_v12 (
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
    crash_count         INTEGER NOT NULL DEFAULT 0,
    crash_cooldown      INTEGER NOT NULL DEFAULT 0,
    thermal_aborts      INTEGER NOT NULL DEFAULT 0,
    cumulative_test_time REAL   NOT NULL DEFAULT 0.0,
    hardening_tier_index INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT    NOT NULL,
    UNIQUE(session_id, core_id)
);
INSERT INTO tuner_core_states_v12 (
    id, session_id, core_id, phase, current_offset, best_offset,
    coarse_fail_offset, confirm_attempts, baseline_offset, backoff_mode,
    consecutive_backoff_fails, backoff_fail_bound, backoff_pass_bound,
    in_test, crash_count, crash_cooldown, thermal_aborts,
    cumulative_test_time, hardening_tier_index, updated_at
)
SELECT
    id, session_id, core_id, phase, current_offset, best_offset,
    coarse_fail_offset, confirm_attempts, baseline_offset,
    COALESCE(backoff_mode, 0), COALESCE(consecutive_backoff_fails, 0),
    backoff_fail_bound, backoff_pass_bound, COALESCE(in_test, 0),
    COALESCE(crash_count, 0), COALESCE(crash_cooldown, 0),
    COALESCE(thermal_aborts, 0), COALESCE(cumulative_test_time, 0.0),
    COALESCE(hardening_tier_index, 0), updated_at
FROM tuner_core_states;
DROP TABLE tuner_core_states;
ALTER TABLE tuner_core_states_v12 RENAME TO tuner_core_states;
"""

    @staticmethod
    def _migrate_v13(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuning_contexts",
            [("ppt_limit_w", "REAL"), ("tdc_limit_a", "REAL"), ("edc_limit_a", "REAL")],
        )
        HistoryDB._add_columns(
            conn,
            "tuner_sessions",
            [
                ("unattributed_crashes", "INTEGER NOT NULL DEFAULT 0"),
                ("hunting_core", "INTEGER"),
            ],
        )
        HistoryDB._add_columns(conn, "tuner_test_log", [("peak_stretch_pct", "REAL")])

    @staticmethod
    def _migrate_v14(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuner_sessions",
            [
                ("validation_stage", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_index", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_half", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_dirty", "INTEGER NOT NULL DEFAULT 0"),
                ("validation_requeue", "TEXT NOT NULL DEFAULT '[]'"),
            ],
        )

    _DDL_MIGRATE_V15 = """\
CREATE TABLE IF NOT EXISTS tuner_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    timestamp   TEXT    NOT NULL,
    boot_id     TEXT    NOT NULL DEFAULT '',
    severity    TEXT    NOT NULL DEFAULT 'info',
    message     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuner_events_session ON tuner_events(session_id);
"""

    @staticmethod
    def _migrate_v16(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_sessions", [("boot_id", "TEXT NOT NULL DEFAULT ''")])
        conn.execute(HistoryDB._SESSION_BOOT_FROM_EVENTS)

    _SESSION_BOOT_FROM_EVENTS = """\
UPDATE tuner_sessions SET boot_id = COALESCE((
    SELECT replace(e.boot_id, '-', '') FROM tuner_events e
    WHERE e.session_id = tuner_sessions.id AND e.boot_id <> ''
      AND e.timestamp <= (
          SELECT MAX(ts) FROM (
              SELECT tuner_sessions.updated_at AS ts
              UNION ALL SELECT updated_at FROM tuner_core_states WHERE session_id = tuner_sessions.id
              UNION ALL SELECT updated_at FROM tuner_co_journal WHERE session_id = tuner_sessions.id
              UNION ALL SELECT tested_at FROM tuner_test_log WHERE session_id = tuner_sessions.id
          )
      )
    ORDER BY e.id DESC LIMIT 1), '')
WHERE boot_id = ''
"""

    @staticmethod
    def _migrate_v17(conn: sqlite3.Connection) -> None:
        if not HistoryDB._column_exists(conn, "tuner_co_journal", "id"):
            conn.executescript(HistoryDB._DDL_MIGRATE_V17_JOURNAL)
        HistoryDB._add_columns(conn, "tuner_sessions", [("kernel_read_ns", "INTEGER")])

    _DDL_MIGRATE_V17_JOURNAL = """\
CREATE TABLE tuner_co_journal_v17 (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES tuner_sessions(id) ON DELETE CASCADE,
    core_id     INTEGER NOT NULL,
    value       INTEGER NOT NULL,
    boot_id     TEXT    NOT NULL,
    mono_ns     INTEGER,
    confirmed   INTEGER NOT NULL DEFAULT 0,
    survived    INTEGER NOT NULL DEFAULT 0,
    retracted   INTEGER NOT NULL DEFAULT 0,
    written_at  TEXT    NOT NULL
);
INSERT INTO tuner_co_journal_v17
    (session_id, core_id, value, boot_id, mono_ns, confirmed, survived, retracted, written_at)
SELECT j.session_id, j.core_id, j.value, s.boot_id, NULL, 0,
       j.survived = 1 AND EXISTS (
           SELECT 1 FROM tuner_test_log t
           WHERE t.session_id = j.session_id AND t.core_id = j.core_id AND t.offset_tested = j.value
             AND t.passed = 1 AND t.duration_seconds IS NOT NULL),
       0, j.updated_at
FROM tuner_co_journal j JOIN tuner_sessions s ON s.id = j.session_id
ORDER BY j.updated_at, j.session_id, j.core_id;
DROP TABLE tuner_co_journal;
ALTER TABLE tuner_co_journal_v17 RENAME TO tuner_co_journal;
CREATE INDEX idx_co_journal_boot ON tuner_co_journal(session_id, boot_id, core_id, id);
CREATE INDEX idx_co_journal_proven ON tuner_co_journal(session_id, core_id, survived, retracted);
"""

    _DDL_MIGRATE_V19 = """\
DROP INDEX IF EXISTS idx_co_journal_proven;
CREATE INDEX idx_co_journal_proven ON tuner_co_journal(session_id, core_id, survived, retracted, value);
"""

    @staticmethod
    def _migrate_v18(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_sessions", [("machine_json", "TEXT")])
        conn.executescript(HistoryDB._DDL_TUNER_TESTS)

    @staticmethod
    def _migrate_v21(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_tests", [("speeds_json", "TEXT")])

    @staticmethod
    def _migrate_v22(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_tests", [("power_json", "TEXT")])

    @staticmethod
    def _migrate_v23(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_tests", [("output", "TEXT")])
        HistoryDB._add_columns(conn, "core_results", [("output", "TEXT")])

    @staticmethod
    def _migrate_v24(conn: sqlite3.Connection) -> None:
        if HistoryDB._column_exists(conn, "tuner_samples", "clocks_json"):
            conn.execute("ALTER TABLE tuner_samples RENAME TO tuner_samples_v23")
            conn.executescript(HistoryDB._DDL_TUNER_SAMPLES)
            lists = tuple((name, f"{name}_json") for name in ("clocks", "tccd", "volts"))
            tests = [row[0] for row in conn.execute("SELECT DISTINCT test_id FROM tuner_samples_v23 ORDER BY test_id")]
            for test_id in tests:
                taken = conn.execute("SELECT * FROM tuner_samples_v23 WHERE test_id=? ORDER BY id", (test_id,))
                block = _pack_samples([_tail_sample(row, lists) for row in taken.fetchall()])
                conn.execute("INSERT INTO tuner_sample_blocks (test_id, data) VALUES (?,?)", (test_id, block))
            conn.execute("DROP TABLE tuner_samples_v23")
        conn.executescript(HistoryDB._DDL_TUNER_SAMPLES)

    @staticmethod
    def _migrate_v25(conn: sqlite3.Connection) -> None:
        conn.executescript(HistoryDB._DDL_TELEMETRY_BLOCKS)
        if not HistoryDB._column_exists(conn, "telemetry_samples", "core_id"):
            return
        v24_columns = ("core_id", "timestamp", "freq_mhz", "effective_max_mhz", "temp_c", "vcore_v")
        runs = [row[0] for row in conn.execute("SELECT DISTINCT run_id FROM telemetry_samples ORDER BY run_id")]
        for run_id in runs:
            taken = conn.execute("SELECT * FROM telemetry_samples WHERE run_id=? ORDER BY id", (run_id,))
            block = _pack_samples([{name: row[name] for name in v24_columns} for row in taken.fetchall()])
            conn.execute("INSERT INTO telemetry_blocks (run_id, data) VALUES (?,?)", (run_id, block))
        conn.execute("DROP TABLE telemetry_samples")

    @staticmethod
    def _migrate_v27(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(
            conn,
            "tuner_sessions",
            [("parent_session_id", "INTEGER REFERENCES tuner_sessions(id)"), ("origin", "TEXT")],
        )

    @staticmethod
    def _migrate_v28(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_tests", [("peaks_json", "TEXT")])

    @staticmethod
    def _migrate_v29(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_tests", [("coverage_json", "TEXT")])

    @staticmethod
    def _migrate_v30(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_samples", [("watts", "TEXT NOT NULL DEFAULT '[]'")])

    @staticmethod
    def _migrate_v31(conn: sqlite3.Connection) -> None:
        HistoryDB._add_columns(conn, "tuner_samples", [("cores", "TEXT NOT NULL DEFAULT '[]'")])

    @staticmethod
    def _migrate_v32(conn: sqlite3.Connection) -> None:
        """Nothing moves: telemetry samples gained the tested core's PM readings, which a schema-31 reader cannot
        load, so the version rises and an older build refuses the database rather than failing on a sample."""

    _MIGRATIONS: dict[int, str | callable] = {
        2: _migrate_v2,
        3: _DDL_MIGRATE_V3,
        4: _migrate_v4,
        5: _migrate_v5,
        6: _migrate_v6,
        7: _migrate_v7,
        8: _migrate_v8,
        9: _migrate_v9,
        10: _migrate_v10,
        11: _migrate_v11,
        12: _DDL_MIGRATE_V12,
        13: _migrate_v13,
        14: _migrate_v14,
        15: _DDL_MIGRATE_V15,
        16: _migrate_v16,
        17: _migrate_v17,
        18: _migrate_v18,
        19: _DDL_MIGRATE_V19,
        20: _DDL_TUNER_SAMPLES,
        21: _migrate_v21,
        22: _migrate_v22,
        23: _migrate_v23,
        24: _migrate_v24,
        25: _migrate_v25,
        26: _DDL_TUNER_BOOTS,
        27: _migrate_v27,
        28: _migrate_v28,
        29: _migrate_v29,
        30: _migrate_v30,
        31: _migrate_v31,
        32: _migrate_v32,
        33: _DDL_TUNER_CO_READS,
    }

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

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

    def list_runs(self) -> list[RunRecord]:
        rows = self.__conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchall()
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
        output: str | None = None,
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
        if output is not None:
            sets.append("output=?")
            vals.append(output)
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

    def insert_telemetry_block(self, samples: list[TelemetrySample]) -> None:
        runs = {sample.run_id for sample in samples}
        if len(runs) != 1:
            raise ValueError(f"a telemetry block holds the samples of exactly one run, not of {sorted(runs)}")
        if not all(sample.timestamp for sample in samples):
            raise ValueError("a telemetry sample has no timestamp")
        taken = [{k: v for k, v in asdict(sample).items() if k != "run_id"} for sample in samples]
        self.__conn.execute(
            "INSERT INTO telemetry_blocks (run_id, data) VALUES (?,?)", (runs.pop(), _pack_samples(taken))
        )

    def get_telemetry(self, run_id: int, *, core_id: int | None = None) -> list[TelemetrySample]:
        blocks = self.__conn.execute("SELECT data FROM telemetry_blocks WHERE run_id=? ORDER BY id", (run_id,))
        samples = (
            TelemetrySample(run_id=run_id, **taken) for (data,) in blocks.fetchall() for taken in _unpack_samples(data)
        )
        return [sample for sample in samples if core_id is None or sample.core_id == core_id]

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
                pbo_scalar, boost_limit_mhz, notes,
                ppt_limit_w, tdc_limit_a, edc_limit_a
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                ctx.created_at,
                ctx.bios_version,
                ctx.co_offsets_json,
                ctx.co_hash,
                ctx.pbo_scalar,
                ctx.boost_limit_mhz,
                ctx.notes,
                ctx.ppt_limit_w,
                ctx.tdc_limit_a,
                ctx.edc_limit_a,
            ),
        )
        if cur.lastrowid and cur.rowcount > 0:
            ctx.id = cur.lastrowid
            return ctx.id
        existing = self.get_context_by_hash(ctx.co_hash, ctx.bios_version)
        if existing:
            ctx.id = existing.id
            return existing.id
        raise RuntimeError(
            f"tuning_contexts insert was ignored but no row matches "
            f"(co_hash={ctx.co_hash!r}, bios={ctx.bios_version!r}) — database inconsistent"
        )

    def get_context(self, context_id: int) -> TuningContextRecord | None:
        row = self.__conn.execute("SELECT * FROM tuning_contexts WHERE id=?", (context_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_context(row)

    def get_context_by_hash(self, co_hash: str, bios_version: str) -> TuningContextRecord | None:
        """Find an existing context matching the given CO hash and BIOS version."""
        row = self.__conn.execute(
            "SELECT * FROM tuning_contexts WHERE co_hash=? AND bios_version=? LIMIT 1",
            (co_hash, bios_version),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_context(row)

    def list_contexts(self, *, limit: int | None = None) -> list[TuningContextRecord]:
        """List tuning contexts, newest first: every one, or the newest `limit`."""
        clause, params = _newest(limit)
        rows = self.__conn.execute(f"SELECT * FROM tuning_contexts ORDER BY id DESC{clause}", params).fetchall()
        return [self._row_to_context(r) for r in rows]

    def update_context_notes(self, context_id: int, notes: str) -> None:
        self.__conn.execute("UPDATE tuning_contexts SET notes=? WHERE id=?", (notes, context_id))

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
            ppt_limit_w=row["ppt_limit_w"],
            tdc_limit_a=row["tdc_limit_a"],
            edc_limit_a=row["edc_limit_a"],
        )

    def next_tuner_session_id(self) -> int:
        row = self.__conn.execute(
            "SELECT MAX(COALESCE((SELECT seq FROM sqlite_sequence WHERE name='tuner_sessions'), 0),"
            " COALESCE((SELECT MAX(id) FROM tuner_sessions), 0)) + 1"
        ).fetchone()
        return row[0]

    def save_machine_session(
        self,
        session_id: int,
        *,
        status: str,
        config_json: str,
        machine_json: str,
        boot_id: str,
        bios_version: str,
        cpu_model: str,
        context_id: int | None,
    ) -> None:
        now = self._now_iso()
        cur = self.__conn.execute(
            """\
            INSERT INTO tuner_sessions
                (id, created_at, updated_at, status, bios_version, cpu_model,
                 config_json, context_id, notes, boot_id, machine_json)
            VALUES (?,?,?,?,?,?,?,?,'',?,?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at=excluded.updated_at, status=excluded.status,
                kernel_read_ns=CASE WHEN tuner_sessions.boot_id = excluded.boot_id
                    THEN tuner_sessions.kernel_read_ns END,
                boot_id=excluded.boot_id, machine_json=excluded.machine_json
            WHERE tuner_sessions.machine_json IS NOT NULL
            """,
            (session_id, now, now, status, bios_version, cpu_model, config_json, context_id, boot_id, machine_json),
        )
        if cur.rowcount != 1:
            raise sqlite3.IntegrityError(f"session {session_id} belongs to the old engine")

    def list_machine_sessions(self, limit: int | None = None) -> list[MachineSessionRecord]:
        clause, params = _newest(limit)
        rows = self.__conn.execute(
            f"""\
            SELECT s.id, s.status, s.created_at, s.updated_at, s.cpu_model, s.bios_version, s.parent_session_id,
                s.origin,
                (SELECT e.message FROM tuner_events e WHERE e.session_id = s.id ORDER BY e.id DESC LIMIT 1)
                    AS last_event
            FROM tuner_sessions s WHERE s.machine_json IS NOT NULL ORDER BY s.id DESC{clause}
            """,
            params,
        ).fetchall()
        return [MachineSessionRecord(**dict(row)) for row in rows]

    _TUNER_TEST_COLUMNS = (
        "id, boot_id, started_at, ended_at, stage, load, klass, core, value, vector_json, cpus_json, seconds_asked,"
        " seconds_run, verdict, detail, coverage_json, power_json, peaks_json, evidence_json"
    )

    def list_tuner_tests(self, session_id: int) -> list[dict]:
        rows = self.__conn.execute(
            f"SELECT {self._TUNER_TEST_COLUMNS} FROM tuner_tests WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_every_tuner_test(self) -> list[dict]:
        rows = self.__conn.execute(f"SELECT {self._TUNER_TEST_COLUMNS} FROM tuner_tests ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def session_boots(self, session_id: int) -> list[dict]:
        rows = self.__conn.execute(
            "SELECT * FROM tuner_boots WHERE boot_id IN (SELECT boot_id FROM tuner_tests WHERE session_id=?"
            " UNION SELECT boot_id FROM tuner_co_journal WHERE session_id=?) ORDER BY first_seen",
            (session_id, session_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def link_session(self, child_id: int, parent_id: int, origin: str) -> None:
        cur = self.__conn.execute(
            "UPDATE tuner_sessions SET parent_session_id=?, origin=? WHERE id=?", (parent_id, origin, child_id)
        )
        if cur.rowcount != 1:
            raise sqlite3.IntegrityError(f"session {child_id} is missing, so it cannot be linked to {parent_id}")

    def abort_held_session(self, session_id: int) -> None:
        cur = self.__conn.execute(
            "UPDATE tuner_sessions SET status='aborted', updated_at=?"
            " WHERE id=? AND machine_json IS NOT NULL AND status IN ('running', 'paused')",
            (self._now_iso(), session_id),
        )
        if cur.rowcount != 1:
            raise sqlite3.IntegrityError(f"session {session_id} is no longer running or paused")

    def journal_inherit(self, session_id: int, proven: dict[int, int]) -> None:
        self.__conn.executemany(
            """\
            INSERT INTO tuner_co_journal
                (session_id, core_id, value, boot_id, mono_ns, confirmed, survived, retracted, written_at)
            VALUES (?,?,?,'',NULL,1,1,0,?)
            """,
            [(session_id, core, value, self._now_iso()) for core, value in sorted(proven.items())],
        )

    def load_machine_session(self, session_id: int) -> tuple[str, str, int | None, str | None] | None:
        row = self.__conn.execute(
            "SELECT machine_json, boot_id, kernel_read_ns, origin FROM tuner_sessions"
            " WHERE id=? AND machine_json IS NOT NULL",
            (session_id,),
        ).fetchone()
        return None if row is None else (row["machine_json"], row["boot_id"], row["kernel_read_ns"], row["origin"])

    def insert_tuner_test(
        self,
        session_id: int,
        boot_id: str,
        token: int,
        stage: str,
        load: str | None,
        klass: str,
        core: int,
        value: int,
        vector_json: str,
        cpus_json: str,
        seconds_asked: float,
        power_json: str,
    ) -> int:
        cur = self.__conn.execute(
            """\
            INSERT INTO tuner_tests
                (session_id, boot_id, token, started_at, stage, load, klass, core, value,
                 vector_json, cpus_json, seconds_asked, power_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session_id,
                boot_id,
                token,
                self._now_iso(),
                stage,
                load,
                klass,
                core,
                value,
                vector_json,
                cpus_json,
                seconds_asked,
                power_json,
            ),
        )
        return cur.lastrowid

    def end_tuner_test(
        self,
        row_id: int,
        verdict: str,
        seconds_run: float | None,
        named_json: str,
        context_json: str | None,
        evidence_json: str,
        detail: str,
        speeds_json: str,
        output: str,
        *,
        peaks_json: str | None = None,
        coverage_json: str | None = None,
    ) -> None:
        cur = self.__conn.execute(
            """\
            UPDATE tuner_tests SET ended_at=?, verdict=?, seconds_run=?, named_json=?,
                context_json=?, evidence_json=?, detail=?, speeds_json=?, output=?, peaks_json=?,
                coverage_json=?
            WHERE id=? AND ended_at IS NULL
            """,
            (
                self._now_iso(),
                verdict,
                seconds_run,
                named_json,
                context_json,
                evidence_json,
                detail,
                speeds_json,
                output,
                peaks_json,
                coverage_json,
                row_id,
            ),
        )
        if cur.rowcount != 1:
            raise sqlite3.IntegrityError(f"test row {row_id} is missing or already ended")

    def insert_tuner_samples(self, test_id: int, samples: list[dict]) -> None:
        with self.transaction() as conn:
            conn.executemany(
                "INSERT INTO tuner_samples (test_id, mono_ns, running, tctl, clocks, tccd, volts, watts, cores)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        test_id,
                        sample["mono_ns"],
                        int(sample["running"]),
                        sample["tctl"],
                        *(json.dumps(sample[name]) for name in SAMPLE_LISTS),
                    )
                    for sample in samples
                ],
            )

    def insert_tuner_block(self, test_id: int, samples: list[dict]) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO tuner_sample_blocks (test_id, data) VALUES (?,?)", (test_id, _pack_samples(samples))
            )
            conn.execute("DELETE FROM tuner_samples WHERE test_id=?", (test_id,))

    def pack_tuner_tail(self, test_id: int) -> None:
        rows = self.__conn.execute("SELECT * FROM tuner_samples WHERE test_id=? ORDER BY id", (test_id,)).fetchall()
        if rows:
            self.insert_tuner_block(test_id, [_tail_sample(row) for row in rows])

    def prune_tuner_samples(self, verdict: str, ended_before: str) -> None:
        self.__conn.execute(
            "DELETE FROM tuner_sample_blocks WHERE test_id IN"
            " (SELECT id FROM tuner_tests WHERE verdict=? AND ended_at < ?)",
            (verdict, ended_before),
        )
        self._shrink()

    def insert_tuner_boot(
        self,
        boot_id: str,
        bios_version: str,
        fclk_mhz: float | None,
        uclk_mhz: float | None,
        mclk_mhz: float | None,
        reset_reasons_json: str,
        igpu_json: str,
    ) -> None:
        self.__conn.execute(
            "INSERT OR IGNORE INTO tuner_boots VALUES (?,?,?,?,?,?,?,?)",
            (boot_id, self._now_iso(), bios_version, fclk_mhz, uclk_mhz, mclk_mhz, reset_reasons_json, igpu_json),
        )

    def tuner_test_samples(self, test_id: int) -> list[dict]:
        blocks = self.__conn.execute("SELECT data FROM tuner_sample_blocks WHERE test_id=? ORDER BY id", (test_id,))
        tail = self.__conn.execute("SELECT * FROM tuner_samples WHERE test_id=? ORDER BY id", (test_id,))
        return [
            {name: [] for name in SAMPLE_LISTS} | sample
            for (data,) in blocks.fetchall()
            for sample in _unpack_samples(data)
        ] + [_tail_sample(row) for row in tail.fetchall()]

    def endurance_tally(self, session_id: int) -> tuple[int, int]:
        ended, failed = self.__conn.execute(
            "SELECT count(*), coalesce(sum(verdict = 'silicon'), 0) FROM tuner_tests"
            " WHERE session_id=? AND stage='endurance' AND ended_at IS NOT NULL",
            (session_id,),
        ).fetchone()
        return ended, failed

    def open_tuner_test(self, session_id: int, boot_id: str) -> dict | None:
        row = self.__conn.execute(
            "SELECT * FROM tuner_tests WHERE session_id=? AND boot_id=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (session_id, boot_id),
        ).fetchone()
        return None if row is None else dict(row)

    def set_kernel_read(self, session_id: int, kernel_read_ns: int) -> None:
        self.__conn.execute(
            "UPDATE tuner_sessions SET kernel_read_ns=MAX(COALESCE(kernel_read_ns, ?), ?), updated_at=? WHERE id=?",
            (kernel_read_ns, kernel_read_ns, self._now_iso(), session_id),
        )

    def journal_co_intent(
        self, session_id: int, core_id: int, value: int, survived: bool, *, boot_id: str, mono_ns: int
    ) -> int:
        """Record the CO value about to be made resident in the SMU, durably.

        Written BEFORE the hardware write so any hard crash (idle, baseline
        restore, post-test revert, validation, or search) is attributable to the
        exact (core, value) that was live at crash time. ``survived`` is True only
        when ``value`` is within the core's already-proven-safe envelope (0 is
        always safe); a value in new, more-aggressive territory is journaled
        un-survived until a test completes with it resident. The commit is
        synced before this returns, so the record is durable before the caller
        touches hardware.
        """
        cur = self.__conn.execute(
            """\
            INSERT INTO tuner_co_journal
                (session_id, core_id, value, boot_id, mono_ns, confirmed, survived, retracted, written_at)
            VALUES (?,?,?,?,?,0,?,0,?)
            """,
            (session_id, core_id, value, boot_id, mono_ns, int(survived), self._now_iso()),
        )
        return cur.lastrowid

    def journal_confirm(self, row_id: int) -> None:
        self.__conn.execute("UPDATE tuner_co_journal SET confirmed=1 WHERE id=?", (row_id,))

    def journal_mark_survived(
        self, session_id: int, *, boot_id: str, exclude_cores: tuple[int, ...] | list[int] = ()
    ) -> None:
        """Mark every resident CO value for the session as survived.

        Called after a test completes without a hard crash: the machine
        demonstrably ran with the whole resident offset vector and lived.
        Resident means each core's last write in this boot, and only once the
        SMU accepted it. ``exclude_cores`` keeps cores with fresh contrary
        evidence (a corrected MCE named them during this very test) un-survived.
        Surviving the test does not clear an error the hardware just reported.
        """
        marks = ",".join("?" * len(exclude_cores))
        self.__conn.execute(
            f"""\
            UPDATE tuner_co_journal SET survived=1
            WHERE id IN (
                SELECT MAX(id) FROM tuner_co_journal WHERE session_id=? AND boot_id=? GROUP BY core_id)
              AND confirmed=1 AND survived=0 AND core_id NOT IN ({marks})
            """,
            (session_id, boot_id, *exclude_cores),
        )

    def journal_proven(self, session_id: int) -> tuple[tuple[int, tuple[int, ...]], ...]:
        self.check_journal(session_id)
        rows = self.__conn.execute(
            "SELECT core_id, value FROM tuner_co_journal WHERE session_id=? AND survived=1 AND retracted=0 "
            "ORDER BY id DESC",
            (session_id,),
        ).fetchall()
        proven: dict[int, list[int]] = {}
        for row in rows:
            proven.setdefault(row["core_id"], []).append(row["value"])
        return tuple((core, tuple(values)) for core, values in sorted(proven.items()))

    def check_journal(self, session_id: int) -> None:
        lo, hi = self._CO_SANE_RANGE
        row = self.__conn.execute(
            "SELECT core_id, value FROM tuner_co_journal WHERE session_id=? "
            "AND (typeof(value) <> 'integer' OR value NOT BETWEEN ? AND ?) ORDER BY id LIMIT 1",
            (session_id, lo, hi),
        ).fetchone()
        if row is not None:
            raise CorruptSessionError(
                session_id,
                f"core {row['core_id']} has CO journal value {row['value']!r}, "
                f"not an integer in the sane CO range {lo}..{hi}",
            )
        stamp = self.__conn.execute(
            "SELECT core_id, mono_ns FROM tuner_co_journal WHERE session_id=? "
            "AND mono_ns IS NOT NULL AND typeof(mono_ns) <> 'integer' ORDER BY id LIMIT 1",
            (session_id,),
        ).fetchone()
        if stamp is not None:
            raise CorruptSessionError(
                session_id, f"core {stamp['core_id']} has CO journal stamp {stamp['mono_ns']!r}, not an integer"
            )
        floor = self.__conn.execute(
            "SELECT kernel_read_ns FROM tuner_sessions WHERE id=? "
            "AND kernel_read_ns IS NOT NULL AND typeof(kernel_read_ns) <> 'integer'",
            (session_id,),
        ).fetchone()
        if floor is not None:
            raise CorruptSessionError(
                session_id, f"the last kernel read stamp {floor['kernel_read_ns']!r} is not an integer"
            )

    def journal_rows(self, session_id: int, boot_id: str) -> list[JournalRow]:
        self.check_journal(session_id)
        rows = self.__conn.execute(
            "SELECT * FROM tuner_co_journal WHERE session_id=? AND boot_id=? ORDER BY id", (session_id, boot_id)
        ).fetchall()
        return [self._row_to_journal_row(r) for r in rows]

    def insert_co_reads(
        self, session_id: int, boot_id: str, mono_ns: int, reads: tuple[tuple[int, int | None], ...]
    ) -> None:
        self.__conn.executemany(
            "INSERT INTO tuner_co_reads (session_id, core_id, value, boot_id, mono_ns) VALUES (?,?,?,?,?)",
            [(session_id, core, value, boot_id, mono_ns) for core, value in reads if value is not None],
        )

    def co_reads(self, boot_id: str) -> list[CoRead]:
        bad = self.__conn.execute(
            "SELECT session_id, core_id, value, mono_ns FROM tuner_co_reads WHERE boot_id=? "
            "AND (typeof(value) <> 'integer' OR typeof(mono_ns) <> 'integer') ORDER BY id LIMIT 1",
            (boot_id,),
        ).fetchone()
        if bad is not None:
            raise CorruptSessionError(
                bad["session_id"], f"core {bad['core_id']} has CO read {bad['value']!r} at {bad['mono_ns']!r}"
            )
        rows = self.__conn.execute(
            "SELECT core_id, value, mono_ns FROM tuner_co_reads WHERE boot_id=? ORDER BY id", (boot_id,)
        ).fetchall()
        return [CoRead(row["core_id"], row["value"], row["mono_ns"]) for row in rows]

    def boot_co_journal(self, boot_id: str) -> list[JournalRow]:
        sessions = self.__conn.execute(
            "SELECT DISTINCT session_id FROM tuner_co_journal WHERE boot_id=? ORDER BY session_id", (boot_id,)
        ).fetchall()
        for row in sessions:
            self.check_journal(row["session_id"])
        rows = self.__conn.execute(
            "SELECT * FROM tuner_co_journal WHERE boot_id=? AND mono_ns IS NOT NULL ORDER BY id", (boot_id,)
        ).fetchall()
        return [self._row_to_journal_row(r) for r in rows]

    def journal_taken_over(self, session_id: int, boot_id: str) -> JournalRow | None:
        row = self.__conn.execute(
            """\
            SELECT * FROM tuner_co_journal
            WHERE boot_id=? AND session_id<>? AND id > (
                SELECT COALESCE(MAX(id), 0) FROM tuner_co_journal WHERE session_id=? AND boot_id=?)
            ORDER BY id LIMIT 1
            """,
            (boot_id, session_id, session_id, boot_id),
        ).fetchone()
        return None if row is None else self._row_to_journal_row(row)

    def journal_proven_values(self, session_id: int, direction: int) -> dict[int, int]:
        self.check_journal(session_id)
        rows = self.__conn.execute(
            """\
            SELECT core_id, ? * MAX(? * value) AS value FROM tuner_co_journal
            WHERE session_id=? AND survived=1 AND retracted=0 GROUP BY core_id
            """,
            (direction, direction, session_id),
        ).fetchall()
        return {r["core_id"]: r["value"] for r in rows}

    def journal_envelope(self, session_id: int, core_id: int) -> int | None:
        row = self.__conn.execute(
            "SELECT MIN(value) FROM tuner_co_journal WHERE session_id=? AND core_id=? AND survived=1 AND retracted=0",
            (session_id, core_id),
        ).fetchone()
        return row[0]

    def journal_retract(self, session_id: int, core_id: int, value: int, direction: int) -> None:
        self.__conn.execute(
            """\
            UPDATE tuner_co_journal SET retracted=1
            WHERE session_id=? AND core_id=? AND survived=1 AND retracted=0 AND ? * value >= ? * ?
            """,
            (session_id, core_id, direction, direction, value),
        )

    @staticmethod
    def _row_to_journal_row(row: sqlite3.Row) -> JournalRow:
        return JournalRow(
            id=row["id"],
            session_id=row["session_id"],
            core_id=row["core_id"],
            value=row["value"],
            boot_id=row["boot_id"],
            mono_ns=row["mono_ns"],
            confirmed=bool(row["confirmed"]),
            survived=bool(row["survived"]),
            retracted=bool(row["retracted"]),
            written_at=row["written_at"],
        )

    def insert_tuner_event(self, session_id: int, message: str, boot_id: str = "", severity: str = "info") -> None:
        self.__conn.execute(
            "INSERT INTO tuner_events (session_id, timestamp, boot_id, severity, message) VALUES (?,?,?,?,?)",
            (session_id, self._now_iso(), boot_id, severity, message),
        )

    def tuner_event_messages(self, session_id: int, severity: str) -> set[str]:
        rows = self.__conn.execute(
            "SELECT message FROM tuner_events WHERE session_id=? AND severity=?", (session_id, severity)
        ).fetchall()
        return {row["message"] for row in rows}

    def get_tuner_events(self, session_id: int, limit: int = 200) -> list[dict]:
        """Newest-last narrative lines for a session (the replayable story)."""
        rows = self.__conn.execute(
            "SELECT * FROM (SELECT * FROM tuner_events WHERE session_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (session_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    _CO_SANE_RANGE = (-100, 100)

    def context_sessions(self, context_id: int) -> list[int]:
        rows = self.__conn.execute(
            "SELECT id FROM tuner_sessions WHERE context_id=? ORDER BY id", (context_id,)
        ).fetchall()
        return [row["id"] for row in rows]

    def delete_context_cascade(self, context_id: int) -> None:
        """Delete a tuning context and its runs; refused while tuner sessions name the context."""
        held = self.context_sessions(context_id)
        if held:
            raise ValueError(f"setup {context_id} holds tuner sessions {held}")
        self.__conn.execute("DELETE FROM runs WHERE context_id=?", (context_id,))
        self.__conn.execute("DELETE FROM tuning_contexts WHERE id=?", (context_id,))

    def get_status_counts(self) -> dict[str, int]:
        rows = self.__conn.execute("SELECT status, COUNT(*) as cnt FROM runs GROUP BY status").fetchall()
        return {r["status"]: r["cnt"] for r in rows}

    def _execute_raw(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Internal: raw SQL access for testing. Not for application code."""
        return self.__conn.execute(sql, params)

    _MERGE_TABLES: tuple[tuple[str, dict[str, str]], ...] = (
        ("runs", {"context_id": "tuning_contexts"}),
        ("tuner_sessions", {"context_id": "tuning_contexts", "parent_session_id": "tuner_sessions"}),
        ("core_results", {"run_id": "runs"}),
        ("events", {"run_id": "runs"}),
        ("telemetry_blocks", {"run_id": "runs"}),
        ("tuner_core_states", {"session_id": "tuner_sessions"}),
        ("tuner_events", {"session_id": "tuner_sessions"}),
        ("tuner_test_log", {"session_id": "tuner_sessions", "run_id": "runs"}),
        ("tuner_co_journal", {"session_id": "tuner_sessions"}),
        ("tuner_co_reads", {"session_id": "tuner_sessions"}),
        ("tuner_tests", {"session_id": "tuner_sessions"}),
        ("tuner_samples", {"test_id": "tuner_tests"}),
        ("tuner_sample_blocks", {"test_id": "tuner_tests"}),
    )
    _MERGE_LINKS_KEPT_WHEN_UNMATCHED = frozenset({"run_id", "context_id", "parent_session_id"})

    def old_tuner_tables(self) -> dict[str, dict[str, list]]:
        sessions = "SELECT id FROM tuner_sessions WHERE machine_json IS NULL"
        queries = {"tuner_sessions": f"SELECT * FROM tuner_sessions WHERE id IN ({sessions}) ORDER BY id"} | {
            table: f"SELECT * FROM {table} WHERE session_id IN ({sessions}) ORDER BY id" for table in OLD_TUNER_TABLES
        }
        return {
            table: {
                "columns": [row["name"] for row in self.__conn.execute(f"PRAGMA main.table_info({table})")],
                "rows": [dict(row) for row in self.__conn.execute(query)],
            }
            for table, query in queries.items()
        }

    def _merged_columns(self, table: str) -> list[str]:
        return [row["name"] for row in self.__conn.execute(f"PRAGMA main.table_info({table})") if row["name"] != "id"]

    def merge_from(self, other_path: str | Path) -> dict[str, int]:
        """Adopt every record from another corecycler history database.

        Merges a database left elsewhere (e.g. under /root by sudo runs):
        the source is first opened through HistoryDB (migrating it to the
        current schema, however old it is), then every run, tuning context and
        tuner session is copied in with fresh ids and remapped references.
        Every column a table has is copied, so a column added later is never
        left behind. Tuning contexts deduplicate by (co_hash, bios_version).
        The source file is not modified beyond its schema migration.
        All-or-nothing: one transaction, rolled back on any error.
        """
        other_path = Path(other_path)
        HistoryDB(other_path).close()
        conn = self.__conn
        conn.execute("ATTACH DATABASE ? AS src", (str(other_path),))
        counts = {"contexts": 0, "runs": 0, "tuner_sessions": 0}
        try:
            with self.transaction():
                maps: dict[str, dict[int, int]] = {table: {} for table, _ in self._MERGE_TABLES}

                ctx_map: dict[int, int] = {}
                ctx_cols = self._merged_columns("tuning_contexts")
                for row in conn.execute("SELECT * FROM src.tuning_contexts ORDER BY id").fetchall():
                    cur = conn.execute(
                        f"INSERT OR IGNORE INTO tuning_contexts ({','.join(ctx_cols)}) "
                        f"VALUES ({','.join('?' * len(ctx_cols))})",
                        [row[c] for c in ctx_cols],
                    )
                    if cur.rowcount > 0:
                        ctx_map[row["id"]] = cur.lastrowid
                        counts["contexts"] += 1
                    else:
                        existing = conn.execute(
                            "SELECT id FROM tuning_contexts WHERE co_hash=? AND bios_version=?",
                            (row["co_hash"], row["bios_version"]),
                        ).fetchone()
                        ctx_map[row["id"]] = existing["id"]
                maps["tuning_contexts"] = ctx_map

                for table, remaps in self._MERGE_TABLES:
                    cols = self._merged_columns(table)
                    for row in conn.execute(f"SELECT * FROM src.{table} ORDER BY id").fetchall():
                        vals = []
                        skip = False
                        for c in cols:
                            v = row[c]
                            if c in remaps and v is not None:
                                v = maps[remaps[c]].get(v)
                                if v is None and c not in self._MERGE_LINKS_KEPT_WHEN_UNMATCHED:
                                    skip = True
                                    break
                            vals.append(v)
                        if skip:
                            continue
                        cur = conn.execute(
                            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                            vals,
                        )
                        maps[table][row["id"]] = cur.lastrowid
                boot_cols = ",".join(self._merged_columns("tuner_boots"))
                conn.execute(f"INSERT OR IGNORE INTO tuner_boots ({boot_cols}) SELECT {boot_cols} FROM src.tuner_boots")
                counts["runs"] = len(maps["runs"])
                counts["tuner_sessions"] = len(maps["tuner_sessions"])

        finally:
            conn.execute("DETACH DATABASE src")
        return counts

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
        stale = self.__conn.execute("SELECT id, started_at FROM runs WHERE status='running'").fetchall()
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

    def checkpoint(self) -> None:
        busy, wal_frames, checkpointed = self.__conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        log.info("WAL checkpoint: busy=%d log=%d checkpointed=%d", busy, wal_frames, checkpointed)

    @property
    def path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.checkpoint()
        finally:
            self.__conn.close()
            self._closed = True
        if str(self._db_path) != ":memory:":
            self._hand_back_files()
