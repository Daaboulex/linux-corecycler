from __future__ import annotations

from dataclasses import fields, replace

import pytest

from corecycler.tuner.machine import search
from corecycler.tuner.machine.session import ALLCORE_LOADS, config_problem, stored, transition
from corecycler.tuner.machine.types import (
    KNOB_FLOOR,
    ContinuationDue,
    ContinueOnSetup,
    CoreRecord,
    CrashEvidenceRead,
    DatabaseFailed,
    Evidence,
    Flow,
    InFlight,
    InternalError,
    Klass,
    Lifecycle,
    Load,
    MachineConfig,
    MarkSurvived,
    Mode,
    Pause,
    PauseReason,
    Phase,
    ReadCrashEvidence,
    Report,
    RestartOnSetup,
    SessionState,
    SmuRefused,
    Stage,
    Start,
    Supersede,
    ValidateProfile,
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
LAYOUT = World(
    cpus_of=((0, (0, 1)), (1, (2, 3))), domain_of=((0, 0), (1, 0)), bios_co=((0, 0), (1, 0)), co_range=(-50, 10)
)


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


@pytest.mark.req("PRELUDE.4c")
@pytest.mark.parametrize(
    ("report", "marks"),
    [
        (Report(Group.PASS, 1.0), True),
        (Report(Group.SILICON, 1.0), True),
        (Report(Group.THERMAL, 1.0), True),
        (Report(Group.APPARATUS, 1.0, detail="tool died"), False),
        (Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True), False),
        (Report(Group.PASS, 0.0, skipped="no memory to give the lanes"), False),
    ],
)
def test_only_a_test_that_ran_and_saw_nothing_unblamed_marks_its_values_survived(report, marks):
    fake = running_fake()
    while fake.worker is None:
        assert fake.progress()
    result = transition(fake.state, WorkerReported(fake.worker.token, report), fake.world())
    assert any(isinstance(effect, MarkSurvived) for effect in result.effects) is marks


def _reported_after_two_resumes(fake, report, **flow):
    state = replace(fake.state, resume_streak=2, flow=replace(fake.state.flow, **flow))
    result = transition(state, WorkerReported(fake.worker.token, report), fake.world())
    marked = any(isinstance(effect, MarkSurvived) for effect in result.effects)
    return marked, result.state.resume_streak


@pytest.mark.req("PRELUDE.4d")
def test_the_resume_streak_is_reset_by_a_test_that_marks_survival_and_by_no_other():
    fake = running_fake()
    while fake.worker is None:
        assert fake.progress()
    assert _reported_after_two_resumes(fake, Report(Group.PASS, 1.0)) == (True, 0)
    spec = fake.worker.spec
    recurring = Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True)
    seen_once = {"q5": (((spec.core, spec.value), 1),), "q5_others": (spec.core,)}
    assert _reported_after_two_resumes(fake, recurring, **seen_once) == (False, 2)
    while fake.worker is None or fake.worker.spec.load is not Load.HOT:
        assert fake.progress()
    evidence = Report(Group.APPARATUS, 1.0, evidence=(Evidence(0, True),))
    assert _reported_after_two_resumes(fake, evidence) == (False, 2)
    fake.memory_skip = "no memory to give the lanes"
    while fake.worker is None or fake.worker.spec.load is not Load.MEMORY:
        assert fake.progress()
    skipped = Report(Group.PASS, 0.0, skipped=fake.memory_skip)
    assert _reported_after_two_resumes(fake, skipped) == (False, 2)


@pytest.mark.req("L19.d.soak", "U22", "L16.G8")
@pytest.mark.parametrize("load", [Load.HOT, Load.SOAK])
def test_a_pause_during_an_all_core_test_stops_it_at_once_and_resume_runs_the_same_test_again(load):
    fake = running_fake()
    while fake.worker is None or fake.worker.spec.load is not load:
        assert fake.progress()
    cut = fake.worker.spec
    fake.post(PauseRequest())
    assert (fake.state.lifecycle, fake.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.USER)
    assert fake.worker is None
    assert {core: fake.co[core] for core, _ in fake.state.baseline} == dict(fake.state.baseline)
    fake.post(Resume(1))
    while fake.worker is None:
        assert fake.progress()
    assert (fake.worker.spec.stage, fake.worker.spec.load, fake.worker.spec.vector) == (cut.stage, cut.load, cut.vector)


@pytest.mark.req("L16.G4", "L18.d.4.4.soak", "K19")
def test_a_crash_in_soak_with_nobody_to_blame_counts_one_incident_and_the_second_pauses():
    fake = running_fake()
    for crashes in (1, 2):
        while fake.worker is None or fake.worker.spec.load is not Load.SOAK:
            assert fake.progress()
        fake.crash()
        fake.post(Resume(1))
        assert fake.state.incidents == crashes
    assert (fake.state.lifecycle, fake.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.INCIDENTS)


@pytest.mark.req("L18.d.4.5")
def test_a_crash_that_leaves_a_machine_check_on_a_stock_core_counts_one_incident():
    fake = fresh(2, [-12, 0], 1, max_offset=-20, k_1t=2, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.load is not Load.HOT:
        assert fake.progress()
    assert dict(fake.worker.spec.vector)[1] == 0
    fake.crash(evidence=(Evidence(1, False),))
    fake.post(Resume(1))
    assert (fake.state.incidents, fake.state.resume_streak) == (1, 1)


@pytest.mark.req("L07.G3")
@pytest.mark.parametrize(("edges", "backed", "incidents"), [([-12, -6], True, 0), ([-12, 0], False, 1)])
def test_a_record_about_another_core_in_live1_backs_it_off_and_requeues_it_or_counts_an_incident_at_0(
    edges, backed, incidents
):
    fake = fresh(2, edges, 1, max_offset=-20, k_1t=2, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1 or fake.worker.spec.core != 0:
        assert fake.progress()
    value = dict(fake.worker.spec.vector)[1]
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(1, True),)))
    flow = fake.state.flow
    assert (fake.state.core(1).value > value, 1 in flow.live1.requeue, flow.dirty) == (backed, backed, backed)
    assert fake.state.incidents == incidents


