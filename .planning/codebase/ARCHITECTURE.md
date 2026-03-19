# Architecture

**Analysis Date:** 2026-03-19

## Pattern Overview

**Overall:** Layered architecture with a Qt6 GUI orchestrating multiple specialized subsystems.

**Key Characteristics:**
- Separation between stress testing engine, system monitoring, SMU control, and UI presentation
- Thread-based worker patterns for non-blocking stress test execution
- Modular backend system supporting multiple stress test tools (mprime, stress-ng, y-cruncher, stressapptest)
- Per-core state machine (scheduler) for cycling through cores with error detection
- Persistent storage (SQLite WAL) for crash-safe history and tuner session state
- Linux sysfs/MSR-based hardware monitoring with automatic fallback chains

## Layers

**Application (GUI) Layer:**
- Purpose: User-facing Qt6 interface for configuring and running tests, monitoring hardware, viewing history
- Location: `src/gui/` (main_window.py, tabs, widgets)
- Contains: QMainWindow, QTabWidget subclasses (ConfigTab, MonitorTab, TunerTab, HistoryTab, SMUTab, etc.)
- Depends on: config, engine, monitor, smu, tuner, history modules
- Used by: Application entry point (`src/main.py`)

**Engine Layer:**
- Purpose: Core testing orchestration and stress backend management
- Location: `src/engine/` (scheduler.py, backends/, detector.py, topology.py)
- Contains: CoreScheduler (per-core cycling), StressBackend implementations, ErrorDetector (MCE monitoring), CPUTopology (system topology detection)
- Depends on: monitor module for telemetry callbacks
- Used by: GUI layer (MainWindow, TunerTab), TestWorker threads

**Tuner Layer:**
- Purpose: Automated Curve Optimizer search algorithm with state machine
- Location: `src/tuner/` (engine.py, state.py, config.py, persistence.py)
- Contains: Coarse-to-fine search state machine, per-core tuning phases, persistence to SQLite
- Depends on: engine (scheduler), monitor (MSR for clock stretch), smu (CO read/write), history (HistoryDB)
- Used by: GUI (TunerTab)

**Monitor Layer:**
- Purpose: Real-time hardware monitoring via hwmon, MSR, and sysfs
- Location: `src/monitor/` (hwmon.py, msr.py, frequency.py, cpu_usage.py, power.py, memory.py)
- Contains: Temperature/voltage readers (HWMonReader with Super I/O fallback), MSR-based clock stretch (APERF/MPERF), frequency scaling, power (RAPL)
- Depends on: sysfs and MSR device files only
- Used by: MainWindow, monitoring tabs, scheduler callbacks

**SMU Layer:**
- Purpose: AMD System Management Unit (Curve Optimizer, PBO limits) communication
- Location: `src/smu/` (driver.py, commands.py, pmtable.py)
- Contains: RyzenSMU driver (sysfs interface), per-generation CO command builders (Zen 3, 4, 5 variants), PM table decoding
- Depends on: sysfs and ryzen_smu kernel module
- Used by: SMUTab, TunerTab, monitoring

**History Layer:**
- Purpose: Crash-safe test run logging and tuning session persistence
- Location: `src/history/` (db.py, logger.py, context.py, export.py)
- Contains: HistoryDB (SQLite WAL), TestRunLogger, BIOS context detection, CSV/JSON export
- Depends on: SQLite (via sqlite3 stdlib)
- Used by: MainWindow, HistoryTab, TunerTab

**Config Layer:**
- Purpose: Settings and test profile management
- Location: `src/config/` (settings.py)
- Contains: AppSettings (window size, theme, poll interval), TestProfile (backend, FFT, duration, safety limits)
- Depends on: json (stdlib)
- Used by: MainWindow, ConfigTab, backend selection

## Data Flow

**Stress Test Execution (Main Tab):**

1. User configures test in ConfigTab (backend, FFT, duration, safety limits)
2. MainWindow.on_start_test() triggered
3. CPUTopology detected from `/proc/cpuinfo` and `/sys/devices/system/cpu/`
4. CoreScheduler initialized with selected backend and topology
5. TestWorker (QThread) runs CoreScheduler.run()
6. For each core:
   - Backend.start() launches stress process with `taskset` to bind core
   - Scheduler polls backend for errors and stops on detected instability or timeout
   - Monitor callbacks (HWMonReader, MSRReader) feed telemetry to MainWindow
   - TestRunLogger records per-core result (pass/fail, max freq/temp/vcore)
7. HistoryDB stores completed run (crash-safe with SQLite WAL)
8. Results displayed in ResultsTab

**Curve Optimizer Tuning (Tuner Tab):**

1. User selects starting CO values (manual or "inherit current")
2. RyzenSMU reads current CO state from SMU sysfs
3. TunerEngine state machine begins coarse-to-fine search per core
4. For each phase (coarse_search, fine_search, confirming):
   - TunerWorker runs CoreScheduler test with configurable duration
   - If test fails or clock stretch detected → phase fails, step size halves, retry
   - If test passes → advance to next phase or mark as confirmed
5. TunerPersistence auto-commits state to SQLite after each phase
6. On reboot/crash, TunerEngine.resume() reads last persisted state and continues
7. On completion, tuner session saved to HistoryDB with all per-core states

**Hardware Monitoring (Monitor Tab):**

1. HWMonReader probes `/sys/class/hwmon/` for CPU temperature drivers (zenpower > k10temp > coretemp)
2. Falls back to Super I/O (nct6799, it8689, etc.) for Vcore on Zen 5
3. MSRReader reads APERF/MPERF from `/dev/msr/` for clock stretch (requires root)
4. MainWindow.update_telemetry() on QTimer (default 1s interval)
5. Real-time display updates per-core stats (freq, temp, usage, vcore, power)

