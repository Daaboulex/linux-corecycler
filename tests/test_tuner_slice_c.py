from __future__ import annotations

import json
from dataclasses import replace
from itertools import takewhile

import pytest

from corecycler.tuner.live import stored_from_json, stored_to_json
from corecycler.tuner.machine import allcore, bisect
from corecycler.tuner.machine.resume import resident
from corecycler.tuner.machine.types import (
    AllCore,
    ContinueOnSetup,
    CoreRecord,
    CrashEvidenceRead,
    FailContext,
    Incident,
    KernelLogRead,
    Lifecycle,
    Load,
    PauseReason,
    Phase,
    SessionState,
    Stage,
    ValidateProfile,
    Waiting,
    WriteCO,
)
from tests._tuner_world import Abort, Close, Evidence, Group, PauseRequest, Report, Resume, fresh, run_to_rest, start
from tests.test_tuner_validate import STOCK_RECORD, completed, values

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


@pytest.mark.req("ALLCORE.bisect.universe", "L09.resolve", "L09.retest.fail", "L10.d.apply")
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
    one_off = report_once(entry, Report(Group.SILICON, 1.0, context=context))
    exited = []

    def spy(fake, spec):
        one_off(fake, spec)
        flow = fake.state.flow
        if spec.stage is entry and flow.one_off is not None and not exited:
            exited.append((dict(spec.vector), flow.dirty, flow.allcore.frozen))

    fake, stages = run([-6, -6], on_start=spy, **cfg)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    failed = next(vector for stage, _, vector in stages if stage is entry)
    last = max(i for i, (stage, _, _) in enumerate(stages) if stage is Stage.BISECT)
    assert stages[last][1:] == (load, failed)
    assert stages[last + 1][0] is entry
    assert exited == [(failed, False, ())]


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


@pytest.mark.req("L09.retest.single", "L10.d.bound")
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
    assert (retest.core, retest.load, retest.seconds, retest.cpus) == (
        failed.core,
        failed.load,
        failed.seconds,
        failed.cpus,
    )
    assert (retest.quick, retest.plan_seed) == (failed.quick, failed.plan_seed)


PAUSED_HOT = FailContext(steady=False, working=(0, 1), paused=True)


@pytest.mark.req("ALLCORE.bisect.seed", "ALLCORE.bisect.replay", "K40.slot", "D40.slot")
@pytest.mark.parametrize(
    ("entry", "nth", "context", "quick"),
    [
        (Stage.TRANSIENT, 1, None, False),
        (Stage.TRANSIENT, 1, STEADY_CLASS, False),
        (Stage.STEADY, 1, STEADY_CLASS, True),
        (Stage.STEADY, 1, PAUSED_HOT, False),
        (Stage.ENDURANCE, 1, STEADY_CLASS, True),
        (Stage.ENDURANCE, 3, None, False),
    ],
)
def test_smaller_groups_replay_the_failed_test_unless_a_steady_class_hot_failure_runs_them_quick(
    entry, nth, context, quick
):
    specs = []
    failure = report_once(entry, Report(Group.SILICON, 1.0, context=context), nth=nth)

    def spy(fake, spec):
        specs.append(spec)
        failure(fake, spec)

    run(
        [-6, -6, -6],
        cores=3,
        on_start=spy,
        endurance=True,
        **{**LONG_HOT, **DISTINCT_LENGTHS, "endurance_window_s": 20.0},
    )
    failed = [spec for spec in specs if spec.stage is entry][nth - 1]
    groups = [spec for spec in specs if spec.stage is Stage.BISECT and spec.vector != failed.vector]
    shapes = {(spec.load, spec.seconds, spec.quick, spec.plan_seed) for spec in groups}
    if quick:
        assert {shape[:3] for shape in shapes} == {(Load.HOT, 1.0, True)}
    else:
        assert shapes == {(failed.load, failed.seconds, False, failed.plan_seed)}


ISSUE_20 = {**FAST, "max_offset": -20, "k_1t": 2, "transient_window_s": 1.0}


@pytest.mark.req("ALLCORE.failed_vector")
@pytest.mark.parametrize("cut", ["paused load", "lost verdict"])
def test_an_unnamed_steady_failure_its_replay_repeats_is_never_tested_or_completed_at_or_past_again(cut):
    fake = fresh(2, [-12, -6], 1, **ISSUE_20)
    specs = []
    check, verdict = fake.check_start, fake.verdict_for

    def recorded(state, spec):
        specs.append(spec)
        check(state, spec)

    fake.check_start = recorded
    start(fake)
    failed = until_stage(fake, Stage.STEADY)
    seed = fake.worker.spec.plan_seed

    def repeating(spec):
        if (dict(spec.vector), spec.plan_seed) == (failed, seed):
            return Report(Group.SILICON, spec.seconds, context=PAUSED_HOT)
        return verdict(spec)

    fake.verdict_for = repeating
    if cut == "lost verdict":
        fake.faults.fail_commits = 1
        fake.deliver()
        assert fake.state.pause.reason is PauseReason.DATABASE
        fake.crash()
        fake.post(Resume(1))
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    since = specs[next(i for i, spec in enumerate(specs) if spec.stage is Stage.STEADY) :]
    at_or_past = [spec for spec in since if all(dict(spec.vector)[core] <= v for core, v in failed.items())]
    assert [(spec.stage, spec.plan_seed) for spec in at_or_past] == [(Stage.STEADY, seed), (Stage.BISECT, seed)]
    assert {r.core: r.value for r in fake.state.cores} == {core: v + 1 for core, v in failed.items()}


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


@pytest.mark.req("L09.one_off.clear", "D118.again")
def test_a_new_live1_pass_starts_without_the_one_off_mark():
    fake, stages = run([-6, -6], on_start=scripted(Stage.STEADY, {1: UNNAMED_STEADY, 3: UNNAMED_STEADY}))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert full_set_slots(stages) == [{0: -3, 1: -3}, {0: -3, 1: -3}]
    assert [stage for stage, _, _ in stages].count(Stage.LIVE1) == 6
    assert (values(fake), fake.state.cleared_vectors) == ({0: -2, 1: -2}, ())


def started(fake):
    specs = []
    check = fake.check_start

    def recorded(state, spec):
        specs.append(spec)
        check(state, spec)

    fake.check_start = recorded
    return specs


def at_or_past(spec, failed):
    vector = dict(spec.vector)
    return all(vector[core] <= value for core, value in failed.items() if value != 0)


def fail_once(fake, failure):
    if failure == "verdict":
        fake.deliver(Report(Group.SILICON, 1.0))
        return
    if failure == "lost verdict":
        fake.faults.fail_commits = 1
        fake.deliver()
    fake.crash()
    fake.post(Resume(1))


