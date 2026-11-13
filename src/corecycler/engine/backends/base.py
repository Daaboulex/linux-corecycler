"""Abstract base class for stress test backends."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum, StrEnum, auto
from typing import TYPE_CHECKING

from corecycler.config import tools

if TYPE_CHECKING:
    from pathlib import Path


class OutcomeKind(Enum):
    SILICON = auto()
    ENVIRONMENT = auto()
    APPARATUS = auto()
    PASS = auto()


class Outcome(StrEnum):
    kind: OutcomeKind

    CPU_SIGNAL = "crash", OutcomeKind.SILICON
    OWN_CORE_MCE = "mce", OutcomeKind.SILICON
    COMPUTATION = "computation", OutcomeKind.SILICON
    CLOCK_STRETCH = "clock_stretch", OutcomeKind.SILICON
    THERMAL = "thermal", OutcomeKind.ENVIRONMENT
    UNATTRIBUTED_MCE = "mce_unattributed", OutcomeKind.ENVIRONMENT
    KERNEL_TROUBLE = "kernel_trouble", OutcomeKind.ENVIRONMENT
    OUTSIDE_KILL = "killed", OutcomeKind.APPARATUS
    DID_NOT_STOP = "did_not_stop", OutcomeKind.APPARATUS
    CONTAINMENT_BROKE = "containment_broke", OutcomeKind.APPARATUS
    STALL = "stall", OutcomeKind.APPARATUS
    NOT_STARTED = "startup", OutcomeKind.APPARATUS
    UNEXPLAINED = "unexplained", OutcomeKind.APPARATUS
    NO_WORK_DONE = "no_work_done", OutcomeKind.APPARATUS
    UNVERIFIED = "unverified", OutcomeKind.APPARATUS
    HELD_TOO_LONG = "held_too_long", OutcomeKind.APPARATUS
    CUT_SHORT = "cut_short", OutcomeKind.APPARATUS
    PASS = "pass", OutcomeKind.PASS

    def __new__(cls, stored: str, kind: OutcomeKind) -> Outcome:
        member = str.__new__(cls, stored)
        member._value_ = stored
        member.kind = kind
        return member

    @property
    def rank(self) -> int:
        return _RANK_BY_DECLARATION_ORDER[self]


_RANK_BY_DECLARATION_ORDER: dict[Outcome, int] = {outcome: position for position, outcome in enumerate(Outcome)}


@dataclass(frozen=True, slots=True)
class LaneSignal:
    outcome: Outcome
    text: str

    def __post_init__(self) -> None:
        if self.outcome is Outcome.PASS:
            raise ValueError("a pass is the absence of every signal, never a signal")


FindingTable = tuple[tuple[re.Pattern[str], Outcome], ...]


def scan(text: str, table: FindingTable) -> tuple[LaneSignal, ...]:
    return tuple(
        LaneSignal(outcome, text[text.rfind("\n", 0, match.start()) + 1 :].partition("\n")[0].strip())
        for pattern, outcome in table
        if (match := pattern.search(text))
    )


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8", errors="replace")


class StressMode(Enum):
    SSE = auto()
    AVX = auto()
    AVX2 = auto()
    AVX512 = auto()
    CUSTOM = auto()


class FFTPreset(Enum):
    SMALLEST = "smallest"
    SMALL = "small"
    LARGE = "large"
    HUGE = "huge"
    ALL = "all"
    MODERATE = "moderate"
    HEAVY = "heavy"
    HEAVY_SHORT = "heavy_short"
    CUSTOM = "custom"


@dataclass(slots=True)
class StressConfig:
    mode: StressMode = StressMode.SSE
    fft_preset: FFTPreset = FFTPreset.SMALL
    fft_min: int | None = None
    fft_max: int | None = None
    threads: int = 1
    memory_mb: int | None = None
    cpus: tuple[int, ...] = ()
    tests: tuple[str, ...] = ()
    seconds_per_test: int | None = None
    binary: str | None = None
    longest_rest_s: float = 0.0


@dataclass(frozen=True, slots=True)
class StressResult:
    core_id: int
    outcome: Outcome
    detail: str
    duration_seconds: float
    output: str = ""
    iterations_completed: int = 0
    speeds: tuple[tuple[str, float, bool], ...] = ()
    frozen_seconds: float = 0.0

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS


class StressBackend(ABC):
    """Base class for every stress test backend."""

    name: str = "base"

    def __init__(self) -> None:
        self._binary: str | None = None

    def is_available(self) -> bool:
        """Locate the backend binary through the one resolver (see config.tools)."""
        resolution = self.resolution()
        self._binary = str(resolution.path) if resolution.path else None
        return self._binary is not None

    def resolution(self) -> tools.Resolution:
        """Where this backend's binary resolved from, or why it did not."""
        return tools.resolve(self.name)

    def require_binary(self) -> str:
        """The resolved binary path, or a RuntimeError naming the backend."""
        if not self._binary:
            self.is_available()
        if not self._binary:
            raise RuntimeError(f"{self.name} binary not found")
        return self._binary

    @abstractmethod
    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        """Build the command line for the stress test (without the containment prefix)."""

    @abstractmethod
    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        """Every signal this backend's own text names: the lane's output plus any results file it keeps."""

    def speeds(self, output: str) -> tuple[tuple[str, float, bool], ...]:
        """Each finished test's own speed figure, as (test, value, passed); none for a backend that prints none."""
        return ()

    def finished(self, output: str) -> int:
        """How many tests the output shows finished, passed or failed; by default one per speed figure."""
        return len(self.speeds(output))

    def progress_window(self, config: StressConfig) -> float | None:
        """Seconds with no newly finished test after which a run is hung, counting the run's planned rests and never
        its heat or unread holds; None when the backend cannot tell."""
        return None

    @abstractmethod
    def get_supported_modes(self) -> list[StressMode]:
        """Return list of stress modes this backend supports."""

    def get_supported_fft_presets(self) -> list[FFTPreset]:
        """Return list of FFT presets this backend supports. Override if applicable."""
        return []

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        """Prepare working directory and config files before running. Override if needed."""
        return None

    def assert_prepared(self, work_dir: Path) -> None:
        """Raise OSError when the prepared state a launch depends on is not readable.

        A backend that launches against absent config silently falls back to its
        own defaults (mprime: one self-pinned worker per detected core), so the
        launch must be refused instead. Override where config files exist."""
        return None
