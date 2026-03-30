# Multi-Mode Tuning Pipeline Design

## Problem Statement

The current auto-tuner has three critical shortcomings:

1. **Single-mode verification**: Finding offsets with SSE SMALL only. An offset stable under SSE can crash under AVX2 (higher power draw, deeper Vdroop) or LARGE FFT (different memory controller stress). The "confirmed" values are only confirmed for one workload type.

2. **Death spiral backoff**: When confirmation fails, the backoff mechanism has no intelligent termination — it grinds cores endlessly toward baseline. Core 1 went from -36 to -15 over 20 hours in observed runs. Core 8 from -29 to -5.

3. **Crash blindness**: System crashes appear as silent gaps in the test log. The algorithm doesn't learn from crashes — it treats a crash the same as a soft mprime error, backing off by 1 fine_step when the offset was dangerously unstable.

Additional need: after BIOS updates, users must re-validate previously found offsets without rediscovering from scratch.

## Design Overview

A single-session pipeline that discovers, confirms, hardens across multiple stress modes, and validates — all without manual intervention. The pipeline progresses automatically through stages, with crash-aware scheduling and intelligent termination to prevent wasted time.

```
DISCOVERY (SSE SMALL, 60s)
    |
CONFIRMATION (SSE SMALL, confirm_duration)
    |
HARDENING T1 (AVX2 SMALL, confirm_duration)
    |
HARDENING T2 (SSE LARGE, confirm_duration)
    |
CROSS-CORE VALIDATION (4 stages, multi-mode)
    |
DONE -- true stable profile
```

## 1. Pipeline State Machine

### Per-Core States

```
NOT_STARTED -> COARSE_SEARCH -> FINE_SEARCH -> SETTLED -> CONFIRMING -> CONFIRMED
                                                  |                        |
                                            FAILED_CONFIRM           HARDENING_T1
                                                  |                        |
                                            BACKOFF_PRECONFIRM       HARDENING_T2
                                                  |                        |
                                            BACKOFF_CONFIRMING         HARDENED
```

### Session States

```
RUNNING -> VALIDATING -> COMPLETED
   |           |
   +-> PAUSED  +-> RUNNING (validation failure triggers backoff, restart validation)
   |
   +-> CRASHED (detected on resume)
   |
   +-> ABORTED (user stop)
```

### State Transitions

**Discovery (COARSE_SEARCH)**:
- Each test runs at `current_offset` for `search_duration_seconds` (default 60s) using the primary backend/mode
- PASS: record as `best_offset`, advance by `coarse_step` toward `max_offset`
- FAIL: record `coarse_fail_offset`, transition to FINE_SEARCH
- Pre-crash safety ramp: when within `coarse_step * 2` of `max_offset`, automatically switch to `fine_step` increments

**Fine Search (FINE_SEARCH)**:
- Narrows between `best_offset` and `coarse_fail_offset` using `fine_step`
- PASS: update `best_offset`, advance by `fine_step`
- FAIL: update `coarse_fail_offset`, check if window <= `fine_step` -> SETTLED

**Confirmation (CONFIRMING)**:
- Tests `best_offset` for `confirm_duration_seconds` (default 300s) using primary backend/mode
- PASS: transition to CONFIRMED
- FAIL: increment `confirm_attempts`. If < `max_confirm_retries` -> retry. If >= -> FAILED_CONFIRM

**Backoff (FAILED_CONFIRM -> BACKOFF_PRECONFIRM -> BACKOFF_CONFIRMING)**:
- BACKOFF_PRECONFIRM: quick filter at `backoff_preconfirm_multiplier * search_duration_seconds`
- Linear backoff by `fine_step` until preconfirm passes or midpoint jump triggers
- Midpoint jump after `midpoint_jump_threshold` consecutive preconfirm failures
- Binary search narrows between `backoff_pass_bound` and `backoff_fail_bound`
- BACKOFF_CONFIRMING: full `confirm_duration_seconds` test at the candidate offset

**Hardening (CONFIRMED -> HARDENING_T1 -> HARDENING_T2 -> HARDENED)**:
- HARDENING_T1: test at `best_offset` using the first hardening tier config (default: AVX2 SMALL) for `confirm_duration_seconds`
  - PASS: advance to HARDENING_T2
  - FAIL: back off by `fine_step`, retry T1
- HARDENING_T2: test at `best_offset` using the second hardening tier config (default: SSE LARGE) for `confirm_duration_seconds`
  - PASS: transition to HARDENED
  - FAIL: back off by `fine_step`, retry T2 directly (T1 result carries forward -- more conservative offset is guaranteed to pass at modes already passed at more aggressive offsets)
