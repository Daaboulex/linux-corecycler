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
        for cid in engine._core_states:
            if cid not in queue:
                engine._co_applied[cid] = -15
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


def _unattributed_payload():
    return json.dumps([
        {"cpu": -1, "bank": 0, "corrected": True, "message": "kernel oops", "raw_ts": 1.0}
    ])


def _foreign_payload(cpu):
    return json.dumps([
        {"cpu": cpu, "bank": 0, "corrected": True, "message": "corrected", "raw_ts": 1.0}
    ])


class TestVerdictRouter:
    """_on_test_finished is the one door every verdict enters by, and most of
    what arrives is NOT a stability verdict: an aborted run, a thermal stop, an
    apparatus fault, a kernel event naming another core. Each has to be routed
    without moving a CO offset."""

    def test_a_result_arriving_after_abort_only_retires_the_worker(self, engine):
        worker = MagicMock()
        engine._worker = worker
        engine._abort_requested = True
        engine._core_states[0].in_test = True
        engine._on_test_finished(0, True, "", "", 1.0, 0.0)
        assert worker.deleteLater.called
        assert engine._worker is None
        assert engine._core_states[0].in_test is True

    def test_a_result_for_an_unknown_core_is_dropped(self, engine):
        engine._on_test_finished(99, True, "", "", 1.0, 0.0)
        assert engine._worker is None

    def test_a_finished_worker_is_retired(self, engine):
        worker = MagicMock()
        engine._worker = worker
        engine._on_test_finished(0, True, "", "", 1.0, 0.0)
        assert worker.wait.called
        assert worker.deleteLater.called
        assert engine._worker is None

    def test_a_thermal_stop_during_a_hunt_retries_the_same_slot(self, engine):
        engine._hunting = True
        engine._hunt_queue = []
        engine._on_test_finished(0, False, "too hot", "thermal", 1.0, 0.0)
        assert engine._hunt_queue == [0]
        assert engine._validation_thermal_aborts == 1
        assert engine._hunting is True

    def test_repeated_thermal_stops_end_the_hunt_honestly(self, engine):
        engine._hunting = True
        engine._validation_thermal_aborts = engine._config.max_thermal_retries
        engine._on_test_finished(0, False, "too hot", "thermal", 1.0, 0.0)
        assert engine.status == "idle"
        assert engine._abort_requested

    def test_a_pass_with_excess_clock_stretch_is_recorded_as_a_failure(
        self, engine, monkeypatch
    ):
        engine._config.stretch_threshold_pct = 3.0
        advanced = []
        monkeypatch.setattr(engine, "_advance_core", lambda cid, ok: advanced.append(ok))
        engine._on_test_finished(0, True, "", "", 1.0, 9.5)
        assert advanced == [False]

    def test_a_passing_hunt_slot_routes_to_the_hunt_flow(self, engine, monkeypatch):
        engine._hunting = True
        seen = []
        monkeypatch.setattr(
            engine, "_on_hunt_slot_finished", lambda *a: seen.append(a)
        )
        engine._on_test_finished(0, True, "", "", 1.0, 0.0)
        assert seen and seen[0][0] == 0

    def test_a_multi_core_stage_records_every_lane(self, engine, monkeypatch):
        engine._validation_stage = 2
        engine._set_status("validating")
        rows = []
        monkeypatch.setattr(engine, "_log_parallel_rows", lambda *a: rows.append(a))
        engine._on_test_finished(
            0, True, "", "", 1.0, 0.0, "", json.dumps([{"core": 1, "passed": True}])
        )
        assert rows and rows[0][2] == "validate_s2"

    def test_a_core_over_its_time_budget_stops_without_advancing(
        self, engine, monkeypatch
    ):
        monkeypatch.setattr(engine, "_check_time_budget", lambda cs: True)
        advanced = []
        monkeypatch.setattr(engine, "_advance_core", lambda *a: advanced.append(a))
        engine._on_test_finished(0, True, "", "", 1.0, 0.0)
        assert advanced == []

    def test_a_startup_fault_during_validation_reverts_every_core(self, engine):
        _confirm(engine, 1, -20)
        engine._set_status("validating")
        engine._on_test_finished(0, False, "backend missing", "startup", 1.0, 0.0)
        assert engine.status == "paused"
        assert engine._co_applied[1] == engine._core_states[1].baseline_offset


