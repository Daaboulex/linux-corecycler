<!-- markdownlint-disable MD013 -->

# Usage guide

## Quick start

1. Launch CoreCycler (`corecycler` or `python src/corecycler/main.py`). The Curve
   Optimizer needs SMU access: grant it to your user once
   ([Device access](installation.md#device-access)) or launch with `sudo`. Clock stretch
   and per-core power read model-specific registers, which need `sudo` today. Without
   access it runs with reduced telemetry and a status-bar warning.
2. The CPU topology (cores, CCDs, SMT, X3D) is detected automatically.
3. In the Configuration tab, select a backend. Use stress-ng if mprime is not installed.
   If a backend is reported as not found, see
   [Where CoreCycler looks for a backend](installation.md#where-corecycler-looks-for-a-backend).
4. The default preset is **Standard** (10 min/core, 1 cycle); adjust or pick another.
5. Click **Start Test** and watch the core grid: each core lights up as it is tested,
   turning green (passed) or red (failed). The Results tab shows per-core detail.

### Test mode presets

| Preset | Time/Core | Cycles | Variable Load | Idle Test | Use Case |
|---|---|---|---|---|---|
| Quick | 2 min | 1 | No | No | Fast screening, rough check |
| Standard | 10 min | 1 | No | No | Initial CO tuning |
| Thorough | 30 min | 2 | No | 5s between | Validation after tuning |
| Full Spectrum | 20 min | 3 | Yes | 60s + 10s between | Comprehensive stability proof |
| Custom | User-defined | User-defined | Optional | Optional | Fine-tuned testing |

## Finding optimal CO values (manual)

The manual workflow for tuning AMD PBO Curve Optimizer:

1. **Set conservative starting CO values in BIOS** (e.g. -15 all-core) -- your baseline.
2. **Run a screening pass**: mprime, SSE, Small FFTs, Standard preset
   (~`10 min x core_count`).
3. **Identify failing cores** -- those that cannot sustain -15 at full single-threaded
   boost.
4. **Reduce CO for failing cores** in BIOS (e.g. -15 to -10, or less); leave passing
   cores at -15.
5. **Repeat** until all cores pass.
6. **Extended validation**: switch to Thorough or Full Spectrum -- the variable-load and
   idle tests catch instability sustained load misses.
7. **Test other instruction sets**: SSE produces the highest clocks and is most
   sensitive; AVX2 exercises different execution units and can expose other failures.

The **Auto-Tuner** (below) automates this whole loop via runtime SMU writes, with no
per-step reboots.

## Curve Optimizer tab

Direct SMU access for reading and writing per-core CO offsets at runtime (requires
`ryzen_smu`). Reads all current offsets, per-core spinboxes, per-core and bulk Apply
(each with a confirmation dialog), Reset to 0, Backup/Restore within a session, Dry Run
(logs writes without touching hardware), and Save/Load CO Profile (JSON, shared with the
History tab's "Load to CO Tab").

All values written here are **volatile** and reset on reboot; set them in BIOS to make
them permanent. For Zen 2 (no Curve Optimizer) the tab shows "Connected (no CO support)".
No GUI or CLI path changes PBO limits, the boost limit or the PBO scalar.

## Auto-Tuner

The Auto-Tuner finds each core's most negative stable Curve Optimizer offset through
runtime SMU writes (requires `ryzen_smu`), then proves the whole set under all-core
loads. It runs as its own process, the tuner driver
(`python -m corecycler.tuner.live_hw`): the Auto-Tuner tab and the `corecycler` tuning
commands start it, and closing or crashing the GUI leaves a running session running.
Every test, CO write, sample and event lands in the history database. The tests use
y-cruncher, and the memory stage stressapptest; the tab asks for either when it is not
found.

### What a session does

1. **Search, one thread per core**: one core at a time under y-cruncher on one of its
   CPUs, from 0 toward `max_offset` (default -50, never below the CPU's CO range) in
   coarse steps of 5, then steps of 1. A value is proven after 10 passes taken in rounds
   with the other cores, 15 s apart; a failure sets the core's fail bound and backs it
   off.
2. **Search, two threads per core**: when every selected core has two online CPUs, the
   same proof with both threads of the core and 6 passes, from the one-thread value.
3. **LIVE1**: every value live at once, each core tested alone again.
4. **STEADY**: a hot all-core load on every selected CPU. A failure that names no core is
   bisected by CCD and by half until one core is convicted; it backs off one step.
5. **TRANSIENT**: bursts, warm and idle phases until an hour passes without a failure.
6. **TIGHTEN**: each core tries one step tighter again while the others hold.
7. **ENDURANCE** (off by default): hot, burst, warm and idle in a cycle until three hours
   pass clean.
8. **MEMORY** (on by default): stressapptest on each selected core, skipped with the
   reason when memory is short.
9. **SOAK** (on by default): 30 minutes without load, watching the kernel log.
10. **FINALIZE**: when a value had to back off after LIVE1, the pass runs again from
    LIVE1 without TIGHTEN; otherwise the session completes with its values.

### Controls

**Start** begins a session with the settings shown. **Pause** stops after the current
test, **Stop** stops now; both put every selected core back to its baseline and keep the
session resumable. **Abort** (with a confirmation) puts every selected core back to its
baseline and stops the session as aborted, which Resume can still take up. **Resume**
goes on with the newest session, **Validate** proves a completed session's values again,
and **Continue** and **Restart** answer a setup change (below). The tab follows the newest session through the history database,
so a driver started from a terminal shows there too and its controls work on it.

The same actions run headless: `corecycler tune` (with `--config FILE` or `--cores`),
`resume`, `validate`, `continue` and `restart` take a session id, `corecycler status`
lists the sessions, and `corecycler --help` lists the flags and exit codes.

### Settings

The tab shows the cores, the order (alternate across CCDs, sequential or random), the
max offset, the thermal limit and the memory, soak and endurance stages; **Advanced**
shows every other setting by its own name. Start refuses a setting outside its range and
names it. When the BIOS already sets a CO on a selected core, Start refuses unless
**Start over a CO the BIOS already sets** is ticked.

### Power state and clocks

A session records the power state at Start: the platform profile, governor, EPP, boost
and the scaling maximum frequency. A capped machine (power-saver holding every core at
4300 MHz, for example) runs, and the cap is the session's range: every result belongs to
it, and a session that changes range midway pauses until the power state is put back. A
later session at another range tests the cores there. Every test also records each core's
highest observed clock, sampled ten times a second; History shows each core's peak in a
session and its highest clock at every range seen so far.

### Crash safety

Hard crashes are expected during CO tuning; that is how each core's limit is found.

- **Write-ahead journal**: every CO value is recorded before it reaches the SMU and marked
  survived once a test completes with it. After a crash, Resume reads the journal, the
  kernel log of the boot that died and the stored test rows, and blames what was live at
  the time: the core gets a fail bound there and backs off. A core that crashes at 0 is
  unstable at stock, and the session pauses.
- **Quarantine**: three crash-resumes in a row with no test surviving between them put
  every selected core back to 0 and stop the session for good.
- **Setup changes**: the session records the BIOS, kernel, microcode and app build at
  Start. A resume or a Validate on a different setup pauses: put the setup back and
  resume, or **Continue** (carry the proven values into a new session under the new
  setup) or **Restart** (start over with the same settings).
- **Outside writes**: at resume and at the start of each stage every core's CO is read
  back. A value the session did not write pauses it, and so does a CO that cannot be read.
- **One writer**: the driver holds a lock for its whole run and the Curve Optimizer tab
  takes it for each write, so the two never write at once.
- **Temperature**: above the thermal limit, or the CPU's Tjmax when that is lower, the
  test stops and the tuner cools down and retries the same value; after three such
  retries in a row the next thermal stop pauses. No readable temperature sensor refuses
  the run.

`corecycler --auto-resume [seconds]` (the NixOS autostart entry passes it) waits, then
resumes the one session a crash interrupted, trying three times a minute apart while the
driver refuses (the SMU driver not loaded yet, for example). A paused session is your
decision and is never resumed by itself, and with two interrupted sessions neither is.

## Backends: Mode and FFT Preset

**Mode** (SSE/AVX/AVX2/AVX512) and **FFT Preset** mean different things per backend in
the Configuration tab:

- **mprime** -- Mode selects the torture-test instruction set and FFT Preset sets the FFT
  size range. `SSE` with Small FFTs is the most sensitive, highest-boost combination.
- **stress-ng** -- Mode selects the CPU stressor method; FFT Preset is ignored.
- **y-cruncher** -- y-cruncher has no instruction-set switch: the instruction set is fixed
  by the per-microarchitecture binary it auto-selects for your CPU. Mode instead chooses
  which of y-cruncher's component tests run (FFT Preset is ignored):
  - `SSE` -- `BKT` only (scalar-integer Basecase/Karatsuba): the lowest-power,
    highest-boost, most CO-sensitive test.
  - `AVX` -- `BKT, BBP, SFTv4, SNT, SVT` (adds the AVX compute and small in-cache tests).
  - `AVX2` / `AVX512` -- all algorithms, including the memory-bandwidth transforms
    (`FFTv4, N63, VT3`). Because y-cruncher auto-selects a supported binary, `AVX512` mode
    runs the strongest instruction set your CPU actually has and never crashes on a
    non-AVX512 chip.

## Recommended settings

| Scenario | Backend | Mode | FFT Preset | Preset |
|---|---|---|---|---|
| Quick screening | stress-ng | SSE | -- | Quick |
| Initial CO tuning | mprime | SSE | Small | Standard |
| Thorough validation | mprime | SSE | Huge | Thorough |
| Comprehensive stability | mprime | SSE | All | Full Spectrum |
| AVX2 validation | mprime | AVX2 | Heavy | Standard |

## Understanding results

Core grid colors -- **blue**: testing now; **green**: passed, or proven by the
Auto-Tuner; **red**: failed; **gray**: pending, or still searching; **purple**: memory
stress (all cores at once).

| Outcome | Meaning | Typical cause |
|---|---|---|
| MCE (`mce`) | The kernel logged a machine check naming the tested core, in any phase (stress, idle, load transition); the detail names the phase | CO too aggressive: voltage too low for the requested frequency |
| Computation (`computation`) | Stress test got a wrong result (rounding, sumout, mismatch) | CO too aggressive: subtle numerical instability |
| Crash (`crash`) | Stress process died of SIGILL, SIGBUS, SIGFPE or SIGSEGV | Core instability causing instruction faults |
| Thermal (`thermal`) | The temperature limit stopped the test | Cooling, not CO |
| Apparatus (`stall`, `killed`, `startup` and the rest) | The test itself could not be judged | Not a CO verdict: the Auto-Tuner retries the step |

**MCE errors are the most serious**: the CPU detected an actual hardware-level error, so
reduce that core's CO (make it less negative). **Computation errors** are the most common
CO-tuning failure. An MCE in the idle phase is a failure mode sustained-load tests miss
entirely.
