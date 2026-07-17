"""Incremental multi-core validation: persisted cursor, one-slot retries,
solo re-tests after back-offs, and the final clean pass required for DONE.
A back-off must never restart the whole stage for every core.
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
        eng._validation_stage = 8  # finalize sentinel
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
        eng._validation_stage = 8
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


class TestSpectrumAndSoak:
    def test_disabled_stages_chain_through_to_finalize(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(
            db, topo_dual_ccd_x3d, mock_backend,
            validate_transitions=False, validate_spectrum=False,
            validate_memory=False, validate_soak=False,
        )
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 4

        # Each skip hop is a queued step; drive the chain to completion.
        for _ in range(8):
            if eng.status == "idle":
                break
            eng._run_validation_next()

        assert eng.status == "idle"
        assert tp.get_session(db, eng._session_id).status == "completed"

    def test_stage5_slot_uses_spectrum_profile(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 5
        eng._validation_core_index = 2
        calls = []
        eng._start_worker = lambda core, dur, **kw: calls.append((core, dur, kw))

        eng._run_validation_stage5()

        assert calls == [(sorted(BEST)[2], eng._config.spectrum_slot_seconds,
                          {"spectrum": True})]

    def test_stage5_pass_advances_and_fail_retries_slot(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 5
        eng._validation_core_index = 3

        eng._on_validation_test_finished(sorted(BEST)[3], passed=True)
        assert eng._validation_core_index == 4

        eng._on_validation_test_finished(sorted(BEST)[4], passed=False)
        assert eng._validation_core_index == 4  # same slot retries
        assert eng._validation_stage == 5
        assert eng._validation_dirty is True

    def test_soak_pass_finalizes(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 7
        eng._soaking = True

        eng._on_test_finished(sorted(BEST)[0], True, "", "", 10.0, 0.0, "", "")

        assert eng._validation_stage == 8
        eng._run_validation_next()
        assert eng.status == "idle"
        assert tp.get_session(db, eng._session_id).status == "completed"

    def test_soak_event_demotes_named_core_and_exits_validation(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        import json as _json

        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_validating(eng, db)
        eng._validation_core_order = sorted(BEST)
        eng._validation_stage = 7
        eng._soaking = True
        eng._co_applied[5] = BEST[5]
        cpu = topo_dual_ccd_x3d.cores[5].logical_cpus[0]
        payload = _json.dumps([
            {"cpu": cpu, "bank": 0, "corrected": True, "message": "soak whisper",
             "raw_ts": 1.0},
        ])

        eng._on_test_finished(
            sorted(BEST)[0], False, "kernel error during soak", "mce",
            10.0, 0.0, payload, "",
        )

        cs = eng._core_states[5]
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.backoff_fail_bound == BEST[5]
        assert eng._validation_dirty is True
        assert eng.status == "running"
        assert tp.get_session(db, eng._session_id).validation_stage == 7

    def test_stage_count_reflects_flags(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        assert eng._get_validation_stage_count() == 7
        eng._config.validate_transitions = False
        eng._config.validate_soak = False
        assert eng._get_validation_stage_count() == 5


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


class _FakeMemBackend:
    """A stand-in memory backend that reports itself available."""

    name = "stressapptest"

    def is_available(self):
        return True


class TestMemoryValidationStage:
    """Stage 6 memory-load validation drives the REAL dispatch/advance state
    machine: it runs only when enabled AND a memory tool is present, uses the
    memory backend (not the CPU backend), advances to soak on pass, and backs
    off the failing core on fail. No mock asserts — every check reads the real
    cursor and the captured dispatch decision."""

    def _seed(self, db, topo, backend, **cfg):
        eng = _make_engine(db, topo, backend, **cfg)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
        eng._start_worker = lambda *a, **k: None
        eng._run_validation_stage4 = lambda *a, **k: None
        eng._set_status("validating")
        eng._validation_core_order = sorted(BEST)
        return eng

    def test_memory_stage_runs_with_memory_backend_when_available(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = self._seed(db, topo_dual_ccd_x3d, mock_backend)
        captured = {}
        eng._get_memory_backend = lambda: _FakeMemBackend()
        eng._start_multi_core_worker = (
            lambda cores, dur, backend=None: captured.update(
                cores=list(cores), backend=backend
            )
        )
        eng._validation_stage = 6

        eng._run_validation_next()

        assert captured["cores"] == sorted(BEST)  # every core stressed together
        assert isinstance(captured["backend"], _FakeMemBackend)  # memory, not CPU
        assert eng._validation_stage == 6

    def test_memory_pass_advances_to_soak(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = self._seed(db, topo_dual_ccd_x3d, mock_backend)
        eng._validation_stage = 6

        eng._on_validation_test_finished(sorted(BEST)[0], passed=True)

        assert eng._validation_stage == 7  # soak is next
        assert tp.get_session(db, eng._session_id).validation_stage == 7

    def test_memory_failure_backs_off_failing_core_and_requeues(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = self._seed(db, topo_dual_ccd_x3d, mock_backend)
        eng._start_multi_core_worker = lambda *a, **k: None
        eng._validation_stage = 6
        before = eng._core_states[5].best_offset

        eng._on_validation_test_finished(5, passed=False)

        assert eng._core_states[5].best_offset == before + 1  # one fine step
        assert eng._validation_requeue == [5]  # solo re-prove, like stage 2/3
        assert eng._validation_stage == 6  # stage reruns, not a restart
        assert eng._validation_dirty is True

    def test_memory_stage_skipped_when_disabled(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = self._seed(db, topo_dual_ccd_x3d, mock_backend, validate_memory=False)
        ran = []
        eng._get_memory_backend = lambda: _FakeMemBackend()
        eng._start_multi_core_worker = lambda *a, **k: ran.append(True)
        eng._validation_stage = 6

        eng._run_validation_next()

        assert ran == []  # memory did not run
        assert eng._validation_stage == 7  # jumped straight to soak

    def test_memory_stage_skipped_when_no_tool_installed(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = self._seed(db, topo_dual_ccd_x3d, mock_backend)
        ran = []
        eng._get_memory_backend = lambda: None  # stressapptest not installed
        eng._start_multi_core_worker = lambda *a, **k: ran.append(True)
        eng._validation_stage = 6

        eng._run_validation_next()

        assert ran == []  # skipped, not failed
        assert eng._validation_stage == 7  # advanced to soak

    def test_get_memory_backend_returns_none_without_stressapptest(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        # In the test/sandbox environment stressapptest is not installed, so the
        # real availability check must yield None (not raise, not a CPU backend).
        assert eng._get_memory_backend() is None
