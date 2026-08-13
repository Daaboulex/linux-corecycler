"""Exhaustive transition relation for the tuner core state machine.

Control-system style: the ALLOWED transition relation is declared as data,
then EVERY (phase x outcome x offset-scenario) combination is driven through
the real _advance_core / _apply_crash_penalty and asserted to land inside the
relation, with the safety invariants holding after every single transition.
An undeclared transition — including one added by a future code change — is a
test failure, not a silent new behavior. docs/tuner-state-spec.md renders
this relation as a chart; this file is its executable source of truth.
"""

from __future__ import annotations

from itertools import product
from unittest.mock import MagicMock

import pytest

from corecycler.history.db import HistoryDB
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.state import CoreState, TunerPhase

P = TunerPhase

# (phase, passed) -> allowed next phases, for direction=-1 (undervolt), no tiers
ADVANCE_RELATION: dict[tuple[TunerPhase, bool], set[TunerPhase]] = {
    (P.NOT_STARTED, True): {P.COARSE_SEARCH},
    (P.NOT_STARTED, False): {P.COARSE_SEARCH},  # entry step ignores the verdict
    (P.COARSE_SEARCH, True): {P.COARSE_SEARCH, P.SETTLED},
    (P.COARSE_SEARCH, False): {P.FINE_SEARCH, P.SETTLED},
    (P.FINE_SEARCH, True): {P.FINE_SEARCH, P.SETTLED},
    (P.FINE_SEARCH, False): {P.SETTLED},
    (P.SETTLED, True): {P.CONFIRMING, P.CONFIRMED},
    (P.SETTLED, False): {P.CONFIRMING, P.CONFIRMED},
    (P.CONFIRMING, True): {P.CONFIRMED},
    (P.CONFIRMING, False): {P.CONFIRMING, P.FAILED_CONFIRM},
    (P.FAILED_CONFIRM, True): {P.BACKOFF_PRECONFIRM, P.CONFIRMED},
    (P.FAILED_CONFIRM, False): {P.BACKOFF_PRECONFIRM, P.CONFIRMED},
    (P.BACKOFF_PRECONFIRM, True): {P.BACKOFF_PRECONFIRM, P.BACKOFF_CONFIRMING, P.CONFIRMED},
    (P.BACKOFF_PRECONFIRM, False): {P.BACKOFF_PRECONFIRM, P.CONFIRMED},
    (P.BACKOFF_CONFIRMING, True): {P.CONFIRMED, P.BACKOFF_PRECONFIRM},
    (P.BACKOFF_CONFIRMING, False): {P.BACKOFF_PRECONFIRM, P.CONFIRMED},
    # terminal phases are absorbing under _advance_core
    (P.CONFIRMED, True): {P.CONFIRMED},
    (P.CONFIRMED, False): {P.CONFIRMED},
    (P.HARDENED, True): {P.HARDENED},
    (P.HARDENED, False): {P.HARDENED},
    # hardening (only reachable with tiers configured; asserted separately too)
    (P.HARDENING_T1, True): {P.HARDENING_T2, P.HARDENED},
    (P.HARDENING_T1, False): {P.HARDENING_T1, P.HARDENED},
    (P.HARDENING_T2, True): {P.HARDENING_T1, P.HARDENED},
    (P.HARDENING_T2, False): {P.HARDENING_T2, P.HARDENED},
}

# With hardening tiers configured, confirmation exits into hardening instead
TIERED_EXTRAS: dict[tuple[TunerPhase, bool], set[TunerPhase]] = {
    (P.CONFIRMING, True): {P.HARDENING_T1},
    (P.BACKOFF_PRECONFIRM, True): {P.HARDENING_T1},
    (P.BACKOFF_CONFIRMING, True): {P.HARDENING_T1},
}

# phase -> allowed phase after a hard-crash penalty
CRASH_RELATION: dict[TunerPhase, set[TunerPhase]] = {
    P.COARSE_SEARCH: {P.BACKOFF_PRECONFIRM},
    P.FINE_SEARCH: {P.BACKOFF_PRECONFIRM},
    P.CONFIRMING: {P.BACKOFF_PRECONFIRM},
    P.CONFIRMED: {P.BACKOFF_PRECONFIRM},
    P.BACKOFF_PRECONFIRM: {P.BACKOFF_PRECONFIRM},
    P.HARDENING_T1: {P.BACKOFF_PRECONFIRM},
    P.HARDENING_T2: {P.BACKOFF_PRECONFIRM},
    P.HARDENED: {P.BACKOFF_PRECONFIRM},
    # phases that keep their phase under the penalty (offsets still back off)
    P.NOT_STARTED: {P.NOT_STARTED},
    P.SETTLED: {P.SETTLED},
    P.FAILED_CONFIRM: {P.FAILED_CONFIRM},
    P.BACKOFF_CONFIRMING: {P.BACKOFF_CONFIRMING},
}

MAX_OFFSET = -30
BASELINES = (0, -10)
CURRENTS = (MAX_OFFSET, -20, -5, 0)
BESTS = (None, -18, -4)
FAIL_BOUNDS = (None, -22)


