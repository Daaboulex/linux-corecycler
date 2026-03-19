# Requirements: CoreCyclerLx

**Defined:** 2026-03-19
**Core Value:** Every per-core stress test result must be accurate and trustworthy

## v1 Requirements

Requirements for this milestone. Each maps to roadmap phases.

### Engine Reliability

- [x] **ENG-01**: Stress benchmark actually runs on the target core for the configured duration (core cycling works across all 16 cores)
- [x] **ENG-02**: Stall detection does not fire false positives during benchmark startup (grace period or readiness detection)
- [x] **ENG-03**: Child threads/processes of stress backends inherit and maintain CPU affinity (verified for mprime, stress-ng, y-cruncher)
- [x] **ENG-04**: All stress processes are fully terminated on test stop/crash/exit (no orphaned process groups)
- [x] **ENG-05**: QThread workers are properly cleaned up on abnormal exit (no stale thread state)

### Signal/Threading Safety

- [x] **SIG-01**: PySide6 Signal(dict) crash fixed — all cross-thread signals use JSON string marshalling
- [x] **SIG-02**: No cross-thread Qt object access without signal/slot or mutex protection

### History & Database

- [ ] **HIST-01**: History summary counters (Completed/Crashed/Stopped) correctly aggregate session statuses
- [ ] **HIST-02**: Stale "Running" sessions are detected and marked as "Crashed" on application startup
- [ ] **HIST-03**: Database access uses public HistoryDB methods, not private `db._conn`
- [ ] **HIST-04**: History tab data is consistent between Grouped and Tuner Sessions views

### Memory Diagnostics

- [x] **MEM-01**: Memory tab displays FCLK, UCLK, MCLK from ryzen_smu PM table with 1:1 vs 1:2 ratio indicator
- [x] **MEM-02**: Memory tab displays actual running VDD/VDDQ voltage (from PM table, not SPD default 1.10V)
- [ ] **MEM-03**: Memory tab displays DDR5 primary timings (tCL, tRCD, tRP, tRAS, tRC) from SPD EEPROM
- [ ] **MEM-04**: Memory tab displays DDR5 secondary timings (tRFC1, tRFCsb, tWR, tRRDS, tRRDL, tFAW, tREFI) from SPD EEPROM
- [x] **MEM-05**: PM table parsing is version-aware (dispatches to correct offset map based on pm_table_version)
- [x] **MEM-06**: DIMM temperature display updates in real-time during stress tests (not just on manual refresh)
- [ ] **MEM-07**: Memory info layout fits properly without column truncation (visible in current UI)

### UI Robustness

- [ ] **UI-01**: All displayed sensor values update reliably during test runs (no stale data)
- [ ] **UI-02**: Monitor tab per-core view shows accurate per-core frequency/usage during active testing

## v2 Requirements

Deferred to future milestone. Tracked but not in current roadmap.

### Memory Diagnostics (Advanced)

- **MEM-A01**: Display actual running DDR5 timings from UMC registers via SMN interface (tCL, tRCD, tRP as the memory controller sees them vs SPD rated values)
- **MEM-A02**: Side-by-side comparison of SPD rated vs actual running timings
- **MEM-A03**: Memory bandwidth estimation from FCLK/channel configuration

### Auto-Tuner Enhancements

- **TUNE-A01**: Auto-tuner session comparison across BIOS versions with statistical significance
- **TUNE-A02**: Export tuner results as PBO2 BIOS settings guide

## Out of Scope

| Feature | Reason |
|---------|--------|
| GPU stress testing | OCCT/FurMark own this space; dilutes CPU focus |
| Voltage suggestion engine | CO offsets are the correct abstraction; suggesting voltages is dangerous |
| Windows/macOS/Intel support | Linux AMD Ryzen only by design |
| Benchmark scoring/leaderboards | Stability testing != benchmarking |
| Auto-BIOS flashing | Extremely dangerous; user responsibility |
| Web UI / remote access | SSH + X11 forwarding exists; doubles frontend maintenance |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| ENG-01 | Phase 1 | Complete |
| ENG-02 | Phase 1 | Complete |
| ENG-03 | Phase 1 | Complete |
| ENG-04 | Phase 2 | Complete |
| ENG-05 | Phase 2 | Complete |
| SIG-01 | Phase 1 | Complete |
| SIG-02 | Phase 2 | Complete |
| HIST-01 | Phase 5 | Pending |
| HIST-02 | Phase 5 | Pending |
| HIST-03 | Phase 5 | Pending |
| HIST-04 | Phase 5 | Pending |
| MEM-01 | Phase 3 | Complete |
| MEM-02 | Phase 3 | Complete |
| MEM-03 | Phase 4 | Pending |
| MEM-04 | Phase 4 | Pending |
| MEM-05 | Phase 3 | Complete |
| MEM-06 | Phase 3 | Complete |
| MEM-07 | Phase 4 | Pending |
| UI-01 | Phase 6 | Pending |
| UI-02 | Phase 6 | Pending |

**Coverage:**
- v1 requirements: 20 total
- Mapped to phases: 20
- Unmapped: 0

---
*Requirements defined: 2026-03-19*
*Last updated: 2026-03-19 after roadmap creation*
