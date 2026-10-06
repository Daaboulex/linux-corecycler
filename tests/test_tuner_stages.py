from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field

import pytest

from corecycler.engine import detector as kernel
from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.backends.ycruncher import ALL_TESTS
from corecycler.tuner import live_hw, stages
from corecycler.tuner.machine.types import Group, Klass, Load, Stage
from corecycler.tuner.machine.types import TestSpec as Spec

CPUS = tuple(range(8))
FIRSTS = (0, 1, 2, 3)
CORE_OF_CPU = {cpu: cpu % 4 for cpu in CPUS}


def plan(load: Load, seconds: float = 480.0, seed: int = 1):
    return stages.plan(load, seconds, CPUS, FIRSTS, 60_000, random.Random(seed))


def test_hot_is_one_all_cpu_segment_of_every_test_with_the_hot_hold():
    (hot,) = plan(Load.HOT, 510.0)
    assert (hot.cpus, hot.tests, hot.seconds_per_test, hot.duration) == (CPUS, ALL_TESTS, 60, 510.0)
    assert hot.memory_mb == 16384 and hot.hold == (95.0, 85.0) and hot.frozen == ()
    (small,) = stages.plan(Load.HOT, 10.0, CPUS, FIRSTS, 4000, random.Random(1))
    assert small.memory_mb == 2000


def test_warm_is_one_sixty_second_slice_per_test_in_the_record_order_with_the_warm_hold():
    slices = plan(Load.WARM, 480.0)
    assert [s.tests for s in slices] == [(test,) for test in ALL_TESTS]
    assert all(s.duration == 60.0 and s.hold == (65.0, 55.0) and s.cpus == CPUS for s in slices)
    assert len(plan(Load.WARM, 150.0)) == 3 and plan(Load.WARM, 150.0)[-1].duration == 30.0


@pytest.mark.parametrize("seed", range(20))
def test_burst_restarts_every_40_to_80_seconds_and_alternates_short_runs_and_freezes(seed):
    segments = plan(Load.BURST, 480.0, seed)
    assert sum(s.duration for s in segments) == pytest.approx(480.0)
    assert all(40.0 <= s.duration <= 80.0 for s in segments[:-1])
    for index, s in enumerate(segments):
        assert ALL_TESTS[-len(s.tests) :] == s.tests
        assert s.cpus == CPUS or (len(s.cpus) in (2,) and set(s.cpus) <= set(FIRSTS))
        first = s.frozen[0][0]
        assert first == 3.0 if index == 0 else 1.0 <= first <= 2.0
        for (start, stop), (after, _) in zip(s.frozen, s.frozen[1:], strict=False):
            assert 0.3 <= stop - start <= 4.0 and 0.2 <= after - stop <= 3.0


@pytest.mark.parametrize("seed", range(20))
def test_idle_wakes_a_few_cores_for_a_moment_between_long_freezes(seed):
    (idle,) = plan(Load.IDLE, 360.0, seed)
    assert len(idle.cpus) in (1, 2, 4) and set(idle.cpus) <= set(FIRSTS)
    assert (idle.seconds_per_test, idle.memory_mb) == (10, 1024)
    assert 0.1 <= idle.frozen[0][0] <= 0.4
    for (start, stop), (after, _) in zip(idle.frozen, idle.frozen[1:], strict=False):
        assert 15.0 <= stop - start <= 40.0 and 0.1 <= after - stop <= 0.4
    assert stages.frozen_at(idle, idle.frozen[0][0] + 1.0) and not stages.frozen_at(idle, 0.0)


def test_a_plan_is_reproducible_from_its_seed_and_refuses_a_per_core_load():
    assert plan(Load.BURST, 480.0, 5) == plan(Load.BURST, 480.0, 5)
    with pytest.raises(ValueError):
        plan(Load.ONE_T)


def test_the_hold_freezes_at_the_hot_mark_and_releases_below_the_cool_one():
    readings = iter([90.0, 96.0, 88.0, None, 84.0])
    hold = live_hw.Hold(plan(Load.HOT)[0], lambda: next(readings))
    assert [hold(0.0) for _ in range(5)] == [False, True, True, True, False]


