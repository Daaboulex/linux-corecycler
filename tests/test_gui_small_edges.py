"""Small-GUI edge coverage: style formatters and completeness guard, core grid
layout teardown, config profile application."""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(cores: int = 4) -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=cores, ccds=1)
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
    return topo


class TestStyleFormatters:
    def test_scheduler_phase_label_capitalizes_and_tolerates_empty(self):
        from corecycler.gui.style import scheduler_phase_label

        assert scheduler_phase_label("coarse") == "Coarse"
        assert scheduler_phase_label("") == ""

    def test_duration_str_hours_and_days(self):
        from corecycler.gui.style import duration_str

        assert duration_str(3661) == "1h 01m"
        assert duration_str(90061) == "1d 01h"

    def test_span_str_missing_bound_is_absent(self):
        from corecycler.gui.style import ABSENT, span_str

        assert span_str(None, "2026-07-24T00:00:00") == ABSENT
        assert span_str("2026-07-24T00:00:00", None) == ABSENT

    def test_span_str_malformed_iso_is_absent(self):
        from corecycler.gui.style import ABSENT, span_str

        assert span_str("not-a-date", "also-not-a-date") == ABSENT

    def test_span_str_valid_range(self):
        from corecycler.gui.style import span_str

        assert span_str("2026-07-24T00:00:00", "2026-07-24T01:30:00") == "1h 30m"


class TestStyleCompletenessGuard:
    """_assert_complete is the import-time guard that a new phase/state/status
    cannot ship unstyled. Each arm must actually fire when its map is broken."""

    def test_missing_phase_raises(self, monkeypatch):
        from corecycler.gui import style

        trimmed = dict(style.PHASE_LABELS)
        trimmed.pop(next(iter(trimmed)))
        monkeypatch.setattr(style, "PHASE_LABELS", trimmed)
        with pytest.raises(AssertionError, match="missing phases"):
            style._assert_complete()

    def test_state_color_label_mismatch_raises(self, monkeypatch):
        from corecycler.gui import style

        trimmed = dict(style.STATE_COLORS)
        trimmed.pop(next(iter(trimmed)))
        monkeypatch.setattr(style, "STATE_COLORS", trimmed)
        with pytest.raises(AssertionError, match="disagree on states"):
            style._assert_complete()

    def test_unknown_grid_state_target_raises(self, monkeypatch):
        from corecycler.gui import style

        mapped = dict(style.PHASE_TO_GRID)
        mapped[next(iter(mapped))] = "no-such-grid-state"
        monkeypatch.setattr(style, "PHASE_TO_GRID", mapped)
        with pytest.raises(AssertionError, match="unknown grid states"):
            style._assert_complete()

    def test_unstyled_session_status_raises(self, monkeypatch):
        from corecycler.gui import style

        extra = dict(style.SESSION_STATUS_LABELS)
        extra["brand-new-status"] = "Brand new"
        monkeypatch.setattr(style, "SESSION_STATUS_LABELS", extra)
        with pytest.raises(AssertionError, match="missing statuses"):
            style._assert_complete()


class TestCoreGridEdges:
    def test_cell_shows_scheduler_phase_while_testing(self):
        _qapp()
        from corecycler.gui.widgets.core_grid import CoreCell

        cell = CoreCell(core_id=0, ccd=0, has_vcache=False)
        cell._state = "testing"
        cell._scheduler_phase = "coarse"
        cell._refresh_labels()
        assert "Coarse" in cell._status_label.text()

    def test_set_topology_clears_nested_layouts(self):
        _qapp()
        from PySide6.QtWidgets import QLabel, QVBoxLayout

        from corecycler.gui.widgets.core_grid import CoreGridWidget

        grid = CoreGridWidget(_topo())
        nested = QVBoxLayout()
        nested.addWidget(QLabel("nested"))
        grid._layout.addLayout(nested)

        grid.set_topology(_topo(2))

        assert set(grid._cells) == {0, 1}


class TestConfigTabProfile:
    def test_set_profile_applies_fft_bounds_and_clears_cores(self):
        _qapp()
        from corecycler.gui.config_tab import ConfigTab

        tab = ConfigTab()
        profile = tab.get_profile()
        profile.fft_min = 16
        profile.fft_max = 4096
        profile.cores_to_test = []
        tab.set_profile(profile)

        assert tab._fft_min_spin.value() == 16
        assert tab._fft_max_spin.value() == 4096
        assert tab._cores_input.text() == ""
