from __future__ import annotations

from dataclasses import replace

import pytest

from corecycler.tuner.machine import bisect, search
from corecycler.tuner.machine.resume import attribute, resident
from corecycler.tuner.machine.session import QUARANTINE_STREAK, transition
from corecycler.tuner.machine.types import (
    Announce,
    BisectRun,
    CoreRecord,
    CrashEvidenceRead,
    Evidence,
    Flow,
    Klass,
    Lifecycle,
    Live1,
    MachineConfig,
    Mode,
    Pause,
    PauseReason,
    Phase,
    SessionState,
    Stage,
    Start,
    World,
)
from corecycler.tuner.orders import Method
from tests._tuner_world import Group, Report, Resume, fresh, run_to_rest, start

CFG = MachineConfig(Mode.CO, Method.SEQUENTIAL, (0, 1, 2), max_offset=-20, k_1t=2)
WORLD = World(
    cpus_of=((0, (0, 1)), (1, (2, 3)), (2, (4, 5))),
    domain_of=((0, 0), (1, 0), (2, 1)),
    bios_co=((0, 0), (1, 0), (2, 0)),
    co_range=(-50, 10),
)


def proven(core: int, value: int) -> CoreRecord:
    return CoreRecord(core, phase=Phase.PROVEN, value=value)


def held(flow: Flow, cores: tuple[CoreRecord, ...], **changes) -> SessionState:
    return SessionState(
        lifecycle=Lifecycle.PAUSED,
        session_id=1,
        config=CFG,
        baseline=((0, 0), (1, 0), (2, 0)),
        cores=cores,
        flow=flow,
        pause=Pause(PauseReason.RESUMING, "reading"),
        **changes,
    )


def evidence_read(**fields) -> CrashEvidenceRead:
    reads = tuple((core, 0) for core, _ in WORLD.cpus_of)
    return CrashEvidenceRead(**{"dirty_end": True, "same_boot": False, "was_running": True, "co_now": reads, **fields})


def test_the_2t_class_proves_each_value_again_and_backs_off_to_the_2t_edge():
    fake = fresh(2, [-8, -6], 2, max_offset=-12, k_1t=2, k_2t=2, wait_between_cores_s=0.0)
    fake.edges_2t = {0: -5, 1: -6}
    start(fake)
    run_to_rest(fake, 4000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == {0: -5, 1: -6}


def test_the_2t_class_is_refused_at_start_when_its_binary_cannot_run():
    world = replace(WORLD, two_t_problem="the Kagari binary is missing")
    result = transition(SessionState(), Start(CFG, 1, 0, True), world)
    assert "Kagari" in result.refused


def test_the_2t_class_is_skipped_with_its_reason_when_a_core_has_one_cpu():
    fake = fresh(2, [-3, -2], 1, smt=False, max_offset=-6, k_1t=1, wait_between_cores_s=0.0)
    start(fake)
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert any(line.startswith("skipped: the 2T class is skipped") for line in fake.log)


def solo_world():
    fake = fresh(2, [0, -5], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    fake.stacks = (((0,), (0, 1), 4),)
    start(fake)
    while not (fake.state.flow.live1 is not None and fake.state.flow.live1.solo is not None):
        assert fake.progress()
    return fake


def test_a_live1_fail_at_stock_with_another_core_live_retests_it_alone_then_backs_the_others_off():
    fake = solo_world()
    assert fake.state.flow.live1.solo == 0
    run_to_rest(fake, 4000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == {0: 0, 1: -4}


def test_a_solo_retest_that_fails_at_stock_pauses_as_stock_unstable():
    fake = solo_world()
    while fake.worker is None:
        fake.fire()
    assert dict(fake.worker.spec.vector) == {0: 0, 1: 0}
    fake.deliver(Report(Group.SILICON, 60.0))
    assert fake.state.pause.reason is PauseReason.STOCK_UNSTABLE


def test_q5_turns_a_repeating_unblamed_record_on_one_core_into_a_one_step_back_off():
    fake = fresh(1, [-12], max_offset=-20, k_1t=3, wait_between_cores_s=0.0)
    start(fake)
    fake.fire()
    tested = fake.worker.spec
    fake.deliver(Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True))
    assert fake.state.core(0).fb is None
    fake.fire()
    assert fake.worker.spec.value == tested.value
    fake.deliver(Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True))
    assert fake.state.core(0).fb == tested.value


def test_q5_never_blames_when_another_core_logged_an_unblamed_record_too():
    fake = fresh(2, [-12, -12], 1, max_offset=-20, k_1t=3, wait_between_cores_s=0.0)
    start(fake)
    fake.fire()
    fake.deliver(Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True))
    fake.fire()
    fake.deliver()
    fake.fire()
    fake.deliver(Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True))
    fake.fire()
    fake.deliver(Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True))
    assert all(r.fb is None for r in fake.state.cores)


