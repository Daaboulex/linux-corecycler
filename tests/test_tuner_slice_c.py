from __future__ import annotations

from itertools import takewhile

from corecycler.tuner.machine import allcore
from corecycler.tuner.machine.types import (
    AllCore,
    CoreRecord,
    FailContext,
    Lifecycle,
    Load,
    PauseReason,
    Phase,
    Stage,
)
from tests._tuner_world import Group, Report, fresh, run_to_rest, start

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


def test_a_clean_co_only_run_walks_every_stage_once_in_the_planned_order():
    fake, stages = run([-4, -2])
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = list(dict.fromkeys(stage for stage, _, _ in stages))
    assert order == [Stage.SEARCH, Stage.LIVE1, Stage.STEADY, Stage.TRANSIENT, Stage.MEMORY, Stage.SOAK]
    assert {r.core: r.value for r in fake.state.cores} == {0: -4, 1: -2}


def test_a_steady_failure_bisects_to_the_failing_core_and_reruns_steady():
    fake, stages = run([-6, -2], hot={0: -4, 1: -2})
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.BISECT in {stage for stage, _, _ in stages}
    assert fake.state.core(0).value >= -4
    hot_runs = [vector for stage, load, vector in stages if stage is Stage.STEADY]
    assert len(hot_runs) >= 2


def test_a_transient_failure_loosens_one_working_core_and_tighten_tries_it_back():
    fake, stages = run([-6, -3], transient={0: -5, 1: -3})
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert fake.state.core(0).value == -5
    tighten = [vector for stage, _, vector in stages if stage is Stage.TIGHTEN]
    assert tighten and all(vector[0] == -6 for vector in tighten)


def test_a_loosening_after_live1_sends_co_only_through_a_second_pass_without_tighten():
    fake, stages = run([-6, -3], transient={0: -5, 1: -3})
    live1_runs = [i for i, (stage, _, _) in enumerate(stages) if stage is Stage.LIVE1]
    steady_runs = [i for i, (stage, _, _) in enumerate(stages) if stage is Stage.STEADY]
    tighten_runs = [i for i, (stage, _, _) in enumerate(stages) if stage is Stage.TIGHTEN]
    assert max(live1_runs) > min(steady_runs)
    assert max(tighten_runs) < max(live1_runs)


def test_soak_can_be_switched_off():
    fake, stages = run([-4, -2], soak=False)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.SOAK not in {stage for stage, _, _ in stages}


def test_the_transient_rule_takes_the_least_loosened_working_core_for_a_light_failure():
    cursor = AllCore(co_delta=((0, 2),))
    cores = (CoreRecord(0, Phase.PROVEN, -5), CoreRecord(1, Phase.PROVEN, -5), CoreRecord(2, Phase.PROVEN, -5))
    fix = allcore.transient_fix(cursor, FailContext(False, (0, 1), True), cores, 3, False)
    assert fix.core == 1


def test_a_boost_failure_names_a_core_only_when_it_recurs_under_the_same_type_key():
    cores = tuple(CoreRecord(c, Phase.PROVEN, -5) for c in range(8))
    history = AllCore(t_n=10, t_cores=((0, 9),))
    ctx = FailContext(False, (0, 3), False, mhz=5300.0, tctl=60.0)
    first = allcore.transient_fix(history, ctx, cores, 8, False)
    second = allcore.transient_fix(first.cursor, ctx, cores, 8, False)
    assert allcore.background(first.cursor) == (0,)
    assert "least loosened" in first.detail
    assert second.core == 3 and "least loosened" not in second.detail


def test_the_first_transient_failure_is_light_because_its_cores_count_as_background():
    cores = tuple(CoreRecord(c, Phase.PROVEN, -5) for c in range(8))
    fix = allcore.transient_fix(AllCore(), FailContext(False, (3,), False, 5300.0, 60.0), cores, 8, False)
    assert fix.detail.startswith("light")
    assert fix.core == 3