class TestSoakVerdict:
    def _soaking(self, engine):
        engine._soaking = True
        engine._validation_stage = 7
        engine._validation_core_order = [0, 1]
        engine._set_status("validating")
        return engine

    def test_a_quiet_soak_advances_past_the_last_stage(self, engine, monkeypatch):
        self._soaking(engine)
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._on_test_finished(0, True, "", "", 1.0, 0.0)
        assert engine._validation_stage == 8
        assert engine._soaking is False
        assert queued[0] == engine._run_validation_next

    def test_a_first_unattributed_event_re_proves_the_profile(self, engine, monkeypatch):
        self._soaking(engine)
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._on_test_finished(
            0, False, "kernel error", "mce", 1.0, 0.0, _unattributed_payload(), ""
        )
        assert engine._validation_dirty is True
        assert engine._soaking is False
        assert queued[0] == engine._run_validation_next

    def test_repeated_unattributed_events_pause_for_the_owner(self, engine):
        self._soaking(engine)
        engine._config.max_unattributed_crash_hunts = 1
        engine._on_test_finished(
            0, False, "kernel error", "mce", 1.0, 0.0, _unattributed_payload(), ""
        )
        assert engine.status == "paused"

    def test_a_soak_naming_another_core_leaves_validation(self, engine, monkeypatch):
        self._soaking(engine)
        _confirm(engine, 1, -20)
        exited = []
        monkeypatch.setattr(
            engine, "_validation_stage_exit_to_search", lambda: exited.append(True)
        )
        engine._on_test_finished(
            0, False, "kernel error", "mce", 1.0, 0.0, _foreign_payload(1), ""
        )
        assert exited == [True]
        assert engine._validation_dirty is True


class TestApparatusFaultWithEvidence:
    def test_kernel_evidence_during_validation_outranks_the_retry(
        self, engine, monkeypatch
    ):
        _confirm(engine, 1, -20)
        engine._validation_stage = 2
        engine._set_status("validating")
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._handle_apparatus_fault(
            0, "stalled", "stall", engine._foreign_mce_by_core(0, _foreign_payload(1))
        )
        assert engine._validation_stage == 0
        assert engine.status == "running"
        assert queued[0] == engine._run_next
        assert engine._apparatus_fault_streak == 0


class TestAbortTeardown:
    def test_a_worker_that_will_not_exit_is_terminated(self, engine):
        worker = MagicMock()
        worker.isRunning.return_value = True
        worker.wait.return_value = False
        engine._worker = worker
        engine._last_tested_core = 0
        engine.abort()
        assert worker.terminate.called
        assert engine._worker is None
        assert engine.status == "idle"

    def test_aborting_a_hunt_clears_the_queue(self, engine):
        engine._hunting = True
        engine._hunt_queue = [1, 2]
        engine.abort()
        assert engine._hunting is False
        assert engine._hunt_queue == []


