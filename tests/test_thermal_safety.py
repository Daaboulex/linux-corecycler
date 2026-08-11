"""Thermal-safety regression tests.

Covers the debounced temperature check (a load-ramp spike must not stop a test
and read as a core failure during auto-tuning) and the tuner's thermal config.
The tuner-side retry behavior is exercised in test_tuner_engine.py.
"""

from __future__ import annotations

from unittest.mock import patch

from corecycler.engine.execution import ThermalWatch
from corecycler.tuner.config import TunerConfig


def _watch(temps: list[float | None], **overrides) -> ThermalWatch:
    feed = iter(temps)
    kwargs = dict(
        max_temperature=95.0,
        grace_seconds=3.0,
        hard_margin=8.0,
        require_sensor=False,
        read=lambda: next(feed),
    )
    kwargs.update(overrides)
    return ThermalWatch(**kwargs)


class TestTemperatureDebounce:
    """A load-ramp spike must not trip; sustained over-temp must."""

    def test_transient_spike_within_grace_does_not_trip(self):
        watch = _watch([100.0, 100.0, 100.0])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            assert watch.safe() is True
            clock[0] = 1.0
            assert watch.safe() is True
            clock[0] = 2.5
            assert watch.safe() is True
        assert watch.tripped is False

    def test_sustained_over_temp_trips_after_grace(self):
        watch = _watch([100.0, 100.0])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            assert watch.safe() is True
            clock[0] = 3.1
            assert watch.safe() is False
        assert watch.tripped is True

    def test_spike_then_recovery_resets_window(self):
        watch = _watch([100.0, 100.0, 80.0, 100.0])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            assert watch.safe() is True
            clock[0] = 1.0
            assert watch.safe() is True
            clock[0] = 2.0
            assert watch.safe() is True
            clock[0] = 2.5
            assert watch.safe() is True
        assert watch.tripped is False

    def test_hard_ceiling_trips_immediately(self):
        watch = _watch([103.5])
        assert watch.safe() is False
        assert watch.tripped is True

    def test_hysteresis_governs_resume(self):
        watch = _watch([100.0, 92.0, 89.0], grace_seconds=0.0)
        assert watch.safe() is False
        assert watch.safe() is False
        assert watch.safe() is True
        assert watch.tripped is False

    def test_unreadable_temperature_does_not_block(self):
        watch = _watch([None])
        assert watch.safe() is True

    def test_unreadable_temperature_blocks_when_the_sensor_is_required(self):
        watch = _watch([None], require_sensor=True)
        assert watch.safe() is False


class TestThermalTunerConfig:
    """The tuner exposes the safety limit + retry policy (so it reaches the scheduler)."""

    def test_thermal_defaults(self):
        c = TunerConfig()
        assert c.max_temperature_c == 95.0
        assert c.over_temp_grace_seconds == 3.0
        assert c.over_temp_hard_margin_c == 8.0
        assert c.max_thermal_retries == 3
        assert c.thermal_cooldown_seconds == 5.0

    def test_out_of_range_temperature_rejected(self):
        assert any("max_temperature_c" in e for e in TunerConfig(max_temperature_c=200).validate())
        assert any("max_temperature_c" in e for e in TunerConfig(max_temperature_c=40).validate())

    def test_negative_retries_rejected(self):
        assert any("max_thermal_retries" in e for e in TunerConfig(max_thermal_retries=-1).validate())

    def test_negative_cooldown_rejected(self):
        assert any(
            "thermal_cooldown_seconds" in e
            for e in TunerConfig(thermal_cooldown_seconds=-1).validate()
        )

    def test_valid_thermal_config_has_no_errors(self):
        cfg = TunerConfig(
            max_temperature_c=90.0,
            over_temp_grace_seconds=2.0,
            over_temp_hard_margin_c=5.0,
            max_thermal_retries=2,
        )
        assert cfg.validate() == []

    def test_json_roundtrip_preserves_thermal(self):
        c = TunerConfig(max_temperature_c=88.0, max_thermal_retries=5)
        restored = TunerConfig.from_json(c.to_json())
        assert restored.max_temperature_c == 88.0
        assert restored.max_thermal_retries == 5
