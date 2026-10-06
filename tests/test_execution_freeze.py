from __future__ import annotations

import math
import signal
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from corecycler.engine import containment, execution
from corecycler.engine.backends.base import Outcome
from tests.test_execution import BUSY, FakeBackend, _child, lane, make_supervisor, run_one, within

if TYPE_CHECKING:
    from pathlib import Path


def state_of(pid: int) -> str:
    with open(f"/proc/{pid}/stat") as stat:
        return stat.read().rpartition(")")[2].split()[0]


class StoppableCpu:
    def __init__(self, supervisor, busy_for: float = math.inf) -> None:
        self.first: float | None = None
        self.stopped_at: float | None = None
        self.held = 0.0
        self.busy_for = busy_for
        send = supervisor._signal_group

        def tracked(run, sig):
            send(run, sig)
            now = time.monotonic()
            if sig is signal.SIGSTOP:
                self.stopped_at = now
            elif sig is signal.SIGCONT and self.stopped_at is not None:
                self.held += now - self.stopped_at
                self.stopped_at = None

        supervisor._signal_group = tracked

    def __call__(self, _pid: int) -> float:
        now = time.monotonic()
        if self.first is None:
            self.first = now
        held = self.held + (now - self.stopped_at if self.stopped_at is not None else 0.0)
        return min(self.busy_for, now - self.first - held)


def run_frozen(tmp_path: Path, frozen, duration: float, busy_for: float = math.inf):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), stall_timeout=0.3)
    supervisor.freeze = frozen
    cpu = StoppableCpu(supervisor, busy_for)
    with (
        patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
        patch.object(containment, "process_group_cpu_seconds", side_effect=cpu),
    ):
        return run_one(supervisor, lane(tmp_path), duration)


@pytest.mark.real_cpu_time
def test_a_lane_frozen_for_most_of_its_run_is_neither_stalled_nor_short_of_work(tmp_path: Path):
    verdict = run_frozen(tmp_path, lambda elapsed: 0.3 <= elapsed < 1.6, 2.0)
    assert verdict.outcome is Outcome.PASS, verdict.detail


@pytest.mark.real_cpu_time
def test_a_lane_that_stops_working_after_a_thaw_is_still_a_stall(tmp_path: Path):
    verdict = run_frozen(tmp_path, lambda elapsed: 0.2 <= elapsed < 0.6, 3.0, busy_for=0.4)
    assert verdict.outcome is Outcome.STALL, verdict.detail


@pytest.mark.real_cpu_time
def test_a_lane_still_frozen_at_its_deadline_stops_cleanly_and_its_frozen_time_is_excused(tmp_path: Path):
    verdict = run_frozen(tmp_path, lambda elapsed: elapsed >= 0.6, 1.8)
    assert verdict.outcome is Outcome.PASS, verdict.detail


def test_a_frozen_lane_is_really_stopped_and_runs_again_when_thawed(tmp_path: Path):
    backend = FakeBackend(_child(BUSY))
    supervisor, _, _ = make_supervisor(backend, stall_timeout=5.0)
    supervisor.freeze = lambda elapsed: 0.3 <= elapsed < 1.0
    states: list[tuple[str, str]] = []
    send = supervisor._signal_group

    def recorded(run, sig):
        send(run, sig)
        if sig in (signal.SIGSTOP, signal.SIGCONT):
            within(1.0, lambda: state_of(run.proc.pid) == ("T" if sig is signal.SIGSTOP else "R"))
            states.append((sig.name, state_of(run.proc.pid)))

    supervisor._signal_group = recorded
    verdict = run_one(supervisor, lane(tmp_path), 1.6)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert states[:2] == [("SIGSTOP", "T"), ("SIGCONT", "R")]


def run_held(tmp_path: Path, frozen, duration: float, cap: float | None, busy_for: float = math.inf):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), stall_timeout=0.3)
    supervisor.freeze = frozen
    supervisor.hold_cap_s = cap
    cpu = StoppableCpu(supervisor, busy_for)
    began = time.monotonic()
    with (
        patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
        patch.object(containment, "process_group_cpu_seconds", side_effect=cpu),
    ):
        verdict = run_one(supervisor, lane(tmp_path), duration)
    return verdict, time.monotonic() - began


@pytest.mark.real_cpu_time
def test_under_a_hold_cap_the_time_a_lane_is_held_does_not_count_toward_its_run(tmp_path: Path):
    verdict, wall = run_held(tmp_path, lambda elapsed: 0.2 <= elapsed < 0.9, 1.0, 5.0)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert wall >= 1.6


@pytest.mark.real_cpu_time
def test_a_hold_longer_than_its_cap_ends_the_run_as_thermal(tmp_path: Path):
    verdict, wall = run_held(tmp_path, lambda elapsed: elapsed >= 0.2, 1.0, 0.5)
    assert verdict.outcome is Outcome.THERMAL
    assert "the load stayed held more than 0.5 s waiting for the CPU to cool" in verdict.detail
    assert wall < 1.5


def test_frozen_time_stops_counting_at_the_deadline(tmp_path: Path):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)))
    run = execution._LaneRun(lane=lane(tmp_path), deadline=10.0, started_at=0.0)
    run.proc = SimpleNamespace(pid=2**22 - 1, returncode=None)
    run.frozen, run.frozen_since = True, 8.0
    supervisor._set_frozen([run], False, 12.0)
    assert run.frozen_total == 2.0 and run.frozen is False
