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
    smu.set_co_offset = MagicMock()
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

    def test_failed_confirm_backs_off_to_fine(self, db, simple_topology, mock_smu, mock_backend):
        eng = self._make_engine(db, simple_topology, mock_smu, mock_backend)
        cs = CoreState(
            core_id=0, phase="failed_confirm", current_offset=-8,
            best_offset=-8, confirm_attempts=2,
        )
        eng._core_states = {0: cs}
        eng._advance_core(0, passed=False)
        # Back off: best was -8, direction=-1, so back off = -8 - (-1)*1 = -7
        assert cs.phase == "fine_search"
        assert cs.best_offset == -7
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

    def test_resume_reapplies_co_offsets(self, db, simple_topology, mock_smu, mock_backend):
        cfg = TunerConfig(cores_to_test=[0], search_duration_seconds=1)
        eng = TunerEngine(
            db=db, topology=simple_topology, smu=mock_smu,
            backend=mock_backend, config=cfg,
        )

        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase="fine_search", current_offset=-12, best_offset=-10,
        ))

        with patch.object(eng, "_run_next"):
            eng.resume(sid)

        # SMU should have been called to re-apply offset
        mock_smu.set_co_offset.assert_any_call(0, -12)


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
