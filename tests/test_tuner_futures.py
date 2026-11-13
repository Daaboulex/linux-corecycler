from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corecycler.engine.detector import ProgramFault, ProgramFaults
from corecycler.tuner.live import resident_at_death
from corecycler.tuner.machine.session import ALLCORE_LOADS, HELD
from corecycler.tuner.machine.types import (
    HELD_STATUSES,
    ContinueOnSetup,
    FailContext,
    Lifecycle,
    Load,
    PauseReason,
    RestartOnSetup,
    Stage,
    ValidateProfile,
)
from corecycler.tuner.orders import Method, order_problem
from tests._tuner_world import (
    Abort,
    Broken,
    Close,
    Evidence,
    Fake,
    Group,
    PauseRequest,
    Report,
    Resume,
    SessionState,
    Stored,
    fresh,
    run_to_rest,
    start,
)

FAULTS = (
    "thermal",
    "apparatus",
    "slept",
    "false_fail",
    "evidence",
    "pause",
    "close",
    "crash",
    "refuse",
    "dbfail",
    "hot",
    "abort",
    "unblamed",
    "mce_other",
    "crash_mce",
    "crash_shared",
    "validate",
    "profile",
    "setup",
    "kernel",
    "continue",
    "restart",
    "outside",
    "reengage",
    "program_fault",
    "paused_hot",
    "start",
    "named_unowned",
    "skipped",
    "crash_mce_between",
)
FOREIGN_FAULTS = ProgramFaults(
    100_000_000_000,
    (
        ProgramFault(
            "wireplumber",
            2884,
            23,
            7,
            "wireplumber[2884]: segfault at 0 ip 00007f6d4a3c2e17 sp 00007ffd2b8e6f40 error 4 in "
            "libasound.so.2.0.0[5ae17,7f6d4a368000+b6000] likely on CPU 23 (core 7, socket 0)",
            115.5,
        ),
    ),
    0,
)


def clone(fake: Fake) -> Fake:
    twin = copy.copy(fake)
    twin.co = dict(fake.co)
    twin.resident = dict(fake.resident)
    twin.failed_at = set(fake.failed_at)
    twin.failed_allcore = set(fake.failed_allcore)
    twin.failed_vectors = set(fake.failed_vectors)
    twin.failed_twice = set(fake.failed_twice)
    twin.failed_slots = set(fake.failed_slots)
    twin.stock_fail_owed = set(fake.stock_fail_owed)
    twin.superseded = set(fake.superseded)
    twin.outside = set(fake.outside)
    twin.behind = set(fake.behind)
    twin.faults = copy.copy(fake.faults)
    twin.owed = set(fake.owed)
    twin.log = []
    return twin


def journal_summary(fake: Fake) -> tuple:
    return tuple(
        sorted(
            (
                core,
                (held.last.value, held.last.confirmed, held.last.survived),
                None
                if held.confirmed_before is None
                else (held.confirmed_before.value, held.confirmed_before.survived),
            )
            for core, held in resident_at_death(fake.journal).items()
        )
    )


def key(fake: Fake) -> tuple:
    return (
        journal_summary(fake),
        fake.dead_suspects,
        tuple(sorted(fake.proven.items())),
        fake.state,
        tuple(sorted(fake.co.items())),
        tuple(sorted(fake.resident.items())),
        fake.db,
        fake.worker,
        fake.timer,
        fake.now,
        fake.temperature,
        fake.crash_note,
        fake.power,
        fake.setup,
        tuple(sorted(fake.edges.items())),
        fake.boot - fake.stored_boot,
        fake.faults.refuse_writes,
        fake.faults.fail_commits,
        frozenset(fake.failed_at),
        frozenset(fake.failed_allcore),
        frozenset(fake.failed_vectors),
        frozenset(fake.failed_twice),
        frozenset(fake.failed_slots),
        frozenset(fake.stock_fail_owed),
        frozenset(fake.superseded),
        frozenset(fake.outside),
        fake.crashed_bisect,
        fake.marker,
        frozenset(fake.behind),
        frozenset(fake.owed),
        fake.died_between_slots,
        fake.epoch,
    )


def resumable(fake: Fake) -> bool:
    s = fake.state
    if s.lifecycle is Lifecycle.PAUSED:
        return s.pause is not None and s.pause.reason is not PauseReason.RESUMING
    if s.lifecycle in (Lifecycle.IDLE, Lifecycle.ABORTED):
        return fake.db is not None and fake.db.status in ("running", "paused", "aborted")
    return False


