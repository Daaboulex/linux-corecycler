# CoreCycler

<!-- BEGIN generated:badges -->
[![NixOS unstable](https://img.shields.io/badge/NixOS-unstable-78C0E8?logo=nixos&logoColor=white)](https://nixos.org)
[![License: GPL-3.0](https://img.shields.io/badge/License-GPL--3.0-blue.svg)](./LICENSE)
<!-- END generated:badges -->

Per-core CPU stress testing and AMD PBO Curve Optimizer tuning for Linux.

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)
![License GPL-3.0](https://img.shields.io/badge/License-GPL--3.0--or--later-green)
![Version 0.0.1](https://img.shields.io/badge/Version-0.0.1-brightgreen)
![Linux only](https://img.shields.io/badge/Platform-Linux-yellow)

> **Development status:** CoreCycler is actively developed and tested on an **AMD Ryzen 9 9950X3D** (Zen 5, dual-CCD X3D, AM5). Other AMD Ryzen processors (Zen 2–5) should work but have not been tested as thoroughly. Intel CPUs are supported for stress testing only (no Curve Optimizer). If you encounter issues on other hardware, please [open an issue](https://github.com/Daaboulex/linux-corecycler/issues) with your CPU model and description.

## What Is This?

CoreCycler is a Linux equivalent of [CoreCycler](https://github.com/sp00n/corecycler) (Windows) for AMD PBO Curve Optimizer tuning. It provides a graphical interface for running per-core stress tests and optionally reading/writing Curve Optimizer values via the AMD SMU (System Management Unit).

**What is Curve Optimizer?**
AMD Precision Boost Overdrive (PBO) Curve Optimizer (CO) lets you adjust the voltage-frequency curve on a per-core basis. Negative CO values reduce voltage at a given frequency, allowing the CPU to boost higher within its thermal and power limits. Each core in a processor is unique -- some can handle aggressive negative offsets (e.g., -30), while others become unstable past modest values (e.g., -10). Finding the right value for each core requires per-core testing.

**Why per-core testing matters:**
All-core stress tests (Prime95 with all threads, Cinebench, etc.) cannot reliably detect per-core instability. When all cores are loaded simultaneously, each core runs at lower boost clocks and voltages than it would under single-threaded load. A core that passes an all-core test at 5.0 GHz may crash when it boosts to 5.7 GHz under single-threaded load with an aggressive CO offset. CoreCycler solves this by testing one core at a time at full single-threaded boost clocks, cycling through every core in sequence.

**Why idle and variable load testing matters:**
CO instability often manifests at idle or during load transitions, not under sustained full load. When a core drops to deep C-states (idle) and then wakes up, the voltage ramp-up may be insufficient with an aggressive CO offset. Similarly, the transition from idle to load or from light load to heavy load stresses the voltage regulator in ways that sustained load does not. A CO value that passes hours of Prime95 can still cause random crashes during normal desktop use. CoreCycler addresses this with dedicated idle stability tests and variable load modes.

## Features

- **Per-core stress test cycling** with configurable time, iterations, and cycle count per core
- **Four stress backends**: mprime (Prime95 CLI), stress-ng, y-cruncher, and stressapptest — auto-discovered via backend registry
- **Five test mode presets**: Quick (2 min/core), Standard (10 min), Thorough (30 min + 2 cycles), Full Spectrum (multi-pass with variable load and idle tests), and Custom
- **Variable load testing**: periodically stops and restarts stress to catch load transition errors
- **Idle stability testing**: monitors for MCE during idle periods between cores to catch C-state transition errors
- **X3D-aware CPU topology detection** -- identifies CCDs, V-Cache CCD (by L3 size comparison), and SMT siblings
- **Live hardware monitoring** -- CPU temperature (Tctl, Tdie, per-CCD Tccd), core voltage (Vcore, Vsoc), frequency, per-core CPU usage, and power via hwmon (k10temp/zenpower/coretemp) with automatic Super I/O fallback (nct6799/nct6798) for Vcore on Zen 5; with root access, MSR-based clock stretch detection (APERF/MPERF), per-core power (RAPL MSR), and package power. Per-core view shows actual frequency vs boost ceiling (scaling_max_freq), usage %, stretch %, per-core watts, and temperature with active core highlighting during tests.
- **MSR-based clock stretch detection** -- reads APERF/MPERF counters to compute the actual-vs-reference clock ratio per core; values below ~97% under load indicate clock stretching (a sign of CO instability or power limiting). Stretch % is only displayed for active cores (>5% usage) to avoid false readings from C-state sleep noise on idle cores. Requires root.
- **Comprehensive SMU integration** for runtime Curve Optimizer, PBO limits, boost override, and PBO scalar via the ryzen_smu kernel module
- **System state detection** -- auto-detects current CO offsets, PBO limits, boost override, PBO scalar, and estimated BCLK before testing
- **MCE error detection** -- monitors Machine Check Exceptions via sysfs and dmesg during stress and idle phases
- **Dark Qt6 GUI** with modern underline-style tabs, SVG-rendered controls, and CCD-aware core grid showing real-time per-core frequency, temperature, and voltage during testing
- **Per-core telemetry logging** -- peak frequency, max temperature, and Vcore range recorded for each core's test run; thread drift warnings condensed to 1 summary line per test (instead of per-TID warnings); phase transition logs show context including phase name, attempt counters, and consecutive failure tracking
- **Test profile save/load** -- export and import test configurations as JSON files
- **CO profile save/load** -- save and load per-core Curve Optimizer offsets as JSON files from the CO tab ("Save CO Profile" / "Load CO Profile"), the Auto-Tuner ("Export Profile" saves to file), and the History tab ("Load to CO Tab" for past tuner sessions); the JSON format is universal across all three
- **Safety features** -- thermal limit monitoring (configurable, default 95C), process group cleanup on stop, confirmation dialogs for CO writes, dry-run mode, backup/restore CO values, volatile-only SMU writes (never touches BIOS)
- **Automated PBO Curve Optimizer tuner** -- coarse-to-fine search algorithm that finds optimal per-core CO values automatically; **smart backoff** with pre-confirm filtering, midpoint jump, and binary search narrows stable offsets efficiently (baseline floor from `inherit_current`, not zero); crash-safe SQLite persistence (WAL mode) resumes exactly where it left off after reboot or crash; configurable search parameters with best-practice defaults; **inherit current CO** option reads existing SMU offsets as starting points for incremental tuning; **CO isolation** ensures only the tested core has a non-baseline offset during search (prevents false blame when a crash occurs); **automatic 3-stage multi-core validation** after all cores are individually confirmed: (1) per-core with all offsets live — catches power delivery interactions, (2) all-core simultaneous stress — full package power worst case, (3) alternating half-core load split by CCD — catches boost ramp voltage transients; failed cores are automatically backed off and validation restarts; session picker dialog for resuming from multiple paused/interrupted sessions; Curve Optimizer tab is locked during tuner operation to prevent SMU conflicts
- **Five tuner test orderings**: sequential (finish each core), round_robin (cycle one test per core), weakest_first (prioritize cores nearest to settling), **ccd_alternating** (alternate between CCDs for thermal coverage), and **ccd_round_robin** (rotate one test per core across CCDs — gives each core cool-down time between tests, catches cold-boot and thermal transition failures)
- **Tuner state machine** -- 7 phases per core: not_started → coarse_search → fine_search → settled → confirming → confirmed (or failed_confirm → smart backoff with pre-confirm filter, midpoint jump, and binary search). Clock stretch detection (APERF/MPERF) during tuner tests with configurable threshold — marks test as FAIL even if stress passed when voltage droop is detected
- **Memory information tab** -- displays per-DIMM details (size, type, speed, manufacturer, part number, rank, voltage) via dmidecode; **Memory Controller group box** showing live FCLK/UCLK/MCLK frequencies and FCLK:UCLK ratio indicator (green for 1:1 coupled, amber for decoupled) from ryzen_smu PM table with version-aware parsing; live VDD voltage from PM table (not the SPD default 1.10V); **SPD Timings group box** showing DDR5 primary timings (tCL-tRCD-tRP-tRAS-tRC in clock cycles) and secondary timings (tRFC1, tRFCsb, tWR in nanoseconds) decoded from SPD EEPROM via spd5118 sysfs; live DDR5 DIMM temperature monitoring via SPD5118 hwmon; configurable memory stress testing (1–60 minutes) with tool selection (stressapptest or stress-ng --vm); dependency status display showing available tools and drivers; PM table version displayed with "Verified" or "Uncalibrated" status for unknown versions
- **Tuner session history** -- the History tab auto-detects tuner sessions and defaults to the Tuner Sessions view with the latest session pre-selected; shows date, status, CPU, core count, confirmed count, duration, and BIOS info; clicking a session reveals per-core state details and the complete test log; sessions can be deleted individually; Ctrl+C copies selected rows from tuner tables as tab-separated text; **"Load to CO Tab"** button loads confirmed offsets from a tuner session into the Curve Optimizer spinboxes for review (blue banner indicates loaded values — click "Apply All" to write to SMU)
- **History management** -- grouped view with tuning context detection (BIOS + CO snapshot), run comparison, context deletion, orphan cleanup, BIOS change detection with visual indicators; SQL-based summary counters (Completed/Crashed/Stopped) that correctly aggregate all sessions; automatic stale session recovery on startup (sessions left "Running" from a crash are marked "Crashed" with per-session logging)

## Screenshots

*Screenshots coming soon — the GUI features a dark theme with CCD-aware core grid, live monitoring charts, per-core results table, and Curve Optimizer SMU interface.*

## Supported Hardware

### Curve Optimizer (SMU) Support

| Generation | Example CPUs | CO Range | SMU Mailbox | PBO Limits | Boost Limit | Notes |
|---|---|---|---|---|---|---|
| Zen 1 / Zen+ | 1800X, 2700X | -- | RSMU | PPT/TDC/EDC | Read only | No CO — PBO limits and scalar only (Matisse SMU fallback) |
| Zen 2 (Matisse) | 3600X, 3700X, 3900X, 3950X | -- | RSMU | PPT/TDC/EDC | Read only | No CO — PBO limits and scalar only |
| Zen 2 (Castle Peak) | 3960X, 3970X, 3990X | -- | RSMU | PPT/TDC/EDC | Read only | Threadripper, no CO |
| Zen 3 (Vermeer) | 5600X, 5800X, 5900X, 5950X | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | Full CO support |
| Zen 3 (Cezanne) | 5600G, 5700G | -30 to +30 | MP1 | PPT/TDC/EDC | -- | APU, same CO commands as Vermeer |
| Zen 3 (Rembrandt) | 6800U, 6900HX | -30 to +30 | MP1 | PPT/TDC/EDC | -- | APU, uses Cezanne CO commands |
| Zen 3D (Warhol) | 5800X3D | -30 to +30 | MP1 | PPT/TDC/EDC | Read only | V-Cache; be conservative (>-25 risky) |
| Zen 4 (Raphael) | 7600X, 7700X, 7900X, 7950X | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Extended negative range |
| Zen 4 X3D (Raphael) | 7800X3D, 7900X3D, 7950X3D | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | V-Cache; same commands as Raphael |
| Zen 4 (Phoenix) | 7840U, 7840HS, 8845HS | -50 to +30 | RSMU | PPT/TDC/EDC | -- | APU (Phoenix / Hawk Point) |
| Zen 4 (Dragon Range) | 7945HX, 7845HX | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Mobile, same silicon as Raphael |
| Zen 4 (Storm Peak) | 7980X, 7970X TR | -50 to +30 | RSMU | PPT/TDC/EDC | Read/Write | Threadripper PRO |
| Zen 5 (Granite Ridge) | 9600X, 9700X, 9900X, 9950X | -60 to +10 | RSMU | PPT/TDC/EDC | Read/Write | Widest negative CO range |
| Zen 5 X3D (Granite Ridge) | 9800X3D, 9900X3D, 9950X3D | -60 to +10 | RSMU | PPT/TDC/EDC | Read/Write | V-Cache; same commands as Granite Ridge |
| Zen 5 (Strix Point) | Ryzen AI 9 HX 370 | -60 to +10 | RSMU | PPT/TDC/EDC | -- | APU |
| Zen 5 (Strix Halo) | Ryzen AI Max | -60 to +10 | RSMU | PPT/TDC/EDC | -- | APU, uses Strix Point commands |
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

The Auto-Tuner also writes CO values via SMU as part of its automated search -- it applies one offset at a time per core, tests it, and advances. All writes are logged and every state transition is persisted to SQLite before acting. The tuner and manual Curve Optimizer tab cannot run simultaneously -- when the tuner is active, all Curve Optimizer write buttons (Apply, Apply All, Reset, Restore) and spinboxes are disabled. The manual Start Test button is also disabled. This mutual exclusion is enforced automatically.

### The BIOS-SMU interaction

The full lifecycle of CO values:

1. **Boot**: BIOS applies your configured PBO Curve Optimizer values (e.g., -20 all-core)
2. **Runtime (optional)**: CoreCycler can override individual core CO values via SMU writes -- these override the BIOS values in the CPU's runtime state
3. **Reboot**: All SMU-written values are discarded; BIOS values are reapplied from step 1

If you have PBO values already set in BIOS, those are your baseline. The stress testing feature tests whether those values are stable under per-core load. The SMU tab lets you experiment with different values at runtime without rebooting between each change.

### Process cleanup

Stress test processes are launched in their own process group (`setsid`) with `PR_SET_PDEATHSIG(SIGKILL)` — if the parent process dies unexpectedly, the kernel automatically kills the stress process (no orphans). On stop, the scheduler sends SIGTERM to the entire process group, waits 3 seconds, then escalates to SIGKILL if needed. No zombie processes are left behind. Closing the application window while a test is running prompts for confirmation and performs the same cleanup. QThread workers use a graceful `force_stop()` → `terminate()` shutdown escalation.

### Thermal safety

The hardware monitor continuously reads CPU temperatures from hwmon (k10temp/zenpower/zenpower3/zenpower5/coretemp). The configurable temperature limit (default 95C, adjustable 50-115C in the Configuration tab) controls automatic test pausing when thermal limits are approached.

<!-- BEGIN generated:upstream -->
## Upstream

| | |
|---|---|
| **Project** | Original code (no upstream) |
| **License** | N/A |
| **Tracked** | N/A |
<!-- END generated:upstream -->

<!-- BEGIN generated:installation -->
## Installation

### NixOS (recommended)

Add the flake input to your `flake.nix`:

```nix
{
  inputs = {
    corecycler.url = "github:Daaboulex/linux-corecycler";
    # ...
  };
}
```

Then import the NixOS module and enable the service:

```nix
{
  # Import the module
  imports = [ inputs.corecycler.nixosModules.default ];

  # Enable with all defaults (FOSS-only, ryzen_smu, device access)
  services.corecycler = {
    enable = true;
    deviceAccessUser = "your-username";  # required — user added to the corecycler group
  };
}
```

The module handles everything: the corecycler package, kernel modules, udev rules for MSR device access, tmpfiles for SMU sysfs permissions, and the `corecycler` group. No manual kernel module configuration needed.

**Full example** (AMD Zen 5 desktop with Nuvoton Super I/O):

```nix
services.corecycler = {
  enable = true;
  deviceAccessUser = "your-username";
  unfreeBackends = true;   # include mprime (best for CO tuning)
  ryzenSmu = true;         # SMU access for Curve Optimizer (default)
  zenpower = true;         # zenpower5: richer monitoring than k10temp
  nct6775 = true;          # Nuvoton Super I/O: motherboard Vcore fallback
};
```

**Full example** (AMD system with Gigabyte board / ITE Super I/O):

```nix
services.corecycler = {
  enable = true;
  deviceAccessUser = "your-username";
  unfreeBackends = true;
  it87 = true;             # ITE Super I/O: motherboard Vcore on Gigabyte
};
```

**Full example** (DDR5 system with DIMM temperature monitoring):

```nix
services.corecycler = {
  enable = true;
  deviceAccessUser = "your-username";
  unfreeBackends = true;
  zenpower = true;         # CPU monitoring
  nct6775 = true;          # Vcore via Super I/O
  spd5118 = true;          # DDR5 DIMM temperatures via SPD hub
};
```

**Module options:**

| Option | Type | Default | Description |
|---|---|---|---|
| `enable` | bool | `false` | Enable CoreCycler |
| `unfreeBackends` | bool | `false` | Include mprime (unfree). When false, stress-ng and stressapptest are bundled |
| **AMD SMU** | | | |
| `ryzenSmu` | bool | `true` | Load [ryzen_smu](https://github.com/amkillam/ryzen_smu) kernel module for CO read/write via SMU. Zen 1–5 |
| **CPU hwmon** | | | |
| `zenpower` | bool | `false` | Load [zenpower5](https://github.com/mattkeenan/zenpower5) instead of k10temp — temps, SVI2 voltage (Zen 1–4), RAPL power. Blacklists k10temp. Zen 1–5 |
| `coretemp` | bool | `false` | Load in-tree coretemp for Intel CPU temperature monitoring |
| **Super I/O** | | | |
| `nct6775` | bool | `false` | Load in-tree nct6775 for Nuvoton Super I/O chips (Vcore, fans, temps). ASUS, MSI, ASRock |
| `it87` | bool | `false` | Load out-of-tree [it87](https://github.com/frankcrawford/it87) for ITE Super I/O chips (38+ models). Gigabyte |
| **Utility** | | | |
| `cpuid` | bool | `false` | Load in-tree cpuid module for /dev/cpu/*/cpuid access |
| **Memory** | | | |
| `spd5118` | bool | `false` | Load spd5118 + i2c_dev modules for DDR5 DIMM temperature monitoring via the SPD hub chip |
| **Device access** | | | |
| `deviceAccess` | bool | `true` | Grant `deviceAccessUser` access to MSR/SMU sysfs without sudo |
| `deviceAccessUser` | string | `""` | Username for device access (required when `deviceAccess` is true) |

All out-of-tree kernel modules (ryzen_smu, zenpower5, it87) are built automatically against your running kernel. Both standard GCC kernels and Clang/LTO kernels (e.g., CachyOS) are supported — the build system auto-detects the compiler toolchain. In-tree modules (msr, nct6775, coretemp, cpuid) are simply loaded via `boot.kernelModules`.

**Package-only install** (no kernel modules or device access — manual setup):

```nix
# FOSS-only (stress-ng bundled, no unfree software):
environment.systemPackages = [
  inputs.corecycler.packages.${pkgs.system}.default
];

# Full (stress-ng + mprime bundled — requires allowUnfree):
environment.systemPackages = [
  inputs.corecycler.packages.${pkgs.system}.full
];
```

| Package variant | Backends included | Unfree software |
|---|---|---|
| `packages.default` | stress-ng, stressapptest | No |
| `packages.full` | stress-ng, stressapptest, mprime | Yes (mprime) |

Both variants include taskset (util-linux) for CPU core pinning. Flake inputs are auto-updated weekly via GitHub Actions, keeping all bundled backends at their latest nixpkgs versions.

### Nix (any distro)

Run directly without installing:

```bash
# FOSS-only
nix run github:Daaboulex/linux-corecycler

# Full (with mprime)
nix run github:Daaboulex/linux-corecycler#full
```

### Arch Linux

```bash
# Core dependencies
sudo pacman -S python python-pyside6 stress-ng stressapptest dmidecode

# mprime (AUR, optional — unfree, best backend for CO tuning)
yay -S mprime-bin

# ryzen_smu kernel module (required for Curve Optimizer)
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install
sudo modprobe ryzen_smu

# Run
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
sudo python src/main.py
```

### Ubuntu / Debian

```bash
# Core dependencies
sudo apt install python3 python3-pip stress-ng stressapptest dmidecode

# PySide6 (Qt6 bindings)
pip3 install PySide6

# mprime (optional — unfree, manual download)
wget https://www.mersenne.org/download/software/v30/30.19/p95v3019b20.linux64.tar.gz
tar xzf p95v3019b20.linux64.tar.gz
sudo install -m755 mprime /usr/local/bin/mprime

# ryzen_smu kernel module (required for Curve Optimizer)
sudo apt install build-essential linux-headers-$(uname -r)
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install
sudo modprobe ryzen_smu

# Run
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
sudo python3 src/main.py
```

### Fedora

```bash
# Core dependencies
sudo dnf install python3 python3-pip stress-ng dmidecode

# PySide6 (Qt6 bindings)
pip3 install PySide6

# stressapptest (build from source — not in default repos)
git clone https://github.com/stressapptest/stressapptest.git
cd stressapptest && ./configure && make && sudo make install

# ryzen_smu kernel module (required for Curve Optimizer)
sudo dnf install kernel-devel gcc make
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu && make && sudo make install
sudo modprobe ryzen_smu

# Run
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
sudo python3 src/main.py
```

### From source (any distro)

```bash
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
pip install PySide6
sudo python src/main.py
```

When running from source, you must install stress test backends and kernel modules separately (see [Backend Setup](#backend-setup) and [Kernel Module Requirements](#kernel-module-requirements) below).

### Running as Root

CoreCycler should be run as root (`sudo`) for full functionality:

```bash
sudo corecycler          # Nix-installed
sudo python src/main.py    # from source
```

| Feature | Without root | With root |
|---|---|---|
| Stress testing (per-core cycling) | ✅ Full | ✅ Full |
| Temperature monitoring (k10temp) | ✅ Full | ✅ Full |
| Per-CCD temperatures (Tccd1/Tccd2) | ✅ Full | ✅ Full |
| Core frequency monitoring (sysfs) | ✅ Full | ✅ Full |
| Package power (RAPL sysfs or hwmon) | ✅ Via hwmon (zenpower) | ✅ Full |
| Per-core power (RAPL MSR) | ❌ Needs /dev/cpu/N/msr | ✅ Full |
| Clock stretch detection (APERF/MPERF) | ❌ Needs /dev/cpu/N/msr | ✅ Full |
| Vcore voltage | ✅ Via Super I/O or zenpower | ✅ Via Super I/O or zenpower |
| DIMM info (dmidecode) | ❌ Needs root | ✅ Full |
| DDR5 DIMM temperatures (SPD5118) | ✅ If spd5118 module loaded | ✅ If spd5118 module loaded |
| Curve Optimizer (SMU read/write) | ❌ Needs /sys/kernel/ryzen_smu_drv | ✅ Full |

When running without root, the status bar displays a warning listing unavailable features. The app adapts gracefully — unavailable data shows "N/A" instead of stale or missing values. Qt/KDE platform warnings (D-Bus portal, window system) that would normally appear under `sudo` are suppressed automatically.

**Non-root access on non-NixOS distros:** The NixOS module sets up udev rules and tmpfiles automatically. On other distros, if you want to avoid running as root, you need to manually set permissions on `/dev/cpu/*/msr` (for MSR access) and `/sys/kernel/ryzen_smu_drv/*` (for SMU access). See the [ryzen_smu section](#ryzen_smu-kernel-module) for an example udev rule. Running as root is the simplest approach.

**Note:** Vcore voltage is read from the CPU hwmon driver (zenpower/zenpower3/zenpower5/k10temp SVI2 registers) when available. On **Zen 5** CPUs, voltage telemetry uses SVI3 which no Linux driver supports yet — the tool automatically falls back to the **Super I/O chip** on the motherboard, which provides an analog Vcore reading from the voltage regulator. Supported Super I/O chips include Nuvoton (nct6775–nct6799, common on ASUS/MSI/ASRock) and ITE (IT8625–IT8772, common on Gigabyte). If neither source is available, Vcore shows "N/A".

### Dependencies

**Required:**
- Python 3.12+
- PySide6 >= 6.7 (Qt6 bindings)

**Runtime (needed for stress testing):**
- **taskset** (from util-linux) -- CPU core pinning. Pre-installed on virtually all Linux distributions.
- At least one stress test backend (see below)

**Optional:**
- **dmidecode** -- DIMM information (size, type, speed, manufacturer) in the Memory tab. Bundled in the Nix package. Requires root.
- **ryzen_smu** kernel module ([amkillam fork](https://github.com/amkillam/ryzen_smu)) -- required for reading/writing CO values via SMU. Supports Zen 1 through Zen 5.

### Kernel Module Requirements

CoreCycler uses several kernel modules for hardware access. None are required for basic stress testing, but they unlock monitoring and Curve Optimizer functionality.

| Module | Type | Purpose | Required for |
|---|---|---|---|
| **msr** | In-tree | `/dev/cpu/N/msr` access for APERF/MPERF counters and per-core RAPL | Clock stretch detection, per-core power. Usually loaded by default on most distros |
| **ryzen_smu** | Out-of-tree | SMU sysfs interface for CO read/write, PBO limits, PM table | Curve Optimizer tab, Auto-Tuner. Without it, stress testing works but CO features are unavailable |
| **zenpower** / **zenpower5** | Out-of-tree | Richer AMD hwmon (SVI2/SVI3 voltage, RAPL power) than k10temp | Better voltage and power monitoring (optional — k10temp works for temps) |
| **nct6775** | In-tree | Nuvoton Super I/O chip (Vcore, fan speeds, temps) | Motherboard Vcore on Zen 5 (ASUS, MSI, ASRock). Automatic fallback when zenpower has no voltage |
| **it87** | Out-of-tree | ITE Super I/O chip (38+ models) | Motherboard Vcore on Gigabyte boards |
| **spd5118** + **i2c_dev** | In-tree | DDR5 DIMM temperature monitoring via SPD hub | Live DIMM temperatures in Memory tab |
| **coretemp** | In-tree | Intel CPU temperature monitoring | Intel systems only |

**NixOS:** The `services.corecycler` module handles all kernel modules automatically (builds out-of-tree modules against your kernel, loads them, sets permissions). No manual setup needed.

**Other distros:** Load in-tree modules with `sudo modprobe <name>`. Out-of-tree modules must be built from source (see [ryzen_smu](#ryzen_smu-kernel-module) and distro-specific install sections above).

### CPU Support Summary

| Generation | Stress Testing | Curve Optimizer | CO Range |
|---|---|---|---|
| Zen 1 / Zen+ | Yes | No (PBO limits/scalar only) | -- |
| Zen 2 | Yes | No (PBO limits/scalar only) | -- |
| Zen 3 / 3D | Yes | Full | -30 to +30 |
| Zen 4 / 4D | Yes | Full | -50 to +30 |
| Zen 5 / 5D | Yes | Full | -60 to +10 |
| Intel | Yes | No | -- |

See the [detailed hardware support table](#curve-optimizer-smu-support) for per-generation SMU mailbox details, PBO limits, and boost override support.

<!-- END generated:installation -->

## Backend Setup

CoreCycler supports four stress test backends. You need at least one installed to run tests. The Nix package bundles backends automatically (see [Installation](#installation)), but if you're running from source or want additional backends, follow the guides below.

### mprime (recommended for CO tuning)

mprime is the Linux command-line build of Prime95 by Mersenne Research. It is the most sensitive backend for detecting Curve Optimizer instability because its FFT workloads exercise different power/thermal profiles per core.

**Why mprime?** Small FFTs generate high power draw on a single core, which is exactly the condition that exposes CO instability. Most CO errors manifest as computation errors in mprime's FFT verification, not as system crashes, making it the safest and most informative tool for finding per-core limits.

**mprime is unfree software** (proprietary, free to use). The `packages.full` Nix variant includes it. The `packages.default` variant does not.

#### NixOS

```nix
# Option 1: Use the full package variant (includes mprime automatically)
environment.systemPackages = [
  inputs.corecycler.packages.${pkgs.system}.full
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

### stressapptest (memory stress)

[stressapptest](https://github.com/stressapptest/stressapptest) is Google's hardware stress test tool, designed to maximize randomized memory traffic to expose RAM errors. It is the fastest tool for finding DDR5 frequency/timing instability and memory controller issues.

stressapptest is used in the **Memory tab** for dedicated memory stress testing (configurable 1–60 minute duration) and is also available as a stress backend for the per-core scheduler. In the scheduler, it runs indefinitely (the scheduler handles timing and termination).

#### NixOS

Bundled automatically in both `packages.default` and `packages.full`. No extra setup needed.

#### Ubuntu / Debian

```bash
sudo apt install stressapptest
```

#### Arch Linux

```bash
sudo pacman -S stressapptest
```

Verify: `stressapptest --help`

### Backend comparison

| Backend | License | Sensitivity | Best for | Bundled in Nix |
|---|---|---|---|---|
| mprime | Unfree (free to use) | Highest | CO tuning, finding per-core limits | `packages.full` only |
| stress-ng | GPL-2.0 | Medium | General stability, quick screening | Both variants |
| y-cruncher | Freeware | Medium-High | Secondary validation, AVX-heavy loads | Not bundled |
| stressapptest | Apache-2.0 | High (memory) | DDR5/RAM stability, memory controller errors | Both variants |

### ryzen_smu kernel module

Required for the **Curve Optimizer tab** and **Auto-Tuner**. Not needed for stress testing alone.

The [amkillam fork](https://github.com/amkillam/ryzen_smu) supports Zen 1 through Zen 5 processors.

#### NixOS

The NixOS module (`services.corecycler`) handles ryzen_smu automatically when `ryzenSmu = true` (the default). It builds the module against your kernel, loads it, and sets up sysfs permissions. No manual configuration needed.

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

Reading and writing CO values through sysfs requires root access or appropriate file permissions on `/sys/kernel/ryzen_smu_drv/`. The NixOS module handles this via tmpfiles rules. On other distros, you can use a udev rule to grant access to a specific group:

```bash
# /etc/udev/rules.d/99-ryzen-smu.rules
KERNEL=="ryzen_smu_drv", SUBSYSTEM=="platform", ATTR{smu_args}="", \
  RUN+="/bin/chmod 0660 /sys/kernel/ryzen_smu_drv/smu_args /sys/kernel/ryzen_smu_drv/rsmu_cmd /sys/kernel/ryzen_smu_drv/mp1_smu_cmd"
```

## Usage Tutorial

### Quick Start

1. Launch CoreCycler (`sudo corecycler` or `sudo python src/main.py`) — root is recommended for full monitoring (clock stretch, per-core power, Curve Optimizer). The app works without root but shows reduced telemetry with a status bar warning.
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
| Thorough | 30 min | 2 | No | No | Validation after tuning |
| Full Spectrum | 20 min | 3 | Yes | 60s + 10s between | Comprehensive stability proof |
| Custom | User-defined | User-defined | Optional | Optional | Fine-tuned testing |

### Finding Optimal CO Values (Step by Step)

This is the primary workflow for tuning AMD PBO Curve Optimizer:

1. **Set conservative starting CO values in BIOS.** Start with a modest negative offset for all cores (e.g., -15 all-core). This is your baseline.

2. **Run a screening pass.** Launch CoreCycler with mprime, SSE mode, Small FFTs, Standard preset (10 minutes per core, 1 cycle). This takes roughly `10 min * core_count` to complete.

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
- **Save CO Profile**: exports current spinbox CO values to a JSON file
- **Load CO Profile**: imports CO values from a JSON file into the spinboxes (review before applying)

The JSON format is shared with the Auto-Tuner's Export Profile and the History tab's "Load to CO Tab" -- profiles saved from any of these three locations can be loaded in any other.

For Zen 2 (Matisse, Castle Peak), the tab shows "Connected (no CO support)" because Zen 2 does not have Curve Optimizer. PBO limits and scalar are still accessible.

Remember: all values written here are **volatile** and reset on reboot. To make changes permanent, set them in your BIOS. This tab is useful for rapid iteration -- adjust CO, run a stress test, adjust again -- without rebooting between each change.

### Using the Auto-Tuner

The Auto-Tuner tab automates the entire PBO Curve Optimizer search process. Instead of manually adjusting CO values core-by-core, rebooting into BIOS, retesting, and repeating, the auto-tuner does it all in one run using runtime SMU writes. It requires the ryzen_smu kernel module.

**How it works:**

The tuner uses a coarse-to-fine search for each core:

1. **Coarse search** -- starts at `start_offset` (default 0) and steps in increments of `coarse_step` (default -5) toward `max_offset`. Each step runs a short stress test (`search_duration`, default 60s). If a step passes, the tuner goes more aggressive. If it fails, the tuner knows the limit is between the last passing value and the failing value.

2. **Fine search** -- starting from the last passing coarse value, steps in increments of `fine_step` (default -1) toward the failure point. This narrows down the exact limit for the core.

3. **Confirmation** -- once the best offset is found, a longer confirmation test (`confirm_duration`, default 300s) validates the value. If confirmation fails, the tuner uses a **smart backoff algorithm** to find the nearest stable offset efficiently:

   - **Pre-confirm filter** -- before a full confirmation test, a shorter pre-confirm test runs (`search_duration` * `backoff_preconfirm_multiplier`, default 2x). This quickly filters unstable offsets during backoff without spending the full confirm duration on values that will fail anyway.
   - **Midpoint jump** -- after `midpoint_jump_threshold` (default 3) consecutive pre-confirm failures, the tuner jumps to the midpoint between the failing offset and the baseline instead of continuing linear backoff. This avoids wasting time stepping through a range that is likely all unstable.
   - **Binary search** -- after a midpoint pass, the tuner narrows the exact stable boundary with logarithmic efficiency, converging on the best offset faster than linear backoff.
   - **Baseline floor** -- backoff stops at the BIOS baseline (from `inherit_current`), not at zero. If you started with -20 in BIOS, the tuner will not back off past -20.

4. **Multi-core validation** (automatic, `auto_validate` enabled by default) -- after all cores are individually confirmed, the tuner automatically enters a 3-stage validation sequence that catches failures invisible to per-core testing:

   - **Stage 1 — Per-core with all offsets live**: applies ALL confirmed best offsets simultaneously, then stress tests each core one at a time (cycling per test order). This catches "core 0 at -39 is stable alone but unstable when core 1 is also at -33 due to shared VRM power delivery."
   - **Stage 2 — All-core simultaneous**: all confirmed offsets applied, ALL cores stressed at once for `validate_duration`. Full package power draw worst case. Tests the absolute maximum thermal and electrical stress the offset profile will see.
   - **Stage 3 — Alternating half-core load**: half the cores loaded, half idle, then swap. Split by CCD when available (tests cross-CCD power interactions), else by even/odd index. Catches voltage transients during boost ramp-up/ramp-down as cores enter and exit C-states.

   If any stage fails: the most aggressive core (highest absolute offset) is backed off by one `fine_step`, and validation restarts from stage 1. If no core can be backed off further (all at `start_offset`), the tuner finalizes with the current profile.

   The status label shows the active validation stage and progress (e.g., "VALIDATING S1 (per-core) 5/16"). Disable with `auto_validate = false` if you want to validate manually.

5. **Result** -- each core ends up with a confirmed and cross-validated best CO offset. The final profile can be exported as JSON or applied to the Curve Optimizer tab.

**CO isolation:**

During per-core search, the tuner ensures that the **only non-baseline CO offset** on the CPU is the one being actively stress-tested. Before each test, all other cores are reverted to their baseline offsets (the values inherited from BIOS/SMU at session start). After each test, the tested core is also reverted. This prevents a scenario where a previously-tested core sits at an aggressive offset (e.g., -39), crashes the system while a different core is being tested, and the tuner incorrectly blames the wrong core.

During multi-core validation (after all cores are individually confirmed), CO isolation is deliberately disabled — all confirmed offsets are applied simultaneously to test power delivery interactions between cores. If a validation stage fails, all cores are reverted to baseline before pausing. If an SMU write fails partway through applying offsets, all already-applied cores are reverted to baseline and the tuner pauses (no partial state left on hardware).

**Crash safety:**

Every state transition is committed to SQLite (WAL mode) before acting. If the system crashes or reboots mid-test (which is expected during CO tuning -- that's how you find the limit), the tuner detects the interrupted session on next launch and offers to resume.

Resume follows a strict safety order to prevent infinite crash loops:

1. **Advance the interrupted core first** -- only the core that was actively testing (`in_test` flag persisted to DB) is treated as a failure and backed off *before* touching the SMU. Queued cores keep their exact state. This ensures the crashing offset is never re-applied, and queued cores don't skip untested offsets.
2. **Restore all baselines** -- after all interrupted cores have been advanced, the tuner restores all cores to their baseline offsets from the database (not from SMU, which resets to zero on reboot). This returns the CPU to its known-stable BIOS configuration.
3. **Continue with isolation** -- the tuner picks the next core and applies isolation (baseline all others, test offset on target only).

This ordering is critical: if the system crashed because a CO offset was too aggressive, naively re-applying saved offsets on resume would re-apply that same crashing value, causing another crash on every boot attempt. By advancing first and restoring baselines, the tuner always moves past the dangerous value before re-engaging the hardware.

**Crash during validation:** if the system crashes during multi-core validation, the session is recoverable. On resume, the tuner detects that all cores are confirmed and re-enters validation from stage 1. Sessions in 'validating' status are included in the resume session picker alongside 'running' and 'paused' sessions.

**Pause during validation:** pausing during validation saves the session as 'paused'. On resume, the tuner detects the all-confirmed state and re-enters validation from stage 1 (validation stage progress is not persisted across pause/resume — stages are fast enough that restarting from S1 is acceptable).

**Per-core state machine** (phases are `TunerPhase` StrEnum — typos caught at import time):

```
NOT_STARTED → COARSE_SEARCH → FINE_SEARCH → SETTLED → CONFIRMING → CONFIRMED
                                    |                       |            |
                                    ↓                       ↓            ↓ (all cores)
                                 SETTLED              FAILED_CONFIRM   VALIDATION
                                                           |          S1 → S2 → S3 → DONE
                                                           ↓            ↑     |     |
                                                    BACKOFF (smart)     └─────┴─────┘
                                                    ├─ pre-confirm    (backoff, restart S1)
                                                    ├─ midpoint jump
                                                    ├─ binary search
                                                    └─ baseline floor
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
| Validate Duration | 300s | 30-3600s | Test duration per stage during multi-core validation |
| Max Confirm Retries | 2 | 0-5 | Retries before backing off from a value |
| Auto Validate | true | true/false | Automatically run 3-stage multi-core validation after all cores are individually confirmed |
| Backend | mprime | mprime/stress-ng/y-cruncher | Stress test backend for per-core testing (stressapptest is Memory tab only) |
| Mode | SSE | SSE/AVX/AVX2/AVX512 | Stress test instruction set |
| FFT Preset | SMALL | SMALL/MEDIUM/LARGE/HEAVY/ALL | FFT size preset (mprime) |
| Test Order | sequential | sequential/round_robin/weakest_first/ccd_alternating/ccd_round_robin | Core testing order (see below) |
| Backoff Pre-Confirm Multiplier | 2.0 | 1.0-5.0 | Pre-confirm duration = search_duration * this multiplier (quick filter for backoff) |
| Midpoint Jump Threshold | 3 | 1-10 | Consecutive pre-confirm failures before jumping to midpoint between failing offset and baseline |
| Stretch Threshold | 3.0% | 0-20% | Clock stretch failure threshold (0 = disabled, requires root) |
| Abort on Consecutive Failures | 0 | 0-10 | Abort if N cores fail at start_offset (0 = disabled) |
| Inherit Current CO | false | true/false | Read current SMU offsets as starting points (skip testing values already proven stable) |

Advanced parameters (Backoff Pre-Confirm Multiplier, Midpoint Jump Threshold) use sensible defaults and are not exposed in the UI.

**Test orderings:**

| Order | Strategy | Best for |
|---|---|---|
| sequential | Finish each core completely before moving to next | Simple, easy to follow progress |
| round_robin | One test per core per round, cycling through all | Partial results for all cores faster |
| weakest_first | Prioritize cores closest to confirmation | Finish off nearly-done cores first |
| ccd_alternating | Alternate between CCDs, prioritize the CCD with fewest confirmed | Balanced thermal coverage across CCDs |
| ccd_round_robin | Round-robin within each CCD, alternating CCDs | Best thermal profile — each core gets cool-down time while the other CCD is tested |

**Workflow:**

1. Go to the **Auto-Tuner** tab
2. Configure search parameters (defaults are good for most CPUs)
3. Click **Start Tuning** -- the tuner begins testing cores sequentially
4. Watch the core status table update in real-time: phase, current offset, best offset, test count, last result
5. When all cores are individually confirmed, the tuner automatically enters multi-core validation (3 stages — status label shows "VALIDATING S1/S2/S3" with progress)
6. If validation fails, the most aggressive core is backed off and validation restarts -- this continues until the profile is stable or no further backoff is possible
7. When validation passes, the confirmed CO profile is applied to the SMU and the session completes
8. Use **Export Profile** to save the results as JSON, or use **Load to CO Tab** in the History tab to load confirmed offsets into the Curve Optimizer spinboxes for review before applying to hardware
9. Completed (and interrupted) sessions appear in the **History** tab's **Tuner Sessions** view -- click any session to review per-core state details and the full test log; sessions with confirmed cores show a "Load to CO Tab" button
10. **Resume** shows a session picker dialog when multiple paused/interrupted sessions exist

**Data persistence:** every tuner session is permanently saved in SQLite -- the config parameters, per-core state machine progress, and every individual test result (offset, phase, pass/fail, duration, error). Starting a new session does not delete old ones. **Load Defaults** only resets the config spinboxes to factory defaults; it does not affect any historical data or in-progress sessions. Action buttons (Start, Abort, Pause, Resume, Validate, Export) gray out when not applicable to the current state. During an active tuner session, the **Curve Optimizer tab** is locked (all Apply buttons, Reset, spinboxes disabled) to prevent SMU conflicts, and the manual **Start Test** button is disabled. Config spinboxes are also disabled during a run — the engine uses a snapshot of the config taken at start time, so changing UI values mid-run has no effect.

**Tips:**

- **mprime with Small FFTs and SSE mode** is the gold standard for CO testing -- it produces the highest single-core clocks and the most sensitive error detection
- The default `max_offset` of -50 is appropriate for Zen 4. For Zen 5, you can push to -60. For Zen 3/3D, the CPU generation's range (-30) is enforced automatically.
- **Sequential test order** (default) finishes one core completely before moving to the next. Use **round robin** if you want partial results for all cores faster.
- A typical 16-core run with default settings takes roughly 2-4 hours depending on how aggressive each core can go, plus ~80 minutes for 3-stage validation (16 cores x 300s for stage 1, plus stage 2 and 3).
- If many cores fail at the starting offset, enable **abort on consecutive failures** (e.g., 3) to stop early -- this usually means BIOS PBO settings need adjustment first.
- **Multi-core validation** is the key differentiator from manual testing -- a core that passes in isolation may fail when all cores draw power simultaneously. The 3-stage validation catches these power delivery and voltage transient failures that per-core testing misses.
- If validation keeps backing off cores and restarting, your VRM or power delivery may not support the aggregate offset profile. Consider reducing `max_offset` or testing with fewer cores.

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

The core sidebar shows different states depending on the execution mode:

**Normal stress test:**
- **Blue (testing)**: the core currently running the stress test (1 core at a time)
- **Green (passed)**: the core completed the stress test with no errors detected
- **Red (failed)**: the core produced an error during the stress test
- **Gray (pending)**: the core has not been tested yet in this cycle

**Auto-tuner:**
- **Blue (testing)**: the single core currently running mprime
- **Queued**: cores waiting for their turn, showing their current phase and CO offset
- **Green (confirmed)**: the core has a confirmed CO offset (shown in the sidebar)
- **Amber (backoff)**: the core is in the smart backoff phase, searching for a stable offset
- **Red (failed)**: the core failed and cannot be tuned further
- **Gray (pending)**: the core has not started tuning yet

**Memory stress:**
- **Purple (mem stress)**: all cores shown simultaneously during memory stress testing

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
      __init__.py            # Backend auto-registry (register_backend decorator, get_backend factory)
      base.py                # Abstract backend interface, shared constants (KILLED_BY_US_CODES, CRASH_SIGNALS)
      mprime.py              # Prime95 CLI backend (local.txt/prime.txt generation, output parsing)
      stress_ng.py           # stress-ng backend (cpu-method selection, verification)
      ycruncher.py           # y-cruncher backend (component stress mode)
      stressapptest.py       # stressapptest backend (memory-intensive stress)
  smu/
    commands.py              # SMU command IDs per CPU generation (Zen 1–5), encoding_scheme dispatch, command set aliasing
    driver.py                # ryzen_smu sysfs interface (CO, PBO limits, boost, scalar, system state)
    pmtable.py               # Version-aware PM table parsing: FCLK/UCLK/MCLK, voltages, ratio computation (Zen 2–5 offset registry)
  monitor/
    hwmon.py                 # k10temp/zenpower/zenpower5/coretemp: Tctl, Tdie, Tccd temps, Vcore, Vsoc; Super I/O fallback (Nuvoton/ITE) for Zen 5 Vcore
    cpu_usage.py             # Per-logical-CPU usage % from /proc/stat (delta-based)
    frequency.py             # Per-core frequency monitoring (sysfs cpufreq), actual + boost ceiling
    memory.py                # DIMM info (dmidecode), SPD5118 hwmon temps, DDR5 SPD EEPROM timing decode
    power.py                 # Package power (RAPL sysfs preferred, hwmon zenpower/zenpower5/k10temp fallback)
    msr.py                   # MSR reader (root): APERF/MPERF clock stretch, per-core RAPL power
  history/
    db.py                    # SQLite WAL-mode database: migration registry (v1-v8), SCHEMA_VERSION constant, run/context/tuner tables
    context.py               # Tuning context detection (BIOS version + CO snapshot grouping)
    logger.py                # TestRunLogger: connects worker signals to DB writes
    export.py                # JSON/CSV export of test results and tuner sessions
  config/
    settings.py              # JSON settings and test profile persistence (~/.config/corecycler/)
  tuner/
    __init__.py              # Package re-exports
    config.py                # TunerConfig dataclass (14 search parameters with defaults)
    state.py                 # TunerPhase StrEnum, CoreState and TunerSession dataclasses
    persistence.py           # SQLite operations: session CRUD, core state upsert, test log
    engine.py                # TunerEngine orchestrator: state machine, core scheduling, crash recovery, 3-stage validation
  gui/
    main_window.py           # Main window: toolbar, tabs, test worker thread, profile management
    config_tab.py            # Test configuration UI (backend, mode, FFT, timing, presets, safety)
    results_tab.py           # Per-core results table and summary
    monitor_tab.py           # Live temperature, voltage, frequency charts
    smu_tab.py               # Curve Optimizer read/write interface with backup/restore and dry-run
    tuner_tab.py             # Auto-Tuner UI: config panel, core status table, test log, controls
    memory_tab.py            # Memory tab: PM table clocks/voltages, SPD timings, DIMM info, temps, stress test
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
  test_monitor.py            # Hardware monitoring (hwmon, frequency, power, Super I/O fallback)
  test_msr.py                # MSR reader: clock stretch, per-core power, availability
  test_pmtable.py            # PM table version dispatch, clock/voltage parsing, ratio computation
  test_memory_monitor.py     # SPD timing decode, EEPROM discovery, MemoryTab behavioral tests
  test_ui_consistency.py     # CoreGridWidget telemetry, MonitorTab staleness, poll interval
  test_history_db.py         # SQLite history database: schema, migrations, queries
  test_history_context.py    # Tuning context detection and grouping
  test_history_logger.py     # TestRunLogger integration
  test_history_export.py     # JSON/CSV export
  test_tuner_config.py       # TunerConfig defaults, JSON roundtrip, CO range clamping
  test_tuner_persistence.py  # Session CRUD, core state upsert, test log, schema migration
  test_tuner_engine.py       # State machine transitions, crash recovery, core scheduling
  test_tuner_tab.py          # Auto-Tuner GUI widget tests
nix/
  module.nix               # NixOS module: services.corecycler options, all kernel modules, device access
  ryzen-smu.nix            # ryzen_smu kernel module derivation (GCC + Clang/LTO auto-detect)
  zenpower.nix             # zenpower5 kernel module derivation (GCC + Clang/LTO auto-detect)
  it87.nix                 # it87 ITE Super I/O kernel module derivation (GCC + Clang/LTO auto-detect)
flake.nix                  # Nix flake: packages (default/full), nixosModules.default, devShell
pyproject.toml             # Python project metadata, entry point
assets/
  icon.svg                 # Application icon (gear with cycling arrows)
  corecycler.desktop     # XDG desktop entry
  arrow-*.svg              # Qt widget arrows for dark theme
.github/
  workflows/
    update-flake.yml       # Weekly auto-update of flake inputs (nixpkgs, backends)
```

The stress test runs in a `QThread` worker. The scheduler pins each stress process to both SMT siblings of the physical core being tested using `taskset` (e.g., `taskset -c 0,16`), with the backend configured for 2 threads to fully utilize the core. It monitors for MCE events during both stress and idle phases, parses backend output for computation errors, and emits Qt signals for GUI updates. Processes are launched in their own process group for clean teardown.

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

When adding a new stress test backend:

1. Create `src/engine/backends/<name>.py` — subclass `StressBackend` from `base.py`
2. Implement `is_available`, `get_command`, `parse_output`, `get_supported_modes`
3. Add `@register_backend("display-name")` decorator — the GUI discovers it automatically

No GUI files need editing. The backend registry auto-populates combo boxes and factory lookups.

## Driver and Kernel Module Sources

CoreCycler integrates with several Linux kernel drivers for hardware monitoring and SMU access. Below are the upstream sources for all supported drivers:

### SMU Access

| Driver | Source | Description |
|---|---|---|
| ryzen_smu (amkillam fork) | [github.com/amkillam/ryzen_smu](https://github.com/amkillam/ryzen_smu) | Zen 1–5 SMU access — CO, PBO limits, boost override, PM table. Fork with Zen 5 support |
| ryzen_smu (upstream) | [github.com/leogx9r/ryzen_smu](https://github.com/leogx9r/ryzen_smu) | Original ryzen_smu (Zen 1–4 only) |

### CPU Temperature and Voltage

| Driver | Source | Description |
|---|---|---|
| zenpower5 | [github.com/mattkeenan/zenpower5](https://github.com/mattkeenan/zenpower5) | Zen 5 hwmon: Tctl/Tdie/Tccd temps + RAPL package power. SVI3 voltage not available |
| zenpower3 | [github.com/Ta180m/zenpower3](https://github.com/Ta180m/zenpower3) | Zen 1–4 hwmon: temps + SVI2 voltage/current |
| zenpower (original) | [github.com/ocerman/zenpower](https://github.com/ocerman/zenpower) | Zen 1–2 hwmon: temps + SVI2 voltage |
| k10temp | [kernel.org (in-tree)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/hwmon/k10temp.c) | In-tree AMD hwmon driver — Tctl/Tdie/Tccd temps. No voltage (requires zenpower for SVI2) |
| coretemp | [kernel.org (in-tree)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/hwmon/coretemp.c) | In-tree Intel hwmon driver — per-core/package temperatures |

### Super I/O (Motherboard Voltage Fallback)

| Driver | Source | Supported Chips | Common Boards |
|---|---|---|---|
| nct6775 | [kernel.org (in-tree)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/hwmon/nct6775-core.c) | NCT6775–NCT6799 | ASUS, MSI, ASRock AM5/AM4 |
| it87 | [kernel.org (in-tree)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/hwmon/it87.c) | IT8625–IT8772 | Gigabyte AM5/AM4 |

Super I/O chips provide analog Vcore (in0_input) from the voltage regulator — world-readable, no root needed. Used as automatic fallback on Zen 5 where SVI3 voltage is unsupported.

### Related Tools

| Tool | Source | Description |
|---|---|---|
| lm-sensors | [github.com/lm-sensors/lm-sensors](https://github.com/lm-sensors/lm-sensors) | `sensors` command, `sensors-detect` for finding hwmon drivers |
| ZenStates-Core | [github.com/irusanov/ZenStates-Core](https://github.com/irusanov/ZenStates-Core) | Reference for SMU command IDs across AMD generations |

## Acknowledgments

- [CoreCycler](https://github.com/sp00n/corecycler) by sp00n -- the original Windows per-core stress test cycler that inspired this project
- [CoreCycler-GUI](https://github.com/LucidLuxxx/CoreCycler-GUI) by LucidLuxxx -- Windows GUI for CoreCycler
- [ryzen_smu](https://github.com/leogx9r/ryzen_smu) by leogx9r and the [amkillam fork](https://github.com/amkillam/ryzen_smu) -- Linux kernel module for AMD SMU access
- [zenpower5](https://github.com/mattkeenan/zenpower5) by mattkeenan -- Zen 5 hwmon driver with RAPL power support
- [zenpower3](https://github.com/Ta180m/zenpower3) by Ta180m -- Zen 1-4 hwmon driver with SVI2 voltage
- [ZenStates-Core](https://github.com/irusanov/ZenStates-Core) by irusanov -- reference for SMU command IDs across generations

<!-- BEGIN generated:options -->
## Options

This module declares options under `services.corecycler`. See [`nix/module.nix`](nix/module.nix) for all available options.
<!-- END generated:options -->

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for details.

<!-- BEGIN generated:footer -->
---

*Maintained as part of the [Daaboulex](https://github.com/Daaboulex) NixOS ecosystem.*
<!-- END generated:footer -->
