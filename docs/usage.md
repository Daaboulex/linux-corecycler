<!-- markdownlint-disable MD013 -->

# Usage guide

## Quick start

1. Launch CoreCycler (`corecycler` or `python src/corecycler/main.py`). The Curve
   Optimizer needs SMU access: grant it to your user once
   ([Device access](installation.md#device-access)) or launch with `sudo`. Clock stretch
   and per-core power read model-specific registers, which need `sudo` today. Without
   access it runs with reduced telemetry and a status-bar warning.
2. The CPU topology (cores, CCDs, SMT, X3D) is detected automatically.
3. In the Configuration tab, choose the test program. Use stress-ng if mprime is not
   installed. If a program is reported as not found, see
   [Where CoreCycler looks for a backend](installation.md#where-corecycler-looks-for-a-backend).
4. The default preset is **Standard** (each core 10 min, 1 round); adjust or pick another.
5. Click **Start Test** and watch the core grid: each core lights up as it is tested,
   turning green (passed) or red (failed). The Results tab shows per-core detail.

### Presets

| Preset | Time per core | Rounds | Changing load | Idle watch after each core | Pause between cores | For |
|---|---|---|---|---|---|---|
| Quick check | 2 min | 1 | No | 0 | 0 | Finds clear errors fast and misses rare ones |
| Standard | 10 min | 1 | No | 0 | 0 | A first check after changing a value |
| Thorough | 30 min | 2 | No | 0 | 5 s | Errors that show only now and then |
| Everything | 20 min | 3 | Yes | 1 min | 10 s | Load changes and idle time, closest to daily use |
| Your own settings | yours | yours | yours | yours | yours | Anything else |

The tab shows the preset whose five values match the settings below it, and **Your own
settings** when none does; the test program, temperature limit, stop at the first error
and the cores are not part of a preset. **Cores to test** has one box per core: every
ticked core is tested, and Start refuses when none is ticked.

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
6. **Extended validation**: switch to Thorough or Everything -- the changing-load and
   idle tests catch instability sustained load misses.
7. **Test other instruction sets**: SSE produces the highest clocks and is most
   sensitive; AVX2 exercises different execution units and can expose other failures.

The **Auto-Tuner** (below) automates this whole loop via runtime SMU writes, with no
per-step reboots.

## Curve-Tuner tab

Reads and writes each core's Curve Optimizer value and the Curve Shaper at runtime
through `ryzen_smu`. The Curve Optimizer table shows the value **Now on the CPU** and a
**Value to set** per core. **Set changed** writes each core whose Value to set differs
from Now on the CPU (an unreadable core counts as different) and **Set all to 0** writes
0 to every core, each after a confirmation that names what it writes. While the tab is
shown it reads the CPU every 5 s into Now on the CPU and the line under the Curve Shaper
grid, and leaves the values being edited alone; **Fill from CPU** copies what is on the
CPU into them. **Remember current values** keeps every core's value until the app closes
and **Put remembered values back** writes them again. **Save values to file** and **Load
values from file** use JSON, the same values History loads from a completed tuning
session.

The **Curve Shaper** grid holds the margin of each of its five frequency bands, Min to
Max, at -5 C, 50 C and 90 C, filled from the CPU at start, with the read or its reason
below it.
**Write Shaper** writes the grid and **Shaper to 0** writes every margin 0, each after a
confirmation, and both read it back. The Curve Shaper applies to every core and stacks
on each core's Curve Optimizer value; only Ryzen 9000 (Granite Ridge) has it here.
`corecycler shaper` prints it and `corecycler shaper --set` writes it from a terminal.

All values written here are **volatile** and reset on reboot; set them in BIOS to make
them permanent. For Zen 2 the tab says "This CPU generation has no Curve Optimizer".
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
   CPUs, from 0 toward the deepest offset to try (default -50, never below the CPU's
   CO range) in coarse steps of 5, then steps of 1. A value is proven after 10 passes
   taken in rounds with the other cores, 15 s apart; a failure sets the core's fail
   bound and backs it off.
2. **Search, two threads per core**: when every selected core has two online CPUs, the
   same proof with both threads of the core and 6 passes, from the one-thread value.
3. **Each core alone** (LIVE1): every value live at once, each core tested alone again.
4. **Hot all-core** (STEADY): a hot all-core load on every selected CPU. A failure that
   names no core is narrowed by CCD and by half until one core is convicted; it backs
   off one step.
5. **Changing loads** (TRANSIENT): burst, warm and idle segments until an hour passes
   without a failure.
6. **Tighten** (TIGHTEN): each core tries one step tighter again while the others hold.
7. **Endurance** (ENDURANCE, off by default): hot, burst, warm and idle in a cycle until
   three hours pass clean.
8. **Memory** (MEMORY, on by default): stressapptest on each selected core, skipped with
   the reason when memory is short.
9. **Idle watch** (SOAK, on by default): 30 minutes without load, watching the kernel
   log.
10. **Finishing** (FINALIZE): when a value had to back off after LIVE1, the pass runs
    again from LIVE1 without TIGHTEN; otherwise the session completes with its values.

The tab names each stage in words; the names in brackets are what the session's events
and the command line print. Every length here is a setting, fixed for the session:
the changing-load, tighten and endurance stages repeat until their clean time is
reached, and a cool-down after a heat stop waits until the CPU is cool, within its
shortest and longest time.

### Controls

**Start** begins a session with the settings shown. **Pause** lets a one-core test
finish and stops an all-core test at once, which runs again from its start on resume;
**Stop** stops now; both put every selected core back to its baseline and keep the
session resumable. **Abort** (with a confirmation) puts every selected core back to its
baseline and stops the session as aborted, which Resume can still take up. **Resume**
goes on with the newest session, **Validate** proves a completed session's values again,
and **Continue** and **Restart** answer a setup change (below). The tab follows the newest session through the history database,
so a driver started from a terminal shows there too and its controls work on it.

The same actions run headless: `corecycler tune` (with `--config FILE` or `--cores`),
`resume`, `validate`, `continue` and `restart` take a session id, `corecycler status`
lists the sessions, and `corecycler --help` lists the flags and exit codes.

### Settings

The tab shows the cores, the core order (alternate across CCDs, one after another or
random), the deepest offset to try, the temperature limit and the memory test, idle watch
and endurance stages; **Advanced settings** shows every other setting in four groups
(the search, the all-core stages, temperature, and what happens when a test goes wrong).
Every setting has its unit to the right of its box and a line saying what it does; a
duration also reads in minutes or hours. Start refuses a setting outside its range and
names it. When a selected core already runs a CO value, set by the BIOS or written since
boot, Start lists those values and asks before it starts over them; the tuner searches
every core from 0, and a reboot brings the BIOS values back. Start also asks, naming the
values then and now, when a selected core reads another CO than when the newest session
started, even when every core reads 0. From the command line,
`tune` and `restart` take `--acknowledge` for the same answer.

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
- **One writer**: the driver holds a lock for its whole run and the Curve-Tuner tab
  takes it for each write, so the two never write at once.
- **Temperature**: above the thermal limit, or the CPU's Tjmax when that is lower, the
  test stops and the tuner cools down and retries the same value; after three such
  retries in a row the next thermal stop pauses. No readable temperature sensor refuses
  the run.
- **Other schedulers**: a stress tool whose nice value or scheduling policy differs from
  what it was started with (ananicy-cpp renices programs by name) is not a verdict: the
  test is repeated, and if it keeps happening the session pauses naming the program and
  how to stop it. A sched_ext scheduler that runs when the tab is about to start tests
  (Start, Resume, Validate, Continue, Restart) is named first, with Start anyway or
  Cancel. When a session ends, a scheduling service
  (ananicy-cpp, scx_loader, scx) that is enabled but stopped is named with the command
  that starts it again. Manual stress runs judge a renice the same way.

`corecycler --auto-resume [seconds]` (the NixOS autostart entry passes it) waits, then
resumes the one session a crash interrupted, trying three times a minute apart while the
driver refuses (the SMU driver not loaded yet, for example). A paused session is your
decision and is never resumed by itself, and with two interrupted sessions neither is.

## Test programs: instruction set and FFT size

The Configuration tab offers each program's own instruction sets (SSE, AVX, AVX2,
AVX-512), and the FFT size only for mprime, each size named with its range. They mean
different things per program:

- **mprime** -- the instruction set selects the torture-test instructions and the FFT
  size the range of FFTs. `SSE` with Small FFTs is the most sensitive, highest-boost
  combination.
- **stress-ng** -- the instruction set selects the CPU stressor method.
- **stressapptest** -- SSE only.
- **y-cruncher** -- y-cruncher has no instruction-set switch: the instruction set is fixed
  by the per-microarchitecture binary it auto-selects for your CPU. The choice instead
  picks which of y-cruncher's component tests run:
  - `SSE` -- `BKT` only (scalar-integer Basecase/Karatsuba): the lowest-power,
    highest-boost, most CO-sensitive test.
  - `AVX` -- `BKT, BBP, SFTv4, SNT, SVT` (adds the AVX compute and small in-cache tests).
  - `AVX2` / `AVX-512` -- all algorithms, including the memory-bandwidth transforms
    (`FFTv4, N63, VT3`). Because y-cruncher auto-selects a supported binary, AVX-512
    runs the strongest instruction set your CPU actually has and never crashes on a
    non-AVX-512 chip.

## Recommended settings

| Scenario | Test program | Instruction set | FFT size | Preset |
|---|---|---|---|---|
| Quick screening | stress-ng | SSE | -- | Quick check |
| Initial CO tuning | mprime | SSE | Small | Standard |
| Thorough validation | mprime | SSE | Huge | Thorough |
| Comprehensive stability | mprime | SSE | All sizes | Everything |
| AVX2 validation | mprime | AVX2 | Heavy | Standard |

## History tab

**Show** picks the view: **Stress tests by setup** groups the manual runs by BIOS, CO
profile and PBO scalar, **All stress tests** lists them one by one, and **Tuning
sessions** lists the Auto-Tuner's sessions, newest first. A session's line names its
core order, deepest offset, passes to prove, BIOS and power state; its table shows each
core's phase, value, the value it failed at, its tests, its last result and its highest
clock. The log below lists the session's settings by the Auto-Tuner tab's names, every
test, the events, the boots it saw and each core's highest clock at every power-state
range. **Load these values into Curve-Tuner** fills the Curve-Tuner tab's Value
to set column from a completed session; nothing reaches the CPU until Set changed.

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
