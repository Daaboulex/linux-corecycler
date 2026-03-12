"""y-cruncher stress test backend."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .base import StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path


class YCruncherBackend(StressBackend):
    name = "y-cruncher"

    def __init__(self) -> None:
        self._binary: str | None = None

    def is_available(self) -> bool:
        # y-cruncher is typically not on PATH — check common locations
        for name in ("y-cruncher", "y_cruncher"):
            self._binary = self.find_binary(name)
            if self._binary:
                return True
        return False

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        if not self._binary:
            self.is_available()
        if not self._binary:
            raise RuntimeError("y-cruncher binary not found")

        # y-cruncher component stress test mode
        cmd = [
            self._binary,
            "stress",
            "-M",
            _mode_flag(config.mode),
            "-T",
            str(config.threads),
        ]
        return cmd

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = stdout + "\n" + stderr

        error_patterns = [
            r"Failed",
            r"FAILED",
            r"Error",
            r"Verification .* FAIL",
            r"Result: FAIL",
        ]
        for pattern in error_patterns:
            match = re.search(pattern, combined)
            if match:
                return False, f"y-cruncher error: {match.group(0)}"

        if returncode in (-9, -15, 137, 143, 0):
            return True, None

        return False, f"y-cruncher exited with code {returncode}"

    def cleanup(self, work_dir: Path) -> None:
        pass


def _mode_flag(mode: StressMode) -> str:
    match mode:
        case StressMode.AVX512:
            return "AVX512"
        case StressMode.AVX2:
            return "AVX2"
        case StressMode.AVX:
            return "AVX"
        case _:
            return "SSE"