def moves(fake: Fake, budget: dict[str, int]) -> list[tuple[str, dict[str, int]]]:
    out: list[tuple[str, dict[str, int]]] = []
    s = fake.state
    if fake.worker is not None:
        out.append(("deliver", budget))
    elif fake.timer is not None:
        out.append(("fire", budget))
    elif resumable(fake):
        out.append(("resume", budget))
    for fault in FAULTS:
        if budget.get(fault, 0) <= 0 or budget["total"] <= 0:
            continue
        spent = {**budget, fault: budget[fault] - 1, "total": budget["total"] - 1}
        if fault in (
            "thermal",
            "apparatus",
            "slept",
            "false_fail",
            "evidence",
            "unblamed",
            "mce_other",
            "program_fault",
        ) and (fake.worker is None):
            continue
        if fault in ("pause", "abort") and s.lifecycle not in (Lifecycle.RUNNING, Lifecycle.PAUSING):
            continue
        if fault in ("crash_mce", "crash_shared") and fake.worker is None:
            continue
        if fault == "paused_hot" and (fake.worker is None or fake.worker.spec.load is not Load.HOT):
            continue
        if fault == "named_unowned" and (fake.worker is None or fake.worker.spec.load not in ALLCORE_LOADS):
            continue
        if fault == "skipped" and (fake.worker is None or fake.worker.spec.load is not Load.MEMORY):
            continue
        if fault == "crash_mce_between" and (fake.worker is not None or s.lifecycle is not Lifecycle.RUNNING):
            continue
        if fault == "validate" and (fake.db is None or fake.db.status != "completed" or s.lifecycle in HELD):
            continue
        if fault == "start" and (s.lifecycle in HELD or fake.db is None or fake.db.status in HELD_STATUSES):
            continue
        if fault in ("close", "crash") and s.lifecycle is Lifecycle.IDLE:
            continue
        if fault in ("refuse", "dbfail", "hot") and s.lifecycle is not Lifecycle.RUNNING:
            continue
        if fault in ("setup", "kernel") and s.lifecycle in (Lifecycle.RUNNING, Lifecycle.PAUSING):
            continue
        if fault == "outside" and not (
            s.lifecycle is Lifecycle.RUNNING
            or (s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is not PauseReason.RESUMING)
        ):
            continue
        if fault == "reengage" and not (
            s.lifecycle is Lifecycle.QUARANTINED and fake.db is not None and fake.db.status == "quarantined"
        ):
            continue
        if fault in ("continue", "restart") and not (
            s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is PauseReason.SETUP_CHANGED
        ):
            continue
        out.append((fault, spent))
    return out


