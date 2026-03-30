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
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
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
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
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
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
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
