from __future__ import annotations

import pytest

from corecycler.tuner.machine import search
from corecycler.tuner.machine.types import (
    CoreRecord,
    Klass,
    Lifecycle,
    MachineConfig,
    Mode,
    PauseReason,
    Phase,
    Stage,
)
from corecycler.tuner.orders import Method
from tests._tuner_world import (
    Abort,
    Broken,
    Close,
    Evidence,
    Group,
    PauseRequest,
    Report,
    Resume,
    fresh,
    run_to_rest,
    start,
)

CFG = MachineConfig(Mode.CO, Method.SEQUENTIAL, (0,), k_1t=3)


def walk(edge: int, cfg: MachineConfig = CFG) -> tuple[list[int], CoreRecord]:
    rec = search.resolve_start(CoreRecord(0), cfg)
    tested = []
    while rec.phase is not Phase.PROVEN:
        tested.append(rec.value)
        passed = rec.value >= edge
        step = (search.on_pass if passed else search.on_fail)(rec, cfg, Klass.T1, 60.0)
        rec = step.record
    return tested, rec


@pytest.mark.req("C3", "INV.L04", "K1", "K2", "L04.G1.fine", "L04.a", "L04.d.first", "L05.G1", "Q4")
def test_the_walk_goes_coarse_then_fine_then_proves_the_edge_k_times():
    tested, rec = walk(-12)
    assert tested == [-5, -10, -15, -11, -12, -13, -12, -12, -12]
    assert (rec.value, rec.fb, rec.count) == (-12, -13, 3)


@pytest.mark.req("L04.G2")
def test_a_core_whose_first_fine_value_fails_settles_at_stock_with_no_proof():
    tested, rec = walk(0)
    assert tested == [-5, -1]
    assert (rec.phase, rec.value) == (Phase.PROVEN, 0)


@pytest.mark.req("K3", "K45", "L04.G1.coarse", "TM.I_M8")
def test_the_walk_never_passes_max_offset():
    tested, rec = walk(-50, MachineConfig(Mode.CO, Method.SEQUENTIAL, (0,), max_offset=-7, k_1t=1))
    assert min(tested) == -7
    assert rec.value == -7


@pytest.mark.req("C2", "G2", "L05.G2", "L05.a", "Q3")
def test_a_fail_during_the_proof_backs_off_one_step_and_restarts_the_count():
    rec = CoreRecord(0, phase=Phase.PROVE, value=-20, fb=-21, count=2)
    rec = search.on_fail(rec, CFG, Klass.T1, 60.0).record
    assert (rec.phase, rec.value, rec.fb, rec.count) == (Phase.PROVE, -19, -20, 0)


@pytest.mark.req("D38", "K12", "L04.G4")
def test_a_crash_backs_off_three_steps_and_sets_the_fail_bound_at_the_crashed_value():
    rec = search.on_crash(CoreRecord(0, phase=Phase.FINE, value=-20, best=-19), CFG).record
    assert (rec.phase, rec.value, rec.fb, rec.crash_count) == (Phase.PROVE, -17, -20, 1)


K = CFG.k(Klass.T1)


def hung_at(value: int) -> CoreRecord:
    return search.on_crash(CoreRecord(0, phase=Phase.FINE, value=value, best=value + 1), CFG).record


def passed(rec: CoreRecord, times: int) -> CoreRecord:
    for _ in range(times):
        rec = search.on_pass(rec, CFG, Klass.T1, 60.0).record
    return rec


@pytest.mark.req("K12.reclaim")
def test_a_proven_crash_landing_proves_each_value_it_skipped_and_stays_at_the_deepest_that_holds():
    landing = hung_at(-20)
    rec = passed(landing, K)
    assert (landing.phase, landing.value, rec.phase, rec.value, rec.count) == (Phase.PROVE, -17, Phase.PROVE, -18, 0)
    assert landing.skipped == -20
    assert passed(rec, K - 1).value == -18
    rec = passed(rec, K)
    assert (rec.phase, rec.value, rec.count) == (Phase.PROVE, -19, 0)
    rec = passed(rec, K)
    assert (rec.phase, rec.value, rec.fb) == (Phase.PROVEN, -19, -20)
    assert (rec.skipped, rec.reclaim_floor) == (None, None)


