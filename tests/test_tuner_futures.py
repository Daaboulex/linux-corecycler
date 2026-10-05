from __future__ import annotations

import copy
import os
from collections import deque
from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corecycler.tuner.machine.types import Lifecycle, PauseReason
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
)


def clone(fake: Fake) -> Fake:
    twin = copy.copy(fake)
    twin.co = dict(fake.co)
    twin.resident = dict(fake.resident)
    twin.failed_at = set(fake.failed_at)
    twin.stock_fail_owed = set(fake.stock_fail_owed)
    twin.faults = copy.copy(fake.faults)
    twin.log = []
    return twin


def key(fake: Fake) -> tuple:
    return (
        fake.state,
        tuple(sorted(fake.co.items())),
        tuple(sorted(fake.resident.items())),
        fake.db,
        fake.worker,
        fake.timer,
        fake.now,
        fake.temperature,
        fake.boot - fake.stored_boot,
        fake.faults.refuse_writes,
        fake.faults.fail_commits,
        frozenset(fake.failed_at),
        frozenset(fake.stock_fail_owed),
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
        if fault in ("thermal", "apparatus", "slept", "false_fail", "evidence") and fake.worker is None:
            continue
        if fault in ("pause", "abort") and s.lifecycle not in (Lifecycle.RUNNING, Lifecycle.PAUSING):
            continue
        if fault in ("close", "crash") and s.lifecycle is Lifecycle.IDLE:
            continue
        if fault in ("refuse", "dbfail", "hot") and s.lifecycle is not Lifecycle.RUNNING:
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
            fake.deliver(Report(Group.SILICON, worker_spec.seconds))
        case "evidence":
            others = [c for c in fake.edges if c != worker_spec.core]
            target = others[0] if others else worker_spec.core
            fake.deliver(Report(Group.PASS, worker_spec.seconds, evidence=(Evidence(target, True),)))
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
        case _:
            raise AssertionError(move)


@dataclass
class Survey:
    states: int = 0
    leaves: dict[str, int] = field(default_factory=dict)
    deepest: int = 0


def explore(fake: Fake, budget: dict[str, int], cap: int) -> Survey:
    survey = Survey()
    seen: set = set()
    queue = deque([(fake, budget, ())])
    while queue:
        node, left, path = queue.popleft()
        depth = len(path)
        k = (key(node), tuple(sorted(left.items())))
        if k in seen:
            continue
        seen.add(k)
        survey.states += 1
        if survey.states > cap:
            raise Broken(f"more than {cap} reachable states: the machine may not be bounded")
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
            queue.append((child, after, (*path, move)))
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
        **cfg,
    )
    start(fake)
    return fake


ONE_OF_EACH = {fault: 1 for fault in FAULTS}


@pytest.mark.parametrize(
    ("cores", "edges", "domains", "total"),
    [(1, [-3], 1, 2), (2, [-2, 0], 2, 2), (2, [-6, -1], 1, 1)],
)
def test_every_future_of_a_tiny_cpu_ends_completed_or_quarantined_and_breaks_no_rule(cores, edges, domains, total):
    survey = explore(tiny(cores, edges, domains), {**ONE_OF_EACH, "total": total}, cap=400_000)
    assert survey.states > 100
    assert set(survey.leaves) <= {"completed", "quarantined", "idle"}
    assert "completed" in survey.leaves


@pytest.mark.slow
def test_every_future_with_a_deeper_fault_budget():
    total = int(os.environ.get("TUNER_FUTURES_FAULTS", "3"))
    survey = explore(tiny(3, [-4, 0, -2], 2), {**ONE_OF_EACH, "crash": 2, "total": total}, cap=5_000_000)
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
