"""Tuner worker threads and their module helpers, run synchronously.

Every worker's run() body is invoked directly — never via start() — against a
stand-in scheduler, so the verdict-reporting, MCE serialisation and
crash-is-an-apparatus-fault decisions are checked without launching a single
stress process.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from corecycler.engine.backends.base import StressResult
from corecycler.engine.detector import MCEEvent
from corecycler.tuner import engine as eng
from corecycler.tuner.engine import (
    _busy_fraction,
    _ParallelWorker,
    _RapidTransitionWorker,
    _read_boot_id,
    _read_cpu_times,
    _serialize_mce,
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


def _result(core_id=0, passed=True, error=None, error_type=None):
    return StressResult(
        core_id=core_id,
        passed=passed,
        duration_seconds=1.0,
        error_message=error,
        error_type=error_type,
    )


def _mce(cpu=0):
    return MCEEvent(timestamp=1.0, cpu=cpu, bank=5, message="corrected", corrected=True)


def _collect(worker):
    seen = []
    worker.finished.connect(lambda *args: seen.append(args))
    return seen


class TestModuleHelpers:
    def test_a_missing_boot_id_reads_as_empty(self, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise OSError("no procfs")

        monkeypatch.setattr("builtins.open", _boom)
        assert _read_boot_id() == ""

    def test_a_real_boot_id_is_returned(self):
        assert isinstance(_read_boot_id(), str)

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

    def test_no_events_serialise_to_nothing(self):
        assert _serialize_mce([]) == ""

    def test_events_serialise_with_their_bank_and_cpu(self):
        payload = json.loads(_serialize_mce([_mce(cpu=3)]))
        assert payload == [
            {"cpu": 3, "bank": 5, "corrected": True, "message": "corrected", "raw_ts": 0.0}
        ]


class TestTunerWorker:
    def _worker(self, results, *, msr=None, mce=()):
        scheduler = MagicMock()
        scheduler.run.return_value = results
        scheduler.observed_mce = list(mce)
        return _TunerWorker(0, 0, scheduler, msr=msr)

    def test_a_passing_core_reports_its_own_verdict(self):
        worker = self._worker({0: [_result(0, True)]}, mce=[_mce()])
        seen = _collect(worker)
        worker.run()
        core_id, passed, message, error_type, _elapsed, stretch, mce_json, _rows = seen[0]
        assert (core_id, passed, message, error_type) == (0, True, "", "")
        assert stretch == 0.0
        assert json.loads(mce_json)[0]["cpu"] == 0

    def test_a_failure_on_another_core_outranks_the_primary_pass(self):
        worker = self._worker({0: [_result(0, True)], 1: [_result(1, False, "mce", "mce")]})
        seen = _collect(worker)
        worker.run()
        assert seen[0][0] == 1
        assert seen[0][1] is False
        assert seen[0][5] == 0.0

    def test_no_verdict_is_an_apparatus_fault_not_instability(self):
        worker = self._worker({})
        seen = _collect(worker)
        worker.run()
        assert seen[0][1] is False
        assert seen[0][2] == "No result returned"
        assert seen[0][3] == "startup"

    def test_a_harness_exception_is_an_apparatus_fault(self):
        scheduler = MagicMock()
        scheduler.run.side_effect = RuntimeError("work dir vanished")
        worker = _TunerWorker(0, 0, scheduler)
        seen = _collect(worker)
        worker.run()
        assert seen[0][3] == "startup"
        assert "work dir vanished" in seen[0][2]

    def test_a_live_msr_contributes_a_peak_stretch(self, monkeypatch):
        monkeypatch.setattr(eng, "_STRETCH_WARMUP_SECONDS", 0)
        msr = MagicMock()
        msr.is_available.return_value = True
        msr.read_clock_stretch.return_value = {}
        worker = self._worker({0: [_result(0, True)]}, msr=msr)
        seen = _collect(worker)
        worker.run()
        assert seen[0][1] is True

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
    def _worker(self, verdict, *, mce=()):
        scheduler = MagicMock()
        scheduler.run_rapid_transitions.return_value = verdict
        scheduler.observed_mce = list(mce)
        return _RapidTransitionWorker(2, 2, scheduler, cores=[0, 1, 2], duration=30.0)

    def test_a_clean_run_reports_a_pass(self):
        worker = self._worker((True, None))
        seen = _collect(worker)
        worker.run()
        assert seen[0][0] == 2
        assert seen[0][1] is True
        assert seen[0][2] == ""
        assert worker.scheduler.run_rapid_transitions.call_args.kwargs["cores"] == [0, 1, 2]

    def test_a_reported_failure_carries_its_message(self):
        worker = self._worker((False, "transition crash"), mce=[_mce(cpu=2)])
        seen = _collect(worker)
        worker.run()
        assert seen[0][1] is False
        assert seen[0][2] == "transition crash"
        assert json.loads(seen[0][6])[0]["cpu"] == 2

    def test_a_harness_exception_is_an_apparatus_fault(self):
        scheduler = MagicMock()
        scheduler.run_rapid_transitions.side_effect = RuntimeError("taskset gone")
        worker = _RapidTransitionWorker(2, 2, scheduler, cores=[0], duration=1.0)
        seen = _collect(worker)
        worker.run()
        assert seen[0][3] == "startup"
        assert "taskset gone" in seen[0][2]


class TestParallelWorker:
    def _worker(self, raw, *, mce=()):
        runner = MagicMock()
        runner.run.return_value = raw
        runner.observed_mce = list(mce)
        return _ParallelWorker(0, 0, runner)

    def test_every_lane_verdict_reaches_the_results_payload(self):
        worker = self._worker({0: _result(0, True), 1: _result(1, True)})
        seen = _collect(worker)
        worker.run()
        rows = json.loads(seen[0][7])
        assert {r["core"] for r in rows} == {0, 1}
        assert all(r["passed"] for r in rows)
        assert seen[0][1] is True

    def test_a_failing_lane_is_the_reported_core(self):
        worker = self._worker(
            {0: _result(0, True), 1: _result(1, False, "rounding", "computation")}
        )
        seen = _collect(worker)
        worker.run()
        assert seen[0][0] == 1
        assert seen[0][3] == "computation"

    def test_an_empty_batch_is_an_apparatus_fault(self):
        worker = self._worker({})
        seen = _collect(worker)
        worker.run()
        assert seen[0][2] == "No result returned"
        assert seen[0][3] == "startup"
        assert json.loads(seen[0][7]) == []

    def test_a_harness_exception_is_an_apparatus_fault(self):
        runner = MagicMock()
        runner.run.side_effect = RuntimeError("lane setup failed")
        worker = _ParallelWorker(0, 0, runner)
        seen = _collect(worker)
        worker.run()
        assert seen[0][3] == "startup"


class TestSoakWorker:
    def test_a_quiet_soak_passes(self):
        worker = _SoakWorker(0, 0)
        worker.detector = MagicMock()
        worker.detector.check_mce.return_value = []
        seen = _collect(worker)
        worker.run()
        assert seen[0][1] is True
        assert seen[0][2] == ""

    def test_a_kernel_event_ends_the_soak(self):
        worker = _SoakWorker(1, 60)
        worker.detector = MagicMock()
        worker.detector.check_mce.return_value = [_mce(cpu=1)]
        seen = _collect(worker)
        worker.run()
        assert seen[0][1] is False
        assert seen[0][3] == "mce"
        assert "corrected" in seen[0][2]
        assert json.loads(seen[0][6])[0]["cpu"] == 1

    def test_a_quiet_watch_polls_until_its_duration_ends(self):
        worker = _SoakWorker(0, 600)
        worker.detector = MagicMock()
        worker.detector.check_mce.return_value = []
        worker._stop = _Ticks([False, False, True])
        seen = _collect(worker)
        worker.run()
        assert seen[0][1] is True
        assert worker.detector.check_mce.call_count == 3

    def test_stopping_ends_the_watch(self):
        worker = _SoakWorker(0, 600)
        worker.detector = MagicMock()
        worker.detector.check_mce.return_value = []
        worker.stop()
        seen = _collect(worker)
        worker.run()
        assert seen[0][1] is True

    def test_a_broken_detector_is_an_apparatus_fault(self):
        worker = _SoakWorker(0, 10)
        worker.detector = MagicMock()
        worker.detector.reset.side_effect = OSError("journal closed")
        seen = _collect(worker)
        worker.run()
        assert seen[0][3] == "startup"
        assert "journal closed" in seen[0][2]

    def test_abort_reaches_the_worker_through_the_scheduler_alias(self):
        worker = _SoakWorker(0, 10)
        assert worker.scheduler is worker
        worker.scheduler.force_stop()
        assert worker._stop.is_set()
