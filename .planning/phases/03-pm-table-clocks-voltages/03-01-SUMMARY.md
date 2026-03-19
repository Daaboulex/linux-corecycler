---
phase: 03-pm-table-clocks-voltages
plan: 01
subsystem: smu
tags: [pm-table, sysfs, struct, dataclass, zen5, fclk, uclk, mclk, voltage]

requires:
  - phase: 02-process-thread-lifecycle
    provides: "Stable process/thread lifecycle for reliable sysfs reads"
provides:
  - "PMTableOffsets frozen dataclass for version-specific byte offset maps"
  - "PM_TABLE_OFFSETS registry with 4 verified Granite Ridge versions"
  - "Zen 5 prefix fallback for unknown 0x62xxxx PM table versions"
  - "PMTableData extended with fclk_mhz, uclk_mhz, mclk_mhz, vddcr_soc_v, vdd_mem_v, pm_table_version, is_calibrated"
  - "compute_fclk_uclk_ratio helper returning (1,1) or (1,2) tuple"
  - "Version-aware read() with graceful degradation for unknown versions"
affects: [03-02, memory-tab, gui]

tech-stack:
  added: []
  patterns:
    - "Version-keyed offset registry (PM_TABLE_OFFSETS dict) for dispatch"
    - "Prefix-based fallback matching for version families (0x62xxxx -> Zen 5)"
    - "_read_float with bounds checking for safe byte-offset access"
    - "is_calibrated flag for graceful uncalibrated state"

key-files:
  created: []
  modified:
    - "src/smu/pmtable.py"
    - "tests/test_pmtable.py"

key-decisions:
  - "Zen 5 prefix fallback uses vdd_mem=-1 (conservative -- only exact version matches get VDD_MEM)"
  - "_parse_granite_ridge always runs for legacy core-level data regardless of version dispatch"
  - "_read_float returns 0.0 for out-of-range offsets rather than raising exceptions"

patterns-established:
  - "Version-dispatch pattern: exact match -> prefix fallback -> legacy parsing"
  - "PMTableOffsets with -1 sentinel for unavailable fields"

requirements-completed: [MEM-01, MEM-02, MEM-05]

duration: 8min
completed: 2026-03-19
---

# Phase 03 Plan 01: PM Table Version-Aware Parsing Summary

**Version-dispatched PM table parsing with PMTableOffsets registry, Zen 5 prefix fallback, and compute_fclk_uclk_ratio for 1:1/1:2 memory controller ratio detection**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-19T14:59:00Z
- **Completed:** 2026-03-19T15:07:48Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 2

## Accomplishments
- PMTableReader dispatches to correct offset map based on pm_table_version read from sysfs
- Known PM table version 0x00620205 produces correct FCLK/UCLK/MCLK and VDD values
- Unknown PM table version produces is_calibrated=False with raw_floats available
- FCLK:UCLK ratio computed as 1:1 or 1:2 from parsed clock values
- All 37 pmtable tests pass, full suite 835 tests pass (1 pre-existing failure in unrelated test_history_logger excluded)

## Task Commits

Each task was committed atomically (TDD RED then GREEN):

1. **Task 1 (RED): Failing tests for version-dispatch parsing** - `c8bed31` (test)
2. **Task 1 (GREEN): Version-aware PM table parsing implementation** - `77a4231` (feat)

## Files Created/Modified
- `src/smu/pmtable.py` - Added PMTableOffsets, PM_TABLE_OFFSETS, _read_float, _find_prefix_offsets, compute_fclk_uclk_ratio; extended PMTableData and PMTableReader.read() with version dispatch
- `tests/test_pmtable.py` - Added 20 new tests: TestPMTableOffsets, TestPMTableDataNewFields, TestVersionDispatch, TestComputeFclkUclkRatio; plus _make_smu_dir and _build_versioned_pm_table helpers

## Decisions Made
- Zen 5 prefix fallback uses vdd_mem=-1 (conservative -- only exact version matches expose VDD_MEM to avoid displaying garbage)
- _parse_granite_ridge always runs for legacy core-level data (per-core freq/voltage/temp/power) regardless of version dispatch, ensuring backward compatibility
- _read_float returns 0.0 for out-of-range or negative offsets rather than raising exceptions, enabling safe parsing of undersized PM tables

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

Pre-existing test failure in `tests/test_history_logger.py::TestTestCompletion::test_on_test_completed` (json.loads called with dict instead of str). Confirmed pre-existing, unrelated to this plan. Logged to `deferred-items.md`.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- PMTableData now carries fclk_mhz, uclk_mhz, mclk_mhz, vddcr_soc_v, vdd_mem_v, pm_table_version, is_calibrated -- ready for Plan 02 (Memory Controller GUI)
- compute_fclk_uclk_ratio ready for FCLK:UCLK ratio indicator display
- is_calibrated flag drives "Verified" vs "Uncalibrated" label logic in MemoryTab

## Self-Check: PASSED

All files exist, all commits verified.

---
*Phase: 03-pm-table-clocks-voltages*
*Completed: 2026-03-19*
