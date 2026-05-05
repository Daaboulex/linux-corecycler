"""Abstract base class for stress test backends."""

from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    pass

# Return codes indicating the process was intentionally killed by the scheduler
KILLED_BY_US_CODES: frozenset[int] = frozenset({-9, -15, 137, 143})

# Signal codes indicating CPU instability (CO too aggressive, hardware fault)
CRASH_SIGNALS: dict[int, str] = {
    -11: "SIGSEGV",
    -6: "SIGABRT",
    -7: "SIGBUS",
    -5: "SIGTRAP",
}


class StressMode(Enum):
    SSE = auto()
    AVX = auto()
    AVX2 = auto()
    AVX512 = auto()
    CUSTOM = auto()


class FFTPreset(Enum):
    SMALLEST = "smallest"  # 4K-21K
    SMALL = "small"  # 36K-248K
    LARGE = "large"  # 426K-8192K
    HUGE = "huge"  # 8960K-65536K
    ALL = "all"  # 4K-65536K
    MODERATE = "moderate"  # 1344K-4096K
    HEAVY = "heavy"  # 4K-1344K
    HEAVY_SHORT = "heavy_short"  # 4K-160K
    CUSTOM = "custom"


@dataclass(slots=True)
class StressConfig:
    mode: StressMode = StressMode.SSE
    fft_preset: FFTPreset = FFTPreset.SMALL
    fft_min: int | None = None  # custom range
    fft_max: int | None = None
    threads: int = 1
    memory_mb: int | None = None  # for linpack-style tests


@dataclass(slots=True)
class StressResult:
    core_id: int
    passed: bool
    duration_seconds: float
    error_message: str | None = None
    error_type: str | None = None  # "computation", "mce", "timeout", "crash"
    iterations_completed: int = 0
    last_fft_size: int | None = None


class StressBackend(ABC):
    """Base class for stress test backends (mprime, y-cruncher, stress-ng)."""

    name: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the stress test binary is installed and runnable."""

    @abstractmethod
    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        """Build the command line for the stress test (without taskset prefix)."""

    @abstractmethod
    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        """Parse stress test output. Returns (passed, error_message)."""

    @abstractmethod
    def get_supported_modes(self) -> list[StressMode]:
        """Return list of stress modes this backend supports."""

    def get_supported_fft_presets(self) -> list[FFTPreset]:
        """Return list of FFT presets this backend supports. Override if applicable."""
        return []

    def prepare(self, work_dir: Path, config: StressConfig) -> None:  # noqa: B027
        """Prepare working directory and config files before running. Override if needed."""

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:  # noqa: B027
        """Clean up after test run. Override if needed.

        Args:
            preserve_on_error: If True, keep diagnostic files (results, logs)
                for post-mortem analysis of failures.
        """

    @staticmethod
    def classify_exit_code(returncode: int) -> str | None:
        """Classify a process exit code.

        Returns:
            "killed_by_us" if intentionally terminated by scheduler
            "crash:<SIGNAL>" if killed by a crash signal (CPU instability)
            None for normal exit (check stdout/stderr for pass/fail)
        """
        if returncode in KILLED_BY_US_CODES:
            return "killed_by_us"
        signal_name = CRASH_SIGNALS.get(returncode)
        if signal_name:
            return f"crash:{signal_name}"
        return None

    def find_binary(self, name: str) -> str | None:
        """Find a binary on PATH."""
        try:
            result = subprocess.run(
                ["which", name], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return None
