from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corecycler.tuner.machine.session import HELD
from corecycler.tuner.machine.types import (
    ContinueOnSetup,
    FailContext,
    Lifecycle,
    Load,
    PauseReason,
    RestartOnSetup,
    ValidateProfile,
)
from corecycler.tuner.orders import Method, order_problem
from corecycler.tuner.persistence import resident_at_death
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
    fresh,
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
    "continue",
    "restart",
    "outside",
)


def clone(fake: Fake) -> Fake:
    twin = copy.copy(fake)
    twin.co = dict(fake.co)
    twin.resident = dict(fake.resident)
    twin.failed_at = set(fake.failed_at)
    twin.stock_fail_owed = set(fake.stock_fail_owed)
    twin.superseded = set(fake.superseded)
    twin.outside = set(fake.outside)
    twin.faults = copy.copy(fake.faults)
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
        fake.boot - fake.stored_boot,
        fake.faults.refuse_writes,
        fake.faults.fail_commits,
        frozenset(fake.failed_at),
        frozenset(fake.stock_fail_owed),
        frozenset(fake.superseded),
        frozenset(fake.outside),
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
        if fault in ("thermal", "apparatus", "slept", "false_fail", "evidence", "unblamed", "mce_other") and (
            fake.worker is None
        ):
            continue
        if fault in ("pause", "abort") and s.lifecycle not in (Lifecycle.RUNNING, Lifecycle.PAUSING):
            continue
        if fault in ("crash_mce", "crash_shared") and fake.worker is None:
            continue
        if fault == "validate" and (fake.db is None or fake.db.status != "completed" or s.lifecycle in HELD):
            continue
        if fault in ("close", "crash") and s.lifecycle is Lifecycle.IDLE:
            continue
        if fault in ("refuse", "dbfail", "hot") and s.lifecycle is not Lifecycle.RUNNING:
            continue
        if fault == "setup" and s.lifecycle in (Lifecycle.RUNNING, Lifecycle.PAUSING):
            continue
        if fault == "outside" and not (
            s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is not PauseReason.RESUMING
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
            fake.post(Resume(fake.db.session_id if fake.db is not None else fake.state.session_id))
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
            fake.deliver(Report(Group.PASS, worker_spec.seconds, evidence=(Evidence(target, True),)))
        case "validate":
            fake.post(ValidateProfile(fake.db.session_id))
        case "profile":
            fake.power = (("platform profile", "power-saver" if fake.power == BALANCED else "balanced"),)
        case "setup":
            fake.setup = (("bios", "B" if fake.setup == BIOS_A else "A"),)
        case "outside":
            core = fake.state.baseline[0][0]
            fake.co[core] = fake.state.config.max_offset - 1
            fake.outside.add(core)
        case "continue":
            fake.post(ContinueOnSetup(fake.db.session_id + 1))
        case "restart":
            fake.post(RestartOnSetup(fake.db.session_id + 1, acknowledged=True))
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
        case _:
            raise AssertionError(move)


LONGEST_FUTURE = 2000


@dataclass
class Survey:
    states: int = 0
    leaves: dict[str, int] = field(default_factory=dict)
    deepest: int = 0


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
            child = clone(node)
            try:
                act(child, move)
            except Broken as broken:
                raise Broken(f"{broken} after {(*path, move)}") from broken
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


def tiny(cores: int, edges: list[int], domains: int = 1, **cfg) -> Fake:
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
    start(fake)
    return fake


BALANCED = (("platform profile", "balanced"),)
BIOS_A = (("bios", "A"),)

ONE_OF_EACH = {fault: 1 for fault in FAULTS if fault not in ("profile", "setup", "outside")}


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
    ],
)
def test_every_future_of_a_cpu_whose_all_core_loads_fail_reaches_a_rest(edges, hot, transient, endurance, names):
    fake = tiny(2, edges, 2, endurance=endurance, endurance_window_s=2.0)
    fake.names_cores = names
    fake.edges_hot = hot
    fake.edges_transient = transient
    survey = explore(fake, {**ONE_OF_EACH, "total": 1}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


def test_every_future_of_a_cpu_whose_memory_lanes_fail_reaches_a_rest():
    fake = tiny(2, [-3, -2], 2)
    fake.edges_memory = {0: -2, 1: -1}
    survey = explore(fake, {**ONE_OF_EACH, "total": 1}, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


def test_no_future_runs_a_test_on_a_setup_changed_while_the_session_was_away():
    budget = {"setup": 2, "pause": 2, "crash": 1, "close": 1, "abort": 1, "validate": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


def test_a_session_continued_or_restarted_on_a_new_setup_never_runs_its_parent_again():
    budget = {"setup": 1, "pause": 1, "crash": 1, "continue": 1, "restart": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


def test_no_future_resumes_round_a_power_state_changed_while_the_session_was_away():
    budget = {"profile": 2, "pause": 2, "crash": 1, "abort": 1, "close": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


def test_no_future_resumes_over_a_co_another_tool_wrote_while_the_session_was_paused():
    budget = {"outside": 2, "pause": 2, "crash": 1, "dbfail": 1, "total": 3}
    survey = explore(tiny(2, [-3, -2], 2), budget, cap=2_000_000)
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
