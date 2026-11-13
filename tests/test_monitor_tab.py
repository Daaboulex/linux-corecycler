"""MonitorTab + CoreFreqBar: readout formatting, view toggle, power limits, paint."""

from __future__ import annotations

import sys as _sys
from pathlib import Path
from types import SimpleNamespace

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.monitor.hwmon import Sensors


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(ccds=2):
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=8, ccds=ccds)
    per = 8 // ccds
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=cid // per, ccx=None, logical_cpus=(cid, cid + 8))
    return topo


def _bar(core_id=0):
    _qapp()
    from corecycler.gui.monitor_tab import CoreFreqBar

    return CoreFreqBar(core_id, f"C{core_id}", max_freq=6000)


def _tab(topo=None):
    _qapp()
    from corecycler.gui.monitor_tab import MonitorTab

    return MonitorTab(Sensors(Path("/nonexistent")), False, topology=topo or _topo())


class TestCoreFreqBarColumns:
    @pytest.mark.req("T22.monitor.bars")
    @pytest.mark.parametrize(
        ("usage", "text", "busy"), [(None, " --%", False), (50.0, " 50%", False), (51.0, " 51%", True)]
    )
    def test_a_core_reads_busy_only_above_half_load(self, usage, text, busy):
        from corecycler.gui.style import theme

        bar = _bar()
        bar.update_data(5000.0, usage_pct=usage)
        assert bar._columns()[0] == (text, theme.COLOR_PASS if busy else theme.COLOR_MUTED)

    @pytest.mark.req("T22.monitor.bars")
    @pytest.mark.parametrize(
        ("usage", "stretch", "shown"), [(6.0, 2.0, "S: 2.0%"), (5.0, 2.0, ""), (None, 2.0, ""), (6.0, None, "")]
    )
    def test_stretch_shows_only_for_a_loaded_core_with_a_reading(self, usage, stretch, shown):
        bar = _bar()
        bar.update_data(5000.0, usage_pct=usage, stretch_pct=stretch)
        assert bar._columns()[2][0].strip() == shown

    @pytest.mark.req("T22.monitor.bars")
    @pytest.mark.parametrize(("freq", "ratio"), [(None, 0.0), (3000.0, 0.5), (7000.0, 1.0)])
    def test_the_bar_fills_by_the_clock_against_its_scale(self, freq, ratio):
        bar = _bar()
        bar.update_data(freq)
        assert bar._fill_ratio() == pytest.approx(ratio)

    @pytest.mark.req("T22.monitor.bars")
    @pytest.mark.parametrize(
        ("stretch", "colour"),
        [(1.0, "COLOR_MUTED_DARK"), (1.1, "COLOR_WARN_SOFT"), (3.0, "COLOR_WARN_SOFT"), (3.1, "CHART_TEMP")],
    )
    def test_stretch_colours_change_just_above_1_and_3_percent(self, stretch, colour):
        from corecycler.gui.style import theme

        bar = _bar()
        bar.update_data(5000.0, usage_pct=90.0, stretch_pct=stretch)
        assert bar._columns()[2][1] == getattr(theme, colour)

    @pytest.mark.req("T22.monitor.bars")
    @pytest.mark.parametrize(
        ("temp", "colour"),
        [(69.0, "COLOR_MUTED"), (70.0, "COLOR_WARN_SOFT"), (84.0, "COLOR_WARN_SOFT"), (85.0, "CHART_TEMP")],
    )
    def test_temperature_colours_change_at_70_and_85(self, temp, colour):
        from corecycler.gui.style import theme

        bar = _bar()
        bar.update_data(5000.0, temp=temp)
        assert bar._columns()[4] == (f"{temp:3.0f}C", getattr(theme, colour))

    def test_any_positive_clock_is_shown(self):
        from corecycler.gui.monitor_tab import CoreFreqBar

        assert CoreFreqBar._freq_text(1.0, 0) == "1MHz"


class TestCoreFreqBar:
    def test_idle_readout_has_no_false_ceiling(self):
        from corecycler.gui.monitor_tab import CoreFreqBar

        assert CoreFreqBar._freq_text(0, 5000) == "  -- MHz"

    def test_live_readout_with_and_without_ceiling(self):
        from corecycler.gui.monitor_tab import CoreFreqBar

        assert CoreFreqBar._freq_text(5200, 5500) == "5200/5500MHz"
        assert CoreFreqBar._freq_text(5200, 0) == "5200MHz"

    def test_update_and_paint(self):
        bar = _bar()
        bar.resize(400, 24)
        bar.update_data(5100, temp=72, usage_pct=95, stretch_pct=1.2, core_watts=18.5, eff_max_mhz=5300)
        bar.set_active(True)
        bar.grab()

    def test_history_bounded_and_paints(self):
        from corecycler.gui.monitor_tab import MAX_FREQ_HISTORY

        bar = _bar()
        bar.resize(400, 24)
        for i in range(MAX_FREQ_HISTORY + 20):
            bar.update_data(4000 + i)
        assert len(bar._history) == MAX_FREQ_HISTORY
        bar.grab()


def _painted(bar, width=400):
    bar.resize(width, 24)
    image = bar.grab().toImage()
    return lambda x, y: image.pixelColor(x, y).name()


def _named(colour):
    from PySide6.QtGui import QColor

    return QColor(colour).name()


@pytest.fixture
def desktop_colors():
    from PySide6.QtGui import QPalette

    from corecycler.gui import style

    style.use_scheme(style.LIGHT, _qapp().palette())
    yield
    style.use_scheme(style.LIGHT, QPalette())


