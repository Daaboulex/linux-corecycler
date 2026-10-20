from __future__ import annotations

from itertools import takewhile

import pytest

from corecycler.tuner.machine import allcore
from corecycler.tuner.machine.types import (
    AllCore,
    ContinueOnSetup,
    CoreRecord,
    FailContext,
    Lifecycle,
    Load,
    PauseReason,
    Phase,
    Stage,
)
from tests._tuner_world import Evidence, Group, Report, Resume, fresh, run_to_rest, start

FAST = {
    "hot_seconds": 1.0,
    "bisect_seconds": 1.0,
    "stage_seconds": (("burst", 1.0), ("warm", 1.0), ("idle", 1.0)),
    "transient_window_s": 2.0,
    "soak_seconds": 1.0,
    "endurance_window_s": 5.0,
    "memory_seconds": 1.0,
    "wait_between_cores_s": 0.0,
    "k_1t": 1,
    "k_2t": 1,
}


def run(
    edges, hot=None, transient=None, cores=2, domains=2, stacks=(), on_start=None, names=False, memory_edges=None, **cfg
):
    fake = fresh(cores, edges, domains, max_offset=-8, **{**FAST, **cfg})
    fake.edges_hot = hot
    fake.edges_transient = transient
    fake.stacks = stacks
    fake.names_cores = names
    fake.edges_memory = memory_edges
    stages = []
    original = fake.check_start

    def spy(state, spec):
        stages.append((spec.stage, spec.load, dict(spec.vector)))
        if on_start is not None:
            on_start(fake, spec)
        original(state, spec)

    fake.check_start = spy
    start(fake)
    run_to_rest(fake, 20_000)
    return fake, stages


@pytest.mark.req(
    "C6",
    "D35",
    "INV.L00",
    "K41",
    "L00.a",
    "L04.b",
    "L07.a",
    "L07.f",
    "L08.G1",
    "L15.G1",
    "L15.a",
    "L16.G1",
    "TERMS.allcore_run",
    "D67",
)
def test_a_clean_co_only_run_walks_every_stage_once_in_the_planned_order():
    fake, stages = run([-4, -2])
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = list(dict.fromkeys(stage for stage, _, _ in stages))
    assert order == [
        Stage.SEARCH,
        Stage.LIVE1,
        Stage.STEADY,
        Stage.TRANSIENT,
        Stage.TIGHTEN,
        Stage.MEMORY,
        Stage.SOAK,
    ]
    assert {r.core: r.value for r in fake.state.cores} == {0: -4, 1: -2}


DISTINCT_LENGTHS = {"stage_seconds": (("burst", 3.0), ("warm", 4.0), ("idle", 5.0))}


@pytest.mark.req("ALLCORE.bisect.entry", "INV.L09", "INV.L10", "L09.b", "L10.a", "L11.a")
def test_an_unnamed_transient_failure_bisects_by_ccd_first_under_the_failed_load_with_the_rest_at_zero():
    slots = []

    def spy(fake, spec):
        if spec.stage is Stage.BISECT:
            slots.append((fake.state.flow.bisect.entry, spec.load, spec.cpus, spec.seconds, dict(spec.vector)))

    fake, _ = run([-6, -6, -6], transient={0: -6, 1: -2, 2: -6}, cores=3, on_start=spy, **DISTINCT_LENGTHS)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    burst = (Stage.TRANSIENT, Load.BURST, (0, 1, 2, 3, 4, 5), 3.0)
    assert slots == [
        (*burst, {0: 0, 1: 0, 2: 0}),
        (*burst, {0: -3, 1: -3, 2: 0}),
        (*burst, {0: 0, 1: 0, 2: -3}),
        (*burst, {0: -3, 1: 0, 2: 0}),
        (*burst, {0: 0, 1: -3, 2: 0}),
    ]


@pytest.mark.req("ALLCORE.bisect.single", "L09.f")
def test_a_core_a_transient_bisect_finds_backs_off_alone_and_the_failed_load_runs_again_from_zero_clean_time():
    fake = fresh(3, [-6, -6, -6], 2, max_offset=-8, **{**FAST, **DISTINCT_LENGTHS, "transient_window_s": 10.0})
    start(fake)
    while fake.worker is None or fake.worker.spec.load is not Load.WARM:
        assert fake.progress()
    assert (fake.worker.spec.stage, fake.state.flow.allcore.t_clean) == (Stage.TRANSIENT, 3.0)
    fake.edges_transient = {0: -6, 1: -2, 2: -6}
    fake.deliver()
    assert fake.state.flow.stage is Stage.BISECT
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    assert (fake.state.flow.stage, fake.state.flow.allcore.t_clean) == (Stage.TRANSIENT, 0.0)
    assert {r.core: r.value for r in fake.state.cores} == {0: -3, 1: -2, 2: -3}
    assert fake.state.core(1).fb == -3
    while fake.worker is None:
        assert fake.progress()
    again = fake.worker.spec
    assert (again.stage, again.load, again.seconds, dict(again.vector)) == (
        Stage.TRANSIENT,
        Load.WARM,
        4.0,
        {0: -3, 1: -2, 2: -3},
    )


