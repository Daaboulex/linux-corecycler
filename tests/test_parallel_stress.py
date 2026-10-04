"""Front spec for ParallelStress lane building over the execution engine."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from corecycler.engine import parallel as parallel_mod
from corecycler.engine.backends.base import Outcome, StressConfig, StressResult
from corecycler.engine.parallel import ParallelStress
from corecycler.engine.scheduler import SchedulerConfig
from corecycler.engine.topology import CPUTopology, PhysicalCore


class RecordingBackend:
    name = "fake"


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
    monkeypatch.setattr(parallel_mod, "Supervisor", ScriptedSupervisor)
    return ScriptedSupervisor


def make_topo(cores: dict[int, tuple[int, ...]] | None = None) -> CPUTopology:
    topo = CPUTopology()
    for core_id, cpus in (cores or {0: (16, 0), 1: (1, 17)}).items():
        topo.cores[core_id] = PhysicalCore(core_id=core_id, ccd=0, ccx=None, logical_cpus=cpus)
    return topo


def make_parallel(tmp_path, *, topo=None, backend=None, cores=None) -> ParallelStress:
    return ParallelStress(
        topology=topo or make_topo(),
        backend=backend or RecordingBackend(),
        stress_config=StressConfig(),
        scheduler_config=SchedulerConfig(seconds_per_core=1, poll_interval=0.01, cores_to_test=cores),
        work_dir=tmp_path / "work",
    )


def lane_result(core_id: int, outcome: Outcome = Outcome.PASS, detail: str = "") -> StressResult:
    return StressResult(core_id=core_id, outcome=outcome, detail=detail, duration_seconds=0.1)


def ok(core_id: int) -> StressResult:
    return lane_result(core_id)


def bad(core_id: int) -> StressResult:
    return lane_result(core_id, Outcome.COMPUTATION, "mprime error: FATAL ERROR")


class TestLaneBuilding:
    def test_lanes_cover_every_core_with_sorted_cpus(self, tmp_path):
        runner = make_parallel(tmp_path)
        runner.stress_config.threads = 2
        runner._requested_threads = 2
        seen: list[tuple[int, tuple[int, ...]]] = []

        def capture(sup, lanes, config, duration):
            seen.extend((one.core_id, one.cpus) for one in lanes)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture]
        results = runner.run()
        assert seen == [(0, (0, 16)), (1, (1, 17))]
        assert sorted(results) == [0, 1]
        assert runner.work_dir.is_dir()

    def test_one_thread_lanes_use_one_logical_cpu_each(self, tmp_path):
        runner = make_parallel(tmp_path)
        seen: list[tuple[int, ...]] = []

        def capture(sup, lanes, config, duration):
            seen.extend(one.cpus for one in lanes)
            return {one.core_id: ok(one.core_id) for one in lanes}

        ScriptedSupervisor.script = [capture]
        runner.run()
        assert seen == [(0,), (1,)]

    def test_a_core_outside_the_topology_is_not_started(self, tmp_path):
        runner = make_parallel(tmp_path, cores=[0, 9])
        results = runner.run()
        assert list(results) == [9]
        assert results[9].outcome is Outcome.NOT_STARTED
        assert ScriptedSupervisor.created == []

    def test_an_empty_topology_returns_empty_not_crash(self, tmp_path):
        runner = make_parallel(tmp_path, topo=CPUTopology())
        assert runner.run() == {}

    def test_cut_short_lanes_stay_absent_from_the_results(self, tmp_path):
        runner = make_parallel(tmp_path)

        def one_fails_one_cut(sup, lanes, config, duration):
            return {0: bad(0), 1: lane_result(1, Outcome.CUT_SHORT, "stopped")}

        ScriptedSupervisor.script = [one_fails_one_cut]
        results = runner.run()
        assert list(results) == [0]
        assert results[0].outcome is Outcome.COMPUTATION

    def test_a_reused_instance_starts_clean(self, tmp_path):
        runner = make_parallel(tmp_path)
        ScriptedSupervisor.script = [lambda sup, lanes, c, d: {0: ok(0), 1: ok(1)}]
        runner.stop()
        runner.observed_mce.append(object())
        results = runner.run()
        assert sorted(results) == [0, 1]
        assert runner.observed_mce == []

    def test_stop_sets_the_shared_event(self, tmp_path):
        runner = make_parallel(tmp_path)
        runner.stop()
        assert runner._stop_event.is_set()
        assert type(runner).force_stop is type(runner).stop

    def test_the_default_work_dir_is_per_user(self):
        from corecycler.config.paths import resolve_work_dir

        runner = ParallelStress(
            topology=make_topo(),
            backend=RecordingBackend(),
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
        )
        assert runner.work_dir == resolve_work_dir()
        assert "/tmp/corecycler" not in str(runner.work_dir)
        assert not (Path(__file__).parent.parent / "core_0").exists()
