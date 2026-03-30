# Multi-Mode Tuning Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the auto-tuner from single-mode SSE-only to a crash-aware, multi-mode pipeline that discovers, hardens, and validates CO offsets across SSE, AVX2, and LARGE FFT workloads — with death spiral prevention and import profile support.

**Architecture:** Extend the existing per-core state machine with new HARDENING_T1/T2/HARDENED phases. Add crash detection via enhanced DB records, death spiral termination via time budget + convergence limits, and a 4-stage multi-mode validation. Import profile mode allows BIOS-update re-validation without rediscovery.

**Tech Stack:** Python 3.11+, PySide6, SQLite WAL, pytest

**Spec:** `.superpowers/specs/2026-03-30-multi-mode-pipeline-design.md`

---

## File Map

### Modified Files

| File | Changes |
|------|---------|
| `src/tuner/state.py` | Add HARDENING_T1, HARDENING_T2, HARDENED phases; add crash_count, crash_cooldown, cumulative_test_time, hardening_tier_index fields to CoreState |
| `src/tuner/config.py` | Add hardening_tiers, max_core_time_seconds, crash_penalty_steps, validate_transitions options |
| `src/tuner/engine.py` | Hardening state transitions, crash detection on resume, crash penalty, death spiral termination, S4 validation, multi-mode validation, import profile, safety ramp, crash cooldown scheduling |
| `src/tuner/persistence.py` | Add log_test_result fields (backend, stress_mode, fft_preset), import/export profile helpers |
| `src/history/db.py` | Schema migration v8→v9: new columns on tuner_core_states (crash_count, crash_cooldown, cumulative_test_time, hardening_tier_index), new columns on tuner_test_log (backend, stress_mode, fft_preset) |
| `src/history/export.py` | Add export_tuner_profile / import_tuner_profile for the new JSON format |
| `src/history/context.py` | Add read_cpu_model() helper |
| `src/gui/tuner_tab.py` | New phase labels, crash styling in log, import profile button, session picker with metadata, pipeline stage display |
| `src/gui/widgets/core_grid.py` | HARDENING/HARDENED state colors, crash count badge, cooldown indicator |
| `src/engine/scheduler.py` | Add rapid transition test method (_run_rapid_transitions) |

### Modified Test Files

| File | Changes |
|------|---------|
| `tests/test_tuner_engine.py` | Tests for hardening transitions, crash detection, death spiral termination, import profile, S4 validation, multi-mode validation |
| `tests/test_tuner_config.py` | Tests for new config options, hardening_tiers validation |
| `tests/test_tuner_persistence.py` | Tests for new DB columns, profile import/export |
| `tests/test_scheduler.py` | Tests for rapid transition method |
| `tests/test_safety.py` | Tests for crash penalty bounds, hardening backoff safety |

---

## Task 1: Extend State Machine — New Phases and CoreState Fields

**Files:**
- Modify: `src/tuner/state.py:1-66`
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing tests for new phases and fields**

Add to `tests/test_tuner_engine.py`:

```python
class TestHardeningPhases:
    def test_hardening_phases_exist(self):
        assert TunerPhase.HARDENING_T1 == "hardening_t1"
        assert TunerPhase.HARDENING_T2 == "hardening_t2"
        assert TunerPhase.HARDENED == "hardened"

    def test_core_state_has_crash_fields(self):
        cs = CoreState(core_id=0)
        assert cs.crash_count == 0
        assert cs.crash_cooldown == 0
        assert cs.cumulative_test_time == 0.0
        assert cs.hardening_tier_index == 0

    def test_phase_ordering_includes_hardening(self):
        phases = list(TunerPhase)
        assert TunerPhase.HARDENING_T1 in phases
        assert TunerPhase.HARDENING_T2 in phases
        assert TunerPhase.HARDENED in phases
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestHardeningPhases -v`
Expected: FAIL — `AttributeError: 'TunerPhase' has no attribute 'HARDENING_T1'`

- [ ] **Step 3: Add new phases to TunerPhase enum**

In `src/tuner/state.py`, add after `BACKOFF_CONFIRMING`:

```python
class TunerPhase(StrEnum):
    NOT_STARTED = "not_started"
    COARSE_SEARCH = "coarse_search"
    FINE_SEARCH = "fine_search"
    SETTLED = "settled"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    FAILED_CONFIRM = "failed_confirm"
    BACKOFF_PRECONFIRM = "backoff_preconfirm"
    BACKOFF_CONFIRMING = "backoff_confirming"
    HARDENING_T1 = "hardening_t1"
    HARDENING_T2 = "hardening_t2"
    HARDENED = "hardened"
```

- [ ] **Step 4: Add new fields to CoreState dataclass**

In `src/tuner/state.py`, add after `in_test: bool = False`:

```python
@dataclasses.dataclass(slots=True)
class CoreState:
    core_id: int
    phase: TunerPhase = TunerPhase.NOT_STARTED
    current_offset: int = 0
    best_offset: int | None = None
    coarse_fail_offset: int | None = None
    confirm_attempts: int = 0
    baseline_offset: int = 0
    backoff_mode: bool = False
    consecutive_backoff_fails: int = 0
    backoff_fail_bound: int | None = None
    backoff_pass_bound: int | None = None
    in_test: bool = False
    # --- new fields ---
    crash_count: int = 0
    crash_cooldown: int = 0
    cumulative_test_time: float = 0.0
    hardening_tier_index: int = 0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestHardeningPhases -v`
Expected: 3 PASSED

