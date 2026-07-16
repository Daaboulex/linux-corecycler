"""ParallelStress: simultaneous per-core lanes with per-core verdicts."""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.backends.base import StressBackend, StressConfig, StressMode
from engine.detector import MCEEvent
from engine.parallel import ParallelStress
from engine.scheduler import SchedulerConfig

# These lanes exec real pinned processes; the nix build sandbox has no taskset.
pytestmark = pytest.mark.skipif(
    shutil.which("taskset") is None, reason="taskset not available"
)

SLEEP_CMD = [sys.executable, "-c", "import time; time.sleep(30)"]
BUSY_CMD = [
    sys.executable, "-c",
    "import time\nend=time.time()+30\nwhile time.time()<end: pass",
]


class LaneBackend(StressBackend):
    name = "lane-fake"

    def __init__(self, cmd=None, error_dirs=None, parse=(True, None)):
        self.cmd = cmd or BUSY_CMD
        self.error_dirs = error_dirs or {}
        self.parse = parse

    def is_available(self):
        return True

    def get_command(self, config, work_dir):
        return list(self.cmd)

    def parse_output(self, stdout, stderr, returncode):
        return self.parse

    def get_supported_modes(self):
        return [StressMode.SSE]

    def poll_errors(self, work_dir):
        return self.error_dirs.get(work_dir.name)


def _runner(topo, backend, cores, tmp_path, **cfg):
    defaults = dict(
        seconds_per_core=1,
        cores_to_test=cores,
        stop_on_error=True,
        poll_interval=0.05,
        stall_timeout=30.0,
    )
    defaults.update(cfg)
    return ParallelStress(
        topology=topo,
        backend=backend,
        stress_config=StressConfig(mode=StressMode.SSE),
        scheduler_config=SchedulerConfig(**defaults),
        work_dir=tmp_path,
    )


class TestParallelLanes:
    def test_all_lanes_run_simultaneously_and_pass(self, topo_dual_ccd_x3d, tmp_path):
        runner = _runner(topo_dual_ccd_x3d, LaneBackend(), [0, 1, 2], tmp_path)
        start = time.monotonic()
        results = runner.run()
        elapsed = time.monotonic() - start

        assert sorted(results) == [0, 1, 2]
        assert all(r.passed for r in results.values())
        assert elapsed < 2.5  # three lanes in one deadline, not three deadlines

    def test_backend_error_names_exactly_its_core(self, topo_dual_ccd_x3d, tmp_path):
        backend = LaneBackend(error_dirs={"core_1": "mprime error: FATAL ERROR"})
        runner = _runner(topo_dual_ccd_x3d, backend, [0, 1, 2], tmp_path,
                         seconds_per_core=30)
        results = runner.run()

        assert results[1].passed is False
        assert results[1].error_type == "computation"
        assert all(r.passed for c, r in results.items() if c != 1)

    def test_mce_event_fails_the_named_lane(self, topo_dual_ccd_x3d, tmp_path):
        runner = _runner(topo_dual_ccd_x3d, LaneBackend(), [0, 3], tmp_path,
                         seconds_per_core=30)
        sibling = sorted(topo_dual_ccd_x3d.cores[3].logical_cpus)[-1]
        fired = []

        def fake_check():
            if not fired:
                fired.append(1)
                return [MCEEvent(0.0, sibling, 0, "corrected err", True)]
            return []

        runner.detector.check_mce = fake_check
        results = runner.run()

        assert results[3].passed is False
        assert results[3].error_type == "mce"
        assert runner.observed_mce

    def test_thermal_trip_stops_the_batch(self, topo_dual_ccd_x3d, tmp_path, monkeypatch):
        from engine import parallel as par

        monkeypatch.setattr(
            par.CoreScheduler, "_read_cpu_temperature", staticmethod(lambda: 200.0)
        )
        runner = _runner(topo_dual_ccd_x3d, LaneBackend(), [0, 1], tmp_path,
                         seconds_per_core=30, over_temp_grace_seconds=0.0)
        results = runner.run()

        failed = [r for r in results.values() if not r.passed]
        assert failed and failed[0].error_type == "thermal"

    def test_launch_failure_fails_closed(self, topo_dual_ccd_x3d, tmp_path):
        backend = LaneBackend(cmd=["/nonexistent-binary-xyz"])
        runner = _runner(topo_dual_ccd_x3d, backend, [0, 1], tmp_path)
        results = runner.run()

        assert results
        assert all(not r.passed for r in results.values())
        assert all(r.error_type == "startup" for r in results.values())

    def test_external_kill_is_a_failure(self, topo_dual_ccd_x3d, tmp_path):
        backend = LaneBackend(
            cmd=[sys.executable, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"],
        )
        runner = _runner(topo_dual_ccd_x3d, backend, [0], tmp_path,
                         seconds_per_core=30)
        results = runner.run()

        assert results[0].passed is False
        assert "killed externally" in results[0].error_message

    def test_stalled_lane_is_named(self, topo_dual_ccd_x3d, tmp_path, monkeypatch):
        from engine import parallel as par

        monkeypatch.setattr(par, "_STALL_GRACE_SECONDS", 0.0)
        monkeypatch.setattr(par, "_busy", lambda prev, now: 0.0)
        backend = LaneBackend(cmd=SLEEP_CMD)
        runner = _runner(topo_dual_ccd_x3d, backend, [2], tmp_path,
                         seconds_per_core=30, stall_timeout=0.3)
        results = runner.run()

        assert results[2].passed is False
        assert results[2].error_type == "stall"

    def test_stop_kills_all_lanes(self, topo_dual_ccd_x3d, tmp_path):
        import threading

        runner = _runner(topo_dual_ccd_x3d, LaneBackend(), [0, 1], tmp_path,
                         seconds_per_core=30)
        threading.Timer(0.3, runner.stop).start()
        start = time.monotonic()
        runner.run()
        assert time.monotonic() - start < 5.0
