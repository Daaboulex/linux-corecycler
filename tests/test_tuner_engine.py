"""Tests for the tuner engine state machine."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from history.db import HistoryDB
from tuner.config import TunerConfig
from tuner.engine import TunerEngine
from tuner.state import CoreState, TunerPhase
from tuner import persistence as tp


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def simple_topology(topo_single_ccd):
    """4-core single CCD topology."""
    return topo_single_ccd


@pytest.fixture
def mock_smu():
    smu = MagicMock()
    smu.commands = MagicMock()
    smu.commands.co_range = (-60, 10)
    smu.set_co_offset = MagicMock(return_value=True)
    smu.get_co_offset = MagicMock(return_value=0)
    smu.get_all_co_offsets = MagicMock(return_value={0: 0, 1: 0, 2: 0, 3: 0})
    smu.get_pbo_scalar = MagicMock(return_value=1.0)
    smu.get_boost_limit = MagicMock(return_value=5500)
    return smu


@pytest.fixture
def engine(db, simple_topology, mock_smu, mock_backend):
    """Engine with mocked dependencies — does NOT auto-start."""
    cfg = TunerConfig(
        coarse_step=5,
        fine_step=1,
        max_offset=-30,
        search_duration_seconds=1,
        confirm_duration_seconds=1,
        cores_to_test=[0, 1],
    )
    eng = TunerEngine(
        db=db,
        topology=simple_topology,
        smu=mock_smu,
        backend=mock_backend,
        config=cfg,
    )
    return eng


class TestStateMachineTransitions:
    """Unit-test _advance_core with direct state manipulation."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        return TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

    def test_not_started_enters_coarse(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.NOT_STARTED, current_offset=0)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.COARSE_SEARCH
        assert cs.current_offset == -5  # 0 + (-1)*5

    def test_coarse_pass_goes_more_aggressive(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.best_offset == -5
        assert cs.current_offset == -10

    def test_coarse_pass_at_max_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_offset=-10)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.SETTLED
        assert cs.best_offset == -10

    def test_coarse_fail_enters_fine_search(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, best_offset=-5)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.FINE_SEARCH
        assert cs.coarse_fail_offset == -10
        assert cs.current_offset == -6  # best(-5) + direction(-1)*fine(1) = -6

    def test_coarse_fail_no_best_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5, best_offset=None)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.SETTLED

    def test_fine_pass_continues(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-6,
            best_offset=-5, coarse_fail_offset=-10,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.FINE_SEARCH
        assert cs.best_offset == -6
        assert cs.current_offset == -7

    def test_fine_pass_at_coarse_fail_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-9,
            best_offset=-8, coarse_fail_offset=-10,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # next would be -10 which equals coarse_fail, so settle
        assert cs.phase == TunerPhase.SETTLED
        assert cs.best_offset == -9

    def test_fine_fail_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-7,
            best_offset=-6, coarse_fail_offset=-10,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.SETTLED

    def test_settled_triggers_confirm(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.SETTLED, current_offset=-8, best_offset=-8)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)  # passed doesn't matter for settled
        assert cs.phase == TunerPhase.CONFIRMING
        assert cs.current_offset == -8

    def test_confirm_pass_marks_confirmed(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, hardening_tiers=[])
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-8, best_offset=-8)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.CONFIRMED

    def test_confirm_fail_retries(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_confirm_retries=3)
        cs = CoreState(
            core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-8,
            best_offset=-8, confirm_attempts=0,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.CONFIRMING  # retry, not failed yet
        assert cs.confirm_attempts == 1

    def test_confirm_max_retries_backs_off(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_confirm_retries=2)
        cs = CoreState(
            core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-8,
            best_offset=-8, confirm_attempts=1,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.confirm_attempts == 2
        assert cs.phase == TunerPhase.FAILED_CONFIRM

    def test_failed_confirm_enters_backoff(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.FAILED_CONFIRM, current_offset=-8,
            best_offset=-8, confirm_attempts=2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off: best was -8, direction=-1, so back off = -8 - (-1)*1 = -7
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.best_offset == -7
        assert cs.backoff_mode is True
        assert cs.confirm_attempts == 0

    def test_max_offset_clamp(self, db, simple_topology, mock_smu, mock_backend):
        # At max_offset itself, next step (even fine_step in ramp zone) exceeds max — settle.
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_offset=-7)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-7)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # distance=0 <= ramp_zone=10, so fine_step=1 used: next=-8 exceeds max(-7) → settle
        assert cs.phase == TunerPhase.SETTLED
        assert cs.best_offset == -7


class TestResumeFromCrash:
    def test_resume_loads_saved_state(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(cores_to_test=[0, 1], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

        # Create a session with saved state
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-20, best_offset=-20,
        ))
        tp.save_core_state(db, sid, CoreState(
            core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, best_offset=-5,
            in_test=True,  # was actively testing when crash happened
        ))

        # Patch _run_next to prevent actual test execution
        with patch.object(eng, "_run_next"):
            eng.resume(sid)

        assert eng._session_id == sid
        assert len(eng._core_states) == 2
        assert eng._core_states[0].phase == TunerPhase.CONFIRMED
        # Core 1 was actively testing (in_test=True) — treated as failure
        assert eng._core_states[1].phase != TunerPhase.COARSE_SEARCH

    def test_resume_reapplies_baseline_offsets(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(cores_to_test=[0], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-12,
            best_offset=-10, baseline_offset=-5,
            in_test=True,  # was actively testing when crash happened
        ))

        with patch.object(eng, "_run_next"):
            eng.resume(sid)

        # SMU should restore to baseline (not the interrupted offset)
        mock_smu.set_co_offset.assert_any_call(0, -5)


    def test_resume_does_not_advance_queued_cores(self, db, simple_topology, mock_smu, mock_backend):
        """Cores queued in active phases (not in_test) should NOT be advanced on resume."""
        cfg = TunerConfig(cores_to_test=[0, 1], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

        sid = tp.create_session(db, cfg, "", "")
        # Core 0 was actively testing when crash happened
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-15,
            best_offset=-12, in_test=True,
        ))
        # Core 1 was queued for fine search (not actively testing)
        tp.save_core_state(db, sid, CoreState(
            core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-10,
            best_offset=-9, coarse_fail_offset=-12,
        ))

        with patch.object(eng, "_run_next"):
            eng.resume(sid)

        # Core 0 was in_test — should be advanced (fail → fine_search)
        assert eng._core_states[0].phase != TunerPhase.COARSE_SEARCH
        # Core 1 was NOT in_test — should remain in fine_search at -10
        assert eng._core_states[1].phase == TunerPhase.FINE_SEARCH
        assert eng._core_states[1].current_offset == -10


