from __future__ import annotations

from dataclasses import replace

import pytest

from corecycler.tuner.machine import allcore
from corecycler.tuner.machine.session import ALLCORE_LOADS, transition
from corecycler.tuner.machine.types import (
    Announce,
    AwaitWorker,
    Commit,
    CrashEvidenceRead,
    Evidence,
    FailContext,
    ForgetResident,
    InternalError,
    Klass,
    Lifecycle,
    Load,
    MachineConfig,
    Mode,
    PauseReason,
    Phase,
    RevertAll,
    Schedule,
    Stage,
    StartWorker,
    StopWorker,
    Waiting,
    WorkerReported,
    WriteCO,
)
from corecycler.tuner.orders import default_method, round_sequence
from tests._tuner_world import (
    Abort,
    Close,
    Fake,
    Group,
    Layout,
    PauseRequest,
    Report,
    Resume,
    fresh,
    run_to_rest,
    start,
)


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


def _until(fake: Fake, wanted) -> object:
    if fake.state.lifecycle is Lifecycle.IDLE:
        start(fake)
    while fake.worker is None or not wanted(fake.worker.spec, fake.state):
        assert fake.progress()
    return fake.worker


def _next_worker(fake: Fake):
    while fake.worker is None:
        assert fake.progress()
    return fake.worker.spec


def _live1(spec, state) -> bool:
    return spec.stage is Stage.LIVE1


def _steady(spec, state) -> bool:
    return spec.stage is Stage.STEADY


@pytest.mark.req("C20")
def test_the_wait_between_cores_comes_before_a_different_core_and_never_before_a_repeat():
    fake = fresh(3, [-7, -4, -2], 1, max_offset=-9, k_1t=2, k_2t=1, wait_between_cores_s=15.0)
    _, effects = _recording(fake)
    start(fake)
    run_to_rest(fake, 5000)
    started, waited = [], False
    for effect in effects:
        if isinstance(effect, Schedule) and effect.seconds == 15.0:
            waited = True
        elif isinstance(effect, StartWorker):
            started.append((effect.spec, waited))
            waited = False
    per_core = (Stage.SEARCH, Stage.LIVE1)
    pairs = [
        (a.core, b.core, wait)
        for (a, _), (b, wait) in zip(started, started[1:], strict=False)
        if a.stage in per_core and b.stage in per_core
    ]
    assert {(a == b, wait) for a, b, wait in pairs} == {(True, False), (False, True)}


@pytest.mark.req("C20")
def test_a_one_core_session_never_waits_between_its_tests():
    fake = fresh(1, [-5], 1, max_offset=-9, k_1t=2, k_2t=1, wait_between_cores_s=15.0)
    _, effects = _recording(fake)
    start(fake)
    run_to_rest(fake, 5000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    started = [effect.spec for effect in effects if isinstance(effect, StartWorker)]
    assert len([spec for spec in started if spec.stage in (Stage.SEARCH, Stage.LIVE1)]) > 3
    assert [effect for effect in effects if isinstance(effect, Schedule) and effect.seconds == 15.0] == []


@pytest.mark.req("C20")
def test_a_core_picked_before_a_pause_starts_at_once_on_resume_without_a_second_wait():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, wait_between_cores_s=15.0)
    start(fake)
    fake.fire()
    fake.deliver()
    fake.fire()
    assert isinstance(fake.state.slot, Waiting) and fake.state.slot.picked == 1
    fake.post(PauseRequest())
    _, effects = _recording(fake)
    fake.post(Resume(1))
    assert _next_worker(fake).core == 1
    assert [effect.seconds for effect in effects if isinstance(effect, Schedule)] == [0.0]