def act(fake: Fake, move: str) -> None:
    worker_spec = fake.worker.spec if fake.worker is not None else None
    match move:
        case "deliver":
            fake.deliver()
        case "fire":
            fake.fire()
            if fake.temperature is not None and fake.temperature > 90 and fake.timer is not None:
                fake.temperature = 60.0
        case "resume":
            session = fake.db.session_id if fake.db is not None else fake.state.session_id
            fake.post(Resume(session, child_id=session + 1))
        case "thermal":
            fake.temperature = 99.0
            fake.deliver(Report(Group.THERMAL, 5.0))
        case "apparatus":
            fake.deliver(Report(Group.APPARATUS, 1.0, detail="injected"))
        case "slept":
            fake.deliver(Report(Group.SLEPT, 1.0))
        case "false_fail":
            steady = worker_spec.load is Load.HOT
            context = FailContext(True, tuple(dict(worker_spec.vector)), False) if steady else None
            fake.deliver(Report(Group.SILICON, worker_spec.seconds, context=context))
        case "evidence":
            others = [c for c in fake.edges if c != worker_spec.core]
            target = others[0] if others else worker_spec.core
            fake.deliver(Report(Group.PASS, worker_spec.seconds, evidence=(Evidence(target, True),) * 2))
        case "validate":
            fake.post(ValidateProfile(fake.db.session_id))
        case "start":
            start(fake, session_id=fake.db.session_id + 1)
        case "profile":
            fake.power = (("platform profile", "power-saver" if fake.power == BALANCED else "balanced"),)
        case "setup":
            now = dict(fake.setup)
            fake.setup = tuple(sorted({**now, "bios": "B" if now["bios"] == "A" else "A"}.items()))
        case "kernel":
            now = dict(fake.setup)
            fake.setup = tuple(sorted({**now, "kernel": f"K{int(now.get('kernel', 'K0')[1:]) + 1}"}.items()))
            fake.edges_hot = fake.edges_hot or fake.edges
            fake.edges_transient = fake.edges_transient or fake.edges
            fake.edges_memory = fake.edges_memory or fake.edges
            deepest = min(fake.edges, key=fake.edges.__getitem__)
            fake.edges = {**fake.edges, deepest: min(0, fake.edges[deepest] + 1)}
        case "outside":
            core = fake.state.baseline[0][0]
            fake.co[core] = fake.state.config.max_offset - 1
            fake.outside.add(core)
        case "continue":
            fake.post(ContinueOnSetup(fake.db.session_id + 1))
        case "restart":
            fake.post(RestartOnSetup(fake.db.session_id + 1, acknowledged=True))
        case "reengage":
            fake.state = SessionState()
            fake.post(Resume(fake.db.session_id, reengage=True))
        case "pause":
            fake.post(PauseRequest())
        case "abort":
            fake.post(Abort())
        case "close":
            fake.post(Close())
        case "crash":
            fake.crash()
        case "refuse":
            fake.faults.refuse_writes = 1
        case "dbfail":
            fake.faults.fail_commits = 1
        case "hot":
            fake.temperature = 99.0
        case "unblamed":
            fake.deliver(Report(Group.ENVIRONMENT, 1.0, unblamed_corrected=True))
        case "mce_other":
            others = [c for c in fake.edges if c != worker_spec.core]
            target = others[0] if others else worker_spec.core
            fake.deliver(Report(Group.PASS, worker_spec.seconds, evidence=(Evidence(target, False),)))
        case "crash_mce":
            fake.crash(evidence=(Evidence(worker_spec.core, False),))
        case "crash_shared":
            fake.crash(shared=True)
        case "program_fault":
            fake.deliver(replace(fake.verdict_for(worker_spec), program_faults=FOREIGN_FAULTS))
        case "paused_hot":
            context = FailContext(False, tuple(dict(worker_spec.vector)), True)
            fake.deliver(Report(Group.SILICON, worker_spec.seconds, context=context))
        case "named_unowned":
            unowned = next(core for core, _ in fake.layout.cpus_of if core not in dict(worker_spec.vector))
            fake.deliver(Report(Group.SILICON, worker_spec.seconds, named=(unowned,)))
        case "skipped":
            fake.deliver(Report(Group.PASS, 0.0, skipped="injected"))
        case "crash_mce_between":
            fake.crash(evidence=tuple(Evidence(core, False) for core, _ in fake.state.baseline))
        case _:
            raise AssertionError(move)


LONGEST_FUTURE = 2000


@dataclass
class Survey:
    states: int = 0
    leaves: dict[str, int] = field(default_factory=dict)
    deepest: int = 0
    taken: dict[str, int] = field(default_factory=dict)
    by_itself: int = 0
    bisected: set[Stage] = field(default_factory=set)
    quick_crashes: int = 0
    tightened_after_one_off: set[Stage] = field(default_factory=set)
    crashed: set[int] = field(default_factory=set)
    reached: set[str] = field(default_factory=set)


def validation_reach(node: Fake, move: str) -> str | None:
    flow = node.state.flow
    if flow is None or flow.validating is None:
        return None
    live = flow.live1
    if move == "crash" and node.worker is None and live is not None and flow.stage is Stage.LIVE1:
        done = live.index >= len(node.state.cores) and not live.requeue and live.solo is None
        return "a dead boot after the last LIVE1 test" if done else None
    if move == "skipped" and Stage.MEMORY in node.owed:
        return "a skipped MEMORY test it owes"
    if move == "mce_other":
        spec = node.worker.spec
        target = next((core for core in node.edges if core != spec.core), spec.core)
        return "a machine check at a stock core" if dict(spec.vector).get(target) == 0 else None
    return None


BETWEEN_SLOTS = "a dead boot between two slots of a bisection, every owned core at its baseline"