- [ ] **Step 6: Run full test suite to check for regressions**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All existing tests pass (new fields have defaults, new enum values don't break existing comparisons)

- [ ] **Step 7: Commit**

```bash
git add src/tuner/state.py tests/test_tuner_engine.py
git commit -m "feat(tuner): add hardening phases and crash tracking fields to state machine"
```

---

## Task 2: Extend TunerConfig — New Options

**Files:**
- Modify: `src/tuner/config.py:1-87`
- Test: `tests/test_tuner_config.py`

- [ ] **Step 1: Write failing tests for new config options**

Add to `tests/test_tuner_config.py`:

```python
class TestNewConfigOptions:
    def test_hardening_tiers_default(self):
        cfg = TunerConfig()
        assert cfg.hardening_tiers == [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]

    def test_max_core_time_default(self):
        cfg = TunerConfig()
        assert cfg.max_core_time_seconds == 7200

    def test_crash_penalty_steps_default(self):
        cfg = TunerConfig()
        assert cfg.crash_penalty_steps == 3

    def test_validate_transitions_default(self):
        cfg = TunerConfig()
        assert cfg.validate_transitions is True

    def test_hardening_tiers_json_roundtrip(self):
        cfg = TunerConfig()
        restored = TunerConfig.from_json(cfg.to_json())
        assert restored.hardening_tiers == cfg.hardening_tiers
        assert restored.max_core_time_seconds == cfg.max_core_time_seconds
        assert restored.crash_penalty_steps == cfg.crash_penalty_steps
        assert restored.validate_transitions == cfg.validate_transitions

    def test_empty_hardening_tiers_valid(self):
        cfg = TunerConfig(hardening_tiers=[])
        errors = cfg.validate()
        assert not any("hardening" in e.lower() for e in errors)

    def test_validate_crash_penalty_range(self):
        cfg = TunerConfig(crash_penalty_steps=0)
        errors = cfg.validate()
        assert any("crash_penalty" in e.lower() for e in errors)

    def test_validate_max_core_time_range(self):
        cfg = TunerConfig(max_core_time_seconds=100)
        errors = cfg.validate()
        assert any("max_core_time" in e.lower() for e in errors)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_config.py::TestNewConfigOptions -v`
Expected: FAIL — `AttributeError: 'TunerConfig' has no attribute 'hardening_tiers'`

- [ ] **Step 3: Add new fields to TunerConfig**

In `src/tuner/config.py`, add after `backoff_preconfirm_multiplier`:

```python
    # Multi-mode hardening tiers (run after confirmation)
    hardening_tiers: list[dict[str, str]] = dataclasses.field(
        default_factory=lambda: [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]
    )

    # Per-core time budget for search phases (seconds)
    max_core_time_seconds: int = 7200

    # Backoff steps after system crash (multiplied by fine_step direction)
    crash_penalty_steps: int = 3

    # Enable S4 rapid transition validation
    validate_transitions: bool = True
```

- [ ] **Step 4: Add validation for new fields**

In `src/tuner/config.py`, in the `validate` method, add:

```python
        if not 1 <= self.crash_penalty_steps <= 10:
            errors.append("crash_penalty_steps must be 1-10")
        if not 1800 <= self.max_core_time_seconds <= 14400:
            errors.append("max_core_time_seconds must be 1800-14400")
        for i, tier in enumerate(self.hardening_tiers):
            if not isinstance(tier, dict):
                errors.append(f"hardening_tiers[{i}] must be a dict")
            elif not all(k in tier for k in ("backend", "stress_mode", "fft_preset")):
                errors.append(f"hardening_tiers[{i}] missing required keys: backend, stress_mode, fft_preset")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_config.py -v`
Expected: All PASSED (including existing tests — `from_json` ignores unknown fields, so existing JSON roundtrip still works)

- [ ] **Step 6: Commit**

```bash
git add src/tuner/config.py tests/test_tuner_config.py
git commit -m "feat(tuner): add hardening_tiers, time budget, crash penalty config options"
```

---

## Task 3: Schema Migration v8→v9 — New DB Columns

**Files:**
- Modify: `src/history/db.py`
- Test: `tests/test_tuner_persistence.py`

- [ ] **Step 1: Write failing tests for new columns**

Add to `tests/test_tuner_persistence.py`:

```python
class TestSchemaV9:
    def test_schema_version_is_9(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            row = db._conn.execute("SELECT version FROM schema_version").fetchone()
            assert row[0] == 9
        finally:
            db.close()

    def test_core_state_crash_fields_persist(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            cs = CoreState(core_id=0, crash_count=2, crash_cooldown=1,
                           cumulative_test_time=3600.5, hardening_tier_index=1)
            db.upsert_tuner_core_state(sid, cs)
            states = db.get_tuner_core_states(sid)
            assert states[0].crash_count == 2
            assert states[0].crash_cooldown == 1
            assert abs(states[0].cumulative_test_time - 3600.5) < 0.01
            assert states[0].hardening_tier_index == 1
        finally:
            db.close()

    def test_test_log_has_backend_fields(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            log_id = db.insert_tuner_test_log(
                sid, core_id=0, offset=-30, phase="hardening_t1",
                passed=True, error_msg=None, error_type=None,
                duration=300.0, run_id=None,
                backend="mprime", stress_mode="AVX2", fft_preset="SMALL",
            )
            logs = db.get_tuner_test_log(sid)
            assert logs[0]["backend"] == "mprime"
            assert logs[0]["stress_mode"] == "AVX2"
            assert logs[0]["fft_preset"] == "SMALL"
        finally:
            db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_persistence.py::TestSchemaV9 -v`
Expected: FAIL — schema version still 8, missing columns

- [ ] **Step 3: Bump SCHEMA_VERSION and add migration**

In `src/history/db.py`:

1. Change `SCHEMA_VERSION = 8` to `SCHEMA_VERSION = 9`

2. In the `_migrate` method, add the v8→v9 migration after the existing v7→v8 block:

```python
        if current < 9:
            self._conn.executescript("""
                ALTER TABLE tuner_core_states ADD COLUMN crash_count INTEGER DEFAULT 0;
                ALTER TABLE tuner_core_states ADD COLUMN crash_cooldown INTEGER DEFAULT 0;
                ALTER TABLE tuner_core_states ADD COLUMN cumulative_test_time REAL DEFAULT 0.0;
                ALTER TABLE tuner_core_states ADD COLUMN hardening_tier_index INTEGER DEFAULT 0;
                ALTER TABLE tuner_test_log ADD COLUMN backend TEXT;
                ALTER TABLE tuner_test_log ADD COLUMN stress_mode TEXT;
                ALTER TABLE tuner_test_log ADD COLUMN fft_preset TEXT;
                UPDATE schema_version SET version = 9;
            """)
```

3. Update the fresh-create schema for `tuner_core_states` to include the new columns.

4. Update the fresh-create schema for `tuner_test_log` to include backend, stress_mode, fft_preset columns.

- [ ] **Step 4: Update upsert_tuner_core_state to persist new fields**

In `src/history/db.py`, find the `upsert_tuner_core_state` method and add the new columns to both the INSERT and ON CONFLICT UPDATE clauses:

```python
    def upsert_tuner_core_state(self, session_id: int, cs: CoreState) -> None:
        self._conn.execute(
            """INSERT INTO tuner_core_states
               (session_id, core_id, phase, current_offset, best_offset,
                coarse_fail_offset, confirm_attempts, baseline_offset,
                backoff_mode, consecutive_backoff_fails,
                backoff_fail_bound, backoff_pass_bound, in_test,
                crash_count, crash_cooldown, cumulative_test_time, hardening_tier_index,
                updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(session_id, core_id) DO UPDATE SET
                 phase=excluded.phase, current_offset=excluded.current_offset,
                 best_offset=excluded.best_offset, coarse_fail_offset=excluded.coarse_fail_offset,
                 confirm_attempts=excluded.confirm_attempts, baseline_offset=excluded.baseline_offset,
                 backoff_mode=excluded.backoff_mode,
                 consecutive_backoff_fails=excluded.consecutive_backoff_fails,
                 backoff_fail_bound=excluded.backoff_fail_bound,
                 backoff_pass_bound=excluded.backoff_pass_bound,
                 in_test=excluded.in_test,
                 crash_count=excluded.crash_count,
                 crash_cooldown=excluded.crash_cooldown,
                 cumulative_test_time=excluded.cumulative_test_time,
                 hardening_tier_index=excluded.hardening_tier_index,
                 updated_at=excluded.updated_at""",
            (session_id, cs.core_id, cs.phase.value, cs.current_offset,
             cs.best_offset, cs.coarse_fail_offset, cs.confirm_attempts,
             cs.baseline_offset, cs.backoff_mode, cs.consecutive_backoff_fails,
             cs.backoff_fail_bound, cs.backoff_pass_bound, cs.in_test,
             cs.crash_count, cs.crash_cooldown, cs.cumulative_test_time,
             cs.hardening_tier_index),
        )
```

- [ ] **Step 5: Update get_tuner_core_states to read new fields**

In `src/history/db.py`, update the SELECT and CoreState construction to include the new columns:

```python
    def get_tuner_core_states(self, session_id: int) -> dict[int, CoreState]:
        rows = self._conn.execute(
            """SELECT core_id, phase, current_offset, best_offset,
                      coarse_fail_offset, confirm_attempts, baseline_offset,
                      backoff_mode, consecutive_backoff_fails,
                      backoff_fail_bound, backoff_pass_bound, in_test,
                      crash_count, crash_cooldown, cumulative_test_time,
                      hardening_tier_index
               FROM tuner_core_states WHERE session_id = ?""",
            (session_id,),
        ).fetchall()
        result = {}
        for r in rows:
            result[r[0]] = CoreState(
                core_id=r[0], phase=TunerPhase(r[1]),
                current_offset=r[2], best_offset=r[3],
                coarse_fail_offset=r[4], confirm_attempts=r[5],
                baseline_offset=r[6], backoff_mode=bool(r[7]),
                consecutive_backoff_fails=r[8],
                backoff_fail_bound=r[9], backoff_pass_bound=r[10],
                in_test=bool(r[11]),
                crash_count=r[12] or 0, crash_cooldown=r[13] or 0,
                cumulative_test_time=r[14] or 0.0,
                hardening_tier_index=r[15] or 0,
            )
        return result
```

- [ ] **Step 6: Update insert_tuner_test_log to accept backend/mode/fft**

In `src/history/db.py`, update `insert_tuner_test_log`:

```python
    def insert_tuner_test_log(
        self, session_id: int, core_id: int, offset: int, phase: str,
        passed: bool, error_msg: str | None, error_type: str | None,
        duration: float | None, run_id: int | None,
        backend: str | None = None, stress_mode: str | None = None,
        fft_preset: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO tuner_test_log
               (session_id, core_id, offset_tested, phase, passed,
                error_message, error_type, duration_seconds, run_id,
                backend, stress_mode, fft_preset, tested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (session_id, core_id, offset, phase, passed,
             error_msg, error_type, duration, run_id,
             backend, stress_mode, fft_preset),
        )
        return cur.lastrowid
```

- [ ] **Step 7: Update get_tuner_test_log to return new fields**

In `src/history/db.py`, update the SELECT and dict construction in `get_tuner_test_log` to include backend, stress_mode, fft_preset.

- [ ] **Step 8: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_persistence.py -v`
Expected: All PASSED including new TestSchemaV9 tests

- [ ] **Step 9: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 10: Commit**

```bash
git add src/history/db.py tests/test_tuner_persistence.py
git commit -m "feat(db): schema v9 — crash tracking, hardening tier, backend info on test log"
```

---

## Task 4: Update Persistence Layer — Profile Export/Import

**Files:**
- Modify: `src/tuner/persistence.py:1-96`
- Modify: `src/history/export.py:1-185`
- Modify: `src/history/context.py:1-122`
- Test: `tests/test_tuner_persistence.py`

- [ ] **Step 1: Write failing tests for profile export/import**

Add to `tests/test_tuner_persistence.py`:

```python
import json

class TestProfileExportImport:
    def test_export_tuner_profile(self, tmp_path):
        from history.export import export_tuner_profile
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "2.04", "AMD Ryzen 9 9950X3D")
            # Set up confirmed cores
            cs0 = CoreState(core_id=0, phase=TunerPhase.CONFIRMED, best_offset=-38)
            cs1 = CoreState(core_id=1, phase=TunerPhase.HARDENED, best_offset=-33)
            cs2 = CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, best_offset=-20)
            db.upsert_tuner_core_state(sid, cs0)
            db.upsert_tuner_core_state(sid, cs1)
            db.upsert_tuner_core_state(sid, cs2)
            result = export_tuner_profile(db, sid)
            data = json.loads(result)
            assert data["bios_version"] == "2.04"
            assert data["cpu_model"] == "AMD Ryzen 9 9950X3D"
            assert data["core_count"] == 2  # only confirmed/hardened
            assert data["profile"] == {"0": -38, "1": -33}
            assert "source_session_id" in data
        finally:
            db.close()

    def test_export_excludes_unconfirmed(self, tmp_path):
        from history.export import export_tuner_profile
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "2.04", "TestCPU")
            cs = CoreState(core_id=0, phase=TunerPhase.SETTLED, best_offset=-20)
            db.upsert_tuner_core_state(sid, cs)
            result = export_tuner_profile(db, sid)
            data = json.loads(result)
            assert data["profile"] == {}
        finally:
            db.close()

    def test_import_tuner_profile_from_json(self):
        from history.export import parse_tuner_profile
        data = json.dumps({
            "cpu_model": "AMD Ryzen 9 9950X3D",
            "core_count": 2,
            "bios_version": "2.04",
            "profile": {"0": -38, "2": -33},
        })
        result = parse_tuner_profile(data)
        assert result["profile"] == {0: -38, 2: -33}
        assert result["cpu_model"] == "AMD Ryzen 9 9950X3D"
        assert result["core_count"] == 2

    def test_import_validates_core_count(self):
        from history.export import validate_tuner_profile_import
        profile_data = {"profile": {0: -38}, "core_count": 1, "cpu_model": "TestCPU"}
        errors = validate_tuner_profile_import(profile_data, system_core_count=16, system_cpu_model="TestCPU")
        assert not any(e["level"] == "error" for e in errors)

    def test_import_blocks_core_count_mismatch(self):
        from history.export import validate_tuner_profile_import
        profile_data = {"profile": {0: -38}, "core_count": 8, "cpu_model": "TestCPU"}
        errors = validate_tuner_profile_import(profile_data, system_core_count=16, system_cpu_model="TestCPU")
        assert any(e["level"] == "error" and "core" in e["message"].lower() for e in errors)

    def test_import_warns_cpu_model_mismatch(self):
        from history.export import validate_tuner_profile_import
        profile_data = {"profile": {0: -38}, "core_count": 16, "cpu_model": "Other CPU"}
        errors = validate_tuner_profile_import(profile_data, system_core_count=16, system_cpu_model="AMD Ryzen 9 9950X3D")
        assert any(e["level"] == "warning" and "cpu" in e["message"].lower() for e in errors)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_persistence.py::TestProfileExportImport -v`
Expected: FAIL — `ImportError: cannot import name 'export_tuner_profile'`

- [ ] **Step 3: Implement export_tuner_profile in export.py**

Add to `src/history/export.py`:

```python
def export_tuner_profile(db: HistoryDB, session_id: int) -> str:
    session = db.get_tuner_session(session_id)
    if session is None:
        raise ValueError(f"Session {session_id} not found")
    states = db.get_tuner_core_states(session_id)
    importable_phases = {TunerPhase.CONFIRMED, TunerPhase.HARDENED}
    profile = {
        str(cs.core_id): cs.best_offset
        for cs in states.values()
        if cs.phase in importable_phases and cs.best_offset is not None
    }
    config = json.loads(session.config_json) if session.config_json else {}
    hardening_tiers = config.get("hardening_tiers", [])
    tiers_passed = [
        f"{t['stress_mode']}:{t['fft_preset']}" for t in hardening_tiers
    ]
    has_hardened = any(cs.phase == TunerPhase.HARDENED for cs in states.values())
    data = {
        "cpu_model": session.cpu_model,
        "core_count": len(profile),
        "bios_version": session.bios_version,
        "source_session_id": session_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "primary_backend": config.get("backend", "mprime"),
        "primary_mode": config.get("stress_mode", "SSE"),
        "primary_fft": config.get("fft_preset", "SMALL"),
        "hardened": has_hardened,
        "hardening_tiers_passed": tiers_passed if has_hardened else [],
        "profile": profile,
    }
    return json.dumps(data, indent=2)
