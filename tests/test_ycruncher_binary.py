from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from corecycler.engine.backends.base import Outcome, StressConfig, StressMode, read_text
from corecycler.engine.backends.ycruncher import ALL_TESTS, CONFIG_FILE, YCruncherBackend
from corecycler.engine.execution import exit_signal
from tests._contract_hw import require

pytestmark = [pytest.mark.slow, pytest.mark.contract]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STARTUP_SECONDS = 6.0
_FIRST_TEST_SECONDS = 40.0
_STOP_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class _Run:
    output: str
    alive_at_stop: bool
    thread_cpus: frozenset[int]
    returncode: int


def _backend() -> YCruncherBackend:
    backend = YCruncherBackend()
    require(backend.is_available(), "y-cruncher is not installed (PATH or CORECYCLER_Y_CRUNCHER_BIN)")
    return backend


def _two_cpus() -> tuple[int, ...]:
    allowed = sorted(os.sched_getaffinity(0))
    require(len(allowed) >= 2, "this process may run on fewer than two CPUs")
    return tuple(allowed[:2])


def _command(work_dir: Path, config: StressConfig) -> list[str]:
    backend = _backend()
    backend.prepare(work_dir, config)
    return backend.get_command(config, work_dir)


def _compute_thread_cpus(pid: int) -> frozenset[int]:
    compute = [int(task.name) for task in Path(f"/proc/{pid}/task").iterdir() if int(task.name) != pid]
    return frozenset().union(*(os.sched_getaffinity(tid) for tid in compute))


def _run(command: list[str], work_dir: Path, seconds: float) -> _Run:
    log_path = work_dir / "output.log"
    with log_path.open("wb") as sink:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
            start_new_session=True,
        )
    try:
        time.sleep(seconds)
        alive = proc.poll() is None
        thread_cpus = _compute_thread_cpus(proc.pid) if alive else frozenset()
        proc.send_signal(signal.SIGTERM)
        returncode = proc.wait(timeout=_STOP_SECONDS)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return _Run(_ANSI_RE.sub("", read_text(log_path)), alive, thread_cpus, returncode)


def _outcomes(run: _Run, work_dir: Path) -> set[Outcome]:
    return {hit.outcome for hit in YCruncherBackend().findings(run.output, work_dir)}


@pytest.fixture(scope="module")
def lane_run(tmp_path_factory) -> tuple[tuple[int, ...], _Run]:
    cpus = _two_cpus()
    work_dir = tmp_path_factory.mktemp("ycruncher-lane")
    config = StressConfig(mode=StressMode.SSE, cpus=cpus, threads=len(cpus), memory_mb=200)
    return cpus, _run(_command(work_dir, config), work_dir, _FIRST_TEST_SECONDS)


def test_the_compute_binary_reaches_the_stress_loop(lane_run):
    _, run = lane_run
    assert "Iteration:" in run.output, run.output
    assert "Press ENTER" not in run.output


def test_a_clean_first_test_prints_its_pass_line_and_no_finding(lane_run):
    _, run = lane_run
    assert re.search(r"Running\s+BKT:\s*Passed", run.output), run.output
    assert YCruncherBackend().findings(run.output, Path(".")) == ()


def test_every_compute_thread_may_run_only_on_the_lanes_cpus(lane_run):
    cpus, run = lane_run
    assert run.alive_at_stop, run.output
    assert run.thread_cpus and run.thread_cpus <= set(cpus), f"threads may run on {sorted(run.thread_cpus)}"


def test_our_sigterm_is_graded_as_our_stop(lane_run):
    _, run = lane_run
    assert run.alive_at_stop, run.output
    assert exit_signal(run.returncode, signal.SIGTERM) is None, f"SIGTERM produced exit {run.returncode}"


@pytest.mark.parametrize("mode", list(StressMode))
def test_every_modes_config_starts_the_stress_loop(tmp_path, mode):
    config = StressConfig(mode=mode, cpus=_two_cpus(), threads=2, memory_mb=200)
    run = _run(_command(tmp_path, config), tmp_path, _STARTUP_SECONDS)
    assert "Iteration:" in run.output, run.output
    assert _outcomes(run, tmp_path) == set(), run.output


def test_an_unknown_test_tag_is_not_started(tmp_path):
    config = StressConfig(mode=StressMode.SSE, cpus=_two_cpus(), threads=2, memory_mb=200)
    command = _command(tmp_path, config)
    stress_cfg = tmp_path / CONFIG_FILE
    stress_cfg.write_text(stress_cfg.read_text().replace('"BKT"', '"NOSUCHTEST"'))
    run = _run(command, tmp_path, _STARTUP_SECONDS)
    assert _outcomes(run, tmp_path) == {Outcome.NOT_STARTED}, run.output


def test_a_core_id_beyond_the_machine_is_not_started(tmp_path):
    config = StressConfig(mode=StressMode.SSE, cpus=((os.cpu_count() or 0) + 64,), memory_mb=200)
    run = _run(_command(tmp_path, config), tmp_path, _STARTUP_SECONDS)
    assert _outcomes(run, tmp_path) == {Outcome.NOT_STARTED}, run.output


def test_the_binary_offers_exactly_the_tests_we_name(tmp_path):
    backend = _backend()
    backend.prepare(tmp_path, StressConfig(mode=StressMode.SSE, cpus=_two_cpus(), memory_mb=200))
    compute = backend.get_command(StressConfig(cpus=_two_cpus()), tmp_path)[0]
    run = _run([compute, "skip-warnings", "colors:0", "stress", "-M:64m", "-D:1"], tmp_path, _STARTUP_SECONDS)
    offered = tuple(tag for number, tag in re.findall(r"^\s*(\d+)\s+(\w+)\s+\S", run.output, re.M) if int(number) >= 11)
    assert offered == ALL_TESTS, run.output


def test_the_backend_picks_the_binary_the_launcher_picks(tmp_path):
    backend = _backend()
    config = StressConfig(mode=StressMode.SSE, cpus=_two_cpus(), threads=2, memory_mb=200)
    backend.prepare(tmp_path, config)
    command = backend.get_command(config, tmp_path)
    log_path = tmp_path / "launcher.log"
    with log_path.open("wb") as sink:
        launcher = subprocess.Popen(
            [backend.require_binary(), *command[1:]],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            cwd=tmp_path,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + _STARTUP_SECONDS
        while "Iteration:" not in read_text(log_path) and time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        os.killpg(launcher.pid, signal.SIGKILL)
        launcher.wait()
    output = _ANSI_RE.sub("", read_text(log_path))
    assert f"Auto-Selecting: {Path(command[0]).name}" in output, output