**State Management:**

- **Test State:** TestState enum (IDLE, RUNNING, STOPPING, FINISHED) in scheduler
- **Core Status:** CoreTestStatus dataclass (core_id, state, errors, elapsed_seconds, etc.)
- **Tuner State:** Per-core phase (not_started → coarse_search → fine_search → settled → confirming → confirmed), persisted in tuner_progress table
- **Settings:** AppSettings JSON file at `~/.config/corecyclerlx/default.json`, loaded/saved by ConfigTab
- **History:** SQLite WAL database at `~/.local/share/corecyclerlx/history.db` with run_record, core_result, tuner_progress tables

## Key Abstractions

**CoreScheduler:**
- Purpose: Orchestrates per-core cycling with error detection and telemetry
- Examples: `src/engine/scheduler.py` (CoreScheduler class), used by MainWindow and TunerEngine
- Pattern: Callback-based (on_core_start, on_core_finish, on_status_update) for decoupled event signaling to GUI/tuner

**StressBackend (Abstract):**
- Purpose: Unified interface for different stress tools (mprime, stress-ng, y-cruncher, stressapptest)
- Examples: `src/engine/backends/mprime.py`, `stress_ng.py`, `ycruncher.py`, `stressapptest.py`
- Pattern: All inherit from `backends.base.StressBackend`, implement start(config), poll(), stop(), results()

**RyzenSMU:**
- Purpose: AMD SMU sysfs access for Curve Optimizer and PBO limits
- Examples: `src/smu/driver.py` (RyzenSMU class), reads/writes `/sys/kernel/ryzen_smu_drv/` entries
- Pattern: Generation-aware command builders (Zen3Commands, Zen4Commands, Zen5Commands) based on CPU family/model

**HWMonReader:**
- Purpose: Automatic detection and reading of hardware monitoring devices
- Examples: `src/monitor/hwmon.py` (HWMonReader class), probes `/sys/class/hwmon/` with fallback chain
- Pattern: Preferential device selection (zenpower > k10temp > coretemp) + Super I/O fallback for voltage

**HistoryDB:**
- Purpose: Crash-safe test run and tuner session persistence
- Examples: `src/history/db.py` (HistoryDB class), SQLite WAL mode with auto-commit transactions
- Pattern: DataClass records (RunRecord, CoreResultRecord, TunerProgressRecord) marshalled to/from SQL

## Entry Points

**Application Entry Point:**
- Location: `src/main.py`
- Triggers: User runs `corecyclerlx` or `python src/main.py` in dev mode
- Responsibilities: Asset detection (dev vs. Nix-installed), Qt stylesheet setup, QApplication creation, MainWindow display, graceful shutdown (cleanup_on_exit)

**Test Execution Entry Point:**
- Location: `src/gui/main_window.py` (MainWindow.on_start_test)
- Triggers: User clicks "Start Test" button in ConfigTab
- Responsibilities: Validate settings, detect CPU topology, create CoreScheduler + TestWorker, wire callbacks, display results

**Tuner Entry Point:**
- Location: `src/gui/tuner_tab.py` (TunerTab.on_start_tuning)
- Triggers: User clicks "Auto-Tune" in TunerTab with CO range and search parameters
- Responsibilities: Read/inherit current CO state, initialize TunerEngine state machine, resume if persisted, run tuner loop with phase transitions

## Error Handling

**Strategy:** Multi-layered error detection and graceful degradation.

**Patterns:**

- **Stress Test Errors:** ErrorDetector monitors stdout/stderr for backend-specific error patterns (mprime "error", stress-ng "stress-ng: error") and dmesg for Machine Check Exceptions (MCE); fails core on first match
- **SMU Access Errors:** RyzenSMU catches FileNotFoundError and IOError for missing sysfs entries, logs warnings, returns None for unsupported generations
- **Hardware Monitoring Fallback:** HWMonReader tries preferred drivers (zenpower) → falls back to generic (k10temp/coretemp) → falls back to Super I/O for voltage if primary missing
- **Tuner Persistence Errors:** TunerPersistence catches sqlite3.Error and logs, continues with in-memory state (no state recovery on crash, but less catastrophic than silent failure)
- **Config Load Errors:** load_settings() catches JSON decode errors, returns AppSettings defaults if corrupted
- **Process Cleanup:** TestWorker ensures stress processes terminated (kill, wait with timeout) even on exception; main.py registers atexit handler to force-stop on SIGTERM/SIGINT

## Cross-Cutting Concerns

**Logging:**
- Module-level `log = logging.getLogger(__name__)` in most modules
- Configured via Python stdlib logging
- Key messages: test started/finished, core pass/fail, MCE detected, SMU commands executed

**Validation:**
- SchedulerConfig dataclass enforces safe defaults (max_temperature=95.0, stop_on_error=False)
- ConfigTab validates FFT range (min ≤ max) and test duration (>0) before enabling Start button
- MainWindow.on_write_co() shows confirmation dialog before writing CO values to SMU
- TestProfile coerces stress_mode/fft_preset enums from JSON strings

**Authentication:**
- No built-in auth (standalone desktop app)
- SMU/MSR device access controlled by NixOS module via udev rules and group membership
- Dry-run mode (SMUTab) simulates CO writes without sysfs permission

**Resource Lifecycle:**
- QThread-based workers (TestWorker, TunerWorker) properly wait() and quit() on stop
- HWMonReader/MSRReader open sysfs files read-only, no persistent handles
- HistoryDB uses context managers (with statements) for SQLite connections, auto-commit on close
- Backend processes killed and waited with 5s timeout; systemd-killed if still running

---

*Architecture analysis: 2026-03-19*
