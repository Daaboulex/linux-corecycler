"""Tuner configuration — all search parameters with best-practice defaults."""

from __future__ import annotations

import dataclasses
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
    test_order: str = "sequential"  # sequential, round_robin, weakest_first, ccd_alternating, ccd_round_robin
    backend: str = "mprime"
    stress_mode: str = "SSE"
    fft_preset: str = "SMALL"

    # Clock stretch detection
    stretch_threshold_pct: float = 3.0  # treat as failure if stretch > this % during test

    # Backoff algorithm
    midpoint_jump_threshold: int = 3  # after this many consecutive backoff fails, jump to midpoint

    # Safety
    abort_on_consecutive_failures: int = 0  # 0 = disabled

    # Inherit current CO offsets from SMU as starting point
    inherit_current: bool = False

    # Automatically run multi-core validation after all cores are individually confirmed
    auto_validate: bool = True

    # Backoff tuning
    backoff_preconfirm_multiplier: float = 2.0

    # Multi-mode hardening tiers (run after confirmation)
    hardening_tiers: list[dict[str, str]] = dataclasses.field(
        default_factory=lambda: [
            {"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"},
            {"backend": "mprime", "stress_mode": "SSE", "fft_preset": "LARGE"},
        ]
    )

    # Per-core time budget for search phases (seconds)
    max_core_time_seconds: int = 7200

    # Backoff steps after system crash (multiplied by fine_step direction)
    crash_penalty_steps: int = 3

    # Enable S4 rapid transition validation
    validate_transitions: bool = True

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))

    @classmethod
    def from_json(cls, data: str) -> TunerConfig:
        d = json.loads(data)
        return cls(**{k: v for k, v in d.items() if k in cls.__slots__})

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if config is valid."""
        errors = []
        if self.direction not in (-1, 1):
            errors.append(f"direction must be -1 or 1, got {self.direction}")
        if self.fine_step > self.coarse_step:
            errors.append(f"fine_step ({self.fine_step}) must be <= coarse_step ({self.coarse_step})")
        if self.cores_to_test is not None and len(self.cores_to_test) == 0:
            errors.append("cores_to_test is empty — no cores to test")
        if self.search_duration_seconds < 1:
            errors.append("search_duration_seconds must be >= 1")
        if self.confirm_duration_seconds < 1:
            errors.append("confirm_duration_seconds must be >= 1")
        if not 1 <= self.crash_penalty_steps <= 10:
            errors.append("crash_penalty_steps must be 1-10")
        if not 1800 <= self.max_core_time_seconds <= 14400:
            errors.append("max_core_time_seconds must be 1800-14400")
        for i, tier in enumerate(self.hardening_tiers):
            if not isinstance(tier, dict):
                errors.append(f"hardening_tiers[{i}] must be a dict")
            elif not all(k in tier for k in ("backend", "stress_mode", "fft_preset")):
                errors.append(f"hardening_tiers[{i}] missing required keys: backend, stress_mode, fft_preset")
        return errors

    def clamp_max_offset(self, co_range: tuple[int, int]) -> None:
        """Clamp max_offset to the CPU generation's supported CO range."""
        if self.direction < 0:
            self.max_offset = max(self.max_offset, co_range[0])
        else:
            self.max_offset = min(self.max_offset, co_range[1])
