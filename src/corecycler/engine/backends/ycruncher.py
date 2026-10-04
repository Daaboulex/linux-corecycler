"""y-cruncher stress test backend."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import LaneSignal, Outcome, StressBackend, StressConfig, StressMode, scan

if TYPE_CHECKING:
    from pathlib import Path

    from .base import FindingTable

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

FINDINGS: FindingTable = tuple(
    (re.compile(pattern, re.IGNORECASE), Outcome.COMPUTATION)
    for pattern in (
        r"Error\(s\) encountered",
        r"Coefficient is too large",
        r"Invalid Parameter",
        r"\bFAIL(?:ED)?\b",
        r"(?<!Stop on )\bError\b(?!\s+Checking)",
    )
)


@register_backend("y-cruncher")
class YCruncherBackend(StressBackend):
    name = "y-cruncher"

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        binary = self.require_binary()
        memory_mib = config.memory_mb if config.memory_mb and config.memory_mb > 0 else _DEFAULT_MEMORY_MIB
        cmd = [
            binary,
            *_HEADLESS_FLAGS,
            "stress",
            f"-M:{memory_mib}M",
            f"-D:{_PER_TEST_SECONDS}",
        ]
        cmd.extend(MODE_TO_ALGORITHMS.get(config.mode, ()))
        return cmd

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        return scan(_ANSI_RE.sub("", output), FINDINGS)