def stack_from(stage, stacks):
    def on_start(fake, spec):
        if spec.stage is stage and not fake.stacks:
            fake.stacks = stacks

    return on_start


@pytest.mark.req("ALLCORE.bisect.group")
def test_cores_that_fail_the_transient_load_only_together_get_the_group_rule_and_the_rest_keep_their_reclaim():
    fake, stages = run([-6, -6, -6], cores=3, on_start=stack_from(Stage.TRANSIENT, (((), (0, 1), 5),)))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert [vector for stage, _, vector in stages if stage is Stage.BISECT] == [
        {0: 0, 1: 0, 2: 0},
        {0: -3, 1: -3, 2: 0},
        {0: 0, 1: 0, 2: -3},
        {0: -3, 1: 0, 2: 0},
        {0: 0, 1: -3, 2: 0},
        {0: -2, 1: -2, 2: 0},
    ]
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {0: (-2, -7), 1: (-2, -7), 2: (-6, -7)}


@pytest.mark.req("ALLCORE.bisect.universe", "L09.resolve", "L09.retest.fail")
def test_only_the_full_set_failing_the_transient_load_again_on_its_re_test_loosens_every_core_one_step():
    fake, stages = run([-6, -6, -6], cores=3, on_start=stack_from(Stage.TRANSIENT, (((), (0, 1, 2), 8),)))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert [vector for stage, _, vector in stages if stage is Stage.BISECT] == [
        {0: 0, 1: 0, 2: 0},
        {0: -3, 1: -3, 2: 0},
        {0: 0, 1: 0, 2: -3},
        {0: -3, 1: -3, 2: -3},
        {0: -2, 1: -2, 2: -2},
    ]
    assert {r.core: r.value for r in fake.state.cores} == {0: -2, 1: -2, 2: -2}


STEADY_CLASS = FailContext(steady=True, working=(0, 1), paused=False)


@pytest.mark.req("L09.retest.pass")
@pytest.mark.parametrize(
    ("entry", "load", "context", "cfg"),
    [
        (Stage.STEADY, Load.HOT, STEADY_CLASS, {}),
        (Stage.ENDURANCE, Load.HOT, STEADY_CLASS, {"endurance": True}),
        (Stage.TRANSIENT, Load.BURST, None, {}),
        (Stage.MEMORY, Load.MEMORY, None, {}),
    ],
)
def test_a_failure_only_the_full_set_had_passes_its_re_test_and_the_bisect_exits_with_nothing_moved(
    entry, load, context, cfg
):
    clean, clean_stages = run([-6, -6], **cfg)
    one_off = Report(Group.SILICON, 1.0, context=context)
    fake, stages = run([-6, -6], on_start=report_once(entry, one_off), **cfg)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    failed = next(vector for stage, _, vector in stages if stage is entry)
    last = max(i for i, (stage, _, _) in enumerate(stages) if stage is Stage.BISECT)
    assert stages[last][1:] == (load, failed)
    assert stages[last + 1][0] is entry
    assert {r.core: r.value for r in fake.state.cores} == {r.core: r.value for r in clean.state.cores}
    live1 = [stage for stage, _, _ in stages].count(Stage.LIVE1)
    assert live1 == [stage for stage, _, _ in clean_stages].count(Stage.LIVE1)


@pytest.mark.req("L09.retest.hang")
@pytest.mark.parametrize("cut", ["hang", "lost verdict"])
def test_a_re_test_of_the_full_set_cut_by_a_hang_or_a_lost_verdict_is_its_fail_on_resume(cut):
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.TRANSIENT:
        assert fake.progress()
    held = dict(fake.worker.spec.vector)
    fake.deliver(Report(Group.SILICON, 1.0))
    while fake.worker is None or 0 in dict(fake.worker.spec.vector).values():
        assert fake.progress()
    assert (fake.worker.spec.stage, dict(fake.worker.spec.vector)) == (Stage.BISECT, held)
    if cut == "lost verdict":
        fake.faults.fail_commits = 1
        fake.deliver()
        assert fake.state.pause.reason is PauseReason.DATABASE
    fake.crash()
    fake.post(Resume(1))
    flow = fake.state.flow
    assert (fake.state.lifecycle, flow.stage, flow.bisect.rerun) == (Lifecycle.RUNNING, Stage.BISECT, (0, 1))
    assert {r.core: r.value for r in fake.state.cores} == {core: value + 1 for core, value in held.items()}
    assert set(flow.allcore.frozen) == {0, 1}


@pytest.mark.req("L09.retest.single")
def test_a_one_core_full_set_that_fails_its_re_test_backs_off_in_place_as_a_found_core():
    fake = fresh(2, [-6, 0], 2, max_offset=-8, **FAST)
    fake.edges_transient = {0: -2, 1: 0}
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.TRANSIENT:
        assert fake.progress()
    assert dict(fake.worker.spec.vector) == {0: -3, 1: 0}
    fake.deliver()
    assert fake.state.flow.bisect.universe == (0,)
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    cursor = fake.state.flow.allcore
    assert (fake.state.flow.stage, cursor.t_clean, 0 in cursor.frozen) == (Stage.TRANSIENT, 0.0, False)
    assert (fake.state.core(0).value, fake.state.core(0).fb, fake.state.core(1).value) == (-2, -3, 0)


