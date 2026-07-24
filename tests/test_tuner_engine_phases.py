"""TunerEngine validation, hunt and SMU-write paths, driven without a worker.

Every worker launch is replaced, so the engine's own decisions are exercised:
which cores get which offset written, what a failed SMU write does, how a hunt
slot isolates a core, and how an apparatus fault refuses to move the search.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.history.db import HistoryDB
from corecycler.tuner import engine as eng
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.state import TunerPhase


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


def _topo(cores=4):
    topo = CPUTopology(
        model_name="Test 8C", family=26, model=0x44, physical_cores=cores,
        logical_cpus_count=cores, ccds=1,
    )
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
    return topo


def _smu(write_ok=True):
    smu = MagicMock()
    smu.commands.co_range = (-50, 10)
    smu.is_available.return_value = True
    smu.set_co_offset.return_value = write_ok
    smu.get_co_offset.return_value = 0
    smu.get_all_co_offsets.return_value = dict.fromkeys(range(4), 0)
    smu.get_pbo_scalar.return_value = 1.0
    smu.get_boost_limit.return_value = 5500
    smu.get_ppt_limit.return_value = 225.0
    smu.get_tdc_limit.return_value = 190.0
    smu.get_edc_limit.return_value = 230.0
    return smu


def _backend():
    backend = MagicMock()
    backend.is_available.return_value = True
    backend.name = "mprime"
    return backend


def _config(**over):
    base = {
        "coarse_step": 5,
        "fine_step": 1,
        "max_offset": -30,
        "search_duration_seconds": 1,
        "confirm_duration_seconds": 1,
        "validate_duration_seconds": 1,
        "cores_to_test": [0, 1, 2, 3],
        "inherit_current": False,
    }
    base.update(over)
    return TunerConfig(**base)


@pytest.fixture
def engine(db, tmp_path, monkeypatch):
    instance = TunerEngine(
        db=db,
        topology=_topo(),
        smu=_smu(),
        backend=_backend(),
        config=_config(),
        work_dir=tmp_path,
    )
    monkeypatch.setattr(instance, "_start_worker", MagicMock())
    monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: None)
    instance.start()
    instance._start_worker.reset_mock()
    instance._smu.set_co_offset.reset_mock()
    return instance


def _confirm(instance, core_id, offset):
    cs = instance._core_states[core_id]
    cs.phase = TunerPhase.CONFIRMED
    cs.best_offset = offset
    cs.current_offset = offset
    tp.save_core_state(instance._db, instance._session_id, cs)
    return cs


class TestMemoryBackendDiscovery:
    def test_an_unknown_tool_is_no_backend(self, engine, monkeypatch):
        def _missing(_name):
            raise KeyError(_name)

        monkeypatch.setattr(eng, "get_backend", _missing)
        assert engine._get_memory_backend() is None

    def test_an_uninstalled_tool_is_no_backend(self, engine, monkeypatch):
        backend = MagicMock()
        backend.is_available.return_value = False
        monkeypatch.setattr(eng, "get_backend", lambda _n: backend)
        assert engine._get_memory_backend() is None

    def test_an_installed_tool_is_used(self, engine, monkeypatch):
        backend = MagicMock()
        backend.is_available.return_value = True
        monkeypatch.setattr(eng, "get_backend", lambda _n: backend)
        assert engine._get_memory_backend() is backend


class TestBackoffSelection:
    def test_no_confirmed_core_has_nothing_to_give(self, engine):
        assert engine._find_most_aggressive_core() is None

    def test_a_core_at_its_baseline_has_nothing_to_give(self, engine):
        cs = engine._core_states[1]
        cs.best_offset = cs.baseline_offset
        assert engine._find_most_aggressive_core() is None

    def test_the_deepest_offset_gives_first(self, engine):
        _confirm(engine, 0, -20)
        _confirm(engine, 2, -35)
        _confirm(engine, 3, -10)
        assert engine._find_most_aggressive_core() == 2

    def test_a_core_without_a_best_offset_cannot_back_off(self, engine):
        assert engine._backoff_core(0) is False

    def test_a_core_at_its_baseline_cannot_back_off(self, engine):
        cs = engine._core_states[0]
        cs.best_offset = cs.baseline_offset
        assert engine._backoff_core(0) is False

    def test_a_backoff_steps_one_fine_step(self, engine):
        _confirm(engine, 0, -20)
        assert engine._backoff_core(0) is True
        assert engine._core_states[0].best_offset == -19

    def test_a_backoff_never_passes_the_baseline(self, engine):
        cs = _confirm(engine, 0, -1)
        cs.baseline_offset = 0
        assert engine._backoff_core(0) is True
        assert engine._core_states[0].best_offset == 0
        assert engine._core_states[0].current_offset == 0


class TestValidationOffsetWrites:
    def test_every_confirmed_core_keeps_its_offset(self, engine):
        _confirm(engine, 1, -20)
        _confirm(engine, 2, -25)
        assert engine._apply_validation_offsets(0, -12) is True
        assert engine._co_applied[0] == -12
        assert engine._co_applied[1] == -20
        assert engine._co_applied[2] == -25

    def test_an_already_applied_offset_is_not_rewritten(self, engine):
        _confirm(engine, 1, -20)
        engine._co_applied[1] = -20
        engine._smu.set_co_offset.reset_mock()
        engine._apply_validation_offsets(0, -12)
        written = {call.args[0] for call in engine._smu.set_co_offset.call_args_list}
        assert 1 not in written

    def test_a_rejected_write_on_another_core_pauses(self, engine):
        _confirm(engine, 1, -20)
        engine._smu.set_co_offset.return_value = False
        assert engine._apply_validation_offsets(0, -12) is False
        assert engine.status == "paused"

    def test_a_raising_write_on_another_core_pauses(self, engine):
        _confirm(engine, 1, -20)
        engine._smu.set_co_offset.side_effect = OSError("smu busy")
        assert engine._apply_validation_offsets(0, -12) is False
        assert engine.status == "paused"

    def test_a_rejected_write_on_the_tested_core_pauses(self, engine):
        engine._smu.set_co_offset.side_effect = lambda core, _value: core != 0
        assert engine._apply_validation_offsets(0, -12) is False
        assert engine.status == "paused"

    def test_a_raising_write_on_the_tested_core_pauses(self, engine):
        def _write(core, _value):
            if core == 0:
                raise OSError("smu busy")
            return True

        engine._smu.set_co_offset.side_effect = _write
        assert engine._apply_validation_offsets(0, -12) is False
        assert engine.status == "paused"


class TestIsolationWrites:
    def test_other_cores_go_to_baseline(self, engine):
        _confirm(engine, 1, -20)
        assert engine._apply_co_isolation(0, -12) is True
        assert engine._co_applied[1] == engine._core_states[1].baseline_offset
        assert engine._co_applied[0] == -12

    def test_a_rejected_baseline_revert_pauses(self, engine):
        engine._co_applied[1] = -20
        engine._smu.set_co_offset.return_value = False
        assert engine._apply_co_isolation(0, -12) is False
        assert engine.status == "paused"

    def test_a_raising_baseline_revert_pauses(self, engine):
        engine._co_applied[1] = -20
        engine._smu.set_co_offset.side_effect = OSError("smu busy")
        assert engine._apply_co_isolation(0, -12) is False
        assert engine.status == "paused"

    def test_a_raising_test_write_pauses(self, engine):
        def _write(core, _value):
            if core == 0:
                raise OSError("smu busy")
            return True

        engine._smu.set_co_offset.side_effect = _write
        assert engine._apply_co_isolation(0, -12) is False
        assert engine.status == "paused"


class TestParallelRowLogging:
    def _rows(self, engine, core_id):
        return tp.get_test_log(engine._db, engine._session_id, core_id=core_id)

    def test_a_malformed_payload_records_nothing(self, engine):
        engine._log_parallel_rows(0, "not json", "validate_s2")
        assert self._rows(engine, 1) == []

    def test_a_non_list_payload_records_nothing(self, engine):
        engine._log_parallel_rows(0, json.dumps({"core": 1}), "validate_s2")
        assert self._rows(engine, 1) == []

    def test_every_other_lane_is_recorded(self, engine):
        _confirm(engine, 1, -20)
        engine._co_applied[1] = -20
        payload = json.dumps([
            {"core": 0, "passed": True, "duration": 60.0},
            {"core": 1, "passed": False, "error_message": "rounding",
             "error_type": "computation", "duration": 12.0},
        ])
        engine._log_parallel_rows(0, payload, "validate_s2")
        rows = self._rows(engine, 1)
        assert len(rows) == 1
        assert rows[0]["passed"] == 0
        assert rows[0]["offset_tested"] == -20
        assert rows[0]["error_message"] == "rounding"
        assert self._rows(engine, 0) == []

    def test_unusable_entries_are_skipped(self, engine):
        payload = json.dumps([
            "not a dict",
            {"core": "one", "passed": True},
            {"core": 99, "passed": True},
            {"core": 2, "passed": True, "duration": "soon"},
        ])
        engine._log_parallel_rows(0, payload, "validate_s2")
        rows = self._rows(engine, 2)
        assert len(rows) == 1
        assert rows[0]["duration_seconds"] is None


class TestValidationStageFour:
    def _prepare(self, engine, monkeypatch):
        for cid in (0, 1, 2, 3):
            _confirm(engine, cid, -20)
        engine._validation_core_order = [0, 1, 2, 3]
        engine._validation_stage = 4
        worker = MagicMock()
        monkeypatch.setattr(eng, "_RapidTransitionWorker", MagicMock(return_value=worker))
        return worker

    def test_the_transition_worker_is_launched_for_every_core(self, engine, monkeypatch):
        worker = self._prepare(engine, monkeypatch)
        stages = []
        engine.validation_progress.connect(lambda s, c, t: stages.append((s, c, t)))
        engine._run_validation_stage4()
        assert worker.start.called
        assert stages == [(4, 0, 1)]
        assert engine._cores_under_stress == [0, 1, 2, 3]

    def test_a_failed_offset_write_stops_the_stage(self, engine, monkeypatch):
        worker = self._prepare(engine, monkeypatch)
        engine._smu.set_co_offset.return_value = False
        engine._run_validation_stage4()
        assert not worker.start.called
        assert engine.status == "paused"

    def test_an_unbuildable_scheduler_is_an_apparatus_fault(self, engine, monkeypatch):
        worker = self._prepare(engine, monkeypatch)

        def _boom(**_kwargs):
            raise RuntimeError("no work dir")

        monkeypatch.setattr(eng, "CoreScheduler", _boom)
        failed = []
        monkeypatch.setattr(engine, "_fail_test_async", lambda cid, msg: failed.append((cid, msg)))
        engine._run_validation_stage4()
        assert not worker.start.called
        assert failed[0][0] == 0
        assert "no work dir" in failed[0][1]


class TestValidationSoak:
    def _prepare(self, engine, monkeypatch):
        for cid in (0, 1):
            _confirm(engine, cid, -20)
        engine._validation_core_order = [0, 1]
        engine._validation_stage = 7
        worker = MagicMock()
        monkeypatch.setattr(eng, "_SoakWorker", MagicMock(return_value=worker))
        return worker

    def test_the_soak_watches_with_no_load(self, engine, monkeypatch):
        worker = self._prepare(engine, monkeypatch)
        engine._run_validation_soak()
        assert worker.start.called
        assert engine._soaking is True
        assert engine._last_tested_core == 0

    def test_a_failed_offset_write_stops_the_soak(self, engine, monkeypatch):
        worker = self._prepare(engine, monkeypatch)
        engine._smu.set_co_offset.return_value = False
        engine._run_validation_soak()
        assert not worker.start.called
        assert engine._soaking is False
        assert engine.status == "paused"


class TestHuntSlots:
    def _hunting(self, engine, queue):
        engine._hunting = True
        engine._hunt_queue = list(queue)
        for cid in queue:
            _confirm(engine, cid, -20)
        return engine

    def test_an_aborted_engine_runs_no_slot(self, engine):
        self._hunting(engine, [0])
        engine._abort_requested = True
        engine._run_next_hunt_slot()
        assert engine._hunt_queue == [0]

    def test_a_paused_engine_runs_no_slot(self, engine):
        self._hunting(engine, [0])
        engine._paused = True
        engine._run_next_hunt_slot()
        assert engine._hunt_queue == [0]

    def test_a_slot_isolates_one_core_at_its_offset(self, engine):
        self._hunting(engine, [0])
        engine._run_next_hunt_slot()
        assert engine._co_applied[0] == -20
        assert all(engine._co_applied[c] == 0 for c in (1, 2, 3))
        assert engine._core_states[0].in_test is True
        assert engine._last_tested_core == 0
        engine._start_worker.assert_called_once()

    def test_a_rejected_stock_write_pauses_the_hunt(self, engine):
        self._hunting(engine, [0])
        engine._smu.set_co_offset.return_value = False
        engine._run_next_hunt_slot()
        assert engine.status == "paused"
        assert not engine._start_worker.called

    def test_a_raising_stock_write_pauses_the_hunt(self, engine):
        self._hunting(engine, [0])
        engine._smu.set_co_offset.side_effect = OSError("smu busy")
        engine._run_next_hunt_slot()
        assert engine.status == "paused"

    def test_a_rejected_slot_write_pauses_the_hunt(self, engine):
        self._hunting(engine, [0])
        engine._smu.set_co_offset.side_effect = lambda core, _value: core != 0
        engine._run_next_hunt_slot()
        assert engine.status == "paused"
        assert not engine._start_worker.called

    def test_a_raising_slot_write_pauses_the_hunt(self, engine):
        self._hunting(engine, [0])

        def _write(core, _value):
            if core == 0:
                raise OSError("smu busy")
            return True

        engine._smu.set_co_offset.side_effect = _write
        engine._run_next_hunt_slot()
        assert engine.status == "paused"

    def test_a_passing_slot_moves_to_the_next(self, engine, monkeypatch):
        self._hunting(engine, [0, 1])
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._on_hunt_slot_finished(0, True, "", {})
        assert queued
        assert engine._hunting is True

    def test_an_unknown_core_is_skipped(self, engine, monkeypatch):
        self._hunting(engine, [0])
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._on_hunt_slot_finished(99, False, "crash", {})
        assert queued
        assert engine._hunting is True

    def test_a_failing_slot_names_the_culprit(self, engine):
        self._hunting(engine, [0])
        engine._co_applied[0] = -20
        engine._on_hunt_slot_finished(0, False, "crash", {})
        assert engine._hunting is False
        assert engine.status == "running"
        assert engine._core_states[0].crash_count == 1

    def test_a_non_crash_failure_costs_one_step(self, engine):
        self._hunting(engine, [0])
        engine._co_applied[0] = -20
        engine._on_hunt_slot_finished(0, False, "computation", {})
        assert engine._core_states[0].crash_count == 0
        assert engine._hunting is False


class TestApparatusFault:
    def test_a_fault_retries_the_same_step(self, engine, monkeypatch):
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._handle_apparatus_fault(0, "backend missing", "startup", {})
        assert engine._apparatus_fault_streak == 1
        assert queued

    def test_repeated_faults_stop_the_session(self, engine):
        engine._apparatus_fault_streak = engine._config.max_apparatus_retries
        engine._handle_apparatus_fault(0, "backend missing", "startup", {})
        assert engine.status in ("idle", "aborted")

    def test_a_fault_during_a_hunt_requeues_the_slot(self, engine, monkeypatch):
        engine._hunting = True
        engine._hunt_queue = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, _fn: None)
        engine._handle_apparatus_fault(0, "backend missing", "startup", {})
        assert engine._hunt_queue == [0]

    def test_a_fault_during_validation_reruns_the_stage(self, engine, monkeypatch):
        engine._validation_stage = 1
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._handle_apparatus_fault(0, "backend missing", "startup", {})
        assert queued[0] == engine._run_validation_next

    def test_a_fault_during_a_requeue_reruns_the_requeue(self, engine, monkeypatch):
        engine._validation_stage = 1
        engine._in_requeue = True
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._handle_apparatus_fault(0, "backend missing", "startup", {})
        assert queued[0] == engine._run_validation_requeue

    def test_a_failed_revert_after_a_fault_pauses(self, engine, monkeypatch):
        engine._co_applied[0] = -20
        engine._smu.set_co_offset.return_value = False
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, _fn: None)
        engine._handle_apparatus_fault(0, "backend missing", "startup", {})
        assert engine.status == "paused"


class TestRequeue:
    def test_an_empty_queue_returns_to_the_stage(self, engine, monkeypatch):
        engine._validation_stage = 1
        engine._in_requeue = True
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._run_validation_requeue()
        assert engine._in_requeue is False
        assert queued[0] == engine._run_validation_next

    def test_an_aborted_engine_runs_no_retest(self, engine):
        engine._abort_requested = True
        engine._validation_requeue = [0]
        engine._run_validation_requeue()
        assert not engine._start_worker.called

    def test_a_queued_core_is_retested_solo(self, engine):
        _confirm(engine, 0, -20)
        engine._validation_stage = 1
        engine._validation_requeue = [0]
        engine._run_validation_requeue()
        assert engine._in_requeue is True
        assert engine._cores_under_stress == [0]
        engine._start_worker.assert_called_once()

    def test_a_failed_offset_write_stops_the_retest(self, engine):
        _confirm(engine, 0, -20)
        engine._validation_stage = 1
        engine._validation_requeue = [0]
        engine._smu.set_co_offset.return_value = False
        engine._run_validation_requeue()
        assert not engine._start_worker.called
        assert engine.status == "paused"


class TestValidateProfile:
    def test_a_running_worker_blocks_validation(self, engine):
        worker = MagicMock()
        worker.isRunning.return_value = True
        engine._worker = worker
        engine.validate_profile(engine._session_id)
        assert engine.status == "running"

    def test_a_session_without_confirmed_cores_is_refused(self, engine):
        engine.validate_profile(engine._session_id)
        assert engine.status != "validating"

    def test_an_unusable_saved_config_is_refused(self, engine, db):
        _confirm(engine, 0, -20)
        db._execute_raw(
            "UPDATE tuner_sessions SET config_json=? WHERE id=?",
            (json.dumps({"coarse_step": 0}), engine._session_id),
        )
        engine.validate_profile(engine._session_id)
        assert engine.status != "validating"

    def test_confirmed_cores_are_reset_for_reconfirmation(self, engine):
        _confirm(engine, 0, -20)
        _confirm(engine, 1, -25)
        engine.validate_profile(engine._session_id)
        assert engine.status == "validating"
        assert engine._core_states[0].phase == TunerPhase.CONFIRMING
        assert engine._core_states[0].best_offset == -20
        assert engine._core_states[0].confirm_attempts == 0
        assert engine._core_states[1].current_offset == -25
        assert engine._start_worker.called


class TestStageOneEvidence:
    def test_no_session_has_no_evidence(self, engine):
        engine._session_id = None
        assert engine._has_stage1_pass_at_current_best(0) is False

    def test_an_unknown_core_has_no_evidence(self, engine):
        assert engine._has_stage1_pass_at_current_best(99) is False

    def test_a_core_without_a_best_offset_has_no_evidence(self, engine):
        assert engine._has_stage1_pass_at_current_best(0) is False

    def test_a_logged_stage_one_pass_is_the_evidence(self, engine):
        _confirm(engine, 0, -20)
        tp.log_test_result(
            engine._db, engine._session_id, 0, -20, "validate_s1", True, duration=300.0
        )
        assert engine._has_stage1_pass_at_current_best(0) is True

    def test_a_pass_at_a_different_offset_is_not_the_evidence(self, engine):
        _confirm(engine, 0, -20)
        tp.log_test_result(
            engine._db, engine._session_id, 0, -18, "validate_s1", True, duration=300.0
        )
        assert engine._has_stage1_pass_at_current_best(0) is False

    def test_a_synthetic_row_is_not_the_evidence(self, engine):
        _confirm(engine, 0, -20)
        tp.log_test_result(
            engine._db, engine._session_id, 0, -20, "validate_s1", True, duration=None
        )
        assert engine._has_stage1_pass_at_current_best(0) is False


def test_the_default_work_dir_is_outside_the_repo(db):
    instance = TunerEngine(
        db=db, topology=_topo(), smu=None, backend=_backend(), config=_config()
    )
    assert instance._work_dir == Path("/tmp/corecycler/tuner")
    assert instance.session_id is None
    assert instance.core_states == {}