@pytest.mark.req("L09.G3")
def test_a_record_about_a_core_at_0_in_a_1t_bisect_slot_counts_an_incident_and_runs_the_slot_again():
    fake = fresh(3, [-12, -6, -8], 1, max_offset=-20, k_1t=2, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1:
        assert fake.progress()
    fake.crash()
    fake.post(Resume(1))
    while fake.worker is None:
        assert fake.progress()
    slot = fake.worker.spec
    assert (slot.stage, slot.load) == (Stage.BISECT, Load.ONE_T)
    at_zero = next(core for core, value in slot.vector if value == 0 and core != slot.core)
    run, incidents = fake.state.flow.bisect, fake.state.incidents
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(at_zero, True),)))
    while fake.worker is None:
        assert fake.progress()
    assert (fake.state.incidents, fake.state.flow.bisect) == (incidents + 1, run)
    assert (fake.worker.spec.stage, fake.worker.spec.vector) == (Stage.BISECT, slot.vector)


@pytest.mark.req("L16.G3")
def test_evidence_in_soak_backs_the_core_off_and_sends_the_profile_through_live1_instead_of_soaking_again():
    fake = running_fake()
    while fake.worker is None or fake.worker.spec.load is not Load.SOAK:
        assert fake.progress()
    core, value = next((core, value) for core, value in fake.worker.spec.vector if value != 0)
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(core, True),)))
    while fake.worker is None:
        assert fake.progress()
    assert fake.worker.spec.stage is Stage.LIVE1
    assert {r.core: r.value for r in fake.state.cores}[core] > value


@pytest.mark.req("G6.soak", "L16.G6", "RV6")
def test_an_unblamed_event_in_soak_counts_an_incident_and_reproves_the_profile_and_the_second_pauses():
    fake = running_fake()
    for incidents in (1, 2):
        while fake.worker is None or fake.worker.spec.load is not Load.SOAK:
            assert fake.progress()
        fake.deliver(Report(Group.ENVIRONMENT, 1.0, detail="soft lockup on CPU 3"))
        assert fake.state.incidents == incidents
        if incidents == 1:
            while fake.worker is None:
                assert fake.progress()
            assert fake.worker.spec.stage is Stage.LIVE1
    assert (fake.state.lifecycle, fake.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.INCIDENTS)
    assert "soft lockup on CPU 3" in fake.state.pause.detail


@pytest.mark.req("T25.start")
def test_a_start_at_minus_1_is_accepted_and_a_core_given_no_start_has_none():
    assert config_problem(replace(CFG, starts=((0, -1),)), (-50, 10)) is None
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, starts=((0, -10),))
    start(fake)
    assert (fake.state.core(0).start, fake.state.core(1).start) == (-10, 0)


@pytest.mark.req("T25.start", "D20")
def test_a_core_given_a_start_tests_it_first_and_a_failed_start_walks_again_from_0_above_it():
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, wait_between_cores_s=0.0, starts=((0, -10), (1, -8)))
    start(fake)
    first: dict[int, int] = {}
    while len(first) < 2:
        while fake.worker is None:
            assert fake.progress()
        first.setdefault(fake.worker.spec.core, fake.worker.spec.value)
        fake.deliver()
    assert first == {0: -10, 1: -8}
    assert fake.state.core(1).fb == -8
    run_to_rest(fake, 4000)
    assert {r.core: r.value for r in fake.state.cores} == {0: -12, 1: -6}


