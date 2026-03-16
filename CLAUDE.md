# CLAUDE.md — Project Context for AI Assistants

## Project

**CoreCyclerLx** — Per-core CPU stability tester and AMD PBO Curve Optimizer tuner for Linux.
Version: 0.0.1-dev (pre-release). Python 3.12 + PySide6/Qt6. NixOS flake with module.

## Architecture

```
src/
  main.py                    # Entry point, dark theme, signal handlers, atexit cleanup
  config/settings.py         # TestProfile dataclass, app settings persistence (JSON)
  engine/
    scheduler.py             # Per-core stress test orchestrator (taskset pinning, SMT siblings)
    topology.py              # CPU topology detection (CCDs, V-Cache, SMT via /proc/cpuinfo + sysfs)
    detector.py              # MCE error detection (sysfs + dmesg)
    backends/
      base.py                # StressBackend ABC, StressConfig, StressResult, StressMode, FFTPreset
      mprime.py              # Prime95 CLI (NumCPUs=1, CoresPerTest=1, respects taskset)
      stress_ng.py           # stress-ng (--cpu N, --cpu-method, --verify)
      ycruncher.py           # y-cruncher component stress
      stressapptest.py       # Google's memory stress (-W, 86400s timeout, scheduler kills)
  gui/
    main_window.py           # MainWindow with 7 tabs, toolbar, core grid, closeEvent cleanup
    config_tab.py            # Test configuration (5 presets, backends, FFT, timing, safety)
    results_tab.py           # Per-core test results table
    monitor_tab.py           # Live charts (temp, voltage, freq, power) + per-core bars
    smu_tab.py               # Curve Optimizer read/write, backup/restore, dry-run
    tuner_tab.py             # Auto-tuner UI (config panel, core table, test log, clear button)
    history_tab.py           # History (grouped/all/tuner views, context deletion, comparison)
    memory_tab.py            # DIMM info, DDR5 temps, memory stress (configurable duration/tool)
    widgets/
      core_grid.py           # CCD-aware vertical core status display
      charts.py              # LiveChart widget
  monitor/
    hwmon.py                 # CPU temp/voltage via k10temp/zenpower/coretemp + Super I/O fallback
    power.py                 # Package power (RAPL sysfs or hwmon)
    msr.py                   # APERF/MPERF clock stretch, per-core RAPL power
    frequency.py             # Per-core frequency from sysfs
    cpu_usage.py             # Per-core CPU usage from /proc/stat
    memory.py                # DIMMInfo (dmidecode), SPD5118Reader (DDR5 temps)
  smu/
    driver.py                # RyzenSMU: CO read/write, PBO limits, boost, backup/restore
    commands.py              # SMU command encoding per generation (Zen 1-5)
    pmtable.py               # PM table parsing
  tuner/
    engine.py                # TunerEngine: state machine, 5 test orders, crash-safe SQLite
    config.py                # TunerConfig: 16 fields with validate()
    state.py                 # CoreState, TunerSession dataclasses
    persistence.py           # SQLite persistence for tuner state
  history/
    db.py                    # HistoryDB: SQLite WAL, schema v5, auto-migration
    context.py               # Tuning context detection, BIOS change detection
    logger.py                # TestRunLogger
    export.py                # JSON/CSV export
nix/
  module.nix                 # NixOS module (9 options: ryzenSmu, zenpower, it87, spd5118, etc.)
  ryzen-smu.nix              # Out-of-tree kernel module (amkillam fork, Zen 1-5)
  zenpower.nix               # Out-of-tree zenpower5 (mattkeenan fork)
  it87.nix                   # Out-of-tree ITE Super I/O (frankcrawford fork)
tests/                       # 21 test modules, 818+ tests, pytest
```

## Key Design Decisions

- **CO values are VOLATILE** — SMU SRAM only, reset on reboot. BIOS never modified.
- **Crash-safe tuner** — SQLite WAL mode, autocommit, every state transition persisted before acting.
- **Process cleanup** — `os.setsid()` + `os.killpg()` for stress processes. `atexit` + `SIGTERM`/`SIGINT` handlers.
- **Pick functions are pure selectors** — no state mutation in `_pick_*()`. State advancement happens in `_run_next()`.
- **SMT siblings pinned together** — `taskset -c 0,16` for physical core 0. Both threads stressed.
- **Signal(str) for dicts** — `session_completed` uses JSON serialization to avoid Qt `_pythonToCppCopy` crash.

## Tuner Test Orders

| Order | Behavior |
|-------|----------|
| `sequential` | Finish each core completely before next (two-pass: active, then settled) |
| `round_robin` | Rotate through cores, one test each (tracks `_last_tested_core`) |
| `weakest_first` | Prioritize cores nearest to settling (fine_search > confirming > coarse) |
| `ccd_alternating` | Alternate CCDs, pick least-confirmed CCD |
| `ccd_round_robin` | Alternate CCDs + rotate within each CCD (tracks `_ccd_last_tested`) |

## Tuner State Machine

```
not_started → coarse_search → (pass: deeper, fail: fine_search or settled)
fine_search → (pass: deeper, fail: settled)
settled → confirming → (pass: confirmed, fail×N: failed_confirm → back off)
```

## Running

```bash
nix develop --command pytest tests/ -v    # run tests
nix develop --command python src/main.py  # run app (dev mode)
sudo nix run --refresh "github:Daaboulex/linux-corecycler#full"  # run from GitHub
```

## Conventions

- Python 3.12+, ruff for linting, line length 100
- PySide6 signals/slots with `@Slot()` decorators
- Dataclasses with `slots=True` for state objects
- `from __future__ import annotations` in all files
- Tests use fixtures from `tests/conftest.py` (mock topologies, backends, SMU sysfs)
- Commits: conventional commits (`feat:`, `fix:`, `chore:`, `docs:`)
- Pre-commit: treefmt + update-options-docs hooks
