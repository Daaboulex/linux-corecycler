"""stressapptest stress backend — Google's memory stress testing tool.

Note: Like all backends, stressapptest runs indefinitely and the
CoreScheduler handles timing by killing the process after
seconds_per_core. We pass -s 86400 (24h) so stressapptest doesn't
self-terminate before the scheduler stops it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import CRASH_SIGNALS, KILLED_BY_US_CODES, StressBackend, StressConfig, StressMode

if TYPE_CHECKING:
    from pathlib import Path


@register_backend("stressapptest")
class StressapptestBackend(StressBackend):
    name = "stressapptest"

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        # stressapptest auto-detects available CPUs from its affinity mask.
        # taskset (applied by CoreScheduler) constrains it to the target core's
        # logical CPUs. No explicit thread count needed — it will use exactly
        # the CPUs available in its affinity set.
        return [
            self.require_binary(),
            "-W",
            "-s", "86400",
        ]

    def parse_output(
        self, stdout: str, stderr: str, returncode: int
    ) -> tuple[bool, str | None]:
        # The scheduler kills stressapptest (a 24h run) before its final
        # "Status: PASS/FAIL" summary line, so detect the memory-error signatures it
        # logs DURING the run. Checking only the final summary meant a killed run
        # that had already found memory errors was reported as passed (false stable).
        lowered = (stdout + "\n" + stderr).lower()
        for signature in ("miscompare", "hardware error", "hardware incident", "status: fail"):
            if signature in lowered:
                return False, f"stressapptest: '{signature}' — memory errors detected"
        # A crash signal always wins, even over a final "Status: PASS": a clean run
        # exits 0 or is killed by the scheduler, never with a crash code, so a crash
        # exit is unambiguous instability and must never be masked by a printed PASS.
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