@pytest.mark.req("G3.backoff")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_evidence_on_another_owned_core_backs_it_off_in_place_sets_its_bound_and_marks_dirty(corrected, steps):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    worker = _until(fake, lambda spec, state: spec.stage is Stage.LIVE1 and spec.core == 0)
    before = fake.state.core(1)
    assert before.value < -steps and not fake.state.flow.dirty
    report = Report(Group.PASS, worker.spec.seconds, evidence=(Evidence(1, corrected),))
    after = transition(fake.state, WorkerReported(worker.token, report), fake.world()).state
    moved = after.core(1)
    assert (moved.value, moved.fb, moved.crash_count) == (before.value + steps, before.value, before.crash_count)
    assert after.flow.dirty


@pytest.mark.req("G3.zero_elsewhere")
def test_evidence_on_a_core_at_stock_outside_search_counts_an_incident_and_the_second_pauses():
    fake = fresh(3, [-12, -6, 0], 1, max_offset=-20, k_1t=2)
    for tested, incidents in ((0, 1), (1, 2)):
        worker = _until(fake, lambda spec, state, core=tested: spec.stage is Stage.LIVE1 and spec.core == core)
        assert fake.state.core(2).value == 0
        fake.deliver(Report(Group.PASS, worker.spec.seconds, evidence=(Evidence(2, True),)))
        assert fake.state.incidents == incidents
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert fake.state.pause.reason is PauseReason.INCIDENTS


@pytest.mark.req("L04.G5")
def test_a_thermal_stop_in_the_walk_sends_the_core_back_counts_its_seconds_and_retries_the_same_test():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _, effects = _recording(fake)
    spec = _until(fake, lambda spec, state: state.core(spec.core).phase in (Phase.COARSE, Phase.FINE)).spec
    walked = fake.state.core(spec.core).walk_seconds
    mark = len(effects)
    fake.deliver(Report(Group.THERMAL, 12.5))
    assert fake.state.core(spec.core).walk_seconds == walked + 12.5
    assert WriteCO(spec.core, 0) in effects[mark:]
    again = _next_worker(fake)
    assert (again.core, again.value, again.stage) == (spec.core, spec.value, spec.stage)


def _proving(spec, state) -> bool:
    record = state.core(spec.core)
    return spec.stage is Stage.SEARCH and record.phase is Phase.PROVE and record.count >= 1


@pytest.mark.req("L05.G5_G8")
@pytest.mark.parametrize(
    "report",
    [Report(Group.THERMAL, 5.0), Report(Group.APPARATUS, 5.0, detail="tool died"), Report(Group.ENVIRONMENT, 5.0)],
)
def test_a_test_with_no_verdict_leaves_a_proving_core_s_count_and_retries_it(report):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=3)
    spec = _until(fake, _proving).spec
    count = fake.state.core(spec.core).count
    fake.deliver(report)
    assert fake.state.core(spec.core).count == count
    again = _next_worker(fake)
    assert (again.core, again.value) == (spec.core, spec.value)
    assert fake.state.core(spec.core).count == count


@pytest.mark.req("L05.G5_G8")
def test_a_pause_and_resume_keep_a_proving_core_s_count():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=3)
    spec = _until(fake, _proving).spec
    count = fake.state.core(spec.core).count
    fake.post(PauseRequest())
    fake.deliver()
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert fake.state.core(spec.core).count == count + 1
    fake.post(Resume(1))
    assert fake.state.core(spec.core).count == count + 1
    assert _next_worker(fake).core == spec.core


@pytest.mark.req("L06.first")
def test_the_two_thread_class_walks_its_own_round_from_the_first_position():
    fake = fresh(3, [-7, -4, -2], 1, max_offset=-9, k_1t=2, k_2t=1)
    specs = _run(fake)
    one = [spec.core for spec in specs if spec.stage is Stage.SEARCH and spec.klass is Klass.T1]
    two = [spec.core for spec in specs if spec.stage is Stage.SEARCH and spec.klass is Klass.T2]
    cores, domains = (0, 1, 2), dict(fake.layout.domain_of)
    round_0 = list(round_sequence(fake.config.method, cores, domains, 7, "2t", 0))
    assert two == round_0
    assert one[-1] != round_0[-1]


