# Test-order specification

The auto-tuner picks the next core to test with one of five orders
(`TunerConfig.test_order`). This is the control-system contract for all five:
what each order must do, what state it carries, and what happens across an
interruption. `tests/test_test_order_spec.py` executes every row of this
chart; a behavior change that is not reflected here fails the suite.

## State the selectors read and write

| State | Kind | Written by | Survives restart? |
|---|---|---|---|
| `CoreState.phase` | persisted (DB) | state machine | yes |
| `CoreState.crash_cooldown` | persisted (DB) | crash penalty / thermal defer | yes |
| `CoreState.crash_count` | persisted (DB) | crash penalty | yes |
| `_last_tested_core` | in-memory cursor | `_run_next` | rebuilt from test log |
| `_ccd_last_tested` | in-memory cursor | `_run_next` | rebuilt from test log |

A core is **available** iff `phase not in (CONFIRMED, HARDENED)` and
`crash_cooldown == 0`.

## The five orders

| Order | Next pick | Cursor state | After interruption (resume) |
|---|---|---|---|
| `sequential` | lowest-id available core; stays on it until that core reaches a terminal phase | none | derived from persisted phases alone |
| `round_robin` | next available core after the last tested one, cyclic ascending; first available if the cursor is gone | `_last_tested_core` | cursor = core of the last REAL test-log row |
| `weakest_first` | minimum of `phase_score + 2 * crash_count`; tie broken by lowest core id | none (derived) | derived from persisted phases and crash counts |
| `ccd_alternating` | a CCD different from the last-tested one whenever it has available work; among candidate CCDs the fewest-confirmed (then lowest index); lowest core id within | `_last_tested_core` | cursor rebuilt from test log |
| `ccd_round_robin` | alternate CCD from the last-tested one; within the chosen CCD rotate to the core after that CCD's last-tested; fewer than 2 CCDs degrades to `round_robin` | `_last_tested_core` + `_ccd_last_tested` | both cursors rebuilt from test log |

`weakest_first` phase scores (lower = sooner): FINE_SEARCH / FAILED_CONFIRM /
BACKOFF_PRECONFIRM / HARDENING_T1 / HARDENING_T2 = 0, BACKOFF_CONFIRMING /
CONFIRMING = 1, COARSE_SEARCH = 2, SETTLED = 3, NOT_STARTED = 4.

## Invariants that hold for EVERY order

1. Never picks a core in a terminal phase (CONFIRMED, HARDENED).
2. Never picks a core with `crash_cooldown > 0`.
3. Picking a core decrements every OTHER core's cooldown by 1.
4. `pick == None` means either every core is terminal (the session completes)
   or every non-terminal core is cooling — then all cooldowns drain by 1 and
   the pick repeats, so the tuner can never deadlock while work remains.
5. The picked core is flagged `in_test` and persisted BEFORE its worker
   starts (crash attribution), and cleared on any delivered verdict.

## Interruption contract

- The cursors are in-memory only; the test log is their source of truth.
  `_reconstruct_scheduling_position()` rebuilds them on resume so cycling
  continues where it stopped instead of restarting at core 0.
- Synthetic crash-recovery rows (logged with `duration_seconds = NULL`) never
  move the cursors — they record a reboot, not a test.
- Crash penalties on resume only apply when the machine actually rebooted
  since the session's last persisted write (`_rebooted_since`); a plain app
  exit mid-test clears `in_test` without a penalty and without moving offsets.
