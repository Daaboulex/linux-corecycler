"""stress-ng stress test backend — always available on NixOS."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import LaneSignal, Outcome, StressBackend, StressConfig, StressMode, scan

if TYPE_CHECKING:
    from pathlib import Path

    from .base import FindingTable

FINDINGS: FindingTable = tuple(
    (re.compile(pattern, re.IGNORECASE), Outcome.COMPUTATION)
    for pattern in (
        r"[1-9]\d*\s+FAILED",
        r"\bFAIL\b(?!\w)",
        r"verification error",
        r"computation mismatch",
        r"error.*incorrect",
        r"killed by signal \d+",
        r"out of memory",
    )
)


@register_backend("stress-ng")
class StressNgBackend(StressBackend):
    name = "stress-ng"

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        binary = self.require_binary()

        # select stressor method based on mode
        method = _mode_to_method(config.mode)

        cmd = [
            binary,
            "--cpu",
            str(config.threads),
            "--cpu-method",
            method,
            "--verify",  # verify computations for error detection
            "--metrics-brief",
            "--temp-path",
            str(work_dir),
        ]

        # Add matrix verification stressor alongside cpu for SSE mode.
        # matrixprod has no built-in verification — adding matrix stressor
        # with --verify provides actual computation checking.
        if method == "matrixprod":
            cmd += ["--matrix", str(config.threads), "--matrix-method", "prod"]

        return cmd

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)

    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        return scan(output, FINDINGS)

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        pass


def _mode_to_method(mode: StressMode) -> str:
    match mode:
        case StressMode.SSE:
            return "matrixprod"
        case StressMode.AVX:
            return "fft"
        case StressMode.AVX2:
            return "fft"  # stress-ng doesn't distinguish AVX/AVX2 methods
        case _:
            return "matrixprod"
