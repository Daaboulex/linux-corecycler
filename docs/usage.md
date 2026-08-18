<!-- markdownlint-disable MD013 -->

# Usage guide

## Quick start

1. Launch CoreCycler (`corecycler` or `python src/corecycler/main.py`). Full monitoring
   (clock stretch, per-core power, Curve Optimizer) needs access to the MSR and SMU
   devices: grant it to your user once ([Device access](installation.md#device-access))
   or launch with `sudo`. Without either it runs with reduced telemetry and a status-bar
   warning.
2. The CPU topology (cores, CCDs, SMT, X3D) is detected automatically.
3. In the Configuration tab, select a backend -- use stress-ng if mprime is not
   installed. A backend that is not on PATH (mprime and y-cruncher are extracted
   tarballs, and `sudo` drops your shell's PATH) is not a dead end: CoreCycler offers
   any copy it finds and remembers the one you pick. `corecycler doctor` shows every
   tool and where it resolved from.
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
Auto-Tuner's Export Profile and the History tab's "Load to CO Tab").

All values written here are **volatile** and reset on reboot; set them in BIOS to make
them permanent. For Zen 2 (no Curve Optimizer) the tab shows "Connected (no CO support)"
-- PBO limits and scalar are still accessible.

## Auto-Tuner

The Auto-Tuner automates the entire PBO Curve Optimizer search via runtime SMU writes
(requires `ryzen_smu`). For each core it runs a coarse-to-fine search:

1. **Coarse search** -- from `start_offset` (default 0), step by `coarse_step`
   (default -5) toward `max_offset`, running a short test (`search_duration`, default
   60s) per step. A pass goes more aggressive; a fail bounds the limit.
2. **Fine search** -- from the last passing coarse value, step by `fine_step`
   (default -1) toward the failure point to narrow the exact limit.
3. **Confirmation** -- a longer test (`confirm_duration`, default 300s) validates the
   best offset. On failure, a **smart backoff** finds the nearest stable offset: a quick
   pre-confirm filter (`search_duration x backoff_preconfirm_multiplier`, default 2x), a
   **midpoint jump** after `midpoint_jump_threshold` (default 3) consecutive pre-confirm
   fails, then **binary search** to the boundary. Backoff stops at the BIOS baseline
   (from `inherit_current`), not zero -- unless the baseline itself crashes, in which
   case it descends toward CO=0 (see [Crash safety](#crash-safety)).
4. **Multi-core validation** (`auto_validate`, on by default) -- after every core is
   individually confirmed, a staged sequence catches failures invisible to per-core
   testing:
   - **Stage 1 -- per-core, all offsets live**: all confirmed offsets applied, each core
     stressed in turn. Catches shared-VRM power-delivery interactions.
   - **Stage 2 -- all-core simultaneous**: one pinned stress process per core at once
     for `validate_duration` -- full package-power worst case, per-core verdicts.
   - **Stage 3 -- alternating half-core load**: half loaded, half idle, then swap
     (split by CCD when available). Catches boost-ramp voltage transients.
   - **Stage 4 -- rapid transitions** (`validate_transitions`): fast load/idle cycling
     across all cores -- catches idle-to-boost instability.
   - **Stage 5 -- per-core spectrum** (`validate_spectrum`): bursts, load transitions
     and idle watch per core with all offsets live.
   - **Stage 6 -- all-core memory load** (`validate_memory`): one memory stressor
     (stressapptest) per core at once, all offsets live -- catches CO marginality
     that only appears under memory-controller load. Skipped with a log if no memory
     stress tool is installed.
   - **Stage 7 -- real-world soak** (`validate_soak`): no synthetic load; the kernel
     error stream is watched for `soak_duration_seconds` while the machine is used
     normally. Any hardware event fails it.

   A failing core is backed off by one `fine_step`, re-proven solo, and only the failed
   stage reruns. A stall, external kill, or unattributable machine check is an
   APPARATUS fault: the step retries without a verdict (up to `max_apparatus_retries`),
   never a back-off. Any back-off marks the pass dirty -- completion requires one final
   clean pass. If a failing core reaches its baseline with real failures persisting,
   the tuner reverts everything to baseline and PAUSES with an honest message instead
   of declaring completion. A reboot mid-validation with no attributable evidence is
   recorded as an unattributed incident; repeated incidents pause for your decision.
5. **Result** -- each core gets a confirmed, cross-validated best offset. Export it as
   JSON or load it into the Curve Optimizer tab.

### CO isolation

During per-core search the **only** non-baseline offset on the CPU is the core under
test -- all others are reverted to baseline before and after each test, so a crash is
never wrongly blamed on a different core. During multi-core validation isolation is
deliberately off (all confirmed offsets are live to test interactions); on a stage
failure or a partial SMU write, all cores revert to baseline before pausing.

### Crash safety

Hard crashes are expected during CO tuning -- that is how each core's limit is found. No
sequence of crashes, reboots, SMU faults, or resumes can leave the tuner re-applying a
configuration that crashes the machine. Three mechanisms enforce this:

1. **CO write-ahead journal** -- every CO value is recorded in SQLite *before* it is
   written to the SMU, and marked "survived" only after a test completes with it
   resident. So on the next launch the exact `(core, value)` live at crash time is known
   (whether or not a test was running, and including a crash during idle, baseline
   restore, post-test revert, or multi-core validation). Every un-survived offset is
   treated as a hard crash: a fail bound is set there and the core backs off past it.
2. **CO=0 is the only safe floor** -- the one state guaranteed stable is CO=0 (stock
   voltage). Crash backoff never settles past it, and if a core's *baseline* is what
   crashed, the baseline is descended toward 0 rather than re-applied -- so an unstable
   inherited baseline is escapable, not an infinite loop.
3. **Resume-crash circuit breaker** -- consecutive crash-resumes with no surviving test
   between them are counted (reset whenever any test completes). After
   `resume_crash_quarantine_threshold` (default 3), the tuner forces every core to CO=0,
   marks the session `quarantined` (never resumed automatically, and re-opened by hand
   only after a warning -- it then re-engages on offsets the machine has already
   survived, stock where it survived none), and surfaces an honest "unsafe on this
   machine" verdict. A machine that keeps dying on resume is
   bounded, not looped -- this holds during multi-core validation too.

An interrupted session is detected on next launch and offered for resume; resume
re-applies only journaled-safe baselines and re-engages one core at a time. A crash or
pause during validation is recoverable: on resume the tuner restores the persisted
validation cursor and continues at the exact stage and position it left off.

### State machine

```text
NOT_STARTED -> COARSE_SEARCH -> FINE_SEARCH -> SETTLED -> CONFIRMING -> CONFIRMED
                                    |                       |            |
                                    v                       v            v (all cores)
                                 SETTLED              FAILED_CONFIRM   VALIDATION
                                                           |     S1 -> S2 -> S3 -> S4 -> S5 -> S6 -> S7
                                                           v      (fail: back off failing core,
                                                    BACKOFF (smart)    solo re-prove, rerun stage;
                                                    - pre-confirm      dirty pass => one final
                                                    - midpoint jump    clean pass before DONE)
                                                    - binary search
                                                    - baseline floor
```

### Configuration options

| Parameter | Default | Range | Description |
|---|---|---|---|
| Start Offset | 0 | -60 to +30 | Starting CO value for all cores |
| Coarse Step | 5 | 1-15 | Step size during coarse search |
| Fine Step | 1 | 1-5 | Step size during fine search |
| Max Offset | -50 | -60 to +60 | Most aggressive offset (auto-clamped to CPU generation) |
| Search Duration | 60s | 10-600s | Test duration per search step |
| Confirm Duration | 300s | 30-1800s | Test duration for the confirmation run |
| Validate Duration | 300s | 30-3600s | Test duration per multi-core validation stage |
| Max Confirm Retries | 2 | 0-5 | Retries before backing off from a value |
| Auto Validate | true | true/false | Run staged multi-core validation (stages 1-7) after all cores confirm |
| Backend | mprime | mprime/stress-ng/y-cruncher | Per-core stress backend (stressapptest is Memory tab only) |
| Mode | SSE | SSE/AVX/AVX2/AVX512 | Stress instruction set |
| FFT Preset | SMALL | SMALLEST/SMALL/LARGE/HUGE/ALL/MODERATE/HEAVY/HEAVY_SHORT | FFT size preset (mprime) |
| Test Order | sequential | see below | Core testing order |
| Stretch Threshold | 3.0% | 0-20% | Clock-stretch failure threshold (0 = off, requires root) |
| Abort on Consecutive Failures | 0 | >= 0 | Abort if N cores fail at start_offset (0 = off) |
| Resume Crash Quarantine Threshold | 3 | 1-20 | Crash-resumes (no surviving test between) before forcing CO=0 and quarantining |
| Allow Missing Thermal Sensor | false | true/false | Permit running with no readable temperature sensor (false = fail closed) |
| Inherit Current CO | false | true/false | Read current SMU offsets as starting points |

Abort on Consecutive Failures, Resume Crash Quarantine Threshold and Allow Missing
Thermal Sensor are not in the panel: set them in the JSON that `corecycler tune
--config` loads. The Backoff Pre-Confirm Multiplier (2.0) and Midpoint Jump
Threshold (3) use sensible defaults and are not exposed at all.

### How each backend uses Mode and FFT Preset

**Mode** (SSE/AVX/AVX2/AVX512) and **FFT Preset** mean different things per backend:

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

### Test orderings

| Order | Strategy | Best for |
|---|---|---|
| sequential | Finish each core completely before the next | Simple, easy to follow |
| round_robin | One test per core per round | Partial results for all cores sooner |
| weakest_first | Prioritize cores closest to confirmation | Finish nearly-done cores first |
| ccd_alternating | Alternate CCDs, prioritize the CCD with fewest confirmed | Balanced thermal coverage |
| ccd_round_robin | Round-robin within each CCD, alternating CCDs | Best thermal profile -- each core cools while the other CCD is tested |

### Tips

- **mprime, Small FFTs, SSE** is the gold standard for CO testing -- highest single-core
  clocks and the most sensitive error detection (rounding checks, SUMOUT verification).
- The default `max_offset` of -50 suits Zen 4 and is Zen 5's firmware limit (a
  deeper request is clamped); Zen 3/3D are clamped to -30 automatically.
- A typical 16-core run takes ~2-4 hours plus several hours of staged validation (stages 1-7 include an all-core memory-load stage and a 30-minute real-world soak).
- If many cores fail at the starting offset, enable **abort on consecutive failures**
  (e.g. 3) -- it usually means BIOS PBO needs adjusting first.
- **Multi-core validation** is the key differentiator from manual testing: a core stable
  in isolation may fail when all cores draw power at once. If validation keeps backing
  off and restarting, the VRM may not support the aggregate profile -- reduce
  `max_offset` or test fewer cores.

During an active tuner session the config snapshot is taken at start, so mid-run UI
changes have no effect; the Curve Optimizer tab and manual Start Test are locked to
prevent SMU conflicts. Every session is saved in SQLite (config, per-core progress, and
each test result); starting a new session never deletes old ones.

## Recommended settings

| Scenario | Backend | Mode | FFT Preset | Preset |
|---|---|---|---|---|
| Quick screening | stress-ng | SSE | -- | Quick |
| Initial CO tuning | mprime | SSE | Small | Standard |
| Thorough validation | mprime | SSE | Huge | Thorough |
| Comprehensive stability | mprime | SSE | All | Full Spectrum |
| AVX2 validation | mprime | AVX2 | Heavy | Standard |

## Understanding results

Core grid colors -- **blue**: testing now; **green**: passed/confirmed; **red**: failed;
**amber**: tuner smart-backoff; **gray**: pending; **purple**: memory stress (all cores
at once).

| Error type | Meaning | Typical cause |
|---|---|---|
| MCE (Machine Check Exception) | Hardware-level CPU error (sysfs or dmesg) | CO too aggressive -- voltage too low for the requested frequency |
| Computation | Stress test got a wrong result (rounding, sumout, mismatch) | CO too aggressive -- subtle numerical instability |
| Idle instability | MCE during an idle/C-state transition | CO unstable during voltage ramp-up from deep sleep |
| Load transition | Error during a variable-load stop/start | Voltage regulation insufficient during rapid load changes |
| Timeout | Stress process stopped responding | Possible instability hang (sometimes benign) |
| Crash | Stress process terminated unexpectedly | Core instability causing instruction faults |

**MCE errors are the most serious** -- the CPU detected an actual hardware-level error;
reduce that core's CO (make it less negative). **Computation errors** are the most common
CO-tuning failure. **Idle instability** is caught by the idle test phase and is a failure
mode sustained-load tests miss entirely.