class TestWorkerLaunch:
    def _real_launch(self, engine, monkeypatch):
        monkeypatch.setattr(
            engine, "_start_worker", eng.TunerEngine._start_worker.__get__(engine)
        )
        worker = MagicMock()
        factory = MagicMock(return_value=worker)
        monkeypatch.setattr(eng, "_TunerWorker", factory)
        return worker, factory

    def test_an_unreadable_mode_or_preset_falls_back_to_a_safe_default(
        self, engine, monkeypatch
    ):
        from corecycler.engine.backends.base import FFTPreset, StressMode

        worker, factory = self._real_launch(engine, monkeypatch)
        engine._config.stress_mode = "NOT_A_MODE"
        engine._config.fft_preset = "NOT_A_PRESET"
        engine._start_worker(0, 1)
        assert worker.start.called
        scheduler = factory.call_args.args[2]
        assert scheduler.stress_config.mode is StressMode.SSE
        assert scheduler.stress_config.fft_preset is FFTPreset.SMALL

    def test_an_unbuildable_scheduler_is_an_apparatus_fault(self, engine, monkeypatch):
        worker, _factory = self._real_launch(engine, monkeypatch)

        def _boom(**_kwargs):
            raise RuntimeError("no work dir")

        monkeypatch.setattr(eng, "CoreScheduler", _boom)
        failed = []
        monkeypatch.setattr(engine, "_fail_test_async", lambda cid, msg: failed.append((cid, msg)))
        engine._start_worker(0, 1)
        assert not worker.start.called
        assert failed[0][0] == 0
        assert "no work dir" in failed[0][1]

    def test_a_core_outside_the_topology_never_launches(self, engine, monkeypatch):
        worker, _factory = self._real_launch(engine, monkeypatch)
        failed = []
        monkeypatch.setattr(engine, "_fail_test_async", lambda cid, msg: failed.append((cid, msg)))
        engine._start_worker(99, 1)
        assert not worker.start.called
        assert failed[0][0] == 99


class TestMultiCoreLaunch:
    def test_every_core_is_stressed_at_once(self, engine, monkeypatch):
        worker = MagicMock()
        monkeypatch.setattr(eng, "_ParallelWorker", MagicMock(return_value=worker))
        engine._start_multi_core_worker([0, 1, 2], 1)
        assert worker.start.called
        assert engine._worker is worker

    def test_an_unbuildable_runner_is_an_apparatus_fault(self, engine, monkeypatch):
        def _boom(**_kwargs):
            raise RuntimeError("no lanes")

        monkeypatch.setattr(eng, "ParallelStress", _boom)
        failed = []
        monkeypatch.setattr(engine, "_fail_test_async", lambda cid, msg: failed.append((cid, msg)))
        engine._start_multi_core_worker([0, 1], 1)
        assert failed[0][0] == 0


class TestBaselineRevert:
    def test_without_an_smu_the_revert_is_a_noop(self, engine):
        engine._smu = None
        assert engine._revert_core_to_baseline(0) is True

    def test_an_unknown_core_needs_no_revert(self, engine):
        assert engine._revert_core_to_baseline(99) is True

    def test_a_core_already_at_baseline_is_not_rewritten(self, engine):
        engine._co_applied[0] = engine._core_states[0].baseline_offset
        engine._smu.set_co_offset.reset_mock()
        assert engine._revert_core_to_baseline(0) is True
        assert not engine._smu.set_co_offset.called

    def test_a_raising_revert_is_reported_as_failed(self, engine):
        engine._co_applied[0] = -20
        engine._smu.set_co_offset.side_effect = OSError("smu busy")
        assert engine._revert_core_to_baseline(0) is False


class TestMemoryStageDispatch:
    def _ready(self, engine):
        for cid in (0, 1):
            _confirm(engine, cid, -20)
        engine._validation_core_order = [0, 1]
        engine._validation_stage = 6
        return engine

    def test_a_tool_that_vanished_skips_to_the_next_stage(self, engine, monkeypatch):
        self._ready(engine)
        monkeypatch.setattr(engine, "_get_memory_backend", lambda: None)
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._run_validation_memory()
        assert engine._validation_stage == 7
        assert queued[0] == engine._run_validation_next

    def test_a_failed_offset_write_stops_the_stage(self, engine, monkeypatch):
        self._ready(engine)
        monkeypatch.setattr(engine, "_get_memory_backend", lambda: MagicMock())
        launched = []
        monkeypatch.setattr(
            engine, "_start_multi_core_worker", lambda *a, **kw: launched.append(a)
        )
        engine._smu.set_co_offset.return_value = False
        engine._run_validation_memory()
        assert launched == []
        assert engine.status == "paused"


