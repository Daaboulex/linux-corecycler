"""y-cruncher stress test backend."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import CRASH_SIGNALS, KILLED_BY_US_CODES, StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path

_HEADLESS_FLAGS = ("skip-warnings", "pause:-2", "status:none")
_DEFAULT_MEMORY_MIB = 1024
_PER_TEST_SECONDS = 30

MODE_TO_ALGORITHMS: dict[StressMode, tuple[str, ...]] = {
    StressMode.SSE: ("BKT",),
    StressMode.AVX: ("BKT", "BBP", "SFTv4", "SNT", "SVT"),
    StressMode.AVX2: (),
    StressMode.AVX512: (),
    StressMode.CUSTOM: (),
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_ERROR_PATTERNS: tuple[str, ...] = (
    r"Error\(s\) encountered",
    r"Coefficient is too large",
    r"Invalid Parameter",
    r"\bFAIL(?:ED)?\b",
    r"(?<!Stop on )\bError\b(?!\s+Checking)",
)


@register_backend("y-cruncher")
class YCruncherBackend(StressBackend):
    name = "y-cruncher"

    def __init__(self) -> None:
        self._binary: str | None = None

    def is_available(self) -> bool:
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

        memory_mib = config.memory_mb if config.memory_mb and config.memory_mb > 0 else _DEFAULT_MEMORY_MIB
        cmd = [
            self._binary,
            *_HEADLESS_FLAGS,
            "stress",
            f"-M:{memory_mib}M",
            f"-D:{_PER_TEST_SECONDS}",
        ]
        cmd.extend(MODE_TO_ALGORITHMS.get(config.mode, ()))
        return cmd

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = _ANSI_RE.sub("", stdout + "\n" + stderr)

        for pattern in _ERROR_PATTERNS:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return False, f"y-cruncher error: {match.group(0)}"

        if returncode in CRASH_SIGNALS:
            return False, f"y-cruncher crashed with {CRASH_SIGNALS[returncode]} (exit {returncode})"

        if returncode in KILLED_BY_US_CODES:
            return True, None

        if returncode == 0:
            return False, "y-cruncher exited on its own (code 0) without being stopped: verdict unavailable"

        return False, f"y-cruncher exited with code {returncode}: verdict unavailable"

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        pass
