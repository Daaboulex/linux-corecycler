"""The theme follows the desktop, and every color it picks stays readable there.

Issue #14: the app forced its colors through a stylesheet while the palette stayed
the desktop's, so text landed on backgrounds it could not be read against. These
assert the relationship (readable contrast, neutrals taken from the desktop), never
the color values themselves.
"""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette

from corecycler.gui import style

TEXT_MIN = 4.5
DIM_MIN = 3.0
GRAPHIC_MIN = 3.0

VERDICT_STATES = ("testing", "passed", "failed", "warned", "backoff", "mem_stress")
QUIET_STATES = ("pending", "queued", "skipped")
VERDICT_STATUSES = ("completed", "crashed", "quarantined", "stopped", "aborted", "running", "validating")
QUIET_STATUSES = ("paused", "idle")


def _relative_luminance(color: str) -> float:
    c = QColor(color)
    channels = []
    for raw in (c.redF(), c.greenF(), c.blueF()):
        channels.append(raw / 12.92 if raw <= 0.04045 else ((raw + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _palette(
    window: str, base: str, alternate: str, text: str, mid: str, highlight: str, on_highlight: str
) -> QPalette:
    p = QPalette()
    role = QPalette.ColorRole
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        p.setColor(group, role.Window, QColor(window))
        p.setColor(group, role.Base, QColor(base))
        p.setColor(group, role.AlternateBase, QColor(alternate))
        p.setColor(group, role.WindowText, QColor(text))
        p.setColor(group, role.Text, QColor(text))
        p.setColor(group, role.Mid, QColor(mid))
        p.setColor(group, role.Highlight, QColor(highlight))
        p.setColor(group, role.HighlightedText, QColor(on_highlight))
    return p


BREEZE_DARK = _palette("#202326", "#141618", "#1d1f22", "#fcfcfc", "#45494d", "#3daee9", "#fcfcfc")
BREEZE_LIGHT = _palette("#eff0f1", "#ffffff", "#f7f7f7", "#232629", "#c4c8cc", "#3daee9", "#ffffff")
SCHEMES = [(style.DARK, BREEZE_DARK), (style.LIGHT, BREEZE_LIGHT)]


class TestContrastHelper:
    def test_black_on_white_is_the_maximum(self):
        assert contrast("#000000", "#ffffff") == pytest.approx(21.0, abs=0.1)

    def test_a_color_against_itself_is_the_minimum(self):
        assert contrast("#4caf50", "#4caf50") == pytest.approx(1.0, abs=0.01)


class TestEveryStateCellIsReadable:
    def test_every_grid_state_is_classified(self):
        assert set(VERDICT_STATES) | set(QUIET_STATES) == set(style.GRID_STATE_LABELS)

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_verdict_cells_carry_their_text(self, scheme, palette):
        cells = style.resolve(scheme, palette)["STATE_COLORS"]
        failures = {
            state: round(contrast(cells[state][1], cells[state][0]), 2)
            for state in VERDICT_STATES
            if contrast(cells[state][1], cells[state][0]) < TEXT_MIN
        }
        assert not failures, f"{scheme}: verdict cells below {TEXT_MIN}:1 -- {failures}"

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_quiet_cells_stay_legible(self, scheme, palette):
        cells = style.resolve(scheme, palette)["STATE_COLORS"]
        failures = {
            state: round(contrast(cells[state][1], cells[state][0]), 2)
            for state in QUIET_STATES
            if contrast(cells[state][1], cells[state][0]) < DIM_MIN
        }
        assert not failures, f"{scheme}: quiet cells below {DIM_MIN}:1 -- {failures}"

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_a_cell_is_never_its_own_border(self, scheme, palette):
        cells = style.resolve(scheme, palette)["STATE_COLORS"]
        for state, (bg, fg, border) in cells.items():
            assert QColor(bg).isValid() and QColor(fg).isValid() and QColor(border).isValid()
            assert border != bg, f"{scheme}/{state}: border invisible on its own cell"


class TestStatusAndPhaseTextIsReadable:
    def test_every_status_is_classified(self):
        assert set(VERDICT_STATUSES) | set(QUIET_STATUSES) == set(style.SESSION_STATUS_LABELS)

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_verdict_status_text_reads_on_window_and_base(self, scheme, palette):
        colors = style.resolve(scheme, palette)["STATUS_COLORS"]
        window = palette.color(QPalette.ColorRole.Window).name()
        base = palette.color(QPalette.ColorRole.Base).name()
        failures = {
            status: (round(contrast(colors[status], window), 2), round(contrast(colors[status], base), 2))
            for status in VERDICT_STATUSES
            if min(contrast(colors[status], window), contrast(colors[status], base)) < TEXT_MIN
        }
        assert not failures, f"{scheme}: status text below {TEXT_MIN}:1 (window, base) -- {failures}"

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_quiet_status_text_stays_legible(self, scheme, palette):
        colors = style.resolve(scheme, palette)["STATUS_COLORS"]
        window = palette.color(QPalette.ColorRole.Window).name()
        failures = {
            status: round(contrast(colors[status], window), 2)
            for status in QUIET_STATUSES
            if contrast(colors[status], window) < DIM_MIN
        }
        assert not failures, f"{scheme}: quiet status below {DIM_MIN}:1 -- {failures}"

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_every_running_phase_reads_on_window_and_base(self, scheme, palette):
        from corecycler.tuner.state import TunerPhase

        colors = style.resolve(scheme, palette)["PHASE_COLORS"]
        window = palette.color(QPalette.ColorRole.Window).name()
        base = palette.color(QPalette.ColorRole.Base).name()
        failures = {
            phase.value: (round(contrast(color, window), 2), round(contrast(color, base), 2))
            for phase, color in colors.items()
            if phase is not TunerPhase.NOT_STARTED and min(contrast(color, window), contrast(color, base)) < TEXT_MIN
        }
        assert not failures, f"{scheme}: phase text below {TEXT_MIN}:1 (window, base) -- {failures}"

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_the_not_started_phase_stays_legible(self, scheme, palette):
        from corecycler.tuner.state import TunerPhase

        colors = style.resolve(scheme, palette)["PHASE_COLORS"]
        window = palette.color(QPalette.ColorRole.Window).name()
        assert contrast(colors[TunerPhase.NOT_STARTED], window) >= DIM_MIN


class TestChartsAndNeutralsAreReadable:
    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_chart_series_stand_out_from_the_chart_ground(self, scheme, palette):
        resolved = style.resolve(scheme, palette)
        ground = resolved["BG_PANEL_DARK"]
        failures = {
            name: round(contrast(resolved[name], ground), 2)
            for name in ("CHART_FREQ", "CHART_TEMP", "CHART_POWER", "CHART_VOLT")
            if contrast(resolved[name], ground) < GRAPHIC_MIN
        }
        assert not failures, f"{scheme}: chart series below {GRAPHIC_MIN}:1 on the chart -- {failures}"

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_chart_title_reads_on_the_chart_ground(self, scheme, palette):
        resolved = style.resolve(scheme, palette)
        assert contrast(resolved["COLOR_TEXT_BRIGHT"], resolved["BG_PANEL_DARK"]) >= TEXT_MIN

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_dimmed_text_stays_legible(self, scheme, palette):
        resolved = style.resolve(scheme, palette)
        window = palette.color(QPalette.ColorRole.Window).name()
        failures = {
            name: round(contrast(resolved[name], window), 2)
            for name in ("COLOR_TEXT_DIM", "COLOR_MUTED", "COLOR_MUTED_DARK", "COLOR_MUTED_DARKER")
            if contrast(resolved[name], window) < DIM_MIN
        }
        assert not failures, f"{scheme}: dimmed text below {DIM_MIN}:1 -- {failures}"

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_a_selection_is_the_desktops_own_pair(self, scheme, palette):
        """Both halves come from the desktop, and are never mixed with ours.

        Breeze's own selection is 2.5:1; an app that overrode it to reach AA would
        stand out against every other app on that desktop. What must hold is that
        the text on a selected background is the desktop's, not CoreCycler's.
        """
        resolved = style.resolve(scheme, palette)
        assert resolved["BG_SELECTED"] == palette.color(QPalette.ColorRole.Highlight).name()
        assert resolved["COLOR_ON_SELECTED"] == palette.color(QPalette.ColorRole.HighlightedText).name()
        assert resolved["COLOR_ON_SELECTED"] != resolved["COLOR_ACTIVE"]

    @pytest.mark.parametrize(("scheme", "palette"), SCHEMES)
    def test_action_buttons_carry_their_white_label(self, scheme, palette):
        resolved = style.resolve(scheme, palette)
        for name in ("BTN_GREEN", "BTN_RED"):
            ratio = contrast("#ffffff", resolved[name])
            assert ratio >= TEXT_MIN, f"{scheme}/{name}: white label at {ratio:.2f}:1"


class TestNeutralsComeFromTheDesktop:
    def test_panels_and_borders_track_the_palette(self):
        dark = style.resolve(style.DARK, BREEZE_DARK)
        light = style.resolve(style.LIGHT, BREEZE_LIGHT)
        assert dark["BG_PANEL_DARK"] == BREEZE_DARK.color(QPalette.ColorRole.Base).name()
        assert light["BG_PANEL_DARK"] == BREEZE_LIGHT.color(QPalette.ColorRole.Base).name()
        assert dark["BG_SELECTED"] == BREEZE_DARK.color(QPalette.ColorRole.Highlight).name()

    def test_a_custom_desktop_palette_moves_the_neutrals(self):
        custom = _palette("#332b2b", "#241f1f", "#2b2525", "#f0e6e6", "#5a4d4d", "#a05050", "#ffffff")
        resolved = style.resolve(style.DARK, custom)
        default = style.resolve(style.DARK, BREEZE_DARK)
        assert resolved["COLOR_MUTED"] != default["COLOR_MUTED"]
        assert resolved["BG_PANEL"] == custom.color(QPalette.ColorRole.AlternateBase).name()

    def test_dimmed_text_sits_between_the_text_and_the_background(self):
        resolved = style.resolve(style.DARK, BREEZE_DARK)
        text = BREEZE_DARK.color(QPalette.ColorRole.WindowText).name()
        window = BREEZE_DARK.color(QPalette.ColorRole.Window).name()
        assert contrast(resolved["COLOR_MUTED"], window) < contrast(text, window)
        assert contrast(resolved["COLOR_MUTED"], window) > contrast(window, window)


class TestSchemeSelection:
    def test_the_desktop_answer_wins_when_it_gives_one(self):
        assert style.scheme_for(Qt.ColorScheme.Dark, BREEZE_LIGHT) == style.DARK
        assert style.scheme_for(Qt.ColorScheme.Light, BREEZE_DARK) == style.LIGHT

    def test_no_answer_falls_back_to_what_the_palette_shows(self):
        assert style.scheme_for(Qt.ColorScheme.Unknown, BREEZE_DARK) == style.DARK
        assert style.scheme_for(Qt.ColorScheme.Unknown, BREEZE_LIGHT) == style.LIGHT

    def test_an_unknown_scheme_name_is_refused(self):
        with pytest.raises(ValueError, match="color scheme"):
            style.resolve("sepia", BREEZE_DARK)


class TestThemeIsLiveNotFrozen:
    def test_use_scheme_replaces_every_color(self):
        try:
            style.use_scheme(style.DARK, BREEZE_DARK)
            dark_panel = style.theme.BG_PANEL_DARK
            style.use_scheme(style.LIGHT, BREEZE_LIGHT)
            assert dark_panel != style.theme.BG_PANEL_DARK
            assert style.theme.scheme == style.LIGHT
        finally:
            style.use_scheme(style.LIGHT, QPalette())

    def test_reading_through_the_theme_sees_the_change(self):
        try:
            style.use_scheme(style.DARK, BREEZE_DARK)
            before = style.theme.COLOR_PASS
            style.use_scheme(style.LIGHT, BREEZE_LIGHT)
            assert before != style.theme.COLOR_PASS
        finally:
            style.use_scheme(style.LIGHT, QPalette())

    def test_an_unset_color_is_refused_rather_than_guessed(self):
        with pytest.raises(AttributeError):
            _ = style.theme.COLOR_INVENTED


class TestFollowingTheDesktop:
    def test_takes_the_desktop_scheme_at_start_and_again_when_it_changes(self):
        from PySide6.QtWidgets import QApplication, QWidget

        app = QApplication.instance() or QApplication([])
        original = QPalette(app.palette())
        probe = QWidget()
        try:
            style.follow(app)
            assert style.theme.scheme == style.scheme_for(app.styleHints().colorScheme(), app.palette())

            app.setPalette(BREEZE_DARK)
            app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
            assert style.theme.scheme == style.DARK
            assert BREEZE_DARK.color(QPalette.ColorRole.Base).name() == style.theme.BG_PANEL_DARK

            app.setPalette(BREEZE_LIGHT)
            app.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
            assert style.theme.scheme == style.LIGHT
            assert BREEZE_LIGHT.color(QPalette.ColorRole.Base).name() == style.theme.BG_PANEL_DARK
        finally:
            app.styleHints().colorSchemeChanged.disconnect()
            app.setPalette(original)
            probe.deleteLater()
            style.use_scheme(style.LIGHT, QPalette())
