"""Real pinned-process exec loops: taskset, affinity, kill and verdict.

The hermetic tests in test_scheduler_phases.py replace subprocess entirely, so
they cannot catch a break in the parts only the operating system can answer:
that taskset actually launches, that a pinned child lands on the requested
CPUs, and that the deadline kill produces the expected exit code. Stall
detection is not exercised here: it reads host CPU usage, which a busy test
machine makes unreliable — test_scheduler_phases.py drives it deterministically.

These run REAL processes and are marked slow, so the nix build sandbox (which
has no taskset and disables slow marks) skips them while a developer box and
the hardware runner execute them.
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

from corecycler.engine.backends.base import (
    FFTPreset,
    StressBackend,
    StressConfig,
    StressMode,
)
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig
from corecycler.engine.topology import CPUTopology, PhysicalCore

pytestmark = [
    pytest.mark.slow,
    pytest.mark.contract,
    pytest.mark.skipif(shutil.which("taskset") is None, reason="taskset not available"),
]

BUSY = [
    sys.executable,
    "-c",
    "import time\nend = time.time() + 120\nwhile time.time() < end:\n    pass\n",
]
INSTANT_FAILURE = [sys.executable, "-c", "raise SystemExit(3)"]


class FakeStress(StressBackend):
    name = "fake-stress"

    def __init__(self, cmd, *, parse=(True, None), error=None):
        self.cmd = list(cmd)
        self.parse = parse
        self.error = error

    def is_available(self):
        return True

    def prepare(self, work_dir, config):
        work_dir.mkdir(parents=True, exist_ok=True)

    def get_command(self, config, work_dir):
        return list(self.cmd)

    def get_supported_modes(self):
        return [StressMode.SSE]

    def parse_output(self, stdout, stderr, returncode):
        return self.parse

    def poll_errors(self, work_dir):
        return self.error


def _topo():
    topo = CPUTopology(physical_cores=1, logical_cpus_count=1)
    topo.cores[0] = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))
    return topo


def _sched(tmp_path, backend, **cfg):
    defaults = {
        "seconds_per_core": 2,
        "poll_interval": 0.2,
        "cores_to_test": [0],
        "max_temperature": 200.0,
        "stall_timeout": 30.0,
    }
    defaults.update(cfg)
    return CoreScheduler(
        topology=_topo(),
        backend=backend,
        stress_config=StressConfig(mode=StressMode.SSE, fft_preset=FFTPreset.SMALL),
        scheduler_config=SchedulerConfig(**defaults),
        work_dir=tmp_path,
    )


def _status(runner):
    return runner.core_status[0]


class TestStressPhase:
    def test_a_pinned_process_runs_to_the_deadline_and_passes(self, tmp_path):
        runner = _sched(tmp_path, FakeStress(BUSY))
        passed, error = runner._run_stress_phase(0, 0, "0", tmp_path, _status(runner))
        assert passed is True
        assert error is None
        assert runner._we_killed_it is True

    def test_a_binary_that_dies_at_startup_yields_no_verdict(self, tmp_path):
        runner = _sched(tmp_path, FakeStress(INSTANT_FAILURE))
        passed, error = runner._run_stress_phase(0, 0, "0", tmp_path, _status(runner))
        assert passed is False
        assert "verdict unavailable" in error
        assert CoreScheduler._classify_error(error) == "startup"

    def test_a_missing_binary_is_an_apparatus_fault(self, tmp_path):
        runner = _sched(tmp_path, FakeStress(["/nonexistent/stress-binary"]))
        passed, error = runner._run_stress_phase(0, 0, "0", tmp_path, _status(runner))
        assert passed is False
        assert CoreScheduler._classify_error(error) == "startup"

    def test_a_backend_error_file_fails_the_core(self, tmp_path):
        runner = _sched(
            tmp_path,
            FakeStress(BUSY, error="FATAL ERROR: rounding was 0.5"),
            seconds_per_core=30,
            stop_on_error=True,
        )
        passed, error = runner._run_stress_phase(0, 0, "0", tmp_path, _status(runner))
        assert passed is False
        assert "rounding" in error


class TestAffinity:
    def test_a_pinned_child_reports_the_requested_cpus(self, tmp_path):
        """The pin must land, observed in the steady state.

        taskset narrows the mask only after the fork and its own exec, so the
        scheduler's first poll can still read the mask the child inherited.
        Sample until the pin appears, and let the deadline fail the test if it
        never does.
        """
        runner = _sched(tmp_path, FakeStress(BUSY), seconds_per_core=30)
        observed: dict[str, object] = {}
        original = CoreScheduler._verify_child_affinity

        def _capture(pid, expected, cpu_list, **kw):
            status = Path(f"/proc/{pid}/task/{pid}/status")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if status.exists():
                    for line in status.read_text().splitlines():
                        if line.startswith("Cpus_allowed_list:"):
                            observed["allowed"] = line.split(":", 1)[1].strip()
                if observed.get("allowed") == cpu_list:
                    break
                time.sleep(0.02)
            runner._stop_event.set()
            return original(pid, expected, cpu_list, **kw)

        runner._verify_child_affinity = _capture
        runner._run_stress_phase(0, 0, "0", tmp_path, _status(runner))
        assert observed.get("allowed") == "0"


class TestVariableLoad:
    def test_a_real_load_idle_cycle_completes(self, tmp_path):
        runner = _sched(tmp_path, FakeStress(BUSY), variable_load_interval=0.5)
        passed, error = runner._run_variable_load(0, 0, "0", 1.5, tmp_path)
        assert passed is True
        assert error is None


class TestRapidTransitions:
    def test_real_cycles_run_and_report_a_pass(self, tmp_path):
        runner = _sched(tmp_path, FakeStress(BUSY))
        passed, error = runner.run_rapid_transitions(
            cores=[0], total_duration=2.0, load_seconds=0.5, idle_seconds=0.2
        )
        assert passed is True
        assert error is None
        assert runner._process is None


class TestFullCoreRun:
    def test_a_whole_core_test_produces_a_result(self, tmp_path):
        runner = _sched(tmp_path, FakeStress(BUSY), idle_stability_test=0.5)
        results = runner.run()
        assert results[0][0].passed is True
        assert results[0][0].duration_seconds > 0
        assert _status(runner).state == "passed"

    def test_no_stress_process_outlives_the_run(self, tmp_path, monkeypatch):
        import corecycler.engine.scheduler as sched

        launched: list[int] = []
        real_popen = sched.subprocess.Popen

        def _tracking(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            launched.append(proc.pid)
            return proc

        monkeypatch.setattr(sched.subprocess, "Popen", _tracking)
        runner = _sched(tmp_path, FakeStress(BUSY))
        runner.run()
        assert runner._process is None
        assert launched
        assert not any(Path(f"/proc/{pid}").exists() for pid in launched)