SEARCHING = held(
    Flow(Stage.SEARCH), (replace(proven(0, -5), phase=Phase.COARSE, in_test=True), proven(1, 0), proven(2, 0))
)
LIVE = held(Flow(Stage.LIVE1, live1=Live1()), (replace(proven(0, -5), in_test=True), proven(1, -3), proven(2, 0)))


@pytest.mark.parametrize(
    ("state", "event", "expected"),
    [
        (SEARCHING, evidence_read(same_boot=True, dirty_end=False, stale_db=True), {"stale_fail": (0,)}),
        (SEARCHING, evidence_read(other_session_wrote=True), {}),
        (SEARCHING, evidence_read(dirty_end=False, evidence=(Evidence(1, True),)), {"evidence": (Evidence(1, True),)}),
        (SEARCHING, evidence_read(evidence=(Evidence(7, False),)), {"held": 7}),
        (LIVE, evidence_read(evidence=(Evidence(1, False),)), {"convict": (1,)}),
        (LIVE, evidence_read(shared_uncorrected=True), {"incident": True}),
        (SEARCHING, evidence_read(), {"convict": (0,)}),
        (
            replace(LIVE, cores=(proven(0, -5), proven(1, -3), proven(2, 0))),
            evidence_read(suspects=(1,)),
            {"convict": (1,)},
        ),
        (LIVE, evidence_read(), {"flow_crash": True}),
        (replace(LIVE, cores=(proven(0, -5), proven(1, -3), proven(2, 0))), evidence_read(), {"incident": True}),
        (LIVE, evidence_read(stale_db=True), {"stale_fail": (0,), "incident": True, "at_stock": True}),
    ],
)
def test_crash_attribution_follows_the_spec_order(state, event, expected):
    verdict = attribute(state, event)
    for field, value in expected.items():
        assert getattr(verdict, field) == value
    if not expected:
        assert verdict == type(verdict)()


@pytest.mark.parametrize(
    ("state", "lifecycle"),
    [(SEARCHING, Lifecycle.RUNNING), (replace(SEARCHING, resume_streak=QUARANTINE_STREAK - 1), Lifecycle.QUARANTINED)],
)
def test_a_dead_boot_whose_kernel_log_cannot_be_read_is_attributed_without_it_and_says_so(state, lifecycle):
    result = transition(state, evidence_read(kernel_problem="journal rotated"), WORLD)
    notes = [e.text for e in result.effects if isinstance(e, Announce) and e.kind == "kernel_log_unread"]
    assert result.state.lifecycle is lifecycle
    assert result.state.core(0).crash_count == 1
    assert notes == [
        "the crashed boot's kernel log could not be read, so the crash was attributed without it: journal rotated"
    ]


def test_a_readable_kernel_log_adds_no_note():
    result = transition(SEARCHING, evidence_read(), WORLD)
    assert not [e for e in result.effects if isinstance(e, Announce) and e.kind == "kernel_log_unread"]


def test_a_boot_whose_end_cannot_be_read_is_treated_as_a_crash_and_says_so_first():
    result = transition(SEARCHING, evidence_read(end_unread="journalctl -b exited 1"), WORLD)
    assert result.state.core(0).crash_count == 1
    assert isinstance(result.effects[0], Announce) and result.effects[0].kind == "boot_end_unread"
    assert result.effects[0].text == (
        "the last boot's end could not be read, so it is treated as a crash: journalctl -b exited 1"
    )


def test_a_readable_boot_end_adds_no_note():
    result = transition(SEARCHING, evidence_read(), WORLD)
    assert not [e for e in result.effects if isinstance(e, Announce) and e.kind == "boot_end_unread"]


def test_a_crash_in_a_multi_core_bisect_slot_is_the_group_s_fail_not_a_conviction_of_its_members():
    run = BisectRun(Stage.LIVE1, 0, Klass.T1, (0, 1), ((0, -5), (1, -3)), ((0, 1),))
    cores = (replace(proven(0, -5), in_test=True), replace(proven(1, -3), in_test=True), proven(2, 0))
    state = replace(LIVE, cores=cores, flow=Flow(Stage.BISECT, live1=Live1(), bisect=run))
    verdict = attribute(state, evidence_read(suspects=(0, 1)))
    assert verdict.convict == ()
    assert verdict.flow_crash


def test_a_bisect_single_core_head_in_test_is_convicted():
    run = BisectRun(Stage.LIVE1, 0, Klass.T1, (0, 1), ((0, -5), (1, -3)), ((1,),), (((0, 1), False),))
    state = replace(LIVE, flow=Flow(Stage.BISECT, live1=Live1(), bisect=run))
    assert attribute(state, evidence_read()).convict == (1,)


