from __future__ import annotations

import random
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from corecycler.engine import detector as kernel
from corecycler.engine.backends import ycruncher
from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.backends.ycruncher import ALL_TESTS
from corecycler.tuner import live_hw, stages, telemetry
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
    hold = live_hw.Hold(plan(Load.HOT)[0], lambda: next(readings), dict, CORE_OF_CPU, 0.25)
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
    evidence_dir: Path | None = None
    sampled: bool = True


def runner(script: Script, readings=None, detector=None, clocks=None):
    class FakeSupervisor:
        def __init__(self, **kwargs):
            self.freeze = kwargs["freeze"]
            self.observed = kwargs["observed"]

        def run(self, lanes, cfg, duration):
            outcome = script.outcomes.pop(0)
            script.seen.append((lanes[0].cpus, cfg.tests, duration, self.freeze))
            if script.sampled:
                self.freeze(duration / 2)
            self.observed.extend(script.events.pop(0) if script.events else [])
            elapsed = duration if outcome is Outcome.PASS else duration / 2
            result = StressResult(lanes[0].core_id, outcome, "d", elapsed, evidence_dir=script.evidence_dir)
            return {lanes[0].core_id: result}

    class FakeDetector:
        def reset(self):
            pass

        def close(self):
            pass

    return live_hw.YCruncherRunner(
        lambda: None,
        Path("/nonexistent"),
        CORE_OF_CPU,
        FakeSupervisor,
        detector=detector or FakeDetector,
        read_temperature=lambda: 70.0 if readings is None else readings(),
        available_mb=lambda: 60_000,
        clocks=clocks or (lambda: dict.fromkeys(CPUS, 5000.0)),
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
def test_a_failing_all_core_segment_reports_the_cores_y_cruncher_named(tmp_path, outcome, log, named):
    (tmp_path / "stdout.log").write_text(log)
    script = Script([outcome], evidence_dir=tmp_path)
    assert runner(script).run(spec(Load.HOT, 510.0), threading.Event()).named == named


def test_a_cpu_the_topology_does_not_have_is_refused(tmp_path):
    (tmp_path / "stdout.log").write_text("Error(s) encountered on logical core 40.")
    with pytest.raises(ValueError, match="40"):
        runner(Script([Outcome.COMPUTATION], evidence_dir=tmp_path)).run(spec(Load.HOT, 510.0), threading.Event())


def test_burst_draws_powers_of_two_up_to_the_cpu_count_on_first_cpus_and_all_cpus_as_the_last_draw():
    cpus = tuple(range(32))
    firsts = tuple(range(16))
    seen = set()
    for seed in range(200):
        for s in stages.plan(Load.BURST, 480.0, cpus, firsts, 60_000, random.Random(seed)):
            seen.add(len(s.cpus))
            assert s.cpus == cpus or set(s.cpus) <= set(firsts)
    assert seen == {2, 4, 8, 16, 32}


def sample(running, mhz, tctl=70.0):
    return telemetry.Sample(running, tuple(enumerate(mhz)), tctl)


def test_working_cores_are_those_near_the_top_clock_in_at_least_half_the_samples():
    ring = [
        sample(True, [5000, 4300, 1000, 5000]),
        sample(True, [5000, 4200, 1000, 600]),
        sample(True, [5000, 4400, 1000, 600]),
    ]
    assert telemetry.working(ring[0]) == (0, 1, 3)
    assert telemetry.ring_cores(ring) == (0, 1)
    assert telemetry.ring_cores([*ring[:2], sample(False, [600, 600, 600, 600])]) == (0, 1, 3)
    assert telemetry.working(telemetry.Sample(True, (), None)) == ()
    assert telemetry.working(sample(True, [5000, 4250, 4249, 3000])) == (0, 1)
    assert telemetry.ring_cores([sample(True, [5000, 1000]), sample(True, [1000, 5000])]) == (0, 1)


def test_a_failure_while_running_reads_the_last_three_running_samples_and_a_frozen_one_reads_the_ring():
    old = [sample(True, [5000, 5000, 1000, 1000]) for _ in range(5)]
    late = [sample(True, [1000, 1000, 5000, 5000]) for _ in range(3)]
    assert telemetry.context(old + late, True).working == (2, 3)
    frozen = telemetry.context([*old, *late[:2], sample(False, [600, 600, 600, 600])], True)
    assert frozen.working == (0, 1) and frozen.paused is True and frozen.steady is False


def test_the_band_clock_and_tctl_come_from_the_hottest_running_sample():
    ring = [
        sample(True, [5000, 4000, 2000, 1000], 80.0),
        sample(True, [5600, 5400, 4000, 600], 90.0),
        sample(False, [600] * 4, 99.0),
    ]
    ctx = telemetry.context(ring, False)
    assert (ctx.mhz, ctx.tctl, ctx.steady) == (5000.0, 90.0, False)
    assert telemetry.band_clock(telemetry.Sample(True, (), None)) is None
    with pytest.raises(ValueError):
        telemetry.context([], True)


def test_the_hold_samples_each_core_at_its_fastest_cpu_on_its_own_cadence():
    clocks = {4: 5000.0, 0: 4000.0, 1: 3000.0, 99: 9000.0}
    hold = live_hw.Hold(plan(Load.WARM)[0], lambda: 60.0, lambda: clocks, CORE_OF_CPU, 0.25)
    for elapsed in (0.0, 0.1, 0.3, 0.5, 0.56):
        hold(elapsed)
    assert len(hold.ring) == 3
    assert hold.ring[0] == telemetry.Sample(True, ((0, 5000.0), (1, 3000.0)), 60.0)
    for elapsed in range(1, 20):
        hold(float(elapsed))
    assert len(hold.ring) == telemetry.RING


def test_clocks_are_read_from_sysfs_in_mhz(tmp_path):
    for cpu, khz in ((0, "5100000"), (17, "624000")):
        freq = tmp_path / f"cpu{cpu}" / "cpufreq"
        freq.mkdir(parents=True)
        (freq / "scaling_cur_freq").write_text(khz + "\n")
    (tmp_path / "cpufreq").mkdir()
    assert live_hw.read_clocks(tmp_path) == {0: 5100.0, 17: 624.0}


def test_the_failure_context_names_the_cores_running_near_the_top_clock():
    clocks = {0: 5000.0, 1: 5000.0, 2: 1000.0, 3: 1000.0}
    report = runner(Script([Outcome.COMPUTATION]), clocks=lambda: clocks).run(spec(Load.HOT, 510.0), threading.Event())
    assert report.context.working == (0, 1) and report.context.mhz == 5000.0


def test_a_failure_before_any_sample_has_no_context():
    report = runner(Script([Outcome.COMPUTATION], sampled=False)).run(spec(Load.HOT, 510.0), threading.Event())
    assert report.group is Group.SILICON and report.context is None


@pytest.mark.parametrize(("load", "every"), [(Load.HOT, 0.25), (Load.IDLE, 0.5)])
def test_idle_samples_every_half_second_and_the_other_loads_every_quarter(load, every):
    script = Script([Outcome.PASS] * 8)
    runner(script).run(spec(load, 30.0), threading.Event())
    assert {hold.every for _, _, _, hold in script.seen} == {every}