class TestStartRefusals:
    def test_an_invalid_config_starts_no_session(self, engine):
        engine._session_id = None
        engine._config.coarse_step = 0
        engine.start()
        assert engine._session_id is None

    def test_recorded_power_limits_are_narrated(self, db, tmp_path, monkeypatch):
        import corecycler.smu.pmtable as pmtable

        monkeypatch.setattr(pmtable, "read_power_limits", lambda _n: (225.0, 190.0, 230.0))
        instance = TunerEngine(
            db=db, topology=_topo(), smu=_smu(), backend=_backend(),
            config=_config(), work_dir=tmp_path,
        )
        monkeypatch.setattr(instance, "_start_worker", MagicMock())
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: None)
        lines = []
        instance.log_message.connect(lines.append)
        instance.start()
        assert any("PPT 225 W" in line and "EDC 230 A" in line for line in lines)


class TestConfigFallbacks:
    def test_an_unreadable_stress_mode_falls_back_to_sse(self, engine):
        from corecycler.engine.backends.base import StressMode

        engine._config.stress_mode = "NOT_A_MODE"
        assert engine._get_stress_mode() is StressMode.SSE

    def test_an_unreadable_fft_preset_falls_back_to_small(self, engine):
        from corecycler.engine.backends.base import FFTPreset

        engine._config.fft_preset = "NOT_A_PRESET"
        assert engine._get_fft_preset() is FFTPreset.SMALL

    def test_an_unknown_backend_name_is_refused(self, engine):
        with pytest.raises(KeyError):
            engine._get_backend_for_name("not-a-backend")

    def test_without_an_explicit_core_list_every_core_is_tested(self, engine):
        engine._config.cores_to_test = None
        assert engine._get_cores_to_test() == [0, 1, 2, 3]

    def test_an_unknown_test_order_falls_back_to_sequential(self, engine):
        engine._config.test_order = "not-an-order"
        assert engine._pick_next_core() == 0

    def test_a_positive_direction_reads_the_baseline_the_other_way(self, engine):
        cs = engine._core_states[0]
        cs.baseline_offset = 0
        engine._config.direction = 1
        assert engine._at_or_past_baseline(0, cs) is True
        assert engine._at_or_past_baseline(1, cs) is False


class TestEvidenceGuards:
    def test_a_non_object_entry_in_the_payload_is_skipped(self, engine):
        assert engine._foreign_mce_by_core(0, json.dumps(["junk"])) == {}

    def test_an_event_on_an_untested_core_is_dropped(self, engine):
        from corecycler.engine.detector import MCEEvent

        events = [MCEEvent(timestamp=1.0, cpu=99, bank=0, message="x", corrected=True)]
        assert engine._events_by_core(events) == {}

    def test_journal_suspects_need_a_session(self, engine):
        engine._session_id = None
        assert engine._handle_journal_suspects(set()) == []

    def test_a_suspect_for_an_unknown_core_is_skipped(self, engine):
        engine._db.journal_co_intent(engine._session_id, 99, -20, survived=False)
        assert 99 not in engine._handle_journal_suspects(set())

    def test_foreign_evidence_for_an_unknown_core_is_skipped(self, engine):
        engine._apply_foreign_evidence({99: {"messages": ["x"], "corrected": True}})
        assert 99 not in engine._core_states

    def test_foreign_evidence_without_a_resident_value_uses_the_current_offset(self, engine):
        _confirm(engine, 1, -20)
        engine._co_applied.pop(1, None)
        engine._apply_foreign_evidence({1: {"messages": ["x"], "corrected": True}})
        assert engine._core_states[1].crash_count >= 0

    def test_mce_evidence_for_an_unknown_core_is_dropped(self, engine):
        engine._apply_mce_evidence(99, -20, corrected=True, messages=["kernel MCE"])
        assert 99 not in engine._core_states

    def test_an_unknown_core_cannot_be_marked_under_stress(self, engine):
        engine._mark_cores_under_stress([0, 99])
        engine._clear_cores_under_stress()
        assert engine._cores_under_stress == []