```

- [ ] **Step 4: Implement parse_tuner_profile and validate_tuner_profile_import**

Add to `src/history/export.py`:

```python
def parse_tuner_profile(json_str: str) -> dict:
    data = json.loads(json_str)
    profile = {int(k): int(v) for k, v in data.get("profile", {}).items()}
    return {
        "profile": profile,
        "cpu_model": data.get("cpu_model", ""),
        "core_count": data.get("core_count", len(profile)),
        "bios_version": data.get("bios_version", ""),
        "hardened": data.get("hardened", False),
        "source_session_id": data.get("source_session_id"),
    }


def validate_tuner_profile_import(
    profile_data: dict,
    system_core_count: int,
    system_cpu_model: str,
) -> list[dict]:
    messages = []
    imported_max_core = max(profile_data["profile"].keys()) + 1 if profile_data["profile"] else 0
    if profile_data.get("core_count", 0) > system_core_count or imported_max_core > system_core_count:
        messages.append({
            "level": "error",
            "message": f"Core count mismatch: profile has {profile_data.get('core_count', imported_max_core)} cores, system has {system_core_count}",
        })
    if profile_data.get("cpu_model") and profile_data["cpu_model"] != system_cpu_model:
        messages.append({
            "level": "warning",
            "message": f"CPU model mismatch: profile='{profile_data['cpu_model']}', system='{system_cpu_model}'",
        })
    if not profile_data.get("profile"):
        messages.append({
            "level": "error",
            "message": "Profile contains no confirmed cores",
        })
    return messages
```

- [ ] **Step 5: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_persistence.py::TestProfileExportImport -v`
Expected: All PASSED

- [ ] **Step 6: Update persistence.py log_test_result to pass through backend fields**

In `src/tuner/persistence.py`, update `log_test_result`:

```python
def log_test_result(
    db: HistoryDB, session_id: int, core_id: int, offset: int,
    phase: str, passed: bool, error_msg: str | None = None,
    error_type: str | None = None, duration: float | None = None,
    run_id: int | None = None,
    backend: str | None = None, stress_mode: str | None = None,
    fft_preset: str | None = None,
) -> int:
    return db.insert_tuner_test_log(
        session_id, core_id, offset, phase, passed,
        error_msg, error_type, duration, run_id,
        backend=backend, stress_mode=stress_mode, fft_preset=fft_preset,
    )
```

- [ ] **Step 7: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/history/export.py src/tuner/persistence.py tests/test_tuner_persistence.py
git commit -m "feat(export): tuner profile export/import with validation"
```

---

## Task 5: Crash Detection on Resume

**Files:**
- Modify: `src/tuner/engine.py` (resume method, ~lines 288-401)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing tests for crash detection**

Add to `tests/test_tuner_engine.py`:

```python
class TestCrashDetection:
    def test_resume_detects_crash_from_in_test_flag(self, tuner_engine, mock_db):
        """When in_test=True on resume, it's a crash — log synthetic CRASH event."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-30, best_offset=-28, in_test=True)
        mock_db.get_tuner_core_states.return_value = {0: cs}
        tuner_engine._detect_and_handle_crashes(mock_db, session_id=1, core_states={0: cs})
        # Crash should be logged
        log_call = mock_db.insert_tuner_test_log.call_args
        assert log_call is not None
        assert log_call.kwargs.get("error_type") == "crash" or log_call[1].get("error_type") == "crash"

    def test_crash_applies_penalty_backoff(self, tuner_engine):
        """After crash, offset backs off by crash_penalty_steps * fine_step."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-30, best_offset=-28, in_test=True)
        tuner_engine._config = TunerConfig(crash_penalty_steps=3, fine_step=1, direction=-1)
        tuner_engine._apply_crash_penalty(cs)
        # -30 + 3*1 = -27 (3 steps toward baseline/0)
        assert cs.current_offset == -27
        assert cs.crash_count == 1
        assert cs.crash_cooldown == 2

    def test_crash_sets_hard_fail_bound(self, tuner_engine):
        """Crashed offset becomes hard fail_bound — never tried again."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-30, best_offset=-28, in_test=True,
                       backoff_fail_bound=None)
        tuner_engine._config = TunerConfig(crash_penalty_steps=3, fine_step=1, direction=-1)
        tuner_engine._apply_crash_penalty(cs)
        assert cs.backoff_fail_bound == -30

    def test_no_crash_when_in_test_false(self, tuner_engine, mock_db):
        """Normal resume (paused, not crashed) — no crash handling."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-30, in_test=False)
        tuner_engine._detect_and_handle_crashes(mock_db, session_id=1, core_states={0: cs})
        mock_db.insert_tuner_test_log.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestCrashDetection -v`
Expected: FAIL — methods don't exist yet

- [ ] **Step 3: Implement _detect_and_handle_crashes in engine.py**

Add to `TunerEngine` class in `src/tuner/engine.py`:

```python
    def _detect_and_handle_crashes(
        self, db: HistoryDB, session_id: int,
        core_states: dict[int, CoreState],
    ) -> list[int]:
        """Detect cores that were testing when the system crashed.
        Returns list of crashed core IDs."""
        crashed_cores = []
        now = time.time()
        for cs in core_states.values():
            if not cs.in_test:
                continue
            crashed_cores.append(cs.core_id)
            # Log synthetic crash event
            gap_note = f"System reboot detected. Offset {cs.current_offset} caused hard crash."
            tp.log_test_result(
                db, session_id, cs.core_id, cs.current_offset,
                cs.phase.value, passed=False,
                error_msg=gap_note, error_type="crash",
                duration=None,
                backend=None, stress_mode=None, fft_preset=None,
            )
            self._apply_crash_penalty(cs)
            cs.in_test = False
            tp.save_core_state(db, session_id, cs)
            logging.warning(
                "Core %d: crash detected at offset %d — applied penalty, "
                "new offset %d, crash_count=%d",
                cs.core_id, cs.current_offset, cs.current_offset, cs.crash_count,
            )
        return crashed_cores

    def _apply_crash_penalty(self, cs: CoreState) -> None:
        """Apply crash penalty: larger backoff + set hard fail bound + cooldown."""
        crashed_offset = cs.current_offset
        # Set hard fail bound — never try this offset or more aggressive again
        if cs.backoff_fail_bound is None or self._is_more_aggressive(crashed_offset, cs.backoff_fail_bound):
            cs.backoff_fail_bound = crashed_offset
        # Back off by crash_penalty_steps
        penalty = self._config.crash_penalty_steps * self._config.fine_step
        cs.current_offset = crashed_offset - (self._config.direction * penalty)
        # Clamp to baseline
        if self._at_or_past_baseline(cs.current_offset, cs):
            cs.current_offset = cs.baseline_offset
        cs.crash_count += 1
        cs.crash_cooldown = 2
        # Force midpoint jump if not already in binary search
        if cs.backoff_pass_bound is None and cs.phase in (
            TunerPhase.COARSE_SEARCH, TunerPhase.FINE_SEARCH,
            TunerPhase.BACKOFF_PRECONFIRM,
        ):
            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
            cs.backoff_mode = True

    def _is_more_aggressive(self, a: int, b: int) -> bool:
        """Returns True if offset a is more aggressive than b."""
        if self._config.direction == -1:
            return a < b
        return a > b
```

- [ ] **Step 4: Wire crash detection into the resume method**

In `src/tuner/engine.py`, in the `resume` method (around line 340, after loading core_states), add:

```python
        # Detect and handle crashes before continuing
        crashed = self._detect_and_handle_crashes(self._db, session_id, self._core_states)
        if crashed:
            self._set_status(f"resumed after crash (cores: {crashed})")
            tp.update_session_status(self._db, session_id, "running")
        else:
            self._set_status("running")
            tp.update_session_status(self._db, session_id, "running")
```

- [ ] **Step 5: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestCrashDetection -v`
Expected: All PASSED

- [ ] **Step 6: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): crash detection on resume with penalty backoff and hard fail bounds"
```

---

## Task 6: Death Spiral Prevention — Time Budget and Convergence

**Files:**
- Modify: `src/tuner/engine.py` (_advance_core, _on_test_finished)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tuner_engine.py`:

```python
class TestDeathSpiralPrevention:
    def test_time_budget_settles_core(self):
        """Core exceeding time budget settles at best_offset."""
        cs = CoreState(core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM,
                       current_offset=-20, best_offset=-25,
                       cumulative_test_time=7201.0)  # over 7200 budget
        cfg = TunerConfig(max_core_time_seconds=7200)
        engine = make_test_engine(cfg)
        engine._check_time_budget(cs)
        assert cs.phase == TunerPhase.CONFIRMED
        assert cs.current_offset == cs.best_offset

    def test_time_budget_no_best_settles_at_baseline(self):
        """Core with no best_offset settles at baseline."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-10, best_offset=None,
                       baseline_offset=0, cumulative_test_time=7201.0)
        cfg = TunerConfig(max_core_time_seconds=7200)
        engine = make_test_engine(cfg)
        engine._check_time_budget(cs)
        assert cs.phase == TunerPhase.CONFIRMED
        assert cs.current_offset == 0

    def test_cumulative_time_tracks_test_duration(self):
        """cumulative_test_time incremented by actual test duration."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       cumulative_test_time=100.0)
        cfg = TunerConfig()
        engine = make_test_engine(cfg)
        engine._accumulate_test_time(cs, 60.5)
        assert abs(cs.cumulative_test_time - 160.5) < 0.01

    def test_forced_midpoint_after_3_linear_fails(self):
        """3 consecutive linear backoff fails force midpoint jump."""
        cs = CoreState(core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM,
                       current_offset=-35, best_offset=-30,
                       baseline_offset=0, backoff_mode=True,
                       consecutive_backoff_fails=2,
                       backoff_pass_bound=None, backoff_fail_bound=None)
        cfg = TunerConfig(midpoint_jump_threshold=3)
        engine = make_test_engine(cfg)
        # This is the 3rd fail — should force midpoint jump
        engine._advance_core(cs, passed=False, error_type="computation")
        # After midpoint jump, pass_bound stays None, fail_bound set
        assert cs.backoff_fail_bound is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestDeathSpiralPrevention -v`
