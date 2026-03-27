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
from engine.scheduler import CoreScheduler, CoreTestStatus, SchedulerConfig, TestState, _STALL_GRACE_SECONDS
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
        assert sched.work_dir == Path("/tmp/corecycler")

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


# ===========================================================================
# Signal marshalling audit (regression guard)
# ===========================================================================


class TestSignalMarshallingAudit:
    """Ensure no Signal(dict) or Signal(list) exists in the codebase.

    PySide6 cannot copy-convert these types across QThread boundaries.
    All complex data must use Signal(str) with JSON serialization.
    """

    def test_no_signal_dict_in_codebase(self):
        """Scan all .py files under src/ for Signal(dict) or Signal(list)."""
        src_dir = Path(__file__).parent.parent / "src"
        violations = []
        for py_file in src_dir.rglob("*.py"):
            content = py_file.read_text()
            for i, line in enumerate(content.splitlines(), 1):
                # Skip comments
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if "Signal(dict)" in line or "Signal(list)" in line:
                    violations.append(f"{py_file.relative_to(src_dir)}:{i}: {line.strip()}")

        assert violations == [], (
            "Found Signal(dict) or Signal(list) — these crash across QThread boundaries.\n"
            "Use Signal(str) with json.dumps/loads instead.\n"
            + "\n".join(violations)
        )


# ===========================================================================
# Stall grace period
# ===========================================================================


class TestStallGracePeriod:
    """Tests for the startup grace period in stall detection."""

    def test_stall_grace_period_constant(self):
        """_STALL_GRACE_SECONDS should be 5.0."""
        assert _STALL_GRACE_SECONDS == 5.0

    def test_no_stall_during_grace_period(self, simple_topo, mock_backend, tmp_path):
        """During the grace period, near-zero CPU usage should NOT trigger a stall."""
        cfg = SchedulerConfig(
            seconds_per_core=3,  # run for 3s (within 5s grace)
            poll_interval=0.1,
            stall_timeout=1.0,  # would fire after 1s without grace
        )
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        # Process that stays alive for the full duration
        call_count = [0]
        mock_proc = MagicMock()

        def poll_side_effect():
            call_count[0] += 1
            # Exit after enough polls to fill 3 seconds
            if call_count[0] > 30:
                return 0
            return None

        mock_proc.poll.side_effect = poll_side_effect
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched, "_read_core_usage", return_value=0.0),
            patch.object(sched, "_check_temperature", return_value=True),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
        ):
            results = sched.run()

        # Core 0 should pass -- stall should NOT have fired during grace period
        assert results[0][0].passed is True

    def test_stall_fires_after_grace_period(self, simple_topo, mock_backend, tmp_path):
        """After the grace period, near-zero CPU usage should trigger a stall."""
        cfg = SchedulerConfig(
            seconds_per_core=60,  # long enough to exceed grace + stall timeout
            poll_interval=0.01,
            stall_timeout=2.0,
        )
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # stays alive
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        stall_callbacks = []
        sched.on_stall_detected.append(lambda cid: stall_callbacks.append(cid))

        # Simulate time: start at 0, advance past grace + stall_timeout
        # _read_core_usage always returns 0.0 (no CPU activity)
        time_values = [0.0]  # mutable container for monotonic mock

        original_monotonic = time.monotonic

        def mock_monotonic():
            # Advance time by 0.5s each call to speed through grace+stall
            time_values[0] += 0.5
            return time_values[0]

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(sched, "_read_core_usage", return_value=0.0),
            patch.object(sched, "_check_temperature", return_value=True),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
            patch("time.monotonic", side_effect=mock_monotonic),
            patch("time.sleep"),  # skip actual sleeping
        ):
            results = sched.run()

        # Core 0 should FAIL with stall error
        assert results[0][0].passed is False
        assert "stall" in results[0][0].error_message.lower()
        assert len(stall_callbacks) > 0


# ===========================================================================
# Child TID affinity verification
# ===========================================================================


