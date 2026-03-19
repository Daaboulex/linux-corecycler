# Technology Stack

**Analysis Date:** 2026-03-19

## Languages

**Primary:**
- Python 3.12+ - Main application language for all logic, testing, and stress test automation
- Nix - Flake-based package configuration and NixOS module system
- C - Kernel module integration (ryzen_smu, zenpower5, it87)

**Secondary:**
- Shell - Build and deployment scripts in flake.nix
- JSON - Test profiles, settings, history logging, and configuration files

## Runtime

**Environment:**
- Linux x86-64 only (no Windows, macOS, or ARM support)
- Requires kernel 4.14+ for MSR/hwmon access
- Kernel modules must match host kernel version

**Package Manager:**
- pip (Python package distribution)
- Nix flakes (system package management)

**Lockfile:**
- `flake.lock` - Pins nixpkgs (nixos-unstable) and flake-utils versions
- No Python lockfile (pyproject.toml uses range specifications)

## Frameworks

**Core:**
- PySide6 6.7+ - Qt6 bindings for GUI (dark theme, SVG support, high DPI awareness)
- setuptools 75.0+ with setuptools-scm - Build system for Python application packaging

**Testing:**
- pytest 8.0+ - Test runner and assertion framework
- Configured in `pyproject.toml` with PytestCollectionWarning suppression for production classes named `Test*`

**Build/Dev:**
- ruff 0.8+ - Python linter and formatter (target Python 3.12, line length 100)
- CachyOS kernel support - Detection for LLVM=1 compilation with clang (zenpower, ryzen-smu, it87 modules)
- Nix derivation builders - custom mkDerivation for kernel module compilation

## Key Dependencies

**Critical:**
- PySide6 6.7+ - Only explicit runtime dependency beyond stdlib
  - Runtime requirement: Qt6 libraries (qt6.qtbase)
  - Plugin path: `QT_QPA_PLATFORM_PLUGIN_PATH` environment variable required

**Infrastructure:**
- qt6.qtbase - Qt6 runtime (included in build environment via nativeBuildInputs)
- qt6.wrapQtAppsHook - Automatic Qt6 wrapper for proper plugin and library loading
- setuptools-scm - Git-based version derivation from flake.nix version field

**Stress Test Backends (optional at runtime):**
- stress-ng - Default FOSS stress test tool (always included)
- stressapptest - Google's memory stress testing tool (always included)
- mprime - Prime95 CLI binary (unfree, requires `packages.full` or `unfreeBackends = true`)
- y-cruncher - Pi computation stress test (supported but optional)

**System Tools (wrapped in PATH at runtime):**
- util-linux - taskset command for CPU affinity
- dmidecode - DIMM information reading (Memory tab)

**Development Dependencies:**
- pytest 8.0+ - Test framework (in devShells)
- ruff 0.8+ - Linting and formatting (in devShells)

## Configuration

**Environment:**
- `QT_QPA_PLATFORM_PLUGIN_PATH` - Required at runtime for PySide6 Qt platform plugins
- `QT_LOGGING_RULES` - Qt warning suppression (set in `src/main.py` by default)
- All other config is file-based (no required env vars for core functionality)

**Build:**
- `pyproject.toml` - Project metadata, dependencies, build backend (setuptools), pytest config, ruff config
- `.flake8` equivalent - Not used; ruff replaces flake8
- No `.eslintrc`, `.prettierrc`, or other linters beyond ruff

**File-based Configuration:**
- `~/.config/corecyclerlx/default.json` - Test profiles, app settings, theme, window size
- Application state stored in `~/.config/corecyclerlx/` (CONFIG_DIR in `src/config/settings.py`)
- History database: `~/.config/corecyclerlx/history.db` (SQLite WAL mode)

## Platform Requirements

**Development:**
- NixOS (flake.nix configured for Linux only)
- Kernel 4.14+ with MSR module (in-tree)
- Python 3.12+ interpreter
- Qt6 development files (qt6.qtbase, qt6.wrapQtAppsHook)
- Build tools: make, gcc or clang (kernel modules require matching kernel compiler)

**Production:**
- Linux kernel 4.14+ with:
  - MSR module loaded (always required)
  - ryzen_smu kernel module (optional, for Curve Optimizer SMU access via amkillam fork)
  - zenpower5 kernel module (optional, for AMD CPU temperature monitoring, Zen 1-5)
  - nct6775 in-tree module (optional, Nuvoton Super I/O on ASUS/MSI/ASRock boards)
  - it87 kernel module (optional, ITE Super I/O on Gigabyte boards, frankcrawford fork)
  - coretemp in-tree module (optional, Intel CPU temperature monitoring)
  - cpuid in-tree module (optional, CPU feature detection)
  - spd5118 + i2c_dev in-tree modules (optional, DDR5 DIMM temperature monitoring)
- NixOS or flake-compatible system with inputs.nixpkgs (can point to other nixpkgs branches)
- User must have read/write access to `/sys/kernel/ryzen_smu_drv/` sysfs files (via udev group) or run as root
- User must have read access to `/dev/msr*` and `/sys/class/hwmon/` (via group or root)

**Deployment:**
- NixOS module: `services.corecyclerlx` with options for kernel modules, device access, unfree backends
- Flake output: `packages.default` (FOSS backends) or `packages.full` (includes mprime)
- Desktop application shipped with icon (`assets/icon.svg`), .desktop file, and asset SVGs

## Build Output

**Nix Flake Outputs:**
- `nixosModules.default` - NixOS module for CoreCyclerLx (kernel modules, device access, package installation)
- `packages.default` (per-system) - Python application + stress-ng + stressapptest
- `packages.full` (per-system) - Python application + mprime + stress-ng + stressapptest
- `devShells.default` (per-system) - Development environment with Python 3.12, pytest, ruff, Qt6, all stress tools

**Python Package:**
- pname: `corecyclerlx`
- entry point: `corecyclerlx` -> `src/main.py:main()`
- installed to: `$out/bin/corecyclerlx`
- wrapped with:
  - Qt6 app hooks (automatic plugin path setup)
  - PATH includes stress tools, taskset, dmidecode
  - DPI scaling enabled for high-res displays

---

*Stack analysis: 2026-03-19*