class TestLifecycleGuards:
    def test_pause_is_ignored_for_a_quarantined_session(self, engine):
        tp.update_session_status(engine._db, engine._session_id, "quarantined")
        engine.pause()
        assert engine.status != "paused"

    def test_an_empty_hunt_queue_ends_the_hunt(self, engine, monkeypatch):
        engine._hunting = True
        engine._hunt_queue = []
        ended = []
        monkeypatch.setattr(engine, "_end_hunt_fruitless", lambda: ended.append(True))
        engine._run_next_hunt_slot()
        assert ended == [True]

    def test_ending_a_hunt_without_a_session_is_safe(self, engine):
        engine._hunting = True
        engine._session_id = None
        engine._end_hunt_fruitless()
        assert engine._hunting is False

    def test_a_quarantine_survives_a_failing_smu(self, engine, caplog):
        _confirm(engine, 0, -20)
        engine._smu.set_co_offset.side_effect = OSError("smu gone")
        with caplog.at_level("WARNING", logger="corecycler.tuner.engine"):
            engine._quarantine_session(3)
        assert "failed to force core" in caplog.text

    def test_rebuilding_the_cursor_needs_a_session(self, engine):
        engine._session_id = None
        engine._last_tested_core = 3
        engine._reconstruct_scheduling_position()
        assert engine._last_tested_core == 3

    def test_an_aborted_engine_runs_no_next_test(self, engine):
        engine._abort_requested = True
        engine._run_next()
        assert not engine._start_worker.called

    def test_an_aborted_engine_dispatches_no_validation(self, engine):
        engine._abort_requested = True
        engine._run_validation_next()
        assert not engine._start_worker.called

    def test_a_failed_isolation_write_stops_the_search(self, engine):
        engine._smu.set_co_offset.return_value = False
        engine._run_next()
        assert not engine._start_worker.called
        assert engine.status == "paused"

    def test_a_failed_validation_write_stops_the_run(self, engine):
        _confirm(engine, 0, -20)
        engine._set_status("validating")
        engine._smu.set_co_offset.return_value = False
        engine._run_next()
        assert not engine._start_worker.called

    def test_a_validating_run_uses_the_validation_duration(self, engine):
        _confirm(engine, 0, -20)
        engine._set_status("validating")
        engine._run_next()
        assert engine._start_worker.call_args.args[1] == (
            engine._config.validate_duration_seconds
        )

    def test_an_unfinished_set_does_not_complete_the_session(self, engine, monkeypatch):
        finalized = []
        monkeypatch.setattr(engine, "_finalize_session", lambda p: finalized.append(p))
        engine._complete_session()
        assert finalized == []

    def test_a_narrative_write_failure_never_reaches_the_caller(self, engine, monkeypatch):
        def _boom(*_a, **_kw):
            raise OSError("db gone")

        monkeypatch.setattr(eng.tp, "log_event", _boom)
        engine._persist_narrative("something happened")

    def test_reverting_without_an_smu_is_a_noop(self, engine):
        engine._smu = None
        engine._revert_all_to_baseline()

    def test_a_thermal_stop_with_a_failed_revert_pauses(self, engine):
        cs = engine._core_states[0]
        engine._co_applied[0] = -20
        engine._smu.set_co_offset.return_value = False
        engine._handle_thermal_abort(0, cs, 1.0)
        assert engine.status == "paused"


