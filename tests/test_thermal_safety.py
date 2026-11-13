"""Thermal-safety regression tests.

Covers the debounced temperature check: a load-ramp spike must not stop a test
and read as a core failure during auto-tuning.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

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

    def test_a_spike_that_dips_under_the_limit_trips_only_once_its_over_time_reaches_the_grace(self):
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


class TestGraceWindow:
    @pytest.mark.req("K17.grace")
    def test_a_reading_jittering_at_the_limit_trips_once_its_time_over_adds_up_to_the_grace(self):
        watch, _ = _watch([])
        clock = [0.0]
        tripped_at = None
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            for step in range(20):
                clock[0] = step * 0.5
                if not watch.safe(95.1 if step % 2 == 0 else 94.9) and tripped_at is None:
                    tripped_at = clock[0]
        assert tripped_at == 6.0

    @pytest.mark.req("K17.grace")
    def test_time_over_the_limit_older_than_the_window_no_longer_counts(self):
        watch, _ = _watch([])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            for t, temp in ((0.0, 100.0), (2.0, 100.0), (2.5, 80.0), (20.0, 100.0), (21.5, 100.0)):
                clock[0] = t
                assert watch.safe(temp) is True
        assert watch.tripped is False

    def test_a_release_below_the_hysteresis_starts_the_count_afresh(self):
        watch, _ = _watch([], grace_seconds=1.0)
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            for t, temp, safe in ((0.0, 100.0, True), (1.0, 100.0, False), (2.0, 80.0, True), (2.5, 96.0, True)):
                clock[0] = t
                assert watch.safe(temp) is safe

    @pytest.mark.req("K17.grace")
    def test_a_reading_exactly_at_the_limit_counts_as_over_it(self):
        watch, _ = _watch([])
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            results = []
            for t in (0.0, 1.0, 2.0, 3.0):
                clock[0] = t
                results.append(watch.safe(95.0))
        assert results == [True, True, True, False]

    @pytest.mark.req("K17.margin")
    def test_a_reading_exactly_at_the_hard_ceiling_trips_at_once(self):
        watch, _ = _watch([])
        assert watch.safe(103.0) is False

    @pytest.mark.req("K17.grace")
    def test_an_overshoot_late_in_a_long_run_trips_after_its_grace(self):
        watch, _ = _watch([])
        clock = [0.0]
        tripped_at = None
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            for t in range(25):
                clock[0] = float(t)
                if not watch.safe(80.0 if t < 15 else 100.0) and tripped_at is None:
                    tripped_at = clock[0]
        assert tripped_at == 18.0