@pytest.mark.req("L12.failed")
@pytest.mark.parametrize("entry", [Stage.STEADY, Stage.TRANSIENT])
@pytest.mark.parametrize("failure", ["verdict", "hang", "lost verdict"])
def test_the_reclaim_never_tests_at_or_past_a_vector_whose_one_failure_its_replay_did_not_repeat(entry, failure):
    fake = fresh(2, [-6, 0], 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    failed = until_stage(fake, entry)
    fail_once(fake, failure)
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert [dict(spec.vector) for spec in specs if spec.stage is Stage.BISECT].count(failed) == 1
    assert [dict(spec.vector) for spec in specs if spec.stage is Stage.TIGHTEN and at_or_past(spec, failed)] == []
    assert {r.core: r.value for r in fake.state.cores} == failed


def until_test(fake, stage, vector=None):
    while (
        fake.worker is None
        or fake.worker.spec.stage is not stage
        or vector not in (None, dict(fake.worker.spec.vector))
    ):
        assert fake.progress()
    return dict(fake.worker.spec.vector)


def steady_one_off(fake):
    failed = until_test(fake, Stage.STEADY)
    fake.deliver(Report(Group.SILICON, 1.0))
    return failed


def tighten_report(fake):
    failed = until_test(fake, Stage.TIGHTEN)
    fake.deliver(Report(Group.SILICON, 1.0))
    return failed


def tighten_dead_boot(fake):
    failed = until_test(fake, Stage.TIGHTEN, {0: -5, 1: -5})
    fake.crash()
    fake.post(Resume(1))
    return failed


def memory_report(fake):
    failed = until_test(fake, Stage.MEMORY)
    fake.deliver(Report(Group.SILICON, 1.0))
    return failed


def memory_dead_boot(fake):
    failed = until_test(fake, Stage.MEMORY)
    fake.crash()
    fake.post(Resume(1))
    return failed


def soak_environment(fake):
    failed = until_test(fake, Stage.SOAK)
    fake.deliver(Report(Group.ENVIRONMENT, 1.0))
    return failed


def bisect_report(fake):
    until_test(fake, Stage.TRANSIENT)
    fake.deliver(Report(Group.SILICON, 1.0))
    until_test(fake, Stage.BISECT, {0: -3, 1: -3, 2: 0, 3: 0})
    fake.deliver(Report(Group.SILICON, 1.0))
    failed = until_test(fake, Stage.BISECT, {0: 0, 1: 0, 2: -3, 3: -3})
    fake.deliver(Report(Group.SILICON, 1.0))
    return failed


PAUSE_IN = (Stage.STEADY, Stage.TRANSIENT, Stage.ENDURANCE, Stage.SOAK)


@pytest.mark.req("L12.failed")
@pytest.mark.parametrize(
    ("fail", "cores", "end"),
    [
        (steady_one_off, 2, {0: -3, 1: -3}),
        (tighten_report, 2, {0: -3, 1: -3}),
        (tighten_dead_boot, 2, {0: -4, 1: -4}),
        (memory_report, 2, {0: -5, 1: -5}),
        (memory_dead_boot, 2, {0: -5, 1: -5}),
        (soak_environment, 2, {0: -5, 1: -5}),
        (bisect_report, 4, {0: -2, 1: -2, 2: -2, 3: -2}),
    ],
    ids=lambda value: value.__name__ if callable(value) else None,
)
def test_every_kind_of_all_core_failure_bars_the_reclaim_of_a_session_continued_under_a_new_setup(fail, cores, end):
    fake = fresh(cores, [-6] * cores, 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    failed = fail(fake)
    while fake.worker is None or fake.worker.spec.stage not in PAUSE_IN or fake.state.flow.bisect is not None:
        assert fake.progress()
    fake.post(PauseRequest())
    fake.setup = (("bios", "B"),)
    fake.post(Resume(1))
    fake.post(ContinueOnSetup(2))
    continued = len(specs)
    reclaimed = until_test(fake, Stage.MEMORY)
    child = specs[continued:]
    assert fake.state.session_id == 2
    assert [spec.stage for spec in child][:2] == [Stage.STEADY, Stage.TRANSIENT]
    assert [dict(spec.vector) for spec in child if spec.stage is Stage.TIGHTEN and at_or_past(spec, failed)] == []
    assert reclaimed == end


@pytest.mark.req("L12.failed", "D118")
def test_a_quarantined_session_re_engaged_never_tightens_at_or_past_a_vector_it_remembers():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    failed = steady_one_off(fake)
    while fake.state.lifecycle is not Lifecycle.QUARANTINED:
        while (
            fake.worker is None
            or fake.worker.spec.stage is not Stage.BISECT
            or 0 in dict(fake.worker.spec.vector).values()
        ):
            assert fake.progress()
        fake.crash()
        fake.post(Resume(1))
    fake.post(Resume(1, reengage=True))
    reengaged = len(specs)
    run_to_rest(fake, 50_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.TRANSIENT in {spec.stage for spec in specs[reengaged:]}
    assert [
        dict(spec.vector) for spec in specs[reengaged:] if spec.stage is Stage.TIGHTEN and at_or_past(spec, failed)
    ] == []
    remembered = [dict(vector) for vector in fake.state.failed_vectors]
    assert remembered == [failed, stepped(failed, (0, 1)), stepped(stepped(failed, (0, 1)), (0, 1))]
    assert (values(fake), fake.state.cleared_vectors) == ({0: 0, 1: 0}, ())
    assert len([line for line in fake.log if line.startswith("loosened: ")]) == 1


@pytest.mark.req("L12.failed", "D118.cleared")
def test_a_validation_keeps_the_failed_vectors_its_session_remembers_and_the_one_off_it_cleared():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    failed = steady_one_off(fake)
    run_to_rest(fake, 20_000)
    remembered = fake.db.failed_vectors
    assert (fake.db.status, remembered, fake.db.cleared_vectors) == ("completed", (tuple(failed.items()),), remembered)
    fake.post(ValidateProfile(1))
    assert (fake.state.failed_vectors, fake.state.cleared_vectors) == (remembered, remembered)
    run_to_rest(fake, 20_000)
    assert (fake.db.status, fake.db.failed_vectors, fake.db.cleared_vectors) == ("completed", remembered, remembered)
    assert values(fake) == failed


@pytest.mark.req("L12.failed")
def test_a_vector_that_fails_again_is_remembered_once():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    failed = until_stage(fake, Stage.STEADY)
    fake.deliver(UNNAMED_STEADY)
    assert until_stage(fake, Stage.STEADY) == failed
    fake.deliver(UNNAMED_STEADY)
    assert fake.db.failed_vectors == (tuple(failed.items()),)


@pytest.mark.req("L12.failed")
def test_an_all_core_failure_with_every_core_at_zero_bars_no_later_round():
    fake, stages = run([-3, -3], on_start=report_once(Stage.STEADY, Report(Group.SILICON, 1.0)))
    assert (fake.state.lifecycle, fake.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.STOCK_UNSTABLE)
    fake.post(Resume(1))
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    tighten = [vector for stage, _, vector in stages if stage is Stage.TIGHTEN]
    assert tighten[::5] == [{0: -1, 1: -1}, {0: -2, 1: -2}, {0: -3, 1: -3}]


@pytest.mark.req("L12.failed")
def test_a_dead_boot_in_a_one_core_test_bars_no_later_round():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    until_stage(fake, Stage.LIVE1)
    fake.crash()
    fake.post(Resume(1))
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    tighten = [dict(spec.vector) for spec in specs if spec.stage is Stage.TIGHTEN]
    assert tighten[::5] == [{0: -4, 1: -4}, {0: -5, 1: -5}, {0: -6, 1: -6}]


def failing_once(fake, *vectors):
    owed = [dict(vector) for vector in vectors]
    verdict = fake.verdict_for

    def failing(spec):
        vector = dict(spec.vector)
        if vector in owed:
            owed.remove(vector)
            return Report(Group.SILICON, spec.seconds)
        return verdict(spec)

    fake.verdict_for = failing


def slot(held, *groups):
    return {core: value if any(core in group for group in groups) else 0 for core, value in held.items()}


def stepped(held, cores):
    return {core: value + (core in cores) for core, value in held.items()}


def bisect_slots(specs):
    return [dict(spec.vector) for spec in specs if spec.stage is Stage.BISECT]


@pytest.mark.req("D116", "SOAK.bisect", "L09.resolve", "Q6")
def test_a_soak_crash_whose_two_halves_each_crash_loosens_both_before_its_bisection_exits():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    held = until_test(fake, Stage.SOAK)
    fake.crash()
    fake.post(Resume(1))
    assert fake.state.flow.bisect.entry is Stage.SOAK
    until_test(fake, Stage.BISECT, slot(held))
    fake.deliver()
    for half in ((0, 1), (2, 3)):
        until_test(fake, Stage.BISECT, slot(held, half))
        fake.crash()
        fake.post(Resume(1))
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    loosened = stepped(held, (0, 1, 2, 3))
    assert bisect_slots(specs) == [
        slot(held),
        slot(held, (0, 1)),
        slot(held, (2, 3)),
        *(slot(held, (core,)) for core in held),
        slot(loosened, (0, 1)),
        slot(loosened, (2, 3)),
    ]
    assert {r.core: r.value for r in fake.state.cores} == loosened
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == loosened


@pytest.mark.req("D116", "ALLCORE.bisect.single", "ALLCORE.bisect.group", "L09.G2.single", "L09.resolve")
def test_a_bisection_that_finds_a_core_and_a_pair_backs_the_core_off_and_loosens_the_pair_before_it_exits():
    fake = fresh(3, [-6] * 3, 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    held = until_test(fake, Stage.TRANSIENT)
    failing_once(fake, held, slot(held, (0, 1)), slot(held, (2,)))
    while fake.state.flow.stage is not Stage.BISECT:
        assert fake.progress()
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    loosened = stepped(held, (0, 1, 2))
    assert bisect_slots(specs) == [
        slot(held),
        slot(held, (0, 1)),
        slot(held, (2,)),
        slot(held, (0,)),
        slot(held, (1,)),
        slot(loosened, (0, 1)),
    ]
    assert {r.core: r.value for r in fake.state.cores} == loosened
    assert (fake.state.core(2).fb, fake.state.flow.stage, fake.state.flow.allcore.frozen) == (
        held[2],
        Stage.TRANSIENT,
        (0, 1),
    )
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == loosened


@pytest.mark.req("D116", "L09.G2.single")
def test_a_bisection_that_finds_a_core_in_each_half_backs_both_off_at_once_and_skips_the_rest_of_its_queue():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    held = until_test(fake, Stage.TRANSIENT)
    failing_once(fake, held, slot(held, (0, 1)), slot(held, (2, 3)), slot(held, (0,)), slot(held, (2,)))
    while fake.state.flow.stage is not Stage.BISECT:
        assert fake.progress()
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    assert bisect_slots(specs) == [
        slot(held),
        slot(held, (0, 1)),
        slot(held, (2, 3)),
        slot(held, (0,)),
        slot(held, (1,)),
        slot(held, (2,)),
    ]
    assert {r.core: r.value for r in fake.state.cores} == stepped(held, (0, 2))
    assert (fake.state.core(0).fb, fake.state.core(2).fb) == (held[0], held[2])
    assert (fake.state.flow.stage, fake.state.flow.allcore.frozen) == (Stage.TRANSIENT, ())


def bisecting_from(fake, entry):
    if entry is Stage.TRANSIENT:
        held = until_test(fake, Stage.TRANSIENT)
        failing_once(fake, held)
        return held
    until_test(fake, Stage.LIVE1)
    fake.crash()
    fake.post(Resume(1))
    return dict(fake.state.flow.bisect.values)


@pytest.mark.req("D116", "L09.G2.single", "L09.G3")
@pytest.mark.parametrize("entry", [Stage.TRANSIENT, Stage.LIVE1])
@pytest.mark.parametrize("ending", ["crash", "record"])
def test_a_core_found_alone_keeps_its_back_off_when_a_later_slot_ends_the_bisection(entry, ending):
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = bisecting_from(fake, entry)
    failing_once(fake, slot(held, (0, 1)), slot(held, (2, 3)), slot(held, (0,)))
    until_test(fake, Stage.BISECT, slot(held, (2,)))
    if ending == "crash":
        fake.crash()
        fake.post(Resume(1))
    else:
        fake.deliver(Report(Group.SILICON, 1.0, evidence=(Evidence(2, False),)))
    assert (fake.state.flow.stage, fake.state.flow.bisect) == (entry, None)
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (held[0] + 1, held[0])


def co_writes(fake):
    writes = []
    apply = fake.apply

    def recorded(state, effect):
        if isinstance(effect, WriteCO):
            writes.append((effect.core, effect.value))
        return apply(state, effect)

    fake.apply = recorded
    return writes


def residents(fake):
    return {r.core: resident(fake.state, r.core) for r in fake.state.cores}


@pytest.mark.req("L18.d.4.3b.between")
def test_a_live1_begun_bisection_writes_back_what_each_slot_moved_and_every_wait_holds_what_resident_reads():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    writes = co_writes(fake)
    waits = []

    def reported():
        writes.clear()
        fake.deliver()
        waits.append((fake.state.flow.stage, tuple(writes), dict(fake.co), residents(fake)))

    held = until_test(fake, Stage.LIVE1)
    reported()
    until_test(fake, Stage.LIVE1)
    fake.crash()
    fake.post(Resume(1))
    for group in ((), (0, 1)):
        until_test(fake, Stage.BISECT, slot(held, group))
        reported()
    baseline = slot(held)
    assert waits == [
        (Stage.LIVE1, (), held, held),
        (Stage.BISECT, (), baseline, baseline),
        (Stage.BISECT, ((0, 0), (1, 0)), baseline, baseline),
    ]


@pytest.mark.req("L18.d.4.3b.between", "L18.d.4.3h")
def test_a_dead_boot_after_a_live1_begun_stock_slot_moves_no_core_and_one_in_a_later_slot_convicts_its_group_s_core():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = bisecting_from(fake, Stage.LIVE1)
    until_test(fake, Stage.BISECT, slot(held))
    fake.deliver()
    waiting = fake.state
    fake.crash((Evidence(1, False),))
    fake.post(Resume(1))
    after = fake.state
    assert (after.lifecycle, after.cores, after.flow.bisect, after.incidents) == (
        Lifecycle.RUNNING,
        waiting.cores,
        waiting.flow.bisect,
        (*waiting.incidents, Incident.CRASH),
    )
    until_test(fake, Stage.BISECT, slot(held, (2, 3)))
    assert fake.worker.spec.core not in (2, 3)
    fake.crash((Evidence(2, False),))
    fake.post(Resume(1))
    s = fake.state
    assert (s.core(2).fb, s.core(2).crash_count, s.flow.stage) == (held[2], 1, Stage.LIVE1)


@pytest.mark.parametrize("stop", [Group.THERMAL, Group.APPARATUS])
@pytest.mark.req("L20.d.bisect", "L22.d.bisect")
def test_a_stop_without_a_verdict_in_a_live1_begun_slot_waits_at_the_baseline_and_retries_the_same_test(stop):
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = bisecting_from(fake, Stage.LIVE1)
    until_test(fake, Stage.BISECT, slot(held, (2, 3)))
    spec = fake.worker.spec
    assert spec.core not in (2, 3)
    fake.deliver(Report(stop, 1.0, detail="injected"))
    assert (dict(fake.co), residents(fake)) == (slot(held), slot(held))
    while fake.worker is None:
        assert fake.progress()
    assert fake.worker.spec == spec


@pytest.mark.req("D116.stored", "L09.c")
def test_a_bisection_closed_in_its_first_group_s_q6_replay_reads_back_and_loosens_the_second_group_after_resume():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    specs = started(fake)
    start(fake)
    held = until_test(fake, Stage.TRANSIENT)
    failing_once(fake, held, slot(held, (0, 1)), slot(held, (2, 3)))
    first = stepped(held, (0, 1))
    until_test(fake, Stage.BISECT, slot(first, (0, 1)))
    fake.post(Close())
    stored = fake.db
    assert (stored.status, stored.flow.bisect.rerun, {r.core: r.value for r in stored.cores}) == (
        "paused",
        (0, 1),
        first,
    )
    read = stored_from_json(json.loads(json.dumps(stored_to_json(stored))), continued=False)
    assert read == stored
    fake.db = read
    closed = len(specs)
    fake.post(Resume(1))
    while fake.state.flow is None or fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    loosened = stepped(held, (0, 1, 2, 3))
    assert bisect_slots(specs[closed:]) == [slot(first, (0, 1)), slot(loosened, (2, 3))]
    assert {r.core: r.value for r in fake.state.cores} == loosened
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


def finalizing(edges):
    fake = fresh(len(edges), edges, 2, max_offset=-8, **FAST)
    start(fake)
    while fake.worker is not None or fake.state.flow.stage is not Stage.FINALIZE:
        assert fake.progress()
    return fake


def remembering(fake, *vectors):
    fake.state = replace(fake.state, failed_vectors=tuple(tuple(vector.items()) for vector in vectors))


@pytest.mark.req("D118")
def test_held_values_strictly_past_a_remembered_vector_loosen_the_cores_it_moved_until_none_is_at_or_past_it():
    fake = finalizing([-6, -6])
    held = values(fake)
    bounds = [r.fb for r in fake.state.cores]
    specs = started(fake)
    remembering(fake, {0: held[0] + 1, 1: 0})
    run_to_rest(fake, 20_000)
    loosened = {0: held[0] + 2, 1: held[1]}
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (values(fake), [r.fb for r in fake.state.cores]) == (loosened, bounds)
    assert [(spec.stage, dict(spec.vector)) for spec in specs[:1]] == [(Stage.LIVE1, loosened)]


@pytest.mark.req("D118.cleared")
def test_a_one_off_closed_before_its_re_test_and_again_after_it_completes_at_its_values():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    failed = steady_one_off(fake)
    until_test(fake, Stage.BISECT, failed)
    for _ in range(2):
        fake.post(Close())
        fake.post(Resume(1))
        while fake.state.flow.stage is Stage.BISECT:
            assert fake.progress()
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (values(fake), fake.state.cleared_vectors) == (failed, (tuple(failed.items()),))


@pytest.mark.req("D118")
def test_held_values_at_or_past_a_remembered_vector_on_only_some_cores_it_moved_complete_unmoved():
    fake = finalizing([-6, -6])
    held = values(fake)
    remembering(fake, {0: held[0], 1: held[1] - 1})
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake) == held


def pair_failed(fake, held):
    failing_once(fake, slot(held, (0, 1)))
    until_test(fake, Stage.BISECT, slot(held, (0, 1)))
    fake.deliver()


def crashed_alone(fake, held):
    pair_failed(fake, held)
    until_test(fake, Stage.BISECT, slot(held, (2,)))
    fake.crash()
    fake.post(Resume(1))


def recorded_alone(fake, held):
    pair_failed(fake, held)
    until_test(fake, Stage.BISECT, slot(held, (2,)))
    assert fake.worker.spec.core != 2
    fake.deliver(Report(Group.SILICON, 1.0, evidence=(Evidence(2, False),)))


def recorded_between(fake, held):
    pair_failed(fake, held)
    fake.post(KernelLogRead(fake.state.slot.token, (Evidence(2, False, held[2]),)))


def recorded_in_a_replay(fake, held):
    failing_once(fake, slot(held, (0, 1)), slot(held, (2, 3)))
    until_test(fake, Stage.BISECT, slot(stepped(held, (0, 1)), (0, 1)))
    core = next(core for core in (0, 1) if core != fake.worker.spec.core)
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(core, False),)))