@pytest.mark.req("L18.d.4.3a")
@pytest.mark.parametrize("stage", [Stage.STEADY, Stage.TRANSIENT])
def test_the_lost_verdict_of_an_all_core_test_is_one_fail_of_that_test(stage):
    fake = fresh(2, [-6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0, transient_window_s=2.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not stage:
        assert fake.progress()
    before = {r.core: r.value for r in fake.state.cores}
    fake.faults.fail_commits = 1
    fake.deliver()
    assert fake.state.pause.reason is PauseReason.DATABASE
    fake.crash()
    fake.post(Resume(1))
    assert fake.state.lifecycle is Lifecycle.RUNNING
    assert {r.core: r.value for r in fake.state.cores} == {core: value + 1 for core, value in before.items()}


@pytest.mark.req("C10", "D34", "TERMS.allcore_baseline", "L08.d.after", "L20.d.allcore", "L22.d.allcore")
@pytest.mark.parametrize("report", [Report(Group.THERMAL, 5.0), Report(Group.APPARATUS, 1.0, detail="tool died")])
def test_an_all_core_test_that_ends_without_a_verdict_sends_every_owned_core_back_to_baseline(report):
    fake = running_fake()
    while fake.worker is None or fake.worker.spec.load not in ALLCORE_LOADS:
        assert fake.progress()
    assert any(value != 0 for _, value in fake.worker.spec.vector)
    fake.deliver(report)
    assert fake.state.lifecycle is Lifecycle.RUNNING
    assert {core: fake.co[core] for core, _ in fake.state.baseline} == dict(fake.state.baseline)


@pytest.mark.req("T22.F6")
@pytest.mark.parametrize(
    ("config", "world", "acknowledged", "says"),
    [
        (CFG, replace(LAYOUT, preflight_refusal="the kernel log cannot be read"), True, "kernel log"),
        (replace(CFG, mode=Mode.SHAPER), LAYOUT, True, "not built yet"),
        (replace(CFG, method=Method.ALTERNATE), LAYOUT, True, "one"),
        (replace(CFG, selected=(0, 1, 5)), LAYOUT, True, "core map"),
        (CFG, replace(LAYOUT, bios_co=((0, 0), (1, None))), True, "the CO of cores [1] could not be read at Start"),
        (
            CFG,
            replace(LAYOUT, bios_co=((0, -10), (1, -10))),
            False,
            "cores [0, 1] read a CO other than 0 at Start, set by the BIOS or written since boot: acknowledge to start",
        ),
        (replace(CFG, fine_step=0), LAYOUT, True, "fine_step 0 is below 1"),
        (replace(CFG, k_1t=0, test_seconds_1t=0.5), LAYOUT, True, "k_1t 0 is below 1; test_seconds_1t 0.5 is"),
        (replace(CFG, wait_between_cores_s=-1.0), LAYOUT, True, "wait_between_cores_s -1.0 is below 0.0"),
        (replace(CFG, fine_step=3, coarse_step=2), LAYOUT, True, "fine_step 3 is above coarse_step 2"),
        (CFG, replace(LAYOUT, co_range=None), True, "CO range is unknown"),
        (CFG, replace(LAYOUT, held_session=(4, "paused")), True, "session 4 is paused: resume it or abort it"),
        (replace(CFG, max_offset=-51), LAYOUT, True, "max_offset -51 is outside -50..-1"),
        (replace(CFG, max_offset=0), LAYOUT, True, "max_offset 0 is outside -50..-1"),
        (replace(CFG, max_offset=-2, fine_step=3, coarse_step=3), LAYOUT, True, "outside -50..-3"),
        (replace(CFG, cool_min_s=10.0, cool_cap_s=5.0), LAYOUT, True, "cool_min_s 10.0 is above cool_cap_s 5.0"),
        (replace(CFG, stage_seconds=(("burst", 1.0), ("warm", 1.0))), LAYOUT, True, "burst, idle, warm once each"),
        (replace(CFG, stage_seconds=(("burst", 1.0), ("burst", 1.0), ("idle", 1.0))), LAYOUT, True, "once each"),
        (replace(CFG, stage_seconds=(("burst", 1.0), ("warm", 0.0), ("idle", 1.0))), LAYOUT, True, "each above 0"),
        (replace(CFG, starts=((5, -3),)), LAYOUT, True, "starts must name each selected core at most once"),
        (replace(CFG, starts=((0, -3), (0, -4))), LAYOUT, True, "at most once: [(0, -3), (0, -4)]"),
        (replace(CFG, starts=((0, -21),)), LAYOUT, True, "a start must be within -20..-1: core 0 at -21"),
        (replace(CFG, starts=((1, 0),)), LAYOUT, True, "a start must be within -20..-1: core 1 at 0"),
    ],
)
@pytest.mark.req("C16", "D82", "E6.B0", "L01.1", "L01.11", "L01.15.cli", "T25.start")
def test_start_is_refused_naming_the_first_gate_that_fails(config, world, acknowledged, says):
    assert says in refusal(SessionState(), Start(config, 1, 0, acknowledged), world)


def test_every_numeric_knob_but_the_offset_has_a_floor_and_the_defaults_pass_it():
    numeric = {f.name for f in fields(MachineConfig) if f.type in ("int", "float")}
    assert set(KNOB_FLOOR) == numeric - {"max_offset"}
    assert config_problem(MachineConfig(Mode.CO, Method.SEQUENTIAL, (0,)), (-50, 10)) is None
    assert config_problem(replace(CFG, max_offset=-50, fine_step=1), (-50, 10)) is None


@pytest.mark.req("K4")
def test_a_stored_configuration_outside_its_ranges_is_not_resumed_or_validated():
    fake = running_fake()
    bad = replace(stored(fake.state), status="paused", config=replace(CFG, coarse_step=0))
    world = replace(fake.world(), stored=bad)
    assert refusal(SessionState(), Resume(bad.session_id), world) == (
        f"coarse_step 0 is below 1; abort session {bad.session_id} and start a new one"
    )
    done = replace(bad, status="completed")
    assert "coarse_step 0 is below 1; start a new session" in refusal(
        SessionState(), ValidateProfile(done.session_id), replace(world, stored=done)
    )


@pytest.mark.req("L01.c.ack")
def test_start_with_a_bios_co_runs_once_acknowledged_and_logs_what_was_acknowledged():
    world = replace(LAYOUT, bios_co=((0, -10), (1, -10)))
    result = transition(SessionState(), Start(CFG, 1, 0, acknowledged=True), world)
    assert result.state.lifecycle is Lifecycle.RUNNING
    said = [(effect.kind, effect.text) for effect in result.effects if type(effect).__name__ == "Announce"]
    assert (
        "acknowledged",
        "cores [0, 1] read a CO other than 0 at Start, set by the BIOS or written since boot",
    ) in said
    plain = transition(SessionState(), Start(CFG, 1, 0, acknowledged=True), LAYOUT)
    assert [effect.kind for effect in plain.effects if type(effect).__name__ == "Announce"] == ["started"]


@pytest.mark.req("L01.2")
def test_start_is_refused_while_a_session_is_held():
    fake = running_fake()
    assert "held" in refusal(fake.state, Start(fake.config, 2, 0, True))


@pytest.mark.req("EVENTS.user.resume_running")
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
    [
        ("completed", "Validate Profile"),
        ("quarantined", "resume it with --reengage"),
        ("superseded", "cannot be resumed"),
    ],
)
@pytest.mark.req("EVENTS.user.superseded", "L17.h", "L24.l24b.terminal")
def test_resume_is_refused_for_a_stored_session_that_cannot_continue(status, says):
    fake = running_fake()
    row = replace(stored(fake.state), status=status)
    assert says in refusal(SessionState(), Resume(1), replace(LAYOUT, stored=row))


@pytest.mark.req("L01.a", "L18.d.1", "ORDER.refuse")
def test_resume_is_refused_for_a_session_that_is_not_stored_or_fails_a_gate_or_its_order():
    fake = running_fake()
    row = stored(fake.state)
    assert "not stored" in refusal(SessionState(), Resume(1))
    assert "not stored" in refusal(SessionState(), Resume(2), replace(LAYOUT, stored=row))
    assert "boot" in refusal(SessionState(), Resume(1), replace(LAYOUT, stored=row, preflight_refusal="no boot id"))
    alternate = replace(row, config=replace(row.config, method=Method.ALTERNATE))
    assert "abort session 1" in refusal(SessionState(), Resume(1), replace(LAYOUT, stored=alternate))


@pytest.mark.req("L19.G8")
def test_pause_and_abort_are_refused_when_there_is_nothing_to_act_on():
    assert "nothing is running" in refusal(SessionState(), PauseRequest())
    assert "nothing to abort" in refusal(SessionState(), Abort())
    fake = running_fake()
    fake.post(PauseRequest())
    assert "already paused" in refusal(fake.state, PauseRequest())


@pytest.mark.req("D21", "EVENTS.machine.paused_smu", "EVENTS.machine.tokens", "L23.G1_G7")
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


@pytest.mark.req("L23.G11_G13", "L23.c")
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


@pytest.mark.req("G13", "L01.G13", "VERDICT.38")
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


@pytest.mark.req("K7", "L04.d.pick", "L19.d.waiting")
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


@pytest.mark.req("D2", "G3.held", "L08.G3", "TERMS.owned_co")
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


@pytest.mark.req("CR.1", "L01.9", "L31.2")
def test_a_capped_power_state_is_the_session_s_range_and_tests_run_in_it():
    capped = (("boost", "1"), ("platform profile", "power-saver"), ("scaling maximum MHz", "4300"))
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    fake.power = capped
    start(fake)
    assert fake.db.power == capped
    fake.fire()
    assert fake.state.pause is None
    assert fake.worker is not None


@pytest.mark.req("D69", "L31.2a")
def test_a_power_state_change_between_tests_pauses_naming_it_and_restoring_it_lets_a_resume_continue():
    fake = running_fake()
    assert fake.db.power == fake.power
    recorded = fake.power
    fake.power = (("platform profile", "power-saver"),)
    fake.fire()
    assert fake.state.pause.reason is PauseReason.SETUP_CHANGED
    assert "platform profile was balanced, is now power-saver" in fake.state.pause.detail
    assert fake.worker is None
    fake.post(Resume(1))
    while fake.worker is None and fake.progress():
        pass
    assert fake.state.pause.reason is PauseReason.SETUP_CHANGED
    fake.power = recorded
    fake.post(Resume(1))
    while fake.worker is None:
        assert fake.progress()
    assert fake.state.pause is None


@pytest.mark.req("L31.5")
def test_a_test_at_or_past_a_fail_bound_is_never_started():
    from corecycler.tuner.machine.session import _start_test

    fake = running_fake()
    waiting = fake.state
    fake.fire()
    spec = fake.state.slot.spec
    bounded = waiting.with_core(replace(waiting.core(spec.core), fb=spec.value))
    result = _start_test(bounded, waiting.flow, spec, fake.world())
    assert result.state.pause.reason is PauseReason.INTERNAL
    assert (
        f"core {spec.core} would run at {spec.value}, outside -20..0 or at or past its fail bound {spec.value}"
        in result.state.pause.detail
    )
    assert not any(type(effect).__name__ == "StartWorker" for effect in result.effects)
    deep = replace(spec, value=-21, vector=tuple((core, -21 if core == spec.core else v) for core, v in spec.vector))
    beyond = _start_test(waiting, waiting.flow, deep, fake.world())
    assert beyond.state.pause.reason is PauseReason.INTERNAL
    assert f"core {spec.core} would run at -21, outside -20..0" in beyond.state.pause.detail
    stock = replace(spec, value=0, vector=tuple((core, 0) for core, _ in spec.vector))
    assert _start_test(bounded, waiting.flow, stock, fake.world()).state.pause is None


@pytest.mark.req("L18.d.2")
def test_a_resume_over_a_changed_power_state_pauses_before_any_test_and_keeps_the_turn():
    fake = running_fake()
    fake.power = (("platform profile", "power-saver"),)
    fake.fire()
    paused = fake.state
    fake.post(Resume(1))
    while fake.progress():
        pass
    assert fake.state.pause.reason is PauseReason.SETUP_CHANGED
    assert "the power state changed since the session started" in fake.state.pause.detail
    assert fake.state.flow == paused.flow
    assert fake.worker is None


@pytest.mark.req("INV.L24", "L18.d.8.setup", "L24.a", "L24.l24a.read", "VERDICT.35")
def test_a_setup_changed_while_paused_pauses_the_resume_and_putting_it_back_lets_it_continue():
    fake = running_fake()
    assert fake.db.setup == fake.setup
    recorded = fake.setup
    fake.post(PauseRequest())
    while fake.state.lifecycle is not Lifecycle.PAUSED:
        assert fake.progress()
    fake.setup = (("bios", "B"),)
    fake.post(Resume(1))
    while fake.worker is None and fake.progress():
        pass
    assert fake.state.pause.reason is PauseReason.SETUP_CHANGED
    assert "bios was A, is now B" in fake.state.pause.detail
    assert fake.worker is None
    fake.setup = recorded
    fake.post(Resume(1))
    while fake.worker is None:
        assert fake.progress()
    assert fake.state.pause is None


def paused_on_a_new_setup():
    fake = running_fake()
    fake.post(PauseRequest())
    while fake.state.lifecycle is not Lifecycle.PAUSED:
        assert fake.progress()
    fake.setup = (("bios", "B"),)
    fake.post(Resume(1))
    while fake.state.pause.reason is PauseReason.RESUMING:
        assert fake.progress()
    assert fake.state.pause.reason is PauseReason.SETUP_CHANGED
    return fake


@pytest.mark.req("G12", "L24.d.continue", "L24.l24b.continue")
def test_continue_carries_the_session_into_a_child_under_the_new_setup_and_supersedes_the_parent():
    fake = paused_on_a_new_setup()
    parent = fake.state
    result = transition(parent, ContinueOnSetup(2), fake.world())
    child = result.state
    assert (child.session_id, child.lifecycle, child.setup) == (2, Lifecycle.RUNNING, (("bios", "B"),))
    assert (child.cores, child.flow.stage, child.baseline) == (parent.cores, parent.flow.stage, parent.baseline)
    [supersede] = [effect for effect in result.effects if isinstance(effect, Supersede)]
    assert (supersede.parent.session_id, supersede.parent.status, supersede.origin) == (1, "superseded", "continue")
    assert supersede.child.session_id == 2
    assert result.effects[0] is supersede


def paused_on_a_new_setup_in(stage, **world):
    fake = fresh(2, [-6, -3], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0, transient_window_s=600.0)
    for name, value in world.items():
        setattr(fake, name, value)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not stage:
        assert fake.progress()
    fake.post(PauseRequest())
    while fake.state.lifecycle is not Lifecycle.PAUSED:
        assert fake.progress()
    fake.setup = (("bios", "B"),)
    fake.post(Resume(1))
    while fake.state.pause.reason is PauseReason.RESUMING:
        assert fake.progress()
    assert fake.state.pause.reason is PauseReason.SETUP_CHANGED
    return fake


@pytest.mark.req("ALLCORE.continue")
def test_a_child_continued_from_the_all_core_stages_re_enters_steady_with_the_margin_over_its_floors():
    fake = paused_on_a_new_setup_in(Stage.TRANSIENT)
    floors = dict(fake.state.flow.allcore.floors)
    parent = fake.state.with_core(replace(fake.state.core(0), value=floors[0] + 1))
    parent = parent.with_core(replace(parent.core(1), value=0))
    moved = replace(parent.flow.allcore, frozen=(1,), rounds=2, t_idx=1, t_clean=30.0)
    parent = replace(parent, flow=replace(parent.flow, allcore=moved))
    child = transition(parent, ContinueOnSetup(2), fake.world()).state
    assert child.flow.stage is Stage.STEADY
    assert {r.core: r.value for r in child.cores} == {0: floors[0] + 3, 1: 0}
    cursor = child.flow.allcore
    assert (cursor.floors, cursor.frozen, cursor.rounds, cursor.tightened, cursor.t_idx, cursor.t_clean) == (
        parent.flow.allcore.floors,
        (1,),
        0,
        False,
        0,
        0.0,
    )
    assert child.slot.token >= parent.next_token
    assert child.next_token > child.slot.token
    said = [
        effect.text
        for effect in transition(parent, ContinueOnSetup(2), fake.world()).effects
        if type(effect).__name__ == "Announce"
    ]
    assert said[0].endswith("it re-enters the all-core stages from steady with the margin")


@pytest.mark.req("ALLCORE.continue")
def test_a_child_continued_from_a_bisect_of_steady_re_enters_steady_without_the_bisect():
    fake = paused_on_a_new_setup_in(Stage.BISECT, edges_hot={0: -2, 1: -2})
    assert fake.state.flow.bisect.entry is Stage.STEADY
    child = transition(fake.state, ContinueOnSetup(2), fake.world()).state
    assert (child.flow.stage, child.flow.bisect) == (Stage.STEADY, None)


@pytest.mark.req("ALLCORE.continue", "G12")
@pytest.mark.parametrize("validating", [False, True])
def test_a_child_continued_from_memory_or_a_validation_keeps_its_stage(validating):
    stage = Stage.TRANSIENT if validating else Stage.MEMORY
    fake = paused_on_a_new_setup_in(stage)
    parent = fake.state
    if validating:
        profile = tuple((r.core, r.value) for r in parent.cores)
        parent = replace(parent, flow=replace(parent.flow, validating=profile))
    result = transition(parent, ContinueOnSetup(2), fake.world())
    assert (result.state.flow.stage, result.state.cores) == (parent.flow.stage, parent.cores)
    said = [effect.text for effect in result.effects if type(effect).__name__ == "Announce"]
    assert "re-enters" not in said[0]


@pytest.mark.req("L01.2.stored")
@pytest.mark.parametrize(("held", "refused"), [((1, "paused"), None), ((9, "paused"), "session 9 is paused")])
def test_a_restart_supersedes_its_own_held_parent_but_not_another_held_session(held, refused):
    fake = paused_on_a_new_setup()
    result = transition(fake.state, RestartOnSetup(2, acknowledged=True), replace(fake.world(), held_session=held))
    assert result.refused == refused or (refused is not None and result.refused.startswith(refused))


@pytest.mark.req("L24.d.restart", "L24.l24b.restart")
def test_restart_begins_a_fresh_session_from_the_parent_s_config_and_supersedes_the_parent():
    fake = paused_on_a_new_setup()
    parent = fake.state
    result = transition(parent, RestartOnSetup(2, acknowledged=True), fake.world())
    child = result.state
    assert (child.session_id, child.lifecycle, child.config, child.setup) == (
        2,
        Lifecycle.RUNNING,
        parent.config,
        (("bios", "B"),),
    )
    assert all(r.phase is Phase.NOT_STARTED for r in child.cores)
    assert child.flow.stage is Stage.SEARCH
    [supersede] = [effect for effect in result.effects if isinstance(effect, Supersede)]
    assert (supersede.parent.status, supersede.origin, supersede.child.session_id) == ("superseded", "restart", 2)
    assert result.effects[0] is supersede


@pytest.mark.req("L24.d.refuse", "L24.l24b.events")
@pytest.mark.parametrize("choice", [ContinueOnSetup(2), RestartOnSetup(2, acknowledged=True)])
def test_a_choice_is_refused_unless_the_session_paused_on_a_setup_that_still_differs(choice):
    fake = paused_on_a_new_setup()
    fake.setup = (("bios", "A"),)
    unchanged = transition(fake.state, choice, fake.world())
    assert "nothing that affects a verdict changed since session 1 started" in unchanged.refused
    fake.setup = (("bios", "A"), ("python", "3.14.7"), ("zenpower", "0.4.0"))
    recorded_only = transition(fake.state, choice, fake.world())
    assert "nothing that affects a verdict changed since session 1 started" in recorded_only.refused
    running = running_fake()
    assert "only a session paused as setup_changed" in transition(running.state, choice, running.world()).refused


def test_a_restart_the_start_gates_refuse_leaves_the_parent_paused_and_untouched():
    fake = paused_on_a_new_setup()
    world = replace(fake.world(), preflight_refusal="the SMU is held by ryzenadj")
    result = transition(fake.state, RestartOnSetup(2, acknowledged=True), world)
    assert "held by ryzenadj" in result.refused
    assert result.state == fake.state
    assert result.effects == ()


HELD = World(
    cpus_of=((0, (0, 1)), (1, (2, 3)), (2, (4, 5))),
    domain_of=((0, 0), (1, 0), (2, 0)),
    bios_co=((0, 0), (1, 0), (2, -5)),
    co_range=(-50, 10),
)


def resuming_with_a_held_core():
    started = transition(SessionState(), Start(CFG, 1, 0, True), HELD).state
    paused = replace(started, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(PauseReason.RESUMING, "reading"))
    return paused


def evidence(same_boot: bool, reads: dict, written: dict | None = None, marked: bool = False) -> CrashEvidenceRead:
    return CrashEvidenceRead(
        dirty_end=not same_boot,
        same_boot=same_boot,
        was_running=False,
        co_now=tuple(sorted(reads.items())),
        last_written=tuple(sorted((written or {}).items())),
        marked_now=marked,
    )


def first_test_of_a_session_with_a_held_core(**world):
    started = transition(SessionState(), Start(CFG, 1, 0, True), HELD).state
    reads = {0: 0, 1: 0, 2: -5}
    seen = replace(HELD, co_now=tuple(sorted(reads.items())), resident=((0, 0), (1, 0)))
    return transition(started, ContinuationDue(started.slot.token), replace(seen, **world))


def test_a_test_with_every_co_as_left_starts():
    result = first_test_of_a_session_with_a_held_core()
    assert isinstance(result.state.slot, InFlight)


@pytest.mark.parametrize(
    ("reads", "reason", "cores", "says"),
    [
        ({0: 0, 1: 0, 2: -9}, PauseReason.OUTSIDE_WRITE, (2,), "core 2 reads -9, the session left it at -5"),
        ({0: -4, 1: 0, 2: -5}, PauseReason.OUTSIDE_WRITE, (0,), "core 0 reads -4, the session left it at 0"),
        ({0: 0, 1: None, 2: -5}, PauseReason.SMU_READ, (1,), "the CO of core 1 could not be read"),
        (None, PauseReason.SMU_READ, (0, 1, 2), "the CO of core 0, 1, 2 could not be read"),
    ],
)
@pytest.mark.req(
    "D3", "INV.L31", "L08.d.l31", "L31.4", "ENTRY_READS.2", "TM.I_M1", "VERDICT.31", "VERDICT.32", "WRITER.held"
)
def test_a_moved_or_unread_co_pauses_before_the_test_starts(reads, reason, cores, says):
    co_now = None if reads is None else tuple(sorted(reads.items()))
    result = first_test_of_a_session_with_a_held_core(co_now=co_now)
    assert (result.state.pause.reason, result.state.pause.cores) == (reason, cores)
    assert says in result.state.pause.detail
    assert not any(type(effect).__name__ == "StartWorker" for effect in result.effects)


def test_an_owned_core_the_rig_forgot_is_not_compared():
    result = first_test_of_a_session_with_a_held_core(co_now=((0, -4), (1, 0), (2, -5)), resident=())
    assert isinstance(result.state.slot, InFlight)


def test_after_a_sleep_gap_only_the_held_cores_are_compared():
    owned_moved = first_test_of_a_session_with_a_held_core(co_now=((0, -4), (1, 0), (2, -5)), gap_grew=True)
    assert isinstance(owned_moved.state.slot, InFlight)
    held_moved = first_test_of_a_session_with_a_held_core(co_now=((0, 0), (1, 0), (2, -9)), gap_grew=True)
    assert held_moved.state.pause.reason is PauseReason.OUTSIDE_WRITE


@pytest.mark.req("L31.b", "ENTRY_READS.1")
def test_an_outside_write_between_two_tests_of_one_stage_pauses_the_second():
    fake = fresh(2, [-3, -2], 1, max_offset=-5, k_1t=2)
    start(fake)
    fake.fire()
    stage = fake.state.flow.stage
    fake.deliver()
    fake.co[0] = -9
    fake.outside.add(0)
    while fake.worker is None and fake.state.lifecycle is Lifecycle.RUNNING:
        fake.fire()
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert (fake.state.pause.reason, fake.state.pause.cores, fake.state.flow.stage) == (
        PauseReason.OUTSIDE_WRITE,
        (0,),
        stage,
    )


def completed_with_a_held_core():
    started = transition(SessionState(), Start(CFG, 1, 0, True), HELD).state
    return replace(stored(started), status="completed")


@pytest.mark.parametrize(
    ("world", "says"),
    [
        (replace(HELD, bios_co=((0, 0), (1, None), (2, -5))), "the CO of core 1 could not be read"),
        (replace(HELD, bios_co=((0, 0), (1, 0))), "the CO of core 2 could not be read"),
        (
            replace(HELD, power=(("platform profile", "power-saver"),)),
            "the power state changed since session 1 started: platform profile was absent, is now power-saver",
        ),
    ],
)
@pytest.mark.req("KC.7")
def test_validate_refuses_a_moved_held_core_an_unread_co_or_a_moved_power_state(world, says):
    done = completed_with_a_held_core()
    assert says in refusal(SessionState(), ValidateProfile(1), replace(world, stored=done))


@pytest.mark.req("D86")
def test_validate_on_a_moved_held_core_pauses_as_setup_changed_naming_it():
    done = completed_with_a_held_core()
    moved = replace(HELD, bios_co=((0, 0), (1, 0), (2, -9)), stored=done)
    result = transition(SessionState(), ValidateProfile(1), moved)
    assert (result.state.pause.reason, result.state.pause.cores) == (PauseReason.SETUP_CHANGED, (2,))
    assert "core 2 is -9, the BIOS gave -5 when the session started" in result.state.pause.detail


def test_validate_starts_when_the_held_cores_and_the_power_state_are_as_recorded():
    done = completed_with_a_held_core()
    owned_moved = replace(HELD, bios_co=((0, -7), (1, 0), (2, -5)), stored=done)
    assert transition(SessionState(), ValidateProfile(1), owned_moved).state.lifecycle is Lifecycle.RUNNING


@pytest.mark.req("D48", "D7", "G10.read", "KC.4", "L18.d.8.read", "TERMS.pause_reasons", "VERDICT.40")
def test_a_core_whose_co_cannot_be_read_pauses_as_smu_read_naming_it():
    result = transition(resuming_with_a_held_core(), evidence(True, {0: 0, 1: None, 2: -5}), HELD)
    assert result.state.pause.reason is PauseReason.SMU_READ
    assert result.state.pause.cores == (1,)
    missing = transition(resuming_with_a_held_core(), evidence(True, {0: 0, 1: 0}), HELD)
    assert missing.state.pause.reason is PauseReason.SMU_READ
    assert missing.state.pause.cores == (2,)


@pytest.mark.req("L18.d.8.reboot", "TM.I_M5", "VERDICT.36")
def test_after_a_reboot_only_a_held_core_s_bios_change_pauses_as_setup_changed():
    owned_moved = transition(resuming_with_a_held_core(), evidence(False, {0: -7, 1: 0, 2: -5}), HELD)
    assert owned_moved.state.lifecycle is Lifecycle.RUNNING
    held_moved = transition(resuming_with_a_held_core(), evidence(False, {0: 0, 1: 0, 2: -9}), HELD)
    assert held_moved.state.pause.reason is PauseReason.SETUP_CHANGED
    assert "core 2 is -9, the BIOS gave -5 when the session started" in held_moved.state.pause.detail
    assert held_moved.state.pause.cores == (2,)


@pytest.mark.req("KC.1")
def test_a_held_core_s_bios_change_can_be_continued_and_the_child_takes_the_co_read_now_as_its_base():
    paused = transition(resuming_with_a_held_core(), evidence(False, {0: 0, 1: 0, 2: -9}), HELD).state
    now = replace(HELD, bios_co=((0, 0), (1, 0), (2, -9)))
    child = transition(paused, ContinueOnSetup(2), now).state
    assert (child.session_id, child.lifecycle, child.base_co) == (2, Lifecycle.RUNNING, now.bios_co)
    assert "nothing that affects a verdict changed" in transition(paused, ContinueOnSetup(2), HELD).refused


@pytest.mark.req("G10.outside", "KC.2", "L18.d.8.sameboot")
def test_in_the_same_boot_a_core_off_its_last_write_or_its_base_pauses_as_an_outside_write():
    owned = transition(resuming_with_a_held_core(), evidence(True, {0: -4, 1: 0, 2: -5}, {0: -3, 1: 0}), HELD)
    assert owned.state.pause.reason is PauseReason.OUTSIDE_WRITE
    assert "core 0 reads -4, the session left it at -3" in owned.state.pause.detail
    held = transition(resuming_with_a_held_core(), evidence(True, {0: -3, 1: 0, 2: -6}, {0: -3, 1: 0}), HELD)
    assert held.state.pause.reason is PauseReason.OUTSIDE_WRITE
    assert held.state.pause.cores == (2,)
    left = transition(resuming_with_a_held_core(), evidence(True, {0: -3, 1: 0, 2: -5}, {0: -3, 1: 0}), HELD)
    assert left.state.lifecycle is Lifecycle.RUNNING


@pytest.mark.req("KC.3")
def test_stock_on_an_owned_core_is_the_tool_s_own_revert_only_in_the_boot_the_marker_names():
    reads, written = {0: 0, 1: 0, 2: -5}, {0: -3, 1: 0}
    marked = transition(resuming_with_a_held_core(), evidence(True, reads, written, marked=True), HELD)
    assert marked.state.lifecycle is Lifecycle.RUNNING
    unmarked = transition(resuming_with_a_held_core(), evidence(True, reads, written), HELD)
    assert unmarked.state.pause.reason is PauseReason.OUTSIDE_WRITE


def test_a_test_value_at_or_past_its_fail_bound_pauses_as_internal_instead_of_running():
    fake = running_fake()
    bad = replace(fake.state.core(0), phase=Phase.PROVE, value=-8, fb=-8)
    fake.state = fake.state.with_core(bad)
    fake.fire()
    assert fake.state.pause.reason is PauseReason.INTERNAL
    assert fake.worker is None


@pytest.mark.req("L31.1", "VERDICT.39")
def test_a_sleep_between_tests_rewrites_every_owned_core():
    fake = running_fake()
    fake.gap_grew = True
    fake.co = {0: -3, 1: -3}
    fake.fire()
    assert fake.co[1] == 0
    assert fake.co[0] == fake.worker.spec.value


@pytest.mark.req("INV.L21", "K15", "L21.a")
def test_consecutive_first_value_fails_pause_at_the_threshold():
    fake = fresh(2, [0, 0], 1, max_offset=-20, k_1t=2, start_failure_threshold=2)
    start(fake)
    while fake.state.lifecycle is Lifecycle.RUNNING:
        fake.progress()
    assert fake.state.pause.reason is PauseReason.START_FAILURES


@pytest.mark.req("C5", "G5", "K17.retries", "L20.G5", "L20.G5.name")
@pytest.mark.parametrize(
    ("detail", "said"),
    [
        (
            "CPU temperature 96.3 C at the 95 C safety limit during the test",
            "4 thermal stops in a row at the 95 C limit; the last: CPU temperature 96.3 C at the 95 C safety limit"
            " during the test",
        ),
        ("", "4 thermal stops in a row at the 95 C limit"),
    ],
)
def test_the_fourth_thermal_stop_in_a_row_gives_up_naming_the_temperature(detail, said):
    fake = running_fake()
    fake.fire()
    for _ in range(4):
        fake.deliver(Report(Group.THERMAL, 5.0, detail=detail))
        if fake.state.lifecycle is not Lifecycle.RUNNING:
            break
        fake.fire()
    assert (fake.state.pause.reason, fake.state.pause.detail) == (PauseReason.THERMAL, said)


def test_a_failed_hint_at_minus_one_settles_at_stock():
    cfg = MachineConfig(Mode.CO, Method.SEQUENTIAL, (0,))
    rec = search.resolve_start(CoreRecord(0, start=-1), cfg)
    rec = search.on_fail(rec, cfg, Klass.T1, 60.0).record
    assert (rec.phase, rec.value) == (Phase.PROVEN, 0)


@pytest.mark.req("K14", "L04.budget")
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


@pytest.mark.req("D53", "K17.limit", "L20.d.wait")
@pytest.mark.parametrize(("tjmax", "limit"), [(None, 95.0), (89.0, 89.0), (100.0, 95.0)])
def test_each_test_runs_under_the_lower_of_the_set_limit_and_the_model_tjmax_and_cools_below_it(tjmax, limit):
    fake = running_fake()
    fake.tjmax_c = tjmax
    while fake.worker is None:
        assert fake.progress()
    assert fake.worker.spec.thermal_limit_c == limit
    fake.deliver(Report(Group.THERMAL, 5.0))
    assert fake.state.slot.cool_below_c == limit - fake.state.config.cool_margin_c


@pytest.mark.req("T22.F6")
def test_a_start_where_every_core_reads_0_needs_no_acknowledgement():
    started = transition(SessionState(), Start(CFG, 1, 0, False), LAYOUT)
    assert started.refused is None
    assert started.state.session_id == 1


@pytest.mark.req("EC.G5")
@pytest.mark.parametrize(("evidence", "failed"), [(True, True), (False, False)])
def test_a_search_pass_with_a_record_from_the_tested_core_fails_that_value(evidence, failed):
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.SEARCH:
        assert fake.progress()
    core = fake.worker.spec.core
    value = dict(fake.worker.spec.vector)[core]
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(core, True),) if evidence else ()))
    assert (fake.state.core(core).fb == value) is failed


