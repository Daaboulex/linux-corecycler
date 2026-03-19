# Codebase Concerns

**Analysis Date:** 2026-03-19

## Known Bugs

**PySide6 Dict Signal Marshalling Crash:**
- Symptoms: Application crashes on tuner completion with `_pythonToCppCopy: Cannot copy-convert (dict) to C++` error
- Files: `src/gui/main_window.py` (line 60), `src/tuner/engine.py` (line 159)
- Trigger: Tuner engine emits `session_completed` signal with dict payload across QThread boundary
- Status: FIXED in `src/tuner/engine.py` (now uses JSON string), but NOT in `src/gui/main_window.py` (still uses dict)
- Details: Line 60 defines `test_completed = Signal(dict)` which PySide6 cannot marshal across threads. The tuner engine was corrected to emit JSON (line 856), but the GUI worker thread still declares dict signal.
- Fix: Change `src/gui/main_window.py` line 60 from `Signal(dict)` to `Signal(str)` and decode JSON in handler

**Stall Detection False Positives:**
- Symptoms: "Stall detected on core N (CPUN): near-zero usage for 30 s" errors flooding logs even when system is idle/stable
- Files: `src/engine/scheduler.py` (lines 300-350)
- Trigger: CPU usage reader returns zero or near-zero on startup before stress process fully loads
- Details: Stall detection compares raw `/proc/stat` CPU usage without accounting for process startup delay. On systems where stress takes >5 seconds to reach 100% load, false positives occur immediately.
- Workaround: Increase stall timeout or skip stall detection on first iteration
- Improvement path: Add startup grace period or weighted moving average to CPU usage calculation

## Tech Debt

**Database Access Pattern - Private Connection Exposure:**
- Issue: Multiple files directly access `db._conn` instead of using public HistoryDB methods
- Files: `src/gui/history_tab.py` (lines 758, 1126-1129), `src/tuner/persistence.py` (lines 33, 46, 53, etc.), `src/history/logger.py`
- Impact: Breaks encapsulation, makes refactoring database layer risky, violates access control
- Fix approach: Create public query/mutation methods in `HistoryDB` class for all operations, migrate callers to use them instead of `._conn`
- Priority: Medium - Affects maintainability but currently works due to SQLite safety (WAL + autocommit)

**Subprocess Lifecycle Management - Race Conditions:**
- Issue: Process termination may not complete before timeout expires, leading to orphaned processes
- Files: `src/engine/scheduler.py` (lines 685-690), `src/gui/memory_tab.py` (lines 295)
- Pattern: Uses `kill()` then `communicate(timeout=10)` but doesn't guarantee process group cleanup on timeout
- Details: When stress process doesn't respond to SIGKILL, logs "output lost" but process group (`os.setsid()` at line 588/407) may remain if daemon threads in stress process are stuck
- Fix approach: Use `os.killpg()` to kill entire process group, add explicit cleanup in app shutdown
- Priority: Medium - Affects system stability when stress tools hang

**Threading Edge Case - QThread Worker Lifecycle:**
- Issue: Worker threads not properly cleaned up on abnormal exit
- Files: `src/main.py` (lines 55-64), `src/gui/main_window.py` (lines 53-120)
- Details: `_cleanup_on_exit()` checks `hasattr()` for attributes that should always exist. If worker thread crashes before fully initialized, cleanup may fail to find running processes.
- Example: If `-worker.start()` never completes, `hasattr(window, '_worker')` passes but `_worker.isRunning()` might be stale
- Fix approach: Use explicit thread safe flags instead of attribute checks; add try/except around all cleanup operations

## Fragile Areas

**Tuner State Machine Recovery:**
- Files: `src/tuner/engine.py` (lines 271-350), `src/tuner/persistence.py`
- Why fragile: Resume path (line 271) depends on correct phase classification to avoid re-applying dangerous CO offsets. If phase is misdetected or database corruption occurs, system crash is possible.
- Safe modification: Always validate loaded state before resuming; add assertions for phase transitions
- Test coverage: Needs unit tests for resume path with simulated crashes at each phase
- Risk: Currently only integration tested, no explicit resume tests

