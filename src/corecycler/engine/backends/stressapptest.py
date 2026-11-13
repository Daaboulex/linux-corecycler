"""stressapptest stress backend: Google's memory stress testing tool.

Like every backend it runs indefinitely (-s 86400) and the Supervisor stops it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import LaneSignal, Outcome, StressBackend, StressConfig, StressMode, scan

if TYPE_CHECKING:
    from pathlib import Path

    from .base import FindingTable

_PAGE_MB = 1
_EMPTY_PAGES_NUMERATOR = 2
_EMPTY_PAGES_DENOMINATOR = 5

ALLOCATION_REFUSALS: tuple[str, ...] = (
    "freepages < neededpages",
    "not enough pages for io",
    "failed to allocate memory",
    "no memory found to test",
)

FINDINGS: FindingTable = (
    (re.compile(r"miscompare", re.IGNORECASE), Outcome.COMPUTATION),
    (re.compile(r"hardware error", re.IGNORECASE), Outcome.COMPUTATION),
    (re.compile(r"Status: FAIL - test discovered HW problems"), Outcome.COMPUTATION),
    (re.compile(r"(?:found|with) [1-9]\d* hardware incidents?", re.IGNORECASE), Outcome.COMPUTATION),
    (re.compile(r"Status: FAIL - test encountered procedural errors"), Outcome.UNEXPLAINED),
    *((re.compile(re.escape(refusal), re.IGNORECASE), Outcome.NOT_STARTED) for refusal in ALLOCATION_REFUSALS),
)


@register_backend("stressapptest")
class StressapptestBackend(StressBackend):
    name = "stressapptest"

    @staticmethod
    def minimum_memory_mb(copy_threads: int) -> int:
        if copy_threads < 1:
            raise ValueError(f"stressapptest runs at least one copy thread, got {copy_threads}")
        empty_pages_needed = -(-copy_threads // _EMPTY_PAGES_NUMERATOR)
        return empty_pages_needed * _EMPTY_PAGES_DENOMINATOR * _PAGE_MB

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        cmd = [self.require_binary(), "-W", "-s", "86400", "-m", str(max(1, config.threads))]
        if config.memory_mb is not None:
            cmd += ["-M", str(config.memory_mb)]
        return cmd

    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        return scan(output, FINDINGS)

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE]