@pytest.mark.req("TERMS.kvw_order")
@pytest.mark.parametrize(
    ("vector", "order"),
    [
        (((1, -10), (0, -5)), [0, 1]),
        (((0, -12), (1, -3)), [1, 0]),
    ],
)
def test_a_vector_change_writes_its_loosening_cores_before_its_tightening_ones(vector, order):
    from corecycler.tuner.machine.session import _start_test
    from corecycler.tuner.machine.types import WriteCO

    fake = running_fake()
    waiting = fake.state
    fake.fire()
    spec = replace(fake.state.slot.spec, vector=vector)
    spec = replace(spec, value=dict(vector)[spec.core])
    held = replace(fake.world(), resident=((0, -10), (1, -5)), co_now=((0, -10), (1, -5)), gap_grew=False)
    result = _start_test(waiting, waiting.flow, spec, held)
    assert [effect.core for effect in result.effects if isinstance(effect, WriteCO)] == order


@pytest.mark.req("TERMS.kvw_order")
def test_a_core_whose_held_value_is_unknown_is_written_between_the_loosening_and_the_tightening_ones():
    from corecycler.tuner.machine.session import _start_test
    from corecycler.tuner.machine.types import WriteCO

    fake = running_fake()
    waiting = fake.state
    fake.fire()
    vector = ((1, -10), (0, -5))
    spec = replace(fake.state.slot.spec, vector=vector, value=dict(vector)[fake.state.slot.spec.core])
    held = replace(fake.world(), resident=((1, -5),), co_now=((0, -10), (1, -5)), gap_grew=False)
    result = _start_test(waiting, waiting.flow, spec, held)
    assert [effect.core for effect in result.effects if isinstance(effect, WriteCO)] == [0, 1]