def recorded_at_the_re_test(fake, held):
    while fake.worker is not None or fake.state.flow.bisect is None or not bisect.retesting(fake.state.flow.bisect):
        assert fake.progress()
    fake.post(KernelLogRead(fake.state.slot.token, (Evidence(2, False),)))


EARLY_EXITS = {
    "a crash in a one-core slot": (crashed_alone, 3, (0, 1), (0, 1)),
    "a record of a core in its own slot": (recorded_alone, 3, (0, 1), (0, 1)),
    "a record between slots": (recorded_between, 3, (0, 1), (0, 1)),
    "a record in a Q6 replay": (recorded_in_a_replay, 4, (2, 3), (2, 3)),
    "a record while the full-set re-test waits": (recorded_at_the_re_test, 3, (0, 1, 2), (0, 1)),
}


@pytest.mark.req("D118", "D116", "L09.G3", "L09.G4")
@pytest.mark.parametrize("entry", [Stage.TRANSIENT, Stage.LIVE1])
@pytest.mark.parametrize("ending", list(EARLY_EXITS))
def test_a_bisection_an_early_exit_ends_never_completes_at_or_past_a_group_it_left_failed(entry, ending):
    exits, cores, group, unmoved = EARLY_EXITS[ending]
    fake = fresh(cores, [-6] * cores, 2, max_offset=-8, **FAST)
    start(fake)
    held = bisecting_from(fake, entry)
    exits(fake, held)
    assert (fake.state.flow.stage, fake.state.flow.bisect) == (entry, None)
    assert {core: values(fake)[core] for core in unmoved} == {core: held[core] for core in unmoved}
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert not all(values(fake)[core] <= held[core] for core in group)


