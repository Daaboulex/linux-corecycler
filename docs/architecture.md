<!-- markdownlint-disable MD013 -->

# Architecture

CoreCycler is a PySide6 (Qt6) desktop app. Stress tests run in a `QThread` worker; the
scheduler pins each stress process to both SMT siblings of the physical core under test
(`taskset -c 0,16`, backend configured for 2 threads), monitors MCE during stress and
idle phases, parses backend output, and emits Qt signals for the GUI. Processes run in
their own process group for clean teardown.

## Source layout

```text
src/
  main.py            Entry point, dark theme, Qt setup
  engine/            Stress execution
    topology.py        CPU topology: cores, CCDs, L3 cache, X3D V-Cache detection
    scheduler.py       Per-core cycling, variable load, idle tests, process management
    detector.py        MCE detection via sysfs machinecheck + dmesg
    backends/          Auto-registered stress backends (mprime, stress_ng, ycruncher, stressapptest)
  smu/               AMD SMU access (ryzen_smu)
    commands.py        Per-generation command IDs, encoding-scheme dispatch, harvested-core slots
    driver.py          ryzen_smu sysfs: CO, PBO limits, boost, scalar, slot probing, system state
    pmtable.py         Version-aware PM table parsing: FCLK/UCLK/MCLK, voltages, ratio
  monitor/           Live hardware telemetry
    hwmon.py           k10temp/zenpower/coretemp temps + Vcore/Vsoc; Super I/O fallback (Zen 5)
    cpu_usage.py       Per-CPU usage from /proc/stat
    frequency.py       Per-core frequency (cpufreq), actual + boost ceiling
    memory.py          DIMM info (dmidecode), SPD5118 temps, DDR5 SPD timing decode
    power.py           Package power (RAPL sysfs, hwmon fallback)
    msr.py             MSR (root): APERF/MPERF clock stretch, per-core RAPL power
  history/           SQLite persistence (WAL): run/context/tuner tables, migration registry, JSON/CSV export
  tuner/             Automated PBO Curve Optimizer tuner
    config.py          TunerConfig dataclass (search parameters with defaults)
    state.py           TunerPhase StrEnum, CoreState / TunerSession dataclasses
    persistence.py     Session CRUD, core-state upsert, test log, CO write-ahead journal
    engine.py          TunerEngine: state machine, scheduling, crash recovery, staged validation (1-7)
  gui/               Qt tabs (config, results, monitor, smu, tuner, memory, history) + core grid widget
  config/            JSON settings and test-profile persistence (~/.config/corecycler/)
nix/                 NixOS module + kernel-module derivations (ryzen-smu, zenpower, it87)
tests/               Pytest suite (unit, property/Hypothesis fuzz, fault-injection, hermetic GUI)
```

The backend registry (`engine/backends/__init__.py`) auto-discovers backends via the
`@register_backend` decorator -- the GUI populates combo boxes and the factory from it,
so no GUI file changes when a backend is added.

## Development

```bash
nix develop          # or: pip install -e ".[dev]"
pytest               # run the suite
ruff check src && ruff format src
nix flake check      # build + every check (what CI runs)
```

### Adding a stress backend

1. Create `src/engine/backends/<name>.py`, subclassing `StressBackend` from `base.py`.
2. Implement `is_available`, `get_command`, `parse_output`, `get_supported_modes`.
3. Add the `@register_backend("display-name")` decorator -- it is discovered
   automatically; no GUI files need editing.

## Driver and kernel module sources

### SMU access

| Driver | Source | Notes |
|---|---|---|
| ryzen_smu (amkillam fork) | [amkillam/ryzen_smu](https://github.com/amkillam/ryzen_smu) | Zen 1-5 SMU: CO, PBO limits, boost, PM table |
| ryzen_smu (upstream) | [leogx9r/ryzen_smu](https://github.com/leogx9r/ryzen_smu) | Original (Zen 1-4 only) |

### CPU temperature and voltage

| Driver | Source | Notes |
|---|---|---|
| zenpower5 | [mattkeenan/zenpower5](https://github.com/mattkeenan/zenpower5) | Zen 5 hwmon: temps + RAPL power (SVI3 voltage unavailable) |
| zenpower3 | [Ta180m/zenpower3](https://github.com/Ta180m/zenpower3) | Zen 1-4 hwmon: temps + SVI2 voltage/current |
| k10temp | [kernel.org (in-tree)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/hwmon/k10temp.c) | In-tree AMD temps (no voltage) |
| coretemp | [kernel.org (in-tree)](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/tree/drivers/hwmon/coretemp.c) | In-tree Intel temps |

### Super I/O (motherboard Vcore fallback)

| Driver | Supported chips | Common boards |
|---|---|---|
| nct6683 (in-tree) | NCT6683/6686/6687 | Modern MSI (B550, B650, X570, X670) |
| nct6775 (in-tree) | NCT6775-NCT6799 | ASUS, MSI, ASRock AM5/AM4 |
| it87 (out-of-tree) | IT8625-IT8772 | Gigabyte AM5/AM4 |

Super I/O chips provide analog Vcore from the voltage regulator (world-readable, no root)
and are the automatic fallback on Zen 5 where SVI3 voltage is unsupported; the tool scans
input labels for the correct Vcore channel.

### Reference

| Tool | Source | Use |
|---|---|---|
| lm-sensors | [lm-sensors/lm-sensors](https://github.com/lm-sensors/lm-sensors) | `sensors`, `sensors-detect` for finding hwmon drivers |
| ZenStates-Core | [irusanov/ZenStates-Core](https://github.com/irusanov/ZenStates-Core) | Reference for SMU command IDs across generations |

## Acknowledgments

- [CoreCycler](https://github.com/sp00n/corecycler) by sp00n -- the original Windows per-core stress cycler that inspired this project, and [CoreCycler-GUI](https://github.com/LucidLuxxx/CoreCycler-GUI) by LucidLuxxx.
- [ryzen_smu](https://github.com/leogx9r/ryzen_smu) by leogx9r and the [amkillam fork](https://github.com/amkillam/ryzen_smu) -- the kernel module for AMD SMU access.
- [zenpower5](https://github.com/mattkeenan/zenpower5) by mattkeenan and [zenpower3](https://github.com/Ta180m/zenpower3) by Ta180m -- AMD hwmon drivers.
- [ZenStates-Core](https://github.com/irusanov/ZenStates-Core) by irusanov -- reference for SMU command IDs.
