from __future__ import annotations

from dataclasses import replace

import pytest

from corecycler.tuner.machine import search
from corecycler.tuner.machine.session import stored, transition
from corecycler.tuner.machine.types import (
    ContinuationDue,
    CoreRecord,
    CrashEvidenceRead,
    DatabaseFailed,
    Evidence,
    Flow,
    InFlight,
    InternalError,
    Klass,
    Lifecycle,
    MachineConfig,
    Mode,
    Pause,
    PauseReason,
    Phase,
    Report,
    SessionState,
    SmuRefused,
    Stage,
    Start,
    Waiting,
    WorkerReported,
    World,
)
from corecycler.tuner.machine.types import (
    TestSpec as Spec,
)
from corecycler.tuner.orders import Method
from tests._tuner_world import (
    Abort,
    Group,
    PauseRequest,
    Resume,
    fresh,
    run_to_rest,
    start,
)

CFG = MachineConfig(Mode.CO, Method.SEQUENTIAL, (0, 1), max_offset=-20, k_1t=2)
LAYOUT = World(cpus_of=((0, (0, 1)), (1, (2, 3))), domain_of=((0, 0), (1, 0)), bios_co=((0, 0), (1, 0)))


def refusal(state: SessionState, event, world: World = LAYOUT) -> str:
    result = transition(state, event, world)
    assert result.state == state
    assert result.effects == ()
    assert result.refused is not None
    return result.refused


def dropped(state: SessionState, event, world: World = LAYOUT) -> None:
    result = transition(state, event, world)
    assert result.state == state
    assert result.refused is None
    assert all(type(effect).__name__ == "Announce" for effect in result.effects)


def running_fake():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    start(fake)
    return fake


@pytest.mark.parametrize(
    ("config", "world", "acknowledged", "says"),
    [
        (CFG, replace(LAYOUT, preflight_refusal="the kernel log cannot be read"), True, "kernel log"),
        (replace(CFG, mode=Mode.SHAPER), LAYOUT, True, "not built yet"),
        (replace(CFG, method=Method.ALTERNATE), LAYOUT, True, "one"),
        (replace(CFG, selected=(0, 1, 5)), LAYOUT, True, "core map"),
        (CFG, replace(LAYOUT, bios_co=((0, 0), (1, None))), True, "could not be read"),
        (CFG, replace(LAYOUT, bios_co=((0, -10), (1, -10))), False, "acknowledge"),
    ],
)
def test_start_is_refused_naming_the_first_gate_that_fails(config, world, acknowledged, says):
    assert says in refusal(SessionState(), Start(config, 1, 0, acknowledged), world)


def test_start_with_a_bios_co_runs_once_acknowledged():
    world = replace(LAYOUT, bios_co=((0, -10), (1, -10)))
    result = transition(SessionState(), Start(CFG, 1, 0, acknowledged=True), world)
    assert result.state.lifecycle is Lifecycle.RUNNING


def test_start_is_refused_while_a_session_is_held():
    fake = running_fake()
    assert "held" in refusal(fake.state, Start(fake.config, 2, 0, True))


def test_resume_is_refused_while_running_and_for_another_session_while_paused():
    fake = running_fake()
    assert "running" in refusal(fake.state, Resume(1))
    fake.post(PauseRequest())
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert "abort it" in refusal(fake.state, Resume(9))
    resuming = replace(fake.state, pause=Pause(PauseReason.RESUMING, "reading"))
    assert "already" in refusal(resuming, Resume(1))


@pytest.mark.parametrize(
    ("status", "says"),
    [("completed", "Validate Profile"), ("quarantined", "cannot be resumed"), ("superseded", "cannot be resumed")],
)
def test_resume_is_refused_for_a_stored_session_that_cannot_continue(status, says):
    fake = running_fake()
    row = replace(stored(fake.state), status=status)
    assert says in refusal(SessionState(), Resume(1), replace(LAYOUT, stored=row))