@pytest.mark.req("D118.live1", "L12.failed")
@pytest.mark.parametrize("origin", ["a one-core load's failure", "a dead boot"])
def test_a_live1_begun_bisection_remembers_the_vector_of_a_slot_of_several_cores_that_failed(origin):
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = bisecting_from(fake, Stage.LIVE1)
    assert fake.state.failed_vectors == ()
    until_test(fake, Stage.BISECT, slot(held, (2, 3)))
    assert fake.worker.spec.load is Load.ONE_T
    if origin == "a dead boot":
        fake.crash()
        fake.post(Resume(1))
    else:
        fake.deliver(Report(Group.SILICON, 1.0))
    assert fake.state.failed_vectors == (tuple(slot(held, (2, 3)).items()),)
    assert fake.state.flow.bisect.results[-1] == ((2, 3), False)


@pytest.mark.req("D118.again")
def test_a_cleared_one_off_that_fails_again_in_its_validation_is_never_completed_at_again():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    failed = steady_one_off(fake)
    run_to_rest(fake, 20_000)
    assert values(fake) == failed
    fake.post(ValidateProfile(1))
    assert until_test(fake, Stage.MEMORY) == failed
    fake.deliver(Report(Group.SILICON, 1.0))
    while fake.state.flow.stage is not Stage.MEMORY:
        assert fake.progress()
    assert fake.state.flow.one_off == (Stage.MEMORY, tuple(failed.items()))
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake) == stepped(failed, (0, 1))
    assert fake.state.cleared_vectors == ()


@pytest.mark.req("D118.again")
def test_a_re_test_that_names_a_core_not_owned_before_it_passes_clears_nothing():
    fake = fresh(3, [-6, 0, -6], 2, max_offset=-8, selected=(0, 2), **FAST)
    start(fake)
    failed = steady_one_off(fake)
    until_test(fake, Stage.BISECT, failed)
    fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
    assert fake.state.pause.reason is PauseReason.HELD_KNOB
    fake.post(Resume(1))
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    assert fake.state.flow.one_off == (Stage.STEADY, tuple(failed.items()))
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake) == stepped(failed, (0, 2))
    assert fake.state.cleared_vectors == ()


@pytest.mark.req("D118.cleared")
def test_a_failure_whose_bisection_a_continue_carried_is_cleared_by_no_re_test_after_it():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    failed = until_test(fake, Stage.MEMORY)
    fake.deliver(Report(Group.SILICON, 1.0))
    until_test(fake, Stage.BISECT)
    fake.post(PauseRequest())
    fake.setup = (("bios", "B"),)
    fake.post(Resume(1))
    fake.post(ContinueOnSetup(2))
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    assert fake.state.flow.one_off == (Stage.MEMORY, tuple(failed.items()))
    run_to_rest(fake, 20_000)
    assert (fake.state.lifecycle, fake.state.session_id) == (Lifecycle.COMPLETED, 2)
    assert values(fake) == stepped(failed, (0, 1))
    assert fake.state.cleared_vectors == ()


@pytest.mark.req("D118.cleared")
def test_a_live1_begun_re_test_at_the_values_of_a_memory_failure_clears_nothing():
    fake = fresh(2, [-6, 0], 2, max_offset=-8, **FAST)
    start(fake)
    failed = until_test(fake, Stage.MEMORY)
    assert failed[1] == 0
    fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
    until_test(fake, Stage.LIVE1)
    fake.crash()
    fake.post(Resume(1))
    assert (fake.state.flow.bisect.entry, values(fake)) == (Stage.LIVE1, failed)
    while fake.state.flow.stage is Stage.BISECT:
        assert fake.progress()
    assert fake.state.flow.one_off == (Stage.LIVE1, tuple(failed.items()))
    assert (fake.state.failed_vectors, fake.state.cleared_vectors) == ((tuple(failed.items()),), ())


@pytest.mark.req("D118.abort", "L30.e.failed")
def test_an_abort_of_a_validation_at_values_a_remembered_failure_bars_ends_aborted_and_restores_no_value():
    fake = completed()
    before = values(fake)
    fake.db = replace(fake.db, failed_vectors=(*fake.db.failed_vectors, tuple(before.items())))
    fake.failed_allcore.add((tuple(before.items()),))
    fake.post(ValidateProfile(1))
    until_stage(fake, Stage.LIVE1)
    assert values(fake) == before
    fake.post(Abort())
    assert (fake.state.lifecycle, fake.db.status) == (Lifecycle.ABORTED, "aborted")
    assert {core: fake.co[core] for core, _ in fake.state.baseline} == dict(fake.state.baseline)
    assert "not restored" in fake.log[-1]


def replay(spec) -> tuple:
    return (spec.vector, spec.load, spec.seconds, spec.plan_seed)


def endurance_session(edges, owned=None):
    selected = {} if owned is None else {"selected": owned}
    fake = fresh(
        len(edges), list(edges), 2, max_offset=-5, k_1t=1, k_2t=1, wait_between_cores_s=0.0, endurance=True, **selected
    )
    start(fake)
    until_stage(fake, Stage.ENDURANCE)
    return fake