class TestChildAffinityVerification:
    """Tests for periodic child-thread affinity scanning and re-pinning."""

    def test_verify_child_affinity_all_pinned(self, tmp_path):
        """When all TIDs have correct Cpus_allowed_list, return True without re-pinning."""
        pid = 9999
        task_dir = tmp_path / "proc" / str(pid) / "task"
        for tid in [9999, 10000, 10001]:
            tid_dir = task_dir / str(tid)
            tid_dir.mkdir(parents=True)
            (tid_dir / "status").write_text(
                "Name:\tstress\n"
                "State:\tR (running)\n"
                "Cpus_allowed_list:\t0,16\n"
            )

        with patch("os.sched_setaffinity") as mock_setaff:
            result = CoreScheduler._verify_child_affinity(
                pid, {0, 16}, "0,16", proc_base=tmp_path / "proc"
            )

        all_pinned, drift_count = result
        assert all_pinned is True
        assert drift_count == 0
        mock_setaff.assert_not_called()

    def test_verify_child_affinity_drifted_tid(self, tmp_path):
        """When a TID has drifted, os.sched_setaffinity should re-pin it."""
        pid = 9999
        task_dir = tmp_path / "proc" / str(pid) / "task"

        # TID 10000 is correctly pinned
        tid_ok = task_dir / "10000"
        tid_ok.mkdir(parents=True)
        (tid_ok / "status").write_text("Cpus_allowed_list:\t0,16\n")

        # TID 10001 has drifted to all CPUs
        tid_bad = task_dir / "10001"
        tid_bad.mkdir(parents=True)
        (tid_bad / "status").write_text("Cpus_allowed_list:\t0-31\n")

        with patch("os.sched_setaffinity") as mock_setaff:
            result = CoreScheduler._verify_child_affinity(
                pid, {0, 16}, "0,16", proc_base=tmp_path / "proc"
            )

        all_pinned, drift_count = result
        # Should have called sched_setaffinity on the drifted TID
        mock_setaff.assert_called_once_with(10001, {0, 16})
        assert drift_count == 1

    def test_verify_child_affinity_proc_unreadable(self):
        """When /proc/pid/task/ is unreadable (OSError), return True (lenient)."""
        with patch("os.sched_setaffinity") as mock_setaff:
            result = CoreScheduler._verify_child_affinity(
                99999, {0, 16}, "0,16", proc_base=Path("/nonexistent")
            )
        all_pinned, drift_count = result
        assert all_pinned is True
        assert drift_count == 0
        mock_setaff.assert_not_called()

    def test_affinity_check_periodic(self, simple_topo, mock_backend, tmp_path):
        """Affinity verification should run multiple times during a >4s stress phase."""
        cfg = SchedulerConfig(
            seconds_per_core=60,
            poll_interval=0.01,
            stall_timeout=999,  # don't trigger stall
            cores_to_test=[0],
        )
        sched = CoreScheduler(
            topology=simple_topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=cfg,
            work_dir=tmp_path,
        )

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_proc.pid = 12345

        affinity_check_calls = []

        def tracking_verify(*args, **kwargs):
            affinity_check_calls.append(True)
            return True, 0

        # Simulate 6 seconds of wall time (0.5s per monotonic call)
        time_values = [0.0]

        def mock_monotonic():
            time_values[0] += 0.5
            return time_values[0]

        with (
            patch("subprocess.Popen", return_value=mock_proc),
            patch.object(CoreScheduler, "_verify_child_affinity", side_effect=tracking_verify),
            patch.object(sched, "_read_core_usage", return_value=50.0),
            patch.object(sched, "_check_temperature", return_value=True),
            patch.object(sched.detector, "check_mce", return_value=[]),
            patch.object(sched.detector, "reset"),
            patch("time.monotonic", side_effect=mock_monotonic),
            patch("time.sleep"),
        ):
            results = sched.run()

        # With 6+ seconds and 2s interval, should have at least 2 affinity checks
        assert len(affinity_check_calls) >= 2, (
            f"Expected at least 2 affinity checks, got {len(affinity_check_calls)}"
        )