Expected: FAIL — methods don't exist

- [ ] **Step 3: Implement _check_time_budget and _accumulate_test_time**

Add to `TunerEngine` in `src/tuner/engine.py`:

```python
    def _check_time_budget(self, cs: CoreState) -> bool:
        """Check if core has exceeded its time budget. Returns True if settled."""
        if cs.cumulative_test_time <= self._config.max_core_time_seconds:
            return False
        # Settle at best known value
        settled_offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        cs.current_offset = settled_offset
        cs.phase = TunerPhase.CONFIRMED
        cs.backoff_mode = False
        logging.warning(
            "Core %d: time budget exceeded (%.0fs > %ds) — settled at %d",
            cs.core_id, cs.cumulative_test_time, self._config.max_core_time_seconds,
            settled_offset,
        )
        return True

    def _accumulate_test_time(self, cs: CoreState, duration: float) -> None:
        """Add test duration to core's cumulative time (search phases only)."""
        if cs.phase in (
            TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2,
            TunerPhase.HARDENED,
        ):
            return  # hardening/validation not subject to time budget
        cs.cumulative_test_time += duration
```

- [ ] **Step 4: Wire time budget into _on_test_finished**

In `src/tuner/engine.py`, in `_on_test_finished` (around line 921), after logging the test result and before calling `_advance_core`:

```python
        # Accumulate test time and check budget
        cs = self._core_states[core_id]
        self._accumulate_test_time(cs, duration)
        if self._check_time_budget(cs):
            tp.save_core_state(self._db, self._session_id, cs)
            self._run_next()
            return
```

- [ ] **Step 5: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestDeathSpiralPrevention -v`
Expected: All PASSED

- [ ] **Step 6: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): death spiral prevention — time budget and forced convergence"
```

---

## Task 7: Pre-Crash Safety Ramp

**Files:**
- Modify: `src/tuner/engine.py` (_advance_core, coarse search section)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_tuner_engine.py`:

```python
class TestSafetyRamp:
    def test_coarse_slows_near_max_offset(self):
        """Within 2*coarse_step of max_offset, step size reduces to fine_step."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-44, best_offset=-44)
        cfg = TunerConfig(coarse_step=2, fine_step=1, max_offset=-50, direction=-1)
        engine = make_test_engine(cfg)
        step = engine._get_coarse_step(cs)
        # -44 is within 2*2=4 of -50, so should use fine_step
        assert step == 1

    def test_coarse_normal_step_far_from_max(self):
        """Far from max_offset, use normal coarse_step."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-30, best_offset=-30)
        cfg = TunerConfig(coarse_step=2, fine_step=1, max_offset=-50, direction=-1)
        engine = make_test_engine(cfg)
        step = engine._get_coarse_step(cs)
        assert step == 2

    def test_advance_core_uses_reduced_step_near_max(self):
        """_advance_core uses fine_step (not coarse_step) when near max_offset."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-46, best_offset=-46)
        cfg = TunerConfig(coarse_step=2, fine_step=1, max_offset=-50, direction=-1)
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=True, error_type=None)
        # Should advance by fine_step (1), not coarse_step (2)
        assert cs.current_offset == -47
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestSafetyRamp -v`
Expected: FAIL

- [ ] **Step 3: Implement _get_coarse_step**

Add to `TunerEngine` in `src/tuner/engine.py`:

```python
    def _get_coarse_step(self, cs: CoreState) -> int:
        """Get coarse step size, reducing near max_offset for safety."""
        distance = abs(cs.current_offset - self._config.max_offset)
        ramp_zone = self._config.coarse_step * 2
        if distance <= ramp_zone:
            return self._config.fine_step
        return self._config.coarse_step
```

- [ ] **Step 4: Wire into _advance_core coarse search**

In `src/tuner/engine.py`, in `_advance_core`, replace the hardcoded `self._config.coarse_step` in the COARSE_SEARCH pass branch with `self._get_coarse_step(cs)`.

- [ ] **Step 5: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestSafetyRamp tests/test_tuner_engine.py::TestStateMachineTransitions -v`
Expected: All PASSED

- [ ] **Step 6: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): pre-crash safety ramp — slow coarse step near max_offset"
```

---

## Task 8: Crash-Aware Scheduling — Cooldown and Crash History

**Files:**
- Modify: `src/tuner/engine.py` (all _pick_* methods)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tuner_engine.py`:

```python
class TestCrashAwareScheduling:
    def test_cooldown_skips_core(self):
        """Core with crash_cooldown > 0 is skipped by picker."""
        states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, crash_cooldown=2),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, crash_cooldown=0),
        }
        cfg = TunerConfig(test_order="sequential")
        engine = make_test_engine(cfg)
        engine._core_states = states
        picked = engine._pick_next_core()
        assert picked == 1

    def test_cooldown_decrements(self):
        """Cooldown decrements when another core is tested."""
        states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, crash_cooldown=2),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, crash_cooldown=0),
        }
        cfg = TunerConfig(test_order="sequential")
        engine = make_test_engine(cfg)
        engine._core_states = states
        engine._decrement_cooldowns(picked_core=1)
        assert states[0].crash_cooldown == 1

    def test_weakest_first_penalizes_crashed_cores(self):
        """Cores with crash history are scored lower in weakest_first."""
        states = {
            0: CoreState(core_id=0, phase=TunerPhase.CONFIRMING, crash_count=2),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, crash_count=0),
        }
        cfg = TunerConfig(test_order="weakest_first")
        engine = make_test_engine(cfg)
        engine._core_states = states
        # Core 0 is closer to done (CONFIRMING) but has crashes
        # Core 1 is earlier (COARSE) but no crashes
        # With penalty, Core 0 score = 1 + 2*2 = 5, Core 1 score = 2
        picked = engine._pick_next_core()
        assert picked == 1  # lower score wins

    def test_all_cores_in_cooldown_returns_none(self):
        """If all active cores are in cooldown, return None (wait)."""
        states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, crash_cooldown=1),
        }
        cfg = TunerConfig(test_order="sequential")
        engine = make_test_engine(cfg)
        engine._core_states = states
        picked = engine._pick_next_core()
        assert picked is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestCrashAwareScheduling -v`
Expected: FAIL

- [ ] **Step 3: Implement cooldown filtering and decrement**

Add to `TunerEngine` in `src/tuner/engine.py`:

```python
    def _is_core_available(self, cs: CoreState) -> bool:
        """Check if core is available for testing (not done, not in cooldown)."""
        if cs.crash_cooldown > 0:
            return False
        if cs.phase in (TunerPhase.CONFIRMED, TunerPhase.HARDENED):
            return False
        return True

    def _decrement_cooldowns(self, picked_core: int) -> None:
        """Decrement crash cooldown for all cores except the one being tested."""
        for cs in self._core_states.values():
            if cs.core_id != picked_core and cs.crash_cooldown > 0:
                cs.crash_cooldown -= 1
```

- [ ] **Step 4: Wire cooldown into all _pick_* methods**

In each `_pick_sequential`, `_pick_round_robin`, `_pick_weakest_first`, `_pick_ccd_alternating`, `_pick_ccd_round_robin`: filter candidates through `_is_core_available` before selecting.

In `_pick_weakest_first`, update the scoring:

```python
    def _pick_weakest_first(self) -> int | None:
        phase_score = {
            TunerPhase.FINE_SEARCH: 0,
            TunerPhase.CONFIRMING: 1,
            TunerPhase.COARSE_SEARCH: 2,
            TunerPhase.SETTLED: 3,
            TunerPhase.NOT_STARTED: 4,
            TunerPhase.FAILED_CONFIRM: 1,
            TunerPhase.BACKOFF_PRECONFIRM: 1,
            TunerPhase.BACKOFF_CONFIRMING: 1,
            TunerPhase.HARDENING_T1: 0,
            TunerPhase.HARDENING_T2: 0,
        }
        candidates = [
            cs for cs in self._core_states.values()
            if self._is_core_available(cs) and cs.phase != TunerPhase.NOT_STARTED
        ]
        if not candidates:
            # Try NOT_STARTED cores
            candidates = [
                cs for cs in self._core_states.values()
                if self._is_core_available(cs)
            ]
        if not candidates:
            return None
        # Score: lower is higher priority. Crash penalty pushes cores down.
        best = min(candidates, key=lambda cs: phase_score.get(cs.phase, 5) + cs.crash_count * 2)
        return best.core_id
```

- [ ] **Step 5: Call _decrement_cooldowns in _run_next**

In `src/tuner/engine.py`, in `_run_next`, after picking the next core and before starting the worker:

```python
        core_id = self._pick_next_core()
        if core_id is None:
            # Check if all cores are done or all in cooldown
            # ... existing completion logic ...
            return
        self._decrement_cooldowns(core_id)
```

- [ ] **Step 6: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestCrashAwareScheduling tests/test_tuner_engine.py::TestPickNextCore -v`
Expected: All PASSED

- [ ] **Step 7: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): crash-aware scheduling — cooldown, crash penalty in weakest_first"
```

---

## Task 9: Hardening State Transitions in Engine

**Files:**
- Modify: `src/tuner/engine.py` (_advance_core, _run_next, _on_test_finished)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tuner_engine.py`:

```python
class TestHardeningTransitions:
    def test_confirmed_enters_hardening_t1(self):
        """CONFIRMED with hardening tiers transitions to HARDENING_T1."""
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING,
                       current_offset=-38, best_offset=-38)
        cfg = TunerConfig(hardening_tiers=[
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ])
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENING_T1

    def test_confirmed_skips_hardening_when_no_tiers(self):
        """CONFIRMED with empty hardening_tiers stays CONFIRMED."""
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING,
                       current_offset=-38, best_offset=-38)
        cfg = TunerConfig(hardening_tiers=[])
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.CONFIRMED

    def test_hardening_t1_pass_enters_t2(self):
        """HARDENING_T1 pass with 2 tiers transitions to HARDENING_T2."""
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1,
                       current_offset=-38, best_offset=-38, hardening_tier_index=0)
        cfg = TunerConfig(hardening_tiers=[
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ])
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENING_T2
        assert cs.hardening_tier_index == 1

    def test_hardening_t2_pass_becomes_hardened(self):
        """Last hardening tier pass transitions to HARDENED."""
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T2,
                       current_offset=-38, best_offset=-38, hardening_tier_index=1)
        cfg = TunerConfig(hardening_tiers=[
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ])
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENED

    def test_hardening_t1_fail_backs_off_retries_t1(self):
        """HARDENING_T1 fail backs off by fine_step and retries T1."""
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1,
                       current_offset=-38, best_offset=-38, hardening_tier_index=0)
        cfg = TunerConfig(fine_step=1, direction=-1, hardening_tiers=[
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ])
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=False, error_type="computation")
        assert cs.phase == TunerPhase.HARDENING_T1
        assert cs.current_offset == -37
        assert cs.best_offset == -37

    def test_hardening_t2_fail_retries_t2_not_t1(self):
        """HARDENING_T2 fail backs off and retries T2 (T1 carries forward)."""
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T2,
                       current_offset=-38, best_offset=-38, hardening_tier_index=1)
        cfg = TunerConfig(fine_step=1, direction=-1, hardening_tiers=[
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ])
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=False, error_type="computation")
        assert cs.phase == TunerPhase.HARDENING_T2  # stays T2, not T1
        assert cs.current_offset == -37

    def test_hardening_backoff_at_baseline_settles(self):
        """Hardening backoff reaching baseline settles core as HARDENED at baseline."""
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1,
                       current_offset=0, best_offset=0, baseline_offset=0)
        cfg = TunerConfig(fine_step=1, direction=-1, hardening_tiers=[
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ])
        engine = make_test_engine(cfg)
        engine._advance_core(cs, passed=False, error_type="computation")
        assert cs.phase == TunerPhase.HARDENED

    def test_get_active_stress_config_returns_tier_during_hardening(self):
        """During hardening, _get_active_stress_config returns the tier's config."""
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1, hardening_tier_index=0)
        cfg = TunerConfig(
            backend="mprime", stress_mode="SSE", fft_preset="SMALL",
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
                {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
            ],
        )
        engine = make_test_engine(cfg)
        backend, mode, fft = engine._get_active_stress_config(cs)
        assert backend == "mprime"
        assert mode == "AVX2"
        assert fft == "SMALL"

    def test_get_active_stress_config_returns_primary_during_search(self):
        """During search/confirm, returns primary backend config."""
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH)
        cfg = TunerConfig(backend="mprime", stress_mode="SSE", fft_preset="SMALL")
        engine = make_test_engine(cfg)
        backend, mode, fft = engine._get_active_stress_config(cs)
        assert backend == "mprime"
        assert mode == "SSE"
        assert fft == "SMALL"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestHardeningTransitions -v`
Expected: FAIL

- [ ] **Step 3: Add hardening transitions to _advance_core**

In `src/tuner/engine.py`, in `_advance_core`, after the existing CONFIRMED handling, add:

```python
        # --- Hardening transitions ---
        if cs.phase == TunerPhase.CONFIRMED and self._config.hardening_tiers:
            cs.phase = TunerPhase.HARDENING_T1
            cs.hardening_tier_index = 0
            return

        if cs.phase in (TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2):
            if passed:
                next_tier = cs.hardening_tier_index + 1
                if next_tier >= len(self._config.hardening_tiers):
                    cs.phase = TunerPhase.HARDENED
                else:
                    cs.hardening_tier_index = next_tier
                    _TIER_PHASES = {0: TunerPhase.HARDENING_T1, 1: TunerPhase.HARDENING_T2}
                    cs.phase = _TIER_PHASES.get(next_tier, TunerPhase.HARDENED)
            else:
                # Back off linearly by fine_step
                new_offset = cs.current_offset - (self._config.direction * self._config.fine_step)
                if self._at_or_past_baseline(new_offset, cs):
                    cs.current_offset = cs.baseline_offset
                    cs.best_offset = cs.baseline_offset
                    cs.phase = TunerPhase.HARDENED
                else:
                    cs.current_offset = new_offset
                    cs.best_offset = new_offset
                    # Stay at current tier (T2 fail retries T2, not T1)
            return
