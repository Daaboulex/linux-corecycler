from __future__ import annotations

import contextlib
import itertools
import json
import random
import re
import threading
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecycler.config import tools
from corecycler.engine import detector as kernel
from corecycler.engine.backends import ycruncher
from corecycler.engine.backends.base import Outcome, StressConfig, StressResult
from corecycler.engine.backends.ycruncher import ALL_TESTS
from corecycler.engine.execution import Frozen, Heartbeat, Lane, Supervisor, _LaneRun
from corecycler.engine.memory_budget import MemoryBudgetUnavailable
from corecycler.monitor.hwmon import SensorRead
from corecycler.tuner import live_hw, stages, telemetry
from corecycler.tuner.machine.types import Group, Klass, Lifecycle, Load, MachineConfig, Mode, PauseReason, Stage
from corecycler.tuner.machine.types import TestSpec as Spec
from corecycler.tuner.orders import Method
from tests._tuner_world import fresh
from tests._tuner_world import start as start_session
from tests.test_execution import FakeBackend, YCruncherByItsWallClock, _child

CPUS = tuple(range(8))


def at(temperature: float | None) -> SensorRead:
    return SensorRead(hottest=temperature, tctl=temperature)


FIRSTS = (0, 1, 2, 3)
CORE_OF_CPU = {cpu: cpu % 4 for cpu in CPUS}


def plan(load: Load, seconds: float = 480.0, seed: int = 1, rest_c: float = 48.0):
    return stages.plan(load, seconds, CPUS, FIRSTS, 60_000, random.Random(seed), 95.0, quick=False, rest_c=rest_c)


@pytest.mark.req("K24")
def test_hot_is_one_all_cpu_segment_of_every_test_with_the_hot_hold():
    (hot,) = plan(Load.HOT, 510.0)
    assert (hot.cpus, hot.tests, hot.seconds_per_test, hot.duration) == (CPUS, ALL_TESTS, 60, 510.0)
    assert hot.memory_mb == 16384 and hot.hold == (95.0, 85.0) and hot.frozen == ()
    (small,) = stages.plan(Load.HOT, 10.0, CPUS, FIRSTS, 4000, random.Random(1), 95.0, quick=False)
    assert small.memory_mb == 2000


@pytest.mark.req("K26", "L25.g")
def test_warm_is_one_sixty_second_slice_per_test_in_the_record_order_with_the_warm_hold():
    slices = plan(Load.WARM, 480.0)
    assert [s.tests for s in slices] == [(test,) for test in ALL_TESTS]
    assert all(s.duration == 60.0 and s.hold == (65.0, 55.0) and s.cpus == CPUS for s in slices)
    assert len(plan(Load.WARM, 150.0)) == 3 and plan(Load.WARM, 150.0)[-1].duration == 30.0


@pytest.mark.req("K26", "WARM.rest")
@pytest.mark.parametrize(
    ("rest_c", "band"),
    [(40.0, (65.0, 55.0)), (53.0, (65.0, 55.0)), (56.0, (68.0, 58.0)), (60.0, (72.0, 62.0))],
    ids=["cool", "at the margin", "the test machine's Tctl at rest", "warmer"],
)
def test_the_warm_release_point_is_never_below_two_degrees_over_the_rest_and_the_band_keeps_its_span(rest_c, band):
    assert {s.hold for s in plan(Load.WARM, rest_c=rest_c)} == {band}


def test_a_warm_plan_without_a_rest_temperature_is_refused():
    with pytest.raises(ValueError, match="no rest temperature"):
        stages.plan(Load.WARM, 480.0, CPUS, FIRSTS, 60_000, random.Random(1), 95.0, quick=False)


@pytest.mark.req("WARM.tctl")
def test_the_warm_hold_judges_tctl_and_every_hot_hold_the_hottest_input():
    hold = live_hw.Hold(plan(Load.WARM, rest_c=56.0)[0], dict, CORE_OF_CPU, 0.25)
    readings = [
        SensorRead(hottest=75.0, tctl=67.0),
        SensorRead(hottest=60.0, tctl=68.0),
        SensorRead(hottest=75.0, tctl=58.0),
        SensorRead(hottest=60.0, tctl=57.9),
    ]
    assert [hold(0.0, reading) for reading in readings] == [Frozen.NO, Frozen.HEAT, Frozen.HEAT, Frozen.NO]
    assert {s.hold_on_tctl for load in (Load.HOT, Load.BURST, Load.IDLE) for s in plan(load)} == {False}


@pytest.mark.req("D120.unread", "D121", "WARM.tctl.unread")
def test_an_unread_tctl_is_missing_input_held_or_not_and_leaves_the_hold_where_it_was():
    unread = SensorRead(hottest=60.0)
    held = live_hw.Hold(plan(Load.WARM)[0], dict, CORE_OF_CPU, 0.25)
    readings = [unread, at(60.0), at(70.0), unread, at(60.0), at(50.0)]
    expected = [Frozen.UNREAD, Frozen.NO, Frozen.HEAT, Frozen.UNREAD, Frozen.HEAT, Frozen.NO]
    assert [held(0.0, reading) for reading in readings] == expected
    hot = live_hw.Hold(plan(Load.HOT)[0], dict, CORE_OF_CPU, 0.25)
    assert hot(0.0, SensorRead(hottest=None, tctl=None)) is Frozen.NO


@pytest.mark.req("HOLD.words")
def test_a_hold_names_its_release_point_the_reading_it_judges_and_its_last_value():
    warm = live_hw.Hold(plan(Load.WARM, rest_c=56.0)[0], dict, CORE_OF_CPU, 0.25)
    assert warm.describe() == "the hold releases below 58.0 C on Tctl, last read unread"
    warm(0.0, SensorRead(hottest=60.0, tctl=57.0))
    assert warm.describe() == "the hold releases below 58.0 C on Tctl, last read 57.0 C"
    warm(0.1, SensorRead(hottest=60.0))
    assert warm.describe() == "the hold releases below 58.0 C on Tctl, last read unread"
    hot = live_hw.Hold(plan(Load.HOT)[0], dict, CORE_OF_CPU, 0.25)
    hot(0.0, at(88.4))
    assert hot.describe() == "the hold releases below 85.0 C on the hottest input, last read 88.4 C"


@pytest.mark.req("WARM.rest.unread")
def test_a_warm_test_whose_tctl_is_unread_before_its_load_runs_nothing_and_gives_no_verdict():
    script = Script([Outcome.PASS] * 8)
    warm = runner(script)
    warm.sensors = lambda: SensorRead(hottest=60.0)
    report = warm.run(spec(Load.WARM), threading.Event())
    assert (report.group, report.seconds) == (Group.APPARATUS, 0.0)
    assert "Tctl could not be read" in report.detail
    assert script.seen == []


@pytest.mark.req("D58")
def test_a_warm_slice_times_out_with_no_verdict_and_every_hot_hold_as_a_thermal_stop():
    assert {s.hold_timeout for s in plan(Load.WARM)} == {Outcome.HELD_TOO_LONG}
    hot_holds = {s.hold_timeout for load in (Load.HOT, Load.BURST, Load.IDLE) for s in plan(load)}
    assert hot_holds == {Outcome.THERMAL}


@pytest.mark.req("D119", "D58")
def test_a_warm_slice_holds_inside_its_wall_time_and_every_hot_hold_counts_run_time():
    assert {s.hold_in_wall_time for s in plan(Load.WARM)} == {True}
    hot_holds = {s.hold_in_wall_time for load in (Load.HOT, Load.BURST, Load.IDLE) for s in plan(load)}
    assert hot_holds == {False}


@pytest.mark.req("WARM.rest")
@pytest.mark.parametrize(
    ("limit_c", "rest_c", "band"),
    [(95.0, 84.0, (85.0, 75.0)), (89.0, 70.0, (79.0, 69.0))],
    ids=["after a burst at 95 C", "at an 89 C limit"],
)
def test_the_warm_band_never_reaches_into_the_hot_band_whatever_the_tctl_read_before_its_load(limit_c, rest_c, band):
    slices = stages.plan(Load.WARM, 480.0, CPUS, FIRSTS, 60_000, random.Random(1), limit_c, quick=False, rest_c=rest_c)
    (hot,) = stages.plan(Load.HOT, 510.0, CPUS, FIRSTS, 60_000, random.Random(1), limit_c, quick=False)
    assert {s.hold for s in slices} == {band}
    assert band[0] <= hot.hold[1]