def test_a_step_failure_falls_back_to_the_least_loosened_core_and_the_owner_order_breaks_ties():
    cores = tuple(CoreRecord(c, Phase.PROVEN, -5) for c in range(16))
    ctx = FailContext(False, tuple(range(16)), False, mhz=5000.0, tctl=80.0)
    assert allcore.transient_fix(AllCore(), ctx, cores, 16, True).core == 8
    assert allcore.transient_fix(AllCore(), ctx, cores, 16, False).core == 0


def test_nothing_left_to_loosen_is_reported():
    cores = (CoreRecord(0, Phase.PROVEN, 0),)
    assert allcore.transient_fix(AllCore(), FailContext(False, (0,), True), cores, 1, False).core is None


def test_bands_and_columns_follow_the_record_and_an_unknown_reading_is_the_middle():
    assert [allcore.band(m) for m in (None, 4000, 4400, 5200, 5700)] == [2, 1, 2, 3, 4]
    assert [allcore.column(c) for c in (None, 60.0, 80.0)] == [1, 1, 2]
    assert allcore.type_key(FailContext(False, (1,), False, 4450.0, 72.0), False, "boost") == "boost-21bc"


def test_a_steady_failure_with_every_core_at_zero_pauses_as_stock_unstable():
    fake, stages = run([0, 0], hot={0: 1, 1: 1})
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert fake.state.pause.reason is PauseReason.STOCK_UNSTABLE
    assert [vector for stage, _, vector in stages if stage is Stage.STEADY] == [{0: 0, 1: 0}]
    assert Stage.BISECT not in {stage for stage, _, _ in stages}


def test_endurance_is_off_unless_asked_and_when_on_cycles_its_five_loads_before_soak():
    fake, stages = run([-4, -2], endurance=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = list(dict.fromkeys(stage for stage, _, _ in stages))
    assert order == [
        Stage.SEARCH,
        Stage.LIVE1,
        Stage.STEADY,
        Stage.TRANSIENT,
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


def test_a_transient_failure_in_endurance_loosens_one_core_and_runs_the_same_load_again():
    fake, stages = run([-6, -3], endurance=True, on_start=degrade(transient={0: -5, 1: -3}))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert fake.state.core(0).value == -5
    endurance = [(load, vector[0]) for stage, load, vector in stages if stage is Stage.ENDURANCE]
    assert endurance[:3] == [(Load.HOT, -6), (Load.BURST, -6), (Load.BURST, -5)]


def test_a_steady_failure_that_names_a_core_backs_that_core_off_in_place_without_a_bisect():
    fake, stages = run([-6, -2], hot={0: -4, 1: -2}, names=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.BISECT not in {stage for stage, _, _ in stages}
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-4, -5)
    assert [vector[0] for stage, _, vector in stages if stage is Stage.STEADY][:3] == [-6, -5, -4]
    order = [stage for stage, _, _ in stages]
    assert len(order) - order[::-1].index(Stage.LIVE1) > order.index(Stage.STEADY)


def test_a_transient_failure_that_names_a_core_backs_off_that_core_and_leaves_nothing_to_tighten():
    fake, stages = run([-6, -3], transient={0: -5, 1: -3}, names=True)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-5, -6)
    assert Stage.TIGHTEN not in {stage for stage, _, _ in stages}


def name_from(stage, hot=None, transient=None):
    def on_start(fake, spec):
        if spec.stage is stage and not fake.names_cores:
            fake.names_cores = True
            fake.edges_hot = hot or fake.edges_hot
            fake.edges_transient = transient or fake.edges_transient

    return on_start


def test_a_tighten_trial_that_names_its_own_core_freezes_the_item_as_before():
    fake, stages = run([-6, -3], transient={0: -5, 1: -3}, on_start=name_from(Stage.TIGHTEN))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-5, -7)
    assert [vector[0] for stage, _, vector in stages if stage is Stage.TIGHTEN] == [-6]


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


def test_a_tighten_trial_that_names_another_core_backs_that_core_off_and_repeats_the_trial():
    named = Report(Group.SILICON, 1.0, named=(1,))
    fake, stages = run([-6, -3], transient={0: -5, 1: -3}, on_start=report_once(Stage.TIGHTEN, named))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (fake.state.core(1).value, fake.state.core(1).fb) == (-2, -3)
    trials = [(vector[0], vector[1]) for stage, _, vector in stages if stage is Stage.TIGHTEN]
    assert trials[:2] == [(-6, -3), (-6, -2)]


