---
phase: 05-history-database-integrity
plan: 02
subsystem: database
tags: [sqlite, historydb, sql-aggregation, stale-recovery, test-fix, view-consistency]

# Dependency graph
requires:
  - phase: 05-01
    provides: "13 public HistoryDB methods including get_status_counts and __conn encapsulation"
provides:
  - "SQL-based summary counters via get_status_counts() eliminating limit=500 truncation"
  - "List-based recover_incomplete_runs with per-session (id, started_at) details"
  - "Per-session stale recovery logging in main_window startup"
  - "Fixed test_on_test_completed matching Phase 1 Signal(str) JSON contract"
  - "View consistency tests proving Grouped and Tuner views share HistoryDB methods"
affects: [05-history-database-integrity]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "SQL GROUP BY aggregation for dashboard summary counters instead of Python-side counting"
    - "SELECT-then-UPDATE pattern for recovery with per-row detail logging"

key-files:
  created: []
  modified:
    - "src/history/db.py"
    - "src/gui/history_tab.py"
    - "src/gui/main_window.py"
    - "tests/test_history_db.py"
    - "tests/test_history_logger.py"

key-decisions:
  - "Summary counters use SQL GROUP BY (get_status_counts) to count ALL runs, not Python-side counting from limit=500 list"
  - "recover_incomplete_runs SELECT before UPDATE to capture per-session details for logging"

patterns-established:
  - "Dashboard summary uses SQL aggregation for accurate counts regardless of pagination limits"
  - "Recovery operations return details (not just count) for structured logging"

requirements-completed: [HIST-01, HIST-02, HIST-04]

# Metrics
duration: 3min
completed: 2026-03-19
---

# Phase 05 Plan 02: History Counters and Recovery Summary

**SQL-based summary counters replacing Python-side limit=500 counting, plus per-session stale recovery logging and Signal(str) test fix**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-19T16:44:51Z
- **Completed:** 2026-03-19T16:48:44Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- _update_summary now uses get_status_counts() SQL aggregation, counting ALL runs regardless of the 500-row page limit
- recover_incomplete_runs returns list of (id, started_at) tuples; main_window logs each recovered session individually
- Fixed test_on_test_completed to pass JSON string via json.dumps matching Phase 1 Signal(str) contract
- Added TestTunerSessionMethods and TestStatusCounts test classes verifying HIST-01 and HIST-04

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix summary counters with SQL aggregation and enhance stale session recovery** - `8f1486d` (test) + `d0ae6e9` (feat) [TDD: red then green]
2. **Task 2: Fix test_on_test_completed to pass JSON string and verify view consistency** - `23096c0` (fix)

## Files Created/Modified
- `src/history/db.py` - recover_incomplete_runs returns list[tuple[int, str]] instead of int
- `src/gui/history_tab.py` - _update_summary uses get_status_counts() SQL aggregation
- `src/gui/main_window.py` - Per-session stale recovery logging with run_id and started_at
- `tests/test_history_db.py` - TestStatusCounts (3 tests), TestTunerSessionMethods (3 tests), updated recover test
- `tests/test_history_logger.py` - test_on_test_completed fixed to use json.dumps with plain dicts

## Decisions Made
- Used SQL GROUP BY via existing get_status_counts() method rather than Python-side counting to eliminate the limit=500 truncation bug
- SELECT-then-UPDATE pattern in recover_incomplete_runs to capture per-row details before bulk update

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All HIST-01, HIST-02, HIST-04 requirements complete
- Phase 05 (History & Database Integrity) fully done -- both plans executed
- Full test suite green (895 tests)

## Self-Check: PASSED

All 5 modified files exist. All 3 commits (8f1486d, d0ae6e9, 23096c0) verified in git log.

---
*Phase: 05-history-database-integrity*
*Completed: 2026-03-19*