LONG_HOT = {**FAST, "hot_seconds": 3.0}


def failing_only_in(entry):
    def on_start(fake, spec):
        if spec.stage is entry and spec.load is Load.HOT and any(value < -2 for _, value in spec.vector):
            once(fake, Report(Group.SILICON, spec.seconds, context=STEADY_CLASS))

    return on_start


@pytest.mark.req("L09.rest")
@pytest.mark.parametrize("entry", [Stage.STEADY, Stage.ENDURANCE])
def test_a_hot_failure_that_only_the_stage_s_own_test_shows_comes_to_rest(entry):
    fake, _ = run([-6, -6], on_start=failing_only_in(entry), endurance=entry is Stage.ENDURANCE, **LONG_HOT)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == {0: -2, 1: -2}


def crashing_until_rest(fake, crashes, limit=3000):
    start(fake)
    for _ in range(limit):
        if fake.worker is not None and crashes(fake.worker.spec):
            fake.crash()
            fake.post(Resume(1))
        elif not fake.progress():
            return
    raise AssertionError(f"no rest within {limit} moves, {fake.state.lifecycle} in {fake.state.flow.stage}")


@pytest.mark.req("L09.rest")
@pytest.mark.parametrize("stage", [Stage.STEADY, Stage.TRANSIENT])
def test_a_hang_that_only_the_stage_s_own_test_shows_comes_to_rest(stage):
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **LONG_HOT)
    crashing_until_rest(fake, lambda spec: spec.stage is stage and any(value < -2 for _, value in spec.vector))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == {0: -2, 1: -2}


@pytest.mark.req("L09.retest.replay")
@pytest.mark.parametrize(
    ("entry", "context", "cfg"),
    [
        (Stage.STEADY, STEADY_CLASS, {}),
        (Stage.ENDURANCE, STEADY_CLASS, {"endurance": True}),
        (Stage.TRANSIENT, None, DISTINCT_LENGTHS),
        (Stage.MEMORY, None, {}),
    ],
)
def test_the_full_set_re_test_replays_the_failed_test_itself(entry, context, cfg):
    specs = []
    one_off = report_once(entry, Report(Group.SILICON, 1.0, context=context))

    def spy(fake, spec):
        specs.append(spec)
        one_off(fake, spec)

    run([-6, -6], on_start=spy, **{**LONG_HOT, **cfg})
    failed = next(spec for spec in specs if spec.stage is entry)
    retest = next(spec for spec in specs if spec.stage is Stage.BISECT and spec.vector == failed.vector)
    assert (retest.load, retest.seconds, retest.cpus) == (failed.load, failed.seconds, failed.cpus)
    assert (retest.quick, retest.plan_seed) == (failed.quick, failed.plan_seed)


@pytest.mark.req("ALLCORE.bisect.seed", "K40.slot", "D40.slot")
@pytest.mark.parametrize(("entry", "context", "cfg"), [(Stage.TRANSIENT, None, {}), (Stage.STEADY, STEADY_CLASS, {})])
def test_transient_slots_replay_the_failed_pattern_and_hot_halves_run_the_quick_hot_test(entry, context, cfg):
    specs = []
    one_off = report_once(entry, Report(Group.SILICON, 1.0, context=context))

    def spy(fake, spec):
        specs.append(spec)
        one_off(fake, spec)

    run([-6, -6, -6], cores=3, on_start=spy, **{**LONG_HOT, **DISTINCT_LENGTHS, **cfg})
    failed = next(spec for spec in specs if spec.stage is entry)
    halves = [spec for spec in specs if spec.stage is Stage.BISECT and spec.vector != failed.vector]
    shapes = {(spec.load, spec.seconds, spec.quick, spec.plan_seed) for spec in halves}
    if entry is Stage.TRANSIENT:
        assert shapes == {(failed.load, failed.seconds, False, failed.plan_seed)}
    else:
        assert {shape[:3] for shape in shapes} == {(Load.HOT, 1.0, True)}


UNNAMED_STEADY = Report(Group.SILICON, 1.0, context=STEADY_CLASS)


def scripted(entry, reports):
    seen = []

    def on_start(fake, spec):
        if spec.stage is entry:
            seen.append(spec)
            if len(seen) in reports:
                once(fake, reports[len(seen)])

    return on_start


def full_set_slots(stages):
    return [vector for stage, _, vector in stages if stage is Stage.BISECT and 0 not in vector.values()]


@pytest.mark.req("L09.one_off")
def test_a_stage_failing_again_at_the_values_its_one_off_held_skips_the_re_test():
    fake, stages = run([-6, -6], on_start=scripted(Stage.STEADY, {1: UNNAMED_STEADY, 2: UNNAMED_STEADY}))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert full_set_slots(stages) == [{0: -3, 1: -3}, {0: -2, 1: -2}]
    assert {r.core: r.value for r in fake.state.cores} == {0: -2, 1: -2}


