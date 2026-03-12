# Linux CoreCycler

Per-core CPU stress testing and AMD PBO Curve Optimizer tuning for Linux.

![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue)
![License GPL-3.0](https://img.shields.io/badge/License-GPL--3.0--or--later-green)
![Linux only](https://img.shields.io/badge/Platform-Linux-yellow)

## What Is This?

Linux CoreCycler is a Linux equivalent of [CoreCycler](https://github.com/sp00n/corecycler) (Windows) for AMD PBO Curve Optimizer tuning. It provides a graphical interface for running per-core stress tests and optionally reading/writing Curve Optimizer values via the AMD SMU (System Management Unit).

**What is Curve Optimizer?**
AMD Precision Boost Overdrive (PBO) Curve Optimizer (CO) lets you adjust the voltage-frequency curve on a per-core basis. Negative CO values reduce voltage at a given frequency, allowing the CPU to boost higher within its thermal and power limits. Each core in a processor is unique -- some can handle aggressive negative offsets (e.g., -30), while others become unstable past modest values (e.g., -10). Finding the right value for each core requires per-core testing.

**Why per-core testing matters:**
All-core stress tests (Prime95 with all threads, Cinebench, etc.) cannot reliably detect per-core instability. When all cores are loaded simultaneously, each core runs at lower boost clocks and voltages than it would under single-threaded load. A core that passes an all-core test at 5.0 GHz may crash when it boosts to 5.7 GHz under single-threaded load with an aggressive CO offset. Linux CoreCycler solves this by testing one core at a time at full single-threaded boost clocks, cycling through every core in sequence.

## Features

- **Per-core stress test cycling** with configurable time, iterations, and cycle count per core
- **Three stress test backends**: mprime (Prime95 CLI), stress-ng, and y-cruncher
- **X3D-aware CPU topology detection** -- identifies CCDs, V-Cache CCD (by L3 size comparison), and SMT siblings
- **Live hardware monitoring** -- CPU temperature (Tctl, Tdie, per-CCD Tccd), core voltage (Vcore, Vsoc), frequency, and power via hwmon/k10temp/zenpower
- **SMU integration** for runtime Curve Optimizer read/write via the ryzen_smu kernel module (Zen 3, Zen 3D, Zen 4, Zen 5)
- **MCE error detection** -- monitors Machine Check Exceptions via sysfs and dmesg during stress tests
- **Dark Qt6 GUI** with CCD-aware core grid showing real-time test status per core
- **Test profile save/load** -- export and import test configurations as JSON files
- **Safety features** -- thermal limit monitoring, process group cleanup on stop, confirmation dialogs for CO writes, volatile-only SMU writes (never touches BIOS)

## Screenshots

<!-- TODO: Add screenshots -->

## Supported Hardware

### Curve Optimizer (SMU) Support

| Generation | Example CPUs | CO Range | SMU Mailbox | Notes |
|---|---|---|---|---|
| Zen 3 (Vermeer) | 5600X, 5800X, 5900X, 5950X | -30 to 0 | MP1 | Full support |
| Zen 3D (Warhol) | 5800X3D | -30 to 0 | MP1 | V-Cache detection |
| Zen 4 (Raphael) | 7600X, 7700X, 7900X, 7950X | -50 to +10 | RSMU | Positive offsets supported |
| Zen 5 (Granite Ridge) | 9600X, 9700X, 9900X, 9950X | -60 to +10 | RSMU | Boost limit read/write |

SMU features (reading and writing CO values) require the [ryzen_smu](https://github.com/amkillam/ryzen_smu) kernel module (amkillam fork) and root or appropriate sysfs permissions.

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

### The Curve Optimizer tab is the only place that writes CO

The SMU tab provides explicit per-core spinboxes, per-core Apply buttons, and an "Apply All" bulk action. Each write operation is visible in the UI. The Reset All button requires a confirmation dialog. No CO values are written automatically or in the background.

### The BIOS-SMU interaction

The full lifecycle of CO values:

1. **Boot**: BIOS applies your configured PBO Curve Optimizer values (e.g., -20 all-core)
2. **Runtime (optional)**: Linux CoreCycler can override individual core CO values via SMU writes -- these override the BIOS values in the CPU's runtime state
3. **Reboot**: All SMU-written values are discarded; BIOS values are reapplied from step 1

If you have PBO values already set in BIOS, those are your baseline. The stress testing feature tests whether those values are stable under per-core load. The SMU tab lets you experiment with different values at runtime without rebooting.

### Process cleanup

Stress test processes are launched in their own process group (`setsid`). On stop, the scheduler sends SIGTERM to the entire process group, waits 3 seconds, then escalates to SIGKILL if needed. No zombie processes are left behind. Closing the application window while a test is running prompts for confirmation and performs the same cleanup.

### Thermal safety

The hardware monitor continuously reads CPU temperatures from hwmon (k10temp/zenpower). Temperature data is displayed in real time in the Monitor tab.

## Installation

### NixOS (recommended)

Add the flake input to your `flake.nix`:

```nix
{
  inputs = {
    linux-corecycler.url = "github:Daaboulex/linux-corecycler";
    # ...
  };
}
```

Then add the package to your system or Home Manager configuration:

```nix
# In your nixosConfiguration or home-manager module:
environment.systemPackages = [
  inputs.linux-corecycler.packages.${pkgs.system}.default
];

# Or with Home Manager:
home.packages = [
  inputs.linux-corecycler.packages.${pkgs.system}.default
];
```

Alternatively, use it as an overlay:

```nix
nixpkgs.overlays = [
  (final: prev: {
    linux-corecycler = inputs.linux-corecycler.packages.${prev.system}.default;
  })
];
```

### Nix (any distro)

Run directly without installing:

```bash
nix run github:Daaboulex/linux-corecycler
```

### From source

```bash
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
nix develop
python src/main.py
```

Or without Nix, install dependencies manually and run:

```bash
git clone https://github.com/Daaboulex/linux-corecycler.git
cd linux-corecycler
pip install PySide6 psutil
python src/main.py
```

### Dependencies

**Required:**
- Python 3.12+
- PySide6 >= 6.7 (Qt6 bindings)
- psutil >= 6.0

**Optional stress test backends (at least one recommended):**
- **mprime** -- Prime95 command-line version (most sensitive for CO testing)
- **stress-ng** -- general-purpose stress tester (often pre-installed on Linux)
- **y-cruncher** -- multi-algorithm computational stress test

**Optional for Curve Optimizer features:**
- **ryzen_smu** kernel module ([amkillam fork](https://github.com/amkillam/ryzen_smu)) -- required for reading/writing CO values via SMU

### Installing ryzen_smu

The amkillam fork of ryzen_smu supports Zen 3 through Zen 5 processors:

```bash
git clone https://github.com/amkillam/ryzen_smu.git
cd ryzen_smu
make
sudo make install   # installs as a DKMS module
sudo modprobe ryzen_smu
```

On NixOS, you can build it as a kernel module:

```nix
boot.extraModulePackages = [
  (config.boot.kernelPackages.callPackage /path/to/ryzen_smu/package.nix {})
];
boot.kernelModules = [ "ryzen_smu" ];
```

Verify the module is loaded:

```bash
ls /sys/kernel/ryzen_smu_drv/
# Expected: mp1_smu_cmd  rsmu_cmd  smu_args  version  ...
```

Note: reading and writing CO values through sysfs requires root access or appropriate file permissions on the `/sys/kernel/ryzen_smu_drv/` entries.

### Installing mprime

mprime is the Linux command-line build of Prime95. It is unfree software distributed by Mersenne Research.

1. Download from [mersenne.org/download](https://www.mersenne.org/download/)
2. Extract and place the `mprime` binary somewhere on your `PATH` (e.g., `/usr/local/bin/mprime` or `~/.local/bin/mprime`)
3. Verify: `mprime -v`

## Usage Tutorial

### Quick Start

1. Launch Linux CoreCycler (`linux-corecycler` or `python src/main.py`)
2. The application detects your CPU topology automatically (cores, CCDs, SMT, X3D)
3. In the Configuration tab, select a backend -- use stress-ng if mprime is not installed
4. Set the time per core (default is 6 minutes; 2 minutes is fine for a quick check)
5. Click **Start Test**
6. Watch the core grid on the left -- each core lights up as it is tested, turning green (passed) or red (failed)
7. The Results tab shows detailed per-core status, elapsed time, and error messages

### Finding Optimal CO Values (Step by Step)

This is the primary workflow for tuning AMD PBO Curve Optimizer:

1. **Set conservative starting CO values in BIOS.** Start with a modest negative offset for all cores (e.g., -15 all-core). This is your baseline.

2. **Run a screening pass.** Launch Linux CoreCycler with mprime, SSE mode, Small FFTs, 10 minutes per core, 1 cycle. This takes roughly `10 min * core_count` to complete.

3. **Identify failing cores.** Note which cores fail. These are the cores that cannot sustain the -15 offset at full single-threaded boost clocks.

4. **Reduce CO for failing cores.** Go back to BIOS and reduce the CO for each failing core individually (e.g., -15 to -10, or -15 to -5 for particularly weak cores). Leave passing cores at -15.

5. **Repeat.** Run the test again. Continue adjusting until all cores pass.

6. **Extended validation.** Once all cores pass at 10 minutes, run a longer test: 30-60 minutes per core, 2-3 cycles, to catch intermittent instability.

7. **Test different instruction sets.** SSE mode produces the highest single-core boost clocks and is the most sensitive test. After SSE passes, run AVX2 mode as well -- AVX2 workloads use different execution units and can expose different failure modes.

### Using the Curve Optimizer Tab

The Curve Optimizer tab provides direct SMU access for reading and writing per-core CO offsets at runtime. This requires the ryzen_smu kernel module.

- **Read All CO**: reads the current runtime CO offset for every core from the SMU
- **Per-core spinboxes**: adjust the desired CO value for each core independently
- **Apply (per-core)**: writes the spinbox value to the SMU for that single core
- **Apply All New Values**: writes all spinbox values to the SMU at once
- **Reset All to 0**: resets all core CO offsets to 0 (requires confirmation)

Remember: all values written here are **volatile** and reset on reboot. To make changes permanent, set them in your BIOS. This tab is useful for rapid iteration -- adjust CO, run a stress test, adjust again -- without rebooting between each change.

### Recommended Test Settings

| Scenario | Backend | Mode | FFT Preset | Time/Core | Cycles | Notes |
|---|---|---|---|---|---|---|
| Quick screening | stress-ng | SSE | -- | 2 min | 1 | Fast check, lower sensitivity |
| Initial CO tuning | mprime | SSE | Small | 10 min | 1 | Best starting point |
| Thorough validation | mprime | SSE | Huge | 30 min | 2 | Catches intermittent errors |
| Extended stability | mprime | SSE | All | 60 min | 3 | Final validation before daily use |
| AVX2 validation | mprime | AVX2 | Heavy | 15 min | 1 | Different execution units |

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
| Timeout | Stress test process stopped responding | Medium | May indicate a hang caused by instability; may also be benign |
| Crash | Stress test process terminated unexpectedly | High | Core instability causing instruction faults |

**MCE errors are the most serious.** They indicate that the CPU detected an actual hardware-level error in computation. If you see MCE errors on a core, that core's CO value must be reduced (made less negative).

**Computation errors** (rounding errors, illegal sumout in mprime) mean the core produced an incorrect result. This is the most common failure mode during CO tuning and indicates the offset is too aggressive for that core.

## Architecture

```
src/
  main.py                    # Application entry point, dark theme, Qt setup
  engine/
    topology.py              # CPU topology: cores, CCDs, L3 cache, X3D V-Cache detection
    scheduler.py             # Per-core test cycling, process management, error callbacks
    detector.py              # MCE detection via sysfs machinecheck + dmesg parsing
    backends/
      base.py                # Abstract backend interface (StressBackend, StressConfig, StressResult)
      mprime.py              # Prime95 CLI backend (local.txt/prime.txt generation, output parsing)
      stress_ng.py           # stress-ng backend (cpu-method selection, verification)
      ycruncher.py           # y-cruncher backend (component stress mode)
  smu/
    commands.py              # SMU command IDs per CPU generation, CO argument encoding/decoding
    driver.py                # ryzen_smu sysfs interface (read/write CO offsets, boost limits)
    pmtable.py               # PM table reading
  monitor/
    hwmon.py                 # k10temp/zenpower: Tctl, Tdie, Tccd temps, Vcore, Vsoc voltages
    frequency.py             # Per-core frequency monitoring
    power.py                 # Power consumption monitoring
  config/
    settings.py              # JSON settings and test profile persistence (~/.config/linux-corecycler/)
  gui/
    main_window.py           # Main window: toolbar, tabs, test worker thread, profile management
    config_tab.py            # Test configuration UI (backend, mode, FFT, timing)
    results_tab.py           # Per-core results table and summary
    monitor_tab.py           # Live temperature, voltage, frequency charts
    smu_tab.py               # Curve Optimizer read/write interface
    widgets/
      core_grid.py           # CCD-aware visual core grid with per-core status coloring
      charts.py              # Real-time monitoring charts
```

The stress test runs in a `QThread` worker. The scheduler pins each stress process to a single logical CPU using `taskset`, monitors for MCE events during execution, parses backend output for computation errors, and emits Qt signals for GUI updates. Processes are launched in their own process group for clean teardown.

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

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) for details.