def explore(fake: Fake, budget: dict[str, int], cap: int) -> Survey:
    survey = Survey()
    seen: set[int] = set()
    stack = [(fake, budget, ())]
    while stack:
        node, left, path = stack.pop()
        depth = len(path)
        k = hash((key(node), tuple(sorted(left.items()))))
        if k in seen:
            continue
        seen.add(k)
        survey.states += 1
        if survey.states > cap:
            raise Broken(f"more than {cap} reachable states: the machine may not be bounded")
        if depth > LONGEST_FUTURE:
            raise Broken(
                f"a future longer than {LONGEST_FUTURE} moves: the machine may never come to rest: {path[-12:]}"
            )
        options = moves(node, left)
        if not options:
            leaf = node.state.lifecycle.value
            if node.state.pause is not None:
                leaf += ":" + node.state.pause.reason.value
            survey.leaves[leaf] = survey.leaves.get(leaf, 0) + 1
            survey.deepest = max(survey.deepest, depth)
            if not settled(node):
                raise Broken(f"a future ends at rest in {leaf}, neither completed nor quarantined: {path}")
            continue
        for move, after in options:
            survey.taken[move] = survey.taken.get(move, 0) + 1
            reach = validation_reach(node, move)
            if reach is not None:
                survey.reached.add(reach)
            child = clone(node)
            try:
                act(child, move)
            except Broken as broken:
                raise Broken(f"{broken} after {(*path, move)}") from broken
            if move == "crash_mce_between" and child.died_between_slots:
                survey.reached.add(BETWEEN_SLOTS)
            if any(line.startswith("continued: the kernel changed") for line in child.log):
                survey.by_itself += 1
            flow = child.state.flow
            if flow is not None and flow.bisect is not None:
                survey.bisected.add(flow.bisect.entry)
            if move in ("crash", "crash_mce", "crash_shared") and node.worker is not None and node.worker.spec.quick:
                survey.quick_crashes += 1
            if flow is not None and flow.stage is Stage.TIGHTEN and flow.one_off is not None:
                survey.tightened_after_one_off.add(flow.one_off[0])
            if move in ("crash", "crash_mce", "crash_shared") and node.worker is not None:
                survey.crashed.add(node.state.session_id)
            stack.append((child, after, (*path, move)))
    return survey


def settled(fake: Fake) -> bool:
    if fake.state.lifecycle in (Lifecycle.COMPLETED, Lifecycle.QUARANTINED):
        return True
    if fake.state.lifecycle is not Lifecycle.IDLE:
        return False
    if fake.db is None:
        return fake.co == fake.bios
    if fake.db.status == "completed":
        if fake.boot != fake.stored_boot:
            return fake.co == fake.bios
        return all(fake.co.get(r.core) == r.value for r in fake.db.cores)
    return fake.db.status == "quarantined"


def tiny(cores: int, edges: list[int], domains: int = 1, completed: tuple[Stored, ...] = (), **cfg) -> Fake:
    fake = fresh(
        cores,
        edges,
        domains,
        max_offset=cfg.pop("max_offset", -6),
        k_1t=cfg.pop("k_1t", 2),
        wait_between_cores_s=0.0,
        hot_seconds=1.0,
        bisect_seconds=1.0,
        stage_seconds=(("burst", 1.0), ("warm", 1.0), ("idle", 1.0)),
        transient_window_s=1.0,
        soak_seconds=1.0,
        memory_seconds=1.0,
        **cfg,
    )
    fake.completed = completed
    fake.setup = (("bios", "A"), ("kernel", "K1"))
    start(fake, session_id=1 + len(completed))
    return fake


BALANCED = (("platform profile", "balanced"),)

ONE_OF_EACH = {
    fault: 1
    for fault in FAULTS
    if fault
    not in (
        "profile",
        "setup",
        "kernel",
        "outside",
        "reengage",
        "program_fault",
        "start",
        "named_unowned",
        "skipped",
        "crash_mce_between",
    )
}