@pytest.mark.req("K12.reclaim.fail")
def test_a_fail_in_a_reclaim_leaves_the_core_proven_at_the_value_it_reclaimed_from():
    rec = search.on_fail(passed(hung_at(-20), K), CFG, Klass.T1, 60.0).record
    assert (rec.phase, rec.value, rec.fb) == (Phase.PROVEN, -17, -18)
    assert (rec.count, rec.skipped, rec.reclaim_floor) == (0, None, None)


@pytest.mark.req("K12.reclaim.crash")
def test_a_crash_in_a_reclaim_leaves_the_core_proven_at_the_value_it_reclaimed_from():
    rec = search.on_crash(passed(hung_at(-20), 2 * K), CFG).record
    assert (rec.phase, rec.value, rec.fb, rec.crash_count) == (Phase.PROVEN, -18, -19, 2)
    assert (rec.count, rec.skipped, rec.reclaim_floor) == (0, None, None)
    again = search.on_crash(hung_at(-20), CFG).record
    assert (again.phase, again.value, again.fb, again.skipped) == (Phase.PROVE, -14, -17, -17)


@pytest.mark.req("K12.reclaim.bound")
def test_a_crash_landing_that_fails_tries_nothing_at_or_past_its_new_fail_bound():
    rec = passed(search.on_fail(hung_at(-20), CFG, Klass.T1, 60.0).record, K)
    assert (rec.phase, rec.value, rec.fb) == (Phase.PROVEN, -16, -17)


@pytest.mark.req("K12.reclaim.stock")
def test_a_hang_within_three_steps_of_stock_proves_each_value_between_and_stays_at_the_deepest_that_holds():
    landing = hung_at(-3)
    assert (landing.phase, landing.value, landing.fb, landing.count, landing.crash_count) == (Phase.PROVE, -1, -3, 0, 1)
    assert passed(landing, K - 1).value == -1
    rec = passed(landing, K)
    assert (rec.phase, rec.value, rec.count) == (Phase.PROVE, -2, 0)
    assert passed(rec, K - 1).value == -2
    rec = passed(rec, K)
    assert (rec.phase, rec.value, rec.fb) == (Phase.PROVEN, -2, -3)
    assert (rec.skipped, rec.reclaim_floor) == (None, None)


@pytest.mark.req("K12.reclaim.stock")
def test_a_fail_one_step_below_stock_after_a_near_stock_hang_returns_the_core_to_stock_proven():
    step = search.on_fail(hung_at(-3), CFG, Klass.T1, 60.0)
    assert (step.record.phase, step.record.value, step.record.fb, step.stock_unstable) == (Phase.PROVEN, 0, -1, False)
    assert (step.record.count, step.record.skipped, step.record.reclaim_floor) == (0, None, None)


@pytest.mark.req("K12.reclaim.stock")
def test_a_hang_one_step_below_stock_has_nothing_between_and_is_proven_at_stock_at_once():
    rec = hung_at(-1)
    assert (rec.phase, rec.value, rec.fb, rec.count, rec.crash_count) == (Phase.PROVEN, 0, -1, 0, 1)


@pytest.mark.req("D20")
def test_a_failed_hint_restarts_the_walk_from_stock():
    rec = search.resolve_start(CoreRecord(0, start=-30), CFG)
    assert rec.value == -30
    rec = search.on_fail(rec, CFG, Klass.T1, 60.0).record
    assert (rec.phase, rec.value, rec.start, rec.fb) == (Phase.COARSE, -5, 0, -30)


@pytest.mark.req("G2.zero")
def test_a_fail_at_stock_is_reported_as_stock_unstable_and_changes_nothing():
    rec = CoreRecord(0, phase=Phase.PROVEN, value=0)
    step = search.on_fail(rec, CFG, Klass.T1, 60.0)
    assert step.stock_unstable and step.record == rec


@pytest.mark.req("L04.d.vector", "L26.d.done")
@pytest.mark.parametrize(("cores", "domains", "smt"), [(1, 1, True), (2, 2, True), (4, 2, False), (3, 1, True)])
def test_a_clean_run_finds_every_true_edge_and_applies_it(cores, domains, smt):
    edges = [-12, -3, -20, 0][:cores]
    fake = fresh(cores, edges, domains, smt, max_offset=-25, k_1t=3)
    start(fake)
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == dict(enumerate(edges))
    assert {c: fake.co[c] for c in range(cores)} == dict(enumerate(edges))