- Hardening backoff is **linear** (fine_step at a time, toward baseline/0), not binary search. The SSE-to-AVX2 stability gap is typically 1-3 steps; binary search would overshoot by jumping to the midpoint and then climbing back up.
- "Back off" always means toward baseline (0) — more conservative voltage. For direction=-1 (undervolting), backing off from -38 means trying -37, -36, etc.
- Convergence limit and time budget apply as termination conditions.

### Configurable Hardening Tiers

```python
hardening_tiers = [
    {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
    {"backend": "mprime", "stress_mode": "SSE",  "fft_preset": "LARGE"},
]
```

- Default: two tiers as above
- Users can add tiers (y-cruncher, stress-ng), remove tiers, or reorder
- Empty list: CONFIRMED goes straight to validation (backward compatible with current behavior)
- Each tier runs for `confirm_duration_seconds`

## 2. Death Spiral Prevention

Three termination conditions. A core hits whichever fires first.

### Convergence Limit

When the binary search window (`|fail_bound - pass_bound|`) narrows to `<= fine_step`, the core is settled. Additionally: if 3 consecutive linear backoff attempts fail without a single preconfirm pass, force the midpoint jump immediately regardless of `midpoint_jump_threshold`.

### Time Budget Per Core

New config: `max_core_time_seconds` (default 7200, range 1800-14400).

