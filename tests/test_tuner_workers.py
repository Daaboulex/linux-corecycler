"""Tuner worker threads and their module helpers, run synchronously.

Every worker's run() body is invoked directly — never via start() — against a
stand-in scheduler, so the report each worker emits is checked without
launching a single stress process.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.detector import CoreMachineCheck, KernelRead, KernelTrouble, UnblamedMachineCheck
from corecycler.tuner import engine as eng
from corecycler.tuner.engine import (
    WorkerReport,
    _boot_identity,
    _busy_fraction,
    _ParallelWorker,
    _RapidTransitionWorker,
    _read_cpu_times,
    _realtime_us,
    _SoakWorker,
    _TunerWorker,
)


class _Ticks:
    """A stop-event stand-in whose wait() answers a scripted sequence."""

    def __init__(self, answers):
        self._answers = list(answers)

    def wait(self, _timeout=None):
        return self._answers.pop(0) if self._answers else True

    def is_set(self):
        return not self._answers


def _result(core_id=0, outcome=Outcome.PASS, detail=""):
    return StressResult(core_id=core_id, outcome=outcome, detail=detail, duration_seconds=1.0)


def _mce(cpu=0):
    return CoreMachineCheck(cpu=cpu, bank=5, corrected=True, message="corrected", raw_ts=1.0)


def _quiet_detector(*reads):
    detector = MagicMock()
    detector.read_new.side_effect = [*reads, *[KernelRead((), None)] * 10]
    return detector


@pytest.fixture
def collect():
    connected = []

    def _collect(worker):
        seen = []
        slot = seen.append
        worker.finished.connect(slot)
        connected.append((worker, slot))
        return seen

    yield _collect
    for worker, slot in connected:
        worker.finished.disconnect(slot)


class TestModuleHelpers:
    def test_an_unreadable_boot_identity_is_a_problem_not_a_boot(self, machine):
        machine.boot_id_path.unlink()
        boot, problem = _boot_identity()
        assert boot == ""
        assert problem.startswith("boot identity unreadable: ")

    def test_a_readable_boot_identity_is_the_boot(self, machine):
        assert _boot_identity() == (machine.boot_id, None)

    def test_a_write_time_without_a_zone_is_read_as_utc(self, monkeypatch):
        monkeypatch.setenv("TZ", "XYZ-2")
        time.tzset()
        try:
            naive = _realtime_us("2026-10-05T06:00:00")
        finally:
            monkeypatch.undo()
            time.tzset()
        assert naive == _realtime_us("2026-10-05T06:00:00+00:00")

    def test_cpu_times_read_a_real_cpu(self):
        sample = _read_cpu_times(0)
        assert sample is not None
        assert sample[1] >= sample[0]

    def test_cpu_times_tolerate_an_absent_cpu(self):
        assert _read_cpu_times(9999) is None

    def test_cpu_times_tolerate_an_unreadable_proc(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise OSError("gone")

        monkeypatch.setattr("builtins.open", _boom)
        assert _read_cpu_times(0) is None

    def test_busy_fraction_needs_two_samples(self):
        assert _busy_fraction(None, (1, 2)) is None
        assert _busy_fraction((1, 2), None) is None

    def test_busy_fraction_needs_forward_progress(self):
        assert _busy_fraction((10, 100), (10, 100)) is None

    def test_busy_fraction_is_the_non_idle_share(self):
        assert _busy_fraction((10, 100), (10, 200)) == pytest.approx(1.0)
        assert _busy_fraction((10, 100), (110, 200)) == pytest.approx(0.0)


class TestWorkerReport:
    def test_a_report_without_a_lane_is_refused(self):
        with pytest.raises(ValueError, match="at least one lane"):
            WorkerReport(lanes=(), peak_stretch_pct=0.0, kernel_events=())

    def test_the_report_names_the_strongest_lane(self):
        lanes = (_result(0, Outcome.STALL, "stalled"), _result(1, Outcome.COMPUTATION, "rounding"))
        assert WorkerReport(lanes=lanes, peak_stretch_pct=0.0, kernel_events=()).result.core_id == 1

    def test_equal_outcomes_report_the_lowest_core(self):
        lanes = (_result(3, Outcome.COMPUTATION, "a"), _result(1, Outcome.COMPUTATION, "b"))
        assert WorkerReport(lanes=lanes, peak_stretch_pct=0.0, kernel_events=()).result.core_id == 1


class TestTunerWorker:
    def _worker(self, results, *, msr=None, mce=()):
        scheduler = MagicMock()
        scheduler.run.return_value = results
        scheduler.observed_mce = list(mce)
        return _TunerWorker(0, 0, scheduler, msr=msr)

    def test_the_report_carries_the_schedulers_result_unchanged(self, collect):
        result = _result(0)
        worker = self._worker({0: [result]}, mce=[_mce()])
        seen = collect(worker)
        worker.run()
        report = seen[0]
        assert report.result is result
        assert report.peak_stretch_pct == 0.0
        assert [event.cpu for event in report.kernel_events] == [0]

    def test_a_failure_on_another_core_outranks_the_primary_pass(self, collect):
        worker = self._worker({0: [_result(0)], 1: [_result(1, Outcome.OWN_CORE_MCE, "mce")]})
        seen = collect(worker)
        worker.run()
        assert seen[0].result.core_id == 1
        assert seen[0].result.outcome is Outcome.OWN_CORE_MCE

    def test_a_run_with_no_result_reports_cut_short(self, collect):
        worker = self._worker({})
        seen = collect(worker)
        worker.run()
        assert seen[0].result.core_id == 0
        assert seen[0].result.outcome is Outcome.CUT_SHORT

    def test_a_harness_exception_is_not_started(self, collect):
        scheduler = MagicMock()
        scheduler.run.side_effect = RuntimeError("work dir vanished")
        worker = _TunerWorker(0, 0, scheduler)
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.NOT_STARTED
        assert "work dir vanished" in seen[0].result.detail

    def test_a_scheduler_that_raises_stops_the_stretch_sampler(self, collect, monkeypatch):
        monkeypatch.setattr(eng, "_STRETCH_WARMUP_SECONDS", 0.05)
        monkeypatch.setattr(eng, "_STRETCH_SAMPLE_INTERVAL", 0.05)
        samplers: list[threading.Thread] = []

        class Recorded(threading.Thread):
            def start(self) -> None:
                samplers.append(self)
                super().start()

        monkeypatch.setattr(eng.threading, "Thread", Recorded)
        msr = MagicMock()
        msr.is_available.return_value = True
        msr.read_clock_stretch.return_value = {}
        scheduler = MagicMock()
        scheduler.run.side_effect = RuntimeError("scheduler blew up")
        worker = _TunerWorker(0, 0, scheduler, msr=msr)
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.NOT_STARTED
        assert len(samplers) == 1
        samplers[0].join(timeout=1.0)
        assert not samplers[0].is_alive()

    def test_a_live_msr_contributes_a_peak_stretch(self, collect, monkeypatch):
        monkeypatch.setattr(eng, "_STRETCH_WARMUP_SECONDS", 0)
        msr = MagicMock()
        msr.is_available.return_value = True
        msr.read_clock_stretch.return_value = {}
        worker = self._worker({0: [_result(0)]}, msr=msr)
        seen = collect(worker)
        worker.run()
        assert seen[0].result.passed is True

    def test_the_scheduler_is_exposed_to_subclasses(self):
        scheduler = MagicMock()
        assert _TunerWorker(0, 0, scheduler).scheduler is scheduler


class TestStretchSampler:
    def _worker(self, msr):
        return _TunerWorker(0, 3, MagicMock(), msr=msr)

    def test_without_an_msr_nothing_is_sampled(self):
        samples = []
        self._worker(None)._stretch_sampler(samples, _Ticks([False, False, True]))
        assert samples == []

    def test_a_test_that_ends_during_warmup_samples_nothing(self):
        msr = MagicMock()
        samples = []
        self._worker(msr)._stretch_sampler(samples, _Ticks([True]))
        assert samples == []
        assert not msr.read_clock_stretch.called

    def test_a_window_under_load_is_recorded(self, monkeypatch):
        monkeypatch.setattr(eng, "_read_cpu_times", lambda _cpu: None)
        msr = MagicMock()
        msr.read_clock_stretch.return_value = {3: MagicMock(stretch_pct=4.25)}
        samples = []
        self._worker(msr)._stretch_sampler(samples, _Ticks([False, False, True]))
        assert samples == [4.25]

    def test_a_window_without_sustained_load_is_discarded(self, monkeypatch):
        times = iter([(0, 100), (90, 200), (180, 300)])
        monkeypatch.setattr(eng, "_read_cpu_times", lambda _cpu: next(times))
        msr = MagicMock()
        msr.read_clock_stretch.return_value = {3: MagicMock(stretch_pct=9.0)}
        samples = []
        self._worker(msr)._stretch_sampler(samples, _Ticks([False, False, True]))
        assert samples == []


class TestRapidTransitionWorker:
    def _worker(self, results, *, mce=()):
        scheduler = MagicMock()
        scheduler.run_rapid_transitions.return_value = results
        scheduler.observed_mce = list(mce)
        return _RapidTransitionWorker(2, 2, scheduler, cores=[0, 1, 2], duration=30.0)

    def test_the_rapid_worker_reports_the_typed_outcome(self, collect):
        too_hot = "CPU temperature exceeded 95.0 C safety limit"
        thermal = {core: _result(core, Outcome.THERMAL, too_hot) for core in (2, 0)}
        worker = self._worker(thermal, mce=[_mce(cpu=2)])
        seen = collect(worker)
        worker.run()
        assert seen[0].lanes == (thermal[0], thermal[2])
        assert seen[0].result.outcome is Outcome.THERMAL
        assert [event.cpu for event in seen[0].kernel_events] == [2]

    def test_every_lane_reaches_the_report_sorted_by_core(self, collect):
        worker = self._worker({2: _result(2), 0: _result(0), 1: _result(1)})
        seen = collect(worker)
        worker.run()
        assert [lane.core_id for lane in seen[0].lanes] == [0, 1, 2]
        assert seen[0].result.passed is True
        assert worker.scheduler.run_rapid_transitions.call_args.kwargs["cores"] == [0, 1, 2]

    def test_the_core_that_logged_a_machine_check_is_the_reported_lane(self, collect):
        mce = _result(2, Outcome.OWN_CORE_MCE, "MCE during rapid transition: CPU:2")
        worker = self._worker({0: _result(0, Outcome.CUT_SHORT, "stopped"), 2: mce, 1: _result(1, Outcome.CUT_SHORT)})
        seen = collect(worker)
        worker.run()
        assert seen[0].result is mce
        assert [lane.outcome for lane in seen[0].lanes] == [Outcome.CUT_SHORT, Outcome.CUT_SHORT, Outcome.OWN_CORE_MCE]

    def test_an_empty_result_is_cut_short_for_its_core(self, collect):
        worker = self._worker({})
        seen = collect(worker)
        worker.run()
        assert seen[0].result.core_id == 2
        assert seen[0].result.outcome is Outcome.CUT_SHORT

    def test_a_harness_exception_is_not_started(self, collect):
        scheduler = MagicMock()
        scheduler.run_rapid_transitions.side_effect = RuntimeError("containment gone")
        worker = _RapidTransitionWorker(2, 2, scheduler, cores=[0], duration=1.0)
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.NOT_STARTED
        assert seen[0].result.core_id == 2
        assert "containment gone" in seen[0].result.detail


class TestParallelWorker:
    def _worker(self, raw, *, mce=()):
        runner = MagicMock()
        runner.run.return_value = raw
        runner.observed_mce = list(mce)
        return _ParallelWorker(0, 0, runner)

    def test_every_lane_reaches_the_report(self, collect):
        worker = self._worker({1: _result(1), 0: _result(0)})
        seen = collect(worker)
        worker.run()
        assert [lane.core_id for lane in seen[0].lanes] == [0, 1]
        assert seen[0].result.passed is True

    def test_a_failing_lane_is_the_reported_core(self, collect):
        worker = self._worker({0: _result(0), 1: _result(1, Outcome.COMPUTATION, "rounding")})
        seen = collect(worker)
        worker.run()
        assert seen[0].result.core_id == 1
        assert seen[0].result.outcome is Outcome.COMPUTATION

    def test_an_outside_kill_batch_reports_the_kill(self, collect):
        cut, killed = _result(0, Outcome.CUT_SHORT, "stopped"), _result(1, Outcome.OUTSIDE_KILL, "killed externally")
        worker = self._worker({0: cut, 1: killed})
        seen = collect(worker)
        worker.run()
        assert seen[0].result is killed
        assert seen[0].lanes == (cut, killed)

    def test_an_empty_batch_is_cut_short(self, collect):
        worker = self._worker({})
        seen = collect(worker)
        worker.run()
        assert seen[0].result.core_id == 0
        assert seen[0].result.outcome is Outcome.CUT_SHORT

    def test_a_harness_exception_is_not_started(self, collect):
        runner = MagicMock()
        runner.run.side_effect = RuntimeError("lane setup failed")
        worker = _ParallelWorker(0, 0, runner)
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.NOT_STARTED


class TestSoakWorker:
    def test_a_quiet_soak_passes(self, collect):
        worker = _SoakWorker(0, 0)
        worker.detector = _quiet_detector()
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.PASS
        assert seen[0].kernel_events == ()

    def test_a_kernel_event_naming_a_cpu_ends_the_soak(self, collect):
        worker = _SoakWorker(1, 60)
        worker.detector = _quiet_detector(KernelRead((_mce(cpu=1),), None))
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.OWN_CORE_MCE
        assert "corrected" in seen[0].result.detail
        assert [event.cpu for event in seen[0].kernel_events] == [1]

    def test_a_kernel_event_blaming_no_core_is_unattributed(self, collect):
        shared = UnblamedMachineCheck(logged_by_cpu=1, bank=21, corrected=True, message="umc", raw_ts=1.0)
        worker = _SoakWorker(1, 60)
        worker.detector = _quiet_detector(KernelRead((shared,), None))
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.UNATTRIBUTED_MCE

    def test_kernel_trouble_ends_the_soak_as_its_own_type(self, collect):
        lockup = KernelTrouble(message="watchdog: BUG: soft lockup - CPU#1 stuck for 22s! [x:1]", raw_ts=1.0)
        worker = _SoakWorker(1, 60)
        worker.detector = _quiet_detector(KernelRead((lockup,), None))
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.KERNEL_TROUBLE

    def test_a_failed_soak_read_is_unverified_not_a_pass(self, collect):
        worker = _SoakWorker(1, 60)
        worker.detector = _quiet_detector(KernelRead((), "kernel log overran; records lost"))
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.UNVERIFIED
        assert "records lost" in seen[0].result.detail

    def test_the_last_soak_read_is_final_and_counts(self, collect):
        worker = _SoakWorker(0, 0)
        worker.detector = _quiet_detector(KernelRead((_mce(cpu=0),), None))
        seen = collect(worker)
        worker.run()
        assert worker.detector.read_new.call_args.kwargs == {"final": True}
        assert seen[0].result.outcome is Outcome.OWN_CORE_MCE

    def test_a_quiet_watch_polls_until_it_is_stopped(self, collect):
        worker = _SoakWorker(0, 600)
        worker.detector = _quiet_detector()
        worker._stop = _Ticks([False, False, True])
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.CUT_SHORT
        assert worker.detector.read_new.call_count == 4

    def test_stopping_ends_the_watch_without_a_pass(self, collect):
        worker = _SoakWorker(0, 600)
        worker.detector = _quiet_detector()
        worker.stop()
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.CUT_SHORT

    def test_a_broken_detector_is_not_started(self, collect):
        worker = _SoakWorker(0, 10)
        worker.detector = MagicMock()
        worker.detector.reset.side_effect = OSError("journal closed")
        seen = collect(worker)
        worker.run()
        assert seen[0].result.outcome is Outcome.NOT_STARTED
        assert "journal closed" in seen[0].result.detail

    def test_abort_reaches_the_worker_through_the_scheduler_alias(self):
        worker = _SoakWorker(0, 10)
        assert worker.scheduler is worker
        worker.scheduler.force_stop()
        assert worker._stop.is_set()
