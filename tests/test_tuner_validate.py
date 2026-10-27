from __future__ import annotations

import json
from dataclasses import replace

import pytest

from corecycler.tuner.live import stored_from_json, stored_to_json
from corecycler.tuner.machine import bisect
from corecycler.tuner.machine.session import transition
from corecycler.tuner.machine.types import (
    Abort,
    Close,
    ContinueOnSetup,
    Evidence,
    Lifecycle,
    PauseReason,
    PauseRequest,
    Resume,
    Stage,
    StartWorker,
    ValidateProfile,
)
from tests._tuner_world import Fake, Group, Report, fresh, run_to_rest, start


def completed(edges: tuple[int, ...] = (-3, -2), owned: tuple[int, ...] | None = None) -> Fake:
    fake = fresh(len(edges), list(edges), 2, max_offset=-5, k_1t=1, k_2t=1, wait_between_cores_s=0.0, endurance=True)
    if owned is not None:
        fake.config = replace(fake.config, selected=owned)
    start(fake)
    run_to_rest(fake, 4000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    return fake


def stages_started(fake: Fake) -> list[Stage]:
    stages: list[Stage] = []
    apply = fake.apply

    def recording(state, effect):
        if isinstance(effect, StartWorker):
            stages.append(effect.spec.stage)
        return apply(state, effect)

    fake.apply = recording
    return stages


def values(fake: Fake) -> dict[int, int]:
    return {r.core: r.value for r in fake.state.cores}


def until_stage(fake: Fake, stage: Stage) -> None:
    while fake.worker is None or fake.worker.spec.stage is not stage:
        assert fake.progress()


def until(stage: Stage):
    return lambda fake: until_stage(fake, stage)


def until_core(core: int):
    def step(fake: Fake) -> None:
        while fake.worker is None or fake.worker.spec.core != core:
            assert fake.progress()

    return step


def until_the_bisect_ends(fake: Fake) -> None:
    while fake.state.flow.bisect is not None:
        assert fake.progress()


def deliver(group: Group = Group.SILICON, **fields):
    return lambda fake: fake.deliver(Report(group, 1.0, **fields))


def crash(*evidence: Evidence):
    return lambda fake: fake.crash(evidence=evidence)


def suspect(core: int):
    def step(fake: Fake) -> None:
        fake.dead_suspects = (core,)

    return step


def pause(fake: Fake) -> None:
    fake.post(PauseRequest())


def close(fake: Fake) -> None:
    fake.post(Close())


def resume(fake: Fake) -> None:
    fake.post(Resume(1))


def fail_the_next_commit(fake: Fake) -> None:
    fake.faults.fail_commits = 1


def lose_the_verdict(fake: Fake) -> None:
    fail_the_next_commit(fake)
    fake.deliver()
    resume(fake)


def fail_until_the_exact_replay_is_due(fake: Fake) -> None:
    fake.deliver(Report(Group.SILICON, 1.0))
    while not bisect.retesting(fake.state.flow.bisect):
        assert fake.progress()


STOCK_RECORD = deliver(evidence=(Evidence(2, True),))


@pytest.mark.req("INV.L30", "L00.b", "L30.a", "L30.d.co")
def test_a_clean_validation_proves_the_values_again_through_its_own_stages_and_ends_completed():
    fake = completed()
    before = values(fake)
    stages = stages_started(fake)
    fake.post(ValidateProfile(1))
    run_to_rest(fake, 4000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake) == before
    assert set(stages) == {Stage.LIVE1, Stage.ENDURANCE, Stage.MEMORY, Stage.SOAK}
    assert fake.db.status == "completed"
    assert fake.co == before


def test_a_value_that_fails_validation_is_backed_off_and_the_session_completes_with_it():
    fake = completed()
    before = values(fake)
    weak = min(before, key=lambda core: before[core])
    fake.edges[weak] = before[weak] + 1
    stages = stages_started(fake)
    fake.post(ValidateProfile(1))
    run_to_rest(fake, 4000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake)[weak] > before[weak]
    assert fake.state.core(weak).fb == before[weak]
    assert stages.count(Stage.LIVE1) > 1
    assert set(stages) <= {Stage.LIVE1, Stage.ENDURANCE, Stage.MEMORY, Stage.SOAK}


@pytest.mark.parametrize(
    ("edges", "owned", "stage", "steps"),
    [
        pytest.param((-3, -2), None, Stage.LIVE1, (), id="running"),
        pytest.param((-3, -2), None, Stage.LIVE1, (pause, Fake.deliver), id="paused by the user"),
        pytest.param((-3, -2), None, Stage.LIVE1, (pause, Fake.deliver, resume), id="paused and resumed"),
        pytest.param((-3, -2), None, Stage.ENDURANCE, (deliver(), until_the_bisect_ends), id="its exact replay passed"),
        pytest.param(
            (-3, -2, 0),
            None,
            Stage.ENDURANCE,
            (STOCK_RECORD, until(Stage.ENDURANCE), Fake.deliver),
            id="the test with a record at stock passed again",
        ),
        pytest.param((-3, -2), None, Stage.SOAK, (deliver(), until(Stage.SOAK), Fake.deliver), id="SOAK passed again"),
        pytest.param(
            (-3, -2),
            None,
            Stage.ENDURANCE,
            (deliver(Group.ENVIRONMENT, unblamed_corrected=True),),
            id="an unblamed record outside SOAK",
        ),
        pytest.param(
            (-3, 0, -2),
            (0, 2),
            Stage.LIVE1,
            (deliver(Group.PASS, evidence=(Evidence(1, True),)),),
            id="a passing test logged a record at a core not owned",
        ),
    ],
)
@pytest.mark.req("L30.e", "L30.e.failed")
def test_an_abort_with_no_failure_left_unresolved_returns_the_session_to_completed_with_its_values_applied(
    edges, owned, stage, steps
):
    fake = completed(edges, owned)
    before = values(fake)
    fake.post(ValidateProfile(1))
    until_stage(fake, stage)
    for step in steps:
        step(fake)
    assert values(fake) == before
    fake.post(Abort())
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert fake.db.status == "completed"
    assert {core: fake.co[core] for core in before} == before


@pytest.mark.parametrize(
    ("edges", "owned", "stage", "steps"),
    [
        pytest.param((-3, -2), None, Stage.ENDURANCE, (lose_the_verdict,), id="a lost verdict's bisect"),
        pytest.param(
            (-3, -2), None, Stage.ENDURANCE, (fail_until_the_exact_replay_is_due,), id="an unnamed failure's replay"
        ),
        pytest.param((-3, -2, 0), None, Stage.MEMORY, (deliver(named=(2,)),), id="a memory lane at stock's solo test"),
        pytest.param(
            (-3, -2, 0), None, Stage.LIVE1, (until_core(2), lose_the_verdict), id="a lost verdict's solo test"
        ),
        pytest.param((-3, 0, -2), (0, 2), Stage.ENDURANCE, (deliver(named=(1,)),), id="a failure at a core not owned"),
        pytest.param((-3, 0, -2), (0, 2), Stage.ENDURANCE, (deliver(named=(1,)), resume), id="that failure, resumed"),
        pytest.param(
            (-3, 0, -2),
            (0, 2),
            Stage.ENDURANCE,
            (deliver(named=(1,)), close, resume),
            id="that failure, closed and resumed",
        ),
        pytest.param(
            (-3, 0, -2),
            (0, 2),
            Stage.ENDURANCE,
            (fail_the_next_commit, deliver(named=(1,))),
            id="that failure, its pause not stored",
        ),
        pytest.param((-3, -2, 0), None, Stage.ENDURANCE, (STOCK_RECORD,), id="a failure with a record at stock"),
        pytest.param(
            (-3, -2, 0),
            None,
            Stage.ENDURANCE,
            (STOCK_RECORD, until(Stage.ENDURANCE), deliver(Group.APPARATUS)),
            id="its replay gave no verdict",
        ),
        pytest.param(
            (-3, -2, 0),
            None,
            Stage.ENDURANCE,
            (STOCK_RECORD, until(Stage.ENDURANCE), deliver(Group.PASS, evidence=(Evidence(2, True),))),
            id="its replay logged a record again",
        ),
        pytest.param(
            (-3, -2, 0),
            None,
            Stage.ENDURANCE,
            (STOCK_RECORD, until(Stage.ENDURANCE), deliver(Group.PASS, skipped="no binary")),
            id="its replay was skipped",
        ),
        pytest.param((-3, -2), None, Stage.SOAK, (deliver(),), id="a SOAK failure"),
        pytest.param((-3, -2), None, Stage.SOAK, (fail_until_the_exact_replay_is_due,), id="a SOAK failure's replay"),
        pytest.param(
            (-3, -2),
            None,
            Stage.SOAK,
            (deliver(Group.ENVIRONMENT, unblamed_corrected=True),),
            id="an unblamed SOAK failure",
        ),
        pytest.param(
            (-3, -2, 0),
            None,
            Stage.SOAK,
            (deliver(Group.ENVIRONMENT, evidence=(Evidence(2, True),)),),
            id="an unblamed SOAK failure with a record at stock",
        ),
        pytest.param((-3, -2), None, Stage.SOAK, (lose_the_verdict,), id="a lost SOAK verdict"),
        pytest.param((-3, -2), None, Stage.SOAK, (Fake.crash, resume), id="a dead boot in SOAK"),
        pytest.param(
            (-3, -2), None, Stage.ENDURANCE, (Fake.deliver, Fake.crash, resume), id="a crash nobody is blamed for"
        ),
        pytest.param(
            (-3, -2),
            None,
            Stage.ENDURANCE,
            (Fake.deliver, Fake.crash, suspect(0), resume),
            id="a crash convicting a core at stock",
        ),
        pytest.param(
            (-3, 0, -2),
            (0, 2),
            Stage.ENDURANCE,
            (crash(Evidence(1, False)), resume, resume),
            id="a crash at a core not owned",
        ),
    ],
)
@pytest.mark.req("L30.e.failed", "L30.e.incident", "L30.e.stock")
def test_an_abort_while_a_validation_failure_is_unresolved_ends_aborted_and_restores_no_value(
    edges, owned, stage, steps
):
    fake = completed(edges, owned)
    before = values(fake)
    fake.post(ValidateProfile(1))
    until_stage(fake, stage)
    for step in steps:
        step(fake)
    assert values(fake) == before
    fake.post(Abort())
    assert fake.state.lifecycle is Lifecycle.ABORTED
    assert fake.db.status == "aborted"
    assert {core: fake.co[core] for core, _ in fake.state.baseline} == dict(fake.state.baseline)
    said = fake.log[-1]
    assert said.startswith("aborted: ")
    assert "failure" in said
    assert "not restored" in said


@pytest.mark.req("L30.e.failed")
def test_an_abort_while_the_resume_reads_the_boot_keeps_a_failure_at_a_core_not_owned_unresolved():
    fake = completed((-3, 0, -2), (0, 2))
    fake.post(ValidateProfile(1))
    until_stage(fake, Stage.ENDURANCE)
    fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
    resuming = transition(fake.state, Resume(1), fake.world()).state
    assert resuming.pause.reason is PauseReason.RESUMING
    assert transition(resuming, Abort(), fake.world()).state.lifecycle is Lifecycle.ABORTED


@pytest.mark.req("L30.e.stored")
def test_a_failure_owed_its_replay_survives_the_session_file_and_an_older_file_owes_none_yet_its_bisect_aborts():
    fake = completed()
    fake.post(ValidateProfile(1))
    until_stage(fake, Stage.ENDURANCE)
    fail_until_the_exact_replay_is_due(fake)
    close(fake)
    st = fake.db
    assert st.flow.unreplayed == (Stage.ENDURANCE,)
    assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    older = json.loads(json.dumps(stored_to_json(st)))
    del older["flow"]["unreplayed"]
    fake.db = stored_from_json(older, continued=False)
    assert fake.db == replace(st, flow=replace(st.flow, unreplayed=()))
    resume(fake)
    fake.post(Abort())
    assert fake.state.lifecycle is Lifecycle.ABORTED
    older["flow"]["unreplayed"] = ["endurance", "later"]
    with pytest.raises(ValueError, match="unreplayed"):
        stored_from_json(older, continued=False)


@pytest.mark.req("L30.e")
def test_an_abort_after_a_machine_check_in_a_passing_test_backed_a_value_off_ends_the_session_aborted():
    fake = completed()
    before = values(fake)
    fake.post(ValidateProfile(1))
    until_stage(fake, Stage.ENDURANCE)
    deliver(Group.PASS, evidence=(Evidence(0, True),))(fake)
    assert values(fake)[0] > before[0]
    fake.post(Abort())
    assert fake.state.lifecycle is Lifecycle.ABORTED
    assert fake.db.status == "aborted"


def test_an_abort_after_a_back_off_ends_the_session_aborted():
    fake = completed()
    before = values(fake)
    weak = min(before, key=lambda core: before[core])
    fake.edges[weak] = before[weak] + 1
    fake.post(ValidateProfile(1))
    while values(fake)[weak] == before[weak]:
        assert fake.progress()
    fake.post(Abort())
    assert fake.state.lifecycle is Lifecycle.ABORTED
    assert fake.db.status == "aborted"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda fake, world: replace(world, stored=None), "session 1 is not stored"),
        (
            lambda fake, world: replace(world, stored=replace(fake.db, status="paused")),
            "only a completed session can be validated",
        ),
        (lambda fake, world: replace(world, preflight_refusal="the SMU is held by ryzenadj"), "held by ryzenadj"),
        (
            lambda fake, world: replace(world, domain_of=((0, 0), (1, 0))),
            "they sit on one, so use sequential",
        ),
        (
            lambda fake, world: replace(world, stored_crash_suspects=(0, 1)),
            "completed in crashed, while cores 0, 1 held unproven values",
        ),
        (
            lambda fake, world: replace(world, stored_crash_suspects=(0, 1), stored_end_unread="journal rotated"),
            "completed in an end that could not be read (journal rotated), treated as a crash, while cores 0, 1",
        ),
    ],
)
@pytest.mark.req("L26.c.method", "L30.b.order", "TRUTH.BOOT_END_UNREAD")
def test_validation_is_refused_naming_why(change, reason):
    fake = completed()
    result = transition(fake.state, ValidateProfile(1), change(fake, fake.world()))
    assert reason in result.refused
    assert result.state == fake.state


@pytest.mark.req("L24.b", "L30.b.setup")
def test_validation_on_a_changed_setup_pauses_and_can_be_continued_under_it():
    fake = completed()
    moved = replace(fake.world(), setup=(("bios", "B"),))
    result = transition(fake.state, ValidateProfile(1), moved)
    assert result.refused is None
    assert (result.state.lifecycle, result.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.SETUP_CHANGED)
    assert "the setup changed since session 1 started: bios was A, is now B" in result.state.pause.detail
    assert "continue or restart under it" in result.state.pause.detail
    child = transition(result.state, ContinueOnSetup(2), moved).state
    assert (child.session_id, child.lifecycle) == (2, Lifecycle.RUNNING)
    assert child.flow.validating == result.state.flow.validating


@pytest.mark.req("EVENTS.user.validate_held")
def test_validation_is_refused_while_a_session_is_held():
    fake = fresh(2, [-3, -2], 2, max_offset=-5, k_1t=1, wait_between_cores_s=0.0)
    start(fake)
    result = transition(fake.state, ValidateProfile(1), fake.world())
    assert "a session is held" in result.refused