def stock_record_then_its_replay(fake):
    failed = fake.worker.spec
    fake.deliver(Report(Group.SILICON, 1.0, evidence=(Evidence(2, True),)))
    until_stage(fake, Stage.ENDURANCE)
    assert replay(fake.worker.spec) == replay(failed)
    fake.deliver()
    return dict(failed.vector)


def unowned_named_then_its_replay(fake):
    failed = fake.worker.spec
    fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
    assert fake.state.pause.reason is PauseReason.HELD_KNOB
    fake.post(Resume(1))
    until_stage(fake, Stage.ENDURANCE)
    assert replay(fake.worker.spec) == replay(failed)
    fake.deliver()
    return dict(failed.vector)


@pytest.mark.req("D118.cleared")
@pytest.mark.parametrize(
    ("edges", "owned", "failing"),
    [
        pytest.param((-3, -2, 0), None, stock_record_then_its_replay, id="a record at stock"),
        pytest.param((-3, 0, -2), (0, 2), unowned_named_then_its_replay, id="a core not owned named"),
    ],
)
def test_a_single_failure_whose_exact_replay_passed_outside_a_bisection_completes_at_its_values(edges, owned, failing):
    fake = endurance_session(edges, owned)
    failed = failing(fake)
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake) == failed
    assert not [line for line in fake.log if line.startswith("loosened: ")]


def validating_a_record_at_stock_replayed():
    fake = completed((-3, -2, 0))
    before = values(fake)
    fake.post(ValidateProfile(1))
    until_stage(fake, Stage.ENDURANCE)
    STOCK_RECORD(fake)
    until_stage(fake, Stage.ENDURANCE)
    fake.deliver()
    return fake, before


def validating_a_memory_failure_at_a_core_not_owned_skipped_then_clean():
    fake = completed((-3, 0, -2), (0, 2))
    before = values(fake)
    fake.post(ValidateProfile(1))
    until_stage(fake, Stage.MEMORY)
    fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
    fake.memory_skip = "no binary"
    fake.post(Resume(1))
    until_stage(fake, Stage.MEMORY)
    fake.deliver()
    fake.memory_skip = ""
    fake.post(Resume(1))
    until_stage(fake, Stage.MEMORY)
    fake.deliver()
    return fake, before


def validating_a_failure_at_a_core_not_owned_twice_then_clean():
    fake = completed((-3, 0, -2), (0, 2))
    before = values(fake)
    fake.post(ValidateProfile(1))
    for _ in range(2):
        until_stage(fake, Stage.ENDURANCE)
        fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
        fake.post(Resume(1))
    until_stage(fake, Stage.ENDURANCE)
    fake.deliver()
    return fake, before


def ended(fake, end):
    if end == "abort":
        fake.post(Abort())
    else:
        run_to_rest(fake, 4000)
    return fake.state.lifecycle, values(fake)


@pytest.mark.req("D118.cleared", "D118.abort", "L30.e.failed")
@pytest.mark.parametrize("end", ["abort", "run on"])
@pytest.mark.parametrize(
    "validating",
    [validating_a_record_at_stock_replayed, validating_a_memory_failure_at_a_core_not_owned_skipped_then_clean],
)
def test_a_validation_whose_single_failure_an_exact_replay_cleared_keeps_its_values_whether_aborted_or_run_on(
    validating, end
):
    fake, before = validating()
    assert ended(fake, end) == (Lifecycle.COMPLETED, before)
    assert (fake.db.status, {core: fake.co[core] for core in before}) == ("completed", before)


@pytest.mark.req("D118.again", "D118.abort")
@pytest.mark.parametrize("end", ["abort", "run on"])
def test_a_validation_whose_failure_repeated_at_its_values_is_no_one_off_and_never_completes_at_them(end):
    fake, before = validating_a_failure_at_a_core_not_owned_twice_then_clean()
    looser = {core: value + 1 for core, value in before.items()}
    expected = (Lifecycle.ABORTED, before) if end == "abort" else (Lifecycle.COMPLETED, looser)
    assert ended(fake, end) == expected
    assert fake.state.cleared_vectors == ()


@pytest.mark.req("D118.cleared", "D116")
def test_a_stage_test_at_the_vector_of_a_bisection_slot_that_failed_clears_nothing():
    fake = fresh(3, [-6] * 3, 2, max_offset=-8, **FAST)
    start(fake)
    held = bisecting_from(fake, Stage.TRANSIENT)
    crashed_alone(fake, held)
    pair = slot(held, (0, 1))
    assert values(fake) == pair
    until_test(fake, Stage.TRANSIENT, pair)
    fake.deliver()
    assert fake.last_report.group is Group.PASS
    assert (tuple(pair.items()) in fake.state.failed_vectors, fake.state.cleared_vectors) == (True, ())
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake) == stepped(pair, (0, 1))


@pytest.mark.req("D118")
def test_the_first_barring_vector_in_stored_order_loosens_first():
    fake = finalizing([-6, -6])
    held = values(fake)
    remembering(fake, {0: held[0], 1: 0}, {0: held[0] + 1, 1: held[1]})
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert values(fake) == {0: held[0] + 2, 1: held[1] + 1}


@pytest.mark.req("D118")
def test_a_dirty_finalize_whose_values_a_remembered_vector_bars_loosens_them_before_its_one_pass():
    fake = finalizing([-6, -6])
    held = values(fake)
    specs = started(fake)
    remembering(fake, held)
    fake.state = replace(fake.state, flow=replace(fake.state.flow, dirty=True))
    run_to_rest(fake, 20_000)
    loosened = {core: value + 1 for core, value in held.items()}
    assert (fake.state.lifecycle, values(fake)) == (Lifecycle.COMPLETED, loosened)
    assert [dict(spec.vector) for spec in specs if spec.stage is Stage.LIVE1] == [loosened] * len(held)


@pytest.mark.req("D118.cleared")
def test_a_validation_continued_on_a_new_setup_keeps_the_one_off_its_session_cleared():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    failed = steady_one_off(fake)
    run_to_rest(fake, 20_000)
    assert values(fake) == failed
    fake.setup = (("bios", "B"),)
    fake.post(ValidateProfile(1))
    assert fake.state.pause.reason is PauseReason.SETUP_CHANGED
    fake.post(ContinueOnSetup(2))
    run_to_rest(fake, 20_000)
    assert (fake.state.lifecycle, fake.state.session_id, values(fake)) == (Lifecycle.COMPLETED, 2, failed)
    assert fake.state.cleared_vectors == (tuple(failed.items()),)


@pytest.mark.req("D118.cleared")
def test_the_latest_failure_s_exact_replay_clears_it_though_an_earlier_failure_was_never_cleared():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    tightened = tighten_report(fake)
    held = until_test(fake, Stage.MEMORY)
    fake.deliver(Report(Group.SILICON, 1.0))
    run_to_rest(fake, 20_000)
    assert (fake.state.lifecycle, values(fake)) == (Lifecycle.COMPLETED, held)
    assert fake.state.failed_vectors == (tuple(tightened.items()), tuple(held.items()))
    assert fake.state.cleared_vectors == (tuple(held.items()),)


def convicted_in_turn(fake, *stages):
    for core, stage in enumerate(stages):
        until_test(fake, stage)
        fake.crash((Evidence(core, False),))
        fake.post(Resume(1))
    stored = fake.db
    fake.state = SessionState(next_token=fake.state.next_token)
    fake.post(Resume(1, reengage=True))
    return stored


@pytest.mark.req("D118.cleared", "L12.failed")
def test_a_re_engage_leaves_no_failure_from_before_its_quarantine_clearable():
    fake = fresh(3, [-8] * 3, 2, max_offset=-10, **FAST)
    start(fake)
    failed = until_test(fake, Stage.STEADY)
    fake.deliver(UNNAMED_STEADY)
    until_test(fake, Stage.BISECT, slot(failed, (0, 1)))
    stored = convicted_in_turn(fake, Stage.BISECT, Stage.STEADY, Stage.STEADY)
    assert (stored.status, stored.failed_vectors, stored.clearable_vector) == (
        "quarantined",
        (tuple(failed.items()),),
        tuple(failed.items()),
    )
    assert (fake.state.failed_vectors, fake.state.clearable_vector) == (stored.failed_vectors, None)