```

- [ ] **Step 4: Add hardening tier backend selection to _run_next**

In `src/tuner/engine.py`, in `_run_next` or `_start_worker`, when the core is in a hardening phase, select the backend/mode/fft from the tier config:

```python
    def _get_active_stress_config(self, cs: CoreState) -> tuple[str, str, str]:
        """Return (backend, stress_mode, fft_preset) for the current core's phase."""
        if cs.phase in (TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2):
            tier = self._config.hardening_tiers[cs.hardening_tier_index]
            return tier["backend"], tier["stress_mode"], tier["fft_preset"]
        return self._config.backend, self._config.stress_mode, self._config.fft_preset
```

- [ ] **Step 5: Update _on_test_finished to log backend info**

In `src/tuner/engine.py`, in `_on_test_finished`, pass the active backend/mode/fft to `log_test_result`:

```python
        backend, mode, fft = self._get_active_stress_config(cs)
        tp.log_test_result(
            self._db, self._session_id, core_id, cs.current_offset,
            cs.phase.value, passed, error_msg, error_type, duration,
            backend=backend, stress_mode=mode, fft_preset=fft,
        )
```

- [ ] **Step 6: Update _complete_session to use HARDENED as completion gate**

In `src/tuner/engine.py`, in `_complete_session` (around line 1021), change the check from all cores CONFIRMED to:

```python
        done_phases = {TunerPhase.CONFIRMED, TunerPhase.HARDENED}
        if self._config.hardening_tiers:
            done_phases = {TunerPhase.HARDENED}
        all_done = all(
            cs.phase in done_phases
            for cs in self._core_states.values()
        )
```

- [ ] **Step 7: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestHardeningTransitions tests/test_tuner_engine.py::TestStateMachineTransitions -v`
Expected: All PASSED

- [ ] **Step 8: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 9: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): multi-mode hardening state transitions with linear backoff"
```

---

## Task 10: S4 Rapid Transition Validation

**Files:**
- Modify: `src/engine/scheduler.py`
- Modify: `src/tuner/engine.py` (validation stages)
- Test: `tests/test_scheduler.py`
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing test for rapid transitions in scheduler**

Add to `tests/test_scheduler.py`:

```python
class TestRapidTransitions:
    def test_run_rapid_transitions_cycles(self, mock_backend, topo_dual_ccd_x3d):
        """Rapid transition method cycles load/idle pattern."""
        config = SchedulerConfig(seconds_per_core=30)
        stress_config = StressConfig(mode=StressMode.SSE)
        sched = CoreScheduler(topo_dual_ccd_x3d, mock_backend, stress_config, config, Path("/tmp"))
        mock_backend.should_pass = True
        # Just verify the method exists and accepts the right params
        # Full integration test requires real subprocess — unit test validates interface
        assert hasattr(sched, 'run_rapid_transitions')

    def test_rapid_transition_duration_config(self, mock_backend, topo_dual_ccd_x3d):
        """Rapid transitions respect total duration and cycle timing."""
        config = SchedulerConfig(seconds_per_core=30)
        stress_config = StressConfig(mode=StressMode.SSE)
        sched = CoreScheduler(topo_dual_ccd_x3d, mock_backend, stress_config, config, Path("/tmp"))
        # Validate method signature
        import inspect
        sig = inspect.signature(sched.run_rapid_transitions)
        params = list(sig.parameters.keys())
        assert "cores" in params
        assert "total_duration" in params
        assert "load_seconds" in params
        assert "idle_seconds" in params
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_scheduler.py::TestRapidTransitions -v`
Expected: FAIL — `run_rapid_transitions` doesn't exist

- [ ] **Step 3: Implement run_rapid_transitions in scheduler.py**

Add to `CoreScheduler` in `src/engine/scheduler.py`:

```python
    def run_rapid_transitions(
        self,
        cores: list[int],
        total_duration: float = 600.0,
        load_seconds: float = 10.0,
        idle_seconds: float = 5.0,
    ) -> tuple[bool, str | None]:
        """Run rapid load/idle cycling on all specified cores simultaneously.
        Returns (passed, error_message)."""
        elapsed = 0.0
        cycle = 0
        while elapsed < total_duration and not self._stop_requested:
            cycle += 1
            # LOAD phase: start stress on all cores
            cpu_list = ",".join(
                str(self.topology.physical_to_logical[c][0]) for c in cores
                if c in self.topology.physical_to_logical
            )
            core_work_dir = self.work_dir / "rapid_transition"
            core_work_dir.mkdir(exist_ok=True)
            stress_config = StressConfig(
                mode=self.stress_config.mode,
                fft_preset=self.stress_config.fft_preset,
                threads=len(cores),
            )
            self.backend.prepare(core_work_dir, stress_config)
            cmd = self.backend.get_command(stress_config, core_work_dir)
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    preexec_fn=self._make_preexec(),
                )
                # Load phase
                time.sleep(min(load_seconds, total_duration - elapsed))
                elapsed += load_seconds
                # Kill for idle
                self._kill_process(proc)
                stdout, stderr = proc.communicate(timeout=5)
                # Check for errors during load
                passed, err = self.backend.parse_output(
                    stdout.decode(errors="replace"),
                    stderr.decode(errors="replace"),
                    proc.returncode,
                )
                exit_class = StressBackend.classify_exit_code(proc.returncode)
                if exit_class and exit_class.startswith("crash:"):
                    return False, f"Crash during rapid transition cycle {cycle}: {exit_class}"
            except Exception as e:
                return False, f"Rapid transition error: {e}"
            finally:
                self.backend.cleanup(core_work_dir)

            # IDLE phase: check for MCE during idle
            if elapsed < total_duration and not self._stop_requested:
                idle_start = time.time()
                time.sleep(min(idle_seconds, total_duration - elapsed))
                elapsed += idle_seconds
                mce_events = self.detector.check_mce()
                if mce_events:
                    return False, f"MCE during idle phase of rapid transition cycle {cycle}"

        return True, None

    @staticmethod
    def _kill_process(proc: subprocess.Popen) -> None:
        """Kill a subprocess and its process group."""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=3)
        except (subprocess.TimeoutExpired, ProcessLookupError):
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
```

- [ ] **Step 4: Write failing test for S4 validation stage in engine**

Add to `tests/test_tuner_engine.py`:

```python
class TestValidationS4:
    def test_validation_runs_4_stages_when_enabled(self):
        """With validate_transitions=True, validation has 4 stages."""
        cfg = TunerConfig(validate_transitions=True)
        engine = make_test_engine(cfg)
        assert engine._get_validation_stage_count() == 4

    def test_validation_runs_3_stages_when_disabled(self):
        """With validate_transitions=False, validation has 3 stages."""
        cfg = TunerConfig(validate_transitions=False)
        engine = make_test_engine(cfg)
        assert engine._get_validation_stage_count() == 3