@pytest.mark.req("INV.L05", "K11", "K5", "L04.f", "L26.d.recheck", "L26.d.repeat", "ORDER.round")
def test_one_core_is_tested_twice_in_a_row_only_right_after_its_own_fail():
    edges = [-8, -3, -6]
    fake = fresh(3, edges, 1, max_offset=-10, k_1t=3, wait_between_cores_s=0)
    tested = []
    original = fake.check_start

    def spy(state, spec):
        tested.append((spec.stage, spec.klass, spec.core, spec.value))
        original(state, spec)

    fake.check_start = spy
    start(fake)
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    first_visits = list(dict.fromkeys(core for _, _, core, _ in tested))
    assert first_visits == [0, 1, 2]
    per_core = [entry for entry in tested if entry[0] in (Stage.SEARCH, Stage.LIVE1)]
    back_to_back = [(a, b) for a, b in zip(per_core, per_core[1:], strict=False) if a[:3] == b[:3]]
    assert back_to_back
    assert all(value < edges[core] for (_, _, core, value), _ in back_to_back)


@pytest.mark.req("G8", "INV.L19", "L04.G8", "L08.G8", "L19.d.report", "VERDICT.25")
def test_a_pause_mid_test_finishes_the_test_then_puts_every_core_at_baseline():
    fake = fresh(2, [-12, -6], 2, max_offset=-20, k_1t=2)
    start(fake)
    fake.fire()
    fake.post(PauseRequest())
    assert fake.state.lifecycle is Lifecycle.PAUSING
    fake.deliver()
    assert fake.state.lifecycle is Lifecycle.PAUSED
    assert fake.co == {0: 0, 1: 0}
    fake.post(Resume(1))
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


@pytest.mark.req("VERDICT.22")
def test_a_crash_mid_test_convicts_the_tested_core_on_resume():
    fake = fresh(2, [-12, -6], 2, max_offset=-20, k_1t=2)
    start(fake)
    fake.fire()
    tested = fake.worker.spec
    fake.crash()
    fake.post(Resume(1))
    rec = fake.state.core(tested.core)
    assert rec.fb == tested.value
    assert rec.crash_count == 1
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