@pytest.mark.parametrize(
    ("cores", "edges", "domains", "total", "stacks"),
    [
        (1, [-3], 1, 2, ()),
        (2, [-2, 0], 2, 2, ()),
        (2, [-6, -1], 1, 1, ()),
        (2, [-3, -3], 2, 1, (((0, 1), (0, 1), 4),)),
        (3, [-2, -2, -2], 2, 1, (((0, 2), (0, 2), 3),)),
        (3, [-2, -2, -1], 2, 2, (((0, 1), (0, 1), 3),)),
        (2, [0, -3], 2, 1, (((0,), (0, 1), 2),)),
    ],
)
@pytest.mark.req(
    "ALLCORE.failed_vector.explorer",
    "C17",
    "D5",
    "K47",
    "L00.e",
    "L04.c.fb",
    "L04.g",
    "L09.G1",
    "L09.G2.group",
    "L09.a",
    "L09.g",
    "Q6",
    "RESIDENCY",
    "TERMS.room",
)
def test_every_future_of_a_tiny_cpu_ends_completed_or_quarantined_and_breaks_no_rule(
    cores, edges, domains, total, stacks
):
    fake = tiny(cores, edges, domains)
    fake.stacks = stacks
    survey = explore(fake, {**ONE_OF_EACH, "total": total}, cap=2_000_000)
    assert survey.states > 100
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.parametrize(
    ("edges", "hot", "transient", "endurance", "names"),
    [
        ([-3, -2], {0: -2, 1: -2}, None, False, False),
        ([-3, -2], None, {0: -2, 1: -1}, False, False),
        ([-3, -2], None, None, True, False),
        ([-3, -2], {0: -2, 1: -2}, None, False, True),
        ([-3, -2], None, {0: -2, 1: -1}, True, True),
        ([-5, -6], None, {0: -5, 1: -2}, False, False),
    ],
)
@pytest.mark.req("D24", "L00.g")
def test_every_future_of_a_cpu_whose_all_core_loads_fail_reaches_a_rest(edges, hot, transient, endurance, names):
    fake = tiny(2, edges, 2, endurance=endurance, endurance_window_s=2.0)
    fake.names_cores = names
    fake.edges_hot = hot
    fake.edges_transient = transient
    survey = explore(fake, {**ONE_OF_EACH, "total": 1}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.parametrize("fault", ["false_fail", "unblamed", "dbfail", "crash"])
@pytest.mark.req("SOAK.bisect.explorer")
def test_every_future_of_an_unnamed_soak_failure_bisects_it_and_completes_short_of_the_failed_vector(fault):
    survey = explore(tiny(2, [-3, -2], 2), {fault: 1, "total": 1}, cap=2_000_000)
    assert Stage.SOAK in survey.bisected
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("D116.explorer")
def test_every_future_of_a_soak_failure_whose_bisection_fails_in_two_groups_completes_short_of_both():
    fake = tiny(3, [-3, -3, -3], 2)
    while fake.worker is None or fake.worker.spec.load is not Load.SOAK:
        assert fake.progress()
    survey = explore(fake, {"false_fail": 3, "total": 3}, cap=2_000_000)
    assert Stage.SOAK in survey.bisected
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("D118.explorer")
@pytest.mark.parametrize("stage", [Stage.SOAK, Stage.LIVE1])
def test_no_future_of_three_crashes_from_a_soak_watch_or_a_first_live1_test_completes_past_a_failed_group(stage):
    fake = tiny(3, [-3, -3, -3], 2)
    while fake.worker is None or fake.worker.spec.stage is not stage:
        assert fake.progress()
    survey = explore(fake, {"crash": 3, "total": 3}, cap=2_000_000)
    assert stage in survey.bisected
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("ALLCORE.bisect.crash.explorer")
def test_every_future_of_a_crash_inside_a_quick_group_runs_the_rest_of_its_bisection_at_full_length():
    fake = tiny(3, [-4, -4, -4], 2)
    fake.edges_hot = {0: 0, 1: 0, 2: 0}
    survey = explore(fake, {"crash": 1, "crash_mce": 1, "crash_shared": 1, "total": 1}, cap=2_000_000)
    assert survey.quick_crashes > 0
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("L18.d.4.3b.between.explorer")
def test_no_dead_boot_between_the_slots_of_an_all_core_bisection_moves_a_core_that_sat_at_its_baseline():
    fake = tiny(2, [-4, -4], 2)
    fake.edges_hot = {0: 0, 1: 0}
    survey = explore(fake, {"crash_mce_between": 1, "total": 1}, cap=2_000_000)
    assert BETWEEN_SLOTS in survey.reached
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("L18.d.4.3b.between.explorer")
def test_no_dead_boot_after_the_stock_slot_of_a_live1_begun_bisection_moves_a_core_that_sat_at_its_baseline():
    fake = tiny(2, [-4, -4], 2)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1:
        assert fake.progress()
    fake.crash()
    fake.post(Resume(1))
    while fake.worker is None:
        assert fake.progress()
    assert (fake.state.flow.bisect.entry, dict(fake.worker.spec.vector)) == (Stage.LIVE1, {0: 0, 1: 0})
    fake.deliver()
    survey = explore(fake, {"crash_mce_between": 1, "total": 1}, cap=2_000_000)
    assert BETWEEN_SLOTS in survey.reached
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("L12.failed.explorer")
def test_no_future_tightens_at_or_past_an_all_core_vector_whose_failure_was_a_one_off():
    survey = explore(tiny(2, [-4, -3], 2), {"false_fail": 1, "total": 1}, cap=2_000_000)
    assert {Stage.STEADY, Stage.TRANSIENT} <= survey.tightened_after_one_off
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.parametrize("stage", [Stage.SEARCH, Stage.STEADY])
@pytest.mark.req("L18.c.marker.explorer")
def test_no_future_of_an_aborted_database_failure_reads_its_lost_verdict_into_a_later_crash(stage):
    fake = tiny(2, [-3, -2], 2)
    while fake.worker is None or fake.worker.spec.stage is not stage:
        assert fake.progress()
    fake.faults.fail_commits = 1
    fake.deliver()
    assert fake.state.pause.reason is PauseReason.DATABASE
    fake.post(Abort())
    survey = explore(fake, {"start": 1, "crash": 1, "total": 2}, cap=2_000_000)
    assert survey.crashed == {1, 2}
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


def test_every_future_of_a_cpu_whose_memory_lanes_fail_reaches_a_rest():
    fake = tiny(2, [-3, -2], 2)
    fake.edges_memory = {0: -2, 1: -1}
    survey = explore(fake, {**ONE_OF_EACH, "total": 1}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


LIVE1_DONE = "a dead boot after the last LIVE1 test"
OWED_SKIP = "a skipped MEMORY test it owes"
STOCK_RECORD = "a machine check at a stock core"


@pytest.mark.parametrize(
    ("faults", "reach"),
    [
        pytest.param(("crash", "abort"), {LIVE1_DONE}, id="a dead boot after LIVE1 then an abort"),
        pytest.param(("named_unowned", "skipped"), {OWED_SKIP}, id="a memory failure at a core not owned then a skip"),
        pytest.param(("mce_other", "abort"), {STOCK_RECORD}, id="a stock core's machine check then an abort"),
        pytest.param(
            (*ONE_OF_EACH, "named_unowned", "skipped"), {LIVE1_DONE, OWED_SKIP, STOCK_RECORD}, id="every routine fault"
        ),
    ],
)
@pytest.mark.req("L30.e.explorer")
def test_every_future_of_a_validation_with_two_faults_left_completes_owing_nothing_and_aborts_only_owing(faults, reach):
    fake = tiny(3, [-3, 0, -2], 2, endurance=True, endurance_window_s=2.0, selected=(0, 1))
    run_to_rest(fake, 10_000)
    survey = explore(fake, {**dict.fromkeys(faults, 1), "validate": 1, "total": 3}, cap=2_000_000)
    assert reach <= survey.reached
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("KERNEL.explorer")
def test_no_future_runs_a_test_on_a_setup_changed_while_the_session_was_away():
    budget = {"setup": 2, "kernel": 1, "pause": 2, "crash": 1, "close": 1, "abort": 1, "validate": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves
    assert survey.by_itself > 0


@pytest.mark.req("KERNEL.explorer", "L00.f", "L24.g", "L24.l24b.laws")
def test_a_session_continued_or_restarted_on_a_new_setup_never_runs_its_parent_again():
    budget = {"setup": 1, "kernel": 1, "pause": 1, "crash": 1, "continue": 1, "restart": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves
    assert survey.by_itself > 0


@pytest.mark.req("KERNEL.explorer")
def test_every_future_of_a_session_carried_onto_two_fresh_kernels_reaches_a_rest():
    survey = explore(tiny(2, [-3, -2], 2), {"kernel": 2, "pause": 2, "total": 4}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves
    assert survey.by_itself > 0


def test_no_future_resumes_round_a_power_state_changed_while_the_session_was_away():
    budget = {"profile": 2, "pause": 2, "crash": 1, "abort": 1, "close": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("KC.8")
def test_no_future_resumes_over_a_co_another_tool_wrote_while_the_session_was_paused():
    budget = {"outside": 2, "pause": 2, "crash": 1, "dbfail": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("L18.b.quarantine", "L18.d.8.reengage")
def test_every_future_of_a_quarantined_session_re_engaged_on_proven_ground_reaches_a_rest():
    fake = tiny(2, [-6, -4], 2, max_offset=-6, starts=((0, -5), (1, -5)))
    survey = explore(fake, {"crash": 3, "reengage": 1, "total": 4}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert survey.taken["reengage"] > 0
    assert "completed" in survey.leaves


@pytest.mark.req("D20", "L04.a.hint")
def test_every_future_of_a_session_started_from_a_completed_sessions_values_reaches_a_rest():
    earlier = tiny(2, [-2, -3], 2)
    run_to_rest(earlier, 10_000)
    fake = tiny(2, [-3, -1], 2, completed=(earlier.db,))
    assert [(r.core, r.start) for r in fake.state.cores] == [(0, -2), (1, -3)]
    unfaulted = clone(fake)
    run_to_rest(unfaulted, 10_000)
    assert (1, -3) in unfaulted.failed_at
    assert [(r.core, r.value, r.start) for r in unfaulted.db.cores] == [(0, -3, -2), (1, -1, 0)]
    survey = explore(fake, {**ONE_OF_EACH, "total": 1}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.req("PROGRAM_FAULT.explorer")
def test_no_transition_of_any_future_reads_the_program_faults_a_report_carries():
    survey = explore(tiny(2, [-3, -2], 2), {"program_fault": 2, "crash": 1, "pause": 1, "total": 3}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves
    assert survey.taken["program_fault"] > 0


@pytest.mark.req("ENTRY_READS.3", "U27")
def test_an_outside_write_while_running_is_caught_before_the_next_test():
    survey = explore(tiny(2, [-3, -2], 2), {"outside": 2, "pause": 1, "total": 2}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.slow
def test_every_future_with_a_deeper_fault_budget():
    total = int(os.environ.get("TUNER_FUTURES_FAULTS", "3"))
    cap = int(os.environ.get("TUNER_FUTURES_CAP", "5000000"))
    survey = explore(
        tiny(3, [-4, 0, -2], 2),
        {**ONE_OF_EACH, "crash": 2, "profile": 2, "setup": 2, "outside": 1, "total": total},
        cap=cap,
    )
    print(f"\nreachable states {survey.states}, leaves {survey.leaves}, longest future {survey.deepest} moves")
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}


@st.composite
def worlds(draw):
    cores = draw(st.integers(1, 6))
    domains = draw(st.integers(1, min(3, cores)))
    max_offset = draw(st.integers(-20, -3))
    edges = draw(st.lists(st.integers(max_offset, 0), min_size=cores, max_size=cores))
    smt = draw(st.booleans())
    method = draw(st.sampled_from(list(Method)))
    fake = fresh(
        cores,
        edges,
        domains,
        smt,
        max_offset=max_offset,
        k_1t=draw(st.integers(1, 4)),
        method=method,
        wait_between_cores_s=draw(st.sampled_from([0.0, 15.0])),
    )
    if order_problem(method, fake.config.selected, dict(fake.layout.domain_of)) is not None:
        fake = fresh(cores, edges, domains, smt, max_offset=max_offset, k_1t=2)
    return fake


@settings(
    max_examples=int(os.environ.get("TUNER_FUZZ_EXAMPLES", "300")),
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@pytest.mark.req("L05.g", "L18.d.5")
@given(worlds(), st.lists(st.sampled_from(("deliver", "fire", "resume", *FAULTS)), max_size=80))
def test_a_random_future_never_breaks_a_rule_and_always_comes_to_rest(fake, script):
    start(fake)
    for move in script:
        options = {name for name, _ in moves(fake, {**ONE_OF_EACH, "total": 1})}
        if move in options:
            act(fake, move)
    fake.faults.refuse_writes = 0
    fake.faults.fail_commits = 0
    fake.temperature = 60.0
    for _ in range(20_000):
        options = moves(fake, {"total": 0})
        if not options:
            break
        act(fake, options[0][0])
    else:
        raise Broken("a random future did not come to rest within 20000 moves")
    assert settled(fake), f"rest in {fake.state.lifecycle} {fake.state.pause}"