@pytest.mark.req("K27", "L25.d.burst")
@pytest.mark.parametrize("seed", range(20))
def test_burst_restarts_at_60_seconds_then_every_40_to_80_and_alternates_short_runs_and_freezes(seed):
    segments = plan(Load.BURST, 480.0, seed)
    assert sum(s.duration for s in segments) == pytest.approx(480.0)
    assert segments[0].duration == 60.0
    assert all(40.0 <= s.duration <= 80.0 for s in segments[1:-1])
    for index, s in enumerate(segments):
        assert ALL_TESTS[-len(s.tests) :] == s.tests
        assert s.cpus == CPUS or (len(s.cpus) in (2, 4) and set(s.cpus) <= set(FIRSTS))
        first = s.frozen[0][0]
        assert first == 3.0 if index == 0 else first in (1.0, 2.0)
        for (start, stop), (after, _) in zip(s.frozen, s.frozen[1:], strict=False):
            assert 0.3 <= stop - start <= 4.0 and 0.2 <= after - stop <= 3.0


@pytest.mark.req("K28", "L25.d.idle")
@pytest.mark.parametrize("seed", range(20))
def test_idle_wakes_a_few_cores_for_a_moment_between_long_freezes(seed):
    (idle,) = plan(Load.IDLE, 360.0, seed)
    assert len(idle.cpus) in (1, 2, 4) and set(idle.cpus) <= set(FIRSTS)
    assert (idle.seconds_per_test, idle.memory_mb) == (10, 1024)
    assert 0.1 <= idle.frozen[0][0] <= 0.4
    for (start, stop), (after, _) in zip(idle.frozen, idle.frozen[1:], strict=False):
        assert 15.0 <= stop - start <= 40.0 and 0.1 <= after - stop <= 0.4
    assert stages.frozen_at(idle, idle.frozen[0][0] + 1.0) and not stages.frozen_at(idle, 0.0)


@pytest.mark.req("K29.burst_idle")
@pytest.mark.parametrize("load", [Load.BURST, Load.IDLE])
def test_burst_and_idle_freeze_at_the_thermal_limit_and_thaw_10_c_under_it_as_hot_does(load):
    assert {s.hold for s in plan(load)} == {(95.0, 85.0)}


def test_a_plan_is_reproducible_from_its_seed_and_refuses_a_per_core_load():
    assert plan(Load.BURST, 480.0, 5) == plan(Load.BURST, 480.0, 5)
    with pytest.raises(ValueError):
        plan(Load.ONE_T)


@pytest.mark.req("K29", "L25.d.hold")
def test_the_hold_freezes_at_the_hot_mark_and_releases_below_the_cool_one():
    readings = iter([90.0, 96.0, 88.0, None, 84.0])
    hold = live_hw.Hold(plan(Load.HOT)[0], dict, CORE_OF_CPU, 0.25)
    assert [hold(0.0, at(next(readings))) for _ in range(5)] == [Frozen.NO, *[Frozen.HEAT] * 3, Frozen.NO]


@pytest.mark.req("HOLD.planned", "D120.unread")
def test_a_hot_hold_released_inside_a_planned_rest_ends_its_heat_time_there(tmp_path):
    burst = plan(Load.BURST)[0]
    rest_starts, rest_ends = burst.frozen[0]
    hold = live_hw.Hold(burst, dict, CORE_OF_CPU, 0.25)
    run = _LaneRun(lane=Lane(core_id=0, cpus=CPUS, work_dir=tmp_path), deadline=60.0)
    run.proc = SimpleNamespace(pid=2**22 - 1, returncode=None)
    hot = hold(0.0, at(96.0))
    Supervisor._set_held([run], hot, 1.0)
    released = hold((rest_starts + rest_ends) / 2, at(80.0))
    Supervisor._set_held([run], released, 2.0)
    assert (hot, released) == (Frozen.HEAT, Frozen.PLANNED)
    assert (run.heat.since, run.heat.total) == (None, 1.0)


@dataclass
class Script:
    outcomes: list
    events: list = field(default_factory=list)
    seen: list = field(default_factory=list)
    paces: list = field(default_factory=list)
    output: str = ""
    sampled: bool = True
    caps: list = field(default_factory=list)
    memory: list = field(default_factory=list)
    limits: list = field(default_factory=list)
    frozen_share: float = 0.0
    configs: list = field(default_factory=list)
    must_finish: list = field(default_factory=list)


def runner(script: Script, readings=None, detector=None, clocks=None, usable_mb=60_000):
    class FakeSupervisor:
        def __init__(self, **kwargs):
            self.freeze = kwargs["freeze"]
            self.sensors = kwargs["sensors"]
            script.caps.append(kwargs["hold_cap"])
            script.must_finish.append(kwargs["must_finish_a_test"])
            script.limits.append(kwargs["thermal"].max_temperature)
            self.observed = kwargs["observed"]

        def run(self, lanes, cfg, duration):
            outcome = script.outcomes.pop(0)
            script.seen.append((lanes[0].cpus, cfg.tests, duration, self.freeze))
            script.paces.append(cfg.seconds_per_test)
            script.memory.append(cfg.memory_mb)
            script.configs.append(cfg)
            if script.sampled:
                self.freeze(duration / 2, self.sensors())
            self.observed.extend(script.events.pop(0) if script.events else [])
            elapsed = duration if outcome is Outcome.PASS else duration / 2
            result = StressResult(
                lanes[0].core_id,
                outcome,
                "d",
                elapsed,
                output=script.output,
                speeds=((f"seg{len(script.seen)}", 1e9, outcome is Outcome.PASS),),
                frozen_seconds=script.frozen_share * elapsed,
            )
            return {lanes[0].core_id: result}

    class FakeDetector:
        read_started_ns = None

        def reset(self):
            pass

        def close(self):
            pass

    return live_hw.StressRunner(
        lambda: None,
        Path("/nonexistent"),
        CORE_OF_CPU,
        lambda: at(70.0 if readings is None else readings()),
        clocks or (lambda: dict.fromkeys(CPUS, 5000.0)),
        supervisor=FakeSupervisor,
        detector=detector or FakeDetector,
        budget=lambda n, floor: Budget(usable_mb=usable_mb),
    )


def spec(load: Load, seconds: float = 480.0) -> Spec:
    vector = tuple((c, -5) for c in range(4))
    return Spec(Klass.T1, 0, -5, CPUS, seconds, vector, Stage.TRANSIENT, load, 95.0, plan_seed="1")


