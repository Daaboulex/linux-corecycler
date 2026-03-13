"""Tuner configuration — all search parameters with best-practice defaults."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass


@dataclass(slots=True)
class TunerConfig:
    """Configuration for the automated PBO Curve Optimizer tuner.

    All fields have sensible defaults for a typical Zen 4/5 CPU.
    ``max_offset`` is auto-clamped to the CPU generation's CO range
    by the engine before use.
    """

    # Search parameters
    start_offset: int = 0
    coarse_step: int = 5
    fine_step: int = 1
    direction: int = -1  # -1 = negative (undervolting), +1 = positive

    # Test durations (seconds)
    search_duration_seconds: int = 60
    confirm_duration_seconds: int = 300
    validate_duration_seconds: int = 300

    # Limits
    max_offset: int = -50
    max_confirm_retries: int = 2

    # Behavior
    cores_to_test: list[int] | None = None  # None = all physical cores
    test_order: str = "sequential"  # sequential, round_robin, weakest_first
    backend: str = "mprime"
    stress_mode: str = "SSE"
    fft_preset: str = "SMALL"

    # Clock stretch detection
    stretch_threshold_pct: float = 3.0  # treat as failure if stretch > this % during test

    # Safety
    abort_on_consecutive_failures: int = 0  # 0 = disabled

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> TunerConfig:
        d = json.loads(data)
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})

    def clamp_max_offset(self, co_range: tuple[int, int]) -> None:
        """Clamp max_offset to the CPU generation's supported CO range."""
        if self.direction < 0:
            self.max_offset = max(self.max_offset, co_range[0])
        else:
            self.max_offset = min(self.max_offset, co_range[1])
