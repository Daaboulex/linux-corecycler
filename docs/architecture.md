<!-- markdownlint-disable MD013 -->

# Architecture

CoreCycler is a PySide6 (Qt6) desktop app. Stress tests run in a `QThread` worker. Every
stress process of a manual run or the Auto-Tuner goes through one supervised execution
engine (`engine/execution.py`), which launches it inside a kernel cgroup cpuset covering
the CPUs of the core under test (`systemd-run --scope -p AllowedCPUs=0,16`, payload
lifetime bound via `setpriv --pdeathsig`), a boundary the backend cannot widen with
`sched_setaffinity`. The engine monitors MCE during stress and idle phases, watches for
containment escapes, stalls, and thermal trips, parses backend output, and refuses to
launch when a backend's config is absent rather than letting the tool fall back to a
full-machine default. Processes run in their own process group for clean teardown. A
supervised launch with no available cgroup mechanism is refused, never run uncontained.

The Memory tab is the exception. `gui/memory_tab.py` starts its one memory stress process
with a plain `subprocess.Popen` in its own process group, outside any cgroup scope.

## Source layout

```text
src/corecycler/
  main.py            Entry point, Qt setup, sudo session handshake
  cli.py             Headless doctor/status/tune/resume commands (no display needed)
  notify.py          Desktop notifications via notify-send, best-effort
  engine/            Stress execution
    topology.py        CPU topology: cores, CCDs, L3 cache, X3D V-Cache detection
    scheduler.py       Per-core cycling, variable load, idle tests
    parallel.py        All-core simultaneous lanes (validation stages)
    execution.py       The one supervised loop: launch, poll, stall/thermal/MCE, verdicts
    containment.py     systemd cgroup scopes (AllowedCPUs) and the kernel cpuset record
    detector.py        MCE and kernel-error records from /dev/kmsg + the systemd journal
    memory_budget.py   One live memory budget shared by every stressapptest instance
    backends/          Stress backends (mprime, stress_ng, ycruncher, stressapptest), registered by load_all()
  smu/               AMD SMU access (ryzen_smu)
    commands.py        Per-generation command IDs, encoding-scheme dispatch, harvested-core slots
    driver.py          ryzen_smu sysfs: CO, PBO limits, boost, scalar, core-slot map, system state
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
    engine.py          TunerEngine: state machine, scheduling, crash recovery, staged validation (1-7);
                       the GUI and corecycler tune both run this
    machine/           The next tuner as a pure state machine: session.transition(state, event, world)
                       returns the new state and its effects (search, bisect, all-core, resume
                       attribution); no GUI or CLI path uses it yet
    orders.py          Core pick orders for machine/ (alternate, sequential, random)
    stages.py          y-cruncher load segments for machine/ (hot, warm, burst, idle)
    telemetry.py       Failure context for machine/ from a ring of clock samples
    live.py            LiveRig: drives machine/ with a JSON state store and a worker thread
    live_hw.py         Real-hardware rig for live.py: python -m corecycler.tuner.live_hw
                       (CO writes only with --write)
  gui/               Qt tabs (config, results, monitor, smu, tuner, memory, history), core grid,
                     and the missing-tool prompt that records a backend's path
    style.py           Display standards: the desktop draws the widgets, this holds only the
                       colors that carry meaning, resolved against its current color scheme
  config/            Settings and test profiles (~/.config/corecycler/), plus tools.py --
                     the one resolver for every external binary (PATH is not enough)
nix/                 NixOS module + kernel-module derivations (ryzen-smu, zenpower, it87)
tests/               Pytest suite (unit, property/Hypothesis fuzz, fault-injection, hermetic GUI)
```

The backend registry (`engine/backends/__init__.py`) holds every class marked with
`@register_backend`. A backend module registers only when `load_all()` imports it, and
`load_all()` names each module explicitly. The GUI fills its backend lists and the factory
from the registry, so no GUI file changes when a backend is added.

## Development

Dev environment, the test suite and gates, and how to add a stress backend:
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Driver and kernel module sources

Each kernel module, what it unlocks and where its source lives is listed under
[Kernel modules](installation.md#kernel-modules).

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