@pytest.mark.req("L09.one_off")
def test_the_one_off_mark_applies_only_at_its_values_so_a_failure_at_moved_values_gets_its_re_test():
    moved = Report(Group.PASS, 1.0, evidence=(Evidence(1, True),))
    script = {1: UNNAMED_STEADY, 2: moved, 3: UNNAMED_STEADY, 4: UNNAMED_STEADY}
    fake, stages = run([-6, -6], on_start=scripted(Stage.STEADY, script))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert full_set_slots(stages) == [{0: -3, 1: -3}, {0: -3, 1: -2}, {0: -2, 1: -1}]


def until_stage(fake, stage):
    while fake.worker is None or fake.worker.spec.stage is not stage:
        assert fake.progress()
    return dict(fake.worker.spec.vector)


@pytest.mark.req("L09.one_off.clear")
def test_a_continue_under_a_new_setup_clears_the_one_off_mark():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    held = until_stage(fake, Stage.STEADY)
    fake.deliver(UNNAMED_STEADY)
    until_stage(fake, Stage.TRANSIENT)
    fake.setup = (("bios", "B"),)
    fake.crash()
    fake.post(Resume(1))
    fake.post(ContinueOnSetup(2))
    assert until_stage(fake, Stage.STEADY) == held
    fake.deliver(UNNAMED_STEADY)
    replays = 0
    while fake.state.flow.stage is Stage.BISECT:
        replays += fake.worker is not None and dict(fake.worker.spec.vector) == held
        assert fake.progress()
    assert (replays, {r.core: r.value for r in fake.state.cores}) == (1, held)


@pytest.mark.req("L09.one_off.clear")
def test_a_new_live1_pass_starts_without_the_one_off_mark():
    steady = scripted(Stage.STEADY, {1: UNNAMED_STEADY, 3: UNNAMED_STEADY})
    tighten = scripted(Stage.TIGHTEN, {1: Report(Group.SILICON, 1.0)})

    def both(fake, spec):
        steady(fake, spec)
        tighten(fake, spec)

    fake, stages = run([-6, -6], on_start=both)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert [stage for stage, _, _ in stages].count(Stage.LIVE1) == 4
    assert full_set_slots(stages) == [{0: -3, 1: -3}, {0: -3, 1: -3}]


@pytest.mark.req("L09.seed.distinct")
def test_every_planned_stage_test_of_a_run_carries_its_own_plan_seed():
    specs = []
    fake, _ = run([-6, -3], memory_edges={0: -4, 1: -3}, endurance=True, on_start=lambda fake, spec: specs.append(spec))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    planned = [spec for spec in specs if spec.plan_seed is not None and spec.stage is not Stage.BISECT]
    assert [spec.stage for spec in planned].count(Stage.ENDURANCE) > 5
    assert len({spec.plan_seed for spec in planned}) == len(planned)


@pytest.mark.req("L09.retest.named")
@pytest.mark.parametrize(("entry", "context"), [(Stage.STEADY, STEADY_CLASS), (Stage.TRANSIENT, None)])
def test_a_re_test_that_fails_naming_a_core_backs_off_only_that_core_and_returns_to_the_stage(entry, context):
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = until_stage(fake, entry)
    fake.deliver(Report(Group.SILICON, 1.0, context=context))
    while fake.worker is None or dict(fake.worker.spec.vector) != held:
        assert fake.progress()
    assert fake.worker.spec.stage is Stage.BISECT
    frozen = fake.state.flow.allcore.frozen
    fake.deliver(Report(Group.SILICON, 1.0, context=context, named=(2,)))
    flow = fake.state.flow
    assert (flow.stage, flow.bisect, flow.allcore.t_clean, flow.allcore.frozen) == (entry, None, 0.0, frozen)
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {0: (-3, -7), 1: (-3, -7), 2: (-2, -3), 3: (-3, -7)}


@pytest.mark.req("L09.retest.named")
def test_a_re_test_that_names_a_core_the_session_does_not_own_pauses_naming_it_as_the_stage_does():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    held = until_stage(fake, Stage.STEADY)
    fake.deliver(UNNAMED_STEADY)
    while fake.worker is None or dict(fake.worker.spec.vector) != held:
        assert fake.progress()
    fake.deliver(Report(Group.SILICON, 1.0, context=STEADY_CLASS, named=(7,)))
    assert (fake.state.pause.reason, fake.state.pause.cores) == (PauseReason.HELD_KNOB, (7,))
    assert {r.core: r.value for r in fake.state.cores} == held


@pytest.mark.req("L09.retest.named")
def test_a_memory_re_test_that_fails_naming_a_lane_backs_it_off_and_requeues_it_as_the_memory_stage_does():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    held = until_stage(fake, Stage.MEMORY)
    fake.deliver(Report(Group.SILICON, 1.0))
    while fake.worker is None or dict(fake.worker.spec.vector) != held:
        assert fake.progress()
    assert fake.worker.spec.stage is Stage.BISECT
    fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
    flow = fake.state.flow
    assert (flow.stage, flow.bisect, flow.live1.requeue, flow.live1.back) == (Stage.LIVE1, None, (1,), Stage.MEMORY)
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {0: (-6, -7), 1: (-5, -6)}


