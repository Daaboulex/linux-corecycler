"""End-to-end stale-results coverage on the REAL CoreScheduler subprocess path
(fake mprime + the real MprimeBackend prepare/poll/parse/cleanup):
a preserved failure file must never be re-parsed as a later run's verdict, and
an error landing only in results.txt is caught by the live poll, not at end.
"""

from __future__ import annotations

import os
import stat
import tempfile
import textwrap
from pathlib import Path

import pytest

from corecycler.engine.backends.mprime import MprimeBackend
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig
from corecycler.engine.topology import CPUTopology, PhysicalCore


def _single_core_topology() -> CPUTopology:
    topo = CPUTopology()
    topo.cores[0] = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))
    topo.ccds = 1
    return topo


@pytest.fixture
def exec_dir():
    """A directory that can hold EXECUTABLE fixtures.

    pytest's tmp_path lives under /tmp, which is mounted noexec on hardened
    systems — the repo's own filesystem is the one place guaranteed exec.
    """
    with tempfile.TemporaryDirectory(prefix=".e2e-bin-", dir=Path(__file__).parent) as d:
        yield Path(d)


def _fake_mprime(exec_dir: Path, body: str) -> Path:
    """Write an executable that stands in for mprime (cwd is the work dir)."""
    script = exec_dir / "fake-mprime"
    script.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _run_one(backend: MprimeBackend, work_dir: Path, seconds: int = 12):
    scheduler = CoreScheduler(
        topology=_single_core_topology(),
        backend=backend,
        stress_config=__import__("corecycler.engine.backends.base", fromlist=["StressConfig"]).StressConfig(),
        scheduler_config=SchedulerConfig(
            seconds_per_core=seconds,
            cores_to_test=[0],
            stop_on_error=True,
            cycle_count=1,
            # this machine is not the DUT — no thermal gating in the replay
            require_thermal_sensor=False,
            max_temperature=110.0,
        ),
        work_dir=work_dir,
    )
    results = scheduler.run()
    assert results[0], "scheduler produced no result"
    return results[0][0]


@pytest.mark.slow
class TestStaleResultsDisasterReplay:
    def test_error_only_in_results_txt_is_caught_live_not_at_timeout(self, tmp_path, exec_dir):
        """mprime keeps running after a computation error (it lands only in
        results.txt). The live poll must fail the test within ~one poll
        interval, not burn the full duration."""
        fake = _fake_mprime(
            exec_dir,
            """
            echo "[Worker #1] Worker starting"
            sleep 1
            echo "FATAL ERROR: Rounding was 0.5, expected less than 0.4" >> results.txt
            sleep 60
        """,
        )
        backend = MprimeBackend()
        backend._binary = str(fake)

        result = _run_one(backend, tmp_path / "work", seconds=30)

        assert result.passed is False
        assert "FATAL ERROR" in (result.error_message or "")
        # caught by the 5 s live poll, not the 30 s deadline
        assert result.duration_seconds < 15, (
            f"error detected only after {result.duration_seconds:.1f}s — live polling is not working"
        )

    def test_failure_cannot_poison_the_next_run(self, tmp_path, exec_dir):
        """After a genuine failure, a CLEAN run in the same per-core work dir
        must PASS — the preserved results.txt must not poison it."""
        work_dir = tmp_path / "work"

        # Run 1: genuine failure (error in results.txt).
        fail_bin = _fake_mprime(
            exec_dir,
            """
            echo "FATAL ERROR: Rounding was 0.5, expected less than 0.4" >> results.txt
            sleep 60
        """,
        )
        backend = MprimeBackend()
        backend._binary = str(fail_bin)
        first = _run_one(backend, work_dir, seconds=20)
        assert first.passed is False

        core_dir = work_dir / "core_0"
        # post-mortem preserved under a name no parser ever reads again
        assert (core_dir / "failed-results.txt").exists()
        assert not (core_dir / "results.txt").exists()

        # Run 2: clean run in the SAME work dir.
        clean_bin = _fake_mprime(
            exec_dir,
            """
            echo "[Worker #1] Self-test 240K passed!" >> results.txt
            echo "[Worker #1] Self-test 240K passed!"
            sleep 60
        """,
        )
        backend._binary = str(clean_bin)
        second = _run_one(backend, work_dir, seconds=8)

        assert second.passed is True, f"clean run poisoned by the previous failure: {second.error_message}"

    def test_planted_stale_results_file_cannot_fail_a_clean_run(self, tmp_path, exec_dir):
        """Belt and braces: even a results.txt left behind by an abort or hard
        crash (cleanup never ran) must not leak into the next verdict —
        prepare() starts every run from a clean slate."""
        work_dir = tmp_path / "work"
        core_dir = work_dir / "core_0"
        core_dir.mkdir(parents=True)
        (core_dir / "results.txt").write_text("FATAL ERROR: Rounding was 0.5, expected less than 0.4\n")

        clean_bin = _fake_mprime(
            exec_dir,
            """
            echo "[Worker #1] Self-test 240K passed!" >> results.txt
            sleep 60
        """,
        )
        backend = MprimeBackend()
        backend._binary = str(clean_bin)
        result = _run_one(backend, work_dir, seconds=8)

        assert result.passed is True, f"stale pre-existing results.txt poisoned the verdict: {result.error_message}"


@pytest.mark.slow
class TestUnreadableResultsFailsClosed:
    def test_unreadable_results_is_an_apparatus_fault_not_a_pass(self, tmp_path, exec_dir):
        """If results.txt cannot be read at the final parse, the verdict is
        unavailable — that must surface as a startup-class failure, never a
        silent pass."""
        if os.geteuid() == 0:
            pytest.skip("root bypasses file permissions")
        # the fake makes results.txt unreadable before the deadline kill
        fake = _fake_mprime(
            exec_dir,
            """
            echo "data" >> results.txt
            chmod 000 results.txt
            sleep 60
        """,
        )
        backend = MprimeBackend()
        backend._binary = str(fake)
        result = _run_one(backend, tmp_path / "work", seconds=8)

        assert result.passed is False
        assert "verdict unavailable" in (result.error_message or "")
        assert result.error_type == "startup"  # engine pauses, never advances
