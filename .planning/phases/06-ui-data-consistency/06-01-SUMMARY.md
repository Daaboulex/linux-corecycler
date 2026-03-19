---
phase: 06-ui-data-consistency
plan: 01
subsystem: ui
tags: [pyside6, telemetry, signal-slot, thread-safety, coregrid]

requires:
  - phase: 02-process-thread-lifecycle
    provides: Signal/slot cache pattern for thread-safe GUI access

provides:
  - Working CoreGridWidget telemetry pipeline via _feed_core_grid_telemetry
  - Signal-cached _active_test_core replacing cross-thread scheduler access
  - Tests preventing regression on telemetry pipeline and thread safety

affects: [06-ui-data-consistency]

tech-stack:
  added: []
  patterns:
    - "_active_test_core signal cache for active core identity in GUI thread"

key-files:
  created:
    - tests/test_ui_consistency.py
  modified:
    - src/gui/main_window.py

key-decisions:
  - "Docstring must not contain literal 'scheduler._current_core' to pass codebase audit test"

patterns-established:
  - "_active_test_core cache: _on_core_started sets it, _feed_core_grid_telemetry reads it, _cleanup_worker clears it"

requirements-completed: [UI-02]

duration: 3min
completed: 2026-03-19
---

# Phase 06 Plan 01: CoreGrid Telemetry Pipeline Summary

**Fixed CoreGridWidget telemetry by replacing broken _poll_core_telemetry(scheduler) with signal-cached _feed_core_grid_telemetry() using _active_test_core**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-19T17:17:21Z
- **Completed:** 2026-03-19T17:20:35Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Eliminated NameError in _update_elapsed that prevented CoreGridWidget from showing any live telemetry during test runs
- Replaced cross-thread scheduler._current_core access with signal-cached _active_test_core (Phase 2 safety pattern)
- Added 5 regression tests covering telemetry pipeline, signal caching, cross-thread audit, and edge cases

## Task Commits

Each task was committed atomically:

1. **Task 1: Create test scaffold for UI consistency** - `b0eb303` (test)
2. **Task 2: Fix telemetry pipeline** - `24d6a75` (feat)

_TDD flow: Task 1 created failing tests (RED), Task 2 made them pass (GREEN)_

## Files Created/Modified
- `tests/test_ui_consistency.py` - 5 tests for telemetry pipeline correctness and cross-thread safety
- `src/gui/main_window.py` - Replaced _poll_core_telemetry with _feed_core_grid_telemetry, added _active_test_core cache

## Decisions Made
- Docstring for _feed_core_grid_telemetry avoids literal "scheduler._current_core" string to pass codebase audit test (uses "scheduler state directly" phrasing instead)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Docstring contained audit-triggering string**
- **Found during:** Task 2
- **Issue:** _feed_core_grid_telemetry docstring contained literal "scheduler._current_core" which the codebase audit test (test_no_cross_thread_scheduler_access) correctly flagged
- **Fix:** Rephrased to "reading scheduler state directly" to avoid false positive while preserving documentation intent
- **Files modified:** src/gui/main_window.py
- **Verification:** All 5 tests pass including audit test
- **Committed in:** 24d6a75 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Minor docstring wording change. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CoreGridWidget telemetry pipeline is functional for Plan 02 work
- _active_test_core cache pattern established for any future active-core-dependent UI updates

## Self-Check: PASSED

---
*Phase: 06-ui-data-consistency*
*Completed: 2026-03-19*
