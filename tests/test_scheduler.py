"""Orchestration spec for CoreScheduler over the execution engine."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from corecycler.engine import execution
from corecycler.engine import scheduler as scheduler_mod
from corecycler.engine.backends.base import LaneSignal, Outcome, StressConfig, StressResult
from corecycler.engine.scheduler import (
    CoreScheduler,
    CoreTestStatus,
    SchedulerConfig,
    TestState,
)
from corecycler.engine.topology import CPUTopology, PhysicalCore


class RecordingBackend:
    name = "fake"

    def prepare(self, work_dir, config) -> None:
        pass

    def assert_prepared(self, work_dir) -> None:
        pass


class ScriptedSupervisor:
    script: ClassVar[list] = []
    created: ClassVar[list] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        ScriptedSupervisor.created.append(self)

    def run(self, lanes, config, duration):
        step = ScriptedSupervisor.script.pop(0)
        return step(self, lanes, config, duration)


@pytest.fixture(autouse=True)
def scripted(monkeypatch):
    ScriptedSupervisor.script = []
    ScriptedSupervisor.created = []
    monkeypatch.setattr(scheduler_mod, "Supervisor", ScriptedSupervisor)
    return ScriptedSupervisor


def make_topo(cores: dict[int, tuple[int, ...]] | None = None) -> CPUTopology:
    topo = CPUTopology()
    for core_id, cpus in (cores or {0: (0, 16), 1: (1, 17)}).items():
        topo.cores[core_id] = PhysicalCore(core_id=core_id, ccd=core_id // 8, ccx=None, logical_cpus=cpus)
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
    return StressResult(core_id=core_id, outcome=Outcome.PASS, detail="", duration_seconds=0.1)


def bad(core_id: int, msg: str = "mprime error: FATAL ERROR", outcome: Outcome = Outcome.COMPUTATION) -> StressResult:
    return StressResult(core_id=core_id, outcome=outcome, detail=msg, duration_seconds=0.1)


def cut(core_id: int) -> StressResult:
    return StressResult(core_id=core_id, outcome=Outcome.CUT_SHORT, detail="stopped", duration_seconds=0.1)


def step_pass(sup, lanes, config, duration):
    return {one.core_id: ok(one.core_id) for one in lanes}


def step_fail(sup, lanes, config, duration):
    return {one.core_id: bad(one.core_id) for one in lanes}


def returns(outcome: Outcome, detail: str):
    def step(sup, lanes, config, duration):
        return {one.core_id: bad(one.core_id, detail, outcome) for one in lanes}

    return step


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


class TestRunOrchestration:
    def test_a_clean_run_records_a_pass_per_core(self, tmp_path):
        sched = make_scheduler(tmp_path)
        ScriptedSupervisor.script = [step_pass, step_pass]
        results = sched.run()
        assert sched.state == TestState.FINISHED
        assert [r[0].outcome for r in results.values()] == [Outcome.PASS, Outcome.PASS]
        assert all(s.state == "passed" for s in sched.core_status.values())
        assert all(s.iterations == 1 for s in sched.core_status.values())

    def test_one_thread_means_one_logical_cpu(self, tmp_path):
        sched = make_scheduler(tmp_path)
        seen: list[tuple[int, ...]] = []

        def capture(sup, lanes, config, duration):
            seen.extend(one.cpus for one in lanes)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture, capture]
        sched.run()
        assert seen == [(0,), (1,)]

    def test_two_threads_mean_both_smt_siblings(self, tmp_path):
        sched = make_scheduler(tmp_path)
        sched.stress_config.threads = 2
        sched._requested_threads = 2
        seen: list[tuple[int, ...]] = []

        def capture(sup, lanes, config, duration):
            seen.extend(one.cpus for one in lanes)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture, capture]
        sched.run()
        assert seen == [(0, 16), (1, 17)]

    def test_the_requested_width_survives_a_narrow_core(self, tmp_path):
        topo = make_topo({0: (0,), 1: (1, 17)})
        sched = make_scheduler(tmp_path, topo=topo)
        sched.stress_config.threads = 2
        sched._requested_threads = 2
        seen: list[tuple[tuple[int, ...], int]] = []

        def capture(sup, lanes, config, duration):
            seen.extend((one.cpus, config.threads) for one in lanes)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture, capture]
        sched.run()
        assert seen == [((0,), 2), ((1, 17), 2)]

    def test_a_failed_core_is_recorded_and_the_run_continues(self, tmp_path):
        sched = make_scheduler(tmp_path)
        ScriptedSupervisor.script = [step_fail, step_pass]
        results = sched.run()
        assert results[0][0].outcome is Outcome.COMPUTATION
        assert results[1][0].outcome is Outcome.PASS
        assert sched.core_status[0].state == "failed"
        assert sched.core_status[0].errors == 1
        assert sched.core_status[0].last_error == "mprime error: FATAL ERROR"

    def test_a_failed_cores_evidence_dir_reaches_the_result(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0])
        evidence = tmp_path / "work" / "core_0" / "run-20261004T000000000000"
        kept = StressResult(
            core_id=0, outcome=Outcome.COMPUTATION, detail="fail", duration_seconds=0.1, evidence_dir=evidence
        )
        ScriptedSupervisor.script = [lambda sup, lanes, c, d: {0: kept}]
        assert sched.run()[0][0].evidence_dir == evidence

    def test_stop_on_error_ends_the_run_at_the_failure(self, tmp_path):
        sched = make_scheduler(tmp_path, stop_on_error=True)
        ScriptedSupervisor.script = [step_fail]
        results = sched.run()
        assert results[0][0].passed is False
        assert results[1] == []
        assert sched.core_status[1].state == "pending"

    def test_the_supervisors_outcome_reaches_the_result_unchanged(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0])
        ScriptedSupervisor.script = [returns(Outcome.NOT_STARTED, "stress exited with code 2 at startup")]
        results = sched.run()
        assert results[0][0].outcome is Outcome.NOT_STARTED
        assert results[0][0].detail == "stress exited with code 2 at startup"

    def test_the_result_carries_the_slot_time_and_iteration(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0], cycle_count=2)

        def slow_pass(sup, lanes, config, duration):
            time.sleep(0.05)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [slow_pass, slow_pass]
        results = sched.run()
        assert [r.iterations_completed for r in results[0]] == [1, 2]
        assert all(r.duration_seconds >= 0.05 for r in results[0])

    def test_an_environment_outcome_ends_the_run_without_stop_on_error(self, tmp_path):
        sched = make_scheduler(tmp_path)
        ScriptedSupervisor.script = [returns(Outcome.THERMAL, "CPU temperature exceeded 95.0 C")]
        results = sched.run()
        assert results[0][0].outcome is Outcome.THERMAL
        assert results[1] == []
        assert sched._stop_requested

    def test_a_silicon_outcome_without_stop_on_error_keeps_going(self, tmp_path):
        sched = make_scheduler(tmp_path)
        ScriptedSupervisor.script = [returns(Outcome.OWN_CORE_MCE, "MCE during stress: x"), step_pass]
        results = sched.run()
        assert [r[0].outcome for r in results.values()] == [Outcome.OWN_CORE_MCE, Outcome.PASS]
        assert not sched._stop_requested

    def test_an_interrupted_core_gets_no_invented_result(self, tmp_path):
        sched = make_scheduler(tmp_path)

        def step_stop_then_cut(sup, lanes, config, duration):
            sup.kwargs["stop_event"].set()
            return {one.core_id: cut(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [step_stop_then_cut]
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
        found = LaneSignal(Outcome.OWN_CORE_MCE, "MCE during inter-core idle: bang")
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: found)
        sched = make_scheduler(tmp_path, idle_between_cores=0.01)
        ScriptedSupervisor.script = [step_pass, step_pass]
        results = sched.run()
        assert results[0][0].passed is True
        assert sched.core_status[0].errors == 1
        assert "MCE" in sched.core_status[0].last_error
        assert results[1][0].passed is True

    def test_an_environment_event_while_idle_between_cores_stops_the_run(self, tmp_path, monkeypatch):
        found = LaneSignal(Outcome.THERMAL, "CPU temperature exceeded 95.0 C safety limit during inter-core idle")
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: found)
        sched = make_scheduler(tmp_path, idle_between_cores=0.01)
        ScriptedSupervisor.script = [step_pass, step_pass]
        results = sched.run()
        assert results[0][0].passed is True
        assert results[1] == []

    def test_an_idle_stability_error_fails_the_core(self, tmp_path, monkeypatch):
        found = LaneSignal(Outcome.OWN_CORE_MCE, "MCE during idle stability: bang")
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: found)
        sched = make_scheduler(tmp_path, cores_to_test=[0], idle_stability_test=0.01)
        ScriptedSupervisor.script = [step_pass]
        results = sched.run()
        assert results[0][0].outcome is Outcome.OWN_CORE_MCE
        assert results[0][0].detail == "MCE during idle stability: bang"

    def test_an_idle_phase_failure_keeps_its_own_outcome(self, tmp_path, monkeypatch):
        found = LaneSignal(Outcome.UNATTRIBUTED_MCE, "Machine check without core attribution during idle stability")
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: found)
        sched = make_scheduler(tmp_path, idle_stability_test=0.01)
        ScriptedSupervisor.script = [step_pass]
        results = sched.run()
        assert results[0][0].outcome is Outcome.UNATTRIBUTED_MCE
        assert results[1] == []


class TestVariableLoadComposition:
    def test_a_variable_segment_failure_is_attributed(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0], variable_load=True, variable_load_interval=0.1)
        ScriptedSupervisor.script = [step_pass, step_fail]
        results = sched.run()
        assert results[0][0].outcome is Outcome.COMPUTATION
        assert "FATAL" in results[0][0].detail

    def test_a_variable_phase_too_short_for_one_segment_is_no_work_done(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0], variable_load=True, stop_on_error=True)
        ScriptedSupervisor.script = [step_pass]
        results = sched.run()
        assert results[0][0].outcome is Outcome.NO_WORK_DONE
        assert "no work done" in results[0][0].detail
        assert sched._stop_requested

    def test_a_clean_variable_phase_keeps_the_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: None)
        sched = make_scheduler(tmp_path, cores_to_test=[0], variable_load=True, variable_load_interval=0.02)

        def quick_pass(sup, lanes, config, duration):
            time.sleep(0.05)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [step_pass] + [quick_pass] * 40
        sched.config.seconds_per_core = 0.3
        results = sched.run()
        assert results[0][0].outcome is Outcome.PASS

    def test_an_idle_transition_failure_keeps_its_outcome(self, tmp_path, monkeypatch):
        found = LaneSignal(Outcome.UNATTRIBUTED_MCE, "Machine check without core attribution during idle transition")
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: found)
        sched = make_scheduler(tmp_path, cores_to_test=[0], variable_load=True, variable_load_interval=0.1)
        ScriptedSupervisor.script = [step_pass, step_pass]
        results = sched.run()
        assert results[0][0].outcome is Outcome.UNATTRIBUTED_MCE

    def test_a_stop_during_the_variable_phase_is_cut_short_not_a_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: None)
        sched = make_scheduler(tmp_path, variable_load_interval=0.02)

        def pass_then_stop(sup, lanes, config, duration):
            sup.kwargs["stop_event"].set()
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [pass_then_stop]
        result = sched._run_variable_load(sched._lane_for(0), 1.0)
        assert result.outcome is Outcome.CUT_SHORT

    def test_the_variable_phase_never_launches_a_sliver_segment(self, tmp_path, monkeypatch):
        def idle(**kwargs):
            time.sleep(kwargs["duration"])

        monkeypatch.setattr(execution, "watch_idle", idle)
        sched = make_scheduler(tmp_path, cores_to_test=[0], variable_load_interval=0.4)
        segments: list[float] = []

        def timed_pass(sup, lanes, config, duration):
            segments.append(duration)
            time.sleep(duration)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [timed_pass] * 5
        sched._run_variable_load(sched._lane_for(0), 1.0)
        assert segments
        assert min(segments) >= 0.4


class TestRapidTransitions:
    def test_cycles_run_and_report_a_pass(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: None)
        sched = make_scheduler(tmp_path)

        def timed_pass(sup, lanes, config, duration):
            time.sleep(0.03)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [timed_pass] * 40
        result = sched.run_rapid_transitions([0, 1], total_duration=0.1, load_seconds=0.03, idle_seconds=0.01)
        assert result.outcome is Outcome.PASS
        assert result.core_id == 0
        assert sched.state == TestState.FINISHED

    def test_a_failure_names_its_cycle(self, tmp_path):
        sched = make_scheduler(tmp_path)

        def timed_fail(sup, lanes, config, duration):
            time.sleep(0.02)
            return {
                one.core_id: bad(one.core_id, "stress process died of SIGSEGV", Outcome.CPU_SIGNAL) for one in lanes
            }

        ScriptedSupervisor.script = [timed_fail]
        result = sched.run_rapid_transitions([0], total_duration=1.0, load_seconds=0.02)
        assert result.outcome is Outcome.CPU_SIGNAL
        assert "rapid transition cycle 1" in result.detail

    def test_a_rapid_cycle_failure_keeps_its_outcome(self, tmp_path):
        sched = make_scheduler(tmp_path)
        ScriptedSupervisor.script = [returns(Outcome.THERMAL, "CPU temperature exceeded 95.0 C safety limit")]
        result = sched.run_rapid_transitions([0], total_duration=1.0, load_seconds=0.02)
        assert result.outcome is Outcome.THERMAL

    def test_a_prior_stop_is_honored_without_running(self, tmp_path):
        sched = make_scheduler(tmp_path)
        sched.stop()
        result = sched.run_rapid_transitions([0], total_duration=5.0)
        assert result.outcome is Outcome.CUT_SHORT
        assert ScriptedSupervisor.created == []

    def test_a_run_too_short_for_one_load_segment_is_no_work_done(self, tmp_path):
        sched = make_scheduler(tmp_path)
        result = sched.run_rapid_transitions([0], total_duration=0.01, load_seconds=0.02)
        assert result.outcome is Outcome.NO_WORK_DONE
        assert "no work done" in result.detail
        assert ScriptedSupervisor.created == []

    def test_unknown_cores_are_not_started(self, tmp_path):
        sched = make_scheduler(tmp_path, topo=make_topo({0: (0,)}))
        result = sched.run_rapid_transitions([7], total_duration=0.1)
        assert result.outcome is Outcome.NOT_STARTED
        assert result.core_id == 7
        assert "harness error" in result.detail

    def test_an_idle_mce_ends_the_cycling(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            execution,
            "watch_idle",
            lambda **kwargs: LaneSignal(Outcome.OWN_CORE_MCE, f"MCE during {kwargs['phase']}: bang"),
        )
        sched = make_scheduler(tmp_path)

        def timed_pass(sup, lanes, config, duration):
            time.sleep(0.02)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [timed_pass]
        result = sched.run_rapid_transitions([0], total_duration=1.0, load_seconds=0.02, idle_seconds=0.02)
        assert result.outcome is Outcome.OWN_CORE_MCE
        assert "idle phase of rapid transition cycle 1" in result.detail


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
            "Found Signal(dict) or Signal(list) — these crash across QThread boundaries.\n" + "\n".join(violations)
        )


class TestStopThreadSafety:
    def test_stop_from_another_thread_ends_a_running_cycle(self, tmp_path):
        sched = make_scheduler(tmp_path, cores_to_test=[0, 1])

        def slow_pass(sup, lanes, config, duration):
            for _ in range(200):
                if sup.kwargs["stop_event"].is_set():
                    return {one.core_id: cut(one.core_id) for one in lanes}
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