@pytest.mark.usefixtures("desktop_colors")
class TestCoreFreqBarPaint:
    def test_a_bar_is_plain_with_a_hairline_border_until_its_core_is_under_test(self):
        from corecycler.gui.style import theme

        plain = _painted(_bar())
        background, border = _named(theme.BG_PANEL_DARK), _named(theme.BORDER_DARKER)
        assert plain(1, 12) == background
        assert plain(0, 12) not in (background, border)
        active = _bar()
        active.set_active(True)
        tinted = _painted(active)
        assert tinted(0, 12) == _named(theme.COLOR_ACTIVE)
        assert tinted(1, 12) == _named(theme.BG_ACTIVE_TINT)

    def test_a_full_scale_clock_fills_the_bar_after_the_label_and_stops_before_the_text_columns(self):
        from corecycler.gui.style import theme

        bar = _bar()
        bar.update_data(6000)
        painted = _painted(bar)
        filled, background = _named(theme.COLOR_PASS), _named(theme.BG_PANEL_DARK)
        assert [painted(x, 4) for x in (39, 40, 130, 200)] == [background, filled, filled, background]

    def test_a_known_ceiling_draws_one_dashed_mark_and_an_unknown_one_none(self):
        plain, ceiled = _bar(), _bar()
        plain.update_data(1200)
        ceiled.update_data(1200, eff_max_mhz=4800)
        unmarked, marked = _painted(plain), _painted(ceiled)
        assert [unmarked(40, y) for y in range(3, 21)] == [unmarked(45, y) for y in range(3, 21)]
        moved = {x for x in range(400) for y in (3, 4) if marked(x, y) != unmarked(x, y)}
        assert moved
        assert max(moved) - min(moved) <= 1
        dashes = [marked(min(moved), y) != unmarked(min(moved), y) for y in range(3, 21)]
        assert any(dashes) and not all(dashes)

    def test_a_bar_given_a_clock_but_no_ceiling_shows_the_clock_alone(self):
        bar = _bar()
        bar.update_data(1200)
        assert bar._columns()[1][0] == "1200MHz"


class TestMonitorTab:
    def test_view_toggle_switches_panels(self):
        tab = _tab()
        tab._toggle_view(True)
        assert tab._per_core_scroll.isVisible() or not tab._per_core_scroll.isHidden()
        assert tab._charts_widget.isHidden()
        assert tab._toggle_btn.text() == "Package View"
        tab._toggle_view(False)
        assert tab._charts_widget.isVisible() or not tab._charts_widget.isHidden()

    def test_set_active_core_highlights_only_that_cell(self):
        tab = _tab()
        tab.set_active_core(3)
        assert tab._per_core_bars[3]._under_test is True
        assert tab._per_core_bars[0]._under_test is False
        tab.set_active_core(None)
        assert tab._per_core_bars[3]._under_test is False

    def test_set_topology_rebuilds_bars(self):
        tab = _tab(_topo(ccds=2))
        tab.set_topology(_topo(ccds=1))
        assert sorted(tab._per_core_bars) == list(range(8))

    @pytest.mark.req("T22.monitor.fallbacks")
    def test_a_machine_with_no_power_source_or_voltage_sensor_says_so_before_the_first_tick(self):
        tab = _tab()
        assert (tab._power_label.text(), tab._vcore_label.text()) == ("Package: unreadable", "Vcore: no sensor")

    def test_power_limits_na_when_pmtable_absent(self):
        tab = _tab()
        tab._show_power_limits(None)
        assert "N/A" in tab._ppt_label.text()

    def test_power_limits_formatted_when_present(self):
        tab = _tab()
        tab._show_power_limits(
            SimpleNamespace(
                ppt_value_w=120.0,
                ppt_limit_w=200.0,
                tdc_value_a=90.0,
                tdc_limit_a=180.0,
                edc_value_a=110.0,
                edc_limit_a=230.0,
            )
        )
        assert "120" in tab._ppt_label.text() and "200" in tab._ppt_label.text()

    def test_a_live_value_the_reader_never_located_is_shown_as_not_read(self):
        tab = _tab()
        tab._show_power_limits(
            SimpleNamespace(
                ppt_value_w=120.0,
                ppt_limit_w=200.0,
                tdc_value_a=90.0,
                tdc_limit_a=180.0,
                edc_value_a=None,
                edc_limit_a=230.0,
            )
        )
        assert tab._edc_label.text() == "EDC: unread/230A"


MONITOR_CONTROLS = {"Per-Core View": "hides the package charts, shows the per-core bars and reads Package View"}


@pytest.mark.req("T23.contract.monitor")
def test_every_control_of_the_monitor_tab_is_listed_with_its_reaction():
    from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit

    tab = _tab()
    assert {button.text() for button in tab.findChildren(QAbstractButton)} == set(MONITOR_CONTROLS)
    assert [*tab.findChildren(QAbstractSpinBox), *tab.findChildren(QComboBox), *tab.findChildren(QLineEdit)] == []


@pytest.mark.req("T23.contract.monitor")
@pytest.mark.parametrize("connected", [True, False])
def test_the_view_toggle_switches_the_view_only_through_its_connection(connected):
    tab = _tab()
    if not connected:
        tab._toggle_btn.toggled.disconnect()
    tab._toggle_btn.click()
    assert (tab._charts_widget.isHidden(), tab._toggle_btn.text() == "Package View") == (connected, connected)
