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


class TestTunerWords:
    def test_a_pause_is_said_by_its_reason_s_label(self):
        from corecycler.gui.style import pause_text

        assert pause_text("thermal: the CPU did not cool") == "Too hot: the CPU did not cool"
        assert pause_text("not_a_reason: as stored") == "not_a_reason: as stored"
        assert pause_text("thermal") == "thermal"

    @pytest.mark.req("UI1.range")
    def test_the_power_state_reads_as_words(self):
        from corecycler.gui.style import power_text

        pairs = (
            ("power profile", "balanced"),
            ("platform profile", "power-saver"),
            ("governor", "powersave"),
            ("EPP", "power"),
            ("boost", "1"),
            ("scaling maximum MHz", "4300"),
            ("new knob", "x"),
        )
        assert power_text(pairs) == (
            "profile balanced, platform profile power-saver, governor powersave, EPP power, boost on, max MHz 4300,"
            " new knob x"
        )
        assert power_text((("boost", "0"),)) == "boost off"

    @pytest.mark.req("UI1.stage")
    def test_a_stage_is_said_in_words(self):
        from corecycler.gui.style import stage_label

        assert (stage_label("transient"), stage_label("later")) == ("Changing loads", "later")


class TestStyleCompletenessGuard:
    """_assert_complete is the import-time guard that a new phase/state/status
    cannot ship unstyled. Each arm must actually fire when its map is broken."""

    @pytest.mark.parametrize(
        ("table", "kind"), [("TUNER_PHASE_LABELS", "Phase"), ("PAUSE_LABELS", "PauseReason"), ("STAGE_LABELS", "Stage")]
    )
    @pytest.mark.req("UI1.pause")
    def test_a_member_without_a_label_raises(self, monkeypatch, table, kind):
        from corecycler.gui import style

        trimmed = dict(getattr(style, table))
        dropped = next(iter(trimmed))
        trimmed.pop(dropped)
        monkeypatch.setattr(style, table, trimmed)
        with pytest.raises(AssertionError, match=f"{kind} has no label for .*{dropped.name}"):
            style._assert_complete()

    def test_state_color_label_mismatch_raises(self, monkeypatch):
        from corecycler.gui import style

        extra = dict(style.GRID_STATE_LABELS)
        extra["brand-new-state"] = "Brand new"
        monkeypatch.setattr(style, "GRID_STATE_LABELS", extra)
        with pytest.raises(AssertionError, match="disagree on states"):
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

    def test_cell_shows_the_label_of_each_machine_phase_and_an_unknown_phase_as_given(self):
        _qapp()
        from corecycler.gui.style import TUNER_PHASE_LABELS
        from corecycler.gui.widgets.core_grid import CoreCell
        from corecycler.tuner.machine.types import Phase

        cell = CoreCell(core_id=0, ccd=0, has_vcache=False)
        shown = {}
        for value in [*(phase.value for phase in Phase), "confirm"]:
            cell.update_telemetry(tuner_phase=value)
            shown[value] = cell._status_label.text()
        assert shown == {phase.value: TUNER_PHASE_LABELS[phase] for phase in Phase} | {"confirm": "confirm"}
        assert all(shown[phase.value] != phase.value for phase in Phase)

    def test_set_topology_clears_nested_layouts(self):
        _qapp()
        from PySide6.QtWidgets import QLabel, QVBoxLayout

        from corecycler.gui.widgets.core_grid import CoreGridWidget

        grid = CoreGridWidget(_topo())
        inner = QVBoxLayout()
        inner.addWidget(QLabel("deep"))
        nested = QVBoxLayout()
        nested.addWidget(QLabel("nested"))
        nested.addLayout(inner)
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
        assert tab.get_profile().cores_to_test is None