class TestConfigVariations:
    def test_abort_on_consecutive_failures(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(
            cores_to_test=[0, 1, 2],
            abort_on_consecutive_failures=2,
            search_duration_seconds=1,
        )
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0),
            1: CoreState(core_id=1),
            2: CoreState(core_id=2),
        }
        eng._consecutive_start_failures = 2
        eng._set_status("running")

        # _run_next should abort
        eng._run_next()
        assert eng.status == "idle"


class TestPickNextCore:
    def test_sequential_picks_first_unfinished(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(cores_to_test=[0, 1, 2], test_order="sequential")
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.CONFIRMED),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5),
            2: CoreState(core_id=2, phase=TunerPhase.NOT_STARTED),
        }
        picked = eng._pick_next_core()
        assert picked == 1

    def test_sequential_returns_none_when_all_done(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(cores_to_test=[0, 1], test_order="sequential")
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.CONFIRMED),
            1: CoreState(core_id=1, phase=TunerPhase.CONFIRMED),
        }
        picked = eng._pick_next_core()
        assert picked is None


class TestPickFunctionsPure:
    """Verify pick functions are pure selectors — no state mutation."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0, 1, 2])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        return eng

    def test_sequential_does_not_advance_not_started(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="sequential")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.NOT_STARTED),
            1: CoreState(core_id=1, phase=TunerPhase.NOT_STARTED),
        }
        picked = eng._pick_next_core()
        assert picked == 0
        assert eng._core_states[0].phase == TunerPhase.NOT_STARTED

    def test_sequential_does_not_advance_settled(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="sequential")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.CONFIRMED),
            1: CoreState(core_id=1, phase=TunerPhase.SETTLED, current_offset=-8, best_offset=-8),
        }
        picked = eng._pick_next_core()
        assert picked == 1
        assert eng._core_states[1].phase == TunerPhase.SETTLED

    def test_round_robin_does_not_advance(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="round_robin")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.NOT_STARTED),
            1: CoreState(core_id=1, phase=TunerPhase.SETTLED, current_offset=-8, best_offset=-8),
            2: CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-5),
        }
        eng._pick_next_core()
        assert eng._core_states[0].phase == TunerPhase.NOT_STARTED
        assert eng._core_states[1].phase == TunerPhase.SETTLED

    def test_weakest_first_does_not_advance(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="weakest_first")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.NOT_STARTED),
            1: CoreState(core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-6, best_offset=-5, coarse_fail_offset=-10),
        }
        picked = eng._pick_next_core()
        assert picked == 1  # fine_search scores 0, not_started scores 4
        assert eng._core_states[0].phase == TunerPhase.NOT_STARTED

    def test_round_robin_rotates(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="round_robin")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5),
            2: CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-5),
        }
        # No last tested — should pick first active
        picked = eng._pick_next_core()
        assert picked == 0

        # After testing core 0, should pick core 1
        eng._last_tested_core = 0
        picked = eng._pick_next_core()
        assert picked == 1

        # After testing core 1, should pick core 2
        eng._last_tested_core = 1
        picked = eng._pick_next_core()
        assert picked == 2

        # After testing core 2, should wrap back to core 0
        eng._last_tested_core = 2
        picked = eng._pick_next_core()
        assert picked == 0


class TestInheritCurrentCO:
    def test_inherit_reads_smu_offsets(self, db, simple_topology, mock_smu, mock_backend):
        """When inherit_current=True, start offsets come from SMU, not config."""
        mock_smu.get_co_offset = MagicMock(side_effect=lambda cid: {0: -15, 1: -20}.get(cid, 0))
        cfg = TunerConfig(
            cores_to_test=[0, 1],
            inherit_current=True,
            search_duration_seconds=1,
        )
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        with patch.object(eng, "_run_next"):
            eng.start()
        assert eng._core_states[0].current_offset == -15
        assert eng._core_states[1].current_offset == -20

    def test_inherit_survives_first_advance(self, db, simple_topology, mock_smu, mock_backend):
        """Inherited offset should be used as base for first coarse step."""
        mock_smu.get_co_offset = MagicMock(side_effect=lambda cid: {0: -15}.get(cid, 0))
        cfg = TunerConfig(
            cores_to_test=[0],
            inherit_current=True,
            coarse_step=5,
            search_duration_seconds=1,
        )
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        with patch.object(eng, "_run_next"):
            eng.start()
        # Core starts at -15 (inherited), first advance should go to -15 + (-1)*5 = -20
        cs = eng._core_states[0]
        eng._advance_core(0, passed=False)  # not_started -> coarse_search
        assert cs.phase == TunerPhase.COARSE_SEARCH
        assert cs.current_offset == -20  # -15 (inherited base) + -5 (coarse step)

    def test_inherit_false_uses_start_offset(self, db, simple_topology, mock_smu, mock_backend):
        """When inherit_current=False (default), use config start_offset."""
        cfg = TunerConfig(
            cores_to_test=[0, 1],
            inherit_current=False,
            start_offset=-5,
            search_duration_seconds=1,
        )
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        with patch.object(eng, "_run_next"):
            eng.start()
        assert eng._core_states[0].current_offset == -5
        assert eng._core_states[1].current_offset == -5


class TestCCDAlternatingOrder:
    def test_alternates_between_ccds(self, db, topo_dual_ccd_x3d, mock_smu, mock_backend):
        """CCD-alternating should pick from CCD0, then CCD1, then CCD0, etc."""
        cfg = TunerConfig(
            cores_to_test=[0, 1, 2, 3, 4, 5, 6, 7],
            test_order="ccd_alternating",
        )
        eng = TunerEngine(
            db=db, topology=topo_dual_ccd_x3d, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            i: CoreState(core_id=i, phase=TunerPhase.COARSE_SEARCH, current_offset=-5)
            for i in range(8)
        }

        order = []
        for _ in range(8):
            picked = eng._pick_next_core()
            if picked is None:
                break
            order.append(picked)
            eng._core_states[picked] = CoreState(
                core_id=picked, phase=TunerPhase.CONFIRMED, current_offset=-5, best_offset=-5,
            )

        # Verify alternation: consecutive picks should be from different CCDs
        topo = topo_dual_ccd_x3d
        for i in range(1, len(order)):
            ccd_prev = topo.cores[order[i - 1]].ccd
            ccd_curr = topo.cores[order[i]].ccd
            if i < len(order) - 1:
                assert ccd_prev != ccd_curr, (
                    f"Picks {i-1} and {i} ({order[i-1]}, {order[i]}) "
                    f"are both on CCD {ccd_curr}"
                )

    def test_falls_back_when_one_ccd_exhausted(self, db, topo_dual_ccd_x3d, mock_smu, mock_backend):
        """When one CCD is all confirmed, pick remaining from the other."""
        cfg = TunerConfig(
            cores_to_test=[0, 1, 4, 5],
            test_order="ccd_alternating",
        )
        eng = TunerEngine(
            db=db, topology=topo_dual_ccd_x3d, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5),
            4: CoreState(core_id=4, phase=TunerPhase.CONFIRMED, current_offset=-10, best_offset=-10),
            5: CoreState(core_id=5, phase=TunerPhase.CONFIRMED, current_offset=-10, best_offset=-10),
        }
        picked = eng._pick_next_core()
        assert picked in (0, 1)


class TestCCDRoundRobinOrder:
    def test_interleaves_ccds_and_rotates_cores(self, db, topo_dual_ccd_x3d, mock_smu, mock_backend):
        """Should produce: CCD0[0]→CCD1[0]→CCD0[1]→CCD1[1]→..."""
        cfg = TunerConfig(
            cores_to_test=[0, 1, 2, 3, 4, 5, 6, 7],
            test_order="ccd_round_robin",
        )
        eng = TunerEngine(
            db=db, topology=topo_dual_ccd_x3d, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        eng._core_states = {
            i: CoreState(core_id=i, phase=TunerPhase.COARSE_SEARCH, current_offset=-5)
            for i in range(8)
        }

        picks = []
        for _ in range(8):
            picked = eng._pick_next_core()
            assert picked is not None
            picks.append(picked)
            eng._last_tested_core = picked
            # Update per-CCD tracking
            core_info = topo_dual_ccd_x3d.cores.get(picked)
            if core_info and core_info.ccd is not None:
                eng._ccd_last_tested[core_info.ccd] = picked
            # Mark as confirmed so it's not picked again
            eng._core_states[picked] = CoreState(
                core_id=picked, phase=TunerPhase.CONFIRMED, current_offset=-5, best_offset=-5,
            )

        topo = topo_dual_ccd_x3d
        # Verify CCD alternation
        for i in range(1, len(picks)):
            prev_ccd = topo.cores[picks[i-1]].ccd
            curr_ccd = topo.cores[picks[i]].ccd
            assert prev_ccd != curr_ccd, f"Picks {i-1},{i} ({picks[i-1]},{picks[i]}) same CCD"

        # Verify all 8 cores were picked (rotation worked)
        assert sorted(picks) == [0, 1, 2, 3, 4, 5, 6, 7]


class TestExceedsMax:
    def test_negative_direction(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(max_offset=-30, direction=-1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        assert eng._exceeds_max(-31) is True
        assert eng._exceeds_max(-30) is False
        assert eng._exceeds_max(-29) is False

    def test_positive_direction(self, db, simple_topology, mock_smu, mock_backend):
        # co_range is (-60, 10), so max_offset=20 gets clamped to 10
        cfg = TunerConfig(max_offset=10, direction=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        assert eng._exceeds_max(11) is True
        assert eng._exceeds_max(10) is False
        assert eng._exceeds_max(9) is False


class TestBackoffAlgorithm:
    """Test the backoff/binary-search algorithm after failed confirmation."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        return TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

    def test_failed_confirm_enters_backoff_preconfirm(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.FAILED_CONFIRM, current_offset=-8,
            best_offset=-8, confirm_attempts=2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.best_offset == -7
        assert cs.backoff_mode is True
        assert cs.confirm_attempts == 0

    def test_backoff_preconfirm_pass_enters_backoff_confirming(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-7,
            best_offset=-7, backoff_mode=True,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.BACKOFF_CONFIRMING
        assert cs.backoff_pass_bound == -7

    def test_backoff_preconfirm_fail_backs_off(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-7,
            best_offset=-7, backoff_mode=True,
            consecutive_backoff_fails=0,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.best_offset == -6  # backed off from -7
        assert cs.consecutive_backoff_fails == 1

    def test_backoff_confirming_pass_confirms(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, hardening_tiers=[])
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_CONFIRMING, current_offset=-7,
            best_offset=-7, backoff_mode=True,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.CONFIRMED

    def test_backoff_confirming_fail_returns_to_preconfirm(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_CONFIRMING, current_offset=-7,
            best_offset=-7, backoff_mode=True,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.best_offset == -6  # backed off from -7

    def test_midpoint_jump_after_threshold(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, midpoint_jump_threshold=3)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-7,
            best_offset=-7, backoff_mode=True,
            consecutive_backoff_fails=2,
            baseline_offset=0,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # 3rd fail triggers midpoint jump
        assert cs.backoff_fail_bound == -7
        assert cs.consecutive_backoff_fails == 0  # reset after jump

    def test_backoff_preconfirm_pass_after_midpoint_sets_bounds(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-4,
            best_offset=-4, backoff_mode=True,
            backoff_fail_bound=-7,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.BACKOFF_CONFIRMING
        assert cs.backoff_pass_bound == -4

    def test_convergence_guard_at_baseline(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-1,
            best_offset=-1, backoff_mode=True,
            consecutive_backoff_fails=0,
            baseline_offset=0,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off from -1: -1 - (-1)*1 = 0, which is baseline
        assert cs.phase == TunerPhase.CONFIRMED
        assert cs.best_offset == 0

    def test_binary_search_narrows_on_pass(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_CONFIRMING, current_offset=-5,
            best_offset=-5, backoff_mode=True,
            backoff_fail_bound=-10, backoff_pass_bound=-5,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # Binary search: midpoint between pass(-5) and fail(-10)
        # mid = -5 + (-1) * (5 // 2) = -5 + (-1)*2 = -7
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.current_offset == -7

    def test_binary_search_narrows_on_fail(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_CONFIRMING, current_offset=-7,
            best_offset=-7, backoff_mode=True,
            backoff_fail_bound=-10, backoff_pass_bound=-5,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Confirm failed — back to preconfirm, back off
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.best_offset == -6  # -7 - (-1)*1 = -6

    def test_binary_search_converges(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, hardening_tiers=[])
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_CONFIRMING, current_offset=-6,
            best_offset=-6, backoff_mode=True,
            backoff_fail_bound=-7, backoff_pass_bound=-6,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # Gap is 1 (== fine_step), so converged
        assert cs.phase == TunerPhase.CONFIRMED

    def test_backoff_floor_uses_baseline_not_start(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.FAILED_CONFIRM, current_offset=-3,
            best_offset=-3, confirm_attempts=2,
            baseline_offset=-2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # -3 - (-1)*1 = -2 = baseline, so should settle at baseline
        assert cs.phase == TunerPhase.CONFIRMED
        assert cs.best_offset == -2

    def test_backoff_with_positive_direction(self, db, simple_topology, mock_smu, mock_backend):
        """Binary search works with direction=+1 (overvolting)."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, direction=1, max_offset=30)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=7,
            best_offset=7, backoff_mode=True,
            backoff_fail_bound=10, backoff_pass_bound=4,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.backoff_pass_bound == 7
        # Binary search midpoint: 7 + (10-7)//2 = 7 + 1 = 8
        assert cs.current_offset == 8
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM

    def test_midpoint_jump_threshold_1(self, db, simple_topology, mock_smu, mock_backend):
        """threshold=1 should trigger midpoint jump on first failure."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, midpoint_jump_threshold=1)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-7,
            best_offset=-7, backoff_mode=True,
            consecutive_backoff_fails=0,
            baseline_offset=-2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Should immediately jump to midpoint (threshold=1, first fail triggers)
        assert cs.consecutive_backoff_fails == 0  # reset after jump
        assert cs.backoff_fail_bound == -7

    def test_resume_from_backoff_preconfirm(self, db, simple_topology, mock_smu, mock_backend):
        """Resuming a session interrupted during backoff_preconfirm should back off."""
        cfg = TunerConfig(cores_to_test=[0], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-10,
            best_offset=-10, backoff_mode=True,
            consecutive_backoff_fails=1, baseline_offset=-5,
            in_test=True,  # was actively testing when crash happened
        ))
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        # Should have advanced (treated as failure) — backed off from -10
        cs = eng._core_states[0]
        assert cs.phase != TunerPhase.BACKOFF_PRECONFIRM or cs.current_offset != -10
        assert cs.consecutive_backoff_fails >= 2 or cs.current_offset != -10


class TestCrashDetection:
    """Tests for _apply_crash_penalty, _is_more_aggressive, and _detect_and_handle_crashes."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0, 1])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        return eng

    def test_is_more_aggressive_negative_direction(self, db, simple_topology, mock_smu, mock_backend):
        """For direction=-1, more negative = more aggressive."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, direction=-1)
        assert eng._is_more_aggressive(-30, -20) is True
        assert eng._is_more_aggressive(-20, -30) is False
        assert eng._is_more_aggressive(-20, -20) is False

    def test_is_more_aggressive_positive_direction(self, db, simple_topology, mock_smu, mock_backend):
        """For direction=+1, more positive = more aggressive."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, direction=1, max_offset=30)
        assert eng._is_more_aggressive(30, 20) is True
        assert eng._is_more_aggressive(20, 30) is False
        assert eng._is_more_aggressive(20, 20) is False

    def test_crash_penalty_backoff(self, db, simple_topology, mock_smu, mock_backend):
        """After crash, offset backs off by crash_penalty_steps * fine_step."""
        eng = self._make_engine(
            db, simple_topology, mock_smu, mock_backend,
            direction=-1, fine_step=1, crash_penalty_steps=3,
        )
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-30, best_offset=-28, in_test=True,
        )
        eng._core_states = {0: cs}
        eng._apply_crash_penalty(cs)
        # -30 - ((-1) * 3 * 1) = -30 + 3 = -27
        assert cs.current_offset == -27

    def test_crash_sets_hard_fail_bound(self, db, simple_topology, mock_smu, mock_backend):
        """Crashed offset becomes hard fail_bound."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-30, best_offset=-28, in_test=True,
            backoff_fail_bound=None,
        )
        eng._core_states = {0: cs}
        eng._apply_crash_penalty(cs)
        assert cs.backoff_fail_bound == -30

    def test_crash_does_not_overwrite_less_aggressive_fail_bound(self, db, simple_topology, mock_smu, mock_backend):
        """fail_bound is only updated if the crashed offset is MORE aggressive."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-20, best_offset=-15, in_test=True,
            backoff_fail_bound=-30,  # existing bound is already more aggressive
        )
        eng._core_states = {0: cs}
        eng._apply_crash_penalty(cs)
        # -30 is more aggressive than -20, so it stays
        assert cs.backoff_fail_bound == -30

    def test_crash_increments_count_and_cooldown(self, db, simple_topology, mock_smu, mock_backend):
        """Crash increments crash_count and sets crash_cooldown=2."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-30, in_test=True,
        )
        eng._core_states = {0: cs}
        eng._apply_crash_penalty(cs)
        assert cs.crash_count == 1
        assert cs.crash_cooldown == 2

    def test_crash_enters_backoff_from_coarse_search(self, db, simple_topology, mock_smu, mock_backend):
        """Crash during coarse_search enters BACKOFF_PRECONFIRM."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-10, in_test=True,
        )
        eng._core_states = {0: cs}
        eng._apply_crash_penalty(cs)
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.backoff_mode is True

    def test_crash_enters_backoff_from_fine_search(self, db, simple_topology, mock_smu, mock_backend):
        """Crash during fine_search enters BACKOFF_PRECONFIRM."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH,
            current_offset=-10, in_test=True,
        )
        eng._core_states = {0: cs}
        eng._apply_crash_penalty(cs)
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.backoff_mode is True

    def test_crash_penalty_clamps_to_baseline(self, db, simple_topology, mock_smu, mock_backend):
        """Penalty that overshoots past baseline is clamped to baseline."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, crash_penalty_steps=10)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-2, baseline_offset=0, in_test=True,
        )
        eng._core_states = {0: cs}
        eng._apply_crash_penalty(cs)
        # -2 - ((-1) * 10 * 1) = -2 + 10 = 8 → past baseline(0), clamp to 0
        assert cs.current_offset == 0

    def test_detect_and_handle_crashes_returns_crashed_ids(self, db, simple_topology, mock_smu, mock_backend):
        """_detect_and_handle_crashes returns list of crashed core IDs."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, in_test=True),
            1: CoreState(core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-8, in_test=False),
        }
        crashed = eng._detect_and_handle_crashes(eng._core_states)
        assert crashed == [0]

    def test_detect_and_handle_crashes_clears_in_test(self, db, simple_topology, mock_smu, mock_backend):
        """After crash detection, in_test is cleared."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, in_test=True)
        eng._core_states = {0: cs}
        eng._detect_and_handle_crashes(eng._core_states)
        assert cs.in_test is False

    def test_detect_and_handle_crashes_applies_penalty(self, db, simple_topology, mock_smu, mock_backend):
        """Crash detection applies penalty (not just a plain failure advance)."""
        eng = self._make_engine(
            db, simple_topology, mock_smu, mock_backend,
            crash_penalty_steps=3, fine_step=1,
        )
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-15, in_test=True,
        )
        eng._core_states = {0: cs}
        eng._detect_and_handle_crashes(eng._core_states)
        # Penalty: -15 - ((-1)*3*1) = -15 + 3 = -12
        assert cs.current_offset == -12
        assert cs.crash_count == 1
        assert cs.crash_cooldown == 2

    def test_detect_and_handle_crashes_logs_to_db(self, db, simple_topology, mock_smu, mock_backend):
        """Crash detection writes a synthetic crash event to the DB."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-10, in_test=True,
        )
        eng._core_states = {0: cs}
        eng._detect_and_handle_crashes(eng._core_states)
        log_entries = tp.get_test_log(db, eng._session_id, core_id=0)
        assert any(e.get("error_type") == "crash" for e in log_entries)

    def test_resume_uses_crash_detection(self, db, simple_topology, mock_smu, mock_backend):
        """resume() uses crash penalty (not plain advance) for in_test cores."""
        cfg = TunerConfig(
            cores_to_test=[0], search_duration_seconds=1,
            crash_penalty_steps=3, fine_step=1,
        )
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-15, in_test=True,
        ))
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        cs = eng._core_states[0]
        # Should have been crash-penalized: -15 + 3 = -12
        assert cs.crash_count == 1
        assert cs.crash_cooldown == 2
        assert cs.current_offset == -12

    def test_resume_non_in_test_not_penalized(self, db, simple_topology, mock_smu, mock_backend):
        """Cores not in_test are not touched by crash detection."""
        cfg = TunerConfig(cores_to_test=[0, 1], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-8, in_test=False,
        ))
        tp.save_core_state(db, sid, CoreState(
            core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, in_test=True,
        ))
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        # Core 0 not in_test — unchanged
        assert eng._core_states[0].phase == TunerPhase.FINE_SEARCH
        assert eng._core_states[0].current_offset == -8
        assert eng._core_states[0].crash_count == 0