def test_a_named_core_outside_the_session_pauses_as_a_held_knob():
    named = Report(Group.SILICON, 1.0, named=(7,))
    fake, _ = run([-4, -2], on_start=report_once(Stage.STEADY, named))
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert fake.state.pause.reason is PauseReason.HELD_KNOB and "core 7" in fake.state.pause.detail


def test_a_named_core_already_at_zero_leaves_the_failure_to_the_stage_rule():
    fake, stages = run([0, -2], hot={0: 1, 1: -2}, names=True)
    assert Stage.BISECT in {stage for stage, _, _ in stages}


def test_a_named_back_off_in_transient_restarts_the_clean_window_on_the_same_load():
    named = Report(Group.SILICON, 1.0, named=(0,))
    fake, stages = run([-6, -3], on_start=report_once(Stage.TRANSIENT, named, nth=2))
    begin = next(i for i, (stage, _, _) in enumerate(stages) if stage is Stage.TRANSIENT)
    first_pass = [load for _, load, _ in takewhile(lambda run: run[0] is Stage.TRANSIENT, stages[begin:])]
    assert first_pass == [Load.BURST, Load.WARM, Load.WARM, Load.BURST]
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-5, -6)


def test_memory_can_be_switched_off():
    fake, stages = run([-4, -2], memory=False)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert Stage.MEMORY not in {stage for stage, _, _ in stages}


def test_a_failing_memory_lane_backs_its_core_off_requeues_it_for_a_live1_slot_and_runs_memory_again():
    fake, stages = run([-6, -3], memory_edges={0: -4, 1: -3})
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert (fake.state.core(0).value, fake.state.core(0).fb) == (-4, -5)
    begin = next(i for i, (stage, _, _) in enumerate(stages) if stage is Stage.MEMORY)
    walk = [(stage, vector[0]) for stage, _, vector in stages[begin : begin + 5]]
    assert walk == [(Stage.MEMORY, -6), (Stage.LIVE1, -5), (Stage.MEMORY, -5), (Stage.LIVE1, -4), (Stage.MEMORY, -4)]
    assert Stage.STEADY in {stage for stage, _, _ in stages[begin:]}


def test_a_memory_lane_failing_at_zero_gets_a_solo_retest_while_another_core_is_not_at_zero():
    fake, stages = run([0, -3], memory_edges={0: 1, 1: -3})
    solos = [vector for stage, _, vector in stages if stage is Stage.LIVE1 and vector == {0: 0, 1: 0}]
    assert solos
    assert fake.state.lifecycle is Lifecycle.PAUSED and fake.state.pause.reason is PauseReason.STOCK_UNSTABLE
    assert fake.state.core(1).value == 0 and fake.state.core(1).fb == -1


def test_a_memory_lane_outside_the_session_pauses_as_a_held_knob():
    fake, _ = run([-4, -2], on_start=report_once(Stage.MEMORY, Report(Group.SILICON, 1.0, named=(5,))))
    assert fake.state.pause.reason is PauseReason.HELD_KNOB and "core 5" in fake.state.pause.detail


def test_a_memory_failure_naming_no_lane_bisects_with_the_memory_slot():
    fake, stages = run([-4, -2], on_start=report_once(Stage.MEMORY, Report(Group.SILICON, 1.0)))
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    slots = [load for stage, load, _ in stages if stage is Stage.BISECT]
    assert slots and set(slots) == {Load.MEMORY}


def test_a_skipped_memory_run_is_no_verdict_and_the_plan_goes_on():
    fake = fresh(2, [-4, -2], 2, max_offset=-8, **FAST)
    fake.memory_skip = "stressapptest is not installed"
    start(fake)
    run_to_rest(fake, 20_000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert any(line.startswith("memory_skipped: ") and "not installed" in line for line in fake.log)


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
