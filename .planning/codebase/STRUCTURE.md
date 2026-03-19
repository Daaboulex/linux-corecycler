# Codebase Structure

**Analysis Date:** 2026-03-19

## Directory Layout

```
linux-corecycler/
├── src/                    # Python application source
│   ├── main.py            # Qt6 application entry point
│   ├── config/            # Settings and test profile management
│   ├── engine/            # Stress testing scheduler and backends
│   ├── gui/               # Qt6 user interface (tabs, windows, widgets)
│   ├── history/           # Test run logging and persistence (SQLite)
│   ├── monitor/           # Hardware monitoring (hwmon, MSR, sysfs)
│   ├── smu/               # AMD SMU driver and Curve Optimizer commands
│   └── tuner/             # Automated PBO Curve Optimizer tuner
├── tests/                 # Pytest test suite
├── nix/                   # NixOS module definitions and kernel module builders
├── assets/                # Application icon, SVG controls, desktop file
├── docs/                  # Documentation
├── flake.nix              # Nix flake entry point
├── pyproject.toml         # Python project metadata and build config
└── README.md              # Feature list and hardware support table
```

## Directory Purposes

**`src/`:**
- Purpose: Complete Python application source code, runnable as package or standalone
- Contains: Modules for configuration, engine, GUI, history, monitoring, SMU, and tuning
- Key files: `main.py` (entry point), `__init__.py` (package marker)

**`src/config/`:**
- Purpose: Settings persistence and test profile management
- Contains: AppSettings (window geometry, theme, poll interval), TestProfile (backend, FFT, duration, safety limits)
- Key files: `settings.py` (load_settings, save_settings, dataclasses)

**`src/engine/`:**
- Purpose: Core stress testing orchestration and backend abstraction
- Contains: CoreScheduler (per-core cycling with error detection), StressBackend implementations (mprime, stress-ng, y-cruncher, stressapptest), ErrorDetector (MCE monitoring), CPUTopology (system topology detection from cpuinfo/sysfs)
- Key files: `scheduler.py` (CoreScheduler, TestMode, CoreTestStatus), `detector.py` (ErrorDetector), `topology.py` (CPUTopology, detect_topology)
- Subdirectory: `backends/` (backend.py base class, mprime.py, stress_ng.py, ycruncher.py, stressapptest.py)

**`src/gui/`:**
- Purpose: Qt6 user interface (QMainWindow, tabs, real-time monitoring, results display)
- Contains: MainWindow orchestrator, tab implementations (ConfigTab, MonitorTab, TunerTab, HistoryTab, SMUTab, ResultsTab, MemoryTab), custom widgets
- Key files: `main_window.py` (MainWindow, TestWorker), `config_tab.py` (profile editor), `monitor_tab.py` (real-time charts), `tuner_tab.py` (auto-tuner UI), `history_tab.py` (run history view)
- Subdirectory: `widgets/` (CoreGridWidget for per-core status, charts.py for matplotlib integration)

**`src/history/`:**
- Purpose: Crash-safe test run logging and tuner session persistence
- Contains: HistoryDB (SQLite WAL database), TestRunLogger (per-core result recording), BIOS context detection, CSV/JSON export
- Key files: `db.py` (HistoryDB class, schema, RunRecord/CoreResultRecord dataclasses), `logger.py` (TestRunLogger), `context.py` (detect_bios_change), `export.py` (CSV/JSON export)

**`src/monitor/`:**
- Purpose: Real-time hardware monitoring via hwmon, MSR, and sysfs
- Contains: Temperature/voltage readers with fallback chains, MSR-based clock stretch (APERF/MPERF), frequency scaling, power (RAPL), memory info
- Key files: `hwmon.py` (HWMonReader with Super I/O fallback), `msr.py` (MSRReader for APERF/MPERF/RAPL), `frequency.py` (read_core_frequencies), `cpu_usage.py`, `power.py` (package power), `memory.py` (DIMM info via dmidecode)

**`src/smu/`:**
- Purpose: AMD System Management Unit (Curve Optimizer, PBO limits, scalar) communication
- Contains: RyzenSMU driver (sysfs interface), per-generation CO command builders, PM table decoding
- Key files: `driver.py` (RyzenSMU class, generation detection), `commands.py` (Zen3Commands, Zen4Commands, Zen5Commands), `pmtable.py` (PM table parsing for boost limits)

