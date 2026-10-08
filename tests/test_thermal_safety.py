"""Thermal-safety regression tests.

Covers the debounced temperature check: a load-ramp spike must not stop a test
and read as a core failure during auto-tuning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from corecycler.engine.execution import ThermalWatch

if TYPE_CHECKING:
    from collections.abc import Iterator


def _watch(temps: list[float | None], **overrides) -> tuple[ThermalWatch, Iterator[float | None]]:
    feed = iter(temps)
    kwargs = dict(
        max_temperature=95.0,
        grace_seconds=3.0,
        hard_margin=8.0,
        require_sensor=False,
    )
    kwargs.update(overrides)
    return ThermalWatch(**kwargs), feed


class TestTemperatureDebounce:
    """A load-ramp spike must not trip; sustained over-temp must."""

    def test_transient_spike_within_grace_does_not_trip(self):
        watch, feed = _watch([100.0, 100.0, 100.0])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            assert watch.safe(next(feed)) is True
            clock[0] = 1.0
            assert watch.safe(next(feed)) is True
            clock[0] = 2.5
            assert watch.safe(next(feed)) is True
        assert watch.tripped is False

    def test_sustained_over_temp_trips_after_grace(self):
        watch, feed = _watch([100.0, 100.0])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            assert watch.safe(next(feed)) is True
            clock[0] = 3.1
            assert watch.safe(next(feed)) is False
        assert watch.tripped is True

    def test_spike_then_recovery_resets_window(self):
        watch, feed = _watch([100.0, 100.0, 80.0, 100.0])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            assert watch.safe(next(feed)) is True
            clock[0] = 1.0
            assert watch.safe(next(feed)) is True
            clock[0] = 2.0
            assert watch.safe(next(feed)) is True
            clock[0] = 2.5
            assert watch.safe(next(feed)) is True
        assert watch.tripped is False

    def test_hard_ceiling_trips_immediately(self):
        watch, feed = _watch([103.5])
        assert watch.safe(next(feed)) is False
        assert watch.tripped is True

    def test_hysteresis_governs_resume(self):
        watch, feed = _watch([100.0, 92.0, 89.0], grace_seconds=0.0)
        assert watch.safe(next(feed)) is False
        assert watch.safe(next(feed)) is False
        assert watch.safe(next(feed)) is True
        assert watch.tripped is False

    def test_unreadable_temperature_does_not_block(self):
        watch, feed = _watch([None])
        assert watch.safe(next(feed)) is True

    def test_unreadable_temperature_blocks_when_the_sensor_is_required(self):
        watch, feed = _watch([None], require_sensor=True)
        assert watch.safe(next(feed)) is False