@pytest.mark.req("D118.cleared", "L12.failed")
def test_a_re_engage_keeps_the_one_off_its_session_cleared():
    fake = fresh(3, [-8] * 3, 2, max_offset=-10, **FAST)
    start(fake)
    failed = steady_one_off(fake)
    stored = convicted_in_turn(fake, Stage.STEADY, Stage.STEADY, Stage.STEADY)
    assert (stored.status, stored.cleared_vectors) == ("quarantined", (tuple(failed.items()),))
    assert fake.state.cleared_vectors == stored.cleared_vectors


@pytest.mark.req("D118.abort", "L30.e")
def test_an_abort_of_a_validation_whose_values_no_remembered_failure_bars_restores_it_completed():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, **FAST)
    start(fake)
    failed = tighten_report(fake)
    run_to_rest(fake, 20_000)
    before = values(fake)
    assert (fake.db.failed_vectors, fake.db.clearable_vector) == ((tuple(failed.items()),), tuple(failed.items()))
    assert not all(before[core] <= value for core, value in failed.items())
    fake.post(ValidateProfile(1))
    assert fake.state.clearable_vector is None
    until_stage(fake, Stage.LIVE1)
    fake.post(Abort())
    assert (fake.state.lifecycle, fake.db.status) == (Lifecycle.COMPLETED, "completed")
    assert {core: fake.co[core] for core in before} == before


@pytest.mark.req("L09.seed.distinct")
def test_every_planned_stage_test_of_a_run_carries_its_own_plan_seed():
    specs = []
    fake, _ = run([-6, -3], memory_edges={0: -4, 1: -3}, endurance=True, on_start=lambda fake, spec: specs.append(spec))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    planned = [spec for spec in specs if spec.plan_seed is not None and spec.stage is not Stage.BISECT]
    assert [spec.stage for spec in planned].count(Stage.ENDURANCE) > 5
    assert len({spec.plan_seed for spec in planned}) == len(planned)


def at_the_re_test(entry, context, edges=(-12, -12, -12, -12)):
    fake = fresh(len(edges), list(edges), 2, max_offset=-20, **FAST)
    start(fake)
    held = until_stage(fake, entry)
    fake.deliver(Report(Group.SILICON, 1.0, context=context))
    while fake.worker is None or dict(fake.worker.spec.vector) != held:
        assert fake.progress()
    assert fake.worker.spec.stage is Stage.BISECT
    return fake, held, {r.core: r.fb for r in fake.state.cores}


def replaying(fake, held, moved):
    flow = fake.state.flow
    universe = tuple(sorted(held))
    assert (fake.state.lifecycle, flow.stage, flow.bisect.universe, flow.bisect.rerun) == (
        Lifecycle.RUNNING,
        Stage.BISECT,
        universe,
        universe,
    )
    assert set(flow.allcore.frozen) >= set(universe)
    while fake.worker is None:
        assert fake.progress()
    assert (fake.worker.spec.stage, dict(fake.worker.spec.vector)) == (Stage.BISECT, moved)


ENTRIES = [(Stage.STEADY, STEADY_CLASS), (Stage.TRANSIENT, None), (Stage.MEMORY, None)]


def records_at_resume(fake, *records):
    read = fake.apply

    def once(state, effect):
        result = read(state, effect)
        if isinstance(result, CrashEvidenceRead):
            fake.apply = read
            return replace(result, evidence=records)
        return result

    fake.apply = once


def moved_once(held, steps):
    return {core: value + (steps if core == 2 else 1) for core, value in held.items()}


def judged_once(fake, held, bounds, steps):
    moved = moved_once(held, steps)
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {
        core: (value, held[2] if core == 2 else bounds[core]) for core, value in moved.items()
    }
    return moved


@pytest.mark.req("D102", "L09.retest.named", "L09.retest.fail")
@pytest.mark.parametrize(("entry", "context"), ENTRIES)
def test_a_re_test_that_fails_naming_a_core_loosens_every_core_of_the_set_one_step_and_replays_it(entry, context):
    fake, held, bounds = at_the_re_test(entry, context)
    fake.deliver(Report(Group.SILICON, 1.0, context=context, named=(2,)))
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {
        core: (value + 1, bounds[core]) for core, value in held.items()
    }
    replaying(fake, held, {core: value + 1 for core, value in held.items()})


@pytest.mark.req("D102", "D32", "L09.retest.named")
def test_the_set_replays_after_each_named_failure_until_it_passes_and_only_then_returns_to_the_stage():
    fake, held, _ = at_the_re_test(Stage.TRANSIENT, None)
    replays = []
    for named in ((2,), (1,)):
        replays.append(dict(fake.worker.spec.vector))
        fake.deliver(Report(Group.SILICON, 1.0, named=named))
        while fake.worker is None:
            assert fake.progress()
    replays.append(dict(fake.worker.spec.vector))
    fake.deliver(Report(Group.PASS, 1.0))
    assert replays == [{core: value + step for core, value in held.items()} for step in range(3)]
    assert (fake.state.flow.stage, fake.state.flow.bisect) == (Stage.TRANSIENT, None)
    assert {r.core: r.value for r in fake.state.cores} == {core: value + 2 for core, value in held.items()}


@pytest.mark.req("D102", "L09.G3", "G3.charged")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_at_a_tuned_value_in_the_re_test_loosens_the_set_and_moves_its_core_once_to_the_looser(
    corrected, steps
):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(2, corrected),)))
    replaying(fake, held, judged_once(fake, held, bounds, steps))


@pytest.mark.req("D102", "L09.G3")
def test_a_record_at_stock_in_the_re_test_counts_an_incident_and_runs_the_re_test_again_with_nothing_moved():
    fake, held, _ = at_the_re_test(Stage.TRANSIENT, None, edges=(-12, -12, -12, 0))
    cores = fake.state.cores
    assert held[3] == 0
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(3, True),)))
    while fake.worker is None:
        assert fake.progress()
    assert (fake.state.incidents, fake.state.cores, fake.state.flow.bisect.rerun) == (
        (Incident.STOCK_MACHINE_CHECK,),
        cores,
        None,
    )
    assert (fake.worker.spec.stage, dict(fake.worker.spec.vector)) == (Stage.BISECT, held)


@pytest.mark.req("D102", "L18.d.4.charged", "L18.d.4.3b", "L09.retest.hang")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_crash_in_the_re_test_with_a_record_loosens_the_set_and_moves_the_record_s_core_once_to_the_looser(
    corrected, steps
):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.crash((Evidence(2, corrected),))
    fake.post(Resume(1))
    moved = judged_once(fake, held, bounds, steps)
    assert [r.crash_count for r in fake.state.cores] == [0] * len(held)
    replaying(fake, held, moved)


@pytest.mark.req("D102", "ALLCORE.bisect.single")
def test_a_named_failure_in_a_smaller_group_still_finds_that_core_alone():
    fake = fresh(4, [-12] * 4, 2, max_offset=-20, **FAST)
    start(fake)
    held = until_stage(fake, Stage.TRANSIENT)
    fake.deliver(Report(Group.SILICON, 1.0))
    bounds = {r.core: r.fb for r in fake.state.cores}
    fake.names_cores = True
    fake.edges_transient = {core: value + 1 if core == 2 else -20 for core, value in held.items()}
    slots = []
    while fake.state.flow.stage is Stage.BISECT:
        if fake.worker is not None:
            slots.append(dict(fake.worker.spec.vector))
        assert fake.progress()
    assert held not in slots
    assert fake.state.flow.stage is Stage.TRANSIENT
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {
        core: (value + 1, value) if core == 2 else (value, bounds[core]) for core, value in held.items()
    }


@pytest.mark.req("D102", "L18.d.3.charged", "L09.G3")
@pytest.mark.parametrize("away", [PauseRequest, Close])
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_of_the_re_test_read_on_a_same_boot_resume_loosens_the_set_and_moves_its_core_once(
    away, corrected, steps
):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.post(away())
    records_at_resume(fake, Evidence(2, corrected, held[2]))
    fake.post(Resume(1))
    replaying(fake, held, judged_once(fake, held, bounds, steps))


