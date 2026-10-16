from __future__ import annotations

import pytest

from corecycler.tuner.machine.session import ALLCORE_LOADS, transition
from corecycler.tuner.machine.types import (
    AwaitWorker,
    Commit,
    ForgetResident,
    InternalError,
    Klass,
    Lifecycle,
    Load,
    MachineConfig,
    Mode,
    PauseReason,
    RevertAll,
    StopWorker,
    WorkerReported,
    WriteCO,
)
from corecycler.tuner.orders import default_method
from tests._tuner_world import Abort, Close, Fake, Group, Layout, PauseRequest, Report, fresh, run_to_rest, start


def _recording(fake: Fake) -> tuple[list, list]:
    specs, effects = [], []
    check, apply = fake.check_start, fake.apply

    def checked(state, spec):
        specs.append(spec)
        check(state, spec)

    def applied(state, effect):
        effects.append(effect)
        return apply(state, effect)

    fake.check_start, fake.apply = checked, applied
    return specs, effects


def _run(fake: Fake) -> list:
    specs, _ = _recording(fake)
    start(fake)
    run_to_rest(fake, 5000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    return specs


def _in_flight(fake: Fake, allcore: bool):
    start(fake)
    while fake.worker is None or (fake.worker.spec.load in ALLCORE_LOADS) is not allcore:
        assert fake.progress()
    return fake.worker


def _per_core(specs) -> set[tuple[Klass, int, tuple[int, ...]]]:
    return {(spec.klass, spec.core, spec.cpus) for spec in specs if spec.load in (None, Load.ONE_T, Load.TWO_T)}


@pytest.mark.req("K8.cpu", "K9.cpus")
def test_one_thread_runs_on_the_first_cpu_core_0_on_its_second_and_two_threads_on_both():
    specs = _run(fresh(3, [-4, -3, -2], 1, max_offset=-6, k_1t=1, k_2t=1))
    assert _per_core(specs) == {
        (Klass.T1, 0, (1,)),
        (Klass.T1, 1, (2,)),
        (Klass.T1, 2, (4,)),
        (Klass.T2, 0, (0, 1)),
        (Klass.T2, 1, (2, 3)),
        (Klass.T2, 2, (4, 5)),
    }


@pytest.mark.req("K8.cpu")
def test_a_core_with_one_cpu_runs_one_thread_on_it_core_0_included():
    specs = _run(fresh(2, [-4, -3], 1, smt=False, max_offset=-6, k_1t=1, k_2t=1))
    assert _per_core(specs) == {(Klass.T1, 0, (0,)), (Klass.T1, 1, (1,))}


@pytest.mark.req("TERMS.cpus")
def test_every_all_core_memory_and_bisect_load_runs_on_exactly_the_selected_cores_cpus():
    layout = Layout.make(3)
    selected = (0, 2)
    method = default_method(selected, dict(layout.domain_of))
    config = MachineConfig(Mode.CO, method, selected, max_offset=-6, k_1t=1, k_2t=1)
    fake = Fake(layout, {0: -6, 1: -6, 2: -6}, {0: 0, 1: 0, 2: 0}, config=config, edges_hot={0: -2, 1: -2, 2: -2})
    specs = _run(fake)
    shared = [spec for spec in specs if spec.load not in (None, Load.ONE_T, Load.TWO_T, Load.SOAK)]
    assert {spec.load for spec in shared} >= {Load.HOT, Load.MEMORY, Load.BURST}
    assert any(spec.stage.value == "bisect" for spec in shared)
    assert {spec.cpus for spec in shared} == {(0, 1, 4, 5)}


@pytest.mark.req("TERMS.order", "D6")
@pytest.mark.parametrize(
    ("event", "allcore"),
    [(Abort(), False), (Close(), False), (InternalError("boom"), False), (PauseRequest(), True)],
)
def test_with_a_worker_alive_it_is_stopped_the_cores_reverted_and_the_worker_awaited_before_the_commit(event, allcore):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    token = _in_flight(fake, allcore).token
    effects = transition(fake.state, event, fake.world()).effects
    order = [effect for effect in effects if isinstance(effect, (StopWorker, RevertAll, AwaitWorker, Commit))]
    assert [type(effect) for effect in order] == [StopWorker, RevertAll, AwaitWorker, Commit]
    assert order[0].token == order[2].token == token


@pytest.mark.req("TERMS.kvw_cache")
def test_a_test_writes_only_the_cores_whose_resident_value_differs_from_its_vector():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _, effects = _recording(fake)
    start(fake)
    while fake.worker is None:
        assert fake.progress()
    spec = fake.worker.spec
    assert dict(spec.vector) == {0: spec.value, 1: 0} and spec.value != 0
    assert [effect for effect in effects if isinstance(effect, WriteCO)] == [WriteCO(0, 0), WriteCO(1, 0)] + [
        WriteCO(0, spec.value)
    ]


@pytest.mark.req("PRELUDE.4")
@pytest.mark.parametrize("allcore", [False, True])
def test_a_report_clears_in_test_for_every_core_the_test_stressed(allcore):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    worker = _in_flight(fake, allcore)
    stressed = {0, 1} if allcore else {worker.spec.core}
    assert {r.core for r in fake.state.cores if r.in_test} == stressed
    reported = WorkerReported(worker.token, Report(Group.PASS, worker.spec.seconds))
    after = transition(fake.state, reported, fake.world()).state
    assert not any(r.in_test for r in after.cores)


@pytest.mark.req("G7.slept")
def test_a_slept_report_forgets_the_resident_values_so_the_retry_writes_every_owned_core():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _in_flight(fake, False)
    retried = fake.worker.spec
    _, effects = _recording(fake)
    fake.deliver(Report(Group.SLEPT, 1.0))
    assert any(isinstance(effect, ForgetResident) for effect in effects)
    while fake.worker is None:
        assert fake.progress()
    assert (fake.worker.spec.core, fake.worker.spec.value) == (retried.core, retried.value)
    written = {effect.core for effect in effects[effects.index(ForgetResident()) :] if isinstance(effect, WriteCO)}
    assert written == {0, 1}


@pytest.mark.req("L01.G10")
def test_a_refused_baseline_write_at_start_pauses_for_the_smu_write():
    fake = fresh(2, [-3, -2], 1, max_offset=-5, k_1t=1)
    fake.faults.refuse_writes = 1
    start(fake)
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert (fake.state.pause.reason, fake.state.pause.cores) == (PauseReason.SMU_WRITE, (0,))
    assert fake.worker is None