@pytest.mark.req("T22.F20")
def test_hot_gets_half_the_usable_memory_up_to_its_cap_and_never_0_mb():
    cap = stages.HOT_MEMORY_CAP_MB
    usable = (1, 3, 4000, 4 * cap)
    assert [stages.hot_memory_mb(mb) for mb in usable] == [1, 3 // 2, 4000 // 2, cap]


@pytest.mark.req("T22.F20")
def test_each_hot_run_logs_the_memory_its_own_plan_gave_y_cruncher():
    usable = iter([4000, 3000])
    said = []
    script = Script([Outcome.PASS] * 2)
    hot = runner(script)
    hot.budget = lambda n, floor: Budget(usable_mb=next(usable))
    hot.say = said.append
    for _ in range(2):
        hot.run(spec(Load.HOT), threading.Event())
    assert script.memory == [4000 // 2, 3000 // 2]
    assert said == [f"hot runs y-cruncher with {mb} MB" for mb in script.memory]


def test_every_sample_of_an_all_core_stage_reaches_the_sink():
    got = []
    script = Script([Outcome.PASS] * 8)
    runner(script).run(spec(Load.WARM), threading.Event(), got.append)
    assert len(got) == len(script.seen)
    assert all(s.mono_ns > 0 and s.tctl == 70.0 and s.mhz for s in got)


def test_a_single_core_test_is_sampled_only_when_a_sink_is_given():
    one = Spec(Klass.T1, 0, -5, (0,), 60.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0)
    got = []
    script = Script([Outcome.PASS])
    runner(script).run(one, threading.Event(), got.append)
    assert [s.mhz for s in got] == [tuple((core, 5000.0) for core in range(4))]
    unsampled = Script([Outcome.PASS], sampled=False)
    runner(unsampled).run(one, threading.Event())
    assert unsampled.seen[0][3] is not None


@pytest.mark.req("K40.slot", "D40.slot", "L09.retest.replay")
def test_a_quick_hot_slot_runs_every_test_at_twenty_seconds_and_steady_and_its_replay_keep_sixty():
    (quick,) = stages.plan(Load.HOT, 160.0, CPUS, FIRSTS, 60_000, random.Random(1), 95.0, quick=True)
    assert quick.tests == ALL_TESTS and quick.seconds_per_test * len(quick.tests) == quick.duration
    slot, steady, replay = Script([Outcome.PASS]), Script([Outcome.PASS]), Script([Outcome.PASS])
    bisect = replace(spec(Load.HOT, 160.0), stage=Stage.BISECT, quick=True)
    runner(slot).run(bisect, threading.Event())
    runner(steady).run(replace(bisect, stage=Stage.STEADY, seconds=510.0, quick=False), threading.Event())
    runner(replay).run(replace(bisect, seconds=510.0, quick=False), threading.Event())
    assert (slot.paces, steady.paces, replay.paces) == ([20], [60], [60])


@pytest.mark.req("L09.retest.seed")
def test_a_planned_test_draws_its_pattern_from_its_plan_seed_alone():
    script = Script([Outcome.PASS] * 60)
    burst = runner(script)
    drawn = []
    for seed in ("a", "a", "b"):
        before = len(script.seen)
        burst.run(replace(spec(Load.BURST), plan_seed=seed), threading.Event())
        drawn.append([(cpus, tests, duration) for cpus, tests, duration, _ in script.seen[before:]])
    assert drawn[0] == drawn[1] != drawn[2]


def test_a_planned_test_without_a_plan_seed_is_refused_before_anything_runs():
    script = Script([Outcome.PASS])
    with pytest.raises(ValueError, match="plan seed"):
        runner(script).run(replace(spec(Load.BURST), plan_seed=None), threading.Event())
    assert script.seen == []


@pytest.mark.req("L25.d.warm")
def test_an_all_core_stage_runs_every_segment_and_passes():
    script = Script([Outcome.PASS] * 8)
    report = runner(script).run(spec(Load.WARM), threading.Event())
    assert report.group is Group.PASS and report.seconds == pytest.approx(480.0)
    assert [tests for _, tests, _, _ in script.seen] == [(test,) for test in ALL_TESTS]


def test_every_segment_s_speeds_reach_the_report_in_order_whether_the_stage_passes_or_fails():
    passing = runner(Script([Outcome.PASS] * 8)).run(spec(Load.WARM), threading.Event())
    assert [name for name, _, _ in passing.speeds] == [f"seg{n}" for n in range(1, 9)]
    failing = runner(Script([Outcome.PASS, Outcome.COMPUTATION])).run(spec(Load.WARM), threading.Event())
    assert failing.speeds == (("seg1", 1e9, True), ("seg2", 1e9, False))
    one = Spec(Klass.T1, 0, -5, (0,), 60.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0)
    assert runner(Script([Outcome.PASS], sampled=False)).run(one, threading.Event()).speeds == (("seg1", 1e9, True),)


@pytest.mark.req("L25.d.restart")
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
        read_started_ns = 7_000

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
@pytest.mark.req("INV.L16", "K43", "L16.G2", "L16.a")
def test_the_soak_watch_reports_what_the_kernel_logged(events, problem, group):
    reads = [kernel.KernelRead(events, problem)]
    report = runner(Script([]), detector=soak_detector(reads)).run(spec(Load.SOAK, 0.05), threading.Event())
    assert report.group is group
    assert report.kernel_read_ns == 7_000
    if events and isinstance(events[0], kernel.CoreMachineCheck):
        assert [e.core for e in report.evidence] == [1]


FAULT_ON_CPU_23 = "wireplumber[2884]: segfault at 0 ip 00007f6d4a3c2e17 sp 00007ffd2b8e6f40 error 4 likely on CPU 23"
TRAP_NAMING_NO_CPU = "traps: foo[12] general protection fault ip:401136 sp:7ffd2b8e6f40 error:0 in foo[401000+1000]"


def started_at_100_s(monkeypatch) -> None:
    monkeypatch.setattr(kernel, "monotonic_ns", lambda: 100_000_000_000)


@pytest.mark.req("PROGRAM_FAULT.evidence")
def test_every_program_fault_an_all_core_test_saw_is_kept_with_it_and_the_test_s_start(monkeypatch):
    started_at_100_s(monkeypatch)
    first = kernel.ProgramFault("wireplumber", 2884, 23, 7, FAULT_ON_CPU_23, 115.5)
    later = kernel.ProgramFault("foo", 12, None, None, TRAP_NAMING_NO_CPU, 403.0)
    machine_check = kernel.UnblamedMachineCheck(logged_by_cpu=1, bank=5, corrected=True, message="u", raw_ts=110.0)
    script = Script([Outcome.PASS] * 8, events=[[first, machine_check], [], [later]])
    report = runner(script).run(spec(Load.WARM), threading.Event())
    assert report.group is Group.PASS
    assert report.program_faults == kernel.ProgramFaults(100_000_000_000, (first, later), 0)


@pytest.mark.req("PROGRAM_FAULT.evidence")
def test_a_program_fault_during_the_soak_is_kept_with_it_and_changes_no_verdict(monkeypatch):
    started_at_100_s(monkeypatch)
    fault = kernel.ProgramFault("wireplumber", 2884, 23, 7, FAULT_ON_CPU_23, 101.0)
    reads = [kernel.KernelRead((fault,), None)]
    report = runner(Script([]), detector=soak_detector(reads)).run(spec(Load.SOAK, 0.05), threading.Event())
    assert report.group is Group.PASS
    assert report.program_faults == kernel.ProgramFaults(100_000_000_000, (fault,), 0)


@pytest.mark.req("PROGRAM_FAULT.evidence")
def test_a_test_keeps_its_latest_program_faults_and_counts_the_rest_and_the_next_test_starts_its_own(monkeypatch):
    started_at_100_s(monkeypatch)
    flood = [
        kernel.ProgramFault("loop", n, None, None, TRAP_NAMING_NO_CPU, 101.0)
        for n in range(kernel.PROGRAM_FAULTS_KEPT + 5)
    ]
    shared = runner(Script([Outcome.PASS] * 16, events=[flood]))
    kept = shared.run(spec(Load.WARM), threading.Event()).program_faults
    assert (kept.kept, kept.dropped) == kernel.kept_faults(flood)
    assert kept.dropped > 0
    assert shared.run(spec(Load.WARM), threading.Event()).program_faults == kernel.ProgramFaults(100_000_000_000, (), 0)


class SleepsThroughItsTest(FakeBackend):
    def is_available(self) -> bool:
        return True

    def finished(self, output: str) -> int:
        return 1


def fault_on(cpu: int) -> kernel.ProgramFault:
    core = CORE_OF_CPU[cpu]
    line = f"wireplumber[1]: segfault at 0 ip 00007f6d4a3c2e17 sp 00007ffd2b8e6f40 error 4 likely on CPU {cpu}"
    return kernel.ProgramFault("wireplumber", 1, cpu, core, f"{line} (core {core}, socket 0)", 101.0)


def real_runner(tmp_path, fault: kernel.ProgramFault, *, in_the_settle: bool) -> live_hw.StressRunner:
    class Detector:
        read_started_ns = 7_000

        def __init__(self) -> None:
            self.given = False

        def reset(self) -> None:
            pass

        def close(self) -> None:
            pass

        def read_new(self, final=False):
            if final is in_the_settle and not self.given:
                self.given = True
                return kernel.KernelRead((fault,), None)
            return kernel.KernelRead((), None)

    return live_hw.StressRunner(
        lambda: SleepsThroughItsTest(_child("import time; time.sleep(30)")),
        tmp_path,
        CORE_OF_CPU,
        lambda: at(50.0),
        lambda: dict.fromkeys(CPUS, 5000.0),
        supervisor=partial(Supervisor, containment_for=lambda cpus: None),
        detector=Detector,
        memory_backend=lambda: SleepsThroughItsTest(_child("import time; time.sleep(30)")),
        budget=lambda n, floor: Budget(),
    )


@pytest.mark.req("PROGRAM_FAULT.verdict")
@pytest.mark.parametrize(
    ("test", "cpu", "in_the_settle"),
    [
        (Spec(Klass.T1, 0, -5, (0, 4), 1.0, ((0, -5), (1, 0), (2, 0), (3, 0)), Stage.SEARCH, None, 95.0), 4, False),
        (spec(Load.HOT, 1.0), 6, False),
        (Spec(Klass.T1, 0, -5, CPUS, 1.0, tuple((c, -5) for c in range(4)), Stage.MEMORY, Load.MEMORY, 95.0), 5, False),
        (Spec(Klass.T1, 0, -5, (0, 4), 1.0, ((0, -5), (1, 0), (2, 0), (3, 0)), Stage.SEARCH, None, 95.0), 0, True),
    ],
    ids=["1t", "all-core", "memory", "in-the-settle"],
)
def test_a_program_fault_on_one_of_the_test_s_cpus_is_kept_and_no_verdict(
    tmp_path, monkeypatch, test, cpu, in_the_settle
):
    monkeypatch.setattr(kernel, "mce_settle_s", lambda: 0.0)
    fault = fault_on(cpu)
    report = real_runner(tmp_path, fault, in_the_settle=in_the_settle).run(test, threading.Event())
    assert report.group is Group.PASS
    assert report.program_faults.kept == (fault,)


RYZEN_AT_REST = SensorRead(hottest=60.0, tctl=56.0, tccd=((0, 42.0), (1, 60.0)))
RYZEN_AT_THE_CAP = SensorRead(hottest=55.1, tctl=55.1, tccd=((0, 42.0), (1, 55.1)))
UNDER_LOAD = SensorRead(hottest=72.0, tctl=70.0, tccd=((0, 66.0), (1, 72.0)))
SOAKED = SensorRead(hottest=61.0, tctl=59.0, tccd=((0, 45.0), (1, 61.0)))


def held_runner(tmp_path, monkeypatch, readings, cap: float = 0.5) -> live_hw.StressRunner:
    monkeypatch.setattr(kernel, "mce_settle_s", lambda: 0.0)
    monkeypatch.setattr(stages, "HOLD_CAP_S", cap)
    return live_hw.StressRunner(
        lambda: SleepsThroughItsTest(_child("import time; time.sleep(30)")),
        tmp_path,
        CORE_OF_CPU,
        lambda: next(readings),
        lambda: dict.fromkeys(CPUS, 5000.0),
        supervisor=partial(Supervisor, containment_for=lambda cpus: None),
        detector=soak_detector([]),
        budget=lambda n, floor: Budget(),
    )


def warm_runner(tmp_path, monkeypatch, after_the_load: list[SensorRead]) -> live_hw.StressRunner:
    readings = itertools.chain([RYZEN_AT_REST], [UNDER_LOAD] * 3, itertools.cycle(after_the_load))
    return held_runner(tmp_path, monkeypatch, readings)


MEASURED_CYCLE = tuple(
    SensorRead(hottest=tctl + 2.0, tctl=tctl)
    for tctl in (54.8, 60.0, 66.0, 64.0, 63.0, 62.0, 61.0, 60.0, 59.0, 58.0, 57.0, 56.5, 56.0, 55.5, 55.2)
)
RAN = re.compile(r"the load ran (\d+\.\d) of (\d+\.\d) s \((\d+)%\)")


def ran_said(detail: str) -> tuple[float, float, int]:
    found = RAN.search(detail)
    assert found is not None, detail
    return float(found.group(1)), float(found.group(2)), int(found.group(3))


@pytest.mark.req("D119", "D119.ran", "K26")
def test_a_warm_test_whose_hold_cycles_as_measured_runs_its_asked_wall_seconds_and_says_how_much_it_ran(
    tmp_path, monkeypatch
):
    readings = itertools.chain([at(48.0)], itertools.cycle(MEASURED_CYCLE))
    report = held_runner(tmp_path, monkeypatch, readings, 1.5).run(spec(Load.WARM, 2.5), threading.Event())
    assert report.group is Group.PASS, report.detail
    assert 2.5 - 0.25 <= report.seconds < 2.5 + 1.5
    ran, wall, percent = ran_said(report.detail)
    assert wall == pytest.approx(report.seconds, abs=0.05)
    assert 8 <= percent <= 20
    assert ran == pytest.approx(wall * percent / 100, abs=0.1)


@pytest.mark.req("D119", "HOLD.words")
def test_a_warm_hold_that_stops_releasing_ends_held_too_long_once_that_one_hold_passes_the_cap(tmp_path, monkeypatch):
    stuck = SensorRead(hottest=62.0, tctl=60.0)
    readings = itertools.chain([at(48.0)], MEASURED_CYCLE * 2, itertools.repeat(stuck))
    report = held_runner(tmp_path, monkeypatch, readings, 1.5).run(spec(Load.WARM, 2.0), threading.Event())
    said = (
        "held_too_long: the load stayed held more than 1.5 s in one hold;"
        " the hold releases below 55.0 C on Tctl, last read 60.0 C; the load ran "
    )
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(said), report.detail
    assert report.seconds > 2.2


TCTL_UNREAD = SensorRead(hottest=60.0)
FLOOR_SAYS = "under the 5% floor for a load held in wall time; the hold releases below 55.0 C on Tctl, last read"


def warm_cycling(tmp_path, monkeypatch, cycle, cap: float, seconds: float):
    readings = itertools.chain([at(48.0)], itertools.cycle(cycle))
    return held_runner(tmp_path, monkeypatch, readings, cap).run(spec(Load.WARM, seconds), threading.Event())


@pytest.mark.req("D120")
def test_a_warm_test_stuck_hot_but_for_a_one_tick_dip_is_no_verdict_rather_than_a_pass(tmp_path, monkeypatch):
    report = warm_cycling(tmp_path, monkeypatch, [at(70.0)] * 50 + [at(50.0)], 3.0, 1.5)
    assert report.group is Group.APPARATUS
    assert report.detail.startswith("held_too_long: the load ran only "), report.detail
    assert f"{FLOOR_SAYS} 50.0 C; the load ran " in report.detail
    assert ran_said(report.detail)[2] < 5


UNREAD_SAYS = "held_too_long: the hold's reading went unread for "
MORE_THAN_HALF = (
    "s, more than the 50% limit for a load held in wall time; the hold releases below 55.0 C on Tctl, last read"
)


@pytest.mark.req("D121", "D120.unread")
def test_a_warm_test_whose_tctl_is_mostly_unread_is_no_verdict_rather_than_a_pass(tmp_path, monkeypatch):
    report = warm_cycling(tmp_path, monkeypatch, [TCTL_UNREAD] * 50 + [SensorRead(hottest=52.0, tctl=50.0)], 3.0, 1.5)
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(UNREAD_SAYS), report.detail
    assert f"{MORE_THAN_HALF} unread; the load ran " in report.detail
    assert report.seconds < 1.5 + 0.25


@pytest.mark.req("D121", "D120.unread")
def test_short_spells_of_unread_tctl_that_add_up_to_most_of_the_slice_are_no_verdict_though_the_load_ran(
    tmp_path, monkeypatch
):
    report = warm_cycling(
        tmp_path, monkeypatch, [TCTL_UNREAD] * 8 + [SensorRead(hottest=52.0, tctl=50.0)] * 2, 1.0, 2.0
    )
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(UNREAD_SAYS), report.detail
    assert MORE_THAN_HALF in report.detail
    assert ran_said(report.detail)[2] >= 10
    assert report.seconds >= 2.0 - 0.25


@pytest.mark.req("D121")
def test_tctl_unread_eleven_reads_in_twelve_ends_the_first_slice_with_no_verdict_though_the_load_ran_over_5_percent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(stages, "WARM_SLICE_S", 1.5)
    report = warm_cycling(tmp_path, monkeypatch, [TCTL_UNREAD] * 11 + [at(50.0)], 7.5, 3.0)
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(UNREAD_SAYS), report.detail
    assert MORE_THAN_HALF in report.detail
    assert ran_said(report.detail)[2] >= 5
    assert report.seconds < 1.5 + 0.25


@pytest.mark.req("D121")
def test_tctl_unread_three_reads_in_four_inside_holds_is_no_verdict_though_each_hold_releases(tmp_path, monkeypatch):
    report = warm_cycling(tmp_path, monkeypatch, [at(70.0), *[TCTL_UNREAD] * 9, at(50.0), at(50.0)], 3.0, 1.5)
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(UNREAD_SAYS), report.detail
    assert f"{MORE_THAN_HALF} 50.0 C; the load ran " in report.detail
    assert ran_said(report.detail)[2] >= 5


@pytest.mark.req("D121", "D119", "HOLD.words")
def test_one_unread_reading_inside_a_stuck_hold_keeps_the_hold_until_it_passes_the_cap(tmp_path, monkeypatch):
    stuck = itertools.chain([at(50.0)] * 10, [at(70.0)] * 40, [TCTL_UNREAD], itertools.repeat(at(70.0)))
    report = held_runner(tmp_path, monkeypatch, itertools.chain([at(48.0)], stuck), 3.0).run(
        spec(Load.WARM, 1.5), threading.Event()
    )
    said = (
        "held_too_long: the load stayed held more than 3 s in one hold;"
        " the hold releases below 55.0 C on Tctl, last read 70.0 C; the load ran "
    )
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(said), report.detail
    assert report.seconds > 3.0


@pytest.mark.req("D119", "D120")
def test_each_warm_slice_follows_its_own_hold_and_meets_its_own_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(stages, "WARM_SLICE_S", 1.0)
    cycling = [at(54.8), at(58.0), at(62.0), at(66.0), *[at(60.0)] * 22, at(54.0)]
    barely = [at(66.0), *[at(60.0)] * 28, at(54.0)]
    readings = itertools.chain([at(48.0)], cycling, cycling, barely)
    slices: list[tuple[float, float]] = []

    class Recorded(Supervisor):
        def run(self, lanes, config, duration):
            results = super().run(lanes, config, duration)
            slices.append((duration, results[lanes[0].core_id].duration_seconds))
            return results

    warm = held_runner(tmp_path, monkeypatch, readings, 5.0)
    warm.supervisor = partial(Recorded, containment_for=lambda cpus: None)
    report = warm.run(spec(Load.WARM, 3.0), threading.Event())
    assert [asked for asked, _ in slices] == [1.0, 1.0, 1.0]
    assert all(lasted > 1.1 for _, lasted in slices), slices
    assert report.group is Group.APPARATUS
    assert report.detail.startswith("held_too_long: the load ran only "), report.detail
    assert f"{FLOOR_SAYS} 54.0 C; the load ran " in report.detail


@pytest.mark.req("D58", "HOLD.planned")
def test_an_idle_test_whose_planned_rests_pass_the_cap_runs_its_asked_seconds(tmp_path, monkeypatch):
    default_seconds = dict(MachineConfig(Mode.CO, Method.SEQUENTIAL, (0,)).stage_seconds)["idle"]
    (default,) = plan(Load.IDLE, default_seconds)
    assert sum(stop - start for start, stop in default.frozen) > stages.HOLD_CAP_S
    report = held_runner(tmp_path, monkeypatch, itertools.repeat(at(50.0))).run(spec(Load.IDLE, 1.2), threading.Event())
    assert report.group is Group.PASS, report.detail
    assert report.seconds == pytest.approx(1.2, abs=0.25)


@pytest.mark.req("D58", "HOLD.planned")
def test_a_burst_segment_whose_planned_gaps_pass_the_cap_runs_its_asked_seconds(tmp_path, monkeypatch):
    burst = spec(Load.BURST, 3.5)
    rng = random.Random(burst.plan_seed)
    (segment,) = stages.plan(Load.BURST, burst.seconds, CPUS, FIRSTS, 60_000, rng, 95.0, quick=False)
    assert sum(stop - start for start, stop in segment.frozen) > 0.2
    report = held_runner(tmp_path, monkeypatch, itertools.repeat(at(50.0)), 0.2).run(burst, threading.Event())
    assert report.group is Group.PASS, report.detail
    assert report.seconds == pytest.approx(3.5, abs=0.25)


@pytest.mark.req("WARM.rest", "WARM.tctl")
def test_a_warm_test_on_a_cpu_resting_above_55_c_releases_its_hold_and_runs_its_asked_seconds(tmp_path, monkeypatch):
    got = []
    runner = warm_runner(tmp_path, monkeypatch, [RYZEN_AT_REST, RYZEN_AT_THE_CAP])
    report = runner.run(spec(Load.WARM, 1.0), threading.Event(), got.append)
    assert report.group is Group.PASS, report.detail
    assert report.seconds == pytest.approx(1.0, abs=0.25)
    assert not got[0].running and got[-1].running


@pytest.mark.req("WARM.rest")
def test_a_warm_test_that_starts_hot_after_a_burst_holds_under_the_hot_band_and_runs_its_asked_seconds(
    tmp_path, monkeypatch
):
    after_burst = SensorRead(hottest=86.0, tctl=84.0)
    loaded = SensorRead(hottest=95.5, tctl=93.0)
    cooled = SensorRead(hottest=76.0, tctl=74.0)
    readings = itertools.chain([after_burst], [loaded] * 3, itertools.repeat(cooled))
    got = []
    report = held_runner(tmp_path, monkeypatch, readings).run(spec(Load.WARM, 1.0), threading.Event(), got.append)
    assert report.group is Group.PASS, report.detail
    assert report.seconds == pytest.approx(1.0, abs=0.25)
    assert not got[0].running and got[-1].running


@pytest.mark.req("WARM.tctl.unread", "D121")
def test_a_tctl_unread_through_a_warm_test_holds_the_load_and_ends_it_with_no_verdict(tmp_path, monkeypatch):
    readings = itertools.chain([RYZEN_AT_REST], itertools.repeat(SensorRead(hottest=60.0)))
    report = held_runner(tmp_path, monkeypatch, readings).run(spec(Load.WARM, 1.0), threading.Event())
    said = (
        "s, more than the 50% limit for a load held in wall time;"
        " the hold releases below 58.0 C on Tctl, last read unread; the load ran 0.0 of "
    )
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(UNREAD_SAYS), report.detail
    assert said in report.detail
    assert report.seconds == pytest.approx(1.0, abs=0.25)


@pytest.mark.req("D58", "HOLD.words")
def test_a_hot_hold_past_its_cap_is_a_thermal_stop_naming_its_release_point_and_cools_before_the_retry(
    tmp_path, monkeypatch
):
    report = held_runner(tmp_path, monkeypatch, itertools.repeat(at(95.0))).run(spec(Load.HOT, 1.0), threading.Event())
    said = (
        "thermal: the load stayed held more than 0.5 s in all;"
        " the hold releases below 85.0 C on the hottest input, last read 95.0 C; the load ran 0.0 of "
    )
    assert report.group is Group.THERMAL
    assert report.detail.startswith(said), report.detail
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    start_session(fake)
    fake.fire()
    fake.deliver(report)
    assert fake.state.slot.cool_below_c == fake.config.thermal_limit_c - fake.config.cool_margin_c


@pytest.mark.req("D58", "HOLD.words")
def test_a_hold_past_its_cap_is_no_verdict_naming_its_release_point_and_last_reading_and_pauses_as_such(
    tmp_path, monkeypatch
):
    report = warm_runner(tmp_path, monkeypatch, [SOAKED]).run(spec(Load.WARM, 1.0), threading.Event())
    said = (
        "held_too_long: the load stayed held more than 0.5 s in one hold;"
        " the hold releases below 58.0 C on Tctl, last read 59.0 C; the load ran 0.0 of "
    )
    assert report.group is Group.APPARATUS
    assert report.detail.startswith(said), report.detail
    fake = fresh(2, [-12, -6], 1, max_offset=-20, k_1t=2)
    start_session(fake)
    fake.fire()
    for _ in range(4):
        fake.deliver(report)
        if fake.state.lifecycle is not Lifecycle.RUNNING:
            break
        fake.fire()
    paused = (fake.state.pause.reason, fake.state.pause.detail)
    assert paused == (PauseReason.APPARATUS, f"no verdict 4 times in a row: {report.detail}")


@pytest.mark.req("EC.G1")
def test_the_soak_reads_the_kernel_log_once_more_after_one_machine_check_poll(monkeypatch):
    monkeypatch.setattr(kernel, "mce_settle_s", lambda: 6.0)
    logged = []

    class LoggedDuringTheSettle(threading.Event):
        def wait(self, timeout=None):
            if timeout == 6.0:
                logged.append(kernel.CoreMachineCheck(cpu=5, bank=1, corrected=True, message="m", raw_ts=0.0))
                return False
            return super().wait(timeout)

    class Detector:
        read_started_ns = 7_000

        def reset(self):
            pass

        def close(self):
            pass

        def read_new(self, final=False):
            found = tuple(logged)
            logged.clear()
            return kernel.KernelRead(found, None)

    report = runner(Script([]), detector=Detector).run(spec(Load.SOAK, 0.05), LoggedDuringTheSettle())
    assert [e.core for e in report.evidence] == [1]


@pytest.mark.req("HANG.heartbeat", "HANG.between")
@pytest.mark.parametrize("driven", [False, True], ids=["alone", "driven"])
@pytest.mark.parametrize("breaks", [False, True], ids=["ends", "raises"])
def test_the_soak_watch_beats_running_while_it_watches_then_between_while_driven_and_else_idle(
    tmp_path, monkeypatch, breaks, driven
):
    monkeypatch.setattr(kernel, "mce_settle_s", lambda: 0.0)
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    seen = []

    class Detector:
        read_started_ns = 7_000

        def reset(self):
            pass

        def close(self):
            pass

        def read_new(self, final=False):
            if not final:
                seen.append(json.loads(heartbeat.read_text()) if heartbeat.exists() else None)
                if breaks:
                    raise RuntimeError("the kernel log reader broke")
            return kernel.KernelRead((), None)

    soak = runner(Script([]), detector=Detector)
    soak.heartbeat = Heartbeat(heartbeat, tmp_path / "history")
    with soak.heartbeat.driven() if driven else contextlib.nullcontext():
        if breaks:
            with pytest.raises(RuntimeError, match="the kernel log reader broke"):
                soak.run(spec(Load.SOAK, 1.2), threading.Event())
        else:
            assert soak.run(spec(Load.SOAK, 1.2), threading.Event()).group is Group.PASS
        ended = json.loads(heartbeat.read_text())["state"]
    assert seen[0] is not None and (seen[0]["state"], seen[0]["test"]) == ("running", "soak watch"), seen[:1]
    assert seen[0]["probe_dir"] == str(tmp_path / "history")
    assert ended == ("between" if driven else "idle")
    assert json.loads(heartbeat.read_text())["state"] == "idle"


def test_a_stopped_soak_is_no_verdict():
    stop = threading.Event()
    stop.set()
    report = runner(Script([]), detector=soak_detector([])).run(spec(Load.SOAK, 5.0), stop)
    assert report.group is Group.APPARATUS and "stopped" in report.detail


@pytest.mark.req("T22.F16")
@pytest.mark.parametrize(
    ("stop_after", "problem", "group"),
    [(None, None, Group.PASS), (2, None, Group.APPARATUS), (None, "kmsg unreadable", Group.APPARATUS)],
)
def test_a_soak_reports_the_seconds_it_watched_never_its_configured_length_or_0(
    monkeypatch, stop_after, problem, group
):
    settle = 6.0
    clock = [100.0]
    monkeypatch.setattr(live_hw, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(kernel, "mce_settle_s", lambda: settle)
    woke = []

    class LateWakeups(threading.Event):
        def wait(self, timeout=None):
            if timeout == settle:
                clock[0] += 0.0 if self.is_set() else settle
                return self.is_set()
            clock[0] += timeout + 0.5
            woke.append(clock[0])
            if len(woke) == stop_after:
                self.set()
            return self.is_set()

    reads = [kernel.KernelRead((), problem)]
    report = runner(Script([]), detector=soak_detector(reads)).run(spec(Load.SOAK, 5.0), LateWakeups())
    assert report.group is group
    assert report.seconds == woke[-1] - 100.0


def test_y_cruncher_names_each_failing_cpu_once_through_its_color_codes():
    out = "\x1b[1;31mError(s) encountered on logical core 6.\x1b[0m\nError(s) encountered on logical core 6.\n"
    assert ycruncher.named_cpus(out + "Error(s) encountered on logical core 13.") == (6, 13)
    assert ycruncher.named_cpus("Error(s) encountered on logical core.") == ()


@pytest.mark.parametrize(
    ("outcome", "log", "named"),
    [
        (Outcome.COMPUTATION, "Error(s) encountered on logical core 6.", (2,)),
        (Outcome.COMPUTATION, "Coefficient is too large.", ()),
        (Outcome.STALL, "Error(s) encountered on logical core 6.", ()),
    ],
)
def test_a_failing_all_core_segment_reports_the_cores_y_cruncher_named(outcome, log, named):
    report = runner(Script([outcome], output=log)).run(spec(Load.HOT, 510.0), threading.Event())
    assert (report.named, report.output) == (named, log)


def test_a_cpu_the_topology_does_not_have_is_refused():
    script = Script([Outcome.COMPUTATION], output="Error(s) encountered on logical core 40.")
    with pytest.raises(ValueError, match="40"):
        runner(script).run(spec(Load.HOT, 510.0), threading.Event())


@pytest.mark.req("D28")
def test_burst_draws_powers_of_two_up_to_the_cpu_count_on_first_cpus_and_all_cpus_as_the_last_draw():
    cpus = tuple(range(32))
    firsts = tuple(range(16))
    seen = set()
    for seed in range(200):
        for s in stages.plan(Load.BURST, 480.0, cpus, firsts, 60_000, random.Random(seed), 95.0, quick=False):
            seen.add(len(s.cpus))
            assert s.cpus == cpus or set(s.cpus) <= set(firsts)
    assert seen == {2, 4, 8, 16, 32}


def sample(running, mhz, tctl=70.0):
    return telemetry.Sample(running, tuple(enumerate(mhz)), tctl)


@pytest.mark.req("D64", "K36.8", "SR.R1")
def test_the_working_cores_are_the_cores_the_failing_segment_ran_on_whatever_their_clocks():
    ring = [sample(True, [5600, 600, 5600, 600]), sample(True, [5600, 600, 5600, 600])]
    ctx = telemetry.context(ring, True, (1, 3), ())
    assert ctx.working == (1, 3) and ctx.paused is False and ctx.steady is True
    assert ctx.mhz == 600.0


@pytest.mark.req("K31")
def test_the_band_clock_is_the_named_cores_own_clock_in_the_hottest_running_sample():
    ring = [
        sample(True, [5000, 4000, 2000, 1000], 80.0),
        sample(True, [5600, 5400, 4000, 600], 90.0),
        sample(False, [600] * 4, 99.0),
    ]
    ctx = telemetry.context(ring, False, (0, 1, 2), (1,))
    assert (ctx.mhz, ctx.tctl, ctx.paused, ctx.steady) == (5400.0, 90.0, True, False)
    assert telemetry.context(ring, False, (0, 1, 2), ()).mhz == 5000.0
    assert telemetry.band_clock(telemetry.Sample(True, (), None), (0,)) is None
    with pytest.raises(ValueError):
        telemetry.context([], True, (0,), ())


def test_the_hold_samples_each_core_at_its_fastest_cpu_on_its_own_cadence():
    clocks = {4: 5000.0, 0: 4000.0, 1: 3000.0, 99: 9000.0}
    hold = live_hw.Hold(plan(Load.WARM)[0], lambda: clocks, CORE_OF_CPU, 0.25)
    for elapsed in (0.0, 0.1, 0.3, 0.5, 0.56):
        hold(elapsed, at(60.0))
    assert len(hold.ring) == 3
    assert replace(hold.ring[0], mono_ns=0) == telemetry.Sample(True, ((0, 5000.0), (1, 3000.0)), 60.0)
    assert 0 < hold.ring[0].mono_ns <= hold.ring[1].mono_ns
    for elapsed in range(1, 20):
        hold(float(elapsed), at(60.0))
    assert len(hold.ring) == telemetry.RING


def test_the_failure_context_names_the_segments_cores_and_the_named_cores_clock():
    clocks = {0: 5000.0, 1: 5000.0, 2: 4200.0, 3: 1000.0}
    script = Script([Outcome.COMPUTATION], output="Error(s) encountered on logical core 6.")
    report = runner(script, clocks=lambda: clocks).run(spec(Load.HOT, 510.0), threading.Event())
    assert report.context.working == (0, 1, 2, 3) and report.named == (2,) and report.context.mhz == 4200.0


def test_a_failure_before_any_sample_has_no_context():
    report = runner(Script([Outcome.COMPUTATION], sampled=False)).run(spec(Load.HOT, 510.0), threading.Event())
    assert report.group is Group.SILICON and report.context is None


@dataclass
class Budget:
    fits: bool = True
    per_instance_mb: int = 512
    minimum_per_instance_mb: int = 256
    usable_mb: int = 60_000

    def describe(self) -> str:
        return "fake budget"


def memory_runner(outcomes, budget=None, available=True, seen=None):
    class LaneSupervisor:
        def __init__(self, **kwargs):
            self.backend = kwargs["backend"]
            self.freeze = kwargs["freeze"]
            self.sensors = kwargs["sensors"]

        def run(self, lanes, cfg, duration):
            seen.append((tuple((lane.core_id, lane.cpus) for lane in lanes), cfg.threads, cfg.memory_mb, self.backend))
            if self.freeze is not None:
                self.freeze(duration / 2, self.sensors())
            return {
                lane.core_id: StressResult(
                    lane.core_id,
                    outcomes.get(lane.core_id, Outcome.PASS),
                    f"lane {lane.core_id}",
                    duration,
                    output="" if lane.core_id not in outcomes else f"out {lane.core_id}",
                )
                for lane in lanes
            }

    class Detector:
        read_started_ns = None

        def reset(self):
            pass

        def close(self):
            pass

    class Backend:
        def is_available(self):
            return available

        def resolution(self):
            return tools.Resolution("stressapptest", None, "PATH", problem="not on PATH")

    tool = Backend()
    return tool, live_hw.StressRunner(
        lambda: None,
        Path("/nonexistent"),
        CORE_OF_CPU,
        lambda: at(60.0),
        lambda: dict.fromkeys(CPUS, 5000.0),
        supervisor=LaneSupervisor,
        detector=Detector,
        memory_backend=lambda: tool,
        budget=budget or (lambda n, floor: Budget()),
    )


@pytest.mark.req("K42", "L15.d")
def test_memory_runs_one_stressapptest_lane_per_core_with_the_budgeted_memory():
    seen = []
    tool, run = memory_runner({}, seen=seen)
    report = run.run(spec(Load.MEMORY, 300.0), threading.Event())
    assert report.group is Group.PASS and report.named == () and report.seconds == 300.0
    assert seen == [(((0, (0, 4)), (1, (1, 5)), (2, (2, 6)), (3, (3, 7))), 2, 512, tool)]


@pytest.mark.req("L15.G7")
def test_memory_names_each_silicon_lane_and_an_apparatus_lane_alone_is_no_silicon_verdict():
    _, run = memory_runner({2: Outcome.COMPUTATION, 3: Outcome.STALL}, seen=[])
    report = run.run(spec(Load.MEMORY, 300.0), threading.Event())
    assert (report.group, report.named) == (Group.SILICON, (2,))
    assert "core 2: lane 2" in report.detail and "core 3: lane 3" in report.detail
    assert report.detail.endswith("memory budget: fake budget")
    assert report.output == "core 2:\nout 2\ncore 3:\nout 3"
    _, run = memory_runner({3: Outcome.STALL}, seen=[])
    report = run.run(spec(Load.MEMORY, 300.0), threading.Event())
    assert (report.group, report.named) == (Group.APPARATUS, ())


def unavailable(n, floor):
    raise MemoryBudgetUnavailable("no meminfo")


@pytest.mark.parametrize(
    ("budget", "available", "reason"),
    [
        (
            lambda n, floor: Budget(fits=False, per_instance_mb=100),
            True,
            "100 MB per instance is below the floor of 256 MB",
        ),
        (unavailable, True, "the memory budget is unavailable: no meminfo"),
        (None, False, "stressapptest is not installed: not on PATH"),
    ],
)
@pytest.mark.req("INV.L15")
def test_memory_that_cannot_run_is_skipped_with_its_reason(budget, available, reason):
    seen = []
    _, run = memory_runner({}, budget=budget, available=available, seen=seen)
    report = run.run(spec(Load.MEMORY, 300.0), threading.Event())
    assert reason in report.skipped and seen == []


@pytest.mark.req("D58")
def test_every_segment_that_holds_carries_a_hold_cap_and_the_runner_hands_it_on():
    for load in (Load.HOT, Load.BURST, Load.WARM, Load.IDLE):
        holding = [s for s in plan(load) if s.hold is not None]
        assert holding and all(s.hold_cap_s is not None for s in holding), load
    script = Script([Outcome.PASS] * 8)
    runner(script).run(spec(Load.WARM), threading.Event())
    assert [cap.seconds for cap in script.caps] == [stages.HOLD_CAP_S] * 8
    assert {cap.outcome for cap in script.caps} == {Outcome.HELD_TOO_LONG}
    assert {cap.in_wall_time for cap in script.caps} == {True}
    assert [cap.describe for cap in script.caps] == [freeze.describe for *_, freeze in script.seen]
    hot = Script([Outcome.PASS])
    runner(hot).run(spec(Load.HOT), threading.Event())
    assert [(cap.outcome, cap.in_wall_time) for cap in hot.caps] == [(Outcome.THERMAL, False)]


@pytest.mark.req("D119.ran")
def test_every_all_core_verdict_says_how_much_of_its_wall_time_the_load_ran():
    warm = runner(Script([Outcome.PASS] * 8, frozen_share=0.75)).run(spec(Load.WARM), threading.Event())
    assert (warm.group, warm.detail) == (Group.PASS, "the load ran 120.0 of 480.0 s (25%)")
    failed = runner(Script([Outcome.PASS, Outcome.COMPUTATION], frozen_share=0.75)).run(
        spec(Load.WARM), threading.Event()
    )
    assert (failed.group, failed.detail) == (Group.SILICON, "computation: d; the load ran 22.5 of 90.0 s (25%)")
    hot = runner(Script([Outcome.PASS], frozen_share=0.2)).run(spec(Load.HOT), threading.Event())
    assert hot.detail == "the load ran 384.0 of 480.0 s (80%)"


def test_the_working_cores_of_a_failing_burst_segment_are_the_cores_it_drew():
    for seed in range(30):
        script = Script([Outcome.COMPUTATION])
        report = runner(script).run(replace(spec(Load.BURST), plan_seed=str(seed)), threading.Event())
        drawn = tuple(sorted({CORE_OF_CPU[c] for c in script.seen[0][0]}))
        if len(drawn) < 4:
            assert report.context.working == drawn
            return
    raise AssertionError("no seed drew a partial segment")


@pytest.mark.req("L25.d.sample", "TM.E2.ccd")
def test_a_sample_stores_the_labelled_tctl_and_each_ccd_while_the_hold_judges_the_hottest():
    got = []
    reading = SensorRead(hottest=97.0, tctl=71.0, tccd=((0, 97.0), (1, None)), volts=(("nct6799:in0", 1.1),))
    hold = live_hw.Hold(plan(Load.HOT)[0], dict, CORE_OF_CPU, 0.25, got.append)
    assert hold(0.0, reading) is Frozen.HEAT
    assert (got[0].tctl, got[0].tccd, got[0].volts) == (71.0, reading.tccd, reading.volts)


def test_a_memory_stage_is_sampled_when_a_sink_is_given():
    got = []
    _, run = memory_runner({}, seen=[])
    run.run(spec(Load.MEMORY, 300.0), threading.Event(), got.append)
    assert len(got) == 1 and got[0].running


@pytest.mark.req("K21.hang")
def test_y_cruncher_reads_a_speed_from_each_test_that_prints_one_and_counts_every_finished_test():
    out = (
        "Running SFTv4: Passed  Test Speed:  6.56 * 10^09  bits / sec\n"
        "Running FFTv4: \x1b[1;32mPassed\x1b[0m  Test Speed:  3.76 * 10^09  bits / sec\n"
        "Running VT3: Failed  Test Speed:  2.37 * 10^09  bits / sec\n"
        "Running BKT: Passed  Test Speed:  2 * 10^10  bits / sec\n"
        "Running BBP: Passed  Test Speed:  5.68 * 10^09  terms / sec\n"
        "Running N63: Passed\n"
        "Running SVT: "
    )
    backend = ycruncher.YCruncherBackend()
    assert backend.speeds(out) == (
        ("SFTv4", 6.56e9, True),
        ("FFTv4", 3.76e9, True),
        ("VT3", 2.37e9, False),
        ("BKT", 2e10, True),
        ("BBP", 5.68e9, True),
    )
    assert backend.finished(out) == 6
    assert (backend.speeds(""), backend.finished("")) == ((), 0)


@pytest.mark.req("D123.window")
def test_the_longest_rest_is_the_longest_frozen_interval_and_none_without_one():
    assert stages.longest_rest(((0.0, 1.5), (3.0, 7.0), (9.0, 9.5))) == 4.0
    assert stages.longest_rest(()) == 0.0


@pytest.mark.req("D123.window")
def test_every_burst_segment_waits_at_most_two_tests_and_four_seconds_and_no_idle_plan_has_a_window():
    backend = ycruncher.YCruncherBackend()
    burst, idle = [], []
    for seed in range(500):
        for load, windows in ((Load.BURST, burst), (Load.IDLE, idle)):
            for segment in plan(load, 360.0 if load is Load.IDLE else 480.0, seed):
                config = StressConfig(
                    seconds_per_test=segment.seconds_per_test, longest_rest_s=stages.longest_rest(segment.frozen)
                )
                windows.append(backend.progress_window(config))
    two_tests = 2 * stages.BURST_PER_TEST_S
    assert min(burst) == two_tests and two_tests + 3.9 < max(burst) <= two_tests + 4.0
    assert set(idle) == {None}


@pytest.mark.req("D123.window")
def test_each_test_hands_its_supervisor_the_longest_rest_of_its_own_plan():
    rests = {}
    for load in (Load.HOT, Load.WARM, Load.BURST, Load.IDLE):
        script = Script([Outcome.PASS] * 500)
        runner(script).run(spec(load), threading.Event())
        planned = [
            max((end - start for start, end in freeze.segment.frozen), default=0.0) for *_, freeze in script.seen
        ]
        assert [cfg.longest_rest_s for cfg in script.configs] == planned
        rests[load] = planned
    assert rests[Load.HOT] == [0.0] and set(rests[Load.WARM]) == {0.0}
    assert all(rest > 0.0 for rest in rests[Load.BURST] + rests[Load.IDLE])
    one = Spec(Klass.T1, 0, -5, (0,), 20.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0)
    script = Script([Outcome.PASS, Outcome.PASS], sampled=False)
    runner(script).run(one, threading.Event())
    runner(script).run(replace(one, klass=Klass.T2, cpus=(0, 16)), threading.Event())
    assert [cfg.longest_rest_s for cfg in script.configs] == [1.0, 0.0]


@pytest.mark.req("D124", "D124.idle")
def test_only_a_warm_slice_and_an_idle_test_must_finish_a_test():
    must_finish = {}
    for load in (Load.HOT, Load.WARM, Load.BURST, Load.IDLE):
        script = Script([Outcome.PASS] * 500)
        runner(script).run(spec(load), threading.Event())
        must_finish[load] = set(script.must_finish)
    assert must_finish == {Load.HOT: {False}, Load.WARM: {True}, Load.BURST: {False}, Load.IDLE: {True}}
    one = Spec(Klass.T1, 0, -5, (0,), 20.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0)
    script = Script([Outcome.PASS, Outcome.PASS], sampled=False)
    runner(script).run(one, threading.Event())
    runner(script).run(replace(one, klass=Klass.T2, cpus=(0, 16)), threading.Event())
    assert script.must_finish == [False, False]


class YCruncherWithAFastClock(YCruncherByItsWallClock):
    """Only y-cruncher's own clock runs sped up; the plan is the real one, and so is the window it gives."""

    def progress_window(self, config: StressConfig) -> float | None:
        return ycruncher.YCruncherBackend.progress_window(self, config)


def ycruncher_runner(tmp_path, readings, hang_after: int, speedup: float) -> live_hw.StressRunner:
    return live_hw.StressRunner(
        lambda: YCruncherWithAFastClock(hang_after, speedup),
        tmp_path,
        CORE_OF_CPU,
        lambda: next(readings),
        lambda: dict.fromkeys(CPUS, 5000.0),
        supervisor=partial(Supervisor, containment_for=lambda cpus: None),
        detector=soak_detector([]),
        budget=lambda n, floor: Budget(),
    )


WARM_SLICE_TESTED_S = 3.0
WARM_CLOCK_SPEEDUP = 16.0
NEVER_HANGS = len(ALL_TESTS)


@pytest.mark.req("D124", "K21.hang")
@pytest.mark.parametrize(
    ("hang_after", "group", "finished"),
    [(0, Group.APPARATUS, 0), (NEVER_HANGS, Group.PASS, 1)],
    ids=["hung", "healthy"],
)
def test_a_warm_slice_held_as_measured_that_finishes_no_test_is_hung_and_one_finishing_late_passes(
    tmp_path, monkeypatch, hang_after, group, finished
):
    monkeypatch.setattr(stages, "WARM_SLICE_S", WARM_SLICE_TESTED_S)
    readings = itertools.chain([at(48.0)], itertools.cycle(MEASURED_CYCLE))
    clock_s = stages.WARM_PER_TEST_S / WARM_CLOCK_SPEEDUP
    assert WARM_SLICE_TESTED_S / 2 < clock_s < WARM_SLICE_TESTED_S
    test = spec(Load.WARM, WARM_SLICE_TESTED_S)
    report = ycruncher_runner(tmp_path, readings, hang_after, WARM_CLOCK_SPEEDUP).run(test, threading.Event())
    assert report.group is group, report.detail
    assert len(report.speeds) == finished
    assert ("no test finished in its" in report.detail) is (finished == 0)


@pytest.mark.req("D124")
def test_only_a_full_warm_slice_must_finish_a_test():
    slices = stages.plan(Load.WARM, 450.0, CPUS, FIRSTS, 60_000, random.Random(1), 95.0, quick=False, rest_c=40.0)
    assert [s.duration for s in slices] == [stages.WARM_SLICE_S] * 7 + [30.0]
    assert [s.must_finish_a_test for s in slices] == [True] * 7 + [False]


@pytest.mark.req("D124.idle")
def test_only_an_idle_test_of_its_default_length_or_more_must_finish_a_test():
    marked = [segment.must_finish_a_test for seconds in (360.0, 120.0) for segment in plan(Load.IDLE, seconds)]
    assert marked == [True, False]


IDLE_ONE_CORE_SEED = "111"


@pytest.mark.req("D124.idle")
@pytest.mark.parametrize(
    ("hang_after", "group"), [(0, Group.APPARATUS), (NEVER_HANGS, Group.PASS)], ids=["hung", "healthy"]
)
def test_an_idle_test_on_a_one_core_draw_that_finishes_no_test_is_hung_and_one_that_finishes_a_test_passes(
    tmp_path, monkeypatch, hang_after, group
):
    monkeypatch.setattr(stages, "IDLE_MUST_FINISH_FROM_S", 1.0)
    test = replace(spec(Load.IDLE, 1.0), plan_seed=IDLE_ONE_CORE_SEED)
    (drawn,) = stages.plan(Load.IDLE, 1.0, CPUS, FIRSTS, 60_000, random.Random(IDLE_ONE_CORE_SEED), 95.0, quick=False)
    assert len(drawn.cpus) == 1 and drawn.frozen[-1][1] == test.seconds
    report = ycruncher_runner(tmp_path, itertools.repeat(at(50.0)), hang_after, 100.0).run(test, threading.Event())
    assert report.group is group, report.detail
    assert (len(report.speeds) > 0) is (group is Group.PASS)


@pytest.mark.req("D71", "K30")
def test_every_stage_samples_at_the_one_rate():
    holds = []
    for load in (Load.HOT, Load.WARM, Load.BURST, Load.IDLE):
        script = Script([Outcome.PASS] * 500)
        runner(script).run(spec(load), threading.Event(), lambda sample: None)
        holds += [freeze for *_, freeze in script.seen]
    one = Script([Outcome.PASS])
    runner(one).run(
        Spec(Klass.T1, 0, -5, (0,), 60.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0),
        threading.Event(),
        lambda sample: None,
    )
    holds += [freeze for *_, freeze in one.seen]
    assert {hold.every for hold in holds} == {telemetry.SAMPLE_S}
    assert len(holds) > 5


def test_an_all_core_load_sizes_its_memory_from_the_one_budget():
    script = Script([Outcome.PASS])
    runner(script, usable_mb=4000).run(spec(Load.HOT, 510.0), threading.Event())
    assert script.memory == [2000]


@pytest.mark.req("C12", "U25")
def test_the_hot_hold_band_sits_at_the_thermal_limit_and_ten_degrees_under_it():
    (hot,) = stages.plan(Load.HOT, 510.0, CPUS, FIRSTS, 60_000, random.Random(1), 89.0, quick=False)
    assert hot.hold == (89.0, 79.0)
    (warm, *_) = stages.plan(Load.WARM, 480.0, CPUS, FIRSTS, 60_000, random.Random(1), 89.0, quick=False, rest_c=48.0)
    assert warm.hold == stages.WARM_HOLD


def test_a_spec_without_a_thermal_limit_is_refused_before_anything_runs():
    script = Script([Outcome.PASS])
    with pytest.raises(ValueError, match="no thermal limit"):
        runner(script).run(Spec(Klass.T1, 0, -5, (0,), 60.0, ((0, -5),), Stage.SEARCH), threading.Event())
    assert script.seen == []


def test_the_thermal_stop_of_every_run_is_the_spec_s_limit():
    script = Script([Outcome.PASS] * 8)
    runner(script).run(replace(spec(Load.WARM), thermal_limit_c=89.0), threading.Event())
    assert set(script.limits) == {89.0}


@pytest.mark.req("K8.freeze", "D36")
@pytest.mark.parametrize(
    ("seconds", "frozen"),
    [
        (20.0, ((9.0, 10.0), (18.0, 19.0))),
        (60.0, ((9.0, 10.0), (20.0, 21.0), (31.0, 32.0), (42.0, 43.0), (53.0, 54.0), (58.0, 59.0))),
        (10.0, ((8.0, 9.0),)),
        (5.0, ((3.0, 4.0),)),
        (2.0, ((0.0, 1.0),)),
        (1.0, ()),
    ],
)
def test_the_record_suspends_one_second_per_tick_and_on_its_last_tick_two_seconds_before_the_end(seconds, frozen):
    assert stages.record_freezes(seconds) == frozen


@pytest.mark.req("K8.freeze", "D36")
def test_a_one_core_test_freezes_its_lane_as_the_record_did_and_a_two_thread_test_does_not():
    one = Spec(Klass.T1, 0, -5, (0,), 20.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0)
    two = replace(one, klass=Klass.T2, cpus=(0, 16))
    reading = at(70.0)
    script = Script([Outcome.PASS, Outcome.PASS], sampled=False)
    runner(script).run(one, threading.Event())
    runner(script).run(two, threading.Event())
    one_freeze, two_freeze = script.seen[0][3], script.seen[1][3]
    planned = [one_freeze(t, reading) for t in (8.9, 9.5, 10.1, 18.5, 19.5)]
    assert planned == [Frozen.NO, Frozen.PLANNED, Frozen.NO, Frozen.PLANNED, Frozen.NO]
    assert two_freeze is None


@pytest.mark.req("K9.memory", "K8.memory")
def test_a_two_thread_test_gives_y_cruncher_the_memory_the_owner_s_r4_record_ran_with():
    two = Spec(Klass.T2, 0, -5, (0, 16), 30.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0)
    one = replace(two, klass=Klass.T1, cpus=(0,))
    script = Script([Outcome.PASS, Outcome.PASS], sampled=False)
    runner(script).run(two, threading.Event())
    runner(script).run(one, threading.Event())
    assert script.memory == [41, 13]