```

- [ ] **Step 5: Implement S4 in engine.py**

Add `_run_validation_stage4` and `_get_validation_stage_count` to `TunerEngine`:

```python
    def _get_validation_stage_count(self) -> int:
        return 4 if self._config.validate_transitions else 3

    def _run_validation_stage4(self) -> None:
        """S4: Rapid transition stress — all cores, load/idle cycling."""
        profile = {cs.core_id: cs.best_offset for cs in self._core_states.values()
                   if cs.phase == TunerPhase.HARDENED}
        cores = list(profile.keys())
        self._apply_validation_offsets_all(profile)

        modes = self._get_validation_modes()
        for mode_idx, (backend_name, stress_mode, fft_preset) in enumerate(modes):
            if self._abort_requested:
                return
            self.validation_progress.emit(4, mode_idx, len(modes))
            self._set_status(f"VALIDATING S4 (rapid transitions) — {stress_mode} {fft_preset}")

            scheduler = self._make_scheduler_for_mode(backend_name, stress_mode, fft_preset)
            passed, err = scheduler.run_rapid_transitions(
                cores, total_duration=600.0, load_seconds=10.0, idle_seconds=5.0,
            )
            if not passed:
                logging.warning("S4 failed (%s %s): %s", stress_mode, fft_preset, err)
                aggressive_core = self._find_most_aggressive_core()
                if aggressive_core is not None:
                    self._backoff_core(aggressive_core)
                self._validation_stage = 1  # restart from S1
                return

        # S4 passed all modes
        self._finalize_validation()
```

- [ ] **Step 6: Wire S4 into validation flow**

In `src/tuner/engine.py`, in `_run_validation_next`, add stage 4 after stage 3:

```python
    def _run_validation_next(self) -> None:
        if self._validation_stage == 1:
            self._run_validation_stage1()
        elif self._validation_stage == 2:
            self._run_validation_stage2()
        elif self._validation_stage == 3:
            self._run_validation_stage3()
        elif self._validation_stage == 4 and self._config.validate_transitions:
            self._run_validation_stage4()
        else:
            self._finalize_validation()
```

- [ ] **Step 7: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_scheduler.py::TestRapidTransitions tests/test_tuner_engine.py::TestValidationS4 -v`
Expected: All PASSED

- [ ] **Step 8: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 9: Commit**

```bash
git add src/engine/scheduler.py src/tuner/engine.py tests/test_scheduler.py tests/test_tuner_engine.py
git commit -m "feat(tuner): S4 rapid transition validation stage with idle/load cycling"
```

---

## Task 11: Multi-Mode Validation (S2/S3 Run Per Mode)

**Files:**
- Modify: `src/tuner/engine.py` (validation stages 2 and 3)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tuner_engine.py`:

```python
class TestMultiModeValidation:
    def test_get_validation_modes_includes_primary_and_tiers(self):
        """Validation modes = primary + each hardening tier."""
        cfg = TunerConfig(
            backend="mprime", stress_mode="SSE", fft_preset="SMALL",
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
                {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
            ],
        )
        engine = make_test_engine(cfg)
        modes = engine._get_validation_modes()
        assert len(modes) == 3
        assert modes[0] == ("mprime", "SSE", "SMALL")
        assert modes[1] == ("mprime", "AVX2", "SMALL")
        assert modes[2] == ("mprime", "SSE", "LARGE")

    def test_validation_modes_no_tiers_primary_only(self):
        """With no hardening tiers, validation uses primary mode only."""
        cfg = TunerConfig(backend="mprime", stress_mode="SSE", fft_preset="SMALL",
                          hardening_tiers=[])
        engine = make_test_engine(cfg)
        modes = engine._get_validation_modes()
        assert len(modes) == 1
        assert modes[0] == ("mprime", "SSE", "SMALL")

    def test_s1_uses_primary_mode_only(self):
        """S1 per-core validation uses primary mode only, not all modes."""
        cfg = TunerConfig(
            backend="mprime", stress_mode="SSE", fft_preset="SMALL",
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            ],
        )
        engine = make_test_engine(cfg)
        s1_modes = engine._get_s1_validation_modes()
        assert len(s1_modes) == 1
        assert s1_modes[0] == ("mprime", "SSE", "SMALL")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestMultiModeValidation -v`
Expected: FAIL

- [ ] **Step 3: Implement _get_validation_modes and _get_s1_validation_modes**

Add to `TunerEngine` in `src/tuner/engine.py`:

```python
    def _get_validation_modes(self) -> list[tuple[str, str, str]]:
        """Return list of (backend, stress_mode, fft_preset) for validation.
        Includes primary mode + all hardening tier modes."""
        modes = [(self._config.backend, self._config.stress_mode, self._config.fft_preset)]
        for tier in self._config.hardening_tiers:
            mode = (tier["backend"], tier["stress_mode"], tier["fft_preset"])
            if mode not in modes:
                modes.append(mode)
        return modes

    def _get_s1_validation_modes(self) -> list[tuple[str, str, str]]:
        """S1 uses primary mode only — cross-core interactions are current-dependent, not mode-dependent."""
        return [(self._config.backend, self._config.stress_mode, self._config.fft_preset)]
```

- [ ] **Step 4: Update _run_validation_stage2 and _run_validation_stage3 to iterate modes**

In `src/tuner/engine.py`, update the existing validation stage methods to loop over `_get_validation_modes()` instead of running once with the primary mode. Each mode iteration within a stage uses the same duration (`validate_duration_seconds`) and the same failure handling (back off most aggressive core, restart from S1).

- [ ] **Step 5: Update validation failure to re-harden backed-off core**

In `src/tuner/engine.py`, update `_on_validation_test_finished` (around line 1333). When validation fails, `_backoff_core` returns the core_id that was backed off. After backing off, reset that core to HARDENING_T1 so it re-hardens before re-entering validation:

```python
    def _on_validation_test_finished(self, core_id: int, passed: bool, error_msg: str = "",
                                     error_type: str = "") -> None:
        if passed:
            # ... existing pass logic: advance to next validation step ...
            return

        # Validation failure — back off the offending core
        if self._validation_stage == 1:
            backed_off = core_id  # S1: tested core is the offender
        else:
            backed_off = self._find_most_aggressive_core()  # S2/S3/S4

        if backed_off is not None:
            penalty = self._config.fine_step
            if error_type == "crash":
                penalty = self._config.crash_penalty_steps * self._config.fine_step
            self._backoff_core(backed_off, steps=penalty)

            # Re-harden the backed-off core before retrying validation
            cs = self._core_states[backed_off]
            cs.phase = TunerPhase.HARDENING_T1
            cs.hardening_tier_index = 0
            tp.save_core_state(self._db, self._session_id, cs)

            # Exit validation mode, re-enter per-core testing
            tp.update_session_status(self._db, self._session_id, "running")
            self._set_status(f"re-hardening core {backed_off} at {cs.current_offset}")
            self._run_next()  # will pick the HARDENING_T1 core
        else:
            # No core can be backed off further — finalize with current values
            self._finalize_validation()
```

- [ ] **Step 5b: Write test for validation failure → re-harden flow**

Add to `tests/test_tuner_engine.py` in `TestMultiModeValidation`:

```python
    def test_validation_failure_re_hardens_core(self, make_test_engine):
        """Validation S2 failure backs off most aggressive core and re-hardens it."""
        cfg = TunerConfig(
            fine_step=1, direction=-1,
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            ],
        )
        engine = make_test_engine(cfg)
        engine._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.HARDENED, best_offset=-38, current_offset=-38),
            1: CoreState(core_id=1, phase=TunerPhase.HARDENED, best_offset=-20, current_offset=-20),
        }
        engine._validation_stage = 2
        # Simulate S2 failure — most aggressive is core 0 at -38
        engine._on_validation_test_finished(core_id=0, passed=False,
                                            error_msg="mprime error", error_type="computation")
        cs0 = engine._core_states[0]
        assert cs0.phase == TunerPhase.HARDENING_T1  # re-entered hardening
        assert cs0.current_offset == -37  # backed off by 1
        assert cs0.hardening_tier_index == 0
```

- [ ] **Step 6: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestMultiModeValidation -v`
Expected: All PASSED

- [ ] **Step 7: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): multi-mode validation — S1 primary-only, S2/S3/S4 all modes"
```

---

## Task 12: Import Profile Mode in Engine

**Files:**
- Modify: `src/tuner/engine.py` (new start_from_profile method)
- Test: `tests/test_tuner_engine.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/test_tuner_engine.py`:

```python
class TestImportProfile:
    def test_imported_cores_enter_confirming(self):
        """Cores with imported values start in CONFIRMING, not COARSE."""
        cfg = TunerConfig()
        engine = make_test_engine(cfg)
        profile = {0: -38, 2: -33}
        states = engine._init_states_from_profile(profile, all_cores=[0, 1, 2, 3])
        assert states[0].phase == TunerPhase.CONFIRMING
        assert states[0].best_offset == -38
        assert states[0].current_offset == -38
        assert states[2].phase == TunerPhase.CONFIRMING
        assert states[2].best_offset == -33

    def test_non_imported_cores_start_coarse(self):
        """Cores NOT in the profile start normal COARSE_SEARCH."""
        cfg = TunerConfig()
        engine = make_test_engine(cfg)
        profile = {0: -38}
        states = engine._init_states_from_profile(profile, all_cores=[0, 1])
        assert states[1].phase == TunerPhase.NOT_STARTED

    def test_import_abandon_after_3_preconfirm_fails(self):
        """3 consecutive preconfirm failures abandon import → COARSE_SEARCH."""
        cs = CoreState(core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM,
                       current_offset=-35, best_offset=-38,
                       consecutive_backoff_fails=2, backoff_mode=True)
        cfg = TunerConfig()
        engine = make_test_engine(cfg)
        engine._imported_cores = {0}  # track which cores were imported
        # 3rd preconfirm failure
        engine._advance_core(cs, passed=False, error_type="computation")
        assert cs.phase == TunerPhase.COARSE_SEARCH  # abandoned
        assert cs.current_offset == cfg.start_offset * cfg.direction
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestImportProfile -v`
Expected: FAIL

- [ ] **Step 3: Implement _init_states_from_profile**

Add to `TunerEngine` in `src/tuner/engine.py`:

```python
    def _init_states_from_profile(
        self, profile: dict[int, int], all_cores: list[int],
    ) -> dict[int, CoreState]:
        """Initialize core states from an imported profile.
        Imported cores enter CONFIRMING. Others start NOT_STARTED."""
        states = {}
        self._imported_cores = set()
        for core_id in all_cores:
            if core_id in profile:
                states[core_id] = CoreState(
                    core_id=core_id,
                    phase=TunerPhase.CONFIRMING,
                    current_offset=profile[core_id],
                    best_offset=profile[core_id],
                    baseline_offset=0,  # will be set from SMU on start
                )
                self._imported_cores.add(core_id)
            else:
                states[core_id] = CoreState(core_id=core_id)
        return states
