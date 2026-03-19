---
phase: 01-core-engine-fixes
plan: 01
subsystem: engine
tags: [scheduler, stall-detection, affinity, proc-fs, sched_setaffinity]

# Dependency graph
requires: []
provides:
  - "5-second startup grace period in stall detection (_STALL_GRACE_SECONDS)"
  - "Periodic child TID affinity scanner via /proc/pid/task/*/status"
  - "Automatic re-pinning of drifted threads with os.sched_setaffinity()"
affects: [02-process-lifecycle]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "proc_base parameter for testable /proc access"
    - "Grace period guard wrapping stall watchdog"
    - "Periodic affinity check with configurable interval"

key-files:
  created: []
  modified:
    - src/engine/scheduler.py
    - tests/test_scheduler.py

key-decisions:
  - "Added proc_base kwarg to _verify_child_affinity for testability without mocking Path globally"
  - "Reset stall baseline (last_active_time) when grace period ends so startup time never counts toward stall timeout"

patterns-established:
  - "TDD: RED-GREEN commit flow for engine features"
  - "Periodic affinity check pattern (_AFFINITY_CHECK_INTERVAL = 2.0s)"

requirements-completed: [ENG-01, ENG-02, ENG-03]

# Metrics
duration: 5min
completed: 2026-03-19
---

# Phase 1 Plan 1: Core Engine Fixes Summary

**Stall detection with 5s startup grace period and periodic child TID affinity verification via /proc/pid/task/*/status with automatic re-pinning**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-19T13:43:29Z
- **Completed:** 2026-03-19T13:49:14Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Stall detection no longer fires false positives during the first 5 seconds of benchmark startup on each core
- Child threads of stress backends (mprime, etc.) are now verified running on the target core every 2 seconds via /proc/pid/task/*/status
- Drifted child threads are automatically re-pinned with os.sched_setaffinity() -- this fixes the root cause of "stays on first core" bug
- 7 new tests (3 stall grace period + 4 child affinity) plus all 38 existing tests pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Stall grace period (TDD)**
   - `76e8f43` test(01-01): add failing tests for stall detection grace period
   - `71e8cfd` feat(01-01): add startup grace period to stall detection

2. **Task 2: Child TID affinity verification (TDD)**
   - `fb4a704` test(01-01): add failing tests for child TID affinity verification
   - `5a85973` feat(01-01): add periodic child TID affinity verification and re-pinning

_TDD tasks each have RED (test) and GREEN (feat) commits._

## Files Created/Modified
- `src/engine/scheduler.py` - Added _STALL_GRACE_SECONDS constant, grace period guard in _run_stress_phase, _verify_child_affinity static method, periodic affinity checking
- `tests/test_scheduler.py` - TestStallGracePeriod (3 tests) and TestChildAffinityVerification (4 tests)

## Decisions Made
- Added `proc_base` keyword argument to `_verify_child_affinity` for testability -- avoids fragile global Path mocking while keeping the method a static method
- Reset `last_active_time` when grace period ends so that startup time never accumulates toward the stall timeout threshold
- Kept original `_verify_affinity` static method for backward compatibility (it's no longer called from _run_stress_phase but may be used elsewhere)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Core cycling engine now has reliable stall detection and affinity enforcement
- Plan 01-02 (Signal(dict) crash fix) already completed separately
- Phase 2 (Process & Thread Lifecycle) can proceed -- depends on stable core cycling engine which is now delivered

## Self-Check: PASSED

All files and commits verified.

---
*Phase: 01-core-engine-fixes*
*Completed: 2026-03-19*