@pytest.mark.req("L07.G6", "D43")
def test_a_second_unblamed_corrected_record_on_one_live1_slot_is_a_failure_there():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, _live1).spec
    bound = fake.state.core(spec.core).fb
    fake.deliver(Report(Group.ENVIRONMENT, 5.0, unblamed_corrected=True))
    assert (fake.state.core(spec.core).value, fake.state.core(spec.core).fb) == (spec.value, bound)
    again = _next_worker(fake)
    assert (again.core, again.value, again.stage) == (spec.core, spec.value, Stage.LIVE1)
    fake.deliver(Report(Group.ENVIRONMENT, 5.0, unblamed_corrected=True))
    record = fake.state.core(spec.core)
    assert (record.value, record.fb) == (spec.value + 1, spec.value)


@pytest.mark.req("L07.G5_G8")
@pytest.mark.parametrize("report", [Report(Group.THERMAL, 5.0), Report(Group.APPARATUS, 5.0, detail="tool died")])
def test_a_live1_slot_with_no_verdict_is_retried_at_the_same_index(report):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, _live1).spec
    index = fake.state.flow.live1.index
    fake.deliver(report)
    again = _next_worker(fake)
    assert (again.core, again.value, again.stage) == (spec.core, spec.value, Stage.LIVE1)
    assert fake.state.flow.live1.index == index


@pytest.mark.req("L07.G5_G8")
def test_a_pause_between_live1_slots_keeps_the_index_and_resume_tests_the_slot_it_points_at():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, wait_between_cores_s=15.0)
    first = _until(fake, _live1).spec
    fake.deliver()
    while not isinstance(fake.state.slot, Waiting) or fake.state.slot.picked is None:
        assert fake.progress()
    picked, index = fake.state.slot.picked, fake.state.flow.live1.index
    assert picked != first.core and index == 1
    fake.post(PauseRequest())
    fake.post(Resume(1))
    again = _next_worker(fake)
    assert (again.core, again.stage, fake.state.flow.live1.index) == (picked, Stage.LIVE1, index)


@pytest.mark.req("L08.d.intest")
def test_an_all_core_test_marks_every_owned_core_in_test():
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    _until(fake, _steady)
    assert [record.in_test for record in fake.state.cores] == [True, True, True]


@pytest.mark.req("L08.G2.transient")
def test_a_steady_failure_with_a_transient_context_loosens_every_core_one_step_and_runs_steady_again():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _steady)
    values = {record.core: record.value for record in fake.state.cores}
    context = FailContext(steady=False, working=(0, 1), paused=False)
    fake.deliver(Report(Group.SILICON, 30.0, context=context))
    assert {record.core: record.value for record in fake.state.cores} == {c: v + 1 for c, v in values.items()}
    assert _next_worker(fake).stage is Stage.STEADY


@pytest.mark.req("L08.G2.mce")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_machine_check_naming_an_owned_core_in_steady_backs_it_off_in_place_and_runs_steady_again(corrected, steps):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _steady)
    before, other = fake.state.core(1), fake.state.core(0).value
    fake.deliver(Report(Group.SILICON, 30.0, evidence=(Evidence(1, corrected),)))
    record = fake.state.core(1)
    assert (record.value, record.fb) == (before.value + steps, before.value)
    assert fake.state.core(0).value == other
    assert _next_worker(fake).stage is Stage.STEADY


@pytest.mark.req("L08.G2.mce")
def test_a_machine_check_naming_a_core_the_session_does_not_own_in_steady_pauses_naming_it():
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    fake.config = replace(fake.config, selected=(0, 1))
    _until(fake, _steady)
    fake.deliver(Report(Group.SILICON, 30.0, evidence=(Evidence(2, True),)))
    assert (fake.state.pause.reason, fake.state.pause.cores) == (PauseReason.HELD_KNOB, (2,))