def test_resume_is_refused_for_a_session_that_is_not_stored_or_fails_a_gate_or_its_order():
    fake = running_fake()
    row = stored(fake.state)
    assert "not stored" in refusal(SessionState(), Resume(1))
    assert "not stored" in refusal(SessionState(), Resume(2), replace(LAYOUT, stored=row))
    assert "boot" in refusal(SessionState(), Resume(1), replace(LAYOUT, stored=row, preflight_refusal="no boot id"))
    alternate = replace(row, config=replace(row.config, method=Method.ALTERNATE))
    assert "abort session 1" in refusal(SessionState(), Resume(1), replace(LAYOUT, stored=alternate))


def test_pause_and_abort_are_refused_when_there_is_nothing_to_act_on():
    assert "nothing is running" in refusal(SessionState(), PauseRequest())
    assert "nothing to abort" in refusal(SessionState(), Abort())
    fake = running_fake()
    fake.post(PauseRequest())
    assert "already paused" in refusal(fake.state, PauseRequest())


def test_late_or_stale_machine_events_are_dropped_without_effects():
    fake = running_fake()
    dropped(fake.state, ContinuationDue(999))
    dropped(SessionState(), ContinuationDue(1))
    dropped(SessionState(), WorkerReported(1, Report(Group.PASS, 1.0)))
    fake.fire()
    dropped(fake.state, WorkerReported(999, Report(Group.PASS, 1.0)))
    dropped(fake.state, CrashEvidenceRead(dirty_end=True, same_boot=False, was_running=True))
    dropped(SessionState(), SmuRefused(0, -5, "late"))
    fake.post(PauseRequest())
    fake.deliver()
    dropped(fake.state, SmuRefused(0, -5, "late"))


def test_an_unknown_event_is_a_programming_error():
    with pytest.raises(TypeError):
        transition(SessionState(), object(), LAYOUT)


def test_a_database_failure_while_already_failed_or_idle_changes_nothing():
    fake = running_fake()
    fake.faults.fail_commits = 1
    fake.fire()
    assert fake.state.pause.reason is PauseReason.DATABASE
    dropped(fake.state, DatabaseFailed("again"))
    result = transition(SessionState(), DatabaseFailed("disk full"), LAYOUT)
    assert result.state == SessionState() and "disk full" in result.effects[0].text
    after = transition(fake.state, InternalError("bug while failed"), LAYOUT)
    assert after.state == fake.state and after.effects[0].kind == "internal_error"


def test_an_internal_error_pauses_a_held_session_and_is_only_announced_when_idle():
    fake = running_fake()
    fake.post(InternalError("bug"))
    assert fake.state.pause.reason is PauseReason.INTERNAL
    assert fake.co == {0: 0, 1: 0}
    result = transition(SessionState(), InternalError("bug"), LAYOUT)
    assert result.state == SessionState() and result.effects[0].kind == "internal_error"


def test_only_a_session_can_be_stored():
    with pytest.raises(ValueError):
        stored(SessionState())


def test_a_pause_during_the_wait_between_cores_keeps_the_picked_core_owed():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, wait_between_cores_s=15.0)
    start(fake)
    fake.fire()
    fake.deliver()
    fake.fire()
    assert isinstance(fake.state.slot, Waiting) and fake.state.slot.picked == 1
    fake.post(PauseRequest())
    assert fake.state.flow.owed == 1
    fake.post(Resume(1))
    fake.fire()
    assert fake.worker.spec.core == 1


def test_a_machine_check_on_a_core_the_session_does_not_own_pauses_naming_it():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    fake.config = replace(fake.config, selected=(0,))
    start(fake)
    fake.fire()
    fake.deliver(Report(Group.PASS, 60.0, evidence=(Evidence(1, False),)))
    assert fake.state.pause.reason is PauseReason.HELD_KNOB
    assert fake.state.pause.cores == (1,)


def test_a_machine_check_found_at_resume_pauses_as_stock_unstable():
    fake = running_fake()
    fake.post(PauseRequest())
    resuming = replace(fake.state, pause=Pause(PauseReason.RESUMING, "reading"))
    result = transition(resuming, CrashEvidenceRead(False, True, False, evidence=(Evidence(1, True),)), LAYOUT)
    assert result.state.pause.reason is PauseReason.STOCK_UNSTABLE


def test_a_frequency_cap_at_test_start_pauses_before_any_test():
    fake = running_fake()
    fake.capped = True
    fake.fire()
    assert fake.state.pause.reason is PauseReason.APPARATUS
    assert fake.worker is None


