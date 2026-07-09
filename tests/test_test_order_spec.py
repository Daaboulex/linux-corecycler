"""Executable spec for the five test orders — docs/test-order-spec.md.

Each test enforces one row or invariant of the chart. The step() helper
mirrors exactly the cursor updates _run_next performs after picking, so the
selectors are exercised the way the engine drives them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from history.db import HistoryDB
from tuner import persistence as tp
from tuner.config import TunerConfig
from tuner.engine import TunerEngine
from tuner.state import CoreState, TunerPhase

ORDERS = ["sequential", "round_robin", "weakest_first", "ccd_alternating", "ccd_round_robin"]

ACTIVE = TunerPhase.COARSE_SEARCH
TERMINAL = TunerPhase.CONFIRMED


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def make_engine(db, topo, order: str) -> TunerEngine:
    cfg = TunerConfig(test_order=order, cores_to_test=sorted(topo.cores))
    return TunerEngine(db=db, topology=topo, smu=None, backend=MagicMock(), config=cfg)


def seed(eng, phases: dict[int, TunerPhase], cooldowns: dict[int, int] | None = None,
         crash_counts: dict[int, int] | None = None) -> None:
    eng._core_states = {
        c: CoreState(
            core_id=c, phase=p,
            crash_cooldown=(cooldowns or {}).get(c, 0),
            crash_count=(crash_counts or {}).get(c, 0),
        )
        for c, p in phases.items()
    }


def step(eng) -> int | None:
    """Pick a core and advance the cursors the way _run_next does."""
    core = eng._pick_next_core()
    if core is None:
        return None
    eng._decrement_cooldowns(core)
    eng._last_tested_core = core
    info = eng._topology.cores.get(core)
    if info and info.ccd is not None:
        eng._ccd_last_tested[info.ccd] = core
    return core


def ccd_of(eng, core: int) -> int:
    info = eng._topology.cores.get(core)
    return info.ccd if info and info.ccd is not None else 0


# ---------------------------------------------------------------------------
# Invariants for EVERY order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("order", ORDERS)
class TestGlobalInvariants:
    def test_never_picks_terminal_or_cooling(self, db, topo_dual_ccd_x3d, order):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(eng, {0: TERMINAL, 1: TunerPhase.HARDENED, 2: ACTIVE, 3: ACTIVE,
                   4: ACTIVE, 5: TERMINAL, 6: ACTIVE, 7: ACTIVE},
             cooldowns={2: 2, 6: 1})
        for _ in range(20):
            core = step(eng)
            assert core is not None
            cs = eng._core_states[core]
            assert cs.phase not in (TunerPhase.CONFIRMED, TunerPhase.HARDENED)
            # cooldown must have been 0 at pick time (step decrements others after)
            assert cs.crash_cooldown == 0

    def test_all_terminal_returns_none(self, db, topo_dual_ccd_x3d, order):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(eng, dict.fromkeys(range(8), TERMINAL))
        assert eng._pick_next_core() is None

    def test_liveness_when_all_actives_cooling(self, db, topo_dual_ccd_x3d, order):
        """Invariant 4: pick None + non-terminal cores cooling -> draining
        cooldowns must eventually yield a pick (no deadlock while work remains)."""
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(eng, {0: ACTIVE, 1: TERMINAL, 2: ACTIVE, 3: TERMINAL,
                   4: TERMINAL, 5: TERMINAL, 6: TERMINAL, 7: TERMINAL},
             cooldowns={0: 3, 2: 2})
        assert eng._pick_next_core() is None
        for _ in range(10):  # the _run_next drain loop, bounded
            if eng._pick_next_core() is not None:
                break
            for cs in eng._core_states.values():
                if cs.crash_cooldown > 0:
                    cs.crash_cooldown -= 1
        assert eng._pick_next_core() is not None

    def test_pick_decrements_other_cooldowns(self, db, topo_dual_ccd_x3d, order):
        eng = make_engine(db, topo_dual_ccd_x3d, order)
        seed(eng, dict.fromkeys(range(8), ACTIVE), cooldowns={5: 2})
        picked = step(eng)
        assert picked != 5
        assert eng._core_states[5].crash_cooldown == 1


# ---------------------------------------------------------------------------
# Per-order behavior
# ---------------------------------------------------------------------------


class TestSequentialSpec:
    def test_lowest_active_and_stays_until_terminal(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "sequential")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        assert step(eng) == 0
        assert step(eng) == 0  # stays on core 0 while it is active
        eng._core_states[0].phase = TERMINAL
        assert step(eng) == 1  # then moves to the next lowest


class TestRoundRobinSpec:
    def test_full_round_visits_each_core_once_cyclically(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        picks = [step(eng) for _ in range(8)]
        assert picks == [0, 1, 2, 3, 4, 5, 6, 7]
        assert step(eng) == 0  # wraps

    def test_missing_cursor_starts_at_first_available(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._last_tested_core = 3
        eng._core_states[3].phase = TERMINAL  # cursor core became terminal
        assert step(eng) == 4  # continues after it, not from 0


class TestWeakestFirstSpec:
    def test_phase_priority(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "weakest_first")
        seed(eng, {0: TunerPhase.NOT_STARTED, 1: TunerPhase.COARSE_SEARCH,
                   2: TunerPhase.FINE_SEARCH, 3: TunerPhase.CONFIRMING,
                   4: TERMINAL, 5: TERMINAL, 6: TERMINAL, 7: TERMINAL})
        assert step(eng) == 2  # FINE_SEARCH (0) < CONFIRMING (1) < COARSE (2) < NOT_STARTED (4)

    def test_crash_count_deprioritizes(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "weakest_first")
        seed(eng, {0: TunerPhase.FINE_SEARCH, 1: TunerPhase.FINE_SEARCH,
                   2: TERMINAL, 3: TERMINAL, 4: TERMINAL, 5: TERMINAL,
                   6: TERMINAL, 7: TERMINAL},
             crash_counts={0: 2})
        assert step(eng) == 1  # same phase, but core 0 carries 2*2 crash penalty


class TestCcdAlternatingSpec:
    def test_alternates_while_both_ccds_have_work(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_alternating")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        picks = [step(eng) for _ in range(6)]
        ccds = [ccd_of(eng, c) for c in picks]
        for a, b in zip(ccds, ccds[1:], strict=False):
            assert a != b, f"consecutive picks on same CCD: {picks}"

    def test_drains_remaining_ccd_when_other_done(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_alternating")
        seed(eng, {0: ACTIVE, 1: ACTIVE, 2: TERMINAL, 3: TERMINAL,
                   4: TERMINAL, 5: TERMINAL, 6: TERMINAL, 7: TERMINAL})
        eng._last_tested_core = 0
        assert ccd_of(eng, step(eng)) == 0  # only CCD0 has work — no deadlock


class TestCcdRoundRobinSpec:
    def test_alternates_ccds_and_rotates_within(self, db, topo_dual_ccd_x3d):
        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        picks = [step(eng) for _ in range(8)]
        ccds = [ccd_of(eng, c) for c in picks]
        for a, b in zip(ccds, ccds[1:], strict=False):
            assert a != b
        # every core exactly once per full round
        assert sorted(picks) == list(range(8))

    def test_single_ccd_degrades_to_round_robin(self, db, topo_single_ccd):
        eng = make_engine(db, topo_single_ccd, "ccd_round_robin")
        cores = sorted(topo_single_ccd.cores)
        seed(eng, dict.fromkeys(cores, ACTIVE))
        picks = [step(eng) for _ in range(len(cores))]
        assert picks == cores


# ---------------------------------------------------------------------------
# Interruption contract
# ---------------------------------------------------------------------------


class TestInterruptionContract:
    def _log_real_test(self, db, sid, core, offset=-10):
        tp.log_test_result(db, sid, core, offset, "coarse", True, duration=60.0)

    def test_cursors_rebuilt_from_test_log(self, db, topo_dual_ccd_x3d):
        sid = tp.create_session(db, TunerConfig(), "", "")
        for core in (0, 4, 1):
            self._log_real_test(db, sid, core)
        # synthetic crash row (duration NULL) must NOT move the cursor
        tp.log_test_result(db, sid, 6, -20, "coarse", False,
                           error_type="crash", duration=None)

        eng = make_engine(db, topo_dual_ccd_x3d, "ccd_round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._session_id = sid
        eng._reconstruct_scheduling_position()

        assert eng._last_tested_core == 1          # last REAL test
        assert eng._ccd_last_tested == {0: 1, 1: 4}

    def test_round_robin_continues_after_resume(self, db, topo_dual_ccd_x3d):
        sid = tp.create_session(db, TunerConfig(), "", "")
        for core in (0, 1, 2):
            self._log_real_test(db, sid, core)
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._session_id = sid
        eng._reconstruct_scheduling_position()
        assert step(eng) == 3  # continues after core 2, not back at core 0

    def test_no_test_log_leaves_cursors_unset(self, db, topo_dual_ccd_x3d):
        sid = tp.create_session(db, TunerConfig(), "", "")
        eng = make_engine(db, topo_dual_ccd_x3d, "round_robin")
        seed(eng, dict.fromkeys(range(8), ACTIVE))
        eng._session_id = sid
        eng._reconstruct_scheduling_position()
        assert eng._last_tested_core is None
        assert step(eng) == 0
