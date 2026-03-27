"""Application settings and test profile management."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from engine.backends.base import FFTPreset, StressMode

CONFIG_DIR = Path.home() / ".config" / "corecycler"
DEFAULT_PROFILE = CONFIG_DIR / "default.json"


@dataclass(slots=True)
class TestProfile:
    name: str = "Default"
    backend: str = "mprime"
    stress_mode: str = "SSE"
    fft_preset: str = "SMALL"
    fft_min: int | None = None
    fft_max: int | None = None
    threads: int = 1
    seconds_per_core: int = 600
    iterations_per_core: int = 0
    cycle_count: int = 1
    stop_on_error: bool = False
    test_smt: bool = False
    cores_to_test: list[int] | None = None
    # Safety
    max_temperature: float = 95.0
    # Test mode preset
    test_mode: str = "STANDARD"
    # Advanced testing
    variable_load: bool = False
    idle_stability_test: float = 0.0
    idle_between_cores: float = 0.0

    def get_stress_mode(self) -> StressMode:
        return StressMode[self.stress_mode]

    def get_fft_preset(self) -> FFTPreset:
        return FFTPreset[self.fft_preset]


@dataclass(slots=True)
class AppSettings:
    work_dir: str = "/tmp/corecycler"
    theme: str = "system"
    poll_interval: float = 1.0
    show_smt_threads: bool = False
    profiles: list[TestProfile] = field(default_factory=lambda: [TestProfile()])
    active_profile_idx: int = 0
    window_width: int = 1200
    window_height: int = 800
    # History
    record_history: bool = True
    record_telemetry: bool = True
    history_retention_days: int = 90

    @property
    def active_profile(self) -> TestProfile:
        if 0 <= self.active_profile_idx < len(self.profiles):
            return self.profiles[self.active_profile_idx]
        return self.profiles[0] if self.profiles else TestProfile()


def load_settings() -> AppSettings:
    """Load settings from disk, or return defaults."""
    settings_file = CONFIG_DIR / "settings.json"
    if not settings_file.exists():
        return AppSettings()

    try:
        data = json.loads(settings_file.read_text())
        profiles = [TestProfile(**p) for p in data.pop("profiles", [TestProfile()])]
        return AppSettings(**data, profiles=profiles)
    except (json.JSONDecodeError, TypeError, KeyError):
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    """Save settings to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    settings_file = CONFIG_DIR / "settings.json"

    data = asdict(settings)
    settings_file.write_text(json.dumps(data, indent=2))


def load_profile(path: Path) -> TestProfile:
    """Load a test profile from a JSON file."""
    data = json.loads(path.read_text())
    return TestProfile(**data)


def save_profile(profile: TestProfile, path: Path) -> None:
    """Save a test profile to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(profile), indent=2))


def save_co_profile(
    offsets: dict[int, int],
    path: Path,
    cpu_model: str = "",
    source: str = "manual",
) -> None:
    """Save a CO offset profile to a JSON file."""
    from datetime import datetime, timezone

    data = {
        "format": "corecycler-co-profile",
        "version": 1,
        "cpu_model": cpu_model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "offsets": {str(k): v for k, v in sorted(offsets.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_co_profile(path: Path) -> dict[int, int]:
    """Load a CO offset profile from a JSON file.

    Returns {core_id: offset}. Ignores unknown fields for forward compatibility.
    """
    data = json.loads(path.read_text())
    raw = data.get("offsets", data)  # support bare {core: offset} dicts too
    return {int(k): int(v) for k, v in raw.items() if k.lstrip("-").isdigit()}