**SMU CO Write Safety:**
- Files: `src/smu/driver.py` (lines 140-170), `src/smu/commands.py`
- Why fragile: CO (Curve Optimizer) writes are VOLATILE and directly interact with kernel module. Malformed values crash system immediately.
- Current safety: Read-back verification (lines 160-170) and permission pre-check (lines 125-137)
- Concern: Pre-check may succeed but sysfs may become unwritable between check and write (TOCTOU race)
- Safe modification: Wrap writes in try/except, fall back to dry-run mode on first permission error
- Test coverage: No negative tests for permission failures or SMU unavailability

**History Database Schema Migration:**
- Files: `src/history/db.py` (lines 141-166, migration code)
- Why fragile: Five sequential migrations (v1→v5) with no rollback. If migration fails mid-execution, database is left in inconsistent state.
- Current approach: Uses `executescript()` for atomic execution, but interrupted shutdown could leave WAL files
- Concern: Schema version constraint added in v5 (line 378) may conflict with existing data
- Safe modification: Add migration sanity checks and validate before/after schema integrity
- Test coverage: No automated migration tests

**Kernel Module Version Pinning:**
- Files: `nix/zenpower.nix` (line 36: fixed commit hash), `nix/ryzen-smu.nix` (lines 28-34: fixed commit hash), `nix/it87.nix` (lines 33-39: fixed commit hash)
- Why fragile: Each kernel module is pinned to a specific upstream commit. If upstream repos become unavailable or commits are lost, builds fail irreversibly.
- Current approach: Explicit SHA256 hashes guard against content change, but not repository deletion
- Concern: These are community-maintained forks (mattkeenan/zenpower5, amkillam/ryzen_smu, frankcrawford/it87) with no guarantee of long-term availability
- Scaling limit: No fallback build sources, no vendored copies
- Improvement path: Mirror critical modules to NixOS cache or vendor inline, add deprecation warnings if upstream goes silent

## Security Considerations

**Root Privilege Usage:**
- Risk: Application requires sudo to access /sys/kernel/ryzen_smu_drv and MSR devices. No privilege separation or capability binding.
- Files: `nix/module.nix` (device access setup), all files in `src/smu/`, `src/monitor/`
- Current mitigation: Udev rules grant access to specific group, reducing need for full root. Sysfs permission pre-check before writes.
- Recommendations: Document that any user in `corecycler` group can modify CPU behavior. Consider adding audit logging for CO writes.

**JSON Injection Risk - Minimal:**
- Risk: History database stores JSON blobs without validation
- Files: `src/history/db.py` (lines 48, 95: JSON fields), `src/history/logger.py` (line 54)
- Current mitigation: JSON encoded by Python stdlib (safe from code injection), SQLite parameterized queries used throughout
- Status: Low risk - no deserialization of untrusted JSON

**Subprocess Command Injection - Mitigated:**
- Risk: Stress backend commands built with string formatting
- Files: `src/engine/backends/mprime.py` (lines 85-109), `src/engine/backends/stress_ng.py`
- Current mitigation: Uses list-based Popen args (not shell=True), no user input in command line
- Status: Safe - backend selection is UI-controlled, not user-supplied

## Performance Bottlenecks

**CPU Usage Reader - Polling Overhead:**
- Problem: `/proc/stat` parsed every second for all cores during test (scheduler.py line 283-310)
- Files: `src/engine/scheduler.py` (lines 283-310), `src/monitor/cpu_usage.py`
- Cause: Full proc read + string parsing on every poll cycle, done in main scheduler thread
- Scaling limit: N cores = N lines parsed every second; marginal CPU impact but avoidable
- Improvement path: Cache /proc/stat reads in background thread, use mmap'd access to single core stats only