class TestSafetyRamp:
    """Tests for _get_coarse_step: reduces step size near max_offset."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=2, fine_step=1, max_offset=-50, cores_to_test=[0])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        return TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

    def test_coarse_slows_near_max_offset(self, db, simple_topology, mock_smu, mock_backend):
        """Within 2*coarse_step of max_offset, step size reduces to fine_step."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        # direction=-1, max_offset=-50, coarse_step=2, ramp_zone=4
        # At -44: distance to -50 is 6, ramp_zone=4. 6 > 4, so coarse_step
        cs_far = CoreState(core_id=0, current_offset=-44)
        assert eng._get_coarse_step(cs_far) == 2
        # At -46: distance to -50 is 4, ramp_zone=4. 4 <= 4, so fine_step
        cs_near = CoreState(core_id=0, current_offset=-46)
        assert eng._get_coarse_step(cs_near) == 1

    def test_coarse_normal_step_far_from_max(self, db, simple_topology, mock_smu, mock_backend):
        """Far from max_offset, use normal coarse_step."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, current_offset=-10)
        assert eng._get_coarse_step(cs) == 2

    def test_advance_core_uses_reduced_step_near_max(self, db, simple_topology, mock_smu, mock_backend):
        """_advance_core uses fine_step (not coarse_step) when near max_offset."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        # At -46 with best_offset=-46: distance to -50 is 4, ramp_zone=4 → fine_step=1
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-46, best_offset=-46)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # Should advance by fine_step=1 (not coarse_step=2): -46 + (-1)*1 = -47
        assert cs.current_offset == -47


