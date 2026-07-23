"""LiveChart widget: data handling and paint under empty/single/degenerate ranges."""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)


def _chart(**kw):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from corecycler.gui.widgets.charts import LiveChart

    return LiveChart(**kw)


class TestLiveChart:
    def test_add_value_tracks_current(self):
        c = _chart(title="Freq", unit="MHz", min_val=0, max_val=6000)
        c.add_value(5200)
        assert c._current == 5200
        assert list(c._data) == [5200]

    def test_ring_buffer_bounded(self):
        from corecycler.gui.widgets.charts import MAX_POINTS

        c = _chart(max_val=100)
        for i in range(MAX_POINTS + 50):
            c.add_value(i)
        assert len(c._data) == MAX_POINTS

    def test_clear_resets(self):
        c = _chart()
        c.add_value(10)
        c.clear()
        assert not c._data
        assert c._current == 0

    def test_paint_empty_does_not_crash(self):
        c = _chart()
        c.resize(200, 100)
        c.grab()

    def test_paint_single_point(self):
        c = _chart(min_val=0, max_val=100)
        c.resize(200, 100)
        c.add_value(42)
        c.grab()

    def test_paint_many_points(self):
        c = _chart(min_val=0, max_val=100)
        c.resize(200, 100)
        for v in range(80):
            c.add_value(v)
        c.grab()

    def test_paint_degenerate_range(self):
        c = _chart(min_val=50, max_val=50)
        c.resize(200, 100)
        c.add_value(50)
        c.grab()
