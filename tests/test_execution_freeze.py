from __future__ import annotations

import math
import os
import signal
import threading
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


def frozen_while(kind: execution.Frozen, window):
    return lambda elapsed, _reading: kind if window(elapsed) else execution.Frozen.NO


def planned_while(window):
    return frozen_while(execution.Frozen.PLANNED, window)


def held_while(window):
    return frozen_while(execution.Frozen.HEAT, window)


def run_frozen(tmp_path: Path, frozen, duration: float, busy_for: float = math.inf):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), stall_timeout=0.3)
    supervisor.freeze = frozen
    cpu = StoppableCpu(supervisor, busy_for)
    with (
        patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
        patch.object(containment, "process_group_cpu_seconds", side_effect=cpu),
    ):
        return run_one(supervisor, lane(tmp_path), duration)


@pytest.mark.req("K22", "U12", "D119.ran")
@pytest.mark.real_cpu_time
def test_a_lane_frozen_for_most_of_its_run_is_neither_stalled_nor_short_of_work(tmp_path: Path):
    verdict = run_frozen(tmp_path, planned_while(lambda elapsed: 0.3 <= elapsed < 1.6), 2.0)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert verdict.frozen_seconds == pytest.approx(1.3, abs=0.1)


@pytest.mark.req("L25.d.watchdog")
@pytest.mark.real_cpu_time
def test_a_lane_that_stops_working_after_a_thaw_is_still_a_stall(tmp_path: Path):
    verdict = run_frozen(tmp_path, planned_while(lambda elapsed: 0.2 <= elapsed < 0.6), 3.0, busy_for=0.4)
    assert verdict.outcome is Outcome.STALL, verdict.detail


@pytest.mark.real_cpu_time
def test_a_lane_still_frozen_at_its_deadline_stops_cleanly_and_its_frozen_time_is_excused(tmp_path: Path):
    verdict = run_frozen(tmp_path, planned_while(lambda elapsed: elapsed >= 0.6), 1.8)
    assert verdict.outcome is Outcome.PASS, verdict.detail


@pytest.mark.req("H29")
def test_a_frozen_lane_is_really_stopped_and_runs_again_when_thawed(tmp_path: Path):
    backend = FakeBackend(_child(BUSY))
    supervisor, _, _ = make_supervisor(backend, stall_timeout=5.0)
    supervisor.freeze = planned_while(lambda elapsed: 0.3 <= elapsed < 1.0)
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


HOLD_SAYS = "the hold releases below 58.0 C on Tctl, last read 59.0 C"


def run_held(
    tmp_path: Path,
    frozen,
    duration: float,
    cap: float,
    *,
    in_wall_time: bool,
    busy_for: float = math.inf,
    timeout: Outcome = Outcome.HELD_TOO_LONG,
    stop_after: float | None = None,
):
    supervisor, stop, _ = make_supervisor(FakeBackend(_child(BUSY)), stall_timeout=0.3)
    supervisor.freeze = frozen
    supervisor.hold_cap = execution.HoldCap(cap, lambda: HOLD_SAYS, timeout, in_wall_time)
    cpu = StoppableCpu(supervisor, busy_for)
    if stop_after is not None:
        threading.Timer(stop_after, stop.set).start()
    began = time.monotonic()
    with (
        patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
        patch.object(containment, "process_group_cpu_seconds", side_effect=cpu),
    ):
        verdict = run_one(supervisor, lane(tmp_path), duration)
    return verdict, time.monotonic() - began


@pytest.mark.req("D58", "L25.d.watchdog")
@pytest.mark.real_cpu_time
def test_under_a_summed_hold_cap_the_time_a_lane_is_held_does_not_count_toward_its_run(tmp_path: Path):
    held = held_while(lambda elapsed: 0.2 <= elapsed < 0.9)
    verdict, wall = run_held(tmp_path, held, 1.0, 5.0, in_wall_time=False, timeout=Outcome.THERMAL)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert wall >= 1.6


@pytest.mark.req("D58")
@pytest.mark.real_cpu_time
def test_a_run_stopped_after_its_asked_seconds_but_before_it_made_up_its_summed_holds_is_cut_short(tmp_path: Path):
    held = held_while(lambda elapsed: 0.2 <= elapsed < 0.9)
    verdict, wall = run_held(tmp_path, held, 1.0, 5.0, in_wall_time=False, timeout=Outcome.THERMAL, stop_after=1.3)
    assert verdict.outcome is Outcome.CUT_SHORT, verdict.detail
    assert 1.2 <= wall < 1.6


@pytest.mark.req("HOLD.planned")
@pytest.mark.real_cpu_time
@pytest.mark.parametrize("in_wall_time", [False, True], ids=["summed", "in wall time"])
def test_planned_freezes_past_the_cap_neither_end_a_run_nor_lengthen_it(tmp_path: Path, in_wall_time: bool):
    planned = planned_while(lambda elapsed: 0.1 <= elapsed < 0.9)
    verdict, wall = run_held(tmp_path, planned, 1.0, 0.3, in_wall_time=in_wall_time)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert wall < 1.4