class TestHardeningPhases:
    def test_hardening_phases_exist(self):
        assert TunerPhase.HARDENING_T1 == "hardening_t1"
        assert TunerPhase.HARDENING_T2 == "hardening_t2"
        assert TunerPhase.HARDENED == "hardened"

    def test_core_state_has_crash_fields(self):
        cs = CoreState(core_id=0)
        assert cs.crash_count == 0
        assert cs.crash_cooldown == 0
        assert cs.cumulative_test_time == 0.0
        assert cs.hardening_tier_index == 0

    def test_phase_ordering_includes_hardening(self):
        phases = list(TunerPhase)
        assert TunerPhase.HARDENING_T1 in phases
        assert TunerPhase.HARDENING_T2 in phases
        assert TunerPhase.HARDENED in phases


class TestDeathSpiralPrevention:
    """Unit tests for _check_time_budget and _accumulate_test_time."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        return eng

    def test_time_budget_settles_core(self, db, simple_topology, mock_smu, mock_backend):
        """Core exceeding time budget settles at best_offset."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                max_core_time_seconds=7200)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-20,
            best_offset=-15, baseline_offset=0, cumulative_test_time=7201.0,
        )
        eng._core_states = {0: cs}

        settled = eng._check_time_budget(cs)

        assert settled is True
        assert cs.phase == TunerPhase.CONFIRMED
        assert cs.current_offset == -15  # settled at best_offset
        assert cs.backoff_mode is False

    def test_time_budget_no_best_settles_at_baseline(self, db, simple_topology, mock_smu, mock_backend):
        """Core with no best_offset settles at baseline when budget exceeded."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                max_core_time_seconds=7200)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
            best_offset=None, baseline_offset=0, cumulative_test_time=7201.0,
        )
        eng._core_states = {0: cs}

        settled = eng._check_time_budget(cs)

        assert settled is True
        assert cs.phase == TunerPhase.CONFIRMED
        assert cs.current_offset == 0  # settled at baseline_offset

    def test_time_budget_not_exceeded_returns_false(self, db, simple_topology, mock_smu, mock_backend):
        """Core under time budget returns False (not settled)."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                max_core_time_seconds=7200)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-20,
            best_offset=-15, baseline_offset=0, cumulative_test_time=3600.0,
        )
        eng._core_states = {0: cs}

        settled = eng._check_time_budget(cs)

        assert settled is False
        assert cs.phase == TunerPhase.COARSE_SEARCH  # unchanged

    def test_cumulative_time_tracks_test_duration(self, db, simple_topology, mock_smu, mock_backend):
        """_accumulate_test_time adds duration for search phases."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10,
            cumulative_test_time=100.0,
        )
        eng._core_states = {0: cs}

        eng._accumulate_test_time(cs, 300.0)

        assert cs.cumulative_test_time == 400.0

    def test_cumulative_time_not_tracked_during_hardening(self, db, simple_topology, mock_smu, mock_backend):
        """Hardening phases don't count toward the time budget."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        for hardening_phase in (
            TunerPhase.HARDENING_T1,
            TunerPhase.HARDENING_T2,
            TunerPhase.HARDENED,
        ):
            cs = CoreState(
                core_id=0, phase=hardening_phase, current_offset=-10,
                cumulative_test_time=500.0,
            )
            eng._accumulate_test_time(cs, 300.0)
            assert cs.cumulative_test_time == 500.0, (
                f"Phase {hardening_phase} should not accumulate time"
            )

    def test_accumulate_counts_all_search_phases(self, db, simple_topology, mock_smu, mock_backend):
        """All non-hardening active phases accumulate time."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        search_phases = (
            TunerPhase.COARSE_SEARCH,
            TunerPhase.FINE_SEARCH,
            TunerPhase.CONFIRMING,
            TunerPhase.BACKOFF_PRECONFIRM,
            TunerPhase.BACKOFF_CONFIRMING,
        )
        for phase in search_phases:
            cs = CoreState(core_id=0, phase=phase, current_offset=-10, cumulative_test_time=0.0)
            eng._accumulate_test_time(cs, 60.0)
            assert cs.cumulative_test_time == 60.0, (
                f"Phase {phase} should accumulate time"
            )


class TestCrashAwareScheduling:
    """Tests for crash cooldown and crash history in core scheduling."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0, 1, 2])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        return eng

    def test_cooldown_skips_core(self, db, simple_topology, mock_smu, mock_backend):
        """Core with crash_cooldown > 0 is skipped by picker."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                test_order="sequential")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_cooldown=2),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_cooldown=0),
        }
        picked = eng._pick_next_core()
        # Core 0 has cooldown, so core 1 should be picked
        assert picked == 1

    def test_cooldown_decrements(self, db, simple_topology, mock_smu, mock_backend):
        """Cooldown decrements when another core is tested."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_cooldown=2),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_cooldown=0),
        }
        # Decrement cooldowns for all except core 1 (which is being tested)
        eng._decrement_cooldowns(picked_core=1)
        assert eng._core_states[0].crash_cooldown == 1
        # Core being tested is not decremented
        assert eng._core_states[1].crash_cooldown == 0

    def test_weakest_first_penalizes_crashed_cores(self, db, simple_topology, mock_smu, mock_backend):
        """Cores with crash history are scored lower (higher score) in weakest_first."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                test_order="weakest_first")
        eng._core_states = {
            # Core 0: fine_search (score 0) but has crash_count=1, so score = 0 + 2 = 2
            0: CoreState(core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-6,
                         best_offset=-5, coarse_fail_offset=-10, crash_count=1),
            # Core 1: coarse_search (score 2), no crashes, so score = 2 + 0 = 2
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_count=0),
            # Core 2: not_started (score 4), no crashes
            2: CoreState(core_id=2, phase=TunerPhase.NOT_STARTED, crash_count=0),
        }
        picked = eng._pick_next_core()
        # Core 1 has score 2 (coarse, no crash), core 0 has score 2 (fine + crash penalty)
        # Both score 2, so lowest core_id (0 vs 1) — but actually core 1 should be preferred
        # because tie-breaking by core_id: 0 < 1, so core 0 wins unless penalty moves it up.
        # With crash penalty: core 0 fine_search=0 + crash_count*2=2 → score 2
        # core 1 coarse_search=2 + 0 = 2. Tie broken by core_id: core 0 picked.
        # Let's instead verify that a heavily crashed core gets deprioritized vs a fresh core
        # with the same base phase.
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-6,
                         best_offset=-5, coarse_fail_offset=-10, crash_count=3),
            1: CoreState(core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-6,
                         best_offset=-5, coarse_fail_offset=-10, crash_count=0),
        }
        picked = eng._pick_next_core()
        # Core 0: score = 0 (fine) + 3*2 = 6
        # Core 1: score = 0 (fine) + 0*2 = 0 → core 1 should be picked
        assert picked == 1

    def test_all_cores_in_cooldown_returns_none(self, db, simple_topology, mock_smu, mock_backend):
        """If all active cores are in cooldown, _pick_next_core returns None."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                test_order="sequential")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_cooldown=1),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_cooldown=2),
        }
        picked = eng._pick_next_core()
        assert picked is None

    def test_cooldown_does_not_skip_confirmed_cores(self, db, simple_topology, mock_smu, mock_backend):
        """Confirmed cores are already excluded regardless of cooldown."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                test_order="sequential")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-20,
                         best_offset=-20, crash_cooldown=0),
            1: CoreState(core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                         crash_cooldown=0),
        }
        picked = eng._pick_next_core()
        assert picked == 1

    def test_is_core_available_confirmed_returns_false(self, db, simple_topology, mock_smu, mock_backend):
        """CONFIRMED phase cores are not available."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-20,
                       best_offset=-20)
        assert eng._is_core_available(cs) is False

    def test_is_core_available_hardened_returns_false(self, db, simple_topology, mock_smu, mock_backend):
        """HARDENED phase cores are not available."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENED, current_offset=-20)
        assert eng._is_core_available(cs) is False

    def test_is_core_available_cooldown_returns_false(self, db, simple_topology, mock_smu, mock_backend):
        """Cores with crash_cooldown > 0 are not available."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                       crash_cooldown=1)
        assert eng._is_core_available(cs) is False

    def test_is_core_available_active_no_cooldown_returns_true(self, db, simple_topology, mock_smu, mock_backend):
        """Active core with no cooldown is available."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
                       crash_cooldown=0)
        assert eng._is_core_available(cs) is True


class TestHardeningTransitions:
    """Tests for hardening phase state transitions in _advance_core."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        return eng

    def test_confirmed_enters_hardening_t1(self, db, simple_topology, mock_smu, mock_backend):
        """CONFIRMING pass with hardening_tiers transitions to HARDENING_T1."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=tiers)
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-8, best_offset=-8)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.HARDENING_T1
        assert cs.hardening_tier_index == 0

    def test_confirmed_skips_hardening_when_no_tiers(self, db, simple_topology, mock_smu, mock_backend):
        """CONFIRMING pass with empty hardening_tiers stays CONFIRMED."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=[])
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-8, best_offset=-8)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.CONFIRMED

    def test_hardening_t1_pass_enters_t2(self, db, simple_topology, mock_smu, mock_backend):
        """HARDENING_T1 pass with 2 tiers transitions to HARDENING_T2."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=tiers)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1, current_offset=-8,
                       best_offset=-8, hardening_tier_index=0)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.HARDENING_T2
        assert cs.hardening_tier_index == 1

    def test_hardening_t2_pass_becomes_hardened(self, db, simple_topology, mock_smu, mock_backend):
        """Last hardening tier pass transitions to HARDENED."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=tiers)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T2, current_offset=-8,
                       best_offset=-8, hardening_tier_index=1)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.HARDENED

    def test_hardening_t1_fail_backs_off_retries_t1(self, db, simple_topology, mock_smu, mock_backend):
        """HARDENING_T1 fail backs off by fine_step and retries T1."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                fine_step=1, hardening_tiers=tiers)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1, current_offset=-8,
                       best_offset=-8, baseline_offset=0, hardening_tier_index=0)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off: -8 - ((-1)*1) = -7
        assert cs.phase == TunerPhase.HARDENING_T1
        assert cs.current_offset == -7
        assert cs.best_offset == -7
        assert cs.hardening_tier_index == 0  # stays at T1

    def test_hardening_t2_fail_retries_t2_not_t1(self, db, simple_topology, mock_smu, mock_backend):
        """HARDENING_T2 fail backs off and retries T2 (T1 carries forward)."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                fine_step=1, hardening_tiers=tiers)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T2, current_offset=-8,
                       best_offset=-8, baseline_offset=0, hardening_tier_index=1)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off: -8 - ((-1)*1) = -7; stays at T2 (tier_index=1)
        assert cs.phase == TunerPhase.HARDENING_T2
        assert cs.current_offset == -7
        assert cs.best_offset == -7
        assert cs.hardening_tier_index == 1  # stays at T2

    def test_hardening_backoff_at_baseline_settles(self, db, simple_topology, mock_smu, mock_backend):
        """Hardening backoff reaching baseline settles core as HARDENED at baseline."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                fine_step=1, hardening_tiers=tiers)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1, current_offset=-1,
                       best_offset=-1, baseline_offset=0, hardening_tier_index=0)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off: -1 - ((-1)*1) = 0 = baseline → settle as HARDENED
        assert cs.phase == TunerPhase.HARDENED
        assert cs.current_offset == 0
        assert cs.best_offset == 0

    def test_get_active_stress_config_returns_tier_during_hardening(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        """During hardening, _get_active_stress_config returns the tier's config."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=tiers)
        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T2, current_offset=-8,
                       hardening_tier_index=1)
        backend, mode, fft = eng._get_active_stress_config(cs)
        assert backend == "mprime"
        assert mode == "SSE"
        assert fft == "LARGE"

    def test_get_active_stress_config_returns_primary_during_search(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        """During search/confirm, _get_active_stress_config returns primary backend config."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                backend="mprime", stress_mode="SSE", fft_preset="SMALL")
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-8)
        backend, mode, fft = eng._get_active_stress_config(cs)
        assert backend == "mprime"
        assert mode == "SSE"
        assert fft == "SMALL"

    def test_backoff_confirming_pass_enters_hardening_when_tiers(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        """BACKOFF_CONFIRMING pass with tiers should enter HARDENING_T1 (not CONFIRMED)."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=tiers)
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_CONFIRMING, current_offset=-7,
            best_offset=-7, backoff_mode=True,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.HARDENING_T1
        assert cs.hardening_tier_index == 0

    def test_complete_session_requires_hardened_when_tiers_configured(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        """With hardening_tiers configured, all cores must reach HARDENED to complete."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=tiers, cores_to_test=[0, 1])
        eng._set_status("running")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.HARDENED, current_offset=-8, best_offset=-8),
            1: CoreState(core_id=1, phase=TunerPhase.CONFIRMED, current_offset=-6, best_offset=-6),
        }
        completed = []
        eng.session_completed.connect(lambda x: completed.append(x))
        eng._complete_session()
        # Core 1 is only CONFIRMED, not HARDENED, so session should NOT complete yet
        assert len(completed) == 0

    def test_complete_session_no_tiers_confirmed_is_done(
        self, db, simple_topology, mock_smu, mock_backend
    ):
        """Without hardening_tiers, CONFIRMED cores complete the session."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=[], cores_to_test=[0, 1],
                                auto_validate=False)
        eng._set_status("running")
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-8, best_offset=-8),
            1: CoreState(core_id=1, phase=TunerPhase.CONFIRMED, current_offset=-6, best_offset=-6),
        }
        completed = []
        eng.session_completed.connect(lambda x: completed.append(x))
        eng._complete_session()
        assert len(completed) == 1