def test_resident_values_follow_the_stage_and_refuse_a_core_the_session_does_not_own():
    finalize = replace(LIVE, flow=Flow(Stage.FINALIZE, applied=True))
    assert resident(finalize, 1) == -3
    assert resident(replace(finalize, flow=Flow(Stage.FINALIZE)), 1) == 0
    with pytest.raises(KeyError):
        resident(finalize, 9)


def test_a_rerun_of_a_group_with_nothing_left_to_loosen_pauses_as_stock_unstable():
    run = BisectRun(Stage.LIVE1, 0, Klass.T1, (0, 1), ((0, -1), (1, -1)), (), rerun=(1, 2))
    fake = fresh(3, [-1, 0, 0], 2, max_offset=-4, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    fake.state = replace(
        fake.state,
        cores=(proven(0, -1), proven(1, 0), proven(2, 0)),
        flow=Flow(Stage.BISECT, live1=Live1(), bisect=run),
    )
    fake.co = {0: 0, 1: 0, 2: 0}
    fake.resident = {}
    fake.fire()
    fake.deliver(Report(Group.SILICON, 60.0))
    assert fake.state.pause.reason is PauseReason.STOCK_UNSTABLE
    assert "none can loosen" in fake.state.pause.detail


def test_a_crash_at_stock_reports_stock_unstable_and_changes_nothing():
    rec = proven(0, 0)
    step = search.on_crash(rec, CFG)
    assert step.stock_unstable and step.record == rec


@pytest.mark.parametrize("dirty", [True, False])
def test_a_core_in_test_in_a_stage_that_never_tests_is_a_programming_error(dirty):
    state = held(Flow(Stage.FINALIZE), (replace(proven(0, -5), in_test=True), proven(1, 0), proven(2, 0)))
    event = evidence_read(same_boot=not dirty, dirty_end=dirty, stale_db=not dirty)
    with pytest.raises(ValueError):
        transition(state, event, WORLD)


def test_halving_splits_by_ccd_then_by_index_and_never_returns_the_parent_or_an_empty_half():
    domains = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2}
    assert bisect.halves((0, 1, 2, 3, 4), domains) == ((0, 1), (2, 3, 4))
    assert bisect.halves((0, 1, 2), {0: 0, 1: 0, 2: 0}) == ((0,), (1, 2))
    assert bisect.halves((0, 1), {0: None, 1: 1}) == ((0,), (1,))
    assert bisect.halves((4,), domains) == ()


def test_resume_reloads_and_resumes_a_bisect_queue():
    fake = fresh(2, [-3, -3], 2, max_offset=-6, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    while fake.state.flow.stage is not Stage.LIVE1:
        assert fake.progress()
    while fake.worker is None:
        fake.fire()
    fake.crash()
    fake.post(Resume(1))
    assert fake.state.flow.stage is Stage.BISECT
    run_to_rest(fake, 4000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


def test_a_crash_record_naming_a_core_the_session_does_not_own_pauses_naming_it():
    fake = fresh(3, [-3, -3, -3], 1, max_offset=-6, k_1t=1, wait_between_cores_s=0.0)
    fake.config = replace(fake.config, selected=(0, 1))
    start(fake)
    fake.fire()
    fake.crash(evidence=(Evidence(2, False),))
    fake.post(Resume(1))
    assert fake.state.pause.reason is PauseReason.HELD_KNOB
    assert fake.state.pause.cores == (2,)
    assert fake.co[0] == 0 and fake.co[1] == 0


def test_a_live1_back_off_sends_the_profile_through_a_second_live1_pass_before_completing():
    fake = fresh(2, [-3, -3], 2, max_offset=-6, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    fake.stacks = (((0, 1), (0, 1), 5),)
    stages = []
    original = fake.check_start

    def spy(state, spec):
        stages.append((spec.stage, state.flow.live1.index if state.flow.live1 else None, spec.core))
        original(state, spec)

    fake.check_start = spy
    start(fake)
    run_to_rest(fake, 4000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    live1 = [entry for entry in stages if entry[0] is Stage.LIVE1]
    restarts = [i for i, entry in enumerate(live1) if entry[1] == 0 and i > 0 and live1[i - 1][1] != 0]
    assert restarts, "a back-off in LIVE1 must send the profile through LIVE1 again"


@pytest.mark.parametrize("ender", ["close", "abort", "crash"])
def test_a_search_test_cut_short_is_retried_first_after_the_resume(ender):
    from tests._tuner_world import Abort, Close

    fake = fresh(3, [-9, -9, -9], 1, max_offset=-12, k_1t=2, wait_between_cores_s=0.0)
    start(fake)
    fake.fire()
    fake.deliver()
    fake.fire()
    interrupted = fake.worker.spec.core
    if ender == "close":
        fake.post(Close())
    elif ender == "abort":
        fake.post(Abort())
    else:
        fake.crash()
        fake.boot -= 1
    fake.post(Resume(1))
    while fake.worker is None:
        fake.fire()
    assert fake.worker.spec.core == interrupted