@pytest.mark.req("L08.G4", "L18.d.4.4.allcore")
@pytest.mark.parametrize(("steady", "stage"), [(True, Stage.BISECT), (False, Stage.STEADY)])
def test_a_crash_in_steady_with_nobody_convicted_is_a_failure_classed_by_its_telemetry(steady, stage):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _steady)
    values = {record.core: record.value for record in fake.state.cores}
    context = FailContext(steady=steady, working=(0, 1), paused=False)
    apply = fake.apply

    def with_context(state, effect):
        read = apply(state, effect)
        return replace(read, context=context) if isinstance(read, CrashEvidenceRead) else read

    fake.apply = with_context
    fake.crash()
    fake.post(Resume(1))
    assert fake.state.lifecycle is Lifecycle.RUNNING
    assert fake.state.flow.stage is stage
    loosened = {record.core: record.value for record in fake.state.cores}
    assert loosened == (values if steady else {c: v + 1 for c, v in values.items()})


def _bisecting(fake: Fake):
    _until(fake, _steady)
    fake.deliver(Report(Group.SILICON, 30.0, context=FailContext(steady=True, working=(0, 1), paused=False)))
    assert fake.state.flow.stage is Stage.BISECT
    return _next_worker(fake)


@pytest.mark.req("L09.G2.baseline")
def test_a_failing_baseline_slot_in_co_only_pauses_as_stock_unstable():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    slot = _bisecting(fake)
    assert dict(slot.vector) == {0: 0, 1: 0}
    fake.deliver(Report(Group.SILICON, 30.0))
    assert fake.state.pause.reason is PauseReason.STOCK_UNSTABLE


@pytest.mark.req("L09.G5_G9")
def test_a_thermal_stop_in_a_bisect_slot_retries_the_same_slot_and_keeps_the_queue():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _bisecting(fake)
    fake.deliver()
    slot = _next_worker(fake)
    queue = fake.state.flow.bisect.queue
    assert slot.stage is Stage.BISECT and queue
    fake.deliver(Report(Group.THERMAL, 5.0))
    again = _next_worker(fake)
    assert (again.stage, again.vector) == (Stage.BISECT, slot.vector)
    assert fake.state.flow.bisect.queue == queue


@pytest.mark.req("L09.G5_G9")
@pytest.mark.parametrize("event", [PauseRequest(), Abort()])
def test_a_pause_or_an_abort_in_a_bisect_stores_its_queue(event):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _bisecting(fake)
    fake.deliver()
    _next_worker(fake)
    run = fake.state.flow.bisect
    assert run.queue
    fake.post(event)
    assert fake.db.flow.bisect == run


@pytest.mark.req("L10.h")
def test_a_bisect_fix_is_committed_before_any_core_is_written_again():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _bisecting(fake)
    fake.deliver()
    slot = _next_worker(fake)
    convicted = [core for core, value in slot.vector if value != 0]
    assert len(convicted) == 1
    _, effects = _recording(fake)
    fake.deliver(Report(Group.SILICON, 30.0))
    assert fake.state.flow.stage is Stage.STEADY and fake.state.core(convicted[0]).fb is not None
    kinds = [type(effect) for effect in effects]
    assert WriteCO in kinds and kinds.index(Commit) < kinds.index(WriteCO)


@pytest.mark.req("L11.b")
def test_a_steady_pass_enters_transient_with_its_clean_time_and_index_at_zero():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    worker = _until(fake, _steady)
    flow = fake.state.flow
    state = replace(fake.state, flow=replace(flow, allcore=replace(flow.allcore, t_clean=100.0, t_idx=2)))
    after = transition(state, WorkerReported(worker.token, Report(Group.PASS, 30.0)), fake.world()).state
    assert after.flow.stage is Stage.TRANSIENT
    assert (after.flow.allcore.t_clean, after.flow.allcore.t_idx) == (0.0, 0)