def test_a_test_value_at_or_past_its_fail_bound_pauses_as_internal_instead_of_running():
    fake = running_fake()
    bad = replace(fake.state.core(0), phase=Phase.PROVE, value=-8, fb=-8)
    fake.state = fake.state.with_core(bad)
    fake.fire()
    assert fake.state.pause.reason is PauseReason.INTERNAL
    assert fake.worker is None


def test_a_sleep_between_tests_rewrites_every_owned_core():
    fake = running_fake()
    fake.gap_grew = True
    fake.co = {0: -3, 1: -3}
    fake.fire()
    assert fake.co[1] == 0
    assert fake.co[0] == fake.worker.spec.value


def test_consecutive_first_value_fails_pause_at_the_threshold():
    fake = fresh(2, [0, 0], 1, max_offset=-20, k_1t=2, start_failure_threshold=2)
    start(fake)
    while fake.state.lifecycle is Lifecycle.RUNNING:
        fake.progress()
    assert fake.state.pause.reason is PauseReason.START_FAILURES


def test_the_fourth_thermal_stop_in_a_row_gives_up():
    fake = running_fake()
    fake.fire()
    for _ in range(4):
        fake.deliver(Report(Group.THERMAL, 5.0))
        if fake.state.lifecycle is not Lifecycle.RUNNING:
            break
        fake.fire()
    assert fake.state.pause.reason is PauseReason.THERMAL


def test_a_failed_hint_at_minus_one_settles_at_stock():
    cfg = MachineConfig(Mode.CO, Method.SEQUENTIAL, (0,))
    rec = search.resolve_start(CoreRecord(0, start=-1), cfg)
    rec = search.on_fail(rec, cfg, Klass.T1, 60.0).record
    assert (rec.phase, rec.value) == (Phase.PROVEN, 0)


def test_the_walk_budget_ends_the_walk_at_the_best_pass():
    cfg = MachineConfig(Mode.CO, Method.SEQUENTIAL, (0,), walk_budget_s=100.0)
    rec = search.resolve_start(CoreRecord(0), cfg)
    rec = search.on_pass(rec, cfg, Klass.T1, 60.0).record
    rec = search.on_pass(rec, cfg, Klass.T1, 60.0).record
    assert (rec.phase, rec.value) == (Phase.PROVE, -10)


def test_a_core_with_no_test_cannot_pass_or_fail():
    for verdict in (search.on_pass, search.on_fail):
        with pytest.raises(ValueError):
            verdict(CoreRecord(0), CFG, Klass.T1, 1.0)


SPEC = Spec(Klass.T1, 0, -5, (0,), 1.0)
GOOD = SessionState(
    lifecycle=Lifecycle.RUNNING,
    session_id=1,
    config=CFG,
    baseline=((0, 0), (1, 0)),
    cores=(CoreRecord(0), CoreRecord(1)),
    flow=Flow(Stage.SEARCH),
    slot=Waiting(1, 0.0),
)


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"lifecycle": Lifecycle.IDLE}, "IDLE"),
        ({"slot": None}, "RUNNING"),
        ({"lifecycle": Lifecycle.PAUSING}, "PAUSING"),
        ({"lifecycle": Lifecycle.PAUSED}, "PAUSED"),
        ({"lifecycle": Lifecycle.COMPLETED}, "terminal"),
        ({"config": None}, "no config"),
        ({"cores": (CoreRecord(1), CoreRecord(0))}, "selected cores"),
    ],
)
def test_an_illegal_session_state_cannot_be_built(changes, says):
    with pytest.raises(ValueError, match=says):
        replace(GOOD, **changes)


def test_the_legal_pausing_and_paused_shapes_can_be_built():
    pause = Pause(PauseReason.USER, "user")
    assert replace(GOOD, lifecycle=Lifecycle.PAUSING, slot=InFlight(2, SPEC), pause=pause).lifecycle
    assert replace(GOOD, lifecycle=Lifecycle.PAUSED, slot=None, pause=pause).pause == pause


def test_a_core_the_session_does_not_own_has_no_record():
    with pytest.raises(KeyError):
        GOOD.core(7)


def test_a_full_run_after_every_edge_still_completes():
    fake = running_fake()
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
