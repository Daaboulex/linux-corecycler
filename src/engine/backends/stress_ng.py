"""stress-ng stress test backend — always available on NixOS."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .base import StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path


class StressNgBackend(StressBackend):
    name = "stress-ng"

    def __init__(self) -> None:
        self._binary: str | None = None

    def is_available(self) -> bool:
        self._binary = self.find_binary("stress-ng")
        return self._binary is not None

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        if not self._binary:
            self.is_available()
        if not self._binary:
            raise RuntimeError("stress-ng binary not found")

        # select stressor method based on mode
        method = _mode_to_method(config.mode)

        cmd = [
            self._binary,
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

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = stdout + "\n" + stderr

        # stress-ng verification failures
        # Note: avoid matching "0 FAILED" in metrics output (false positive)
        error_patterns = [
            r"[1-9]\d*\s+FAILED",  # "N FAILED" where N > 0
            r"\bFAIL\b(?!\w)",  # standalone FAIL (not part of FAILED)
            r"verification error",
            r"computation mismatch",
            r"error.*incorrect",
            r"killed by signal \d+",
            r"out of memory",
        ]
        for pattern in error_patterns:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return False, f"stress-ng error: {match.group(0)}"

        # killed by us (timeout) = passed, but only if no error patterns matched above
        if returncode in (-9, -15, 137, 143, 0):
            return True, None

        # SIGSEGV/SIGABRT/SIGBUS = likely CO instability crash
        signal_names = {-11: "SIGSEGV", -6: "SIGABRT", -7: "SIGBUS", -5: "SIGTRAP"}
        if returncode in signal_names:
            return False, f"stress-ng crashed with {signal_names[returncode]} (exit {returncode})"

        return False, f"stress-ng exited with code {returncode}"

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