# ===========================================================================
# Helpers for TestValidationS4
# ===========================================================================


def _make_minimal_topology():
    """Build a 4-core single CCD topology without sysfs."""
    from engine.topology import CPUTopology, PhysicalCore
    topo = CPUTopology()
    topo.physical_cores = 4
    topo.smt_enabled = False
    topo.logical_cpus_count = 4
    for i in range(4):
        topo.cores[i] = PhysicalCore(
            core_id=i,
            ccd=0,
            ccx=None,
            logical_cpus=(i,),
        )
    return topo


def make_test_engine(cfg: "TunerConfig") -> "TunerEngine":
    """Build a minimal TunerEngine for unit testing (no Qt event loop needed)."""
    from history.db import HistoryDB
    from unittest.mock import MagicMock
    from engine.backends.base import StressConfig, StressMode
    from engine.scheduler import SchedulerConfig

    db = HistoryDB(":memory:")
    topo = _make_minimal_topology()
    smu = MagicMock()
    smu.commands = MagicMock()
    smu.commands.co_range = (-60, 10)

    class _MockBackend:
        name = "mock"
        def is_available(self): return True
        def get_command(self, config, work_dir): return ["echo", "mock"]
        def parse_output(self, stdout, stderr, returncode): return True, None
        def get_supported_modes(self): return [StressMode.SSE]
        def prepare(self, work_dir, config): work_dir.mkdir(parents=True, exist_ok=True)
        def cleanup(self, work_dir, *, preserve_on_error=False): pass

    backend = _MockBackend()
    return TunerEngine(db=db, topology=topo, smu=smu, backend=backend, config=cfg)