@pytest.mark.req("ALLCORE.bisect.hang", "L09.G4")
def test_a_hang_in_a_transient_bisect_slot_is_that_group_s_fail_on_resume():
    fake = fresh(3, [-6, -6, -6], 2, max_offset=-8, **FAST)
    fake.edges_transient = {0: -6, 1: -2, 2: -6}
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.TRANSIENT:
        assert fake.progress()
    fake.deliver()
    assert fake.state.flow.stage is Stage.BISECT
    while fake.worker is None or dict(fake.worker.spec.vector) != {0: -3, 1: -3, 2: 0}:
        assert fake.progress()
    fake.crash()
    fake.post(Resume(1))
    bisecting = fake.state.flow.bisect
    assert (fake.state.lifecycle, fake.state.flow.stage, bisecting.entry) == (
        Lifecycle.RUNNING,
        Stage.BISECT,
        Stage.TRANSIENT,
    )
    assert (bisecting.results[-1], bisecting.queue) == (((0, 1), False), ((2,), (0,), (1,)))
    assert {r.core: r.value for r in fake.state.cores} == {0: -3, 1: -3, 2: -3}


@pytest.mark.req("ALLCORE.bisect.zero")
def test_an_unnamed_transient_failure_with_every_core_at_zero_pauses_as_stock_unstable_without_a_bisect():
    fake, stages = run([0, 0], transient={0: 1, 1: 1})
    assert (fake.state.lifecycle, fake.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.STOCK_UNSTABLE)
    assert fake.state.pause.detail == "the all-core load fails with every owned core at 0"
    assert Stage.BISECT not in {stage for stage, _, _ in stages}


@pytest.mark.req("ALLCORE.margin", "ALLCORE.rounds", "ALLCORE.second_pass", "L12.a", "L12.floor")
def test_the_all_core_stages_start_every_core_three_steps_looser_and_reclaim_it_in_three_rounds():
    fake, stages = run([-6, -2])
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert next(vector for stage, _, vector in stages if stage is Stage.STEADY) == {0: -3, 1: 0}
    tighten = [(load, vector) for stage, load, vector in stages if stage is Stage.TIGHTEN]
    assert [load for load, _ in tighten] == [Load.HOT, Load.BURST, Load.WARM, Load.BURST, Load.IDLE] * 3
    assert [vector for _, vector in tighten[::5]] == [{0: -4, 1: -1}, {0: -5, 1: -2}, {0: -6, 1: -2}]
    assert {r.core: r.value for r in fake.state.cores} == {0: -6, 1: -2}
    assert [stage for stage, _, _ in stages].count(Stage.LIVE1) == 2


@pytest.mark.req("ALLCORE.freeze", "L08.G2.steady", "L08.a", "L08.b", "L09.G2.single", "L09.d")
def test_a_steady_failure_bisects_to_the_failing_core_and_reruns_steady():
    fake, stages = run([-6, -2], hot={0: -2, 1: -2})
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.BISECT in {stage for stage, _, _ in stages}
    assert {r.core: r.value for r in fake.state.cores} == {0: -2, 1: -2}
    hot_runs = [vector for stage, load, vector in stages if stage is Stage.STEADY]
    assert len(hot_runs) >= 2
    assert all(vector[0] == -2 for stage, _, vector in stages if stage is Stage.TIGHTEN)


@pytest.mark.req("ALLCORE.rounds", "C18", "INV.L11", "L12.a", "L12.floor")
def test_an_unnamed_failure_in_a_round_ends_the_reclaim_with_no_record_moved():
    fake, stages = run([-6, -3], transient={0: -5, 1: -3})
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == {0: -5, 1: -2}
    tighten = [vector for stage, _, vector in stages if stage is Stage.TIGHTEN]
    assert {0: -6, 1: -3} in tighten
    assert Stage.BISECT not in {stage for stage, _, _ in stages}


@pytest.mark.req("D44", "L11.G1", "L17.d.redirect", "RV1")
def test_a_loosening_after_live1_sends_co_only_through_a_second_pass_without_tighten():
    fake, stages = run([-6, -3], transient={0: -5, 1: -3})
    live1_runs = [i for i, (stage, _, _) in enumerate(stages) if stage is Stage.LIVE1]
    steady_runs = [i for i, (stage, _, _) in enumerate(stages) if stage is Stage.STEADY]
    tighten_runs = [i for i, (stage, _, _) in enumerate(stages) if stage is Stage.TIGHTEN]
    assert max(live1_runs) > min(steady_runs)
    assert max(tighten_runs) < max(live1_runs)


@pytest.mark.req("L16.b")
def test_soak_can_be_switched_off():
    fake, stages = run([-4, -2], soak=False)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.SOAK not in {stage for stage, _, _ in stages}


@pytest.mark.req("L10.d.bound", "L10.e")
def test_a_failure_naming_no_core_with_every_core_at_zero_pauses_as_nothing_left_to_loosen():
    fake, _ = run([0, 0], on_start=report_once(Stage.STEADY, Report(Group.SILICON, 1.0)))
    assert (fake.state.lifecycle, fake.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.STOCK_UNSTABLE)
    assert "nothing left to loosen" in fake.state.pause.detail
    assert {r.core: r.value for r in fake.state.cores} == {0: 0, 1: 0}