# ===========================================================================
# Process cleanup — PR_SET_PDEATHSIG and kill escalation
# ===========================================================================


class TestProcessCleanup:
    """Tests for preexec_fn PR_SET_PDEATHSIG and process group cleanup."""

    def test_make_preexec_calls_setsid_and_pdeathsig(self):
        """_make_preexec() returns a callable that calls both os.setsid() and prctl(PR_SET_PDEATHSIG, SIGKILL)."""
        preexec = CoreScheduler._make_preexec()
        assert callable(preexec)

        with (
            patch("os.setsid") as mock_setsid,
            patch("ctypes.CDLL") as mock_cdll,
            patch("ctypes.util.find_library", return_value="libc.so.6"),
        ):
            mock_libc = MagicMock()
            mock_cdll.return_value = mock_libc

            preexec()

            mock_setsid.assert_called_once()
            # PR_SET_PDEATHSIG = 1, signal.SIGKILL = 9
            mock_libc.prctl.assert_called_once_with(1, signal.SIGKILL)

    def test_no_bare_setsid_in_src(self):
        """Scan all .py files under src/ for bare preexec_fn=os.setsid — assert zero matches."""
        src_dir = Path(__file__).parent.parent / "src"
        violations = []
        for py_file in src_dir.rglob("*.py"):
            content = py_file.read_text()
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if "preexec_fn=os.setsid" in line:
                    violations.append(f"{py_file.relative_to(src_dir)}:{i}: {line.strip()}")

        assert violations == [], (
            "Found bare preexec_fn=os.setsid without PR_SET_PDEATHSIG.\n"
            "All subprocess launches must use _make_preexec() or equivalent.\n"
            + "\n".join(violations)
        )


# ===========================================================================
# Cross-thread safety audit
# ===========================================================================


class TestCrossThreadSafety:
    """Enforce that GUI code never directly accesses scheduler state across threads."""

    def test_no_direct_core_status_access_in_gui(self):
        """GUI files must not access scheduler.core_status directly — use signal/slot cache.

        The one allowed exception is in _start_test() where scheduler.core_status is read
        BEFORE the worker thread starts (no cross-thread race).
        """
        import re

        gui_dir = Path(__file__).parent.parent / "src" / "gui"
        violations = []
        for py_file in gui_dir.glob("*.py"):
            text = py_file.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if re.search(r"scheduler\.core_status", line):
                    # Allow the init_cores() call in _start_test — happens before thread start
                    if "init_cores" in line:
                        continue
                    violations.append(f"{py_file.name}:{i}: {line.strip()}")
        assert violations == [], (
            "Direct cross-thread scheduler.core_status access found:\n"
            + "\n".join(violations)
        )

    def test_tuner_abort_stops_scheduler_before_terminate(self):
        """TunerEngine.abort() must call force_stop() before terminate()."""
        import re

        engine_file = Path(__file__).parent.parent / "src" / "tuner" / "engine.py"
        text = engine_file.read_text()
        abort_match = re.search(
            r"def abort\(self\).*?(?=\n    def |\nclass |\Z)", text, re.DOTALL
        )
        assert abort_match, "TunerEngine.abort() method not found"
        abort_body = abort_match.group()
        force_pos = abort_body.find("force_stop")
        term_pos = abort_body.find("terminate")
        assert force_pos != -1, "abort() must call force_stop()"
        assert term_pos != -1, "abort() must call terminate() as fallback"
        assert force_pos < term_pos, "force_stop() must be called BEFORE terminate()"

    def test_main_window_has_core_status_cache(self):
        """MainWindow must use _core_status_cache for thread-safe status access."""
        mw_file = Path(__file__).parent.parent / "src" / "gui" / "main_window.py"
        text = mw_file.read_text()
        assert "_core_status_cache" in text, "MainWindow must define _core_status_cache"
        assert "_core_status_cache: dict" in text or "_core_status_cache =" in text, \
            "MainWindow must initialize _core_status_cache"