def _transient(spec, state) -> bool:
    return spec.stage is Stage.TRANSIENT and state.flow.allcore.t_clean > 0


@pytest.mark.req("L11.G3")
def test_a_record_that_moves_a_core_in_transient_restarts_its_clean_time_and_one_at_stock_does_not():
    fake = fresh(3, [-12, -6, 0], 1, max_offset=-20, k_1t=2)
    worker = _until(fake, _transient)
    clean, incidents, value = fake.state.flow.allcore.t_clean, fake.state.incidents, fake.state.core(0).value
    assert fake.state.core(2).value == 0 and value < 0

    def reported(core):
        report = Report(Group.PASS, 30.0, evidence=(Evidence(core, True),))
        return transition(fake.state, WorkerReported(worker.token, report), fake.world()).state

    at_stock, moved = reported(2), reported(0)
    assert (at_stock.flow.allcore.t_clean, at_stock.incidents) == (clean, incidents + 1)
    assert (moved.flow.allcore.t_clean, moved.core(0).value) == (0.0, value + 1)


@pytest.mark.req("L11.G4", "L18.d.4.4.allcore")
def test_a_crash_in_transient_with_nobody_convicted_loosens_every_core_one_step():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _transient)
    values = {record.core: record.value for record in fake.state.cores}
    fake.crash()
    fake.post(Resume(1))
    assert (fake.state.lifecycle, fake.state.flow.stage) == (Lifecycle.RUNNING, Stage.TRANSIENT)
    assert {record.core: record.value for record in fake.state.cores} == {c: v + 1 for c, v in values.items()}


@pytest.mark.req("L11.G5_G8")
@pytest.mark.parametrize("report", [Report(Group.THERMAL, 5.0), Report(Group.APPARATUS, 5.0, detail="tool died")])
def test_a_transient_test_with_no_verdict_leaves_its_clean_time(report):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, _transient).spec
    clean = fake.state.flow.allcore.t_clean
    fake.deliver(report)
    again = _next_worker(fake)
    assert (again.stage, again.load, fake.state.flow.allcore.t_clean) == (Stage.TRANSIENT, spec.load, clean)


@pytest.mark.req("L11.G5_G8")
def test_a_pause_in_transient_leaves_its_clean_time_and_the_cut_test_runs_again():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, _transient).spec
    clean = fake.state.flow.allcore.t_clean
    fake.post(PauseRequest())
    assert fake.state.lifecycle is Lifecycle.PAUSED and fake.worker is None
    fake.post(Resume(1))
    again = _next_worker(fake)
    assert (again.stage, again.load, fake.state.flow.allcore.t_clean) == (Stage.TRANSIENT, spec.load, clean)


def _tightening(spec, state) -> bool:
    return spec.stage is Stage.TIGHTEN and state.flow.allcore.t_idx > 0


@pytest.mark.req("L12.G2.mce", "L12.G3")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_in_tighten_backs_its_core_off_in_place_and_the_round_s_load_repeats(corrected, steps):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, _tightening).spec
    index, resident = fake.state.flow.allcore.t_idx, dict(spec.vector)[1]
    fake.deliver(Report(Group.PASS, 30.0, evidence=(Evidence(1, corrected),)))
    record = fake.state.core(1)
    assert (record.value, record.fb) == (min(0, resident + steps), resident)
    again = _next_worker(fake)
    assert (again.stage, again.load, fake.state.flow.allcore.t_idx) == (Stage.TIGHTEN, spec.load, index)