@pytest.mark.req("L08.f")
def test_a_steady_failure_with_every_core_at_zero_pauses_as_stock_unstable():
    fake, stages = run([0, 0], hot={0: 1, 1: 1})
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert fake.state.pause.reason is PauseReason.STOCK_UNSTABLE
    assert [vector for stage, _, vector in stages if stage is Stage.STEADY] == [{0: 0, 1: 0}]
    assert Stage.BISECT not in {stage for stage, _, _ in stages}


@pytest.mark.req("INV.L12", "K34", "L12.f", "L14.G1", "L14.a", "L14.b", "PLAN.co", "Q9")
def test_endurance_is_off_unless_asked_and_when_on_cycles_its_five_loads_before_soak():
    fake, stages = run([-4, -2], endurance=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = list(dict.fromkeys(stage for stage, _, _ in stages))
    assert order == [
        Stage.SEARCH,
        Stage.LIVE1,
        Stage.STEADY,
        Stage.TRANSIENT,
        Stage.TIGHTEN,
        Stage.ENDURANCE,
        Stage.MEMORY,
        Stage.SOAK,
    ]
    loads = [load for stage, load, _ in stages if stage is Stage.ENDURANCE]
    assert loads == [Load.HOT, Load.BURST, Load.WARM, Load.BURST, Load.IDLE]
    assert Stage.ENDURANCE not in {stage for stage, _, _ in run([-4, -2])[1]}


def degrade(hot=None, transient=None):
    def on_start(fake, spec):
        if spec.stage is Stage.ENDURANCE and fake.edges_hot is None and fake.edges_transient is None:
            fake.edges_hot = hot
            fake.edges_transient = transient

    return on_start


@pytest.mark.req("INTERRUPT.bisect_clock", "L09.b", "L09.f", "L14.G2", "NESTING")
def test_a_steady_failure_in_endurance_bisects_and_comes_back_to_endurance_at_hot_with_its_clean_time_reset():
    fake, stages = run([-6, -2], endurance=True, on_start=degrade(hot={0: -4, 1: -2}))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert fake.state.core(0).value >= -4
    seen = [(stage, load) for stage, load, _ in stages]
    first_bisect = seen.index((Stage.BISECT, Load.HOT))
    assert seen[first_bisect - 1] == (Stage.ENDURANCE, Load.HOT)
    returns = [seen[i + 1] for i in range(first_bisect, len(seen) - 1) if seen[i][0] is Stage.BISECT]
    assert {after for after in returns if after[0] is not Stage.BISECT} == {(Stage.ENDURANCE, Load.HOT)}
    last = max(i for i, (stage, _) in enumerate(seen) if stage is Stage.BISECT)
    after = [load for stage, load in seen[last:] if stage is Stage.ENDURANCE][:5]
    assert after == [Load.HOT, Load.BURST, Load.WARM, Load.BURST, Load.IDLE]


@pytest.mark.req("ALLCORE.unnamed", "L10.d.apply")
def test_a_transient_failure_in_endurance_loosens_every_core_and_runs_the_same_load_again():
    fake, stages = run([-6, -3], endurance=True, on_start=degrade(transient={0: -5, 1: -3}))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert fake.state.core(0).value == -5
    endurance = [(load, vector) for stage, load, vector in stages if stage is Stage.ENDURANCE]
    assert endurance[:3] == [
        (Load.HOT, {0: -6, 1: -3}),
        (Load.BURST, {0: -6, 1: -3}),
        (Load.BURST, {0: -5, 1: -2}),
    ]


@pytest.mark.req("D57", "INV.L08", "L08.G2.named", "L08.d.vector")
def test_a_steady_failure_that_names_a_core_backs_that_core_off_in_place_without_a_bisect():
    fake, stages = run([-6, -2], hot={0: -2, 1: -2}, names=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.BISECT not in {stage for stage, _, _ in stages}
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-2, -3)
    assert [vector[0] for stage, _, vector in stages if stage is Stage.STEADY][:2] == [-3, -2]
    order = [stage for stage, _, _ in stages]
    assert len(order) - order[::-1].index(Stage.LIVE1) > order.index(Stage.STEADY)


@pytest.mark.req("ALLCORE.bisect.named", "L12.b", "D30")
def test_a_transient_failure_that_names_a_core_backs_it_off_and_the_reclaim_leaves_it_there():
    fake, stages = run([-6, -3], transient={0: -2, 1: -3}, names=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.BISECT not in {stage for stage, _, _ in stages}
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-2, -3)
    assert fake.state.core(1).value == -3
    assert all(vector[0] == -2 for stage, _, vector in stages if stage is Stage.TIGHTEN)


def name_from(stage, hot=None, transient=None):
    def on_start(fake, spec):
        if spec.stage is stage and not fake.names_cores:
            fake.names_cores = True
            fake.edges_hot = hot or fake.edges_hot
            fake.edges_transient = transient or fake.edges_transient

    return on_start


@pytest.mark.req("C18", "L12.G2.named")
def test_a_round_failure_that_names_a_round_core_freezes_it_and_the_round_repeats_without_it():
    fake, stages = run([-6, -3], transient={0: -5, 1: -3}, on_start=name_from(Stage.TIGHTEN))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == {0: -5, 1: -3}
    tighten = [vector for stage, _, vector in stages if stage is Stage.TIGHTEN]
    failing = len(tighten) - 1 - tighten[::-1].index({0: -6, 1: -3})
    assert tighten[failing + 1] == {0: -5, 1: -3}
    loads = [load for stage, load, _ in stages if stage is Stage.TIGHTEN]
    assert loads[failing + 1] is Load.HOT


def report_once(stage, report, nth=1):
    seen = []

    def on_start(fake, spec):
        if spec.stage is stage:
            seen.append(spec)
        if len(seen) == nth and not getattr(fake, "reported", False):
            fake.reported = True
            original = fake.verdict_for

            def once(spec):
                fake.verdict_for = original
                return report

            fake.verdict_for = once

    return on_start


@pytest.mark.req("L12.G2.named", "RV5")
def test_a_round_failure_that_names_a_core_outside_the_round_backs_it_off_and_repeats_the_round():
    named = Report(Group.SILICON, 1.0, named=(1,))
    fake, stages = run([-6, -1], on_start=report_once(Stage.TIGHTEN, named, nth=6))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (fake.state.core(1).value, fake.state.core(1).fb) == (0, -1)
    assert fake.state.core(0).value == -6
    tighten = [(load, vector) for stage, load, vector in stages if stage is Stage.TIGHTEN]
    assert tighten[5:7] == [(Load.HOT, {0: -5, 1: -1}), (Load.HOT, {0: -5, 1: 0})]


def test_a_named_core_outside_the_session_pauses_as_a_held_knob():
    named = Report(Group.SILICON, 1.0, named=(7,))
    fake, _ = run([-4, -2], on_start=report_once(Stage.STEADY, named))
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert fake.state.pause.reason is PauseReason.HELD_KNOB and "core 7" in fake.state.pause.detail


def test_a_named_core_already_at_zero_leaves_the_failure_to_the_stage_rule():
    fake, stages = run([0, -6], hot={0: 1, 1: -6}, names=True)
    assert Stage.BISECT in {stage for stage, _, _ in stages}


@pytest.mark.req("K32", "L11.G2", "L11.d")
def test_a_named_back_off_in_transient_restarts_the_clean_window_on_the_same_load():
    named = Report(Group.SILICON, 1.0, named=(0,))
    fake, stages = run([-6, -3], on_start=report_once(Stage.TRANSIENT, named, nth=2))
    begin = next(i for i, (stage, _, _) in enumerate(stages) if stage is Stage.TRANSIENT)
    first_pass = [load for _, load, _ in takewhile(lambda run: run[0] is Stage.TRANSIENT, stages[begin:])]
    assert first_pass == [Load.BURST, Load.WARM, Load.WARM, Load.BURST]
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-2, -3)


