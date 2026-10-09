from __future__ import annotations

from dataclasses import replace

import pytest

from corecycler.tuner.machine.session import transition
from corecycler.tuner.machine.types import (
    Abort,
    ContinueOnSetup,
    Lifecycle,
    PauseReason,
    Stage,
    StartWorker,
    ValidateProfile,
)
from tests._tuner_world import Fake, fresh, run_to_rest, start


def completed(**cfg) -> Fake:
    fake = fresh(2, [-3, -2], 2, max_offset=-5, k_1t=1, k_2t=1, wait_between_cores_s=0.0, endurance=True, **cfg)
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


def test_an_abort_with_nothing_changed_returns_the_session_to_completed_with_its_values_applied():
    fake = completed()
    before = values(fake)
    fake.post(ValidateProfile(1))
    while fake.worker is None:
        fake.fire()
    fake.post(Abort())
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert fake.db.status == "completed"
    assert fake.co == before


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
def test_validation_is_refused_naming_why(change, reason):
    fake = completed()
    result = transition(fake.state, ValidateProfile(1), change(fake, fake.world()))
    assert reason in result.refused
    assert result.state == fake.state


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


def test_validation_is_refused_while_a_session_is_held():
    fake = fresh(2, [-3, -2], 2, max_offset=-5, k_1t=1, wait_between_cores_s=0.0)
    start(fake)
    result = transition(fake.state, ValidateProfile(1), fake.world())
    assert "a session is held" in result.refused