```

- [ ] **Step 4: Add import abandonment logic to _advance_core**

In `src/tuner/engine.py`, in the BACKOFF_PRECONFIRM failure handling within `_advance_core`, add a check:

```python
        # For imported cores: abandon after 3 consecutive preconfirm fails
        if (hasattr(self, '_imported_cores') and cs.core_id in self._imported_cores
                and cs.consecutive_backoff_fails >= 3
                and cs.backoff_pass_bound is None):
            # Import is stale — restart from scratch
            cs.phase = TunerPhase.COARSE_SEARCH
            cs.current_offset = self._config.start_offset * self._config.direction
            cs.best_offset = None
            cs.coarse_fail_offset = None
            cs.confirm_attempts = 0
            cs.backoff_mode = False
            cs.consecutive_backoff_fails = 0
            cs.backoff_fail_bound = None
            cs.backoff_pass_bound = None
            self._imported_cores.discard(cs.core_id)
            logging.info("Core %d: import abandoned after 3 preconfirm failures — starting fresh", cs.core_id)
            return
```

- [ ] **Step 5: Add start_from_profile public method**

Add to `TunerEngine`:

```python
    def start_from_profile(self, profile: dict[int, int]) -> None:
        """Start a new session using imported CO values as starting points."""
        cores = self._get_cores_to_test()
        self._core_states = self._init_states_from_profile(profile, cores)
        # Read baseline from SMU
        for cs in self._core_states.values():
            if self._smu:
                smu_offset = self._smu.read_co_offset(cs.core_id)
                cs.baseline_offset = smu_offset if smu_offset is not None else 0
        # Create session
        self._session_id = tp.create_session(
            self._db, self._config,
            bios_version=self._read_bios_version(),
            cpu_model=self._read_cpu_model(),
        )
        for cs in self._core_states.values():
            tp.save_core_state(self._db, self._session_id, cs)
        self._set_status(f"imported profile ({len(profile)}/{len(cores)} cores)")
        self._run_next()
```

- [ ] **Step 6: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_engine.py::TestImportProfile -v`
Expected: All PASSED

- [ ] **Step 7: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/tuner/engine.py tests/test_tuner_engine.py
git commit -m "feat(tuner): import profile mode — skip discovery, re-confirm with fast abandon"
```

---

## Task 13: Enhanced in_test DB Record

**Files:**
- Modify: `src/tuner/engine.py` (_start_worker, _on_test_finished)
- Modify: `src/history/db.py` (tuner_core_states schema — add in_test_* columns)
- Test: `tests/test_tuner_persistence.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_tuner_persistence.py`:

```python
class TestEnhancedInTest:
    def test_in_test_context_persisted(self, tmp_path):
        db = HistoryDB(tmp_path / "test.db")
        try:
            sid = db.create_tuner_session("{}", "1.0", "TestCPU")
            cs = CoreState(core_id=0, in_test=True)
            db.upsert_tuner_core_state(sid, cs)
            db.set_in_test_context(sid, 0, backend="mprime",
                                   stress_mode="AVX2", fft_preset="SMALL",
                                   start_timestamp=1711720000.0)
            ctx = db.get_in_test_context(sid, 0)
            assert ctx["backend"] == "mprime"
            assert ctx["stress_mode"] == "AVX2"
            assert ctx["fft_preset"] == "SMALL"
            assert ctx["start_timestamp"] == 1711720000.0
        finally:
            db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_persistence.py::TestEnhancedInTest -v`
Expected: FAIL

- [ ] **Step 3: Add in_test context columns to schema**

In `src/history/db.py`, add to the v8→v9 migration (or a separate v9→v10 if Task 3 already did v9 — check current state):

```python
        ALTER TABLE tuner_core_states ADD COLUMN in_test_backend TEXT;
        ALTER TABLE tuner_core_states ADD COLUMN in_test_stress_mode TEXT;
        ALTER TABLE tuner_core_states ADD COLUMN in_test_fft_preset TEXT;
        ALTER TABLE tuner_core_states ADD COLUMN in_test_start_ts REAL;