**`src/tuner/`:**
- Purpose: Automated Curve Optimizer search algorithm with crash-safe state machine
- Contains: Coarse-to-fine search per core, phase transitions, APERF/MPERF clock stretch detection, per-generation CO search ranges, state persistence
- Key files: `engine.py` (TunerEngine state machine, TunerWorker QThread), `state.py` (CoreState phases), `config.py` (TunerConfig, per-generation ranges), `persistence.py` (tuner_progress SQLite table)

**`tests/`:**
- Purpose: Pytest test suite for all modules
- Contains: Unit and integration tests for scheduler, backends, SMU, monitoring, history, tuner
- Key files: `conftest.py` (pytest fixtures: mock CPU topology, mock backends, temp SQLite DB), `test_scheduler.py`, `test_backends.py`, `test_smu_driver.py`, `test_topology.py`, `test_history_db.py`, `test_tuner_engine.py`
- Pattern: One test file per source module (e.g., `test_scheduler.py` for `src/engine/scheduler.py`)

**`nix/`:**
- Purpose: NixOS module definitions and out-of-tree kernel module builders
- Contains: NixOS module for kernel modules (ryzen_smu, zenpower, it87, nct6775), device access (udev rules, tmpfiles), package selection
- Key files: `module.nix` (main NixOS module, mkOption schema), `ryzen-smu.nix` (out-of-tree kernel module builder), `zenpower.nix`, `it87.nix`

**`assets/`:**
- Purpose: Application assets (icon, SVG controls, desktop file)
- Contains: Scalable Vector Graphics for UI controls (arrow-up.svg, arrow-down.svg, etc.), app icon (icon.svg), desktop entry
- Key files: `icon.svg` (taskbar/launcher icon), `corecyclerlx.desktop` (GNOME/KDE desktop entry)

## Key File Locations

**Entry Points:**
- `src/main.py`: Application entry point. Detects assets (dev vs. Nix-installed), sets up Qt stylesheet, creates QApplication, shows MainWindow. Handles SIGTERM/SIGINT gracefully.

**Configuration:**
- `src/config/settings.py`: AppSettings and TestProfile dataclasses, JSON serialization, `~/.config/corecyclerlx/default.json` path

**Core Logic:**
- `src/engine/scheduler.py`: CoreScheduler class (320+ lines), per-core cycling state machine, error detection callbacks
- `src/engine/topology.py`: CPUTopology detection from /proc/cpuinfo and /sys/devices/system/cpu/, CCD/CCX/X3D identification
- `src/monitor/hwmon.py`: HWMonReader with automatic device selection and Super I/O fallback for Vcore
- `src/smu/driver.py`: RyzenSMU class, generation-aware CO command dispatch, PMU table reading

**Testing:**
- `tests/conftest.py`: Pytest fixtures (mock_topology, mock_backend, mock_history_db)
- `tests/test_scheduler.py`: CoreScheduler unit tests
- `tests/test_smu_driver.py`: RyzenSMU generation-specific command tests

## Naming Conventions

**Files:**
- `[module_name].py`: Standard Python module files (e.g., `scheduler.py`, `hwmon.py`)
- `test_[module_name].py`: Pytest test file for corresponding module (e.g., `test_scheduler.py`)
- `[feature]_tab.py`: Qt6 QWidget subclass for a tab (e.g., `config_tab.py`, `tuner_tab.py`)
- `__init__.py`: Package marker (empty or re-exports key classes)

**Directories:**
- Lowercase, underscored: `src/monitor/`, `src/smu/`, `src/tuner/`
- Grouped by concern: `engine/` (testing), `gui/` (interface), `history/` (persistence), `monitor/` (telemetry)

**Classes:**
- PascalCase: `CoreScheduler`, `TestWorker`, `HWMonReader`, `RyzenSMU`, `HistoryDB`, `TunerEngine`
- Dataclasses: `TestProfile`, `CoreTestStatus`, `RunRecord`, `CoreState`
- Enums: `TestState`, `TestMode`, `StressMode`, `FFTPreset` (all UPPERCASE for enum members)