@pytest.mark.req("K18", "L18.d.4.5", "L18.d.6", "L18.g")
def test_three_crashes_in_a_row_quarantine_the_session_at_baseline():
    fake = fresh(3, [-30, -30, -30], max_offset=-40, k_1t=2, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.value > -20:
        fake.progress()
    crashed = []
    for _ in range(3):
        while fake.worker is None:
            fake.fire()
        crashed.append((fake.worker.spec.core, fake.worker.spec.value))
        fake.crash()
        fake.post(Resume(1))
    assert len({core for core, _ in crashed}) == 1
    assert [value for _, value in crashed] == [crashed[0][1], crashed[0][1] + 3, crashed[0][1] + 6]
    assert fake.state.lifecycle is Lifecycle.QUARANTINED
    assert fake.co == {0: 0, 1: 0, 2: 0}


@pytest.mark.req("G7", "K16", "L22.G6_G7", "L22.a", "L22.d.percore")
def test_the_fourth_apparatus_failure_in_a_row_pauses_with_every_owned_core_at_baseline():
    fake = fresh(1, [-30], max_offset=-40, k_1t=2)
    start(fake)
    fake.fire()
    for _ in range(3):
        fake.deliver(Report(Group.APPARATUS, 1.0, detail="tool died"))
        fake.fire()
    assert fake.state.lifecycle is Lifecycle.RUNNING
    fake.deliver(Report(Group.APPARATUS, 1.0, detail="tool died"))
    assert fake.state.pause.reason is PauseReason.APPARATUS
    assert fake.co == {0: 0}


@pytest.mark.req("G1")
@pytest.mark.parametrize("group", [Group.APPARATUS, Group.THERMAL])
def test_a_verdict_between_failures_without_one_starts_their_count_again(group):
    fake = fresh(1, [-30], max_offset=-40, k_1t=2)
    start(fake)

    def fail(times: int) -> None:
        for _ in range(times):
            while fake.worker is None:
                assert fake.progress()
            fake.deliver(Report(group, 1.0, detail="no verdict"))
            fake.temperature = 60.0

    fail(3)
    while fake.worker is None:
        assert fake.progress()
    fake.deliver()
    fail(3)
    assert fake.state.lifecycle is Lifecycle.RUNNING
    fail(1)
    assert fake.state.lifecycle is Lifecycle.PAUSED


@pytest.mark.req("INV.L20", "K17.cooldown", "L20.a", "L20.cap", "TRUTH.TEMPERATURE_UNREAD", "VERDICT.37")
def test_a_thermal_stop_waits_for_the_cpu_to_cool_and_gives_up_at_the_cap():
    fake = fresh(1, [-30], max_offset=-40, k_1t=2)
    start(fake)
    fake.fire()
    fake.temperature = 99.0
    fake.deliver(Report(Group.THERMAL, 5.0))
    while fake.timer is not None and fake.state.lifecycle is Lifecycle.RUNNING:
        fake.fire()
    assert fake.state.pause.reason is PauseReason.THERMAL
    assert "99.0 C" in fake.state.pause.detail
    assert fake.co == {0: 0}


@pytest.mark.req("K17.missing", "TRUTH.TEMPERATURE_UNREAD")
def test_a_temperature_unreadable_while_cooling_pauses_as_no_verdict_not_as_heat():
    fake = fresh(1, [-30], max_offset=-40, k_1t=2)
    start(fake)
    fake.fire()
    fake.temperature = 99.0
    fake.deliver(Report(Group.THERMAL, 5.0))
    fake.temperature = None
    while fake.timer is not None and fake.state.lifecycle is Lifecycle.RUNNING:
        fake.fire()
    assert fake.state.pause.reason is PauseReason.APPARATUS
    assert "could not be read" in fake.state.pause.detail
    assert fake.co == {0: 0}


@pytest.mark.req("G10", "VERDICT.29")
def test_a_refused_write_pauses_with_every_owned_core_at_baseline():
    fake = fresh(2, [-12, -6], 2, max_offset=-20, k_1t=2)
    start(fake)
    fake.faults.refuse_writes = 1
    fake.fire()
    assert fake.state.pause.reason is PauseReason.SMU_WRITE
    fake.post(Resume(1))
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


@pytest.mark.req("G11", "L23.resume", "VERDICT.34")
def test_a_database_failure_puts_every_owned_core_at_stock_and_holds_the_session_in_memory():
    fake = fresh(2, [-12, -6], 2, max_offset=-20, k_1t=2)
    start(fake)
    fake.fire()
    fake.faults.fail_commits = 1
    fake.deliver()
    assert fake.state.pause.reason is PauseReason.DATABASE
    assert fake.co == {0: 0, 1: 0}
    fake.post(Resume(1))
    run_to_rest(fake, 2000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


@pytest.mark.req("G3.search_zero", "L04.G3.zero", "L05.G3", "PRELUDE.2")
def test_a_machine_check_on_an_idle_core_at_stock_pauses_as_stock_unstable():
    fake = fresh(2, [-12, -6], 2, max_offset=-20, k_1t=2)
    start(fake)
    fake.fire()
    fake.deliver(Report(Group.PASS, 60.0, evidence=(Evidence(1, True),)))
    assert fake.state.pause.reason is PauseReason.STOCK_UNSTABLE
    assert fake.state.pause.cores == (1,)


@pytest.mark.req("G9", "L19.d.close", "TERMS.revertall", "TM.B1.co", "VERDICT.28")
def test_abort_and_close_leave_every_owned_core_at_baseline():
    for ender in (Abort(), Close()):
        fake = fresh(2, [-12, -6], 2, max_offset=-20, k_1t=2)
        start(fake)
        fake.fire()
        fake.post(ender)
        assert fake.co == {0: 0, 1: 0}
        assert fake.worker is None


def test_the_harness_catches_a_machine_that_retests_a_failed_value(monkeypatch):
    fake = fresh(1, [-12], max_offset=-20, k_1t=2)
    start(fake)
    monkeypatch.setattr(search, "on_fail", lambda rec, cfg, klass, seconds: search.CoreStep(rec))
    with pytest.raises(Broken, match="after a FAIL"):
        run_to_rest(fake, 2000)
