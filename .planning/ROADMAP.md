# Roadmap: CoreCyclerLx

## Overview

CoreCyclerLx is a mature Linux per-core CPU stability tester whose primary function (core cycling) is currently broken. This roadmap follows a bugs-first, features-second strategy: fix the core engine and process lifecycle (Phases 1-2) so test results are trustworthy, then complete the memory diagnostics panel with PM table clocks/voltages and SPD timing decode (Phases 3-4), fix history database integrity (Phase 5), and finish with a cross-cutting UI data consistency audit (Phase 6).

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Core Engine Fixes** - Fix core cycling, stall detection, affinity verification, and Signal crash so per-core testing actually works
- [ ] **Phase 2: Process & Thread Lifecycle** - Harden subprocess cleanup, QThread shutdown, and cross-thread safety to prevent orphans and stale state
- [ ] **Phase 3: PM Table Clocks & Voltages** - Display running FCLK/UCLK/MCLK and VDD/VDDQ from version-aware PM table parsing with real-time updates
- [ ] **Phase 4: SPD Timings & Memory UI** - Decode DDR5 timing parameters from SPD EEPROM and fix memory tab layout
- [ ] **Phase 5: History & Database Integrity** - Fix history counters, stale session cleanup, and database access patterns
- [ ] **Phase 6: UI Data Consistency** - Verify all displayed sensor values update accurately during test runs

## Phase Details

### Phase 1: Core Engine Fixes
**Goal**: Per-core stress testing works correctly -- benchmarks rotate through all cores with accurate stall detection and stable test completion
**Depends on**: Nothing (first phase)
**Requirements**: ENG-01, ENG-02, ENG-03, SIG-01
**Success Criteria** (what must be TRUE):
  1. Starting a 16-core test run cycles the stress benchmark through all 16 cores in sequence (not stuck on first core)
  2. Benchmark startup on each core completes without triggering false stall detection alarms
  3. Child threads/processes of each stress backend (mprime, stress-ng, y-cruncher) are verified running on the target core via /proc affinity checks
  4. Test completion signal arrives at the GUI without cross-thread crash (no Signal(dict) segfault)
**Plans**: 2 plans

Plans:
- [x] 01-01-PLAN.md -- Core cycling engine: stall detection grace period + periodic child TID affinity verification
- [x] 01-02-PLAN.md -- Signal(dict) crash fix: JSON string marshalling for cross-thread test completion signal

### Phase 2: Process & Thread Lifecycle
**Goal**: Stress processes and worker threads are fully managed -- no orphans survive application exit, no stale thread state accumulates
**Depends on**: Phase 1
**Requirements**: ENG-04, ENG-05, SIG-02
**Success Criteria** (what must be TRUE):
  1. Stopping a test or closing the application leaves zero orphaned stress processes (verified via ps/pgrep after exit)
  2. Killing the application mid-test (SIGTERM, SIGKILL) leaves zero orphaned stress processes (PR_SET_PDEATHSIG or equivalent)
  3. QThread workers reach a clean shutdown state on application exit (no stale thread references, no segfault on close)
  4. All cross-thread Qt object access uses signal/slot or explicit synchronization (no unprotected shared state)
**Plans**: 2 plans

Plans:
- [ ] 02-01-PLAN.md -- Process orphan prevention: PR_SET_PDEATHSIG on all subprocess launches, hardened cleanup handler
- [ ] 02-02-PLAN.md -- QThread lifecycle and cross-thread safety: tuner abort fix, core_status cache, audit tests

### Phase 3: PM Table Clocks & Voltages
**Goal**: Memory tab displays live clock frequencies and voltages sourced from the ryzen_smu PM table with version-aware parsing
**Depends on**: Phase 1 (stable engine needed for live testing)
**Requirements**: MEM-01, MEM-02, MEM-05, MEM-06
**Success Criteria** (what must be TRUE):
  1. Memory tab shows FCLK, UCLK, and MCLK frequencies with a clear 1:1 or 1:2 ratio indicator matching BIOS configuration
  2. Memory tab shows actual running VDD and VDDQ voltages (not the SPD default 1.10V)
  3. PM table parser checks pm_table_version before decoding and shows "uncalibrated" state for unknown versions (no silent garbage data)
  4. DIMM temperature readings update in real-time during stress tests without requiring manual refresh