class TestFinalizeAndBackoffExhaustion:
    def test_offsets_that_will_not_apply_are_reported(self, engine, monkeypatch):
        for cid in (0, 1):
            _confirm(engine, cid, -20)
        engine._smu.set_co_offset.side_effect = [False, OSError("smu gone")]
        lines = []
        engine.log_message.connect(lines.append)
        engine._finalize_session({0: -20, 1: -20})
        assert any("Could not apply confirmed offsets" in line for line in lines)

    def test_a_requeued_core_that_cannot_back_off_finalizes(self, engine, monkeypatch):
        cs = _confirm(engine, 0, -20)
        cs.baseline_offset = -20
        engine._validation_stage = 1
        engine._in_requeue = True
        engine._validation_requeue = [0]
        done = []
        monkeypatch.setattr(engine, "_finalize_exhausted", lambda: done.append(True))
        engine._on_validation_test_finished(0, False)
        assert done == [True]

    def test_a_requeued_core_that_fails_backs_off_and_retries_its_slot(
        self, engine, monkeypatch
    ):
        cs = _confirm(engine, 0, -20)
        cs.baseline_offset = 0
        engine._validation_stage = 1
        engine._in_requeue = True
        engine._validation_requeue = [0]
        queued = []
        monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: queued.append(fn))
        engine._on_validation_test_finished(0, False)
        assert engine._validation_dirty is True
        assert engine._core_states[0].best_offset == -19
        assert queued[0] == engine._run_validation_requeue

    def test_a_stage_with_nothing_left_to_give_finalizes(self, engine, monkeypatch):
        for cid in engine._core_states:
            cs = _confirm(engine, cid, 0)
            cs.baseline_offset = 0
        engine._validation_stage = 2
        engine._validation_core_order = sorted(engine._core_states)
        done = []
        monkeypatch.setattr(engine, "_finalize_exhausted", lambda: done.append(True))
        engine._on_validation_test_finished(0, False)
        assert done == [True]


class TestResumeGuards:
    def test_a_live_worker_blocks_the_resume(self, engine):
        worker = MagicMock()
        worker.isRunning.return_value = True
        engine._worker = worker
        sid = engine._session_id
        engine._paused = True
        engine.resume(sid)
        assert engine._paused is True

    def test_an_unknown_session_is_refused(self, engine):
        lines = []
        engine.log_message.connect(lines.append)
        engine.resume(99999)
        assert any("not found" in line for line in lines)

    def test_a_baseline_restore_that_fails_pauses_the_resume(self, engine, monkeypatch):
        sid = engine._session_id
        for cid in (0, 1):
            cs = _confirm(engine, cid, -20)
            cs.baseline_offset = -5
            tp.save_core_state(engine._db, sid, cs)
        tp.update_session_status(engine._db, sid, "paused")
        engine._smu.set_co_offset.return_value = False
        engine._worker = None
        engine.resume(sid)
        assert engine.status == "paused"
        assert not engine._start_worker.called

    def test_a_baseline_restore_error_pauses_the_resume(self, engine):
        sid = engine._session_id
        cs = _confirm(engine, 0, -20)
        cs.baseline_offset = -5
        tp.save_core_state(engine._db, sid, cs)
        tp.update_session_status(engine._db, sid, "paused")
        engine._smu.set_co_offset.side_effect = OSError("smu gone")
        engine._worker = None
        engine.resume(sid)
        assert engine.status == "paused"

    def test_an_abandoned_hunt_is_cleared_on_a_clean_restart(
        self, engine, monkeypatch, assume_clean_shutdown
    ):
        sid = engine._session_id
        _confirm(engine, 0, -20)
        tp.set_hunting_core(engine._db, sid, 0)
        tp.update_session_status(engine._db, sid, "paused")
        monkeypatch.setattr(eng, "_rebooted_since", lambda *_a, **_kw: False)
        engine._worker = None
        engine.resume(sid)
        assert tp.get_session(engine._db, sid).hunting_core is None

    def test_an_unattributed_crash_starts_a_hunt_before_validation(
        self, engine, monkeypatch
    ):
        sid = engine._session_id
        for cid in (0, 1):
            cs = _confirm(engine, cid, -20)
            cs.in_test = True
            tp.save_core_state(engine._db, sid, cs)
        tp.update_session_status(engine._db, sid, "validating")
        tp.journal_mark_survived(engine._db, sid)
        monkeypatch.setattr(eng, "_rebooted_since", lambda *_a, **_kw: True)
        monkeypatch.setattr(engine, "_forensics", lambda *_a, **_kw: ([], True))
        hunts = []
        monkeypatch.setattr(engine, "_start_hunt", lambda: hunts.append(True))
        engine._worker = None
        engine.resume(sid)
        assert hunts == [True]