@pytest.mark.req("D102", "L09.G3")
def test_a_record_of_the_tested_core_in_a_live1_bisect_s_full_set_is_its_test_s_fail_one_step_for_every_core():
    fake = fresh(4, [-12] * 4, 2, max_offset=-20, **FAST)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1:
        assert fake.progress()
    fake.crash()
    fake.post(Resume(1))
    assert (fake.state.flow.stage, fake.state.flow.bisect.entry) == (Stage.BISECT, Stage.LIVE1)
    held = {r.core: r.value for r in fake.state.cores}
    bounds = {r.core: r.fb for r in fake.state.cores}
    while fake.worker is None or dict(fake.worker.spec.vector) != held:
        assert fake.progress()
    lane = fake.worker.spec.core
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(lane, False),)))
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {
        core: (value + 1, bounds[core]) for core, value in held.items()
    }
    assert fake.state.flow.bisect.rerun == fake.state.flow.bisect.universe == tuple(sorted(held))
    while fake.worker is None:
        assert fake.progress()
    assert (fake.worker.spec.stage, dict(fake.worker.spec.vector)) == (
        Stage.BISECT,
        {core: value + 1 for core, value in held.items()},
    )


@pytest.mark.req("D102", "L18.d.4.3b.held")
def test_a_dead_boot_in_the_re_test_with_a_record_of_a_core_not_owned_pauses_with_the_set_loosened():
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.crash((Evidence(2, False), Evidence(7, False)))
    fake.post(Resume(1))
    assert (fake.state.pause.reason, fake.state.pause.cores) == (PauseReason.HELD_KNOB, (7,))
    moved = judged_once(fake, held, bounds, 3)
    fake.post(Resume(1))
    replaying(fake, held, moved)


@pytest.mark.req("D102", "L18.d.3.charged")
def test_a_late_record_the_kernel_log_charges_to_the_set_s_values_while_its_re_test_waits_loosens_the_set():
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.post(PauseRequest())
    fake.post(Resume(1))
    slot = fake.state.slot
    assert (isinstance(slot, Waiting), fake.state.flow.bisect.rerun) == (True, None)
    fake.post(KernelLogRead(slot.token, (Evidence(2, True, held[2]),)))
    replaying(fake, held, judged_once(fake, held, bounds, 1))


@pytest.mark.req("D102", "L18.d.3.charged")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_late_record_of_a_re_test_that_already_failed_moves_no_core_twice(corrected, steps):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.deliver(Report(Group.SILICON, 1.0, named=(2,)))
    fake.post(PauseRequest())
    records_at_resume(fake, Evidence(2, corrected, held[2]))
    fake.post(Resume(1))
    replaying(fake, held, judged_once(fake, held, bounds, steps))


def q6_replay_waiting(fake, held):
    fake.deliver(Report(Group.SILICON, 1.0, named=(2,)))
    replay = {core: value + 1 for core, value in held.items()}
    assert (isinstance(fake.state.slot, Waiting), fake.state.flow.bisect.rerun) == (True, tuple(sorted(held)))
    assert {r.core: r.value for r in fake.state.cores} == replay
    return replay


def moved_alone(fake, before, bounds, core, steps):
    moved = {other: value + steps if other == core else value for other, value in before.items()}
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {
        other: (value, before[core] if other == core else bounds[other]) for other, value in moved.items()
    }
    return moved


@pytest.mark.req("D102", "L09.G3", "L18.d.3.charged")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_the_kernel_log_cannot_place_while_the_q6_replay_waits_backs_off_its_own_core_alone(corrected, steps):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    replay = q6_replay_waiting(fake, held)
    fake.post(KernelLogRead(fake.state.slot.token, (Evidence(1, corrected),)))
    replaying(fake, held, moved_alone(fake, replay, bounds, 1, steps))


@pytest.mark.req("D102", "L09.G3", "L18.d.3.charged")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_a_same_boot_resume_cannot_place_while_the_q6_replay_waits_backs_off_its_own_core_alone(
    corrected, steps
):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    replay = q6_replay_waiting(fake, held)
    fake.post(PauseRequest())
    records_at_resume(fake, Evidence(1, corrected))
    fake.post(Resume(1))
    replaying(fake, held, moved_alone(fake, replay, bounds, 1, steps))


@pytest.mark.req("D102", "D87", "L18.d.3.outside")
@pytest.mark.parametrize("journal", ["re-test", "replay"])
def test_a_record_on_a_core_another_program_wrote_while_the_q6_replay_waits_loosens_no_set_and_pauses_outside_write(
    journal,
):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    replay = q6_replay_waiting(fake, held)
    fake.post(PauseRequest())
    fake.co[1] = -30
    fake.outside.add(1)
    records_at_resume(fake, Evidence(1, True, {"re-test": held, "replay": replay}[journal][1]))
    fake.post(Resume(1))
    s = fake.state
    assert (s.pause.reason, s.pause.cores, s.flow.stage, s.flow.bisect.rerun) == (
        PauseReason.OUTSIDE_WRITE,
        (1,),
        Stage.BISECT,
        tuple(sorted(held)),
    )
    moved_alone(fake, replay, bounds, 1, 1)


@pytest.mark.req("D102", "L09.G3", "L18.d.3.charged")
def test_a_record_the_kernel_log_cannot_place_while_the_re_test_waits_backs_off_its_core_and_ends_the_bisection():
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.post(PauseRequest())
    fake.post(Resume(1))
    slot = fake.state.slot
    assert (isinstance(slot, Waiting), fake.state.flow.bisect.rerun) == (True, None)
    fake.post(KernelLogRead(slot.token, (Evidence(1, False),)))
    moved_alone(fake, held, bounds, 1, 3)
    assert (fake.state.flow.stage, fake.state.flow.bisect) == (Stage.TRANSIENT, None)


@pytest.mark.req("D102", "L09.G3", "G3.charged")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_in_the_running_q6_replay_s_report_loosens_the_set_once_more_and_moves_its_core_once(corrected, steps):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    replay = q6_replay_waiting(fake, held)
    replaying(fake, held, replay)
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(2, corrected),)))
    replaying(fake, held, judged_once(fake, replay, bounds, steps))


@pytest.mark.req("D102", "L09.G3", "L18.d.3.charged")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_the_journal_charged_to_the_waiting_replay_fails_it_beside_an_equal_record_it_could_not_place(
    corrected, steps
):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    replay = q6_replay_waiting(fake, held)
    fake.post(KernelLogRead(fake.state.slot.token, (Evidence(2, corrected), Evidence(2, corrected, replay[2]))))
    replaying(fake, held, judged_once(fake, replay, bounds, steps))


@pytest.mark.req("D102", "L09.G3")
def test_a_record_of_a_core_not_owned_in_the_re_test_s_report_pauses_as_held_knob_and_moves_no_core():
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(7, True),)))
    s = fake.state
    assert (s.pause.reason, s.pause.cores, s.flow.bisect.rerun) == (PauseReason.HELD_KNOB, (7,), None)
    assert {r.core: (r.value, r.fb) for r in s.cores} == {core: (value, bounds[core]) for core, value in held.items()}
    fake.post(Resume(1))
    while fake.worker is None:
        assert fake.progress()
    assert (fake.worker.spec.stage, dict(fake.worker.spec.vector)) == (Stage.BISECT, held)


@pytest.mark.req("D102", "L18.d.4.3b.held")
def test_a_dead_boot_in_a_live1_begun_re_test_with_a_record_of_a_core_not_owned_pauses_with_the_set_loosened():
    fake = fresh(4, [-12] * 4, 2, max_offset=-20, **FAST)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1:
        assert fake.progress()
    fake.crash()
    fake.post(Resume(1))
    held = {r.core: r.value for r in fake.state.cores}
    bounds = {r.core: r.fb for r in fake.state.cores}
    while fake.worker is None or dict(fake.worker.spec.vector) != held:
        assert fake.progress()
    lane = fake.worker.spec.core
    assert [r.core for r in fake.state.cores if r.in_test] == [lane]
    fake.crash((Evidence(2, False), Evidence(7, False)))
    fake.post(Resume(1))
    assert (fake.state.pause.reason, fake.state.pause.cores) == (PauseReason.HELD_KNOB, (7,))
    judged_once(fake, held, bounds, 3)


@pytest.mark.req("D102", "L09.G3", "L18.d.3.charged")
@pytest.mark.parametrize(("corrected", "steps"), [(True, 1), (False, 3)])
def test_a_record_a_resume_cannot_place_after_the_driver_ended_in_the_re_test_loosens_the_set_and_moves_its_core_once(
    corrected, steps
):
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    assert all(r.in_test for r in fake.db.cores)
    fake.state, fake.worker, fake.timer = SessionState(next_token=fake.state.next_token), None, None
    records_at_resume(fake, Evidence(2, corrected))
    fake.post(Resume(1))
    replaying(fake, held, judged_once(fake, held, bounds, steps))