def test_memory_can_be_switched_off():
    fake, stages = run([-4, -2], memory=False)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.MEMORY not in {stage for stage, _, _ in stages}


@pytest.mark.req("L07.b", "L07.d", "L15.G2", "ORDER.where")
def test_a_failing_memory_lane_backs_its_core_off_requeues_it_for_a_live1_slot_and_runs_memory_again():
    fake, stages = run([-6, -3], memory_edges={0: -4, 1: -3})
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-4, -5)
    begin = next(i for i, (stage, _, _) in enumerate(stages) if stage is Stage.MEMORY)
    walk = [(stage, vector[0]) for stage, _, vector in stages[begin : begin + 5]]
    assert walk == [(Stage.MEMORY, -6), (Stage.LIVE1, -5), (Stage.MEMORY, -5), (Stage.LIVE1, -4), (Stage.MEMORY, -4)]
    assert Stage.STEADY in {stage for stage, _, _ in stages[begin:]}


@pytest.mark.req("L15.G2.zero")
def test_a_memory_lane_failing_at_zero_gets_a_solo_retest_while_another_core_is_not_at_zero():
    fake, stages = run([0, -3], memory_edges={0: 1, 1: -3})
    solos = [vector for stage, _, vector in stages if stage is Stage.LIVE1 and vector == {0: 0, 1: 0}]
    assert solos
    assert fake.state.lifecycle is Lifecycle.PAUSED and fake.state.pause.reason is PauseReason.STOCK_UNSTABLE
    assert fake.state.core(1).value == 0 and fake.state.core(1).fb == -1


@pytest.mark.req("L15.G2.held")
def test_a_memory_lane_outside_the_session_pauses_as_a_held_knob():
    fake, _ = run([-4, -2], on_start=report_once(Stage.MEMORY, Report(Group.SILICON, 1.0, named=(5,))))
    assert fake.state.pause.reason is PauseReason.HELD_KNOB and "core 5" in fake.state.pause.detail