@pytest.mark.req("L12.G4.other", "D23")
def test_a_conviction_in_tighten_freezes_that_core_and_restarts_the_round():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _tightening)
    cursor = fake.state.flow.allcore
    members = allcore.round_members(fake.state.cores, fake.state.config, cursor)
    assert members and cursor.t_idx > 0
    fake.deliver(Report(Group.SILICON, 30.0, named=(members[0],)))
    after = fake.state.flow.allcore
    assert (members[0] in after.frozen, after.t_idx, after.t_clean) == (True, 0, 0.0)
    assert _next_worker(fake).stage is Stage.TIGHTEN


def _enduring(spec, state) -> bool:
    return spec.stage is Stage.ENDURANCE and state.flow.allcore.t_clean > 0


@pytest.mark.req("L14.G3_G7")
def test_a_record_that_moves_a_core_in_endurance_restarts_its_clean_time():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, endurance=True)
    worker = _until(fake, _enduring)
    value = fake.state.core(0).value
    report = Report(Group.PASS, 30.0, evidence=(Evidence(0, True),))
    after = transition(fake.state, WorkerReported(worker.token, report), fake.world()).state
    assert (after.flow.allcore.t_clean, after.core(0).value) == (0.0, value + 1)


@pytest.mark.req("L14.G3_G7")
@pytest.mark.parametrize("report", [Report(Group.THERMAL, 5.0), Report(Group.APPARATUS, 5.0, detail="tool died")])
def test_an_endurance_test_with_no_verdict_leaves_its_clean_time(report):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, endurance=True)
    spec = _until(fake, _enduring).spec
    clean = fake.state.flow.allcore.t_clean
    fake.deliver(report)
    again = _next_worker(fake)
    assert (again.stage, again.load, fake.state.flow.allcore.t_clean) == (Stage.ENDURANCE, spec.load, clean)


@pytest.mark.req("L14.G3_G7", "L18.d.4.4.allcore")
@pytest.mark.parametrize(("steady", "stage"), [(True, Stage.BISECT), (False, Stage.ENDURANCE)])
def test_a_crash_in_endurance_takes_the_flow_rule_its_telemetry_classes(steady, stage):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, endurance=True)
    _until(fake, _enduring)
    values = {record.core: record.value for record in fake.state.cores}
    context = FailContext(steady=steady, working=(0, 1), paused=False)
    apply = fake.apply

    def with_context(state, effect):
        read = apply(state, effect)
        return replace(read, context=context) if isinstance(read, CrashEvidenceRead) else read

    fake.apply = with_context
    fake.crash()
    fake.post(Resume(1))
    assert fake.state.flow.stage is stage
    loosened = {record.core: record.value for record in fake.state.cores}
    assert loosened == (values if steady else {c: v + 1 for c, v in values.items()})


@pytest.mark.req("L15.G3")
def test_a_record_in_memory_backs_its_core_off_marks_dirty_and_memory_runs_again():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, lambda spec, state: spec.stage is Stage.MEMORY).spec
    resident = dict(spec.vector)[1]
    assert not fake.state.flow.dirty
    fake.deliver(Report(Group.PASS, 30.0, evidence=(Evidence(1, True),)))
    record = fake.state.core(1)
    assert (record.value, record.fb, fake.state.flow.dirty) == (resident + 1, resident, True)
    assert _next_worker(fake).stage is Stage.MEMORY


@pytest.mark.req("L17.G10")
def test_a_refused_final_write_pauses_at_baseline_and_resume_only_applies_the_profile():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    start(fake)
    while fake.state.flow is None or fake.state.flow.stage is not Stage.FINALIZE or fake.worker is not None:
        assert fake.progress()
    final = {record.core: record.value for record in fake.state.cores}
    fake.faults.refuse_writes = 1
    fake.fire()
    assert fake.state.pause.reason is PauseReason.PROFILE_APPLY
    assert fake.co == {0: 0, 1: 0}
    _, effects = _recording(fake)
    fake.post(Resume(1))
    run_to_rest(fake, 50)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert not any(isinstance(effect, StartWorker) for effect in effects)
    assert {effect.core: effect.value for effect in effects if isinstance(effect, WriteCO)} == final