**Plans**: 2 plans

Plans:
- [ ] 03-01-PLAN.md -- Version-aware PM table parsing: PMTableOffsets registry, version dispatch, memory clock/voltage fields
- [ ] 03-02-PLAN.md -- Memory Controller QGroupBox, unified 1s timer, graceful degradation for unknown versions and missing driver

### Phase 4: SPD Timings & Memory UI
**Goal**: Memory tab displays DDR5 timing parameters decoded from SPD EEPROM with a clean, untruncated layout
**Depends on**: Phase 3 (PM table infrastructure provides data display patterns)
**Requirements**: MEM-03, MEM-04, MEM-07
**Success Criteria** (what must be TRUE):
  1. Memory tab displays DDR5 primary timings (tCL, tRCD, tRP, tRAS, tRC) labeled as "SPD Rated" values
  2. Memory tab displays DDR5 secondary timings (tRFC1, tRFCsb, tWR, tRRDS, tRRDL, tFAW, tREFI) labeled as "SPD Rated" values
  3. All memory info columns are fully visible without truncation at the default window size
**Plans**: 2 plans

Plans:
- [ ] 04-01-PLAN.md -- SPD EEPROM timing decode: SPDTimingData dataclass, decode_spd_timings function, eeprom discovery in SPD5118Reader
- [ ] 04-02-PLAN.md -- SPD Timings QGroupBox display, primary/secondary timing labels, DIMM table column resize fix

### Phase 5: History & Database Integrity
**Goal**: History tab displays correct session data with proper status tracking and clean database access patterns
**Depends on**: Phase 1 (correct test completion flow needed for accurate status tracking)
**Requirements**: HIST-01, HIST-02, HIST-03, HIST-04
**Success Criteria** (what must be TRUE):
  1. History summary shows correct Completed/Crashed/Stopped counts matching actual session records
  2. On application startup, any sessions left in "Running" status from a previous crash are automatically marked "Crashed"
  3. All database queries go through public HistoryDB methods (no direct db._conn access in application code)
  4. Switching between Grouped and Tuner Sessions views shows consistent, matching data for the same sessions
**Plans**: 2 plans

Plans:
- [ ] 05-01-PLAN.md -- Database access migration: add public HistoryDB methods, migrate all _conn call sites, rename _conn to __conn
- [ ] 05-02-PLAN.md -- Summary counter fix (SQL aggregation), stale session recovery enhancement, view consistency, test fix

### Phase 6: UI Data Consistency
**Goal**: All displayed sensor values are accurate and update reliably during active test runs
**Depends on**: Phase 1, Phase 2, Phase 3 (all data sources must be correct before validating display)
**Requirements**: UI-01, UI-02
**Success Criteria** (what must be TRUE):
  1. During an active test run, all sensor values (frequency, temperature, voltage, power) update at the configured refresh interval without stale readings
  2. Monitor tab per-core view shows frequency and usage bars that accurately reflect the core currently under test (active core shows high usage, idle cores show low)
**Plans**: 2 plans

Plans:
- [ ] 06-01-PLAN.md -- CoreGridWidget telemetry pipeline fix: NameError elimination, signal-cached active core, cross-thread safety
- [ ] 06-02-PLAN.md -- MonitorTab reliability: poll_interval propagation, staleness indicator (grey text), narrowed exception handling

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4 -> 5 -> 6
Note: Phases 3-4 (memory) and Phase 5 (history) are independent tracks. Phase 5 can be interleaved with 3-4 if desired.

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Core Engine Fixes | 2/2 | Complete | 2026-03-19 |
| 2. Process & Thread Lifecycle | 0/2 | Not started | - |
| 3. PM Table Clocks & Voltages | 0/2 | Not started | - |
| 4. SPD Timings & Memory UI | 0/2 | Not started | - |
| 5. History & Database Integrity | 0/2 | Not started | - |
| 6. UI Data Consistency | 0/2 | Not started | - |
