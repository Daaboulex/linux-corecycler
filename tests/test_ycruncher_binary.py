"""Contract tests that run a real y-cruncher binary.

They fail loudly if an upstream y-cruncher update breaks a backend assumption
(CLI flags, algorithm names, output format, or the kill-signal exit code),
rather than letting the fixture-based unit tests pass on stale assumptions.
Skipped only when no y-cruncher binary is present.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends.base import KILLED_BY_US_CODES, StressConfig, StressMode
from corecycler.engine.backends.ycruncher import MODE_TO_ALGORITHMS, YCruncherBackend

pytestmark = pytest.mark.slow

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _discover_binary() -> str | None:
    override = os.environ.get("YCRUNCHER_BIN")
    if override and Path(override).is_file():
        return override
    backend = YCruncherBackend()
    if backend.is_available():
        return backend._binary
    return None


_BINARY = _discover_binary()
_WORKDIR = str(Path(_BINARY).parent) if _BINARY else "."
_MAPPED_ALGORITHMS = sorted({algo for algos in MODE_TO_ALGORITHMS.values() for algo in algos})

requires_ycruncher = pytest.mark.skipif(
    _BINARY is None,
    reason="no y-cruncher binary (set YCRUNCHER_BIN or put y-cruncher on PATH)",
)


def _run(args: list[str], run_seconds: float) -> tuple[int, str]:
    proc = subprocess.Popen(
        [_BINARY, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=_WORKDIR,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
    )
    time.sleep(run_seconds)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        out, _ = proc.communicate()
    return proc.returncode, out


def _stress(algorithms: list[str], run_seconds: float) -> tuple[int, str]:
    return _run(
        ["skip-warnings", "pause:-2", "status:none", "stress", "-M:200M", "-D:2", *algorithms],
        run_seconds,
    )


@requires_ycruncher
class TestYCruncherBinaryContract:
    def test_backend_command_reaches_stress_loop(self):
        backend = YCruncherBackend()
        backend._binary = _BINARY
        cmd = backend.get_command(StressConfig(mode=StressMode.SSE), Path(_WORKDIR))
        assert cmd[0] == _BINARY
        rc, out = _run(cmd[1:], 6)
        assert "Invalid Parameter" not in out
        assert "Press ENTER" not in out
        assert "Start Stress-Testing!" in out
        assert "Iteration:" in out

    def test_scheduler_kill_yields_killed_by_us_code(self):
        rc, _out = _stress(["BKT"], 4)
        assert rc in KILLED_BY_US_CODES, (
            f"SIGTERM produced exit {rc}; the backend treats any non-killed exit as a failure, "
            "so a trapped signal here would flip every real run to a false verdict"
        )

    def test_pass_line_format_present(self):
        rc, out = _stress(["BKT"], 6)
        assert re.search(r"Running\s+BKT:\s*Passed", _ANSI_RE.sub("", out)), (
            "y-cruncher stopped emitting the 'Running <algo>: Passed' line the backend was built against"
        )

    @pytest.mark.parametrize("algo", _MAPPED_ALGORITHMS)
    def test_mapped_algorithm_still_accepted(self, algo):
        rc, out = _stress([algo], 3)
        assert "Invalid Parameter" not in out, f"y-cruncher rejected {algo!r}; upstream may have renamed it"
        assert "Start Stress-Testing!" in out

    def test_invalid_algorithm_is_rejected_loudly(self):
        rc, out = _stress(["NOSUCHTEST"], 3)
        assert "Invalid Parameter" in out

    def test_parse_output_agrees_with_real_killed_run(self):
        rc, out = _stress(["BKT"], 5)
        passed, msg = YCruncherBackend().parse_output(out, "", rc)
        assert passed, msg

    def test_parse_output_fails_a_real_invalid_parameter(self):
        rc, out = _stress(["NOSUCHTEST"], 3)
        passed, msg = YCruncherBackend().parse_output(out, "", rc)
        assert not passed
        assert "Invalid Parameter" in msg
