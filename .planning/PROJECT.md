# CoreCyclerLx — Linux Core Stability Tester

## What This Is

A Linux-native per-core CPU stability testing tool with automated Curve Optimizer tuning. Built with Python/PySide6 (Qt6), it cycles stress test benchmarks across individual CPU cores to identify per-core instability from overclocking or undervolting. Targets AMD Ryzen processors (Zen 3/4/5) with deep ryzen_smu integration for reading/writing CO offsets, PM table decoding, and real-time monitoring.

## Core Value

Every per-core stress test result must be accurate and trustworthy — if the tool says a core passed or failed, it must have actually tested that core with the correct workload for the configured duration.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

- ✓ Qt6 dark-theme GUI with 7 tabs (Configuration, Results, Monitor, Curve Optimizer, Auto-Tuner, History, Memory) — existing
- ✓ Per-CCD core grouping with V-Cache labeling (C0V-C7V / C8-C15) — existing
- ✓ Multiple stress backends: mprime, stress-ng, y-cruncher, stressapptest — existing
- ✓ Configurable test presets (Standard, Quick, Extended) with per-core timing — existing
- ✓ Real-time CPU monitoring: frequency, temperature (Tctl, per-CCD), Vcore, package power graphs — existing
- ✓ Per-core monitoring view with frequency bars, usage %, power, temperature per core — existing
- ✓ Curve Optimizer tab: read/write CO offsets via ryzen_smu, backup/restore, bulk operations — existing
- ✓ Auto-Tuner with coarse/fine search algorithm, recoverable sessions, configurable parameters — existing
- ✓ History database (SQLite WAL) with BIOS version tracking, grouped/session views — existing
- ✓ Profile save/load system for test configurations — existing
- ✓ CPU topology detection (CCD count, V-Cache, SMT, core count) — existing
- ✓ Safety: max temperature cutoff, stall detection — existing
- ✓ Memory tab: basic DIMM info (slot, size, type, SPD speed, running speed, manufacturer, part number, rank) via dmidecode — existing
- ✓ Memory tab: DIMM temperatures from SPD5118 hwmon sensors — existing
- ✓ Memory tab: built-in memory stress test (stressapptest/stress-ng) — existing
- ✓ Crash-resilient log database — sessions persist through crashes — existing

### Active

<!-- Current scope. Building toward these. -->

- [ ] Fix core cycling bug: benchmark stays on first core instead of rotating through all cores during testing
- [ ] Fix stall detection false positives: detector fires during process startup before stress reaches 100% load
- [ ] Fix PySide6 dict Signal marshalling crash: `Signal(dict)` in main_window.py causes cross-thread crash
- [ ] Fix History counter logic: summary shows "Completed: 0" even when completed sessions exist
- [ ] Fix History stale session status: "Running" status persists after crash/exit without cleanup
- [ ] Memory tab: display actual running voltages (VDD/VDDQ from ryzen_smu PM table, not SPD 1.10V default)
- [ ] Memory tab: display FCLK/UCLK/MCLK from ryzen_smu PM table with 1:1 vs 1:2 ratio indicator
- [ ] Memory tab: display DDR5 timing parameters (tCL, tRCD, tRP, tRAS, tRC, tRFC1, tRFCsb, tWR, etc.)
- [ ] Audit subprocess lifecycle: ensure stress process groups are fully killed on timeout/exit (no orphans)
- [ ] Audit QThread worker cleanup: proper shutdown on abnormal exit
- [ ] Audit database access patterns: migrate private `db._conn` access to public HistoryDB methods
- [ ] UI data consistency: ensure all displayed values are accurate and update reliably during test runs

### Out of Scope

- Windows/macOS support — Linux-only tool by design
- Intel CPU support — AMD Ryzen specific (ryzen_smu dependency)
- Voltage suggestion engine — CO offsets are the tuning mechanism, not direct voltage control
- ARM/non-x86 architectures — desktop Ryzen only

## Context

- Built as a Linux port of the Windows CoreCycler concept, but has grown beyond feature parity with SMU integration, auto-tuning, and hardware monitoring
- The ryzen_smu kernel module provides direct access to AMD SMU for CO read/write, PM table (containing FCLK/UCLK/MCLK, voltages, power), and PBO configuration
- zenpower5 kernel module provides CPU sensor data; it87 provides Super I/O chip monitoring
- SPD5118 hwmon driver (mainline kernel) provides DDR5 DIMM temperatures
- The PM table binary blob at `/sys/kernel/ryzen_smu_drv/pm_table` contains all clocks and voltages — offsets vary by PM table version (need version-aware parsing)
- DDR5 timing data can potentially be read from MSR/SMN registers via ryzen_smu SMN interface, or parsed from dmidecode SPD data
- The tool already has a `src/smu/pmtable.py` module for PM table decoding — extend this for memory data
- User (Kiro) actively uses this tool for Curve Optimizer tuning on a Ryzen 9 9950X3D with 4x16GB DDR5-6000

## Constraints

- **Runtime**: Requires root/sudo for MSR access, ryzen_smu sysfs, and CPU affinity operations
- **Kernel modules**: ryzen_smu must be loaded for CO/PM table features; zenpower5 for CPU sensors
- **Packaging**: Distributed as Nix flake with kernel module compilation; also supports pip install
- **GUI framework**: PySide6 (Qt6) — cannot switch without full rewrite
- **Database**: SQLite with WAL mode for crash safety — must maintain backward compatibility with existing session data

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| PySide6 over PyQt6 | LGPL license, official Qt bindings | ✓ Good |
| SQLite WAL for history | Crash-safe writes, no external DB dependency | ✓ Good |
| ryzen_smu sysfs interface | Direct SMU access without custom kernel patches | ✓ Good |
| Multiple stress backends | Different backends stress different CPU subsystems | ✓ Good |
| Per-core sequential testing | Isolates instability to specific cores | ✓ Good |
| PM table for real voltages | SPD only shows default 1.10V, PM table has actual running values | — Pending |

---
*Last updated: 2026-03-19 after initialization*