**History Database - Full Table Scans:**
- Problem: History tab loads all runs on startup without pagination
- Files: `src/gui/history_tab.py` (lines 100-200)
- Cause: UI has no pagination; `refresh()` fetches entire history
- Scaling limit: Breaks with >10,000 runs (typical user: thousands of runs over months)
- Improvement path: Add pagination (limit 100 per page), indexed queries by context_id and date range

**Qt Signal Emissions - No Batching:**
- Problem: CoreScheduler emits individual status signals per core per second
- Files: `src/gui/main_window.py` (TestWorker.finished signal), `src/engine/scheduler.py`
- Cause: Each core status triggers UI update independently
- Scaling limit: 192-core systems would emit 192 signals/second, overwhelming UI thread
- Improvement path: Batch updates — emit once per second with all core deltas

## Scaling Limits

**GUI Responsiveness - Large Core Counts:**
- Current capacity: Tested up to ~32 cores smoothly
- Limit: 192-core EPYC systems would likely freeze during core grid updates
- Scaling path: Move core grid to virtualized widget, defer rendering until scroll
- Files: `src/gui/widgets/core_grid.py` (line 270)

**Database Growth - Unbounded History:**
- Current capacity: ~100k test runs (gigabytes on disk)
- Limit: No archival or cleanup mechanism; SQLite becomes slow after millions of rows
- Scaling path: Add history archival (export old runs as JSON archives, delete from DB), implement pagination
- Files: `src/history/db.py` (no cleanup method)

**Stress Tool Process Management:**
- Current capacity: All cores can run simultaneously (stress process per core)
- Limit: System OOM if stress processes consume all RAM (no per-process memory limit set)
- Current mitigation: Manual monitoring, user stops test if system swaps
- Improvement path: Add resource limits via `resource.setrlimit()` before stress process exec

## Missing Critical Features

**No Process Resource Limits:**
- Problem: Stress processes (stress-ng, mprime) can consume unlimited memory
- Blocks: Cannot safely leave tuner running unattended on memory-limited systems
- Files: `src/engine/backends/base.py`, `src/engine/scheduler.py` (lines 401-420, 582-600)
- Approach: Use `resource.setrlimit()` in preexec_fn to set RLIMIT_AS per config

**No Persistence of Manual CO Tuning:**
- Problem: Manual CO writes via GUI aren't recorded in tuning context, making resume impossible
- Blocks: Users can't manually adjust CO then resume auto-tuning from current point
- Files: `src/smu/driver.py` (CO write methods), `src/gui/smu_tab.py`
- Approach: Log manual writes to history DB with "manual" session type

**No Automatic System Recovery on Crash:**
- Problem: If CPU crash occurs (freeze, watchdog), user must manually undo last CO offset
- Blocks: Cannot safely run overnight without supervision
- Files: All files in `src/tuner/` — no "safe mode" fallback
- Approach: On startup, detect if previous session crashed, offer rollback to known-good CO values

## Test Coverage Gaps

**SMU Driver Error Cases:**
- What's not tested: Permission failures, sysfs unavailability, malformed responses from kernel module
- Files: `src/smu/driver.py` (lines 125-250)
- Risk: Permission error halfway through CO write leaves system in partially applied state
- Priority: High - affects core functionality

**Tuner Resume After Crash:**
- What's not tested: Complete resume flow with phase transitions, offset persistence verification
- Files: `src/tuner/engine.py` (lines 271-350)
- Risk: Resume could re-apply dangerous offset if database corrupted
- Priority: High - crash recovery is safety-critical

**Kernel Module Availability:**
- What's not tested: ryzen_smu unavailable, zenpower not loaded, it87 missing
- Files: `src/smu/driver.py`, `src/engine/detector.py`
- Risk: Silent degradation - app runs but can't access CO, user unaware
- Priority: Medium - affects feature detection, not core functionality

**Large History (1000+ runs):**
- What's not tested: UI performance, database query speed, pagination edge cases
- Files: `src/gui/history_tab.py`, `src/history/db.py`
- Risk: Crashes when browsing history on long-running deployments
- Priority: Low - edge case, rare in practice

---

*Concerns audit: 2026-03-19*
