"""Tuner configuration — all search parameters with best-practice defaults."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import asdict, dataclass


def _json_value_ok(default: object, value: object) -> bool:
    """True when a JSON-decoded value is type-compatible with a config field's
    default. ``from_json`` uses this to DROP a wrong-typed field and fall back to
    the safe default (fail closed), so a corrupted or hand-edited config_json can
    never smuggle a None/str/int into a field the engine treats as an int/list/bool
    -- which crashed both start() (via validate()) and resume() (which skips it)."""
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(default, str):
        return isinstance(value, str)
    if isinstance(default, list):
        return isinstance(value, list)
    if default is None:  # the optional list field (cores_to_test)
        return value is None or isinstance(value, list)
    return True


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

    # Resume-crash circuit breaker. After this many consecutive crash-resumes with
    # no surviving test in between, force every core to CO=0 (stock) and quarantine
    # the session instead of re-applying a profile that keeps crashing the machine.
    resume_crash_quarantine_threshold: int = 3

    # Fail closed when no CPU temperature sensor is readable: refuse to drive a
    # stress test with zero thermal protection unless the user explicitly opts in.
    allow_missing_thermal_sensor: bool = False

    # Thermal safety (plumbed into the per-core scheduler). A thermal stop is
    # not a stability verdict: the tuner retries the same offset rather than
    # recording a failure, up to max_thermal_retries, then aborts.
    max_temperature_c: float = 95.0  # stop a test if CPU exceeds this
    over_temp_grace_seconds: float = 3.0  # sustained over-limit time before stopping
    over_temp_hard_margin_c: float = 8.0  # instant stop at max + this (runaway)
    max_thermal_retries: int = 3  # retry same offset this many times before aborting
    thermal_cooldown_seconds: float = 5.0  # real wall-clock cooldown before a thermal retry

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
        """Parse a config from JSON. Fails closed: malformed JSON, a non-object
        payload, or any wrong-typed field (e.g. a corrupted or hand-edited DB row)
        falls back to the safe default rather than raising, so start/resume/abort
        never break on a bad config_json -- a None hardening_tiers or a str step no
        longer crashes validate()/the engine; it reverts to the default."""
        try:
            d = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return cls()
        if not isinstance(d, dict):
            return cls()
        defaults = cls()
        clean = {
            k: v for k, v in d.items()
            if k in cls.__slots__ and _json_value_ok(getattr(defaults, k), v)
        }
        return cls(**clean)

    def validate(self) -> list[str]:
        """Return list of validation errors, empty if config is valid."""
        errors = []
        if self.direction not in (-1, 1):
            errors.append(f"direction must be -1 or 1, got {self.direction}")
        if self.coarse_step < 1:
            errors.append(f"coarse_step must be >= 1, got {self.coarse_step}")
        if self.fine_step < 1:
            errors.append(f"fine_step must be >= 1, got {self.fine_step}")
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
        if not 1 <= self.resume_crash_quarantine_threshold <= 20:
            errors.append("resume_crash_quarantine_threshold must be 1-20")
        if not 1800 <= self.max_core_time_seconds <= 14400:
            errors.append("max_core_time_seconds must be 1800-14400")
        for i, tier in enumerate(self.hardening_tiers):
            if not isinstance(tier, dict):
                errors.append(f"hardening_tiers[{i}] must be a dict")
            elif not all(k in tier for k in ("backend", "stress_mode", "fft_preset")):
                errors.append(f"hardening_tiers[{i}] missing required keys: backend, stress_mode, fft_preset")
        if not 60 <= self.max_temperature_c <= 110:
            errors.append(f"max_temperature_c must be 60-110, got {self.max_temperature_c}")
        if self.over_temp_grace_seconds < 0:
            errors.append("over_temp_grace_seconds must be >= 0")
        if self.over_temp_hard_margin_c < 0:
            errors.append("over_temp_hard_margin_c must be >= 0")
        if self.max_thermal_retries < 0:
            errors.append("max_thermal_retries must be >= 0")
        if self.thermal_cooldown_seconds < 0:
            errors.append("thermal_cooldown_seconds must be >= 0")
        if self.validate_duration_seconds < 1:
            errors.append("validate_duration_seconds must be >= 1")
        if self.max_confirm_retries < 0:
            errors.append("max_confirm_retries must be >= 0")
        if self.midpoint_jump_threshold < 1:
            errors.append("midpoint_jump_threshold must be >= 1")
        if self.abort_on_consecutive_failures < 0:
            errors.append("abort_on_consecutive_failures must be >= 0")
        if self.backoff_preconfirm_multiplier <= 0:
            errors.append("backoff_preconfirm_multiplier must be > 0")
        if self.stretch_threshold_pct < 0:
            errors.append("stretch_threshold_pct must be >= 0")
        return errors

    def clamp_max_offset(self, co_range: tuple[int, int]) -> None:
        """Clamp max_offset to the CPU generation's supported CO range."""
        if self.direction < 0:
            self.max_offset = max(self.max_offset, co_range[0])
        else:
            self.max_offset = min(self.max_offset, co_range[1])