@pytest.mark.req("K40.memory", "L15.G4")
def test_a_memory_failure_naming_no_lane_bisects_with_the_memory_slot():
    fake, stages = run([-4, -2], on_start=report_once(Stage.MEMORY, Report(Group.SILICON, 1.0)))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    slots = [load for stage, load, _ in stages if stage is Stage.BISECT]
    assert slots and set(slots) == {Load.MEMORY}


@pytest.mark.req("L15.b")
def test_a_skipped_memory_run_is_no_verdict_and_the_plan_goes_on():
    fake = fresh(2, [-4, -2], 2, max_offset=-8, **FAST)
    fake.memory_skip = "stressapptest is not installed"
    start(fake)
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert any(line.startswith("memory_skipped: ") and "not installed" in line for line in fake.log)


@pytest.mark.req("L09.skip")
def test_a_skipped_bisect_slot_counts_as_apparatus_and_runs_again():
    def on_start(fake, spec):
        if spec.stage is Stage.MEMORY and not getattr(fake, "failed_memory", False):
            fake.failed_memory = True
            once(fake, Report(Group.SILICON, 1.0))
        elif spec.stage is Stage.BISECT and not getattr(fake, "skipped_slot", False):
            fake.skipped_slot = True
            once(fake, Report(Group.PASS, 0.0, skipped="the memory budget no longer fits"))

    fake, stages = run([-4, -2], on_start=on_start)
    slots = [vector for stage, _, vector in stages if stage is Stage.BISECT]
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert slots[0] == slots[1]


def once(fake, report):
    original = fake.verdict_for

    def first(spec):
        fake.verdict_for = original
        return report

    fake.verdict_for = first


@pytest.mark.req("ALLCORE.freeze", "K47")
def test_cores_that_fail_only_together_are_loosened_without_a_fail_bound_and_left_out_of_the_reclaim():
    fake, stages = run([-6, -6], stacks=(((), (0, 1), 5),))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert any(stage is Stage.BISECT for stage, _, _ in stages)
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {0: (-2, -7), 1: (-2, -7)}
    assert Stage.TIGHTEN not in {stage for stage, _, _ in stages}


@pytest.mark.req("L12.G4.own")
def test_a_crash_blamed_on_a_round_core_backs_it_off_three_steps_and_takes_it_out_of_the_reclaim():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    fake.edges_transient = {0: -5, 1: -5}
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.TIGHTEN:
        assert fake.progress()
    trial = dict(fake.worker.spec.vector)[0]
    fake.crash(evidence=(Evidence(0, False),))
    fake.post(Resume(1))
    assert 0 in fake.state.flow.allcore.frozen
    assert (fake.state.core(0).fb, fake.state.core(0).value) == (trial, trial + 3)


@pytest.mark.req("L11.h")
def test_a_crash_convicted_in_transient_restarts_its_clean_time_and_freezes_the_core():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.TRANSIENT or fake.state.flow.allcore.t_clean == 0:
        assert fake.progress()
    before = fake.state.core(0).value
    fake.crash(evidence=(Evidence(0, False),))
    fake.post(Resume(1))
    cursor = fake.state.flow.allcore
    assert (fake.state.flow.stage, cursor.t_clean, 0 in cursor.frozen) == (Stage.TRANSIENT, 0.0, True)
    assert fake.state.core(0).value > before


@pytest.mark.req("ALLCORE.rounds")
def test_the_reclaim_stops_after_three_rounds_even_with_more_steps_to_take_back():
    unnamed = Report(Group.SILICON, 1.0)
    fake, stages = run([-6, -5], on_start=report_once(Stage.STEADY, unnamed))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert sum(1 for stage, _, _ in stages if stage is Stage.TIGHTEN) == 3 * 5
    assert {r.core: r.value for r in fake.state.cores} == {0: -5, 1: -4}


@pytest.mark.req("L12.floor")
@pytest.mark.parametrize(
    ("value", "fb", "frozen", "member"),
    [(-2, -9, (), True), (-3, -9, (), False), (-2, -3, (), False), (-2, -9, (0,), False)],
)
def test_a_round_takes_a_core_only_above_its_floor_short_of_its_fail_bound_and_not_frozen(value, fb, frozen, member):
    from corecycler.tuner.machine.types import MachineConfig, Mode
    from corecycler.tuner.orders import Method

    cfg = MachineConfig(Mode.CO, Method.ALTERNATE, (0,), max_offset=-10)
    core = CoreRecord(0, phase=Phase.PROVEN, value=value, fb=fb)
    cursor = AllCore(floors=((0, -3),), frozen=frozen)
    assert allcore.round_members((core,), cfg, cursor) == ((0,) if member else ())


@pytest.mark.req("D44", "L17.d.redirect")
@pytest.mark.parametrize("named", [(0,), ()])
def test_a_back_off_after_the_reclaim_sends_the_session_through_another_live1_pass(named):
    report = Report(Group.SILICON, 1.0, named=named)
    fake, stages = run([-4, -2], endurance=True, on_start=report_once(Stage.ENDURANCE, report))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = [stage for stage, _, _ in stages]
    assert max(i for i, stage in enumerate(order) if stage is Stage.LIVE1) > order.index(Stage.ENDURANCE)