@pytest.mark.req("D58", "D119", "HOLD.words")
@pytest.mark.real_cpu_time
@pytest.mark.parametrize(
    ("timeout", "in_wall_time", "counted"),
    [(Outcome.HELD_TOO_LONG, True, "in one hold"), (Outcome.THERMAL, False, "in all")],
    ids=["one hold in wall time", "summed"],
)
def test_a_hold_longer_than_its_cap_ends_the_run_as_the_cap_declares_in_the_hold_s_own_words(
    tmp_path: Path, timeout: Outcome, in_wall_time: bool, counted: str
):
    held = held_while(lambda elapsed: elapsed >= 0.2)
    verdict, wall = run_held(tmp_path, held, 1.0, 0.5, in_wall_time=in_wall_time, timeout=timeout)
    assert verdict.outcome is timeout
    assert verdict.detail == f"the load stayed held more than 0.5 s {counted}; {HOLD_SAYS}"
    assert wall < 1.5


def test_frozen_time_stops_counting_at_the_deadline_for_the_work_floor_and_counts_in_all_for_the_result(
    tmp_path: Path,
):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)))
    run = execution._LaneRun(lane=lane(tmp_path), deadline=10.0, started_at=0.0)
    run.proc = SimpleNamespace(pid=2**22 - 1, returncode=None)
    run.frozen, run.frozen_since = True, 8.0
    supervisor._set_frozen([run], False, 12.0)
    assert run.frozen_total == 2.0 and run.frozen is False
    assert run.frozen_in_all == 4.0


@pytest.mark.req("D58")
@pytest.mark.real_cpu_time
def test_summed_holds_each_shorter_than_the_cap_end_the_run_once_their_total_passes_it(tmp_path: Path):
    held = held_while(lambda elapsed: any(start <= elapsed < start + 0.3 for start in (0.1, 0.5, 0.9, 1.3)))
    verdict, wall = run_held(tmp_path, held, 1.0, 0.5, in_wall_time=False, timeout=Outcome.THERMAL)
    assert verdict.outcome is Outcome.THERMAL
    assert verdict.detail == f"the load stayed held more than 0.5 s in all; {HOLD_SAYS}"
    assert wall < 1.5


@pytest.mark.req("D119")
@pytest.mark.real_cpu_time
def test_holds_in_wall_time_each_shorter_than_the_cap_neither_end_the_run_nor_lengthen_it(tmp_path: Path):
    held = held_while(lambda elapsed: any(start <= elapsed < start + 0.3 for start in (0.1, 0.5)))
    verdict, wall = run_held(tmp_path, held, 1.0, 0.5, in_wall_time=True)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert wall < 1.3
    assert verdict.frozen_seconds == pytest.approx(0.6, abs=0.1)


@pytest.mark.req("D119", "L25.d.watchdog")
@pytest.mark.real_cpu_time
def test_a_hold_in_wall_time_still_on_at_the_deadline_keeps_the_run_until_it_releases(tmp_path: Path):
    held = held_while(lambda elapsed: 0.7 <= elapsed < 1.3)
    verdict, wall = run_held(tmp_path, held, 1.0, 2.0, in_wall_time=True)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert 1.25 <= wall < 1.5
    assert verdict.duration_seconds - verdict.frozen_seconds == pytest.approx(0.7, abs=0.1)


@pytest.mark.req("D119", "HOLD.words")
@pytest.mark.real_cpu_time
def test_a_hold_in_wall_time_that_never_releases_ends_the_run_past_its_deadline_once_it_passes_the_cap(
    tmp_path: Path,
):
    held = held_while(lambda elapsed: elapsed >= 0.8)
    verdict, wall = run_held(tmp_path, held, 1.0, 0.5, in_wall_time=True)
    assert verdict.outcome is Outcome.HELD_TOO_LONG
    assert verdict.detail == f"the load stayed held more than 0.5 s in one hold; {HOLD_SAYS}"
    assert 1.3 <= wall < 1.8


@pytest.mark.req("D58", "D119")
@pytest.mark.real_cpu_time
@pytest.mark.parametrize("in_wall_time", [False, True], ids=["summed", "in wall time"])
def test_a_run_never_held_is_never_held_too_long_whatever_its_cap(tmp_path: Path, in_wall_time: bool):
    verdict, _ = run_held(tmp_path, held_while(lambda _elapsed: False), 0.4, 0.0, in_wall_time=in_wall_time)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert verdict.frozen_seconds == 0.0


def unread_while(window):
    return frozen_while(execution.Frozen.UNREAD, window)


UNREAD_LIMIT_SAYS = "more than the 50% limit for a load held in wall time"