def _crashed_with(fake: Fake, context: FailContext | None) -> None:
    apply = fake.apply

    def with_context(state, effect):
        read = apply(state, effect)
        return replace(read, context=context) if isinstance(read, CrashEvidenceRead) else read

    fake.apply = with_context
    fake.crash()
    fake.post(Resume(1))


@pytest.mark.req("L18.d.4.4.allcore")
def test_a_crash_in_a_tighten_round_ends_the_reclaim():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _tightening)
    _crashed_with(fake, FailContext(steady=True, working=(0, 1), paused=False))
    assert fake.state.lifecycle is Lifecycle.RUNNING
    assert fake.state.flow.stage is Stage.MEMORY and fake.state.flow.allcore.tightened


@pytest.mark.req("L18.d.4.4.memory")
def test_a_crash_in_memory_with_nobody_convicted_bisects_with_memory_slots():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, lambda spec, state: spec.stage is Stage.MEMORY)
    _crashed_with(fake, None)
    assert fake.state.flow.stage is Stage.BISECT and fake.state.flow.bisect.entry is Stage.MEMORY
    slot = _next_worker(fake)
    assert (slot.stage, slot.load) == (Stage.BISECT, Load.MEMORY)


@pytest.mark.req("L18.e.G11")
def test_a_failing_commit_pauses_for_the_database_at_stock_and_resume_reloads_the_stored_row():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _live1)
    kept = fake.db
    fake.faults.fail_commits = 1
    fake.deliver()
    assert fake.state.pause.reason is PauseReason.DATABASE
    assert fake.co == {0: 0, 1: 0} and fake.db == kept
    tested = next(record for record in kept.cores if record.in_test)
    untested = next(record for record in kept.cores if not record.in_test)
    fake.post(Resume(1))
    assert fake.state.lifecycle is Lifecycle.RUNNING
    assert fake.state.core(untested.core) == untested
    assert (fake.state.core(tested.core).value, fake.state.core(tested.core).fb) == (tested.value + 1, tested.value)


@pytest.mark.req("L19.c")
def test_a_pause_is_stored_before_it_is_announced_with_no_core_left_in_test():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    worker = _until(fake, _steady)
    effects = transition(fake.state, PauseRequest(), fake.world()).effects
    kinds = [type(effect) for effect in effects]
    commit = effects[kinds.index(Commit)]
    assert kinds.index(Commit) < kinds.index(Announce)
    assert worker.spec.load in ALLCORE_LOADS
    assert not any(record.in_test for record in commit.stored.cores)


@pytest.mark.req("L19.G5", "L19.G6_G7")
@pytest.mark.parametrize(
    ("report", "streak", "reason"),
    [
        (Report(Group.THERMAL, 5.0), "thermal", PauseReason.THERMAL),
        (Report(Group.APPARATUS, 5.0, detail="tool died"), "apparatus", PauseReason.APPARATUS),
    ],
)
def test_no_verdict_while_pausing_pauses_for_the_user_with_the_retry_owed_or_gives_up_at_the_fourth(
    report, streak, reason
):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    worker = _until(fake, lambda spec, state: spec.stage is Stage.SEARCH)
    pausing = transition(fake.state, PauseRequest(), fake.world()).state
    assert pausing.lifecycle is Lifecycle.PAUSING
    reported = WorkerReported(worker.token, report)
    once = transition(pausing, reported, fake.world()).state
    assert (once.pause.reason, once.flow.owed) == (PauseReason.USER, worker.spec.core)
    third = replace(pausing, streaks=replace(pausing.streaks, **{streak: 3}))
    assert transition(third, reported, fake.world()).state.pause.reason is reason