@pytest.mark.req("TERMS.kvw_order")
def test_a_loosening_core_is_written_before_a_core_whose_held_value_is_unknown():
    from corecycler.tuner.machine.session import _start_test
    from corecycler.tuner.machine.types import WriteCO

    fake = running_fake()
    waiting = fake.state
    fake.fire()
    vector = ((1, -10), (0, -5))
    spec = replace(fake.state.slot.spec, vector=vector, value=dict(vector)[fake.state.slot.spec.core])
    held = replace(fake.world(), resident=((0, -10),), co_now=((0, -10), (1, -5)), gap_grew=False)
    result = _start_test(waiting, waiting.flow, spec, held)
    assert [effect.core for effect in result.effects if isinstance(effect, WriteCO)] == [0, 1]


@pytest.mark.req("C14", "L01.15.changed", "D54")
@pytest.mark.parametrize(
    ("bios", "newest", "says"),
    [
        (((0, 0), (1, 0)), ((0, -10), (1, 0)), "the CO differs from when session 3 started: C0 -10 to 0"),
        (
            ((0, 5), (1, 0)),
            ((0, 5), (1, 0)),
            "cores [0] read a CO other than 0 at Start, set by the BIOS or written since boot",
        ),
        (
            ((0, -5), (1, 0)),
            ((0, 0), (1, 0)),
            "cores [0] read a CO other than 0 at Start, set by the BIOS or written since boot;"
            " the CO differs from when session 3 started: C0 0 to -5",
        ),
        (((0, 0), (1, 0)), ((0, None),), "the CO differs from when session 3 started: C0 unread to 0, C1 unread to 0"),
    ],
)
def test_a_start_names_every_selected_core_whose_co_differs_from_the_newest_session_s_base(bios, newest, says):
    world = replace(LAYOUT, bios_co=bios, newest_base=(3, newest))
    assert refusal(SessionState(), Start(CFG, 4, 0, False), world) == f"{says}: acknowledge to start"


