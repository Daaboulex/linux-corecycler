<!-- markdownlint-disable MD013 -->

# Usage guide

## Quick start

1. Launch CoreCycler (`corecycler` or `python src/corecycler/main.py`). The Curve
   Optimizer needs SMU access: grant it to your user once
   ([Device access](installation.md#device-access)) or launch with `sudo`. Clock stretch
   reads the SMU's PM table, which device access covers; per-core power reads
   model-specific registers, which need `sudo` today. Without
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
Every test, CO write, sample and event lands in the history database. When the database
fails, the session pauses with every core it owns back at stock, and a tuner run the tab
started raises a dialog naming the failure. The tests use
y-cruncher, and the memory stage stressapptest; the tab asks for either when it is not
found.

### What a session does

1. **Search, one thread per core**: one core at a time under y-cruncher on one of its
   CPUs, from 0 toward the deepest offset to try (default -50, never below the CPU's
   CO range) in coarse steps of 5, then steps of 1. A new session starts each core
   instead at the value the newest completed session with the same setup, the same
   power state (platform profile, governor, EPP, boost and scaling maximum) and the
   same Curve Shaper ended at, when that value is below 0 and not past the deepest
   offset, unless its `--config` file names `starts`, which then decide alone; a start
   that fails walks
   again from 0 above it. The session's events name where each core started. A value is
   proven after 10 passes taken in rounds with the other cores, 15 s apart; a failure
   sets the core's fail bound and backs it off.
2. **Search, two threads per core**: when every selected core has two online CPUs, the
   same proof with both threads of the core and 6 passes, from the one-thread value.
3. **Each core alone** (LIVE1): every value live at once, each core tested alone again.
4. **Hot all-core** (STEADY): every core enters 3 steps looser than its proven value,
   under a hot load on every selected CPU. A failure that names a core backs that core
   off one step. One that names no core is narrowed by CCD and by half until every group
   that failed holds a core or a smaller group found: each core found alone backs off,
   each group that fails only together loosens and leaves the tighten rounds, and the
   stage runs again only once all of them have; every core loosens only when the full set
   fails and no smaller group does. The groups are tested by a short hot load after a test reports a failure
   under steady heat, and otherwise, a crash included, by the failed load replayed at its
   full length; a crash during a short group makes every later group replay at full
   length. A failure whose exact replay passes is a one-off, once per stage, and moves
   nothing.
5. **Changing loads** (TRANSIENT): burst, warm, burst and idle segments until an hour
   passes without a failure, a failure handled as in STEADY.
6. **Tighten** (TIGHTEN): in rounds, every core still in them tries one step tighter at
   once, for at most 3 rounds; a failure that names a core puts it back and takes it out
   of the rounds, and one that names no core ends them. The rounds also end before a step
   that would put every core an earlier all-core failure moved at or past the value it
   failed at, a one-off included.
7. **Endurance** (ENDURANCE, off by default): hot, burst, warm, burst and idle in a cycle
   until three hours pass clean, a failure handled as in STEADY.
8. **Memory** (MEMORY, on by default): stressapptest on each selected core, skipped with
   the reason when memory is short.
9. **Idle watch** (SOAK, on by default): 30 minutes without load, watching the kernel
   log. A machine check naming a core backs that core off; any other failure, a machine
   check no core is blamed for, kernel trouble or a crash, is narrowed as in STEADY with
   every group watched for the full length, and the core found backs off. The exception is
   a crash whose kernel log holds an uncorrected machine check no core is blamed for: it
   counts an incident and the watch runs again. A back-off here sends the session to
   FINALIZE.
10. **Finishing** (FINALIZE): when a value had to back off after LIVE1, the pass runs
    again from LIVE1 without TIGHTEN; otherwise the session completes with its values.

The tab names each stage in words; the names in brackets are what the session's events
and the command line print. Every length here is a setting, fixed for the session:
the changing-load, tighten and endurance stages repeat until their clean time is
reached, and a cool-down after a heat stop waits until the CPU is cool, within its
shortest and longest time.

### Controls

**Start** begins a session with the settings shown. **Pause** lets a one-core test
finish and stops an all-core test at once, which runs again from its start on resume,
unless the stopped test had already caught a failure naming a core: that failure is
judged and the session goes on from its verdict; **Stop** stops now and drops such a
failure; both put every selected core back to its baseline and keep the
session resumable. **Abort** (with a confirmation) puts every selected core back to its
baseline and stops the session as aborted, which Resume can still take up. A paused or
interrupted session that cannot be read stops as aborted unread by its status alone and
stays in History as stored: every core its CO journal shows it wrote in this boot goes
back to stock, and when the journal cannot be read or another session wrote after it, no
CO is written, so reboot or check the CO before tuning again. **Resume**
goes on with the newest session, **Validate** proves a completed session's values again,
and **Continue** and **Restart** answer a setup change (below). The tab follows the newest session through the history database,
so a driver started from a terminal shows there too and its controls work on it.

The same actions run headless: `corecycler tune` (with `--config FILE` or `--cores`),
`resume`, `validate`, `continue`, `restart` and `abort` take a session id, `corecycler status`
lists the sessions, and `corecycler --help` lists the flags and exit codes.
`corecycler margins SESSION_ID` prints, for each core and each 100 MHz of its clock, the
deepest offset its one-core tests passed above the shallowest that failed there, beside
the session's own result; it reads only a session that ran with the Curve Shaper at 0.

### Settings

The tab shows the cores, the core order (alternate across CCDs, one after another or
random), the deepest offset to try, the temperature limit and the memory test, idle watch
and endurance stages; **Advanced settings** shows every other setting in four groups
(the search, the all-core stages, temperature, and what happens when a test goes wrong).
Every setting has its unit to the right of its box and a line saying what it does; a
duration also reads in minutes or hours. Start refuses a setting outside its range and
names it. When a selected core already runs a CO value, set by the BIOS or written since
boot, Start lists those values and asks before it starts over them; the tuner searches
each core from 0 or from the value of the newest completed session with the same setup
and power state, never from the BIOS value, and a reboot brings the BIOS values back.
Start also asks, naming the
values then and now, when a selected core reads another CO than when the newest session
started, even when every core reads 0, and asks, naming it, when the newest session
cannot be read, as whether the CO moved since then cannot be judged. From the command
line, `tune` and `restart` take `--acknowledge` for the same answer.

### Power state and clocks

A session records the power state at Start: the platform profile, governor, EPP, boost
and the scaling maximum frequency. A capped machine (power-saver holding every core at
4300 MHz, for example) runs, and the cap is the session's range: every result belongs to
it, and a session that changes range midway pauses until the power state is put back. A
later session at another range tests the cores there. Every test also records each core's
highest observed clock, sampled ten times a second, and each CPU's scaling maximum;
History shows each core's peak in its own single-core tests of a session, and its highest
clock at every range seen so far beside the scaling maximum of the CPUs it ran on.

### Crash safety

Hard crashes are expected during CO tuning; that is how each core's limit is found.

- **Write-ahead journal**: every CO value is recorded before it reaches the SMU and marked
  survived once a test completes with it. After a crash, Resume reads the journal, the
  kernel log of the boot that died and the stored test rows, and blames what was live at
  the time: the core gets a fail bound there and backs off. A core that crashes at 0 is
  unstable at stock, and the session pauses. A program's fault is recorded into the
  session's log from the journal a resume reads, the dead boot's after a crash or this
  boot's on a resume in the same boot, once each, naming the process, the CPU and core, and
  its time into that boot; one logged between tests in a live session is not read.
- **Quarantine**: three crash-resumes in a row with no test surviving between them put
  every selected core back to 0 and stop the session for good.
- **Setup changes**: the session records the BIOS, kernel, microcode and app build at
  Start. A resume that finds only the kernel changed after a kernel update, at boot, by
  hand or from the tab, continues by itself as **Continue** does and says so in the
  session's log; `restart` and `abort` never continue by themselves. A kernel whose build
  could not be read, then or now, pauses instead. Any other change, alone or with the
  kernel, and a Validate on a different setup pause: put
  the setup back and resume, or **Continue** (carry the proven values into a new session
  under the new setup) or **Restart** (start over with the same settings). A continued
  session tests again what it carried: from the all-core stages it re-enters them from
  steady with the margin, and before it completes every core is tested alone at its value
  on the new setup, where a value that fails moves and is proven again.
- **Outside writes**: at resume and at the start of each stage every core's CO is read
  back. A value the session did not write pauses it, and so does a CO that cannot be read.
- **One writer**: the driver holds a lock for its whole run and the Curve-Tuner tab
  takes it for each write, so the two never write at once.
- **Temperature**: above the thermal limit, or the CPU's Tjmax when that is lower, the
  test stops and the tuner cools down and retries the same value; after three such
  retries in a row the next thermal stop pauses. Under the all-core loads the load is held
  when the CPU runs hot and goes on once it has cooled: the hot, burst and idle loads hold at
  the limit on the hottest sensor and go on 10 C under it; the warm load holds Tctl between
  55 and 65 C, or from 2 C above the Tctl read before its load when that is warmer, over the
  same span and never above the hot loads' release, and it holds while Tctl cannot be read.
  A hot, burst or idle test makes up the time it is held, and held more than 300 s in all it
  ends as a thermal stop. A warm test lasts its asked seconds with the hold cycling inside
  them, plus at most the rest of a hold still on when one of its slices ends; one hold longer
  than 300 s, a slice whose load ran under 5 percent of its time, or one whose Tctl went
  unread for more than half of it ends it with no verdict and it runs again; after three
  such warm retries in a row the next pauses the session. Both name where the hold releases and the last reading,
  and every all-core result says how many of its seconds the load ran. The burst and idle
  loads' own planned rests are not heat holds: they neither count toward the 300 s nor
  lengthen the test. A cool-down that does not bring the CPU below the limit within 300 s
  pauses the session. No readable temperature sensor refuses the run.
- **Other schedulers**: a stress tool whose nice value or scheduling policy differs from
  what it was started with (ananicy-cpp renices programs by name) is not a verdict: the
  test is repeated, and if it keeps happening the session pauses naming the program and
  how to stop it. A sched_ext scheduler that runs when the tab is about to start tests
  (Start, Resume, Validate, Continue, Restart) is named first, with Start anyway or
  Cancel. When a session ends, a scheduling service
  (ananicy-cpp, scx_loader, scx) that is enabled but stopped is named with the command
  that starts it again. Manual stress runs judge a renice the same way.
- **Hang watch**: `corecycler hang-watch`, run as the user who tunes (on NixOS a user unit
  of `deviceAccessUser` when `hangReset.enable` and `deviceAccess` are on), resets the
  machine through the kernel's sysrq when two checks 15 s apart find the session's
  heartbeat stopped for more than 120 s while its process lives and is not stopped: in a
  test, the idle watch's included, and between tests (the cool-down, the CO writes, the
  kernel-log read before a test, a verdict's commit). While a session drives, each check
  also writes and syncs a small file next to its history database on a thread of its own,
  and resets the same way when no such write has finished for more than 120 s: a freeze
  that stops the disk while the board's watchdog is still fed, which stops the journal and
  the database together. A write that fails with an error is logged and is no reset. Its
  own log never holds it: a line the journal does not take in time is dropped and counted,
  and once it refuses a line with an error the watch keeps checking and resetting with
  its lines held, and exits for systemd to start it again only at a check that suspects
  no hang and probes no disk. A paused or finished session is never reset. A freeze that
  stops the watch itself while the board's watchdog is still fed is not caught; that is
  left to the kernel's panics and to a reset by hand.

`corecycler resume --interrupted` resumes the one session a crash left running; with none
it does nothing and exits 0, and with two or more it resumes neither and names them. On
NixOS, `services.corecycler.autoResume.enable` runs it at boot with no login: the
`deviceAccessUser` lingers, and a user unit of theirs runs it once `autoResume.delaySeconds`
have passed. After a refusal (the SMU driver not loaded yet, the GUI already driving a
session, or another heavy test holding the shared lock) the unit tries again, three times a
minute apart. A paused session is your decision and is never resumed by itself.

## One heavy test at a time

Heavy tests never overlap for one user: each heavy test tool holds one shared lock, a file
in that user's runtime directory, for its whole heavy run and refuses to start while another
tool holds it. Tools run by different users take different files. The tuner driver
holds it from tune, resume, validate, continue or restart (the boot resume included) until
the session comes to rest, and **Start Test** and the Memory tab's stress test hold it for
their run. Abort runs no test and goes ahead; status, margins, doctor, shaper and hang-watch
never take it. A refusal names the holder,
`another heavy test holds the shared lock: <its line>`, and the driver then exits 5, so the
boot resume tries again.

Another tool takes the same lock in three steps:

1. Open `/run/user/<uid>/heavy-test.lock`, the uid of the user who runs the tool (under
   sudo, `SUDO_UID`), with `O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC` and mode 0600, never
   `O_TRUNC`. In a shell, `umask 077; exec 9<>"$path"`, never `exec 9>"$path"`, which
   empties the holder's line before the lock is tried; a shell open follows a link. Under
   sudo, a file this open created belongs to root: chown it to `SUDO_UID:SUDO_GID`, or the
   user's own runs cannot open it until reboot.
2. `flock(fd, LOCK_EX | LOCK_NB)`. On `EWOULDBLOCK`, read the file's first line and refuse
   naming it.
3. `ftruncate(fd, 0)`, then write one line at offset 0, `<tool> <pid> <time>` (a name
   without spaces, the process id, the local time in ISO 8601 to the second with its offset,
   like `vkbasalt-faults 4242 2026-10-22T14:25:03+02:00`), and keep the fd open for the whole
   heavy run: the kernel releases the lock when the process exits, however it exits.

Any error in any step refuses, naming it, and never runs unlocked.

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
core's phase, value and the value it failed at, and from its own single-core tests their
count, the last result and its highest clock. The log below lists the session's settings
by the Auto-Tuner tab's names, every test (an all-core or group test with the CPUs it
loaded and each core's value), the events, the boots it saw and each core's highest clock
at every power-state range. **Load these values into Curve-Tuner** fills the Curve-Tuner
tab's Value to set column from a completed session; nothing reaches the CPU until Set
changed.

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

A segfault or trap of another program that the kernel logs during a test is never a
verdict, whatever CPU it names. The Auto-Tuner keeps each one with the test as evidence,
with the test's start, and its log shows the fault as about N s into the test, which
compares the kernel log's clock with that start. A test run with **Start Test** keeps none.