def make_engine(db, hardening: bool) -> TunerEngine:
    from corecycler.engine.topology import CPUTopology, PhysicalCore

    topo = CPUTopology()
    topo.cores[0] = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))
    topo.ccds = 1
    tiers = [{"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"}] if hardening else []
    cfg = TunerConfig(
        cores_to_test=[0],
        coarse_step=5,
        fine_step=1,
        max_offset=MAX_OFFSET,
        hardening_tiers=tiers,
        max_confirm_retries=2,
        midpoint_jump_threshold=3,
    )
    return TunerEngine(db=db, topology=topo, smu=None, backend=MagicMock(), config=cfg)


def scenarios():
    """Every (phase, outcome, offsets) combination that is representable."""
    yield from product(list(TunerPhase), (True, False), BASELINES, CURRENTS, BESTS, FAIL_BOUNDS)


def check_invariants(eng: TunerEngine, cs: CoreState, label: str) -> None:
    # Offsets never exceed the configured max in the aggressive direction
    assert not eng._exceeds_max(cs.current_offset), f"{label}: current beyond max"
    if cs.best_offset is not None:
        assert not eng._exceeds_max(cs.best_offset), f"{label}: best beyond max"
    # A proven pass bound is never MORE aggressive than the fail bound
    if cs.backoff_pass_bound is not None and cs.backoff_fail_bound is not None:
        assert not eng._is_more_aggressive(cs.backoff_pass_bound, cs.backoff_fail_bound), (
            f"{label}: pass bound more aggressive than fail bound"
        )
    # Counters never go negative
    assert cs.confirm_attempts >= 0 and cs.crash_count >= 0, label
    # The persistence boundary accepts the state (same guard the DB applies)
    HistoryDB._check_core_state_sane(cs)


@pytest.mark.parametrize("hardening", [False, True])
def test_every_transition_is_declared(hardening):
    """Drive the REAL _advance_core over the full scenario grid."""
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db, hardening)
        covered: set[tuple[TunerPhase, bool]] = set()
        for phase, passed, baseline, current, best, fail_bound in scenarios():
            cs = CoreState(
                core_id=0,
                phase=phase,
                current_offset=current,
                best_offset=best,
                baseline_offset=baseline,
                backoff_fail_bound=fail_bound,
                backoff_mode=phase in (P.BACKOFF_PRECONFIRM, P.BACKOFF_CONFIRMING),
            )
            eng._core_states = {0: cs}
            label = (
                f"{phase}/{'pass' if passed else 'fail'} "
                f"b={baseline} c={current} best={best} fb={fail_bound} "
                f"tiers={hardening}"
            )

            eng._advance_core(0, passed)

            allowed = set(ADVANCE_RELATION[(phase, passed)])
            if hardening:
                allowed |= TIERED_EXTRAS.get((phase, passed), set())
            assert cs.phase in allowed, (
                f"UNDECLARED TRANSITION {label}: {phase} -> {cs.phase} (allowed: {sorted(p.value for p in allowed)})"
            )
            check_invariants(eng, cs, label)
            covered.add((phase, passed))
        # the grid exercised every declared (phase, outcome) pair
        assert covered == set(ADVANCE_RELATION), "declared relation not fully exercised"
    finally:
        db.close()


def test_every_crash_penalty_transition_is_declared():
    """Drive the REAL _apply_crash_penalty over every phase and offset combo."""
    db = HistoryDB(":memory:")
    try:
        eng = make_engine(db, hardening=False)
        covered: set[TunerPhase] = set()
        for phase, _passed, baseline, current, best, fail_bound in scenarios():
            cs = CoreState(
                core_id=0,
                phase=phase,
                current_offset=current,
                best_offset=best,
                baseline_offset=baseline,
                backoff_fail_bound=fail_bound,
            )
            eng._core_states = {0: cs}
            label = f"crash@{phase} b={baseline} c={current} best={best} fb={fail_bound}"

            eng._apply_crash_penalty(cs)

            assert cs.phase in CRASH_RELATION[phase], f"UNDECLARED CRASH TRANSITION {label}: {phase} -> {cs.phase}"
            # A crash always produces a usable, bounded state:
            assert cs.best_offset is not None, f"{label}: best is None after crash"
            assert cs.crash_count >= 1 and cs.crash_cooldown >= 1, label
            # The crashed value became a hard fail bound at least that aggressive
            assert cs.backoff_fail_bound is not None, label
            # Never overshoots stock in the opposite direction
            assert not eng._is_more_aggressive(0, cs.current_offset) or (cs.current_offset == 0), (
                f"{label}: penalty overshot past stock"
            )
            # best is never left more aggressive than the penalized current
            assert not eng._is_more_aggressive(cs.best_offset, cs.current_offset), (
                f"{label}: best={cs.best_offset} more aggressive than current={cs.current_offset} after crash"
            )
            check_invariants(eng, cs, label)
            covered.add(phase)
        assert covered == set(CRASH_RELATION)
    finally:
        db.close()