@pytest.mark.req("D121", "D120.unread")
@pytest.mark.real_cpu_time
def test_missing_input_for_more_than_half_the_wall_time_is_no_verdict_at_the_deadline_never_at_the_cap(
    tmp_path: Path,
):
    unread = unread_while(lambda elapsed: any(start <= elapsed < start + 0.3 for start in (0.1, 0.5)))
    verdict, wall = run_held(tmp_path, unread, 1.0, 0.5, in_wall_time=True)
    assert verdict.outcome is Outcome.HELD_TOO_LONG
    assert verdict.detail.startswith(f"the hold's reading went unread for 0.6 of 1.0 s, {UNREAD_LIMIT_SAYS}; ")
    assert verdict.detail.endswith(HOLD_SAYS)
    assert wall >= 0.95


@pytest.mark.req("D121")
@pytest.mark.parametrize(
    ("unread", "in_wall_time", "outcome", "said"),
    [
        (10.0, True, Outcome.PASS, ""),
        (
            10.5,
            True,
            Outcome.HELD_TOO_LONG,
            f"the hold's reading went unread for 10.5 of 20.0 s, {UNREAD_LIMIT_SAYS}; {HOLD_SAYS}",
        ),
        (15.0, False, Outcome.PASS, ""),
    ],
    ids=["exactly half", "more than half", "more than half but summed"],
)
def test_a_load_held_in_wall_time_whose_reading_went_unread_more_than_half_of_it_is_no_verdict_whatever_it_ran(
    tmp_path: Path, unread: float, in_wall_time: bool, outcome: Outcome, said: str
):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)))
    supervisor.hold_cap = execution.HoldCap(300.0, lambda: HOLD_SAYS, Outcome.HELD_TOO_LONG, in_wall_time)
    run = execution._LaneRun(lane=lane(tmp_path), deadline=20.0, started_at=0.0, stopped_at=20.0)
    run.proc = SimpleNamespace(pid=2**22 - 1, returncode=0)
    run.unread.total = run.frozen_total = run.frozen_in_all = unread
    run.cpu_seconds = 20.0 - unread
    verdict = supervisor._grade(run)
    assert (verdict.outcome, verdict.detail) == (outcome, said)


@pytest.mark.req("D120.unread")
@pytest.mark.real_cpu_time
def test_missing_input_still_on_at_the_deadline_is_not_followed_past_it(tmp_path: Path):
    verdict, wall = run_held(tmp_path, unread_while(lambda elapsed: elapsed >= 0.7), 1.0, 2.0, in_wall_time=True)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert wall < 1.2


@pytest.mark.req("D120")
@pytest.mark.parametrize(
    ("frozen", "in_wall_time", "outcome", "said"),
    [
        (19.0, True, Outcome.PASS, ""),
        (
            19.5,
            True,
            Outcome.HELD_TOO_LONG,
            f"the load ran only 0.5 of 20.0 s, under the 5% floor for a load held in wall time; {HOLD_SAYS}",
        ),
        (19.5, False, Outcome.PASS, ""),
    ],
    ids=["at the floor", "under the floor", "under it but summed"],
)
def test_a_load_held_in_wall_time_must_run_five_percent_of_it_and_a_summed_hold_needs_no_share(
    tmp_path: Path, frozen: float, in_wall_time: bool, outcome: Outcome, said: str
):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)))
    supervisor.hold_cap = execution.HoldCap(300.0, lambda: HOLD_SAYS, Outcome.HELD_TOO_LONG, in_wall_time)
    run = execution._LaneRun(lane=lane(tmp_path), deadline=20.0, started_at=0.0, stopped_at=20.0)
    run.proc = SimpleNamespace(pid=2**22 - 1, returncode=0)
    run.frozen_total = run.frozen_in_all = frozen
    run.cpu_seconds = 20.0 - frozen
    verdict = supervisor._grade(run)
    assert (verdict.outcome, verdict.detail) == (outcome, said)


@pytest.mark.req("D120.killed")
def test_a_payload_killed_while_frozen_counts_its_open_freeze_as_frozen(tmp_path: Path):
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), stall_timeout=5.0)
    supervisor.freeze = planned_while(lambda elapsed: elapsed >= 0.2)
    send = supervisor._signal_group
    killers: list[threading.Timer] = []

    def killed_once_stopped(run, sig):
        send(run, sig)
        if sig is signal.SIGSTOP and not killers:
            killers.append(threading.Timer(0.4, os.killpg, (run.proc.pid, signal.SIGKILL)))
            killers[0].start()

    supervisor._signal_group = killed_once_stopped
    verdict = run_one(supervisor, lane(tmp_path), 3.0)
    assert verdict.outcome is Outcome.OUTSIDE_KILL, verdict.detail
    assert verdict.duration_seconds < 1.5
    assert verdict.duration_seconds - verdict.frozen_seconds == pytest.approx(0.2, abs=0.1)