@pytest.mark.req("C14", "L01.15.changed")
@pytest.mark.parametrize(
    ("newest", "acknowledged"),
    [(None, False), ((3, ((0, 0), (1, 0), (2, -7))), False), ((3, ((0, -10), (1, 0))), True)],
)
def test_a_start_asks_nothing_when_no_selected_core_changed_and_starts_once_a_change_is_acknowledged(
    newest, acknowledged
):
    result = transition(SessionState(), Start(CFG, 4, 0, acknowledged), replace(LAYOUT, newest_base=newest))
    assert result.refused is None
    assert result.state.session_id == 4


@pytest.mark.req("L17.d.readback")
def test_a_co_another_tool_writes_after_the_final_values_pauses_the_session_instead_of_completing_it():
    fake = fresh(2, [-3, -2], 1, max_offset=-5, k_1t=1)
    start(fake)
    while not (fake.state.flow.stage is Stage.FINALIZE and fake.state.flow.applied):
        fake.deliver() if fake.worker is not None else fake.fire()
    final = {r.core: r.value for r in fake.state.cores}
    fake.co[0] = -9
    fake.outside.add(0)
    fake.fire()
    assert (fake.state.lifecycle, fake.state.pause.reason, fake.state.pause.cores) == (
        Lifecycle.PAUSED,
        PauseReason.OUTSIDE_WRITE,
        (0,),
    )
    assert "core 0 reads -9" in fake.state.pause.detail
    fake.post(Resume(fake.state.session_id))
    while fake.state.lifecycle is Lifecycle.RUNNING:
        fake.fire()
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {core: fake.co[core] for core in final} == final


