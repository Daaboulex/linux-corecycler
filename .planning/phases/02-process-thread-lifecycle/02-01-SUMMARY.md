---
phase: 02-process-thread-lifecycle
plan: 01
subsystem: engine
tags: [subprocess, PR_SET_PDEATHSIG, prctl, process-group, signal, cleanup]

# Dependency graph
requires:
  - phase: 01-core-engine-fixes
    provides: "CoreScheduler with _kill_current SIGTERM->SIGKILL escalation pattern"
provides:
  - "PR_SET_PDEATHSIG on all subprocess launches -- orphan stress processes impossible"
  - "Crash-safe _cleanup_on_exit with independent try/except per subsystem"
  - "_make_preexec() staticmethod combining os.setsid + prctl(PR_SET_PDEATHSIG, SIGKILL)"
  - "Memory tab kill escalation matching scheduler _kill_current pattern"
  - "Codebase audit test enforcing no bare preexec_fn=os.setsid"
affects: [02-process-thread-lifecycle]

# Tech tracking
tech-stack:
  added: [ctypes/prctl for PR_SET_PDEATHSIG]
  patterns: [_make_preexec combining setsid+pdeathsig, SIGTERM->wait->SIGKILL escalation, independent try/except cleanup]

key-files:
  created: []
  modified:
    - src/engine/scheduler.py
    - src/gui/memory_tab.py
    - src/main.py
    - tests/test_scheduler.py

key-decisions:
  - "_make_preexec as staticmethod on CoreScheduler, inline closure in memory_tab (QThread context)"
  - "PR_SET_PDEATHSIG sends SIGKILL (not SIGTERM) -- ensures child dies even if it ignores signals"
  - "Replaced hasattr() with try/except(Exception) -- more robust for partially-initialized state"

patterns-established:
  - "_make_preexec pattern: all subprocess.Popen calls must use combined setsid+PR_SET_PDEATHSIG preexec_fn"
  - "Independent try/except blocks for multi-subsystem cleanup -- one failure cannot cascade"
  - "Codebase audit test pattern: test_no_bare_setsid_in_src prevents regression via src/ file scan"

requirements-completed: [ENG-04]

# Metrics
duration: 3min
completed: 2026-03-19
---

# Phase 02 Plan 01: Process Cleanup Summary

**PR_SET_PDEATHSIG on all subprocess launches with crash-safe cleanup and SIGTERM->SIGKILL kill escalation**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-19T14:07:03Z
- **Completed:** 2026-03-19T14:10:47Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- All subprocess.Popen calls now use PR_SET_PDEATHSIG(SIGKILL) via _make_preexec() -- child processes die automatically when parent exits
- Memory tab _StressWorker.stop() hardened with SIGTERM->wait(3)->SIGKILL->wait(2) escalation matching scheduler pattern
- _cleanup_on_exit() crash-safe with independent try/except per subsystem, no more fragile hasattr() checks
- Codebase audit test (test_no_bare_setsid_in_src) prevents future regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Add PR_SET_PDEATHSIG to all subprocess launch sites and harden memory_tab kill** - `7c5cea2` (test: RED), `b1cb18d` (feat: GREEN)
2. **Task 2: Harden _cleanup_on_exit with try/except around each subsystem** - `9bc427e` (feat)

_Note: Task 1 was TDD with RED->GREEN commits_

## Files Created/Modified
- `src/engine/scheduler.py` - Added _make_preexec() staticmethod, replaced bare os.setsid at both Popen call sites
- `src/gui/memory_tab.py` - Added PR_SET_PDEATHSIG preexec, hardened stop() with SIGTERM->SIGKILL escalation
- `src/main.py` - Replaced hasattr() with try/except(Exception) around each subsystem cleanup
- `tests/test_scheduler.py` - Added TestProcessCleanup with _make_preexec and codebase audit tests

## Decisions Made
- _make_preexec as staticmethod on CoreScheduler, inline closure in memory_tab (QThread has different import context)
- PR_SET_PDEATHSIG sends SIGKILL (not SIGTERM) -- ensures child dies even if stress tool ignores softer signals
- Replaced hasattr() with try/except(Exception) -- catches AttributeError naturally, more robust for partially-initialized state

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Process lifecycle hardening complete for subprocess launches
- Ready for Plan 02 (thread lifecycle / QThread management)
- All 48 scheduler tests passing

## Self-Check: PASSED

All 4 modified files exist. All 3 commits (7c5cea2, b1cb18d, 9bc427e) verified in git log.

---
*Phase: 02-process-thread-lifecycle*
*Completed: 2026-03-19*
