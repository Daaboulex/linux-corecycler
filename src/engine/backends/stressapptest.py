"""stressapptest stress backend — Google's memory stress testing tool.

Note: Like all backends, stressapptest runs indefinitely and the
CoreScheduler handles timing by killing the process after
seconds_per_core. We pass -s 86400 (24h) so stressapptest doesn't
self-terminate before the scheduler stops it.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from engine.backends import register_backend

from .base import KILLED_BY_US_CODES, StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path


@register_backend("stressapptest")
class StressapptestBackend(StressBackend):
    name = "stressapptest"

    def is_available(self) -> bool:
        return shutil.which("stressapptest") is not None

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        # stressapptest auto-detects available CPUs from its affinity mask.
        # taskset (applied by CoreScheduler) constrains it to the target core's
        # logical CPUs. No explicit thread count needed — it will use exactly
        # the CPUs available in its affinity set.
        return [
            "stressapptest",
            "-W",
            "-s", "86400",
        ]

    def parse_output(
        self, stdout: str, stderr: str, returncode: int
    ) -> tuple[bool, str | None]:
        if "Status: FAIL" in stdout:
            return False, "stressapptest: FAIL — memory errors detected"
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