@pytest.mark.req("L01.G11")
def test_a_first_commit_that_fails_at_start_leaves_no_session_and_writes_nothing():
    fake = fresh(2, [-3, -2], 1, bios=-4, max_offset=-5, k_1t=1)
    fake.faults.fail_commits = 1
    start(fake)
    assert (fake.state.lifecycle, fake.state.session_id, fake.db) == (Lifecycle.IDLE, None, None)
    assert fake.co == {0: -4, 1: -4}
    assert fake.log[-1] == "database_failed: injected database failure; session 1 was not started"


@pytest.mark.req("ORDER.single", "L05.c.single")
@pytest.mark.parametrize(
    ("selected", "named"),
    [((1,), "session 1, 1 core, not interleaved"), ((0, 1), "session 1, 2 cores, sequential order")],
)
def test_start_names_a_one_core_selection_as_not_interleaved(selected, named):
    result = transition(SessionState(), Start(replace(CFG, selected=selected), 1, 0, True), LAYOUT)
    assert ("started", named) in [
        (effect.kind, effect.text) for effect in result.effects if type(effect).__name__ == "Announce"
    ]


@pytest.mark.req("L18.b.quarantine", "L18.d.8.reengage")
def test_a_quarantined_session_reengages_each_core_from_its_newest_proven_value_inside_its_bound():
    fake = running_fake()
    cores = (
        replace(fake.state.cores[0], value=-9, fb=-12, phase=Phase.PROVE),
        replace(fake.state.cores[1], value=-3, fb=-5, phase=Phase.PROVE),
    )
    cores = tuple(replace(rec, count=1, in_test=True) for rec in cores)
    row = replace(stored(replace(fake.state, cores=cores, resume_streak=3)), status="quarantined", flow=None)
    world = replace(LAYOUT, stored=row, proven=((0, (-14, -10, -8)), (1, (0, -6))))
    assert refusal(SessionState(), Resume(1, reengage=True), replace(world, preflight_refusal="no kmsg")) == "no kmsg"
    result = transition(SessionState(), Resume(1, reengage=True), world)
    first, second = result.state.cores
    assert (first.phase, first.value, first.count, first.in_test) == (Phase.PROVEN, -10, 0, False)
    assert (second.phase, second.value, second.start, second.count, second.in_test) == (
        Phase.NOT_STARTED,
        0,
        0,
        0,
        False,
    )
    assert result.state.resume_streak == 0
    assert result.effects[0].text == "session 1 re-engaged on proven ground: core 0 at -10 (proven), core 1 from stock"
    assert isinstance(result.effects[-1], ReadCrashEvidence)


@pytest.mark.req("L18.b.quarantine", "L18.d.8.reengage")
def test_a_session_quarantined_by_three_crashes_runs_again_only_when_reengaged():
    fake = fresh(2, [-40, -6], 1, max_offset=-40, k_1t=2, wait_between_cores_s=0.0, starts=((0, -30),))
    start(fake)
    while fake.state.lifecycle is not Lifecycle.QUARANTINED:
        while fake.worker is None:
            assert fake.progress()
        fake.crash()
        fake.post(Resume(1))
    assert "--reengage" in refusal(SessionState(), Resume(1), fake.world())
    fake.state = SessionState()
    fake.post(Resume(1, reengage=True))
    while fake.worker is None:
        assert fake.progress()
    assert fake.state.lifecycle is Lifecycle.RUNNING
    assert fake.state.resume_streak == 0