# ===========================================================================
# TestValidationS4
# ===========================================================================


class TestValidationS4:
    def test_validation_stage_count_with_transitions(self):
        """With validate_transitions=True, validation has 4 stages."""
        cfg = TunerConfig(validate_transitions=True, hardening_tiers=[])
        engine = make_test_engine(cfg)
        assert engine._get_validation_stage_count() == 4

    def test_validation_stage_count_without_transitions(self):
        """With validate_transitions=False, validation has 3 stages."""
        cfg = TunerConfig(validate_transitions=False, hardening_tiers=[])
        engine = make_test_engine(cfg)
        assert engine._get_validation_stage_count() == 3

    def test_stage4_dispatched_when_validate_transitions(self):
        """_run_validation_next dispatches S4 when validate_transitions=True."""
        cfg = TunerConfig(validate_transitions=True, hardening_tiers=[])
        engine = make_test_engine(cfg)
        engine._validation_stage = 4
        engine._validation_core_order = [0, 1]
        with patch.object(engine, "_run_validation_stage4") as mock_s4:
            engine._run_validation_next()
        mock_s4.assert_called_once()

    def test_stage4_skipped_when_no_validate_transitions(self):
        """_run_validation_next skips S4 and finalizes when validate_transitions=False."""
        cfg = TunerConfig(validate_transitions=False, hardening_tiers=[])
        engine = make_test_engine(cfg)
        engine._validation_stage = 4
        engine._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.HARDENED, best_offset=-8),
        }
        with patch.object(engine, "_finalize_session") as mock_fin:
            engine._run_validation_next()
        mock_fin.assert_called_once()

    def test_stage3_complete_advances_to_s4_when_enabled(self):
        """Stage 3 completion sets stage=4 when validate_transitions=True."""
        cfg = TunerConfig(validate_transitions=True, hardening_tiers=[])
        engine = make_test_engine(cfg)
        engine._validation_stage = 3
        engine._validation_halves = []  # empty = already done
        engine._validation_half_index = 0
        with patch("PySide6.QtCore.QTimer.singleShot"):
            engine._run_validation_stage3()
        assert engine._validation_stage == 4

    def test_stage3_complete_skips_s4_when_disabled(self):
        """Stage 3 completion sets stage=5 (sentinel) when validate_transitions=False."""
        cfg = TunerConfig(validate_transitions=False, hardening_tiers=[])
        engine = make_test_engine(cfg)
        engine._validation_stage = 3
        engine._validation_halves = []
        engine._validation_half_index = 0
        with patch("PySide6.QtCore.QTimer.singleShot"):
            engine._run_validation_stage3()
        assert engine._validation_stage == 5

    def test_validation_pass_s4_advances_to_finalize(self):
        """S4 pass advances to sentinel stage (finalize)."""
        cfg = TunerConfig(validate_transitions=True, hardening_tiers=[])
        engine = make_test_engine(cfg)
        engine._validation_stage = 4
        with patch("PySide6.QtCore.QTimer.singleShot"):
            engine._on_validation_test_finished(0, passed=True)
        assert engine._validation_stage == 5

    def test_validation_fail_s4_backs_off(self):
        """S4 failure backs off the most aggressive core."""
        cfg = TunerConfig(validate_transitions=True, hardening_tiers=[])
        engine = make_test_engine(cfg)
        engine._validation_stage = 4
        engine._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.HARDENED, best_offset=-10,
                         baseline_offset=0, current_offset=-10),
        }
        with patch.object(engine, "_find_most_aggressive_core", return_value=0):
            with patch("PySide6.QtCore.QTimer.singleShot"):
                engine._on_validation_test_finished(0, passed=False)
        # Should restart from stage 1
        assert engine._validation_stage == 1