def test_free_memory_is_read_from_meminfo(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 64000000 kB\nMemAvailable: 32768000 kB\n")
    assert live_hw.free_mb(meminfo) == 32000
    meminfo.write_text("MemTotal: 1 kB\n")
    with pytest.raises(OSError):
        live_hw.free_mb(meminfo)


@dataclass
class Script:
    outcomes: list
    events: list = field(default_factory=list)
    seen: list = field(default_factory=list)


def runner(script: Script, readings=None, detector=None):
    class FakeSupervisor:
        def __init__(self, **kwargs):
            self.freeze = kwargs["freeze"]
            self.observed = kwargs["observed"]

        def run(self, lanes, cfg, duration):
            outcome = script.outcomes.pop(0)
            script.seen.append((lanes[0].cpus, cfg.tests, duration, self.freeze))
            self.freeze(duration / 2)
            self.observed.extend(script.events.pop(0) if script.events else [])
            elapsed = duration if outcome is Outcome.PASS else duration / 2
            return {lanes[0].core_id: StressResult(lanes[0].core_id, outcome, "d", elapsed)}

    class FakeDetector:
        def reset(self):
            pass

        def close(self):
            pass

    return live_hw.YCruncherRunner(
        lambda: None,
        __import__("pathlib").Path("/nonexistent"),
        CORE_OF_CPU,
        FakeSupervisor,
        detector=detector or FakeDetector,
        read_temperature=lambda: 70.0 if readings is None else readings(),
        available_mb=lambda: 60_000,
    )


def spec(load: Load, seconds: float = 480.0) -> Spec:
    return Spec(Klass.T1, 0, -5, CPUS, seconds, tuple((c, -5) for c in range(4)), Stage.TRANSIENT, load)


def test_an_all_core_stage_runs_every_segment_and_passes():
    script = Script([Outcome.PASS] * 8)
    report = runner(script).run(spec(Load.WARM), threading.Event())
    assert report.group is Group.PASS and report.seconds == pytest.approx(480.0)
    assert [tests for _, tests, _, _ in script.seen] == [(test,) for test in ALL_TESTS]


def test_a_failing_segment_reports_its_cores_and_whether_the_load_was_paused():
    script = Script([Outcome.PASS, Outcome.COMPUTATION])
    report = runner(script).run(spec(Load.HOT, 510.0), threading.Event())
    assert report.group is Group.PASS
    script = Script([Outcome.COMPUTATION])
    report = runner(script).run(spec(Load.HOT, 510.0), threading.Event())
    assert report.group is Group.SILICON
    assert report.context.steady is True and report.context.working == (0, 1, 2, 3)
    assert report.context.tctl == 70.0


def test_a_hot_failure_while_held_for_heat_is_not_a_steady_failure():
    script = Script([Outcome.COMPUTATION])
    readings = iter([97.0, 80.0])
    report = runner(script, lambda: next(readings)).run(spec(Load.HOT, 510.0), threading.Event())
    assert report.group is Group.SILICON
    assert report.context.paused is True and report.context.steady is False


def test_a_core_bank_record_in_an_all_core_run_names_its_core_even_inside_the_lane():
    record = kernel.CoreMachineCheck(cpu=6, bank=1, corrected=True, message="m", raw_ts=0.0)
    script = Script([Outcome.PASS], events=[[record]])
    report = runner(script).run(spec(Load.HOT, 510.0), threading.Event())
    assert [(e.core, e.corrected) for e in report.evidence] == [(2, True)]


def test_an_unblamed_record_in_a_passing_segment_does_not_end_the_stage():
    record = kernel.UnblamedMachineCheck(logged_by_cpu=1, bank=5, corrected=True, message="u", raw_ts=0.0)
    script = Script([Outcome.PASS] * 8, events=[[record]])
    assert runner(script).run(spec(Load.WARM), threading.Event()).group is Group.PASS


def soak_detector(reads):
    class Detector:
        def reset(self):
            pass

        def close(self):
            pass

        def read_new(self, final=False):
            return reads.pop(0) if reads else kernel.KernelRead((), None)

    return Detector


@pytest.mark.parametrize(
    ("events", "problem", "group"),
    [
        ((), None, Group.PASS),
        ((kernel.CoreMachineCheck(cpu=5, bank=1, corrected=True, message="m", raw_ts=0.0),), None, Group.PASS),
        (
            (kernel.UnblamedMachineCheck(logged_by_cpu=1, bank=5, corrected=False, message="u", raw_ts=0.0),),
            None,
            Group.ENVIRONMENT,
        ),
        ((), "kmsg unreadable", Group.APPARATUS),
    ],
)
def test_the_soak_watch_reports_what_the_kernel_logged(events, problem, group):
    reads = [kernel.KernelRead(events, problem)]
    report = runner(Script([]), detector=soak_detector(reads)).run(spec(Load.SOAK, 0.05), threading.Event())
    assert report.group is group
    if events and isinstance(events[0], kernel.CoreMachineCheck):
        assert [e.core for e in report.evidence] == [1]


def test_a_stopped_soak_is_no_verdict():
    stop = threading.Event()
    stop.set()
    report = runner(Script([]), detector=soak_detector([])).run(spec(Load.SOAK, 5.0), stop)
    assert report.group is Group.APPARATUS and "stopped" in report.detail
