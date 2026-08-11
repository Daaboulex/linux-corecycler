"""Orchestration spec for CoreScheduler over the execution engine."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from corecycler.engine import execution
from corecycler.engine import scheduler as scheduler_mod
from corecycler.engine.backends.base import StressConfig, StressResult
from corecycler.engine.scheduler import (
    CoreScheduler,
    CoreTestStatus,
    SchedulerConfig,
    TestState,
)
from corecycler.engine.topology import CPUTopology, PhysicalCore


class RecordingBackend:
    name = "fake"

    def __init__(self) -> None:
        self.cleaned: list[tuple[Path, bool]] = []

    def prepare(self, work_dir, config) -> None:
        pass

    def assert_prepared(self, work_dir) -> None:
        pass

    def cleanup(self, work_dir, *, preserve_on_error: bool = False) -> None:
        self.cleaned.append((Path(work_dir), preserve_on_error))


class ScriptedSupervisor:
    script: ClassVar[list] = []
    created: ClassVar[list] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        ScriptedSupervisor.created.append(self)

    def run(self, lanes, config_for, duration):
        step = ScriptedSupervisor.script.pop(0)
        return step(self, lanes, config_for, duration)


@pytest.fixture(autouse=True)
def scripted(monkeypatch):
    ScriptedSupervisor.script = []
    ScriptedSupervisor.created = []
    monkeypatch.setattr(scheduler_mod, "Supervisor", ScriptedSupervisor)
    return ScriptedSupervisor


def make_topo(cores: dict[int, tuple[int, ...]] | None = None) -> CPUTopology:
    topo = CPUTopology()
    for core_id, cpus in (cores or {0: (0, 16), 1: (1, 17)}).items():
        topo.cores[core_id] = PhysicalCore(
            core_id=core_id, ccd=core_id // 8, ccx=None, logical_cpus=cpus
        )
    return topo


def make_scheduler(tmp_path, *, topo=None, backend=None, **config) -> CoreScheduler:
    return CoreScheduler(
        topology=topo or make_topo(),
        backend=backend or RecordingBackend(),
        stress_config=StressConfig(),
        scheduler_config=SchedulerConfig(seconds_per_core=1, poll_interval=0.01, **config),
        work_dir=tmp_path / "work",
    )


def ok(core_id: int) -> StressResult:
    return StressResult(core_id=core_id, passed=True, duration_seconds=0.1)


def bad(core_id: int, msg: str = "mprime error: FATAL ERROR") -> StressResult:
    return StressResult(
        core_id=core_id, passed=False, duration_seconds=0.1,
        error_message=msg, error_type=execution.classify_error(msg),
    )


def step_pass(sup, lanes, config_for, duration):
    for one in lanes:
        config_for(one)
    return {one.core_id: ok(one.core_id) for one in lanes}


def step_fail(sup, lanes, config_for, duration):
    return {one.core_id: bad(one.core_id) for one in lanes}


def step_none(sup, lanes, config_for, duration):
    return {one.core_id: None for one in lanes}


class TestDataShapes:
    def test_states(self):
        assert {s.name for s in TestState} == {"IDLE", "RUNNING", "STOPPING", "FINISHED"}

    def test_status_defaults(self):
        status = CoreTestStatus(core_id=3)
        assert status.state == "pending"
        assert status.iterations == 0
        assert status.errors == 0
        assert status.last_error is None

    def test_config_defaults(self):
        config = SchedulerConfig()
        assert config.seconds_per_core == 360
        assert config.cores_to_test is None
        assert config.stop_on_error is False
        assert config.cycle_count == 1
        assert config.require_thermal_sensor is False


class TestInit:
    def test_initial_state_and_statuses(self, tmp_path):
        sched = make_scheduler(tmp_path)
        assert sched.state == TestState.IDLE
        assert sorted(sched.core_status) == [0, 1]
        assert sched.core_status[0].ccd == 0
        assert sched.results == {0: [], 1: []}

    def test_specific_cores_only(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[1])
        assert sorted(sched.core_status) == [1]

    def test_default_work_dir_is_per_user(self):
        from corecycler.config.paths import resolve_work_dir

        sched = CoreScheduler(
            topology=make_topo(),
            backend=RecordingBackend(),
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
        )
        assert sched.work_dir == resolve_work_dir()
        assert "/tmp/corecycler" not in str(sched.work_dir)

    def test_callbacks_start_empty(self, tmp_path):
        sched = make_scheduler(tmp_path)
        assert sched.on_core_start == []
        assert sched.on_test_complete == []

    def test_classify_delegates_to_the_engine(self):
        message = "MCE during stress (CPU 0,16): bang"
        assert CoreScheduler._classify_error(message) == execution.classify_error(message)


class TestRunOrchestration:
    def test_a_clean_run_records_a_pass_per_core(self, tmp_path):
        sched = make_scheduler(tmp_path)
        ScriptedSupervisor.script = [step_pass, step_pass]
        results = sched.run()
        assert sched.state == TestState.FINISHED
        assert [r[0].passed for r in results.values()] == [True, True]
        assert all(s.state == "passed" for s in sched.core_status.values())
        assert all(s.iterations == 1 for s in sched.core_status.values())
        assert sched.work_dir.is_dir()

    def test_one_thread_means_one_logical_cpu(self, tmp_path):
        sched = make_scheduler(tmp_path)
        seen: list[tuple[tuple[int, ...], int]] = []

        def capture(sup, lanes, config_for, duration):
            seen.extend((one.cpus, config_for(one).threads) for one in lanes)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture, capture]
        sched.run()
        assert seen == [((0,), 1), ((1,), 1)]

    def test_two_threads_mean_both_smt_siblings(self, tmp_path):
        sched = make_scheduler(tmp_path)
        sched.stress_config.threads = 2
        sched._requested_threads = 2
        seen: list[tuple[tuple[int, ...], int]] = []

        def capture(sup, lanes, config_for, duration):
            seen.extend((one.cpus, config_for(one).threads) for one in lanes)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture, capture]
        sched.run()
        assert seen == [((0, 16), 2), ((1, 17), 2)]

    def test_the_requested_width_survives_a_narrow_core(self, tmp_path):
        topo = make_topo({0: (0,), 1: (1, 17)})
        sched = make_scheduler(tmp_path, topo=topo)
        sched.stress_config.threads = 2
        sched._requested_threads = 2
        seen: list[tuple[int, ...]] = []

        def capture(sup, lanes, config_for, duration):
            seen.extend(one.cpus for one in lanes)
            for one in lanes:
                config_for(one)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture, capture]
        sched.run()
        assert seen == [(0,), (1, 17)]

    def test_a_failed_core_is_recorded_and_the_run_continues(self, tmp_path):
        backend = RecordingBackend()
        sched = make_scheduler(tmp_path, backend=backend)
        ScriptedSupervisor.script = [step_fail, step_pass]
        results = sched.run()
        assert results[0][0].passed is False
        assert results[1][0].passed is True
        assert sched.core_status[0].state == "failed"
        assert sched.core_status[0].errors == 1
        assert backend.cleaned[0][1] is True
        assert backend.cleaned[1][1] is False

    def test_stop_on_error_ends_the_run_at_the_failure(self, tmp_path):
        sched = make_scheduler(tmp_path, stop_on_error=True)
        ScriptedSupervisor.script = [step_fail]
        results = sched.run()
        assert results[0][0].passed is False
        assert results[1] == []
        assert sched.core_status[1].state == "pending"

    def test_an_interrupted_core_gets_no_invented_result(self, tmp_path):
        sched = make_scheduler(tmp_path)

        def step_stop_then_none(sup, lanes, config_for, duration):
            sup.kwargs["stop_event"].set()
            return {one.core_id: None for one in lanes}

        ScriptedSupervisor.script = [step_stop_then_none]
        results = sched.run()
        assert results[0] == []
        assert results[1] == []
        assert sched.core_status[0].state == "pending"
        assert sched.core_status[0].iterations == 0

    def test_multiple_cycles_visit_every_core_again(self, tmp_path):
        sched = make_scheduler(tmp_path, cycle_count=2)
        ScriptedSupervisor.script = [step_pass] * 4
        cycles: list[int] = []
        sched.on_cycle_complete.append(cycles.append)
        results = sched.run()
        assert cycles == [0, 1]
        assert [len(r) for r in results.values()] == [2, 2]
        assert all(s.iterations == 2 for s in sched.core_status.values())

    def test_callbacks_fire_in_order(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0])
        ScriptedSupervisor.script = [step_pass]
        events: list[str] = []
        sched.on_core_start.append(lambda cid, cyc: events.append(f"start:{cid}:{cyc}"))
        sched.on_core_finish.append(lambda cid, res: events.append(f"finish:{cid}:{res.passed}"))
        sched.on_test_complete.append(lambda res: events.append("complete"))
        sched.run()
        assert events == ["start:0:0", "finish:0:True", "complete"]

    def test_a_core_missing_from_the_topology_is_skipped(self, tmp_path):
        sched = make_scheduler(tmp_path, topo=make_topo({0: (0, 16)}), cores_to_test=[0, 5])
        ScriptedSupervisor.script = [step_pass]
        results = sched.run()
        assert sched.core_status[5].state == "skipped"
        assert results[5] == []
        assert results[0][0].passed is True

    def test_stop_sets_the_stopping_state(self, tmp_path):
        sched = make_scheduler(tmp_path)
        sched.stop()
        assert sched.state == TestState.STOPPING

    def test_force_stop_is_stop(self, tmp_path):
        sched = make_scheduler(tmp_path)
        sched.force_stop()
        assert sched.state == TestState.STOPPING
        assert sched._stop_requested


class TestIdleComposition:
    def test_inter_core_idle_runs_between_cores(self, tmp_path, monkeypatch):
        calls: list[str] = []

        def fake_idle(**kwargs):
            calls.append(kwargs["phase"])
            return None

        monkeypatch.setattr(execution, "watch_idle", fake_idle)
        sched = make_scheduler(tmp_path, idle_between_cores=0.01)
        ScriptedSupervisor.script = [step_pass, step_pass]
        sched.run()
        assert calls == ["inter-core idle", "inter-core idle"]

    def test_an_idle_error_is_recorded_without_a_verdict_change(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            execution, "watch_idle", lambda **kwargs: "MCE during inter-core idle: bang"
        )
        sched = make_scheduler(tmp_path, idle_between_cores=0.01)
        ScriptedSupervisor.script = [step_pass, step_pass]
        results = sched.run()
        assert results[0][0].passed is True
        assert sched.core_status[0].errors == 1
        assert "MCE" in sched.core_status[0].last_error

    def test_an_idle_stability_error_fails_the_core(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            execution, "watch_idle", lambda **kwargs: "MCE during idle stability: bang"
        )
        sched = make_scheduler(tmp_path, cores_to_test=[0], idle_stability_test=0.01)
        ScriptedSupervisor.script = [step_pass]
        results = sched.run()
        assert results[0][0].passed is False
        assert results[0][0].error_type == "mce"


class TestVariableLoadComposition:
    def test_a_variable_segment_failure_is_attributed(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0], variable_load=True)
        ScriptedSupervisor.script = [step_pass, step_fail]
        results = sched.run()
        assert results[0][0].passed is False
        assert "FATAL" in results[0][0].error_message

    def test_a_clean_variable_phase_keeps_the_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: None)
        sched = make_scheduler(tmp_path, cores_to_test=[0], variable_load=True)

        def quick_pass(sup, lanes, config_for, duration):
            time.sleep(0.05)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [step_pass] + [quick_pass] * 40
        sched.config.seconds_per_core = 0.3
        results = sched.run()
        assert results[0][0].passed is True


class TestRapidTransitions:
    def test_cycles_run_and_report_a_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: None)
        sched = make_scheduler(tmp_path)

        def timed_pass(sup, lanes, config_for, duration):
            time.sleep(0.03)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [timed_pass] * 40
        passed, error = sched.run_rapid_transitions(
            [0, 1], total_duration=0.1, load_seconds=0.03, idle_seconds=0.01
        )
        assert passed is True and error is None
        assert sched.state == TestState.FINISHED

    def test_a_failure_names_its_cycle(self, tmp_path):
        sched = make_scheduler(tmp_path)

        def timed_fail(sup, lanes, config_for, duration):
            time.sleep(0.02)
            return {one.core_id: bad(one.core_id, "mprime crashed with SIGSEGV (exit -11)")
                    for one in lanes}

        ScriptedSupervisor.script = [timed_fail]
        passed, error = sched.run_rapid_transitions([0], total_duration=1.0, load_seconds=0.02)
        assert passed is False
        assert "rapid transition cycle 1" in error

    def test_a_prior_stop_is_honored_without_running(self, tmp_path):
        sched = make_scheduler(tmp_path)
        sched.stop()
        passed, error = sched.run_rapid_transitions([0], total_duration=5.0)
        assert (passed, error) == (True, None)
        assert ScriptedSupervisor.created == []

    def test_unknown_cores_are_a_harness_error(self, tmp_path):
        sched = make_scheduler(tmp_path, topo=make_topo({0: (0,)}))
        passed, error = sched.run_rapid_transitions([7], total_duration=0.1)
        assert passed is False
        assert "harness error" in error

    def test_an_idle_mce_ends_the_cycling(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            execution, "watch_idle",
            lambda **kwargs: f"MCE during {kwargs['phase']}: bang",
        )
        sched = make_scheduler(tmp_path)

        def timed_pass(sup, lanes, config_for, duration):
            time.sleep(0.02)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [timed_pass]
        passed, error = sched.run_rapid_transitions(
            [0], total_duration=1.0, load_seconds=0.02, idle_seconds=0.02
        )
        assert passed is False
        assert "idle phase of rapid transition cycle 1" in error


class TestSignalMarshallingAudit:
    """PySide6 cannot copy-convert dict/list Signals across QThread boundaries."""

    def test_no_signal_dict_in_codebase(self):
        src_dir = Path(__file__).parent.parent / "src"
        violations = []
        for py_file in src_dir.rglob("*.py"):
            for i, line in enumerate(py_file.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if "Signal(dict)" in line or "Signal(list)" in line:
                    violations.append(f"{py_file.relative_to(src_dir)}:{i}: {line.strip()}")
        assert violations == [], (
            "Found Signal(dict) or Signal(list) — these crash across QThread boundaries.\n"
            + "\n".join(violations)
        )


class TestStopThreadSafety:
    def test_stop_from_another_thread_ends_a_running_cycle(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0, 1])

        def slow_pass(sup, lanes, config_for, duration):
            for _ in range(200):
                if sup.kwargs["stop_event"].is_set():
                    return {one.core_id: None for one in lanes}
                time.sleep(0.01)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [slow_pass, slow_pass]
        runner = threading.Thread(target=sched.run)
        runner.start()
        time.sleep(0.05)
        sched.stop()
        runner.join(timeout=5)
        assert not runner.is_alive()
        assert sched.state == TestState.FINISHED