class TestHardeningTierPhaseLabeling:
    """Tests that 3+ hardening tiers cycle T1/T2 labels correctly."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        return eng

    def test_three_tiers_cycle_phases(self, db, simple_topology, mock_smu, mock_backend):
        """With 3 tiers: T1(0) → T2(1) → T1(2) → HARDENED."""
        tiers = [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "LARGE"},
        ]
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend,
                                hardening_tiers=tiers)

        cs = CoreState(core_id=0, phase=TunerPhase.HARDENING_T1, current_offset=-8,
                       best_offset=-8, hardening_tier_index=0)
        eng._core_states = {0: cs}

        # Tier 0 pass → T2 (index 1)
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.HARDENING_T2
        assert cs.hardening_tier_index == 1

        # Tier 1 pass → T1 (index 2, even)
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.HARDENING_T1
        assert cs.hardening_tier_index == 2

        # Tier 2 pass → HARDENED (all tiers exhausted)
        eng._advance_core(0, passed=True)
        assert cs.phase == TunerPhase.HARDENED


class TestCooldownDrainLoop:
    """Tests that cooldown drain uses a loop (not recursion)."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0, 1])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )
        eng._session_id = tp.create_session(db, cfg, "", "")
        return eng

    def test_high_cooldown_drains_without_deep_recursion(self, db, simple_topology, mock_smu, mock_backend):
        """Cooldown of 10 drains iteratively without stack overflow."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                         current_offset=-5, crash_cooldown=10),
            1: CoreState(core_id=1, phase=TunerPhase.CONFIRMED,
                         current_offset=-8, best_offset=-8),
        }
        # After draining, core 0 should be picked (cooldown=0)
        # and its test should start. Mock _start_worker to prevent real work.
        with patch.object(eng, "_start_worker"):
            eng._run_next()
        assert eng._core_states[0].crash_cooldown == 0
