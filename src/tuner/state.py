"""Tuner state dataclasses — per-core state and session metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class CoreState:
    """Per-core state in the auto-tuner state machine.

    Phases: not_started, coarse_search, fine_search, settled,
    confirming, confirmed, failed_confirm, backoff_preconfirm,
    backoff_confirming.
    """

    core_id: int
    phase: str = "not_started"
    current_offset: int = 0
    best_offset: int | None = None
    coarse_fail_offset: int | None = None
    confirm_attempts: int = 0
    baseline_offset: int = 0
    backoff_mode: bool = False
    consecutive_backoff_fails: int = 0
    backoff_fail_bound: int | None = None
    backoff_pass_bound: int | None = None
    in_test: bool = False


@dataclass(slots=True)
class TunerSession:
    """Metadata for a tuner session row."""

    id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    status: str = "running"  # running, paused, completed, validating, aborted
    bios_version: str = ""
    cpu_model: str = ""
    config_json: str = "{}"
    context_id: int | None = None
    notes: str = ""
