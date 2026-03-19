---
phase: 06-ui-data-consistency
plan: 02
subsystem: ui
tags: [pyside6, qtimer, staleness, poll-interval, sensor-monitoring]

requires:
  - phase: 06-ui-data-consistency
    provides: CoreGrid telemetry pipeline fix (Plan 01)

provides:
  - MonitorTab poll_interval propagation from AppSettings
  - Visual staleness indicators (grey labels) after consecutive sensor failures
  - Narrowed exception handling (OSError/ValueError/PermissionError instead of bare Exception)
  - Auto-recovery of label styles on successful sensor read

affects: []

tech-stack:
  added: []
  patterns:
    - "Staleness tracking: _hwmon_fail_count/_power_fail_count with _STALE_THRESHOLD=3 for grey-out"
    - "Class-level _NORMAL_STYLE/_STALE_STYLE constants for consistent label styling"

key-files:
  created: []
  modified:
    - src/gui/monitor_tab.py
    - tests/test_ui_consistency.py

key-decisions:
  - "Class-level constants (_STALE_THRESHOLD, _NORMAL_STYLE, _STALE_STYLE) instead of module or instance level"
  - "Shared _hwmon_fail_count for tctl and vcore (both come from same hwmon.read() call)"
  - "Separate _power_fail_count for power since it has independent sysfs/MSR fallback path"

patterns-established:
  - "Staleness grey-out: _fail_count >= _STALE_THRESHOLD -> setStyleSheet(_STALE_STYLE), reset on success"

requirements-completed: [UI-01]

duration: 3min
completed: 2026-03-19
---

# Phase 06 Plan 02: MonitorTab Sensor Reliability Summary

**MonitorTab poll_interval propagation from AppSettings, grey staleness indicators after 3 consecutive sensor failures, and narrowed exception handling to OSError/ValueError/PermissionError**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-19T17:23:09Z
- **Completed:** 2026-03-19T17:27:03Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- MonitorTab.__init__ and start_monitoring() now read AppSettings.poll_interval instead of hardcoding 1000ms
- Tctl, Vcore, and Package Power labels turn grey (#666) after 3 consecutive None sensor reads, preserving last-known values
- Labels auto-recover normal styling when sensor reads succeed again (fail counter resets to 0)
- Replaced overly broad `contextlib.suppress(Exception)` with targeted `except (OSError, ValueError, PermissionError)` so real bugs surface

## Task Commits

Each task was committed atomically:

1. **Task 1: Add staleness and poll_interval tests** - `553a67e` (test)
2. **Task 2: Implement poll_interval propagation, staleness, narrowed exceptions** - `493bdde` (feat)

_TDD flow: Task 1 created failing tests (RED), Task 2 made them pass (GREEN)_

## Files Created/Modified
- `tests/test_ui_consistency.py` - 5 new tests for poll_interval, staleness indicator/recovery, narrowed exceptions
- `src/gui/monitor_tab.py` - poll_interval propagation, _STALE_THRESHOLD/fail_count mechanism, narrowed exception types

## Decisions Made
- Class-level constants for staleness thresholds and styles (not module-level or instance-level) to keep MonitorTab self-contained
- Shared _hwmon_fail_count for tctl and vcore since both originate from same hwmon.read() call
- Separate _power_fail_count for power labels due to independent sysfs RAPL / MSR fallback path

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 06 complete -- all UI data consistency issues addressed
- MonitorTab sensor reliability improvements complement Plan 01's CoreGrid telemetry pipeline fix

## Self-Check: PASSED

---
*Phase: 06-ui-data-consistency*
*Completed: 2026-03-19*
