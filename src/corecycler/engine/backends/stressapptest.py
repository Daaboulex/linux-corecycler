"""stressapptest stress backend: Google's memory stress testing tool.

Like every backend it runs indefinitely (-s 86400) and the scheduler stops it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import CRASH_SIGNALS, KILLED_BY_US_CODES, StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path

_PAGE_MB = 1
_EMPTY_PAGES_NUMERATOR = 2
_EMPTY_PAGES_DENOMINATOR = 5

ALLOCATION_REFUSALS: tuple[str, ...] = (
    "freepages < neededpages",
    "not enough pages for io",
    "failed to allocate memory",
    "no memory found to test",
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

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        lowered = (stdout + "\n" + stderr).lower()
        for refusal in ALLOCATION_REFUSALS:
            if refusal in lowered:
                return False, f"stressapptest could not allocate its test memory ('{refusal}'): verdict unavailable"
        for signature in ("miscompare", "hardware error", "hardware incident", "status: fail"):
            if signature in lowered:
                return False, f"stressapptest: '{signature}' — memory errors detected"
        if returncode in CRASH_SIGNALS:
            return False, f"stressapptest crashed with {CRASH_SIGNALS[returncode]} (exit {returncode})"
        if "Status: PASS" in stdout:
            return True, None
        if returncode in KILLED_BY_US_CODES:
            return True, None
        if returncode != 0:
            return False, f"stressapptest exited with code {returncode}"
        return True, None

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        pass
