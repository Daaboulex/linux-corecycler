"""Tests for the tuner engine state machine."""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch

from history.db import HistoryDB
from tuner.config import TunerConfig
from tuner.engine import TunerEngine
from tuner.state import CoreState
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
        cs = CoreState(core_id=0, phase="not_started", current_offset=0)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == "coarse_search"
        assert cs.current_offset == -5  # 0 + (-1)*5

    def test_coarse_pass_goes_more_aggressive(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase="coarse_search", current_offset=-5)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.best_offset == -5
        assert cs.current_offset == -10

    def test_coarse_pass_at_max_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_offset=-10)
        cs = CoreState(core_id=0, phase="coarse_search", current_offset=-10)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == "settled"
        assert cs.best_offset == -10

    def test_coarse_fail_enters_fine_search(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase="coarse_search", current_offset=-10, best_offset=-5)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == "fine_search"
        assert cs.coarse_fail_offset == -10
        assert cs.current_offset == -6  # best(-5) + direction(-1)*fine(1) = -6

    def test_coarse_fail_no_best_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase="coarse_search", current_offset=-5, best_offset=None)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == "settled"

    def test_fine_pass_continues(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="fine_search", current_offset=-6,
            best_offset=-5, coarse_fail_offset=-10,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == "fine_search"
        assert cs.best_offset == -6
        assert cs.current_offset == -7

    def test_fine_pass_at_coarse_fail_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="fine_search", current_offset=-9,
            best_offset=-8, coarse_fail_offset=-10,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # next would be -10 which equals coarse_fail, so settle
        assert cs.phase == "settled"
        assert cs.best_offset == -9

    def test_fine_fail_settles(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="fine_search", current_offset=-7,
            best_offset=-6, coarse_fail_offset=-10,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == "settled"

    def test_settled_triggers_confirm(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase="settled", current_offset=-8, best_offset=-8)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)  # passed doesn't matter for settled
        assert cs.phase == "confirming"
        assert cs.current_offset == -8

    def test_confirm_pass_marks_confirmed(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(core_id=0, phase="confirming", current_offset=-8, best_offset=-8)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == "confirmed"

    def test_confirm_fail_retries(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_confirm_retries=3)
        cs = CoreState(
            core_id=0, phase="confirming", current_offset=-8,
            best_offset=-8, confirm_attempts=0,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == "confirming"  # retry, not failed yet
        assert cs.confirm_attempts == 1

    def test_confirm_max_retries_backs_off(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_confirm_retries=2)
        cs = CoreState(
            core_id=0, phase="confirming", current_offset=-8,
            best_offset=-8, confirm_attempts=1,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.confirm_attempts == 2
        assert cs.phase == "failed_confirm"

    def test_failed_confirm_enters_backoff(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="failed_confirm", current_offset=-8,
            best_offset=-8, confirm_attempts=2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off: best was -8, direction=-1, so back off = -8 - (-1)*1 = -7
        assert cs.phase == "backoff_preconfirm"
        assert cs.best_offset == -7
        assert cs.current_offset == -7
        assert cs.backoff_mode is True
        assert cs.confirm_attempts == 0

    def test_max_offset_clamp(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, max_offset=-7)
        cs = CoreState(core_id=0, phase="coarse_search", current_offset=-5)
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # Next would be -10, but max is -7 so it settles
        assert cs.phase == "settled"
        assert cs.best_offset == -5


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
            core_id=0, phase="confirmed", current_offset=-20, best_offset=-20,
        ))
        tp.save_core_state(db, sid, CoreState(
            core_id=1, phase="coarse_search", current_offset=-10, best_offset=-5,
        ))

        # Patch _run_next to prevent actual test execution
        with patch.object(eng, "_run_next"):
            eng.resume(sid)

        assert eng._session_id == sid
        assert len(eng._core_states) == 2
        assert eng._core_states[0].phase == "confirmed"
        # Core 1 was mid-coarse_search — treated as failure
        assert eng._core_states[1].phase != "coarse_search"

    def test_resume_reapplies_baseline_offsets(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(cores_to_test=[0], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase="fine_search", current_offset=-12,
            best_offset=-10, baseline_offset=-5,
        ))

        with patch.object(eng, "_run_next"):
            eng.resume(sid)

        # SMU should restore to baseline (not the interrupted offset)
        mock_smu.set_co_offset.assert_any_call(0, -5)


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
            0: CoreState(core_id=0, phase="confirmed"),
            1: CoreState(core_id=1, phase="coarse_search", current_offset=-5),
            2: CoreState(core_id=2, phase="not_started"),
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
            0: CoreState(core_id=0, phase="confirmed"),
            1: CoreState(core_id=1, phase="confirmed"),
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
            0: CoreState(core_id=0, phase="not_started"),
            1: CoreState(core_id=1, phase="not_started"),
        }
        picked = eng._pick_next_core()
        assert picked == 0
        assert eng._core_states[0].phase == "not_started"

    def test_sequential_does_not_advance_settled(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="sequential")
        eng._core_states = {
            0: CoreState(core_id=0, phase="confirmed"),
            1: CoreState(core_id=1, phase="settled", current_offset=-8, best_offset=-8),
        }
        picked = eng._pick_next_core()
        assert picked == 1
        assert eng._core_states[1].phase == "settled"

    def test_round_robin_does_not_advance(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="round_robin")
        eng._core_states = {
            0: CoreState(core_id=0, phase="not_started"),
            1: CoreState(core_id=1, phase="settled", current_offset=-8, best_offset=-8),
            2: CoreState(core_id=2, phase="coarse_search", current_offset=-5),
        }
        eng._pick_next_core()
        assert eng._core_states[0].phase == "not_started"
        assert eng._core_states[1].phase == "settled"

    def test_weakest_first_does_not_advance(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="weakest_first")
        eng._core_states = {
            0: CoreState(core_id=0, phase="not_started"),
            1: CoreState(core_id=1, phase="fine_search", current_offset=-6, best_offset=-5, coarse_fail_offset=-10),
        }
        picked = eng._pick_next_core()
        assert picked == 1  # fine_search scores 0, not_started scores 4
        assert eng._core_states[0].phase == "not_started"

    def test_round_robin_rotates(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, test_order="round_robin")
        eng._core_states = {
            0: CoreState(core_id=0, phase="coarse_search", current_offset=-5),
            1: CoreState(core_id=1, phase="coarse_search", current_offset=-5),
            2: CoreState(core_id=2, phase="coarse_search", current_offset=-5),
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
        assert cs.phase == "coarse_search"
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
            i: CoreState(core_id=i, phase="coarse_search", current_offset=-5)
            for i in range(8)
        }

        order = []
        for _ in range(8):
            picked = eng._pick_next_core()
            if picked is None:
                break
            order.append(picked)
            eng._core_states[picked] = CoreState(
                core_id=picked, phase="confirmed", current_offset=-5, best_offset=-5,
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
            0: CoreState(core_id=0, phase="coarse_search", current_offset=-5),
            1: CoreState(core_id=1, phase="coarse_search", current_offset=-5),
            4: CoreState(core_id=4, phase="confirmed", current_offset=-10, best_offset=-10),
            5: CoreState(core_id=5, phase="confirmed", current_offset=-10, best_offset=-10),
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
            i: CoreState(core_id=i, phase="coarse_search", current_offset=-5)
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
                core_id=picked, phase="confirmed", current_offset=-5, best_offset=-5,
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
    """Tests for the backoff state machine (backoff_preconfirm, backoff_confirming)."""

    def _make_engine(self, db, simple_topology, mock_smu, mock_backend, **cfg_kwargs):
        defaults = dict(coarse_step=5, fine_step=1, max_offset=-30, cores_to_test=[0])
        defaults.update(cfg_kwargs)
        cfg = TunerConfig(**defaults)
        return TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

    def test_failed_confirm_enters_backoff_preconfirm(self, db, simple_topology, mock_smu, mock_backend):
        """failed_confirm backs off by 1 fine step and enters backoff_preconfirm."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="failed_confirm", current_offset=-8,
            best_offset=-8, baseline_offset=-3, confirm_attempts=2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off: -8 - (-1)*1 = -7
        assert cs.phase == "backoff_preconfirm"
        assert cs.best_offset == -7
        assert cs.current_offset == -7
        assert cs.backoff_mode is True
        assert cs.confirm_attempts == 0

    def test_backoff_preconfirm_pass_enters_backoff_confirming(self, db, simple_topology, mock_smu, mock_backend):
        """Pre-confirm pass transitions to backoff_confirming."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-7,
            best_offset=-7, baseline_offset=-3, backoff_mode=True,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == "backoff_confirming"
        assert cs.consecutive_backoff_fails == 0

    def test_backoff_preconfirm_fail_backs_off(self, db, simple_topology, mock_smu, mock_backend):
        """Pre-confirm fail backs off by 1, increments consecutive_backoff_fails."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-7,
            best_offset=-7, baseline_offset=-3, backoff_mode=True,
            consecutive_backoff_fails=0,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == "backoff_preconfirm"
        assert cs.consecutive_backoff_fails == 1
        # Backed off: -7 - (-1)*1 = -6
        assert cs.current_offset == -6
        assert cs.best_offset == -6

    def test_backoff_confirming_pass_confirms(self, db, simple_topology, mock_smu, mock_backend):
        """backoff_confirming pass sets confirmed, backoff_mode=False."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_confirming", current_offset=-6,
            best_offset=-6, baseline_offset=-3, backoff_mode=True,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        assert cs.phase == "confirmed"
        assert cs.backoff_mode is False
        assert cs.confirm_attempts == 0
        assert cs.consecutive_backoff_fails == 0

    def test_backoff_confirming_fail_returns_to_preconfirm(self, db, simple_topology, mock_smu, mock_backend):
        """backoff_confirming fail backs off, resets consecutive_backoff_fails."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_confirming", current_offset=-6,
            best_offset=-6, baseline_offset=-3, backoff_mode=True,
            consecutive_backoff_fails=2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        assert cs.phase == "backoff_preconfirm"
        assert cs.consecutive_backoff_fails == 0
        # Backed off: -6 - (-1)*1 = -5
        assert cs.current_offset == -5
        assert cs.best_offset == -5

    def test_midpoint_jump_after_threshold(self, db, simple_topology, mock_smu, mock_backend):
        """After midpoint_jump_threshold consecutive pre-confirm failures, jump to midpoint."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, midpoint_jump_threshold=3)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-10,
            best_offset=-10, baseline_offset=0, backoff_mode=True,
            consecutive_backoff_fails=2,  # next fail will be the 3rd
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # consecutive_backoff_fails was 2, now becomes 3 >= threshold 3
        # Midpoint jump: baseline(0) + (fail(-10) - baseline(0)) // 2 = -5
        assert cs.phase == "backoff_preconfirm"  # stays to test midpoint
        assert cs.current_offset == -5
        assert cs.best_offset == -5
        assert cs.backoff_fail_bound == -10
        assert cs.consecutive_backoff_fails == 0

    def test_backoff_preconfirm_pass_after_midpoint_sets_bounds(self, db, simple_topology, mock_smu, mock_backend):
        """After midpoint pass, sets backoff_pass_bound and enters binary search."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-5,
            best_offset=-5, baseline_offset=0, backoff_mode=True,
            backoff_fail_bound=-10,  # set from previous midpoint jump
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # Pass at -5 with fail_bound=-10: sets pass_bound=-5
        # Both bounds now set, gap=5 > fine_step(1) → binary search continues
        # Midpoint: -5 + (-10-(-5))//2 = -5 + (-3) = -8
        assert cs.backoff_pass_bound == -5
        assert cs.phase == "backoff_preconfirm"  # still binary searching
        assert cs.current_offset == -8  # midpoint between -5 and -10

    def test_convergence_guard_at_baseline(self, db, simple_topology, mock_smu, mock_backend):
        """When within fine_step of baseline, goes to backoff_confirming at baseline."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, midpoint_jump_threshold=2)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-1,
            best_offset=-1, baseline_offset=0, backoff_mode=True,
            consecutive_backoff_fails=1,  # next fail triggers threshold=2
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # abs(fail(-1) - baseline(0)) = 1 <= fine_step(1), convergence guard
        assert cs.phase == "backoff_confirming"
        assert cs.best_offset == 0  # baseline
        assert cs.current_offset == 0

    def test_binary_search_narrows_on_pass(self, db, simple_topology, mock_smu, mock_backend):
        """Binary search: pass should narrow pass_bound toward fail_bound."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-7,
            best_offset=-7, backoff_mode=True,
            backoff_fail_bound=-10, backoff_pass_bound=-4,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # Pass at -7: update pass_bound to -7
        # New bounds: fail=-10, pass=-7. Gap=3 > fine_step(1)
        # Midpoint: -7 + (-10-(-7))//2 = -7 + (-3)//2 = -7 + (-2) = -9
        assert cs.backoff_pass_bound == -7
        assert cs.current_offset == -9  # midpoint, testing next
        assert cs.phase == "backoff_preconfirm"  # still searching

    def test_binary_search_narrows_on_fail(self, db, simple_topology, mock_smu, mock_backend):
        """Binary search: fail should narrow fail_bound toward pass_bound."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-9,
            best_offset=-9, backoff_mode=True,
            backoff_fail_bound=-10, backoff_pass_bound=-7,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Fail at -9: update fail_bound to -9
        # New bounds: fail=-9, pass=-7. Gap=2 > fine_step(1)
        # Midpoint: -7 + (-9-(-7))//2 = -7 + (-2)//2 = -7 + (-1) = -8
        assert cs.backoff_fail_bound == -9
        assert cs.current_offset == -8
        assert cs.phase == "backoff_preconfirm"

    def test_binary_search_converges(self, db, simple_topology, mock_smu, mock_backend):
        """Binary search should settle when bounds are within fine_step."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-8,
            best_offset=-8, backoff_mode=True,
            backoff_fail_bound=-9, backoff_pass_bound=-7,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=True)
        # Pass at -8: update pass_bound to -8
        # New bounds: fail=-9, pass=-8. Gap=1 = fine_step(1) → converged
        assert cs.backoff_pass_bound == -8
        assert cs.current_offset == -8
        assert cs.best_offset == -8
        assert cs.phase == "backoff_confirming"

    def test_backoff_floor_uses_baseline_not_start(self, db, simple_topology, mock_smu, mock_backend):
        """Backoff stops at baseline_offset, not start_offset."""
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend, start_offset=0)
        # baseline is -3 (inherited from SMU), start is 0
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-4,
            best_offset=-4, baseline_offset=-3, backoff_mode=True,
            consecutive_backoff_fails=0,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Linear backoff: -4 - (-1)*1 = -3 which is at baseline
        # _at_or_past_baseline(-3, cs) with direction=-1: -3 >= -3 = True
        assert cs.phase == "backoff_confirming"
        assert cs.best_offset == -3  # baseline, not start(0)
        assert cs.current_offset == -3


class TestBackoffStateFields:
    def test_core_state_defaults(self):
        cs = CoreState(core_id=0)
        assert cs.backoff_mode is False
        assert cs.consecutive_backoff_fails == 0
        assert cs.backoff_fail_bound is None
        assert cs.backoff_pass_bound is None

    def test_backoff_fields_persist_roundtrip(self, db):
        cfg = TunerConfig(cores_to_test=[0])
        sid = tp.create_session(db, cfg, "", "")
        cs = CoreState(
            core_id=0, phase="backoff_preconfirm", current_offset=-30,
            best_offset=-30, backoff_mode=True,
            consecutive_backoff_fails=2,
            backoff_fail_bound=-33, backoff_pass_bound=-24,
        )
        tp.save_core_state(db, sid, cs)
        loaded = tp.load_core_states(db, sid)
        assert loaded[0].backoff_mode is True
        assert loaded[0].consecutive_backoff_fails == 2
        assert loaded[0].backoff_fail_bound == -33
        assert loaded[0].backoff_pass_bound == -24

    def test_config_backoff_fields(self):
        cfg = TunerConfig()
        assert cfg.backoff_preconfirm_multiplier == 2.0
        assert cfg.midpoint_jump_threshold == 3
        restored = TunerConfig.from_json(cfg.to_json())
        assert restored.backoff_preconfirm_multiplier == 2.0
        assert restored.midpoint_jump_threshold == 3
