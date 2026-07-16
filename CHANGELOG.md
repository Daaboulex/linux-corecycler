# Changelog

All notable changes to this CoreCycler-for-Linux project are recorded here,
following [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Current version: 0.0.1. A per-core CPU stability tester and AMD PBO Curve
Optimizer tuner for Linux, packaged as a NixOS module with an overlay.

### Fixed (2026-07-16 truth-and-attribution session, ryzen-9950x3d forensics)

Field incident driving all of this: three freezes in one day during
validation while the kernel logged corrected Load-Store MCEs naming cores 9
and 12 — and the engine saw none of it, then stood ready to crash-penalize
core 7 (the deepest, most-proven offset) on the next resume.

- MCE detection had never fired: the sysfs machinecheck bankN files it
  counted are MCA control registers (constant `ffffffffffffffff`), not error
  counters, and the dmesg parser rejected the real AMD Zen decoded format
  (header killed by an info-pattern, `[Hardware Error]:` detail lines failing
  the token gate, `CPU:9` colon form never matching `CPU (\d+)`). The sysfs
  path is deleted; dmesg parsing is rewritten against the captured kernel
  lines from the incident (now regression fixtures); events carry CPU, bank,
  and CE/UE severity; both SMT siblings map to their physical core.
- Crash blame is now evidence first, never a guess: on resume after a reboot
  the engine harvests the kernel journal since the session's last activity
  (`journalctl -k`, cross-boot) and penalizes exactly the cores the kernel
  named. With no forensics, a persisted isolated hunt slot convicts its core;
  a single in-test core in the search flow keeps direct blame; anything
  ambiguous (multi-core set, or any crash under validation) triggers the
  isolated crash hunt — each suspect alone at its tuned value, every other
  core at stock, under stress + load transitions + idle watch, most suspect
  first. Two fruitless hunts pause the session for the owner instead of
  guessing. The old "penalize the most aggressive in-test core" rule is gone.
- Cross-core MCE evidence acts immediately: a corrected error on ANY core
  during ANY test backs that core off one step and demotes it to re-earn
  confirmation (uncorrected: full crash-grade penalty); its journal entry is
  kept un-survived. An error at stock (CO=0) is surfaced loudly as a non-CO
  problem instead of walking a zero offset.
- Multi-core stage verdicts no longer read only the first core: a detected
  failure on any core in a batch is reported for THAT core instead of
  vanishing behind core 0's pass (stage 2/3 pass verdicts were fictional).
- The CO drift warning compared live SMU values against session baselines,
  so reopening the app mid-validation reported the tuner's own confirmed
  offsets as foreign "drift" and promised to "restore baselines" — false on
  both counts. Drift now compares against the tuner's last journaled write
  and only fires when something outside the tuner changed the hardware.
- Clock-stretch sampling discards windows without sustained load (>= 90 %
  busy) instead of reporting boost/idle artifacts as stretch; the peak is now
  persisted per test (`peak_stretch_pct`) instead of living only in fail text.
- Stage 2/3 log lines claimed "simultaneous"/"half loaded" while
  CoreScheduler cycles cores one at a time — reworded to what actually runs
  (true parallel load is the validation-restructure work).
- Export/Validate read only `phase='confirmed'` and reported "no confirmed
  cores" on a fully HARDENED session; hardened cores are included now.

### Added (2026-07-17, light-load coverage + real-world soak)

- The load class that actually crashes machines is now tested per core: a
  third default hardening tier runs the light-load spectrum (max-boost
  bursts, load transitions, idle watch) instead of sustained stress, and
  validation gains stage 5 — the same spectrum per core with every offset
  applied. Sustained-only testing passes cores that fail at max boost.
- Validation stage 6: the real-world soak. After a clean pass, the tuner
  applies the profile and just watches the kernel error stream (no
  synthetic load) while the machine is used normally; any hardware whisper
  demotes the named core and validation resumes after it re-earns. A dirty
  pass skips the soak; only the final clean pass earns it.
- Config: `validate_spectrum`/`spectrum_slot_seconds`,
  `validate_soak`/`soak_duration_seconds`, and hardening tiers accept
  `profile: spectrum`; all validated. Stage flow is a skip-chain (4 ->
  5 -> 6 -> finalize sentinel 7), so disabled stages fall through and the
  persisted cursor stays meaningful across versions.

### Changed (2026-07-17, simultaneous validation + observability)

- Validation stages 2/3 now genuinely run all target cores at once: one
  pinned stress process per core (`engine/parallel.py`), full package power
  draw, and per-core verdicts — every lane's pass/fail is logged, a failure
  names exactly its core, and launch failures fail closed (a missing binary
  behind taskset previously read as a clean pass).
- Live MCE polling no longer filters dmesg by level: AMD decoded
  corrected-error lines sit below err/warn and were silently invisible to
  the in-test detector (found live: a corrected error on an idle core during
  a validation slot left no trace). The line classifier is the filter.
- Two log surfaces: the human narrative stays at INFO on stderr; a rotating
  DEBUG file (`~/.local/share/corecycler/logs/corecycler.log`) records what
  verdicts drop — every dmesg poll and its events, every CO write with its
  survived state, and every validation-cursor save.
- Narration comments stripped repo-wide (175 lines): the source states
  timeless facts and constraints; the change story lives here.

### Changed (2026-07-16, incremental validation — schema v14)

- Validation progress is persisted after every transition
  (`tuner_sessions.validation_stage/index/half/dirty/requeue`): a reboot,
  app restart, or search interlude continues exactly where validation was.
  The old behavior restarted stage 1 for every core on every re-entry and
  every back-off — one field session logged 141 stage-1 tests (~12 h).
- A back-off now costs one re-test, not sixteen: a stage-1 failure retries
  only its own slot; a stage-2/3/4 failure backs off the FAILING core
  (per-core verdicts from phase 1), gives it one solo re-test with all
  offsets live, then reruns just the failed stage. Justification: raising
  one core's voltage cannot destabilize the others, so their coverage
  stays valid. Cores whose offset changed during a search interlude are
  detected by evidence (no stage-1 pass logged at their current best) and
  requeued automatically.
- DONE is stricter, not looser: if any back-off happened during a pass,
  one final complete validation pass must come through with zero
  back-offs before the profile is declared finished.

### Added (2026-07-16)

- PBO power limits (PPT W / TDC A / EDC A) captured into every tuning
  context (schema v13) and folded into the context identity — the same CO
  profile can be stable at PPT 200 W and unstable at 230 W. Read from the PM
  table with evidence-based Zen 5 header indices (live-verified on a
  9950X3D 0x620205: [2]=PPT limit, [3]=package power, [8]=TDC limit,
  [9]=TDC value, [63]=EDC-limit candidate); the old guessed [0..5] pair
  block decoded zeros and mislabeled every field on real Zen 5 silicon.
  Unknown table generations fail closed to "unknown" rather than store a
  mislabeled number.
- Schema v13: `tuning_contexts.ppt_limit_w/tdc_limit_a/edc_limit_a`,
  `tuner_sessions.unattributed_crashes/hunting_core`,
  `tuner_test_log.peak_stretch_pct`; fresh and migrated shapes stay
  identical; the DB-merge path carries the new columns.
- Tuner config: `hunt_slot_seconds` (default 60) and
  `max_unattributed_crash_hunts` (default 2), both validated.

### Fixed (2026-07-09 field-debug session, ryzen-9950x3d logs)

- mprime stale-results false-failure cascade: `cleanup(preserve_on_error=True)`
  left `results.txt` in place, `prepare()` never removed it, and mprime appends,
  so after one genuine failure every later test in that work dir re-parsed the
  old `FATAL ERROR` and failed at full duration. Observed live walking cores
  2/3/6 from -49/-44/-50 back to baseline one full 300 s "FAIL" at a time.
  Preserved post-mortem files are now renamed `failed-*`, `prepare()` cleans
  leftovers, and the scheduler polls `results.txt` every 5 s so a real error
  fails fast instead of at end-of-test.
- mprime patterns verified against Prime95 30.19b20 source (commonb.c): removed
  the benign `Worker stopped.` graceful-stop line from the fatal list (false
  positive), fixed the success pattern (`Self-test 4K passed!`, K-suffixed),
  corrected the torture-summary pattern, dropped folklore patterns with no
  source counterpart, and stopped writing `ResultsFile=`/`LogFile=` keys that
  mprime never reads.
- One database for sudo and non-sudo: history and settings paths resolved
  through `Path.home()`, so `sudo corecycler` silently used `/root/...` — a
  second database with divergent BIOS-change detection and invisible tuner
  sessions. All state now resolves to the INVOKING user (`SUDO_USER`),
  root-created files are chowned back, and any history the old bug left in
  `/root` is auto-merged into the user database once (source renamed
  `*.adopted`) via the new `HistoryDB.merge_from`.
- `sudo corecycler` aborted at startup (Qt xcb "could not connect to display",
  SIGABRT): sudo strips the display handshake env. main() now derives
  XDG_RUNTIME_DIR/WAYLAND_DISPLAY/XAUTHORITY from the invoking user's session
  and fails closed with an actionable message instead of Qt's abort when no
  display is reachable.
- Resume no longer penalizes plain app exits: crash detection (in_test flag +
  CO journal suspects) only fires when the machine actually REBOOTED since the
  session's last persisted write (`/proc/stat` btime); closing the app mid-test
  no longer walks good offsets away with a phantom "crash detected".
- Resume CO-drift warning no longer fires on the normal post-reboot state (SMU
  SRAM zeroed): `actual == 0` is expected, only a third value is drift.
- A hard crash at a CONFIRMED/HARDENED value now invalidates the confirmation:
  best_offset is demoted and the core re-enters backoff, so validation can no
  longer re-apply the exact value that crashed the machine (observed live on
  core 1 at -42). A multi-core validation crash penalizes only the most
  aggressive resident offset (matching the soft-fail policy) instead of
  demolishing the whole profile.
- Start-time/environment failures (missing binary, scheduler construction
  error, harness exception) now pause the tuner instead of being recorded as
  core stability failures that advance the search.
- Round-robin orders continue the cycle when the cursor core finishes instead
  of snapping back to core 0 (positional rotation), keeping cool-down fairness.
- Resuming a session mirrors its SAVED config into the Auto-Tuner panel, so
  the boxes show what is actually being executed.
- History tab detail pane: entering the tab auto-selected a session and then
  unconditionally cleared the detail, so it stayed collapsed until a top button
  was clicked; the 50/50 splitter math also no-oped before first layout. The
  detail now expands deterministically and the splitter state cannot
  degenerate.
- Every TunerPhase now has an explicit UI mapping (`gui/phase_style.py`,
  exhaustive at import): hardening/hardened cores no longer render as
  "pending", and abort resets the sidebar to each core's real phase.
- Fresh and migrated databases are now structurally identical (migration v12
  rebuilds `tuner_core_states` to the canonical NOT NULL shape), enforced by a
  fresh-equals-migrated schema test; plus WAL `busy_timeout` for concurrent
  access and an integrity `quick_check` on open that fails closed.

### Fixed (2026-07-09, poisoned-state prevention and exception honesty)

- Apparatus circuit breaker (`apparatus_failure_streak`, default 12): N
  consecutive FAILs on one core is physically implausible — every backoff step
  ADDS voltage and the midpoint jumps converge exponentially — so it means the
  test apparatus is lying, not the silicon (the stale-results.txt class:
  broken backend, corrupt work dir, dying disk). On trip the core rolls back
  to its most aggressive PROVEN pass (passes cannot be faked by a stale error
  file), poisoned backoff bounds are cleared, the core must re-confirm, and
  the tuner pauses loudly naming the suspicion.
- SMU revert failures now fail closed instead of being logged-and-forgotten:
  a failed post-test baseline revert leaves the aggressive offset RESIDENT,
  so the tuner pauses (search, thermal, and resume baseline-restore paths all
  covered) rather than marching on with poisoned hardware state.
- Unreadable `results.txt` is an apparatus fault, not a pass: parse_output
  previously swallowed the OSError and could silently pass a failing run; it
  now returns a "verdict unavailable" failure that the engine classifies as
  environment (pause), never as a stability verdict. Rapid-transition harness
  exceptions are classified the same way instead of as core instability.
- Global exception hooks (`sys.excepthook` + `threading.excepthook`): an
  uncaught exception logs the full traceback, force-stops the tuner (reverting
  CO toward baselines), and shows an error dialog — nothing can die silently
  mid-session anymore. Root logging is now actually configured (INFO to
  stderr; journald captures it), so the engine's root-cause breadcrumbs land
  somewhere instead of nowhere.
- Migration ALTERs no longer suppress all exceptions: the known
  partial-migration case is detected by an explicit column-existence check;
  any other failure raises instead of leaving a half-migrated schema marked
  as migrated. The impossible `create_context` fallback now raises a
  database-inconsistent error instead of returning a bogus id.
- Corrupt `settings.json` is preserved as `settings.json.corrupt` with a
  logged reason instead of being silently replaced by defaults; and
  `TunerConfig.from_json` logs which invalid/unknown fields it dropped.
- No-reboot resume no longer assumes zero baselines are resident: without a
  reboot the SMU still holds whatever was live at app exit (e.g. a mid-test
  offset), so the baseline is now written back explicitly; the reboot verdict
  is computed once and drives the drift check, crash detection, and baseline
  restore consistently. The display preflight respects an explicit
  `QT_QPA_PLATFORM` (offscreen/vnc/linuxfb need no display server).

### Fixed (2026-07-09, adversarial review + exhaustive state sweep)

- Startup/environment failures now revert the never-tested offset, persist
  the cleared in_test flag, and are handled BEFORE the journal is marked
  survived — previously the aggressive offset stayed resident during the
  pause, a later reboot+resume fabricated a crash verdict for a core that
  only had a missing binary, and the untested offset was journaled survived.
- Validation-stage scheduler failures route through the startup path instead
  of being logged as validate FAILs that backed off a healthy core.
- Engine self-pauses (apparatus breaker, SMU faults, startup) now enable the
  Resume button — previously every self-pause was a GUI dead end.
- resume()/validate_profile() refuse while a worker is still in flight —
  resuming under a live stress test rewrote SMU baselines beneath it (false
  PASS at an untested offset) and orphaned the worker thread.
- The apparatus breaker judges the search flow only: validation failures are
  legitimate consecutive backoffs, and isolation passes are not valid
  rollback evidence for the all-offsets-live context.
- Migration v12 runs in one transaction (an interrupted upgrade rolls back
  instead of bricking the DB), and the first sudo run no longer leaves the
  parent state directory root-owned.
- Exhaustive state-machine sweep (docs/tuner-state-spec.md, executed by
  tests/test_state_transition_spec.py: every phase x outcome x offset
  scenario through the real transitions) found and forced two fixes: a PASS
  at/beyond the recorded fail bound inverted the bounds and made the backoff
  binary search DIVERGE toward more aggressive values (failures now outrank
  passes — the search steps back inside the fail bound); and a persisted
  backoff row with best_offset NULL raised TypeError instead of failing
  closed to baseline.
- Evidence reconciliation on resume: a core claiming CONFIRMED/HARDENED at a
  non-baseline best with no logged pass to back it is demoted to re-confirm
  from its most aggressive proven pass (corruption or hand-edits cannot
  masquerade as proven results).
- Core-state sanity guard on the persistence boundary, both directions:
  offsets outside the sane CO range or negative counters raise instead of
  being written or loaded as truth.

### Added (2026-07-09, robustness infrastructure)

- The full unit/property suite now runs inside the nix package build
  (doCheck; offscreen Qt) — CI gates the artifact, not just packaging. The
  e2e subprocess replays stay in the dev loop (marked slow).
- `Path.home()`/`os.path.expanduser` are banned by lint outside
  `config/paths.py` (ruff TID251) — the split-database bug class is now
  unwritable.
- The closed-loop property fuzz additionally injects no-reboot APP EXITS
  (window closed/SIGKILL) at random points, exercising the no-penalty resume
  world alongside power-loss reboots.

### Added (2026-07-09)

- `docs/test-order-spec.md`: control-system spec chart for all five test
  orders (pick rule, cursor state, interruption/resume semantics, global
  invariants), executed by `tests/test_test_order_spec.py`.
- Duplicate-function guard (`tests/test_no_duplicate_functions.py`): identical
  function bodies in `src/` fail the suite; first catch (the `_item` table
  helper, duplicated in two tabs) extracted to `gui.widgets.table_item`.
- ruff enforced in the pre-commit gate (was configured but never wired); repo
  is lint-clean at line-length 120 (matching the codebase's de-facto style).

### Added

- Auto-tuner CO write-ahead journal: every Curve Optimizer write to the SMU is
  durably recorded before the hardware write, so any hard crash (idle, baseline
  restore, post-test revert, validation, or search) is attributable to the exact
  (core, value) that was resident when the machine died — not just to a core that
  happened to be mid-test.
- Resume-crash circuit breaker (`resume_crash_quarantine_threshold`, default 3):
  after that many consecutive crash-resumes with no surviving test in between, the
  tuner forces every core to stock (CO=0), quarantines the session, and reports an
  honest "unsafe" verdict instead of re-applying a profile that keeps crashing.
- Fail-closed thermal protection (`allow_missing_thermal_sensor`, default off):
  the tuner refuses to drive a stress test with no readable CPU temperature sensor
  unless explicitly allowed.
- End-to-end fault-injection test suite (`tests/test_tuner_faults.py`) covering
  unflagged hard crashes, unstable baselines, repeated resume-crashes, SMU write
  faults, the write-ahead journal, fail-closed thermal, and every test-order style.
- PBO power/current limit setters (PPT/TDC/EDC) now range-check their input and
  reject out-of-range values before any SMU write.

### Fixed

- Auto-tuner could enter an infinite resume-crash loop: an unstable baseline (or a
  crash that left no `in_test` flag) was re-applied verbatim on every resume. Crash
  backoff now treats CO=0 (stock) as the only axiomatically safe floor and an
  unstable baseline descends toward 0, so resume can never re-apply the value that
  crashed the machine, and the circuit breaker bounds the loop.
- The resume-crash circuit breaker now also engages during multi-core validation:
  a hard crash there was previously invisible to both crash detectors, so a profile
  that crashes only under combined load could be re-applied into the same crash on
  every resume.
- Per-core Curve Optimizer writes address the correct physical core on harvested and
  multi-CCD parts (5900X, 7900X, 9900X, 5600X, ...) by using the physical,
  gap-preserving core id Linux exposes (the kernel's own APIC-ID decode); the earlier
  SMU slot-probe heuristic was removed as unnecessary on Linux and unreliable
  (it depended on undocumented GET-on-disabled-slot firmware behavior).
- Backend pass/fail parsing treats a crash signal (including SIGILL and SIGFPE) as
  instability even after an earlier "passed" line, so an unstable offset is never
  reported as stable.
- Malformed kernel, sysfs, or config input (a non-decimal L3 cache id, a wrong-typed
  `config_json` field) now fails closed instead of crashing.

[Unreleased]: https://github.com/Daaboulex/linux-corecycler