```

- [ ] **Step 4: Implement set_in_test_context and get_in_test_context**

Add to `HistoryDB`:

```python
    def set_in_test_context(
        self, session_id: int, core_id: int, *,
        backend: str, stress_mode: str, fft_preset: str,
        start_timestamp: float,
    ) -> None:
        self._conn.execute(
            """UPDATE tuner_core_states
               SET in_test_backend=?, in_test_stress_mode=?,
                   in_test_fft_preset=?, in_test_start_ts=?
               WHERE session_id=? AND core_id=?""",
            (backend, stress_mode, fft_preset, start_timestamp,
             session_id, core_id),
        )

    def get_in_test_context(self, session_id: int, core_id: int) -> dict | None:
        row = self._conn.execute(
            """SELECT in_test_backend, in_test_stress_mode,
                      in_test_fft_preset, in_test_start_ts
               FROM tuner_core_states
               WHERE session_id=? AND core_id=?""",
            (session_id, core_id),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return {
            "backend": row[0], "stress_mode": row[1],
            "fft_preset": row[2], "start_timestamp": row[3],
        }
```

- [ ] **Step 5: Wire into engine _start_worker and _on_test_finished**

In `_start_worker`, after setting `cs.in_test = True`:

```python
        backend, mode, fft = self._get_active_stress_config(cs)
        self._db.set_in_test_context(
            self._session_id, cs.core_id,
            backend=backend, stress_mode=mode, fft_preset=fft,
            start_timestamp=time.time(),
        )
```

In `_on_test_finished`, after setting `cs.in_test = False`, clear the context:

```python
        self._db.clear_in_test_context(self._session_id, core_id)
```

- [ ] **Step 5b: Implement clear_in_test_context in db.py**

Add to `HistoryDB`:

```python
    def clear_in_test_context(self, session_id: int, core_id: int) -> None:
        self._conn.execute(
            """UPDATE tuner_core_states
               SET in_test_backend=NULL, in_test_stress_mode=NULL,
                   in_test_fft_preset=NULL, in_test_start_ts=NULL
               WHERE session_id=? AND core_id=?""",
            (session_id, core_id),
        )
```

- [ ] **Step 6: Update crash detection to use in_test context**

In `_detect_and_handle_crashes`, read the context for better crash logging:

```python
        ctx = self._db.get_in_test_context(session_id, cs.core_id)
        if ctx:
            backend, mode, fft = ctx["backend"], ctx["stress_mode"], ctx["fft_preset"]
        else:
            backend, mode, fft = None, None, None
```

- [ ] **Step 7: Run tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_persistence.py tests/test_tuner_engine.py -v --tb=short`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add src/history/db.py src/tuner/engine.py tests/test_tuner_persistence.py
git commit -m "feat(tuner): enhanced in_test record with backend/mode/fft for crash attribution"
```

---

## Task 14: UI Updates — Phase Labels, Crash Styling, Import Button

**Files:**
- Modify: `src/gui/tuner_tab.py`
- Modify: `src/gui/widgets/core_grid.py`

- [ ] **Step 1: Add hardening phase colors and grid states**

In `src/gui/widgets/core_grid.py`, add to `STATE_COLORS`:

```python
    "hardening": {"bg": "#1a2a3a", "fg": "#64b5f6", "border": "#42a5f5"},  # blue
    "hardened":  {"bg": "#1b3a1b", "fg": "#66bb6a", "border": "#66bb6a"},  # bright green
    "crash":     {"bg": "#4a1010", "fg": "#ff5252", "border": "#d32f2f"},  # dark red
    "reimport":  {"bg": "#2a2a1a", "fg": "#fff176", "border": "#fdd835"},  # yellow
```

- [ ] **Step 2: Update phase-to-grid mapping in tuner_tab.py**

In `src/gui/tuner_tab.py`, update `_PHASE_TO_GRID`:

```python
_PHASE_TO_GRID = {
    TunerPhase.COARSE_SEARCH: "queued",
    TunerPhase.FINE_SEARCH: "queued",
    TunerPhase.CONFIRMING: "queued",
    TunerPhase.CONFIRMED: "passed",
    TunerPhase.SETTLED: "pending",
    TunerPhase.FAILED_CONFIRM: "backoff",
    TunerPhase.NOT_STARTED: "pending",
    TunerPhase.BACKOFF_PRECONFIRM: "backoff",
    TunerPhase.BACKOFF_CONFIRMING: "backoff",
    TunerPhase.HARDENING_T1: "hardening",
    TunerPhase.HARDENING_T2: "hardening",
    TunerPhase.HARDENED: "hardened",
}
```

- [ ] **Step 3: Add phase colors for new phases**

In `src/gui/tuner_tab.py`, update `PHASE_COLORS`:

```python
    TunerPhase.HARDENING_T1: (100, 150, 250),   # blue
    TunerPhase.HARDENING_T2: (80, 130, 230),     # slightly darker blue
    TunerPhase.HARDENED: (80, 200, 80),           # bright green
```

- [ ] **Step 4: Add crash count badge to core grid**

In `src/gui/widgets/core_grid.py`, in `CoreCell.update_status`, show crash count:

```python
        # Show crash count badge if any
        if hasattr(status, 'crash_count') and status.crash_count > 0:
            self._detail_label.setText(
                f"{self._detail_label.text()}  [{status.crash_count} crash{'es' if status.crash_count > 1 else ''}]"
            )
```

- [ ] **Step 5: Add crash styling to test log table**

In `src/gui/tuner_tab.py`, in `_on_test_completed` or `_refresh_log_table`, style CRASH entries:

```python
        if error_type == "crash":
            for col in range(self._log_table.columnCount()):
                item = self._log_table.item(row, col)
                if item:
                    item.setForeground(QColor(255, 82, 82))  # bright red
                    item.setBackground(QColor(74, 16, 16))    # dark red bg
```

- [ ] **Step 6: Add Import Profile button and session picker**

In `src/gui/tuner_tab.py`, in `_build_controls`, add after the existing Start button:

```python
        self._import_btn = QPushButton("Import Profile")
        self._import_btn.setToolTip("Start session from previously confirmed CO values")
        self._import_btn.clicked.connect(self._on_import_profile)
        controls_layout.addWidget(self._import_btn)
```

Implement `_on_import_profile`:

```python
    def _on_import_profile(self) -> None:
        """Show import options: previous session or file."""
        from PySide6.QtWidgets import QMenu, QFileDialog
        menu = QMenu(self)
        # List previous sessions with confirmed cores
        sessions = self._engine._db.list_tuner_sessions(limit=10)
        confirmed_sessions = []
        for s in sessions:
            states = self._engine._db.get_tuner_core_states(s.id)
            confirmed = sum(1 for cs in states.values()
                          if cs.phase in (TunerPhase.CONFIRMED, TunerPhase.HARDENED))
            if confirmed > 0:
                confirmed_sessions.append((s, confirmed))
                label = f"Session {s.id} ({s.created_at[:10]}) — {confirmed} cores, BIOS {s.bios_version}"
                action = menu.addAction(label)
                action.setData(("session", s.id))

        menu.addSeparator()
        file_action = menu.addAction("Import from file...")
        file_action.setData(("file", None))

        chosen = menu.exec(self._import_btn.mapToGlobal(self._import_btn.rect().bottomLeft()))
        if chosen is None:
            return

        source_type, source_id = chosen.data()
        if source_type == "session":
            from history.export import export_tuner_profile, parse_tuner_profile
            json_str = export_tuner_profile(self._engine._db, source_id)
            profile_data = parse_tuner_profile(json_str)
        elif source_type == "file":
            path, _ = QFileDialog.getOpenFileName(self, "Import Profile", "", "JSON (*.json)")
            if not path:
                return
            from history.export import parse_tuner_profile
            with open(path) as f:
                profile_data = parse_tuner_profile(f.read())
        else:
            return

        # Validate
        from history.export import validate_tuner_profile_import
        num_cores = len(self._engine._topology.physical_cores)
        cpu_model = self._engine._topology.cpu_model if hasattr(self._engine._topology, 'cpu_model') else ""
        messages = validate_tuner_profile_import(profile_data, num_cores, cpu_model)
        errors = [m for m in messages if m["level"] == "error"]
        if errors:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.critical(self, "Import Failed", "\n".join(m["message"] for m in errors))
            return

        warnings = [m for m in messages if m["level"] == "warning"]
        if warnings:
            from PySide6.QtWidgets import QMessageBox
            result = QMessageBox.warning(
                self, "Import Warnings",
                "\n".join(m["message"] for m in warnings) + "\n\nContinue anyway?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if result != QMessageBox.Yes:
                return

        self._engine.start_from_profile(profile_data["profile"])
```

- [ ] **Step 7: Show current backend/mode in status**

In `src/gui/tuner_tab.py`, the existing `_on_worker_started` slot (connected to `self._engine.worker_started`) already updates the UI when a core starts testing. Extend it to show the active stress mode:

```python
    @Slot(int)
    def _on_worker_started(self, core_id: int) -> None:
        cs = self._engine.core_states.get(core_id)
        if cs:
            backend, mode, fft = self._engine._get_active_stress_config(cs)
            phase_label = cs.phase.value.replace("_", " ").title()
            self._status_label.setText(
                f"Testing Core {core_id} — {backend} {mode} {fft} — {phase_label} @ {cs.current_offset}"
            )
        self.tuner_core_testing.emit(core_id, "testing")
```

This replaces the existing `_on_worker_started` implementation in tuner_tab.py (around line 760). The `worker_started` signal is already wired in `_wire_engine` (line ~580).

- [ ] **Step 8: Run app manually to verify UI renders**

This is a UI change that requires visual verification. Run the app and confirm:
- Hardening phases show blue colors
- Crash entries appear with red background
- Import button shows session picker menu
- Status bar shows backend/mode/fft during tests

- [ ] **Step 9: Commit**

```bash
git add src/gui/tuner_tab.py src/gui/widgets/core_grid.py
git commit -m "feat(gui): hardening phase colors, crash styling, import profile button"
```

---

## Task 15: Integration Test — Full Pipeline Flow

**Files:**
- Create: `tests/test_tuner_pipeline.py`

- [ ] **Step 1: Write integration test for full pipeline**

Create `tests/test_tuner_pipeline.py`:

```python
"""Integration tests for the multi-mode tuning pipeline.

These tests verify the full flow from discovery through hardening and validation,
using mock backends to simulate pass/fail scenarios without real stress testing.
"""
import pytest
from tuner.config import TunerConfig
from tuner.state import CoreState, TunerPhase


class TestPipelineFlow:
    """Verify the full state machine flow for a single core."""

    def test_happy_path_fresh_start(self, make_test_engine):
        """Core progresses: COARSE → FINE → SETTLED → CONFIRM → T1 → T2 → HARDENED."""
        cfg = TunerConfig(
            coarse_step=5, fine_step=1, direction=-1, max_offset=-50,
            search_duration_seconds=60, confirm_duration_seconds=300,
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
                {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
            ],
        )
        engine = make_test_engine(cfg)
        cs = CoreState(core_id=0, phase=TunerPhase.NOT_STARTED)

        # Coarse: pass until -30, fail at -35
        engine._advance_core(cs, passed=True, error_type=None)  # NOT_STARTED → COARSE
        assert cs.phase == TunerPhase.COARSE_SEARCH
        for offset in range(-5, -35, -5):
            cs.current_offset = offset
            cs.best_offset = offset
            engine._advance_core(cs, passed=True, error_type=None)
        cs.current_offset = -35
        engine._advance_core(cs, passed=False, error_type="computation")
        assert cs.phase == TunerPhase.FINE_SEARCH

        # Fine: pass at -31, settles
        cs.current_offset = -31
        cs.best_offset = -31
        engine._advance_core(cs, passed=True, error_type=None)
        # Eventually settles
        while cs.phase == TunerPhase.FINE_SEARCH:
            cs.current_offset = cs.best_offset - 1
            engine._advance_core(cs, passed=False, error_type="computation")

        assert cs.phase in (TunerPhase.SETTLED, TunerPhase.CONFIRMING)
        if cs.phase == TunerPhase.SETTLED:
            engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.CONFIRMING

        # Confirm: pass
        engine._advance_core(cs, passed=True, error_type=None)
        # With hardening tiers, CONFIRMED → HARDENING_T1
        assert cs.phase == TunerPhase.HARDENING_T1

        # T1 pass → T2
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENING_T2

        # T2 pass → HARDENED
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENED

    def test_hardening_failure_and_recovery(self, make_test_engine):
        """Core fails T1, backs off, passes T1 at conservative offset, then T2."""
        cfg = TunerConfig(
            fine_step=1, direction=-1,
            hardening_tiers=[
                {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
                {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
            ],
        )
        engine = make_test_engine(cfg)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1,
                       current_offset=-38, best_offset=-38, baseline_offset=0)

        # T1 fail → back off to -37
        engine._advance_core(cs, passed=False, error_type="computation")
        assert cs.phase == TunerPhase.HARDENING_T1
        assert cs.current_offset == -37

        # T1 pass → T2
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENING_T2

        # T2 fail → back off to -36, retry T2
        engine._advance_core(cs, passed=False, error_type="computation")
        assert cs.phase == TunerPhase.HARDENING_T2
        assert cs.current_offset == -36

        # T2 pass → HARDENED
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENED
        assert cs.best_offset == -36

    def test_crash_penalty_and_cooldown(self, make_test_engine):
        """Crash applies 3x penalty and sets cooldown."""
        cfg = TunerConfig(crash_penalty_steps=3, fine_step=1, direction=-1)
        engine = make_test_engine(cfg)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-38, best_offset=-36, in_test=True)
        engine._apply_crash_penalty(cs)
        assert cs.current_offset == -35  # -38 + 3 = -35
        assert cs.crash_count == 1
        assert cs.crash_cooldown == 2
        assert cs.backoff_fail_bound == -38

    def test_time_budget_terminates_search(self, make_test_engine):
        """Core exceeding time budget settles without death spiral."""
        cfg = TunerConfig(max_core_time_seconds=7200)
        engine = make_test_engine(cfg)
        cs = CoreState(core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM,
                       current_offset=-20, best_offset=-25,
                       cumulative_test_time=7201.0)
        settled = engine._check_time_budget(cs)
        assert settled is True
        assert cs.phase == TunerPhase.CONFIRMED
        assert cs.current_offset == -25

    def test_import_profile_confirm_and_harden(self, make_test_engine):
        """Imported profile values confirm quickly and enter hardening."""
        cfg = TunerConfig(hardening_tiers=[
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ])
        engine = make_test_engine(cfg)
        profile = {0: -38}
        states = engine._init_states_from_profile(profile, all_cores=[0, 1])

        cs = states[0]
        assert cs.phase == TunerPhase.CONFIRMING
        assert cs.current_offset == -38

        # Confirm passes → hardening
        engine._advance_core(cs, passed=True, error_type=None)
        assert cs.phase == TunerPhase.HARDENING_T1
```

- [ ] **Step 2: Run integration tests**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/test_tuner_pipeline.py -v`
Expected: All PASSED

- [ ] **Step 3: Run full suite**

Run: `cd /home/user/Documents/nix/repos/linux-corecycler && python -m pytest tests/ -v --tb=short`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_tuner_pipeline.py
git commit -m "test: integration tests for multi-mode tuning pipeline"
```

---

## Task 16: Update README — Document New Features

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add multi-mode pipeline section to README**

In the Auto-Tuner section of `README.md`, add documentation for:
- The hardening pipeline (discovery → confirm → harden T1/T2 → validate S1-S4)
- New config options (hardening_tiers, max_core_time_seconds, crash_penalty_steps, validate_transitions)
- Import profile mode (BIOS update workflow)
- Crash detection and recovery behavior
- Recommended settings for different use cases

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document multi-mode pipeline, crash detection, import profile"
```

---

## Summary

| Task | Component | New/Modified |
|------|-----------|-------------|
| 1 | State machine phases + fields | state.py |
| 2 | Config options | config.py |
| 3 | Schema migration v9 | db.py |
| 4 | Profile export/import | export.py, persistence.py |
| 5 | Crash detection on resume | engine.py |
| 6 | Death spiral prevention | engine.py |
| 7 | Safety ramp | engine.py |
| 8 | Crash-aware scheduling | engine.py |
| 9 | Hardening transitions | engine.py |
| 10 | S4 rapid transitions | scheduler.py, engine.py |
| 11 | Multi-mode validation | engine.py |
| 12 | Import profile mode | engine.py |
| 13 | Enhanced in_test record | db.py, engine.py |
| 14 | UI updates | tuner_tab.py, core_grid.py |
| 15 | Integration tests | test_tuner_pipeline.py |
| 16 | Documentation | README.md |

Tasks 1-4 are foundation (data model + persistence). Tasks 5-9 are core engine logic. Tasks 10-12 are new features. Tasks 13-16 are polish and integration.