class TestValidationStageWriteFailures:
    def _staged(self, engine, stage):
        for cid in (0, 1, 2, 3):
            _confirm(engine, cid, -20)
        engine._validation_core_order = [0, 1, 2, 3]
        engine._validation_core_index = 0
        engine._validation_halves = [[0, 1], [2, 3]]
        engine._validation_half_index = 0
        engine._validation_stage = stage
        engine._set_status("validating")
        engine._smu.set_co_offset.return_value = False
        return engine

    def test_stage_one_stops_when_the_offsets_will_not_apply(self, engine):
        self._staged(engine, 1)._run_validation_stage1()
        assert not engine._start_worker.called
        assert engine.status == "paused"

    def test_stage_two_stops_when_the_offsets_will_not_apply(self, engine, monkeypatch):
        launched = []
        monkeypatch.setattr(engine, "_start_multi_core_worker", lambda *a, **kw: launched.append(a))
        self._staged(engine, 2)._run_validation_stage2()
        assert launched == []

    def test_stage_three_stops_when_the_offsets_will_not_apply(self, engine, monkeypatch):
        launched = []
        monkeypatch.setattr(engine, "_start_multi_core_worker", lambda *a, **kw: launched.append(a))
        self._staged(engine, 3)._run_validation_stage3()
        assert launched == []

    def test_stage_five_stops_when_the_offsets_will_not_apply(self, engine):
        self._staged(engine, 5)._run_validation_stage5()
        assert not engine._start_worker.called


class TestRemainingEvidencePaths:
    def test_a_lane_with_no_resident_value_falls_back_to_its_best(self, engine):
        _confirm(engine, 1, -20)
        engine._co_applied.pop(1, None)
        engine._log_parallel_rows(
            0, json.dumps([{"core": 1, "passed": True, "duration": 1.0}]), "validate_s2"
        )
        rows = tp.get_test_log(engine._db, engine._session_id, core_id=1)
        assert rows[0]["offset_tested"] == -20

    def test_a_hunt_slot_applies_evidence_about_other_cores(self, engine, monkeypatch):
        engine._hunting = True
        _confirm(engine, 1, -20)
        applied = []
        monkeypatch.setattr(engine, "_apply_foreign_evidence", lambda f: applied.append(f))
        engine._on_hunt_slot_finished(0, True, "", {1: {"messages": ["x"], "corrected": True}})
        assert applied and 1 in applied[0]

    def test_forensics_that_name_a_core_end_an_open_hunt(self, engine, monkeypatch):
        from corecycler.engine.detector import MCEEvent

        sid = engine._session_id
        _confirm(engine, 1, -20)
        tp.set_hunting_core(engine._db, sid, 1)
        events = [MCEEvent(timestamp=1.0, cpu=1, bank=0, message="corrected", corrected=True)]
        monkeypatch.setattr(engine, "_forensics", lambda *_a, **_kw: (events, True))
        session = tp.get_session(engine._db, sid)
        engine._attribute_crash_after_reboot(session)
        assert tp.get_session(engine._db, sid).hunting_core is None


class TestSearchArithmetic:
    def test_a_first_coarse_step_past_the_limit_is_clamped(self, engine):
        engine._config.coarse_step = 90
        engine._config.max_offset = -30
        cs = engine._core_states[0]
        cs.phase = TunerPhase.NOT_STARTED
        cs.current_offset = 0
        engine._advance_core(0, True)
        assert cs.current_offset == -30

    def test_a_midpoint_that_reaches_the_baseline_settles_there(self, engine):
        cs = engine._core_states[0]
        cs.phase = TunerPhase.BACKOFF_PRECONFIRM
        cs.baseline_offset = 0
        cs.best_offset = -1
        cs.current_offset = -1
        cs.backoff_pass_bound = None
        cs.consecutive_backoff_fails = engine._config.midpoint_jump_threshold - 1
        engine._advance_core(0, False)
        assert cs.phase is TunerPhase.CONFIRMED
        assert cs.best_offset == 0
        assert cs.current_offset == 0
