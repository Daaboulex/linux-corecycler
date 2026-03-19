---
phase: 03-pm-table-clocks-voltages
plan: 02
subsystem: gui
tags: [memory-tab, pm-table, fclk, uclk, mclk, voltage, qtimer, graceful-degradation]

requires:
  - phase: 03-pm-table-clocks-voltages
    provides: "PMTableReader with version-dispatch, PMTableData with memory fields, compute_fclk_uclk_ratio"
provides:
  - "Memory Controller QGroupBox in MemoryTab with FCLK/UCLK/MCLK clock labels and ratio indicator"
  - "VDD/VDDQ voltage labels from PM table in Memory Controller group"
  - "Unified QTimer replacing separate temp timer, using AppSettings.poll_interval (1s default)"
  - "Graceful degradation: calibrated, uncalibrated, and driver-missing states"
  - "Headless behavioral tests for MemoryTab label-update methods"
affects: [memory-tab, gui, phase-04]

tech-stack:
  added: []
  patterns:
    - "Unified QTimer for multiple data sources (PM table + SPD temps) in single tick"
    - "SimpleNamespace + MethodType for headless Qt widget testing without pytest-qt"
    - "Tolerance-based ratio check (5%) instead of round() for FCLK:UCLK"

key-files:
  created: []
  modified:
    - "src/gui/memory_tab.py"
    - "tests/test_memory_monitor.py"
    - "src/smu/pmtable.py"

key-decisions:
  - "Headless test approach using SimpleNamespace + MethodType to avoid pytest-qt dependency"
  - "Tolerance-based ratio check (abs(ratio - target) < 0.05) replaces round() to avoid banker's rounding edge cases"
  - "Driver-missing state uses hidden clock/voltage labels with visible message label rather than disabling group box"

patterns-established:
  - "Unified timer pattern: single QTimer drives multiple data source reads in one callback"
  - "Headless Qt widget testing: SimpleNamespace with bound methods for label-level behavioral tests"

requirements-completed: [MEM-01, MEM-02, MEM-05, MEM-06]

duration: 5min
completed: 2026-03-19
---

# Phase 03 Plan 02: Memory Controller GUI with Live Clocks and Voltages Summary

**Memory Controller QGroupBox with live FCLK/UCLK/MCLK clocks, ratio indicator, VDD voltage, unified 1s timer, and headless behavioral tests covering calibrated/uncalibrated/unavailable states**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-19T15:11:01Z
- **Completed:** 2026-03-19T15:15:50Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Memory Controller QGroupBox displays live FCLK/UCLK/MCLK with color-coded ratio indicator (green 1:1, yellow 1:2)
- VDD voltage from PM table shown as running voltage, VDDQ shows "--" (uncalibrated offset)
- Unified QTimer at AppSettings.poll_interval (1s default) replaces old 2-second temp-only timer
- Graceful degradation: calibrated Verified label, uncalibrated shows float count, missing driver shows clear message
- 18 new tests added (8 ratio, 2 PMTableData fields, 8 MemoryTab behavioral), all 26 memory monitor tests pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Add Memory Controller QGroupBox and unified timer** - `1af925c` (feat)
2. **Task 2: Add tests for MemoryTab live update behavior** - `b90bd57` (test)

## Files Created/Modified
- `src/gui/memory_tab.py` - Added Memory Controller QGroupBox with clock/voltage labels, unified QTimer, _update_live_data and helper methods for calibrated/uncalibrated/unavailable states
- `tests/test_memory_monitor.py` - Added TestFCLKUCLKRatio (8 tests), TestPMTableDataMemoryFields (2 tests), TestMemoryTabBehavior (8 tests) using headless SimpleNamespace approach
- `src/smu/pmtable.py` - Fixed compute_fclk_uclk_ratio to use tolerance-based check instead of round()

## Decisions Made
- Used headless SimpleNamespace + MethodType approach for MemoryTab behavioral tests since pytest-qt is not available in dev shell -- avoids adding test dependencies while still providing full behavioral coverage
- Fixed compute_fclk_uclk_ratio to use tolerance-based check (5% of target) instead of Python's round() which uses banker's rounding and misclassifies edge cases like 2000/5000=2.5 as 1:2
- Driver-missing state hides individual clock/voltage labels and shows a "Requires ryzen_smu driver" message label, keeping the group box visible for context

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed compute_fclk_uclk_ratio banker's rounding edge case**
- **Found during:** Task 2 (test_unexpected_ratio_returns_none)
- **Issue:** Python's round(2.5) returns 2 (banker's rounding), causing compute_fclk_uclk_ratio(2000, 5000) to wrongly return (1, 2) instead of None
- **Fix:** Replaced round() with tolerance-based check: abs(ratio - target) < 0.05 for both 1:1 and 1:2
- **Files modified:** src/smu/pmtable.py
- **Verification:** All 8 ratio tests in test_memory_monitor.py pass, all 8 ratio tests in test_pmtable.py still pass
- **Committed in:** b90bd57 (Task 2 commit)

**2. [Rule 3 - Blocking] Adjusted test approach for missing pytest-qt**
- **Found during:** Task 2 (test infrastructure check)
- **Issue:** pytest-qt not available in nix dev shell, cannot construct QWidget instances
- **Fix:** Used SimpleNamespace + types.MethodType to bind MemoryTab methods to mock label objects for headless behavioral testing
- **Files modified:** tests/test_memory_monitor.py
- **Verification:** All 8 TestMemoryTabBehavior tests pass without Qt event loop
- **Committed in:** b90bd57 (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (1 bug, 1 blocking)
**Impact on plan:** Bug fix improves ratio computation correctness. Headless testing provides equivalent behavioral coverage without pytest-qt dependency. No scope creep.

## Issues Encountered

Pre-existing test failure in `tests/test_history_logger.py::TestTestCompletion::test_on_test_completed` (json.loads called with dict instead of str). Confirmed pre-existing, unrelated to this plan.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Memory Controller group box fully wired with PM table data -- live clocks, voltages, and ratio visible during stress testing
- Phase 3 complete: both PM table parsing (Plan 01) and GUI display (Plan 02) done
- Ready for Phase 4 (SPD5118 EEPROM decode) which adds timing details to the existing DIMM table

## Self-Check: PASSED

All files exist, all commits verified.

---
*Phase: 03-pm-table-clocks-voltages*
*Completed: 2026-03-19*