**Functions:**
- snake_case: `detect_topology()`, `load_settings()`, `read_core_frequencies()`
- Callbacks: `on_core_start()`, `on_core_finish()`, `on_status_update()`

**Variables:**
- Private (class-internal): `_worker`, `_settings`, `_hwmon` (leading underscore)
- Configuration: UPPERCASE constants like `HWMON_BASE`, `CONFIG_DIR`, `SCHEMA_VERSION`

## Where to Add New Code

**New Stress Backend:**
- Primary code: `src/engine/backends/[tool_name].py` (inherit from `StressBackend`, implement start/poll/stop/results)
- Registration: Add to `backend` choice in `src/gui/config_tab.py` combo box
- Tests: `tests/test_backends.py` (add new test class)

**New Hardware Monitoring Source:**
- Primary code: `src/monitor/[hwmon_type].py` (e.g., `src/monitor/acpi_power.py`)
- Integration: Add detection logic to `src/gui/main_window.py` MainWindow.__init__() telemetry initialization
- Tests: `tests/test_monitor.py` (add mock device and parsing tests)

**New GUI Tab:**
- Implementation: `src/gui/[feature]_tab.py` (inherit from QWidget)
- Registration: Import and add tab to `MainWindow._create_tabs()` in `src/gui/main_window.py`
- Tests: Optional, but follow pattern in `tests/test_tuner_tab.py` if testing UI state

**New SMU Command (Per-Generation):**
- Add to `src/smu/commands.py`: New generation class (e.g., `Zen5CommandSet`) with co_get/co_set methods
- Update dispatch in `src/smu/driver.py` (check family/model, return appropriate command set)
- Tests: `tests/test_smu_commands.py` (add generation-specific test cases)

**Utilities/Helpers:**
- Shared helpers: `src/[module]/[utility_name].py` (e.g., `src/monitor/utils.py` for parsing helpers)
- DO NOT create a catch-all `utils.py` in `src/` — place in the module that uses them

**Configuration/Constants:**
- Add to appropriate dataclass in `src/config/settings.py` (TestProfile for test knobs, AppSettings for app-level)
- DO NOT create config files — use JSON persistence via load_settings/save_settings

## Special Directories

**`.planning/`:**
- Purpose: GSD planning documents and phase tracking
- Generated: No (hand-maintained)
- Committed: Yes (tracks project planning history)
- Subdirs: `.planning/codebase/` (this analysis), `.planning/phases/` (phase plans and summaries)

**`.git/`:**
- Purpose: Git repository metadata
- Generated: Yes (by git init)
- Committed: No (git metadata, not source code)

**`__pycache__/` and `.pytest_cache/`:**
- Purpose: Python bytecode cache and pytest cache
- Generated: Yes (by Python and pytest)
- Committed: No (excluded via .gitignore)

**`.ruff_cache/`:**
- Purpose: Ruff linter cache
- Generated: Yes (by ruff lint/format)
- Committed: No (excluded via .gitignore)

## Entry Point Flow

1. **User runs:** `corecyclerlx` (installed via Nix) or `python src/main.py` (dev mode)
2. **`src/main.py:main()`** executed:
   - Sets Qt environment variables (DPI scaling, logging rules for sudo)
   - Detects assets directory (dev vs. Nix-installed path)
   - Creates Qt stylesheet from `_dark_stylesheet()`
   - Instantiates `MainWindow()`
3. **`MainWindow.__init__()`:**
   - Loads AppSettings from JSON
   - Initializes HistoryDB
   - Probes hardware (CPUTopology, HWMonReader, MSRReader)
   - Creates tabs (ConfigTab, MonitorTab, TunerTab, HistoryTab, SMUTab, ResultsTab, MemoryTab)
   - Sets up QTimer for telemetry updates (default 1s)
4. **GUI shown:** `MainWindow.show()`
5. **User interactions:** Each tab handles user input (ConfigTab → test settings, TunerTab → auto-tuner, etc.)
6. **Test execution:** User clicks "Start Test" → `MainWindow.on_start_test()` → CPUTopology detected → CoreScheduler + TestWorker created
7. **Graceful shutdown:** On SIGTERM/SIGINT or window close, `_cleanup_on_exit()` kills stress processes and saves settings

---

*Structure analysis: 2026-03-19*
