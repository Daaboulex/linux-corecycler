# CoreCyclerLx

Per-core CPU stress testing and AMD PBO Curve Optimizer tuning for Linux.

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)
![License GPL-3.0](https://img.shields.io/badge/License-GPL--3.0--or--later-green)
![Linux only](https://img.shields.io/badge/Platform-Linux-yellow)

## What Is This?

CoreCyclerLx is a Linux equivalent of [CoreCycler](https://github.com/sp00n/corecycler) (Windows) for AMD PBO Curve Optimizer tuning. It provides a graphical interface for running per-core stress tests and optionally reading/writing Curve Optimizer values via the AMD SMU (System Management Unit).

**What is Curve Optimizer?**
AMD Precision Boost Overdrive (PBO) Curve Optimizer (CO) lets you adjust the voltage-frequency curve on a per-core basis. Negative CO values reduce voltage at a given frequency, allowing the CPU to boost higher within its thermal and power limits. Each core in a processor is unique -- some can handle aggressive negative offsets (e.g., -30), while others become unstable past modest values (e.g., -10). Finding the right value for each core requires per-core testing.

**Why per-core testing matters:**
All-core stress tests (Prime95 with all threads, Cinebench, etc.) cannot reliably detect per-core instability. When all cores are loaded simultaneously, each core runs at lower boost clocks and voltages than it would under single-threaded load. A core that passes an all-core test at 5.0 GHz may crash when it boosts to 5.7 GHz under single-threaded load with an aggressive CO offset. CoreCyclerLx solves this by testing one core at a time at full single-threaded boost clocks, cycling through every core in sequence.

**Why idle and variable load testing matters:**
CO instability often manifests at idle or during load transitions, not under sustained full load. When a core drops to deep C-states (idle) and then wakes up, the voltage ramp-up may be insufficient with an aggressive CO offset. Similarly, the transition from idle to load or from light load to heavy load stresses the voltage regulator in ways that sustained load does not. A CO value that passes hours of Prime95 can still cause random crashes during normal desktop use. CoreCyclerLx addresses this with dedicated idle stability tests and variable load modes.

## Features

- **Per-core stress test cycling** with configurable time, iterations, and cycle count per core
- **Three stress test backends**: mprime (Prime95 CLI), stress-ng, and y-cruncher
- **Five test mode presets**: Quick (2 min/core), Standard (10 min), Thorough (30 min + 2 cycles), Full Spectrum (multi-pass with variable load and idle tests), and Custom
- **Variable load testing**: periodically stops and restarts stress to catch load transition errors
- **Idle stability testing**: monitors for MCE during idle periods between cores to catch C-state transition errors
- **X3D-aware CPU topology detection** -- identifies CCDs, V-Cache CCD (by L3 size comparison), and SMT siblings
- **Live hardware monitoring** -- CPU temperature (Tctl, Tdie, per-CCD Tccd), core voltage (Vcore, Vsoc), frequency, and power via hwmon/k10temp/zenpower
- **Comprehensive SMU integration** for runtime Curve Optimizer, PBO limits, boost override, and PBO scalar via the ryzen_smu kernel module
- **System state detection** -- auto-detects current CO offsets, PBO limits, boost override, PBO scalar, and estimated BCLK before testing
- **MCE error detection** -- monitors Machine Check Exceptions via sysfs and dmesg during stress and idle phases
- **Dark Qt6 GUI** with modern underline-style tabs, SVG-rendered controls, and CCD-aware core grid showing real-time per-core frequency, temperature, and voltage during testing
- **Per-core telemetry logging** -- peak frequency, max temperature, and Vcore range recorded for each core's test run
- **Test profile save/load** -- export and import test configurations as JSON files
- **Safety features** -- thermal limit monitoring (configurable, default 95C), process group cleanup on stop, confirmation dialogs for CO writes, dry-run mode, backup/restore CO values, volatile-only SMU writes (never touches BIOS)
- **Automated PBO Curve Optimizer tuner** -- coarse-to-fine search algorithm that finds optimal per-core CO values automatically; crash-safe SQLite persistence resumes exactly where it left off after reboot or crash; configurable search parameters with best-practice defaults
- **Tuner session history** -- the History tab's "Tuner Sessions" view shows all past auto-tuner sessions with date, status, CPU, core count, confirmed count, duration, and BIOS info; clicking a session reveals per-core state details and the complete test log; detail sections stay hidden until a session is selected to avoid wasted space

## Screenshots

*Screenshots coming soon — the GUI features a dark theme with CCD-aware core grid, live monitoring charts, per-core results table, and Curve Optimizer SMU interface.*

## Supported Hardware

### Curve Optimizer (SMU) Support

| Generation | Example CPUs | CO Range | SMU Mailbox | PBO Limits | Boost Limit | Notes |
|---|---|---|---|---|---|---|
| Zen 2 (Matisse) | 3600X, 3700X, 3900X, 3950X | -- | RSMU | PPT/TDC/EDC | Read only | No CO -- PBO limits and scalar only |
| Zen 2 (Castle Peak) | 3960X, 3990X | -- | RSMU | PPT/TDC/EDC | Read only | Threadripper, no CO |
| Zen 3 (Vermeer) | 5600X, 5800X, 5900X, 5950X | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | Full CO support |
| Zen 3 (Cezanne) | 5600G, 5700G | -30 to +30 | MP1 | PPT/TDC/EDC | -- | APU, same CO commands as Vermeer |
| Zen 3D (Warhol) | 5800X3D | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | V-Cache; be conservative (>-25 risky) |
| Zen 4 (Raphael) | 7600X, 7700X, 7900X, 7950X | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Extended negative range |
| Zen 4 (Phoenix) | 7840U, 8845HS | -50 to +30 | RSMU | PPT/TDC/EDC | -- | APU |
| Zen 4 (Storm Peak) | 7980X TR | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Threadripper |
| Zen 5 (Granite Ridge) | 9600X, 9700X, 9900X, 9950X | -60 to +10 | RSMU | PPT/TDC/EDC | Read/Write | Widest negative CO range |
| Zen 5 (Strix Point) | Ryzen AI 9 HX 370 | -60 to +10 | RSMU | PPT/TDC/EDC | -- | APU |
| Zen 5 (Shimada Peak) | Zen 5 Threadripper | -60 to +10 | RSMU | PPT/TDC/EDC | Read/Write | Different SMU addresses (get_co=0xA3) |

All generations support PBO scalar read/write (1.0x to 10.0x) and OC mode enable/disable.

SMU features require the [ryzen_smu](https://github.com/amkillam/ryzen_smu) kernel module (amkillam fork) and root or appropriate sysfs permissions.

### Curve Shaper (Zen 5)

Curve Shaper is a Zen 5 feature that adjusts voltage across 5 frequency regions and 3 temperature points (15 tuning parameters total). These adjustments stack on top of Curve Optimizer offsets. **Curve Shaper is BIOS-only** -- there are no known SMU commands to read or write Curve Shaper values at runtime. This tool cannot interact with Curve Shaper settings. Configure Curve Shaper in your BIOS alongside CO, then use this tool to validate the combined result.

### PBO Boost Override and BCLK

This tool does not impose any artificial limits on boost clocks. If you have PBO Boost Override set to +200 MHz in BIOS, the tool respects that. If you're running BCLK at 105 MHz or higher (AM5), the effective clocks scale accordingly and the tool adapts. The system state detection reads the actual max frequency from cpufreq sysfs and estimates BCLK when possible.

### Stress Testing Support

Stress testing works on **any x86-64 CPU**, including Intel. The per-core cycling, MCE detection, topology detection, temperature monitoring, and all stress test backends function without the ryzen_smu module. Only the Curve Optimizer tab (SMU read/write) is AMD-specific.

## Safety and PBO Interaction

This is the most important section. Read it carefully before using the Curve Optimizer features.

### CO values set via SMU are volatile

Values written through the SMU interface exist only in the CPU's runtime state. **They always reset on reboot.** There is no mechanism in this tool to write to BIOS/UEFI NVRAM. If something goes wrong, a reboot restores your BIOS settings completely.

### Your BIOS PBO settings are never modified

This tool cannot and does not modify your BIOS configuration. Your BIOS Curve Optimizer values, PBO limits, boost override, and all other PBO settings remain exactly as you configured them. Only the runtime SMU state is affected, and only when you explicitly write values in the Curve Optimizer tab.

### Stress testing does not change any voltage or frequency

The stress testing feature (Start Test button, core cycling, all backends) only runs computational workloads pinned to individual cores. It does not write any CO values, change any voltages, modify any frequencies, or interact with the SMU in any way. It is purely a test harness.

### The Curve Optimizer tab and Auto-Tuner are the only places that write CO

The SMU tab provides explicit per-core spinboxes, per-core Apply buttons, and an "Apply All" bulk action. Each write operation requires a confirmation dialog. Dry-run mode lets you preview writes without touching hardware. Backup/restore lets you save and revert CO values within a session.

The Auto-Tuner also writes CO values via SMU as part of its automated search -- it applies one offset at a time per core, tests it, and advances. All writes are logged and every state transition is persisted to SQLite before acting. The tuner and manual Curve Optimizer tab cannot run simultaneously (mutual exclusion).

### The BIOS-SMU interaction

The full lifecycle of CO values:

1. **Boot**: BIOS applies your configured PBO Curve Optimizer values (e.g., -20 all-core)
2. **Runtime (optional)**: CoreCyclerLx can override individual core CO values via SMU writes -- these override the BIOS values in the CPU's runtime state
3. **Reboot**: All SMU-written values are discarded; BIOS values are reapplied from step 1

If you have PBO values already set in BIOS, those are your baseline. The stress testing feature tests whether those values are stable under per-core load. The SMU tab lets you experiment with different values at runtime without rebooting between each change.

### Process cleanup

Stress test processes are launched in their own process group (`setsid`). On stop, the scheduler sends SIGTERM to the entire process group, waits 3 seconds, then escalates to SIGKILL if needed. No zombie processes are left behind. Closing the application window while a test is running prompts for confirmation and performs the same cleanup.

### Thermal safety

The hardware monitor continuously reads CPU temperatures from hwmon (k10temp/zenpower). The configurable temperature limit (default 95C, adjustable 50-115C in the Configuration tab) controls automatic test pausing when thermal limits are approached.

## Installation

### NixOS (recommended)

Add the flake input to your `flake.nix`:

```nix
{
  inputs = {
    corecyclerlx.url = "github:Daaboulex/corecyclerlx";
    # ...
  };
}
```

Then add the package to your system or Home Manager configuration:

```nix
# FOSS-only (stress-ng bundled, no unfree software):
environment.systemPackages = [
  inputs.corecyclerlx.packages.${pkgs.system}.default
];

# Full (stress-ng + mprime bundled — requires allowUnfree):
environment.systemPackages = [
  inputs.corecyclerlx.packages.${pkgs.system}.full
];
```

| Package variant | Backends included | Unfree software |
|---|---|---|
| `packages.default` | stress-ng | No |
| `packages.full` | stress-ng + mprime | Yes (mprime) |

Both variants include taskset (util-linux) for CPU core pinning. Flake inputs are auto-updated weekly via GitHub Actions, keeping all bundled backends at their latest nixpkgs versions.

### Nix (any distro)

Run directly without installing:

```bash
# FOSS-only
nix run github:Daaboulex/corecyclerlx

# Full (with mprime)
nix run github:Daaboulex/corecyclerlx#full
```

### From source (non-Nix)

```bash
git clone https://github.com/Daaboulex/corecyclerlx.git
cd corecyclerlx
pip install PySide6
python src/main.py
```

When running from source, you must install backends separately (see below).

### Dependencies

**Required:**
- Python 3.12+
- PySide6 >= 6.7 (Qt6 bindings)

**Runtime (needed for stress testing):**
- **taskset** (from util-linux) -- CPU core pinning. Pre-installed on virtually all Linux distributions.
- At least one stress test backend (see below)

**Optional for Curve Optimizer features:**
- **ryzen_smu** kernel module ([amkillam fork](https://github.com/amkillam/ryzen_smu)) -- required for reading/writing CO values via SMU. Supports Zen 1 through Zen 5.

## Backend Setup

CoreCyclerLx supports three stress test backends. You need at least one installed to run tests. The Nix package bundles backends automatically (see [Installation](#installation)), but if you're running from source or want additional backends, follow the guides below.

### mprime (recommended for CO tuning)

mprime is the Linux command-line build of Prime95 by Mersenne Research. It is the most sensitive backend for detecting Curve Optimizer instability because its FFT workloads exercise different power/thermal profiles per core.

**Why mprime?** Small FFTs generate high power draw on a single core, which is exactly the condition that exposes CO instability. Most CO errors manifest as computation errors in mprime's FFT verification, not as system crashes, making it the safest and most informative tool for finding per-core limits.

**mprime is unfree software** (proprietary, free to use). The `packages.full` Nix variant includes it. The `packages.default` variant does not.

#### NixOS

```nix
# Option 1: Use the full package variant (includes mprime automatically)
environment.systemPackages = [
  inputs.corecyclerlx.packages.${pkgs.system}.full
];

# Option 2: Install mprime separately (requires nixpkgs.config.allowUnfree = true)
environment.systemPackages = [ pkgs.mprime ];
```

#### Arch Linux

```bash
# AUR
yay -S mprime-bin
```

#### Ubuntu / Debian

```bash
# Download from mersenne.org
wget https://www.mersenne.org/download/software/v30/30.19/p95v3019b20.linux64.tar.gz
tar xzf p95v3019b20.linux64.tar.gz
sudo install -m755 mprime /usr/local/bin/mprime
```

#### Manual (any distro)

1. Download from [mersenne.org/download](https://www.mersenne.org/download/) (Linux 64-bit)
2. Extract the archive
3. Place the `mprime` binary on your PATH:
   ```bash
   sudo install -m755 mprime /usr/local/bin/mprime
   # or for user-local install:
   install -m755 mprime ~/.local/bin/mprime
   ```
4. Verify: `mprime -v`

### stress-ng (FOSS alternative)

stress-ng is a general-purpose stress testing tool. Less sensitive than mprime for CO detection, but fully open source and often pre-installed.

#### NixOS

Bundled automatically in both `packages.default` and `packages.full`. No extra setup needed.

#### Ubuntu / Debian

```bash
sudo apt install stress-ng
```

#### Arch Linux

```bash
sudo pacman -S stress-ng
```

#### Fedora

```bash
sudo dnf install stress-ng
```

Verify: `stress-ng --version`

### y-cruncher

y-cruncher is a multi-algorithm computational stress test. Useful as a secondary validation backend.

#### Manual (any distro)

1. Download from [numberworld.org/y-cruncher](http://www.numberworld.org/y-cruncher/)
2. Extract and place `y-cruncher` on your PATH:
   ```bash
   sudo install -m755 y-cruncher /usr/local/bin/y-cruncher
   ```
3. Verify: `y-cruncher version`

y-cruncher is not currently packaged in nixpkgs. If you need it on NixOS, install the binary manually to `~/.local/bin/`.

### Backend comparison

| Backend | License | Sensitivity | Best for | Bundled in Nix |
|---|---|---|---|---|
| mprime | Unfree (free to use) | Highest | CO tuning, finding per-core limits | `packages.full` only |
| stress-ng | GPL-2.0 | Medium | General stability, quick screening | Both variants |
| y-cruncher | Freeware | Medium-High | Secondary validation, AVX-heavy loads | Not bundled |

### ryzen_smu kernel module

Required for the **Curve Optimizer tab** and **Auto-Tuner**. Not needed for stress testing alone.

The [amkillam fork](https://github.com/amkillam/ryzen_smu) supports Zen 1 through Zen 5 processors.

#### NixOS

```nix
# Build as a kernel module (adjust the source path or use a flake input)
boot.extraModulePackages = [
  (config.boot.kernelPackages.callPackage /path/to/ryzen_smu/package.nix {})
];
boot.kernelModules = [ "ryzen_smu" ];
```

#### Other distros (DKMS)

```bash
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu
make
sudo make install   # installs as a DKMS module
sudo modprobe ryzen_smu
```

#### Verify

```bash
ls /sys/kernel/ryzen_smu_drv/
# Expected: mp1_smu_cmd  rsmu_cmd  smu_args  version  pm_table
```

Reading and writing CO values through sysfs requires root access or appropriate file permissions on `/sys/kernel/ryzen_smu_drv/`. You can use a udev rule to grant access to a specific group:

```bash
# /etc/udev/rules.d/99-ryzen-smu.rules
KERNEL=="ryzen_smu_drv", SUBSYSTEM=="platform", ATTR{smu_args}="", \
  RUN+="/bin/chmod 0660 /sys/kernel/ryzen_smu_drv/smu_args /sys/kernel/ryzen_smu_drv/rsmu_cmd /sys/kernel/ryzen_smu_drv/mp1_smu_cmd"
```

## Usage Tutorial

### Quick Start

1. Launch CoreCyclerLx (`corecyclerlx` or `python src/main.py`)
2. The application detects your CPU topology automatically (cores, CCDs, SMT, X3D)
3. In the Configuration tab, select a backend -- use stress-ng if mprime is not installed
4. The default preset is **Standard** (10 min/core, 1 cycle) -- adjust or choose a different preset as needed
5. Click **Start Test**
6. Watch the core grid on the left -- each core lights up as it is tested, turning green (passed) or red (failed)
7. The Results tab shows detailed per-core status, elapsed time, and error messages

### Test Mode Presets

| Preset | Time/Core | Cycles | Variable Load | Idle Test | Use Case |
|---|---|---|---|---|---|
| Quick | 2 min | 1 | No | No | Fast screening, rough check |
| Standard | 10 min | 1 | No | No | Initial CO tuning |
| Thorough | 30 min | 2 | No | 5s between cores | Validation after tuning |
| Full Spectrum | 20 min | 3 | Yes | 60s + 10s between | Comprehensive stability proof |
| Custom | User-defined | User-defined | Optional | Optional | Fine-tuned testing |

### Finding Optimal CO Values (Step by Step)

This is the primary workflow for tuning AMD PBO Curve Optimizer:

1. **Set conservative starting CO values in BIOS.** Start with a modest negative offset for all cores (e.g., -15 all-core). This is your baseline.

2. **Run a screening pass.** Launch CoreCyclerLx with mprime, SSE mode, Small FFTs, Standard preset (10 minutes per core, 1 cycle). This takes roughly `10 min * core_count` to complete.

3. **Identify failing cores.** Note which cores fail. These are the cores that cannot sustain the -15 offset at full single-threaded boost clocks.

4. **Reduce CO for failing cores.** Go back to BIOS and reduce the CO for each failing core individually (e.g., -15 to -10, or -15 to -5 for particularly weak cores). Leave passing cores at -15.

5. **Repeat.** Run the test again. Continue adjusting until all cores pass.

6. **Extended validation.** Once all cores pass at Standard, switch to Thorough or Full Spectrum preset. The variable load and idle stability tests catch instability that sustained load misses.

7. **Test different instruction sets.** SSE mode produces the highest single-core boost clocks and is the most sensitive test. After SSE passes, run AVX2 mode as well -- AVX2 workloads use different execution units and can expose different failure modes.

### Using the Curve Optimizer Tab

The Curve Optimizer tab provides direct SMU access for reading and writing per-core CO offsets at runtime. This requires the ryzen_smu kernel module.

- **Read All CO**: reads the current runtime CO offset for every core from the SMU
- **Per-core spinboxes**: adjust the desired CO value for each core independently
- **Apply (per-core)**: writes the spinbox value to the SMU for that single core (with confirmation dialog)
- **Apply All New Values**: writes all spinbox values to the SMU at once (with confirmation dialog)
- **Reset All to 0**: resets all core CO offsets to 0 (with confirmation dialog)
- **Backup Current CO**: saves current CO values so they can be restored later in the session
- **Restore Backup**: reverts CO values to the most recent backup
- **Dry Run**: when checked, CO writes are logged but not applied to hardware

For Zen 2 (Matisse, Castle Peak), the tab shows "Connected (no CO support)" because Zen 2 does not have Curve Optimizer. PBO limits and scalar are still accessible.

Remember: all values written here are **volatile** and reset on reboot. To make changes permanent, set them in your BIOS. This tab is useful for rapid iteration -- adjust CO, run a stress test, adjust again -- without rebooting between each change.

### Using the Auto-Tuner

The Auto-Tuner tab automates the entire PBO Curve Optimizer search process. Instead of manually adjusting CO values core-by-core, rebooting into BIOS, retesting, and repeating, the auto-tuner does it all in one run using runtime SMU writes. It requires the ryzen_smu kernel module.

**How it works:**

The tuner uses a coarse-to-fine search for each core:

1. **Coarse search** -- starts at `start_offset` (default 0) and steps in increments of `coarse_step` (default -5) toward `max_offset`. Each step runs a short stress test (`search_duration`, default 60s). If a step passes, the tuner goes more aggressive. If it fails, the tuner knows the limit is between the last passing value and the failing value.

2. **Fine search** -- starting from the last passing coarse value, steps in increments of `fine_step` (default -1) toward the failure point. This narrows down the exact limit for the core.

3. **Confirmation** -- once the best offset is found, a longer confirmation test (`confirm_duration`, default 300s) validates the value. If confirmation fails, the tuner backs off by one fine step and retries. After `max_confirm_retries` (default 2) failures at a value, it backs off further.

4. **Result** -- each core ends up with a confirmed best CO offset. The final profile can be exported as JSON or applied to the Curve Optimizer tab.

**Crash safety:**

Every state transition is committed to SQLite before acting. If the system crashes or reboots mid-test (which is expected during CO tuning -- that's how you find the limit), the tuner detects the interrupted session on next launch and offers to resume. It re-applies CO offsets from saved state, treats the interrupted test as a failure, and continues from the next action. No progress is lost.

**Per-core state machine:**

```
NOT_STARTED → COARSE_SEARCH → FINE_SEARCH → SETTLED → CONFIRMING → CONFIRMED
                                    |                       |
                                    ↓                       ↓
                                 SETTLED              FAILED_CONFIRM
                                                      (backs off, retries)
```

**Configuration options:**

| Parameter | Default | Range | Description |
|---|---|---|---|
| Start Offset | 0 | -60 to +30 | Starting CO value for all cores |
| Coarse Step | 5 | 1-15 | Step size during coarse search |
| Fine Step | 1 | 1-5 | Step size during fine search |
| Max Offset | -50 | -60 to +60 | Most aggressive offset to try (auto-clamped to CPU generation) |
| Search Duration | 60s | 10-600s | Test duration per step during search |
| Confirm Duration | 300s | 30-1800s | Test duration for confirmation run |
| Max Confirm Retries | 2 | 0-5 | Retries before backing off from a value |
| Backend | mprime | mprime/stress-ng/y-cruncher | Stress test backend |
| Mode | SSE | SSE/AVX/AVX2/AVX512 | Stress test instruction set |
| FFT Preset | SMALL | SMALL/MEDIUM/LARGE/HEAVY/ALL | FFT size preset (mprime) |
| Test Order | sequential | sequential/round_robin/weakest_first | Core testing order |
| Abort on Consecutive Failures | 0 | 0-10 | Abort if N cores fail at start_offset (0 = disabled) |

**Workflow:**

1. Go to the **Auto-Tuner** tab
2. Configure search parameters (defaults are good for most CPUs)
3. Click **Start** -- the tuner begins testing cores sequentially
4. Watch the core status table update in real-time: phase, current offset, best offset, test count, last result
5. When all cores are confirmed, the session completes with a full CO profile
6. Use **Export Profile** to copy the results or apply them to the Curve Optimizer tab
7. Completed (and interrupted) sessions appear in the **History** tab's **Tuner Sessions** view -- click any session to review per-core state details and the full test log

**Tips:**

- **mprime with Small FFTs and SSE mode** is the gold standard for CO testing -- it produces the highest single-core clocks and the most sensitive error detection
- The default `max_offset` of -50 is appropriate for Zen 4. For Zen 5, you can push to -60. For Zen 3/3D, the CPU generation's range (-30) is enforced automatically.
- **Sequential test order** (default) finishes one core completely before moving to the next. Use **round robin** if you want partial results for all cores faster.
- A typical 16-core run with default settings takes roughly 2-4 hours depending on how aggressive each core can go.
- If many cores fail at the starting offset, enable **abort on consecutive failures** (e.g., 3) to stop early -- this usually means BIOS PBO settings need adjustment first.

### Recommended Test Settings

| Scenario | Backend | Mode | FFT Preset | Preset | Notes |
|---|---|---|---|---|---|
| Quick screening | stress-ng | SSE | -- | Quick | Fast check, lower sensitivity |
| Initial CO tuning | mprime | SSE | Small | Standard | Best starting point |
| Thorough validation | mprime | SSE | Huge | Thorough | Variable load + idle tests |
| Comprehensive stability | mprime | SSE | All | Full Spectrum | Multi-cycle with all test modes |
| AVX2 validation | mprime | AVX2 | Heavy | Standard | Different execution units |

**Backend notes:**
- **mprime** (Small FFTs, SSE) is the gold standard for CO testing -- it produces the highest single-core clocks and the most sensitive error detection (rounding checks, SUMOUT verification)
- **stress-ng** is a good fallback when mprime is not installed; it uses computational verification but is generally less sensitive
- **y-cruncher** provides a different class of workload (multi-algorithm) and is good for supplementary testing

### Understanding Results

The core grid and results table use the following states:

- **Green (passed)**: the core completed the stress test with no errors detected
- **Red (failed)**: the core produced an error during the stress test
- **Yellow/active**: the core is currently being tested
- **Gray (pending)**: the core has not been tested yet in this cycle

**Error types:**

| Error Type | Meaning | Severity | Typical Cause |
|---|---|---|---|
| MCE (Machine Check Exception) | Hardware-level CPU error detected via sysfs or dmesg | Critical | CO too aggressive -- core voltage too low for requested frequency |
| Computation | Stress test detected a wrong result (rounding error, illegal sumout, mismatch) | High | CO too aggressive -- subtle numerical instability |
| Idle instability | MCE detected during idle/C-state transition | High | CO offset unstable during voltage ramp-up from deep sleep |
| Load transition | Error during variable load stop/start cycle | High | Voltage regulation insufficient during rapid load changes |
| Timeout | Stress test process stopped responding | Medium | May indicate a hang caused by instability; may also be benign |
| Crash | Stress test process terminated unexpectedly | High | Core instability causing instruction faults |

**MCE errors are the most serious.** They indicate that the CPU detected an actual hardware-level error in computation. If you see MCE errors on a core, that core's CO value must be reduced (made less negative).

**Computation errors** (rounding errors, illegal sumout in mprime) mean the core produced an incorrect result. This is the most common failure mode during CO tuning and indicates the offset is too aggressive for that core.

**Idle instability** errors are caught by the idle stability test phase. These indicate that the CO offset is unstable when the core transitions between C-states (sleep/wake). This is a common failure mode that traditional sustained-load stress tests miss entirely.

## Architecture

```
src/
  main.py                    # Application entry point, dark theme, Qt setup
  engine/
    topology.py              # CPU topology: cores, CCDs, L3 cache, X3D V-Cache detection
    scheduler.py             # Per-core test cycling, variable load, idle tests, process management
    detector.py              # MCE detection via sysfs machinecheck + dmesg parsing
    backends/
      base.py                # Abstract backend interface (StressBackend, StressConfig, StressResult)
      mprime.py              # Prime95 CLI backend (local.txt/prime.txt generation, output parsing)
      stress_ng.py           # stress-ng backend (cpu-method selection, verification)
      ycruncher.py           # y-cruncher backend (component stress mode)
  smu/
    commands.py              # SMU command IDs per CPU generation (12 gens), CO argument encoding/decoding
    driver.py                # ryzen_smu sysfs interface (CO, PBO limits, boost, scalar, system state)
    pmtable.py               # PM table reading
  monitor/
    hwmon.py                 # k10temp/zenpower: Tctl, Tdie, Tccd temps, Vcore, Vsoc voltages
    frequency.py             # Per-core frequency monitoring
    power.py                 # Power consumption monitoring
  config/
    settings.py              # JSON settings and test profile persistence (~/.config/corecyclerlx/)
  tuner/
    __init__.py              # Package re-exports
    config.py                # TunerConfig dataclass (14 search parameters with defaults)
    state.py                 # CoreState and TunerSession dataclasses
    persistence.py           # SQLite operations: session CRUD, core state upsert, test log
    engine.py                # TunerEngine orchestrator: state machine, core scheduling, crash recovery
  gui/
    main_window.py           # Main window: toolbar, tabs, test worker thread, profile management
    config_tab.py            # Test configuration UI (backend, mode, FFT, timing, presets, safety)
    results_tab.py           # Per-core results table and summary
    monitor_tab.py           # Live temperature, voltage, frequency charts
    smu_tab.py               # Curve Optimizer read/write interface with backup/restore and dry-run
    tuner_tab.py             # Auto-Tuner UI: config panel, core status table, test log, controls
    history_tab.py           # History tab: test run log and tuner session browser with per-core detail view
    widgets/
      core_grid.py           # CCD-aware visual core grid with per-core status coloring
      charts.py              # Real-time monitoring charts
tests/
  conftest.py                # Shared fixtures, mock topology builders, mock backends
  test_smu_commands.py       # 160+ tests: generation detection, encode/decode round-trips, command sets
  test_smu_driver.py         # SMU driver: send command, CO read/write, boost limits, backup/restore
  test_safety.py             # Safety invariants: bounds checking, process cleanup, file containment
  test_scheduler.py          # Scheduler: init, run, callbacks, stop/kill, missing cores
  test_detector.py           # MCE detection: sysfs, dmesg, graceful failure
  test_topology.py           # CPU topology parsing and CCD detection
  test_settings.py           # Settings persistence and profile save/load
  test_backends.py           # Backend command generation and output parsing
  test_monitor.py            # Hardware monitoring
  test_pmtable.py            # PM table reading
  test_tuner_config.py       # TunerConfig defaults, JSON roundtrip, CO range clamping
  test_tuner_persistence.py  # Session CRUD, core state upsert, test log, schema migration
  test_tuner_engine.py       # State machine transitions, crash recovery, core scheduling
  test_tuner_tab.py          # Auto-Tuner GUI widget tests
flake.nix                  # Nix package: default (FOSS) and full (+ mprime) variants
pyproject.toml             # Python project metadata, entry point
assets/
  icon.svg                 # Application icon (gear with cycling arrows)
  corecyclerlx.desktop     # XDG desktop entry
  arrow-*.svg              # Qt widget arrows for dark theme
.github/
  workflows/
    update-flake.yml       # Weekly auto-update of flake inputs (nixpkgs, backends)
```

The stress test runs in a `QThread` worker. The scheduler pins each stress process to a single logical CPU using `taskset`, monitors for MCE events during both stress and idle phases, parses backend output for computation errors, and emits Qt signals for GUI updates. Processes are launched in their own process group for clean teardown.

## Contributing

Contributions are welcome. To set up a development environment:

```bash
nix develop
# or manually:
pip install -e ".[dev]"
```

Run tests:

```bash
pytest tests/
```

Lint:

```bash
ruff check src/
ruff format src/
```

When adding a new stress test backend, subclass `StressBackend` from `src/engine/backends/base.py` and implement the required methods (`is_available`, `get_command`, `parse_output`, `get_supported_modes`). Register it in `MainWindow._get_backend()`.

## Acknowledgments

- [CoreCycler](https://github.com/sp00n/corecycler) by sp00n -- the original Windows per-core stress test cycler that inspired this project
- [CoreCycler-GUI](https://github.com/LucidLuxxx/CoreCycler-GUI) by LucidLuxxx -- Windows GUI for CoreCycler
- [ryzen_smu](https://github.com/leogx9r/ryzen_smu) by leogx9r and the [amkillam fork](https://github.com/amkillam/ryzen_smu) -- Linux kernel module for AMD SMU access
- [ZenStates-Core](https://github.com/irusanov/ZenStates-Core) by irusanov -- reference for SMU command IDs across generations

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for details.
