# Tuner core state machine — transition specification

The per-core state machine's ALLOWED transition relation, declared as data
and exhaustively executed by `tests/test_state_transition_spec.py`: every
(phase x outcome x offset-scenario) combination is driven through the real
`_advance_core` / `_apply_crash_penalty`, and any transition outside this
chart fails the suite. Safety invariants are asserted after every single
transition. `docs/test-order-spec.md` covers which core gets tested next;
this chart covers what happens to a core once it has a verdict.

## Verdict transitions (`_advance_core`)

| Phase | PASS -> | FAIL -> |
|---|---|---|
| NOT_STARTED | COARSE_SEARCH (entry step, verdict ignored) | COARSE_SEARCH |
| COARSE_SEARCH | COARSE_SEARCH, SETTLED (hit max) | FINE_SEARCH, SETTLED |
| FINE_SEARCH | FINE_SEARCH, SETTLED | SETTLED |
| SETTLED | CONFIRMING, CONFIRMED (no best found) | same |
| CONFIRMING | CONFIRMED (HARDENING_T1 with tiers) | CONFIRMING (retry), FAILED_CONFIRM |
| FAILED_CONFIRM | BACKOFF_PRECONFIRM, CONFIRMED (at baseline) | same |
| BACKOFF_PRECONFIRM | BACKOFF_PRECONFIRM (midpoint probe), BACKOFF_CONFIRMING, CONFIRMED (converged; HARDENING_T1 with tiers) | BACKOFF_PRECONFIRM, CONFIRMED (floor/baseline) |
| BACKOFF_CONFIRMING | CONFIRMED, BACKOFF_PRECONFIRM (midpoint; HARDENING_T1 with tiers) | BACKOFF_PRECONFIRM, CONFIRMED |
| HARDENING_T1 | HARDENING_T2, HARDENED | HARDENING_T1 (backed off), HARDENED (at baseline) |
| HARDENING_T2 | HARDENING_T1 (next tier), HARDENED | HARDENING_T2 (backed off), HARDENED |
| CONFIRMED | CONFIRMED (absorbing) | CONFIRMED |
| HARDENED | HARDENED (absorbing) | HARDENED |

## Hard-crash transitions (`_apply_crash_penalty`)

Every search/confirm/terminal phase is forced into BACKOFF_PRECONFIRM (a
crash invalidates any confirmation); NOT_STARTED, SETTLED, FAILED_CONFIRM and
BACKOFF_CONFIRMING keep their phase while the offsets back off. After every
crash penalty: `best_offset` is set (never None), never more aggressive than
the penalized current, the crashed value is a hard fail bound, and the
penalty never overshoots past stock (CO=0).

## Guards that make the function total

- **Contradictory evidence**: a PASS at/beyond the recorded fail bound must
  not widen the bounds (failures outrank passes) — otherwise the backoff
  binary search diverges toward more aggressive values. The pass is dropped
  and the search steps back to just inside the fail bound.
- **Normalization**: a persisted backoff-phase row with `best_offset = NULL`
  (older versions, hand edits) is normalized to the baseline instead of
  crashing the arithmetic.
- **Persistence boundary**: reading or writing a core state with offsets
  outside the sane CO range or negative counters raises — corruption is
  rejected at the boundary, in both directions.

## Invariants (asserted after every transition in the sweep)

1. Offsets never exceed `max_offset` in the aggressive direction.
2. `backoff_pass_bound` is never more aggressive than `backoff_fail_bound`.
3. Counters never go negative.
4. Every produced state passes the persistence-boundary sanity guard.

## Verdict classes that never enter this state machine

- `thermal` — cool down and retry the same offset (no transition).
- `startup` — environment fault: revert the offset, persist `in_test=0`,
  pause. Never logged as a verdict, never marks the journal survived.
- Apparatus-breaker trips (implausible fail streaks, search flow only) —
  roll back to the most aggressive proven pass, re-enter CONFIRMING, pause.

## Crash attribution on resume (`_attribute_crash_after_reboot`)

Evidence outranks policy; a guess is never written. Priority order:

1. Kernel-journal forensics (`journalctl -k` since the session's last
   activity, cross-boot): penalize exactly the cores the kernel's MCE lines
   name, anchored at their journaled resident values.
2. A persisted hunt slot (`tuner_sessions.hunting_core`): the box died while
   one core was stressed alone with every other core at stock — proof by
   isolation.
3. A single in-test core in the SEARCH flow (isolation mode): direct blame.
4. The CO journal's un-survived residents.
5. Anything ambiguous (multi-core in-test set, or any crash under
   validation): penalize NOBODY; run the isolated crash hunt — per-core
   slots at the tuned value with all other cores at stock, most suspect
   first (prior MCE rows, crash history, deepest undervolt). A slot failure
   convicts its core. After `max_unattributed_crash_hunts` fruitless hunts
   in a row the session pauses for the owner.

Cross-core MCE evidence during a live test uses `_apply_crash_penalty` with
`steps=1, count_crash=False` for corrected errors (one-step backoff, re-earn
confirmation, journal kept un-survived) and the full penalty for uncorrected
ones — the same declared transition relation, so the chart above holds.