@pytest.mark.req("L19.h")
def test_a_reboot_while_paused_counts_no_incident_and_no_resume_streak():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _live1)
    fake.deliver()
    fake.post(PauseRequest())
    assert fake.state.lifecycle is Lifecycle.PAUSED
    incidents, streak = fake.state.incidents, fake.state.resume_streak
    fake.crash()
    fake.post(Resume(1))
    assert fake.state.lifecycle is Lifecycle.RUNNING
    assert (fake.state.incidents, fake.state.resume_streak) == (incidents, streak)


@pytest.mark.req("L20.d.percore")
@pytest.mark.parametrize("phase", [Phase.COARSE, Phase.FINE])
def test_a_thermal_stop_adds_its_seconds_to_the_walk_in_either_walk_phase(phase):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, lambda spec, state: spec.stage is Stage.SEARCH and state.core(spec.core).phase is phase).spec
    walked = fake.state.core(spec.core).walk_seconds
    fake.deliver(Report(Group.THERMAL, 7.5))
    assert fake.state.core(spec.core).walk_seconds == walked + 7.5


@pytest.mark.req("L20.d.percore")
def test_a_thermal_stop_in_live1_keeps_the_slot_s_vector_and_the_walk():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    spec = _until(fake, _live1).spec
    walked = {record.core: record.walk_seconds for record in fake.state.cores}
    fake.deliver(Report(Group.THERMAL, 7.5))
    assert {record.core: record.walk_seconds for record in fake.state.cores} == walked
    assert _next_worker(fake).vector == spec.vector


@pytest.mark.req("L20.G6_G7")
def test_an_apparatus_failure_after_a_thermal_stop_keeps_the_thermal_streak():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _live1)
    fake.deliver(Report(Group.THERMAL, 5.0))
    _next_worker(fake)
    fake.deliver(Report(Group.APPARATUS, 5.0, detail="tool died"))
    assert (fake.state.streaks.thermal, fake.state.streaks.apparatus) == (1, 1)


@pytest.mark.req("L20.h")
def test_a_reboot_while_cooling_counts_one_incident_and_convicts_nobody():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    _until(fake, _live1)
    fake.deliver(Report(Group.THERMAL, 5.0))
    assert isinstance(fake.state.slot, Waiting) and fake.state.slot.cool_below_c is not None
    records, incidents = fake.state.cores, fake.state.incidents
    fake.crash()
    fake.post(Resume(1))
    assert fake.state.incidents == incidents + 1
    assert [(r.value, r.fb, r.crash_count) for r in fake.state.cores] == [
        (r.value, r.fb, r.crash_count) for r in records
    ]


@pytest.mark.req("L21.c")
def test_the_first_value_failure_count_starts_fresh_at_resume():
    fake = fresh(2, [-3, -2], 1, max_offset=-20, k_1t=2, start_failure_threshold=3)
    _until(fake, lambda spec, state: spec.stage is Stage.SEARCH)
    fake.deliver()
    assert fake.state.streaks.start_failures == 1
    fake.post(PauseRequest())
    fake.state = replace(fake.state, streaks=replace(fake.state.streaks, start_failures=2))
    fake.post(Resume(1))
    assert fake.state.lifecycle is Lifecycle.RUNNING and fake.state.streaks.start_failures == 0


@pytest.mark.req("L21.d")
def test_only_a_pass_at_a_first_walk_value_resets_the_first_value_failure_count():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    worker = _until(fake, lambda spec, state: state.core(spec.core).walk_tests == 0)
    counted = replace(fake.state, streaks=replace(fake.state.streaks, start_failures=2))
    passed = WorkerReported(worker.token, Report(Group.PASS, 60.0))
    assert transition(counted, passed, fake.world()).state.streaks.start_failures == 0
    worker = _until(fake, _proving)
    counted = replace(fake.state, streaks=replace(fake.state.streaks, start_failures=2))
    passed = WorkerReported(worker.token, Report(Group.PASS, 60.0))
    assert transition(counted, passed, fake.world()).state.streaks.start_failures == 2
