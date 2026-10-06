from __future__ import annotations

from corecycler.tuner.machine import allcore
from corecycler.tuner.machine.types import AllCore, CoreRecord, FailContext, Lifecycle, PauseReason, Phase, Stage
from tests._tuner_world import fresh, run_to_rest, start

FAST = {
    "hot_seconds": 1.0,
    "bisect_seconds": 1.0,
    "stage_seconds": (("burst", 1.0), ("warm", 1.0), ("idle", 1.0)),
    "transient_window_s": 2.0,
    "soak_seconds": 1.0,
    "wait_between_cores_s": 0.0,
    "k_1t": 1,
    "k_2t": 1,
}


def run(edges, hot=None, transient=None, cores=2, domains=2, stacks=(), **cfg):
    fake = fresh(cores, edges, domains, max_offset=-8, **{**FAST, **cfg})
    fake.edges_hot = hot
    fake.edges_transient = transient
    fake.stacks = stacks
    stages = []
    original = fake.check_start

    def spy(state, spec):
        stages.append((spec.stage, spec.load, dict(spec.vector)))
        original(state, spec)

    fake.check_start = spy
    start(fake)
    run_to_rest(fake, 20_000)
    return fake, stages


def test_a_clean_co_only_run_walks_every_stage_once_in_the_planned_order():
    fake, stages = run([-4, -2])
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    order = list(dict.fromkeys(stage for stage, _, _ in stages))
    assert order == [Stage.SEARCH, Stage.LIVE1, Stage.STEADY, Stage.TRANSIENT, Stage.SOAK]
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
