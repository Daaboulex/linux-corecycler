"""ParallelStress lane bookkeeping, driven without launching a real process.

test_parallel_stress.py runs true pinned lanes and is therefore gated on
taskset. The decisions a lane makes — startup refusal, verdict selection,
stall sampling, thermal grace and teardown escalation — are pure logic and are
driven here directly, so they are checked in every environment.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from corecycler.engine import parallel
from corecycler.engine.backends.base import StressConfig, StressMode
from corecycler.engine.detector import MCEEvent
from corecycler.engine.parallel import ParallelStress, _Lane
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig
from corecycler.engine.topology import CPUTopology, PhysicalCore


def _topo(cores=(0, 1), logical=True) -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=len(cores))
    for cid in cores:
        topo.cores[cid] = PhysicalCore(
            core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,) if logical else ()
        )
    return topo


def _backend(parse=(True, None), poll_error=None):
    backend = MagicMock()
    backend.parse_output.return_value = parse
    backend.poll_errors.return_value = poll_error
    backend.get_command.return_value = ["true"]
    return backend


def _runner(tmp_path, *, topo=None, backend=None, **cfg):
    defaults = {"seconds_per_core": 1, "poll_interval": 0.01, "stall_timeout": 30}
    defaults.update(cfg)
    return ParallelStress(
        topology=topo or _topo(),
        backend=backend or _backend(),
        stress_config=StressConfig(mode=StressMode.SSE),
        scheduler_config=SchedulerConfig(**defaults),
        work_dir=tmp_path,
    )


def _lane(tmp_path, core_id=0, cpus=(0,), **over):
    lane = _Lane(
        core_id=core_id,
        cpus=set(cpus),
        cpu_list=",".join(str(c) for c in cpus),
        work_dir=tmp_path / f"core_{core_id}",
    )
    for key, value in over.items():
        setattr(lane, key, value)
    return lane


def _proc(returncode=0, poll=None, out="", err=""):
    proc = MagicMock()
    proc.poll.return_value = poll
    proc.returncode = returncode
    proc.communicate.return_value = (out, err)
    proc.pid = 4242
    return proc


def _mce(cpu=0, message="Machine check: L1 cache"):
    return MCEEvent(timestamp=1.0, cpu=cpu, bank=5, message=message, corrected=True)


@pytest.fixture(autouse=True)
def never_signal_the_test_runner(monkeypatch):
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda _pgid, _sig: None)


@pytest.fixture
def instant(monkeypatch):
    monkeypatch.setattr(parallel.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        CoreScheduler, "_verify_child_affinity", staticmethod(lambda *_a: (True, 0))
    )
    monkeypatch.setattr(CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: 60.0))


class TestStartupRefusals:
    def test_a_core_outside_the_topology_fails_closed(self, tmp_path):
        runner = _runner(tmp_path, cores_to_test=[7])
        results = runner.run()
        assert results[7].passed is False
        assert results[7].error_type == "startup"
        assert "not in topology" in results[7].error_message

    def test_a_core_without_logical_cpus_fails_closed(self, tmp_path):
        runner = _runner(tmp_path, topo=_topo(cores=(0,), logical=False), cores_to_test=[0])
        results = runner.run()
        assert results[0].error_type == "startup"

    def test_a_process_that_will_not_launch_fails_closed(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, cores_to_test=[0])
        monkeypatch.setattr(
            parallel.subprocess, "Popen", MagicMock(side_effect=OSError("no taskset"))
        )
        runner.detector.check_mce = MagicMock(
            return_value=[
                MCEEvent(timestamp=1.0, cpu=0, bank=5, message="corrected", corrected=True)
            ]
        )
        results = runner.run()
        assert results[0].error_type == "startup"
        assert "no taskset" in results[0].error_message
        assert len(runner.observed_mce) == 1


class TestBackendErrorPoll:
    def test_a_lane_with_a_verdict_is_not_polled_again(self, tmp_path):
        runner = _runner(tmp_path)
        decided = _lane(tmp_path, verdict=MagicMock())
        runner._lanes = {0: decided}
        assert runner._poll_backend_errors(time.monotonic()) is False
        assert not runner.backend.poll_errors.called

    def test_a_clean_lane_reports_no_error(self, tmp_path):
        runner = _runner(tmp_path, backend=_backend(poll_error=None))
        runner._lanes = {0: _lane(tmp_path, proc=_proc(poll=None))}
        assert runner._poll_backend_errors(time.monotonic()) is False


class TestExitAndStallPoll:
    def test_decided_and_unstarted_lanes_are_skipped(self, tmp_path):
        runner = _runner(tmp_path)
        runner._lanes = {
            0: _lane(tmp_path, 0, verdict=MagicMock()),
            1: _lane(tmp_path, 1, proc=None),
        }
        assert runner._poll_exits_and_stalls(time.monotonic()) is False

    def test_a_clean_exit_after_startup_passes_that_lane(self, tmp_path):
        runner = _runner(tmp_path, backend=_backend(parse=(True, None)))
        lane = _lane(tmp_path, proc=_proc(returncode=0, poll=0))
        runner._lanes = {0: lane}
        assert runner._poll_exits_and_stalls(time.monotonic() - 10) is False
        assert lane.verdict.passed is True

    def test_a_reported_failure_after_startup_fails_that_lane(self, tmp_path):
        runner = _runner(tmp_path, backend=_backend(parse=(False, "rounding error")))
        lane = _lane(tmp_path, proc=_proc(returncode=1, poll=1))
        runner._lanes = {0: lane}
        assert runner._poll_exits_and_stalls(time.monotonic() - 10) is True
        assert lane.verdict.passed is False
        assert lane.verdict.error_message == "rounding error"

    def test_a_busy_lane_refreshes_its_activity_stamp(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path)
        monkeypatch.setattr(parallel, "_cpu_times", lambda _cpu: (100, 1000))
        monkeypatch.setattr(
            CoreScheduler, "_verify_child_affinity", staticmethod(lambda *_a: (True, 0))
        )
        lane = _lane(tmp_path, proc=_proc(poll=None), last_active=0.0)
        lane.prev_times = {0: (100, 900)}
        runner._lanes = {0: lane}
        assert runner._poll_exits_and_stalls(time.monotonic() - 10) is False
        assert lane.last_active > 0

    def test_an_unreadable_cpu_cannot_accuse_a_lane(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path)
        monkeypatch.setattr(parallel, "_cpu_times", lambda _cpu: None)
        monkeypatch.setattr(
            CoreScheduler, "_verify_child_affinity", staticmethod(lambda *_a: (True, 0))
        )
        lane = _lane(tmp_path, proc=_proc(poll=None), last_active=0.0)
        runner._lanes = {0: lane}
        assert runner._poll_exits_and_stalls(time.monotonic() - 10) is False
        assert lane.verdict is None
        assert lane.last_active > 0


class TestFinalVerdict:
    def _stopped(self, tmp_path, backend):
        runner = _runner(tmp_path, backend=backend)
        runner._stop_event.set()
        return runner

    def test_a_live_error_file_beats_a_clean_exit(self, tmp_path):
        runner = self._stopped(tmp_path, _backend(poll_error="Hardware failure detected"))
        lane = _lane(tmp_path, proc=_proc(returncode=-15))
        verdict = runner._final_verdict(lane, elapsed=60.0)
        assert verdict.passed is False
        assert verdict.error_message == "Hardware failure detected"

    def test_a_parsed_failure_is_attributed_to_the_lane(self, tmp_path):
        runner = self._stopped(tmp_path, _backend(parse=(False, "FATAL ERROR: rounding")))
        lane = _lane(tmp_path, proc=_proc(returncode=-15))
        verdict = runner._final_verdict(lane, elapsed=60.0)
        assert verdict.passed is False
        assert "rounding" in verdict.error_message

    def test_an_instant_nonzero_exit_proves_nothing(self, tmp_path):
        runner = self._stopped(tmp_path, _backend(parse=(True, None)))
        lane = _lane(tmp_path, proc=_proc(returncode=127))
        verdict = runner._final_verdict(lane, elapsed=0.4)
        assert verdict.passed is False
        assert verdict.error_type == "startup"

    def test_a_lane_killed_at_the_deadline_passes(self, tmp_path):
        runner = self._stopped(tmp_path, _backend(parse=(True, None)))
        lane = _lane(tmp_path, proc=_proc(returncode=-15))
        verdict = runner._final_verdict(lane, elapsed=60.0, interrupted=False)
        assert verdict.passed is True
        assert verdict.duration_seconds == 60.0


class TestThermalGuard:
    def test_a_missing_sensor_fails_closed_when_required(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: None)
        )
        runner = _runner(tmp_path, require_thermal_sensor=True)
        assert runner._check_temperature() is False

    def test_a_missing_sensor_is_tolerated_when_optional(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: None)
        )
        runner = _runner(tmp_path, require_thermal_sensor=False)
        assert runner._check_temperature() is True

    def test_a_brief_overshoot_is_granted_grace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: 90.0)
        )
        runner = _runner(
            tmp_path, max_temperature=85.0, over_temp_grace_seconds=30.0,
            over_temp_hard_margin=10.0,
        )
        assert runner._check_temperature() is True
        assert runner._thermal_over_since is not None

    def test_a_sustained_overshoot_stops_the_batch(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: 90.0)
        )
        runner = _runner(
            tmp_path, max_temperature=85.0, over_temp_grace_seconds=0.0,
            over_temp_hard_margin=10.0,
        )
        assert runner._check_temperature() is False


class TestTeardown:
    def test_an_unstarted_lane_is_skipped(self, tmp_path):
        runner = _runner(tmp_path)
        runner._lanes = {0: _lane(tmp_path, proc=None)}
        runner._kill_all()
        assert runner._we_killed

    def test_a_process_that_ignores_sigterm_is_killed(self, tmp_path, monkeypatch):
        killed = []
        monkeypatch.setattr(os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append(sig))
        runner = _runner(tmp_path)
        proc = _proc(poll=None)
        proc.wait.side_effect = [subprocess.TimeoutExpired("cmd", 3), None]
        proc.stdout = MagicMock()
        proc.stderr = None
        runner._lanes = {0: _lane(tmp_path, proc=proc)}
        runner._kill_all()
        import signal

        assert killed == [signal.SIGTERM, signal.SIGKILL]
        assert proc.stdout.close.called


class TestCpuSampling:
    def test_an_unreadable_proc_stat_yields_no_sample(self):
        with patch("builtins.open", side_effect=OSError("gone")):
            assert parallel._cpu_times(0) is None

    def test_an_absent_cpu_yields_no_sample(self):
        assert parallel._cpu_times(9999) is None

    def test_a_real_cpu_yields_a_sample(self):
        sample = parallel._cpu_times(0)
        assert sample is not None
        assert sample[1] >= sample[0]

    def test_busy_needs_two_samples(self):
        assert parallel._busy(None, (1, 2)) is None
        assert parallel._busy((1, 2), None) is None

    def test_busy_needs_forward_progress(self):
        assert parallel._busy((10, 100), (10, 100)) is None
        assert parallel._busy((10, 100), (10, 90)) is None

    def test_busy_is_the_non_idle_fraction(self):
        assert parallel._busy((10, 100), (10, 200)) == pytest.approx(1.0)
        assert parallel._busy((10, 100), (110, 200)) == pytest.approx(0.0)


def test_the_work_dir_default_is_not_the_repo(tmp_path):
    runner = ParallelStress(
        topology=_topo(),
        backend=_backend(),
        stress_config=StressConfig(mode=StressMode.SSE),
        scheduler_config=SchedulerConfig(),
    )
    assert runner.work_dir == Path("/tmp/corecycler")


class TestFullBatch:
    def _runner_with(self, tmp_path, monkeypatch, procs, **cfg):
        cfg.setdefault("seconds_per_core", 0.05)
        runner = _runner(
            tmp_path,
            topo=_topo(cores=(0, 1)),
            cores_to_test=[0, 1],
            **cfg,
        )
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        monkeypatch.setattr(parallel.subprocess, "Popen", MagicMock(side_effect=procs))
        return runner

    def test_a_clean_batch_passes_every_lane(self, tmp_path, monkeypatch, instant):
        runner = self._runner_with(
            tmp_path, monkeypatch, lambda *_a, **_kw: _proc(poll=None, returncode=-15, out="ok")
        )
        results = runner.run()
        assert set(results) == {0, 1}
        assert all(r.passed for r in results.values())
        assert runner._lanes == {}

    def test_an_overheat_stops_the_whole_batch(self, tmp_path, monkeypatch, instant):
        monkeypatch.setattr(
            CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: 120.0)
        )
        runner = self._runner_with(
            tmp_path,
            monkeypatch,
            lambda *_a, **_kw: _proc(poll=None, returncode=-15),
            seconds_per_core=60,
            max_temperature=85.0,
            over_temp_hard_margin=10.0,
        )
        results = runner.run()
        assert results[0].passed is False
        assert "temperature" in results[0].error_message

    def test_a_lane_failure_leaves_the_others_without_an_invented_pass(
        self, tmp_path, monkeypatch, instant
    ):
        backend = _backend(parse=(True, None))
        backend.poll_errors.side_effect = (
            lambda work_dir: "FATAL ERROR: rounding" if work_dir.name == "core_1" else None
        )
        runner = self._runner_with(
            tmp_path,
            monkeypatch,
            lambda *_a, **_kw: _proc(poll=None, returncode=-15),
            seconds_per_core=60,
        )
        runner.backend = backend
        results = runner.run()
        assert results[1].passed is False
        assert 0 not in results

    def test_stopping_kills_every_lane(self, tmp_path):
        runner = _runner(tmp_path)
        runner._lanes = {0: _lane(tmp_path, proc=_proc(poll=None))}
        runner.stop()
        assert runner._stop_event.is_set()
        assert runner._we_killed


class TestStallWatchdog:
    def test_an_idle_lane_is_failed_as_stalled(self, tmp_path, monkeypatch):
        runner = _runner(tmp_path, stall_timeout=0.0)
        monkeypatch.setattr(parallel, "_cpu_times", lambda _cpu: (1000, 1000))
        monkeypatch.setattr(
            CoreScheduler, "_verify_child_affinity", staticmethod(lambda *_a: (True, 2))
        )
        lane = _lane(tmp_path, proc=_proc(poll=None), last_active=0.0)
        lane.prev_times = {0: (0, 0)}
        runner._lanes = {0: lane}
        assert runner._poll_exits_and_stalls(time.monotonic() - 10) is True
        assert lane.verdict.passed is False
        assert "stalled" in lane.verdict.error_message
        assert lane.repins == 2


class TestErrorPollAttribution:
    def test_a_backend_error_fails_that_lane(self, tmp_path):
        runner = _runner(tmp_path, backend=_backend(poll_error="FATAL ERROR: rounding"))
        lane = _lane(tmp_path, proc=_proc(poll=None))
        runner._lanes = {0: lane}
        assert runner._poll_backend_errors(time.monotonic()) is True
        assert lane.verdict.passed is False
        assert runner._stop_event.is_set()

    def test_an_external_kill_fails_that_lane(self, tmp_path):
        runner = _runner(tmp_path)
        lane = _lane(tmp_path, proc=_proc(poll=-9, returncode=-9))
        runner._lanes = {0: lane}
        assert runner._poll_exits_and_stalls(time.monotonic() - 10) is True
        assert "killed externally" in lane.verdict.error_message

    def test_an_instant_exit_is_a_startup_fault(self, tmp_path):
        runner = _runner(tmp_path)
        lane = _lane(tmp_path, proc=_proc(poll=127, returncode=127))
        runner._lanes = {0: lane}
        assert runner._poll_exits_and_stalls(time.monotonic()) is True
        assert lane.verdict.error_type == "startup"


class TestInterruptedBatch:
    def test_an_interrupted_lane_gets_no_invented_verdict(self, tmp_path):
        runner = _runner(tmp_path, backend=_backend(parse=(True, None)))
        runner._stop_event.set()
        lane = _lane(tmp_path, proc=_proc(returncode=-15))
        assert runner._final_verdict(lane, elapsed=5.0, interrupted=True) is None


class TestThermalCeiling:
    def test_a_runaway_stops_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: 120.0)
        )
        runner = _runner(tmp_path, max_temperature=85.0, over_temp_hard_margin=10.0)
        assert runner._check_temperature() is False

    def test_a_cooled_cpu_clears_the_overshoot_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: 60.0)
        )
        runner = _runner(tmp_path, max_temperature=85.0)
        runner._thermal_over_since = 1.0
        assert runner._check_temperature() is True
        assert runner._thermal_over_since is None


class TestBatchWideEvidence:
    def _runner(self, tmp_path, monkeypatch, events, **cfg):
        cfg.setdefault("seconds_per_core", 60)
        runner = _runner(tmp_path, topo=_topo(cores=(0, 1)), cores_to_test=[0, 1], **cfg)
        runner.detector = MagicMock()
        runner.detector.check_mce.side_effect = lambda: events.pop(0) if events else []
        monkeypatch.setattr(
            parallel.subprocess,
            "Popen",
            MagicMock(side_effect=lambda *_a, **_kw: _proc(poll=None, returncode=-15)),
        )
        return runner

    def test_an_attributed_mce_fails_only_its_own_lane(self, tmp_path, monkeypatch, instant):
        runner = self._runner(
            tmp_path, monkeypatch, [[_mce(cpu=1)]]
        )
        results = runner.run()
        assert results[1].passed is False
        assert "MCE during parallel stress" in results[1].error_message
        assert 0 not in results

    def test_an_unattributed_mce_stops_the_batch_without_blaming_a_core(
        self, tmp_path, monkeypatch, instant
    ):
        runner = self._runner(tmp_path, monkeypatch, [[_mce(cpu=-1)]])
        results = runner.run()
        assert results[0].error_type == "mce_unattributed"
        assert 1 not in results

    def test_a_lane_exit_during_the_batch_ends_it(self, tmp_path, monkeypatch, instant):
        runner = _runner(
            tmp_path, topo=_topo(cores=(0, 1)), cores_to_test=[0, 1], seconds_per_core=60,
            backend=_backend(parse=(False, "FATAL ERROR: rounding")),
        )
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        monkeypatch.setattr(
            parallel.subprocess,
            "Popen",
            MagicMock(side_effect=lambda *_a, **_kw: _proc(poll=1, returncode=1)),
        )
        results = runner.run()
        assert any(r.passed is False for r in results.values())

    def test_repinned_threads_are_summarised(self, tmp_path, monkeypatch, instant, caplog):
        monkeypatch.setattr(
            CoreScheduler, "_verify_child_affinity", staticmethod(lambda *_a: (True, 4))
        )
        runner = self._runner(tmp_path, monkeypatch, [], seconds_per_core=0.02)
        with caplog.at_level("INFO", logger="corecycler.engine.parallel"):
            runner.run()
        assert "re-pinned" in caplog.text

    def test_an_empty_topology_returns_no_verdicts(self, tmp_path):
        runner = _runner(tmp_path, topo=_topo(cores=()))
        assert runner.run() == {}


class TestLiveErrorAtCompletion:
    def test_an_error_file_at_normal_completion_fails_the_lane(self, tmp_path):
        runner = _runner(tmp_path, backend=_backend(poll_error="FATAL ERROR: rounding"))
        lane = _lane(tmp_path, proc=_proc(returncode=0))
        verdict = runner._final_verdict(lane, elapsed=60.0)
        assert verdict.passed is False
        assert "rounding" in verdict.error_message
