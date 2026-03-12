"""Comprehensive tests for CoreScheduler."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.backends.base import StressConfig, StressMode, StressResult
from engine.scheduler import CoreScheduler, CoreTestStatus, SchedulerConfig, TestState
from engine.topology import CPUTopology, PhysicalCore


# ===========================================================================
# Fixtures
# ===========================================================================


def make_topology(num_cores: int = 4, smt: bool = False) -> CPUTopology:
    """Build a minimal CPUTopology for scheduler testing."""
    topo = CPUTopology()
    topo.physical_cores = num_cores
    topo.smt_enabled = smt
    lcpus_per_core = 2 if smt else 1
    topo.logical_cpus_count = num_cores * lcpus_per_core

    for i in range(num_cores):
        logical_ids = tuple(range(i * lcpus_per_core, (i + 1) * lcpus_per_core))
        topo.cores[i] = PhysicalCore(
            core_id=i,
            ccd=i // 4,
            ccx=None,
            logical_cpus=logical_ids,
        )

    return topo


@pytest.fixture
def simple_topo():
    return make_topology(4, smt=False)


@pytest.fixture
def smt_topo():
    return make_topology(4, smt=True)


# ===========================================================================
# TestState enum
# ===========================================================================


class TestTestState:
    def test_all_states(self):
        assert TestState.IDLE
        assert TestState.RUNNING
        assert TestState.STOPPING
        assert TestState.FINISHED


# ===========================================================================
# CoreTestStatus
# ===========================================================================


class TestCoreTestStatus:
    def test_defaults(self):
        s = CoreTestStatus(core_id=5)
        assert s.core_id == 5
        assert s.ccd is None
        assert s.state == "pending"
        assert s.iterations == 0
        assert s.errors == 0
        assert s.last_error is None
        assert s.elapsed_seconds == 0.0
        assert s.current_fft is None


# ===========================================================================
# SchedulerConfig
# ===========================================================================


class TestSchedulerConfig:
    def test_defaults(self):
        cfg = SchedulerConfig()
        assert cfg.seconds_per_core == 360
        assert cfg.cores_to_test is None
        assert cfg.stop_on_error is False
        assert cfg.cycle_count == 1
        assert cfg.poll_interval == 1.0


# ===========================================================================
# CoreScheduler initialization
# ===========================================================================


class TestSchedulerInit:
    def test_init_state(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        assert sched.state == TestState.IDLE
        assert len(sched.core_status) == 4
        assert len(sched.results) == 4
        for core_id in range(4):
            assert core_id in sched.core_status
            assert sched.core_status[core_id].state == "pending"
            assert sched.results[core_id] == []

    def test_init_with_specific_cores(self, simple_topo, mock_backend, tmp_path):
        cfg = SchedulerConfig(cores_to_test=[1, 3])
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )
        assert len(sched.core_status) == 2
        assert 1 in sched.core_status
        assert 3 in sched.core_status
        assert 0 not in sched.core_status

    def test_default_work_dir(self, simple_topo, mock_backend):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
        )
        assert sched.work_dir == Path("/tmp/linux-corecycler")

    def test_ccd_assigned_in_status(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        for core_id in range(4):
            assert sched.core_status[core_id].ccd == simple_topo.cores[core_id].ccd

    def test_callbacks_initialized_empty(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        assert sched.on_core_start == []
        assert sched.on_core_finish == []
        assert sched.on_status_update == []
        assert sched.on_cycle_complete == []
        assert sched.on_test_complete == []


# ===========================================================================
# _get_test_cores
# ===========================================================================


class TestGetTestCores:
    def test_all_cores(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        assert sched._get_test_cores() == [0, 1, 2, 3]

    def test_specific_cores(self, simple_topo, mock_backend, tmp_path):
        cfg = SchedulerConfig(cores_to_test=[3, 1])
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )
        assert sched._get_test_cores() == [1, 3]  # sorted


# ===========================================================================
# _classify_error
# ===========================================================================


class TestClassifyError:
    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("MCE detected on CPU 0", "mce"),
            ("machine check exception", "mce"),
            ("Rounding was 0.5", "computation"),
            ("FATAL ERROR in test", "computation"),
            ("ILLEGAL SUMOUT", "computation"),
            ("result mismatch", "computation"),
            ("timeout after 600s", "timeout"),
            ("process crash detected", "crash"),
            ("signal 11 received", "crash"),
            ("some unknown error", "unknown"),
            (None, "unknown"),
            ("", "unknown"),
        ],
    )
    def test_classification(self, msg, expected):
        assert CoreScheduler._classify_error(msg) == expected


# ===========================================================================
# run() integration tests with mocked subprocess
# ===========================================================================


class TestRun:
    def test_basic_run(self, simple_topo, mock_backend, tmp_path):
        """Full run through all cores should complete."""
        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # process immediately exits
        mock_proc.communicate.return_value = ("passed", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            results = sched.run()

        assert sched.state == TestState.FINISHED
        assert len(results) == 4
        for core_id in range(4):
            assert len(results[core_id]) == 1
            assert results[core_id][0].passed is True

    def test_callbacks_fire(self, simple_topo, mock_backend, tmp_path):
        """Callbacks should be called during run."""
        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        core_starts = []
        core_finishes = []
        cycle_completes = []
        test_completes = []

        sched.on_core_start.append(lambda cid, cyc: core_starts.append((cid, cyc)))
        sched.on_core_finish.append(lambda cid, res: core_finishes.append(cid))
        sched.on_cycle_complete.append(lambda cyc: cycle_completes.append(cyc))
        sched.on_test_complete.append(lambda res: test_completes.append(True))

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.communicate.return_value = ("passed", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            sched.run()

        assert len(core_starts) == 4
        assert len(core_finishes) == 4
        assert cycle_completes == [0]
        assert test_completes == [True]

    def test_multiple_cycles(self, simple_topo, mock_backend, tmp_path):
        """cycle_count=2 should test each core twice."""
        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01, cycle_count=2)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            results = sched.run()

        for core_id in range(4):
            assert len(results[core_id]) == 2

    def test_backend_error_detected(self, simple_topo, mock_backend, tmp_path):
        """Backend parse_output returning failure should mark core as failed."""
        mock_backend.should_pass = False
        mock_backend.error_message = "FATAL ERROR"

        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.communicate.return_value = ("FATAL ERROR", "")
        mock_proc.returncode = 1
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            results = sched.run()

        for core_id in range(4):
            assert results[core_id][0].passed is False
            assert sched.core_status[core_id].errors > 0

    def test_process_start_failure(self, simple_topo, mock_backend, tmp_path):
        """OSError on Popen should be handled gracefully."""
        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        with (
            patch("subprocess.Popen", side_effect=OSError("No such file")),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            results = sched.run()

        for core_id in range(4):
            assert results[core_id][0].passed is False
            assert "Failed to start" in results[core_id][0].error_message

    def test_work_dir_created(self, simple_topo, mock_backend, tmp_path):
        work = tmp_path / "deep" / "nested" / "work"
        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=work,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            sched.run()

        assert work.exists()

    def test_backend_prepare_called(self, simple_topo, mock_backend, tmp_path):
        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            sched.run()

        assert len(mock_backend.prepared_dirs) == 4
        assert len(mock_backend.cleaned_dirs) == 4

    def test_taskset_command(self, simple_topo, mock_backend, tmp_path):
        """Command should be prefixed with taskset for CPU pinning."""
        cfg = SchedulerConfig(seconds_per_core=1, poll_interval=0.01)
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        popen_calls = []
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        def capture_popen(cmd, **kwargs):
            popen_calls.append(cmd)
            return mock_proc

        with (
            patch("subprocess.Popen", side_effect=capture_popen),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            sched.run()

        # First core (core 0, logical CPU 0) should get taskset -c 0
        assert popen_calls[0][:3] == ["taskset", "-c", "0"]


# ===========================================================================
# stop() and force_stop()
# ===========================================================================


class TestStop:
    def test_stop_sets_state(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        sched.stop()
        assert sched._stop_requested is True
        assert sched.state == TestState.STOPPING

    def test_force_stop_sets_state(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        sched.force_stop()
        assert sched._stop_requested is True
        assert sched.state == TestState.STOPPING

    def test_kill_current_no_process(self, simple_topo, mock_backend, tmp_path):
        """_kill_current with no running process should not crash."""
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        sched._process = None
        sched._kill_current()  # should not raise

    def test_kill_current_already_dead(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already dead
        sched._process = mock_proc
        sched._kill_current()  # should not raise

    def test_kill_current_sigterm(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.pid = 12345
        mock_proc.wait.return_value = None
        sched._process = mock_proc

        with patch("os.killpg") as mock_killpg, patch("os.getpgid", return_value=12345):
            sched._kill_current()

        mock_killpg.assert_called_with(12345, signal.SIGTERM)

    def test_kill_current_escalates_to_sigkill(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("test", 3), None]
        sched._process = mock_proc

        with patch("os.killpg") as mock_killpg, patch("os.getpgid", return_value=12345):
            sched._kill_current()

        # Should have been called twice: SIGTERM then SIGKILL
        calls = mock_killpg.call_args_list
        assert len(calls) == 2
        assert calls[0][0] == (12345, signal.SIGTERM)
        assert calls[1][0] == (12345, signal.SIGKILL)

    def test_kill_current_handles_process_gone(self, simple_topo, mock_backend, tmp_path):
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(),
            work_dir=tmp_path,
        )
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345
        sched._process = mock_proc

        with patch("os.killpg", side_effect=ProcessLookupError), patch(
            "os.getpgid", return_value=12345
        ):
            sched._kill_current()  # should not raise


# ===========================================================================
# Missing core handling
# ===========================================================================


class TestMissingCore:
    def test_core_not_in_topology(self, mock_backend, tmp_path):
        """If cores_to_test references a core not in topology, it should be skipped."""
        topo = make_topology(4)
        cfg = SchedulerConfig(
            cores_to_test=[0, 99],  # 99 doesn't exist
            seconds_per_core=1,
            poll_interval=0.01,
        )
        sched = CoreScheduler(
            topology=topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            results = sched.run()

        # Core 99 should be skipped
        assert sched.core_status[99].state == "skipped"
        assert results[0][0].passed is True
