"""stress-ng stress test backend."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import LaneSignal, Outcome, StressBackend, StressConfig, StressMode, scan

if TYPE_CHECKING:
    from pathlib import Path

    from .base import FindingTable

FINDINGS: FindingTable = (
    (re.compile(r"stress-ng: fail:(?!.*out of memory)"), Outcome.COMPUTATION),
    (re.compile(r"\(stressor failed\)"), Outcome.COMPUTATION),
    (re.compile(r"aborted early, no system resources"), Outcome.NOT_STARTED),
    (re.compile(r"choice '[^']*' not known"), Outcome.NOT_STARTED),
)

STRESSOR_SIGNAL: FindingTable = (
    (re.compile(r"stressor terminated with unexpected signal (?:4|7|8|11)\b"), Outcome.CPU_SIGNAL),
    (re.compile(r"stressor terminated with unexpected signal \d+"), Outcome.UNEXPLAINED),
)

FAILURE_SUMMARY: FindingTable = ((re.compile(r"unsuccessful run completed"), Outcome.COMPUTATION),)


@register_backend("stress-ng")
class StressNgBackend(StressBackend):
    name = "stress-ng"

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        binary = self.require_binary()
        method = _mode_to_method(config.mode)

        cmd = [
            binary,
            "--cpu",
            str(config.threads),
            "--cpu-method",
            method,
            "--verify",
            "--abort",
            "--metrics-brief",
            "--temp-path",
            str(work_dir),
        ]

        # cpu-method matrixprod verifies nothing; the --matrix twin is the checked computation
        if method == "matrixprod":
            cmd += ["--matrix", str(config.threads), "--matrix-method", "prod"]

        return cmd

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2]

    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        signalled = scan(output, STRESSOR_SIGNAL)
        return (*scan(output, FINDINGS), *(signalled or scan(output, FAILURE_SUMMARY)))


def _mode_to_method(mode: StressMode) -> str:
    match mode:
        case StressMode.SSE:
            return "matrixprod"
        case StressMode.AVX | StressMode.AVX2:
            return "fft"
        case _:
            return "matrixprod"
