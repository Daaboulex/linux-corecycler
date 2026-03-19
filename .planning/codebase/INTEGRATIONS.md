# External Integrations

**Analysis Date:** 2026-03-19

## APIs & External Services

**Kernel/System Interfaces:**
- AMD SMU (System Management Unit) via `/sys/kernel/ryzen_smu_drv/` sysfs
  - Purpose: Read/write Curve Optimizer (CO) offsets per-core, PBO limits (PPT/TDC/EDC), boost override, PBO scalar
  - Implementation: `src/smu/driver.py` communicates with amkillam fork of ryzen_smu kernel module
  - Access: Requires sysfs group permissions or root (corecycler group via udev in NixOS module)
  - Volatile: All SMU writes reset on reboot/S3 sleep/driver reload — BIOS values never modified

**CPU Feature Detection:**
- CPUID via `/dev/cpu/*/cpuid` (optional cpuid kernel module)
  - Purpose: Detect CPU generation, core topology, feature flags
  - Implementation: Read CPUID leaves for generation detection in `src/engine/detector.py`
  - Fallback: /proc/cpuinfo parsing if cpuid device unavailable

**MSR (Model-Specific Register) Access:**
- MSR devices `/dev/msr0`, `/dev/msr1`, etc.
  - Purpose: Read APERF/MPERF for clock stretch detection, RAPL energy counters for per-core power
  - Implementation: `src/monitor/msr.py` reads raw MSR registers
  - Access: Group readable via udev (corecycler group), no write permission needed
  - Reads:
    - RAPL_POWER_UNIT (0x606): Energy unit conversions
    - RAPL_PKG_ENERGY_STATUS (0x611): Package-level power in joules
    - RAPL_PP0_ENERGY_STATUS (0x639): Per-core power in joules
    - APERF_CTR (0xe8), MPERF_CTR (0xe7): Clock stretch detection (actual vs reference clock)

## Data Storage

**Databases:**
- SQLite3 (in-tree, no external service)
  - Location: `~/.config/corecyclerlx/history.db`
  - Mode: WAL (write-ahead logging) for crash-safe persistence
  - Schema managed by: `src/history/db.py` via raw SQL CREATE TABLE statements
  - Data: Test results, per-core telemetry (peak frequency, max temp, voltage range), tuner session state, tuning contexts (BIOS snapshots)
  - Client: sqlite3 Python stdlib module

**File Storage:**
- Local filesystem only (no cloud integration)
  - Config: `~/.config/corecyclerlx/` (user home directory)
  - Test profiles: `default.json` in config directory (JSON format)
  - Work directory: `/tmp/corecyclerlx/` (stress test logs, temporary files)
  - Assets: `/usr/share/corecyclerlx/assets/` (SVG icons, desktop file) when installed, or `assets/` in source tree

**Caching:**
- In-memory only (Python runtime state)
  - No persistent cache layer

## Authentication & Identity

**Auth Provider:**
- None (local application, no remote auth)
- Privilege escalation: Runs normally as user with group permissions (corecycler group in NixOS module) or full sudo for root-only features (MCE dmesg reading)
- No service accounts, API keys, or credentials required

## Monitoring & Observability

**Error Tracking:**
- None (no external error reporting service)
- Machine Check Exceptions (MCE) monitoring via local dmesg/sysfs
  - Purpose: Detect CPU errors during stress testing and idle phases
  - Source: `src/engine/scheduler.py` and `src/tuner/engine.py` parse `/dev/kmsg` or sysfs hwmon error counts
  - No network transmission of error data

**Logs:**
- Local logging to console via Python logging module
  - Configuration: `logging.getLogger(__name__)` per-module
  - Log files: Optional, written to `~/.config/corecyclerlx/` or `/tmp/corecyclerlx/` on-demand
  - No centralized log aggregation
  - History database logs test results (see Data Storage section)

**Hardware Monitoring:**
- sysfs hwmon interface (`/sys/class/hwmon/`)
  - CPU temperature: k10temp, zenpower5, coretemp drivers via hwmon
  - Super I/O voltage/fan/temp: nct6775 (Nuvoton), it87 (ITE) drivers
  - Read-only interface, no configuration writes needed
  - Implementation: `src/monitor/hwmon.py` scans `/sys/class/hwmon/*/name` to find CPU temp driver, then reads temp*_input, in*_input files

