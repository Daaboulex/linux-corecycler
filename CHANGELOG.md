# Changelog

All notable changes to this CoreCycler-for-Linux project are recorded here,
following [Keep a Changelog](https://keepachangelog.com/) and
[Semantic Versioning](https://semver.org/).

## [Unreleased]

Current version: 0.0.1. A per-core CPU stability tester and AMD PBO Curve
Optimizer tuner for Linux, packaged as a NixOS module with an overlay.

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
- Zen 3 Curve Optimizer writes now honor the topology-probed physical core slot, so
  a harvested or 2-CCD Zen 3 part (5900X, 5600X) no longer writes the wrong core.
- Backend pass/fail parsing treats a crash signal (including SIGILL and SIGFPE) as
  instability even after an earlier "passed" line, so an unstable offset is never
  reported as stable.
- Malformed kernel, sysfs, or config input (a non-decimal L3 cache id, a wrong-typed
  `config_json` field) now fails closed instead of crashing.

[Unreleased]: https://github.com/Daaboulex/linux-corecycler