- Tracks cumulative **test time** per core (not wall clock -- crash gaps don't count)
- Covers discovery + confirmation + backoff phases only
- Hardening and validation are NOT subject to the time budget (they test confirmed values, not searching)
- When exceeded: settle the core at `best_offset` (last passing value)
- The settled value still enters hardening. If hardening can't confirm it, the core settles with a warning flag: "confirmed but NOT hardened -- time budget exceeded during search"

### Crash Penalty

- Soft fail (mprime error, computation error): back off by `fine_step` (1)
- Hard crash (system reboot detected): back off by `crash_penalty_steps` (default: `3 * fine_step`) and immediately enter midpoint jump if not already in binary search
- New config: `crash_penalty_steps` (default 3, range 1-10)

## 3. Crash Detection & Logging

### On Resume -- Crash Event Synthesis

When the tuner resumes and finds `in_test=True` for a core in the database:

1. Calculate gap: `resume_timestamp - last_test_start_timestamp`
2. Log a synthetic test entry:
   - `error_type`: `"crash"` (new enum value)
   - `phase`: the phase the core was in when it crashed
   - `duration`: the gap duration (shows time machine was down)
   - `backend`, `stress_mode`, `fft_preset`: from the enhanced `in_test` DB record
   - `note`: `"System reboot detected (~Xh Ym gap). Offset {offset} caused hard crash."`
3. Apply crash penalty (`crash_penalty_steps` backoff)
4. The crashed offset becomes a hard `fail_bound` -- the algorithm will never try that offset or more aggressive again for this core in this session

### Enhanced in_test DB Record

The existing `in_test` flag is enhanced to store the full test context:
- `core_id`, `offset`, `phase`
- `backend`, `stress_mode`, `fft_preset` (new -- identifies what was running at crash time)
- `start_timestamp`

This replaces the previously proposed heartbeat file. The SQLite DB with WAL mode is already crash-safe and doesn't have the tmpfs loss problem.

### Pre-Crash Safety Ramp

During COARSE_SEARCH, when `current_offset` is within `coarse_step * 2` of `max_offset`:
- Switch from `coarse_step` to `fine_step` for remaining coarse range
- Example: coarse_step=2, max_offset=-50, passing at -44 -> next tests: -45, -46, -47... instead of -46, -48, -50
- Reduces crash risk in the most dangerous territory (deep offsets)

## 4. Crash-Aware Scheduling

### Single-Core Invariant

Hard rule: during discovery, confirmation, and hardening phases, exactly ONE core is under stress at any time. All other cores are at their baseline offset (CO isolation).

Exceptions:
- Validation S2 (all-core simultaneous) -- intentionally multi-core
- Validation S3 (alternating half-core) -- intentionally multi-core
- Validation S4 (rapid transitions) -- intentionally multi-core

### Crash Cooldown

After a crash is detected on resume:
- Set `crash_cooldown = 2` for the crashed core
- Each time the scheduler picks a different core, decrement the cooldown
- When cooldown reaches 0, the core re-enters normal scheduling at its penalized (safer) offset
- Purpose: let other cores progress while the crashed core's safer offset is ready; allows power delivery to stabilize after reboot

### Crash History in Scheduling

**weakest_first** scoring adds crash penalty:
```
score = base_phase_score + (crash_count * 2)
```

Cores with crash history are deprioritized -- they test later when more progress is banked on other cores.

**Cross-session**: when using import profile mode, previous session crash history is loaded as advisory data for scheduling decisions.

## 5. Import Profile Mode

For continuing after BIOS updates or loading known-good values as starting points.

### Session Start Action (Not Config)

Import is a session initialization choice:
- UI: "New Session" dropdown -> "Start Fresh" / "Start from Previous Session" / "Start from File"
- CLI: `--import-profile previous` or `--import-profile /path/to/profile.json`
- Not a persistent config option

### What Qualifies for Import

Only cores that reached CONFIRMED or HARDENED in the source session. Excluded:
- Cores still in coarse/fine search (incomplete discovery)
- Cores settled by time budget (forced value, not truly confirmed)
- Cores still in backoff when session ended (unstable)

### Validation on Import

- Core count mismatch -> **hard block** (different CPU)
- CPU model string mismatch -> **warning**, proceed (formatting differences between kernel versions)
- BIOS version mismatch -> **informational note** (expected case for BIOS update re-validation)
- Source session with 0 confirmed cores -> **reject** (nothing useful to import)

### Import Flow

For each core with an imported value:
1. Set `baseline_offset` from current SMU readout (post-BIOS, likely 0)
2. Set `best_offset` to imported value
3. Enter CONFIRMING at imported offset
4. If confirmation passes -> proceed to hardening tiers normally
5. If confirmation fails -> enter backoff: up to 3 preconfirm attempts (backing off by fine_step each)
   - If any preconfirm passes -> continue normal backoff/confirm flow
   - If 3 consecutive preconfirm failures -> **abandon import** for this core, enter COARSE_SEARCH from scratch (the BIOS change was too significant for re-validation)
6. 3 failures = 3 x 60s + initial 300s = ~6 minutes to validate or abandon per core

For cores NOT in the imported profile -> COARSE_SEARCH from scratch.

### Export Format

```json
{
  "cpu_model": "AMD Ryzen 9 9950X3D",
  "core_count": 16,
  "bios_version": "2.04",
  "source_session_id": "abc123",
  "exported_at": "2026-03-29T21:37:50",
  "primary_backend": "mprime",
  "primary_mode": "SSE",
  "primary_fft": "SMALL",
  "hardened": true,
  "hardening_tiers_passed": ["AVX2:SMALL", "SSE:LARGE"],
  "profile": {
    "0": -38,
    "2": -33,
    "3": -40,
    "11": -31
  }
}
```

The `hardened` and `hardening_tiers_passed` fields are informational. New sessions always run the full pipeline regardless.

### Previous Session Picker (UI)

Lists past sessions with confirmed cores, sorted by recency. Each entry shows:
- Date, confirmed core count, hardened status, BIOS version
- User picks one. Default selection: most recent session with confirmed cores.

## 6. Cross-Core Validation

Validation runs after ALL cores reach HARDENED status. Four stages:

### S1 -- Per-Core With All Offsets (Primary Mode Only)

- All hardened offsets applied simultaneously via SMU
- Stress test each core one at a time in `test_order`
- Duration: `validate_duration_seconds` per core
- **Primary mode only** -- cross-core power delivery interactions are current-draw dependent, not instruction-set dependent. Each core was already individually hardened across all modes.
- On failure: back off the tested core by `fine_step`, restart from S1

### S2 -- All-Core Simultaneous (All Modes)

- All hardened offsets applied, all cores stressed at once
- Runs once per mode: primary + each hardening tier mode
- Duration: `validate_duration_seconds` per mode
- Catches package-level power delivery limits under worst-case all-core load
- On failure: back off most aggressive core by `fine_step`, restart from S1
- On crash: same as failure but apply `crash_penalty_steps` to most aggressive core

### S3 -- Alternating Half-Core Load (All Modes)

- Cores split into two halves (by CCD if multi-CCD, else even/odd)
- Each half loaded while other idles, then swap
- Runs once per mode: primary + each hardening tier mode
- Duration: `validate_duration_seconds` per half per mode
- Catches voltage transients during boost ramp-up/ramp-down
- On failure: back off most aggressive core in the loaded half, restart from S1

### S4 -- Rapid Transition Stress (All Modes, New)

- All hardened offsets applied, all cores simultaneously
- Pattern: 10s full load -> 5s idle -> repeat for 10 minutes per mode
- Tests C-state entry/exit voltage regulation under concurrent ramp
- Catches the most common real-world crash: not sustained load, but idle-to-boost transitions (game loading screen -> intense scene, IDE idle -> build starts)
- On failure: back off most aggressive core, restart from S1
- Default enabled. Config: `validate_transitions = True`

### Validation Time Estimate

With 2 hardening tiers (3 modes total), 16 cores, 300s validate_duration:
- S1: 16 cores x 300s = 80 min (primary mode only)
- S2: 3 modes x 300s = 15 min
- S3: 3 modes x 2 halves x 300s = 30 min
- S4: 3 modes x 10 min = 30 min
- **Total: ~2.5 hours** for validation

### Validation Failure Loop

Any stage failure:
1. Back off the offending core by `fine_step` (or `crash_penalty_steps` if crash)
2. Re-harden that core through T1 + T2 at the new offset (single-core, CO isolated)
3. Restart validation from S1

This ensures the backed-off offset is verified across all modes before re-entering multi-core validation. Continue until all 4 stages pass consecutively or no core can be backed off further (all at baseline).

### S4 Implementation

The rapid transition pattern (10s load → 5s idle) is implemented by the scheduler, not the backend:
- Scheduler launches the stress process for 10s, kills it (SIGTERM → SIGKILL), waits 5s idle, relaunches
- This works with any backend — no backend-specific cycling support needed
- The kill/relaunch cycle itself creates the realistic voltage transient (boost ramp from idle C-state)
- Error detection checks both: stress output during load windows AND MCE/dmesg during idle windows (idle MCEs indicate C-state transition instability)

## 7. New Configuration Options

| Parameter | Default | Range | Purpose |
|-----------|---------|-------|---------|
| `hardening_tiers` | see above | list of tier configs | Stress modes for multi-mode hardening |
| `max_core_time_seconds` | 7200 | 1800-14400 | Per-core time budget for search phases |
| `crash_penalty_steps` | 3 | 1-10 | Backoff multiplier after system crash |
| `validate_transitions` | True | bool | Enable S4 rapid transition validation |

Existing options unchanged. No options removed.

## 8. UI Requirements

### Core Grid Widget

- Show current `backend:mode:fft` for the active test (e.g., "mprime AVX2 SMALL")
- New phase labels: `HARDENING_T1`, `HARDENING_T2`, `HARDENED`
- CRASH events shown with distinct styling (not the same as FAIL)
- Crash count badge per core (e.g., "2 crashes")
- Cores in crash cooldown show cooldown indicator instead of "waiting"
- Cores settled by time budget show warning: "confirmed, NOT hardened -- budget exceeded"
- Import mode: show "RE-CONFIRMING" for imported cores, "REDISCOVERING" if import abandoned

### Test Log

- CRASH entries with distinct formatting and gap duration
- Backend/mode/fft shown for each entry (already partially there, needs hardening tier info)
- Import-related entries labeled (RE-CONFIRMING, IMPORT ABANDONED)

### Session Status

- Show pipeline stage: "DISCOVERY", "CONFIRMING", "HARDENING", "VALIDATING S1/S2/S3/S4"
- After crash resume: "RESUMED AFTER CRASH" status with crash details
- Import mode: "IMPORTED PROFILE (12/16 cores from session 2026-03-29)"

### History/Results

- Validation stage breakdown visible in results
- Per-core history shows which modes passed/failed at which offsets
- Export button produces the JSON format defined in Section 5

## 9. Backward Compatibility

- Empty `hardening_tiers` list = current behavior (CONFIRMED -> validation, no hardening)
- `validate_transitions = False` = current 3-stage validation (no S4)
- `crash_penalty_steps = 1` = current behavior (same as fine_step backoff)
- `max_core_time_seconds = 14400` (4h) effectively disables time budget for most runs
- Import profile is opt-in (session start action, not default)
- All existing config options retain their defaults and behavior

## 10. Estimated Pipeline Duration

### Fresh Start (16 cores, default settings)

| Phase | Duration Estimate |
|-------|------------------|
| Discovery (coarse + fine) | ~5-7 hours |
| Confirmation | ~1.5 hours |
| Hardening T1 + T2 | ~2-3 hours (includes backoff) |
| Validation S1-S4 | ~2.5 hours |
| **Total (happy path)** | **~12-14 hours** |
| **Total (with failures)** | **~16-24 hours** |

### Import Profile (BIOS update, 16 cores)

| Phase | Duration Estimate |
|-------|------------------|
| Re-confirmation | ~1.5 hours (6 min per abandoned core) |
| Hardening T1 + T2 | ~2-3 hours |
| Validation S1-S4 | ~2.5 hours |
| **Total (happy path)** | **~6-7 hours** |

Compared to current behavior: Run 2 took 2+ days and didn't complete, with death spirals and no multi-mode verification. This design completes faster AND produces more trustworthy results.