**Frequency/Scaling Monitoring:**
- cpufreq sysfs (`/sys/devices/system/cpu/cpu*/cpufreq/`)
  - Purpose: Read current frequency, max frequency (scaling_max_freq), available governors
  - Implementation: `src/monitor/frequency.py` parses frequency/timing_max_freq_khz and similar files
  - Read-only, no frequency scaling control from application

## Webhooks & Callbacks

**Incoming:**
- None (no remote webhooks accepted)

**Outgoing:**
- None (no external service notifications)

## Stress Test Backends (System Integration)

**Backends Available:**
- stress-ng (`stress-ng` binary)
  - Purpose: Multi-mode CPU stress testing (CPU, memory, I/O)
  - Control: `src/engine/backends/stress_ng.py` spawns `stress-ng --cpu 1 --timeout X` for per-core testing
  - Integration: Binary wrapped in PATH via Nix flake

- stressapptest (`stressapptest` binary)
  - Purpose: Memory stress testing with error detection
  - Control: `src/engine/backends/stressapptest.py` spawns `stressapptest -s X -m 1 -W`
  - Integration: Binary wrapped in PATH via Nix flake

- Prime95 mprime (`mprime` binary, optional/unfree)
  - Purpose: Integer and floating-point stress testing
  - Control: `src/engine/backends/mprime.py` spawns `mprime -t` with worker threads
  - Integration: Only available in `packages.full`; must enable `unfreeBackends = true` in NixOS module

- y-cruncher (`ycruncher` binary, optional)
  - Purpose: Pi computation stress testing
  - Control: `src/engine/backends/ycruncher.py` spawns y-cruncher with precision settings
  - Integration: Not included in default builds; must be added to flake.nix manually

**Backend Base Class:**
- `src/engine/backends/base.py` - Abstract backend interface
  - Defines: StressMode (SSE, SSE2, AVX, AVX2, AVX512), FFTPreset (SMALL, MEDIUM, LARGE, HUGE)
  - All backends inherit and implement: `start()`, `stop()`, `is_running()`, `get_cpu_usage()`, `get_error_count()`

## System Daemon Integration

**init/systemd:**
- No systemd service unit (application is GUI, runs in user session)
- Can be launched as application shortcut via desktop file (corecyclerlx.desktop)
- NixOS module only configures kernel modules and device access, not application autostart

## udev Integration

**Device Rules:**
- MSR device group access: `SUBSYSTEM=="msr", KERNEL=="msr[0-9]*", GROUP="corecycler", MODE="0640"`
  - Applied by: `services.corecyclerlx` NixOS module in `services.udev.extraRules`
  - Purpose: Allow non-root users in corecycler group to read MSR registers

**tmpfiles Integration:**
- SMU sysfs permissions: Applied via systemd.tmpfiles.rules
  - Rules: Grant corecycler group read/write to `/sys/kernel/ryzen_smu_drv/smu_args`, `mp1_smu_cmd`, `rsmu_cmd`
  - Frequency: Applied on boot and whenever SMU module loads
  - Applied by: `services.corecyclerlx` NixOS module

## Environment Configuration

**Required Environment Variables:**
- `QT_QPA_PLATFORM_PLUGIN_PATH` - Path to Qt6 platform plugins (set automatically in flake.nix devShell)
  - Value: `${pkgs.qt6.qtbase}/lib/qt-6/plugins/platforms`
  - Fallback: Application will crash if Qt platform plugin not found

**Optional Environment Variables:**
- `QT_LOGGING_RULES` - Suppress Qt warnings when running under sudo (no D-Bus session)
  - Set in `src/main.py` to `qt.qpa.services.warning=false;kf.windowsystem.warning=false` by default

**Secrets/Credentials:**
- None required for core functionality
- All configuration is local (no API keys, tokens, or credentials stored)

## NixOS Module Integration

**Module Configuration:**
- Location: `nix/module.nix` (imported as `nixosModules.default` from flake)
- Options: `services.corecyclerlx.enable`, `services.corecyclerlx.unfreeBackends`, `services.corecyclerlx.deviceAccessUser`
- Kernel modules: Conditional loading of ryzen_smu, zenpower, coretemp, nct6775, it87, cpuid, spd5118
- Device access: Group creation (corecycler), udev rules, tmpfiles rules for sysfs permissions
- Example usage in consumer flake:
  ```nix
  imports = [ inputs.linux-corecycler.nixosModules.default ];
  services.corecyclerlx = {
    enable = true;
    deviceAccessUser = "myuser";
    unfreeBackends = true;
  };
  ```

---

*Integration audit: 2026-03-19*
