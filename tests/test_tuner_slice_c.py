from __future__ import annotations

from itertools import pairwise, takewhile

import pytest

from corecycler.tuner.machine import allcore
from corecycler.tuner.machine.types import (
    AllCore,
    CoreRecord,
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


@pytest.mark.req("ALLCORE.unnamed", "INV.L10", "L10.a", "L11.a")
def test_a_failure_that_names_no_core_loosens_every_core_by_one_step():
    fake, stages = run([-6, -5], transient={0: -2, 1: -1})
    allcore_runs = [vector for stage, _, vector in stages if stage in (Stage.TRANSIENT, Stage.TIGHTEN)]
    changes = [
        {core: after[core] - before[core] for core in before}
        for before, after in pairwise(allcore_runs)
        if after != before
    ]
    assert {0: 1, 1: 1} in changes
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = [stage for stage, _, _ in stages]
    assert max(i for i, stage in enumerate(order) if stage is Stage.LIVE1) > order.index(Stage.STEADY)


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
    fake, _ = run([0, 0], transient={0: 1, 1: 1})
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


@pytest.mark.req("L10.d.apply")
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


@pytest.mark.req("L12.b", "D30")
def test_a_transient_failure_that_names_a_core_backs_it_off_and_the_reclaim_leaves_it_there():
    fake, stages = run([-6, -3], transient={0: -2, 1: -3}, names=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
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
    fake, stages = run([-6, -5], on_start=report_once(Stage.TRANSIENT, unnamed))
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
