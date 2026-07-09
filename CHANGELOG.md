# Changelog

All notable changes to this CoreCycler-for-Linux project are recorded here,
following [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Current version: 0.0.1. A per-core CPU stability tester and AMD PBO Curve
Optimizer tuner for Linux, packaged as a NixOS module with an overlay.

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