@pytest.mark.req("D102", "L09.G3", "L18.d.3.charged")
def test_two_records_the_kernel_log_cannot_place_while_the_q6_replay_waits_back_off_their_own_cores_alone():
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    replay = q6_replay_waiting(fake, held)
    fake.post(KernelLogRead(fake.state.slot.token, (Evidence(1, True), Evidence(2, True))))
    moved = {core: value + 1 if core in (1, 2) else value for core, value in replay.items()}
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {
        core: (value, replay[core] if core in (1, 2) else bounds[core]) for core, value in moved.items()
    }
    replaying(fake, held, moved)


@pytest.mark.req("D102", "L18.d.4.3b.between")
def test_a_dead_boot_after_the_database_fell_behind_while_the_re_test_waits_moves_no_core_for_a_record_charged_to_it():
    fake, held, bounds = at_the_re_test(Stage.TRANSIENT, None)
    fake.post(PauseRequest())
    fake.post(Resume(1))
    fake.faults.fail_commits = 1
    fake.fire()
    assert fake.state.pause.reason is PauseReason.DATABASE
    fake.crash((Evidence(2, True, held[2]),))
    fake.post(Resume(1))
    s = fake.state
    assert (s.lifecycle, s.flow.stage, s.flow.bisect.rerun) == (Lifecycle.RUNNING, Stage.BISECT, None)
    assert {r.core: (r.value, r.fb) for r in s.cores} == {core: (value, bounds[core]) for core, value in held.items()}


@pytest.mark.req("D102", "L09.retest.named", "L30.e.failed")
def test_a_re_test_naming_a_core_not_owned_pauses_inside_the_bisection_its_resume_replays_and_abort_ends_aborted():
    fake = completed((-3, 0, -2), (0, 2))
    validated = values(fake)
    fake.post(ValidateProfile(1))
    until_stage(fake, Stage.ENDURANCE)
    fake.deliver(Report(Group.SILICON, 1.0))
    while fake.worker is None or dict(fake.worker.spec.vector) != validated:
        assert fake.progress()
    assert fake.worker.spec.stage is Stage.BISECT
    fake.deliver(Report(Group.SILICON, 1.0, named=(1,)))
    s = fake.state
    assert (s.pause.reason, s.pause.cores, s.flow.stage, values(fake)) == (
        PauseReason.HELD_KNOB,
        (1,),
        Stage.BISECT,
        validated,
    )
    fake.post(Resume(1))
    while fake.worker is None:
        assert fake.progress()
    assert (fake.worker.spec.stage, dict(fake.worker.spec.vector)) == (Stage.BISECT, validated)
    fake.post(Abort())
    assert (fake.state.lifecycle, fake.db.status) == (Lifecycle.ABORTED, "aborted")


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


@pytest.mark.req("L10.e")
def test_an_unnamed_steady_failure_with_every_core_at_zero_pauses_as_stock_unstable_without_a_bisect():
    fake, stages = run([0, 0], on_start=report_once(Stage.STEADY, Report(Group.SILICON, 1.0)))
    assert (fake.state.lifecycle, fake.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.STOCK_UNSTABLE)
    assert fake.state.pause.detail == "the all-core load fails with every owned core at 0"
    assert {r.core: r.value for r in fake.state.cores} == {0: 0, 1: 0}
    assert Stage.BISECT not in {stage for stage, _, _ in stages}


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


@pytest.mark.req("ALLCORE.unnamed", "ALLCORE.bisect.replay", "INV.L10", "L09.b", "L10.a", "L14.G2")
def test_an_unnamed_warm_failure_in_endurance_bisects_on_warm_and_only_the_core_it_finds_backs_off():
    fake = fresh(3, [-6, -6, -6], 2, max_offset=-8, endurance=True, **{**FAST, **DISTINCT_LENGTHS})
    start(fake)
    while fake.worker is None or (fake.worker.spec.stage, fake.worker.spec.load) != (Stage.ENDURANCE, Load.WARM):
        assert fake.progress()
    held = dict(fake.worker.spec.vector)
    before = {r.core: (r.value, r.fb) for r in fake.state.cores}
    fake.edges_transient = {0: -6, 1: -2, 2: -6}
    fake.deliver()
    assert (fake.state.flow.stage, fake.state.flow.bisect.entry) == (Stage.BISECT, Stage.ENDURANCE)
    slots = []
    while fake.state.flow.stage is Stage.BISECT:
        if fake.worker is not None:
            spec = fake.worker.spec
            slots.append((spec.load, spec.seconds, spec.cpus, dict(spec.vector)))
        assert fake.progress()
    warm = (Load.WARM, 4.0, (0, 1, 2, 3, 4, 5))
    assert slots == [
        (*warm, {0: 0, 1: 0, 2: 0}),
        (*warm, {0: held[0], 1: held[1], 2: 0}),
        (*warm, {0: 0, 1: 0, 2: held[2]}),
        (*warm, {0: held[0], 1: 0, 2: 0}),
        (*warm, {0: 0, 1: held[1], 2: 0}),
    ]
    assert (fake.state.flow.stage, fake.state.flow.allcore.t_clean) == (Stage.ENDURANCE, 0.0)
    assert {r.core: (r.value, r.fb) for r in fake.state.cores} == {**before, 1: (held[1] + 1, held[1])}
    again = next_spec(fake)
    assert (again.stage, again.load, again.seconds, dict(again.vector)) == (
        Stage.ENDURANCE,
        Load.WARM,
        4.0,
        {**held, 1: held[1] + 1},
    )


def next_spec(fake):
    while fake.worker is None:
        assert fake.progress()
    return fake.worker.spec


@pytest.mark.req("D57", "INV.L08", "L08.G2.named", "L08.d.vector")
def test_a_steady_failure_that_names_a_core_backs_that_core_off_in_place_without_a_bisect():
    fake, stages = run([-6, -2], hot={0: -2, 1: -2}, names=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.BISECT not in {stage for stage, _, _ in stages}
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-2, -3)
    assert [vector[0] for stage, _, vector in stages if stage is Stage.STEADY][:2] == [-3, -2]
    order = [stage for stage, _, _ in stages]
    assert len(order) - order[::-1].index(Stage.LIVE1) > order.index(Stage.STEADY)


@pytest.mark.req("D57", "L14.G2")
def test_a_failure_naming_a_core_in_endurance_backs_off_only_that_core_and_starts_no_bisect():
    named = Report(Group.SILICON, 1.0, named=(1,))
    fake, stages = run([-6, -6], endurance=True, on_start=report_once(Stage.ENDURANCE, named))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    held = next(vector for stage, _, vector in stages if stage is Stage.ENDURANCE)
    assert Stage.BISECT not in {stage for stage, _, _ in stages}
    core_0, core_1 = fake.state.cores
    assert (core_0.value, core_1.value, core_1.fb) == (held[0], held[1] + 1, held[1])


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
def test_the_reclaim_stops_after_three_rounds_even_when_a_stored_session_holds_more_steps_to_take_back():
    fake = fresh(2, [-6, -5], 2, max_offset=-8, **FAST)
    stages = []
    check = fake.check_start

    def recorded(state, spec):
        stages.append(spec.stage)
        check(state, spec)

    fake.check_start = recorded
    start(fake)
    until_stage(fake, Stage.TRANSIENT)
    fake.post(Close())
    fake.db = replace(fake.db, cores=tuple(replace(r, value=r.value + 2) for r in fake.db.cores))
    fake.post(Resume(1))
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert stages.count(Stage.TIGHTEN) == 3 * 5
    assert {r.core: r.value for r in fake.state.cores} == {0: -4, 1: -3}


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
@pytest.mark.parametrize("failure", ["named", "found"])
def test_a_back_off_after_the_reclaim_sends_the_session_through_another_live1_pass(failure):
    named = report_once(Stage.ENDURANCE, Report(Group.SILICON, 1.0, named=(0,)))
    on_start = named if failure == "named" else degrade(transient={0: -3, 1: -2})
    fake, stages = run([-4, -2], endurance=True, on_start=on_start)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = [stage for stage, _, _ in stages]
    assert max(i for i, stage in enumerate(order) if stage is Stage.LIVE1) > order.index(Stage.ENDURANCE)
