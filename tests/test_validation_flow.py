"""Incremental multi-core validation: persisted cursor, one-slot retries,
solo re-tests after back-offs, and the final clean pass required for DONE.

Field motivation: one session logged 141 stage-1 tests (~12 hours) because
every single back-off restarted the whole stage for all 16 cores.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from history.db import HistoryDB
from tests.test_crash_attribution import (
    BASELINES,
    BEST,
    _make_engine,
    _seed_hardened_validating,
)
from tuner import persistence as tp
from tuner.state import TunerPhase


@pytest.fixture
def db(tmp_path):
    d = HistoryDB(tmp_path / "test.db")
    yield d
    d.close()


def _seed_validating(eng, db):
    session = _seed_hardened_validating(eng, db, BEST, BASELINES)
    for cs in eng._core_states.values():
        cs.in_test = False
    eng._start_worker = lambda *a, **k: None  # no real worker threads
    eng._start_multi_core_worker = lambda *a, **k: None
    eng._run_validation_stage4 = lambda *a, **k: None
    eng._set_status("validating")
    return session


class TestIncrementalValidation:
    def test_stage1_failure_retries_the_slot_not_the_stage(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 1
        eng._validation_core_index = 5

        eng._on_validation_test_finished(sorted(BEST)[5], passed=False)

        assert eng._validation_core_index == 5  # same slot retries
        assert eng._validation_stage == 1
        assert eng._validation_dirty is True
        sess = tp.get_session(db, eng._session_id)
        assert sess.validation_stage == 1
        assert sess.validation_index == 5
        assert sess.validation_dirty is True

    def test_stage2_failure_requeues_the_failing_core_only(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 2
        before = eng._core_states[5].best_offset

        eng._on_validation_test_finished(5, passed=False)

        assert eng._core_states[5].best_offset == before + 1  # one fine step
        assert eng._validation_requeue == [5]
        assert eng._validation_stage == 2  # stage reruns, not restart
        assert eng._validation_dirty is True

    def test_requeue_pass_returns_to_pending_stage(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 2
        eng._validation_requeue = [5]
        eng._in_requeue = True

        eng._on_validation_test_finished(5, passed=True)

        assert eng._validation_requeue == []
        assert eng._validation_stage == 2
        sess = tp.get_session(db, eng._session_id)
        assert sess.validation_requeue == "[]"

    def test_dirty_completion_runs_one_final_clean_pass(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_halves = [sorted(BEST)[:4], sorted(BEST)[4:]]
        eng._validation_stage = 5  # finalize sentinel
        eng._validation_dirty = True

        eng._run_validation_next()

        assert eng._validation_stage == 1  # clean pass ordered
        assert eng._validation_core_index == 0
        assert eng._validation_dirty is False
        assert eng.status == "validating"

    def test_clean_completion_finalizes(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 5
        eng._validation_dirty = False

        eng._run_validation_next()

        assert eng.status == "idle"
        assert tp.get_session(db, eng._session_id).status == "completed"
        assert tp.get_session(db, eng._session_id).validation_stage == 0


class TestValidationResumePosition:
    def test_reenter_continues_at_persisted_stage_and_requeues_changed_cores(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        sid = eng._session_id
        # Every core has a stage-1 pass at its current best...
        for core_id, offset in BEST.items():
            tp.log_test_result(
                db, sid, core_id, offset, "validate_s1", passed=True, duration=300.0,
            )
        # ...except core 5, whose offset changed after a penalty (no pass at new best).
        eng._core_states[5].best_offset = BEST[5] + 1
        eng._core_states[5].current_offset = BEST[5] + 1
        db.set_validation_position(sid, 2, 0, 0, True, "[]")
        session = tp.get_session(db, sid)

        profile = {c: cs.best_offset for c, cs in eng._core_states.items()}
        eng._enter_auto_validation(profile, resume_from=session)

        assert eng._validation_stage == 2  # position preserved
        assert eng._validation_dirty is True
        assert eng._validation_requeue == [5]  # only the changed core re-tests
        assert eng._in_requeue is True

    def test_malformed_requeue_cursor_fails_closed_to_empty(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        sid = eng._session_id
        for core_id, offset in BEST.items():
            tp.log_test_result(
                db, sid, core_id, offset, "validate_s1", passed=True, duration=300.0,
            )
        db.set_validation_position(sid, 3, 0, 1, False, "not json")
        session = tp.get_session(db, sid)

        profile = {c: cs.best_offset for c, cs in eng._core_states.items()}
        eng._enter_auto_validation(profile, resume_from=session)

        assert eng._validation_stage == 3
        assert eng._validation_half_index == 1
        assert eng._validation_requeue == []

    def test_fresh_entry_resets_cursor(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        db.set_validation_position(eng._session_id, 4, 3, 1, True, "[1]")

        profile = {c: cs.best_offset for c, cs in eng._core_states.items()}
        eng._enter_auto_validation(profile)  # no resume_from — fresh

        assert eng._validation_stage == 1
        assert eng._validation_core_index == 0
        assert eng._validation_dirty is False
        sess = tp.get_session(db, eng._session_id)
        assert (sess.validation_stage, sess.validation_index) == (1, 0)


class TestBackoffKeepsPhase:
    def test_stage1_backoff_leaves_core_confirmed_grade(self, db, topo_dual_ccd_x3d, mock_backend):
        """_backoff_core lowers best without demoting phase — the solo retry
        (not a full re-search) is the re-proof for a soft validation fail."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 1
        eng._validation_core_index = 0

        eng._on_validation_test_finished(sorted(BEST)[0], passed=False)

        cs = eng._core_states[sorted(BEST)[0]]
        assert cs.phase == TunerPhase.HARDENED
        assert cs.best_offset == BEST[sorted(BEST)[0]] + 1
