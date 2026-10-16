"""smu_tab CO action coverage: read/apply/reset/backup/restore branch matrix."""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from PySide6.QtWidgets import QLabel

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(cores: int = 4) -> CPUTopology:
    topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=cores, ccds=1)
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
    return topo


@pytest.fixture
def tab(monkeypatch):
    import corecycler.gui.smu_tab as st

    monkeypatch.setattr(st, "QMessageBox", MagicMock())
    _qapp()
    t = st.SMUTab(topology=_topo())
    t._tuner_active = False
    monkeypatch.setattr(t, "_confirm_write", lambda _what, _detail: True)
    return t


def _smu(**kw):
    smu = MagicMock()
    smu.get_co_offset.return_value = kw.get("get", -10)
    smu.set_co_offset.return_value = kw.get("set", True)
    smu.reset_all_co.return_value = kw.get("reset", True)
    smu.has_backup.return_value = kw.get("has_backup", True)
    smu.backup_co_offsets.return_value = kw.get("backup", {0: -10, 1: -5})
    smu.restore_co_offsets.return_value = kw.get("restore", (True, []))
    smu.commands.get_curve_shaper_cmd = kw.get("shaper_cmd", 0x84)
    smu.get_curve_shaper.return_value = kw.get("shaper", ((0, 0, 0), (-30, -27, -12), (0, 0, 0), (0, 0, 0), (0, 0, 0)))
    return smu


class TestReadAllCo:
    def test_no_smu_is_a_noop(self, tab):
        tab._smu = None
        tab._fill_from_cpu()
        assert {tab._table.item(row, 2).text() for row in range(tab._table.rowCount())} == {"--"}

    @pytest.mark.req("S1.read")
    def test_fill_from_cpu_puts_the_cpu_values_into_the_values_to_set(self, tab):
        tab._smu = _smu(get=-20, shaper=LOW_ONLY)
        tab._fill_btn.click()
        assert tab._spinboxes[0].value() == -20
        assert _shown_shaper(tab) == LOW_ONLY

    @pytest.mark.req("S1.read")
    def test_the_shown_tab_reads_the_cpu_and_keeps_the_values_being_edited(self, tab):
        tab._smu = _smu(get=-20, shaper=LOW_ONLY)
        tab._spinboxes[0].setValue(-5)
        tab._shaper_spins[4][2].setValue(-2)
        tab.show()
        assert tab._table.item(0, 2).text() == "-20"
        assert "Low -30/-27/-12" in tab._shaper_label.text()
        assert tab._spinboxes[0].value() == -5
        assert tab._shaper_spins[4][2].value() == -2

    @pytest.mark.req("S1.read")
    def test_the_shown_tab_rereads_on_its_timer_and_stops_when_hidden(self, tab):
        tab._smu = _smu(get=-20)
        tab.show()
        assert tab._reader.isActive()
        tab._smu.get_co_offset.return_value = -21
        tab._reader.timeout.emit()
        assert tab._table.item(0, 2).text() == "-21"
        tab.hide()
        assert not tab._reader.isActive()

    @pytest.mark.req("S0.view")
    @pytest.mark.parametrize(
        ("shaper_cmd", "shaper", "shown"),
        [
            (
                0x84,
                ((0, 0, 0), (-30, -27, -12), (0, 0, 0), (0, 0, 0), (0, 0, 0)),
                "Min 0/0/0, Low -30/-27/-12, Medium 0/0/0",
            ),
            (0x84, None, "unreadable: the SMU refused the read or its band check failed"),
            (None, None, "none on this generation"),
        ],
    )
    def test_read_from_cpu_shows_the_curve_shaper_now_on_the_cpu(self, tab, shaper_cmd, shaper, shown):
        assert tab._shaper_label.text().startswith("Not read yet")
        tab._smu = _smu(shaper_cmd=shaper_cmd, shaper=shaper)
        tab._fill_from_cpu()
        assert shown in tab._shaper_label.text()

    def test_retries_then_succeeds(self, tab):
        smu = _smu()
        smu.get_co_offset.side_effect = [None, -7] + [-7] * 32
        tab._smu = smu
        tab._fill_from_cpu()
        assert tab._spinboxes[0].value() == -7

    def test_persistent_failure_marks_err(self, tab):
        smu = _smu()
        smu.get_co_offset.return_value = None
        tab._smu = smu
        tab._fill_from_cpu()
        assert tab._table.item(0, 2).text() == "unreadable"


def _recorded_confirmation(tab, monkeypatch) -> list[str]:
    asked = []
    monkeypatch.setattr(tab, "_confirm_write", lambda _what, detail: asked.append(detail) or True)
    return asked


class TestSetChanged:
    def test_without_driver_warns(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = None
        tab._apply_changed()
        assert st.QMessageBox.warning.call_args.args[1] == "No driver"

    def test_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._apply_changed()
        tab._smu.set_co_offset.assert_not_called()

    def test_declined_confirmation_does_not_write(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_write", lambda _w, _d: False)
        tab._apply_changed()
        tab._smu.set_co_offset.assert_not_called()

    @pytest.mark.req("S1.setchanged")
    def test_only_the_cores_whose_value_differs_are_written_and_named(self, tab, monkeypatch):
        tab._smu = _smu(get=-10)
        tab._fill_from_cpu()
        tab._spinboxes[1].setValue(-12)
        asked = _recorded_confirmation(tab, monkeypatch)
        tab._apply_changed()
        tab._smu.set_co_offset.assert_called_once_with(1, -12)
        assert asked == ["These cores change: C1 -10 to -12"]

    @pytest.mark.req("S1.setchanged")
    def test_an_unreadable_core_counts_as_changed(self, tab, monkeypatch):
        smu = _smu()
        smu.get_co_offset.side_effect = lambda core: None if core == 2 else -10
        tab._smu = smu
        tab._fill_from_cpu()
        asked = _recorded_confirmation(tab, monkeypatch)
        tab._apply_changed()
        smu.set_co_offset.assert_called_once_with(2, 0)
        assert asked == ["These cores change: C2 unreadable to 0"]

    @pytest.mark.req("S1.setchanged")
    def test_nothing_changed_writes_nothing_and_says_so(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = _smu(get=-10)
        tab._fill_from_cpu()
        tab._apply_changed()
        tab._smu.set_co_offset.assert_not_called()
        assert st.QMessageBox.information.call_args.args[1] == "Nothing to set"

    @pytest.mark.req("S1.read")
    def test_a_written_core_shows_the_value_read_back(self, tab):
        smu = _smu(get=-10)
        tab._smu = smu
        tab._fill_from_cpu()
        tab._spinboxes[0].setValue(-15)

        def lands_one_off(core, value):
            smu.get_co_offset.side_effect = lambda c: value + 1 if c == core else -10
            return True

        smu.set_co_offset.side_effect = lands_one_off
        tab._apply_changed()
        assert tab._table.item(0, 2).text() == "-14"

    def test_a_written_profile_hides_its_banner_and_leaves_the_controls_usable(self, tab):
        tab._smu = _smu(set=True)
        tab._refresh_writes()
        tab.set_co_profile({0: -5})
        tab._apply_changed()
        assert tab._profile_banner.isHidden()
        assert tab._set_changed_btn.isEnabled()

    def test_a_failed_write_is_named_and_the_buttons_come_back(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = _smu(set=False)
        tab._refresh_writes()
        tab._apply_changed()
        assert st.QMessageBox.warning.call_args.args[1] == "Write failed"
        assert tab._set_changed_btn.isEnabled()
        assert tab._reset_btn.isEnabled()


class TestProfileBanner:
    @pytest.mark.req("T22.curve.banner")
    def test_the_loaded_values_banner_goes_once_the_cpu_values_replace_them(self, tab):
        tab._smu = _smu(get=-5)
        tab.set_co_profile({0: -20}, "session 7")
        assert not tab._profile_banner.isHidden()
        tab._fill_from_cpu()
        assert (tab._spinboxes[0].value(), tab._profile_banner.isHidden()) == (-5, True)

    def test_a_fill_that_reads_nothing_keeps_the_loaded_values_and_their_banner(self, tab):
        tab._smu = _smu(get=None)
        tab.set_co_profile({0: -20}, "session 7")
        tab._fill_from_cpu()
        assert (tab._spinboxes[0].value(), tab._profile_banner.isHidden()) == (-20, False)


class TestReset:
    def test_reset_without_driver(self, tab, monkeypatch):
        asked = []
        monkeypatch.setattr(tab, "_confirm_write", lambda what, detail: asked.append(what))
        tab._smu = None
        tab._reset_all_co()
        assert asked == []

    def test_reset_blocked_while_tuner_active(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._reset_all_co()
        tab._smu.reset_all_co.assert_not_called()

    def test_reset_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_write", lambda _w, _d: False)
        tab._reset_all_co()
        tab._smu.reset_all_co.assert_not_called()

    def test_reset_uses_bulk_command(self, tab):
        tab._smu = _smu(reset=True)
        tab._reset_all_co()
        tab._smu.reset_all_co.assert_called_once()
        tab._smu.set_co_offset.assert_not_called()

    def test_reset_falls_back_to_per_core_writes(self, tab):
        tab._smu = _smu(reset=False)
        tab._reset_all_co()
        assert [call.args for call in tab._smu.set_co_offset.call_args_list] == [(core, 0) for core in tab._spinboxes]

    @pytest.mark.req("T23.curve.zero")
    def test_a_core_the_fallback_cannot_set_to_0_is_named_in_a_warning(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = _smu(reset=False)
        tab._smu.set_co_offset.side_effect = lambda core_id, value: core_id != 2
        tab._reset_all_co()
        assert st.QMessageBox.warning.call_args.args[1:] == ("Write failed", "These cores could not be set to 0: [2].")

    def test_a_fallback_that_sets_every_core_to_0_warns_nothing(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = _smu(reset=False)
        tab._reset_all_co()
        assert not st.QMessageBox.warning.called


class TestBackupRestore:
    def test_backup_without_driver(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = None
        tab._backup_co()
        assert not st.QMessageBox.information.called
        assert not tab._restore_btn.isEnabled()

    def test_backup_enables_restore(self, tab):
        tab._smu = _smu()
        tab._backup_co()
        assert tab._restore_btn.isEnabled()

    def test_restore_without_backup_warns(self, tab):
        tab._smu = _smu(has_backup=False)
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_not_called()

    def test_restore_declined(self, tab, monkeypatch):
        tab._smu = _smu()
        monkeypatch.setattr(tab, "_confirm_write", lambda _w, _d: False)
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_not_called()

    @pytest.mark.req("T23.curve.remember")
    def test_remember_and_put_back_name_the_cores_they_cover_and_the_unread_ones(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = _smu(backup={0: -10, 1: -5, 3: -7}, restore=(True, []))
        tab._backup_co()
        assert st.QMessageBox.information.call_args.args[1:] == (
            "Values remembered",
            "The values of cores [0, 1, 3] are kept until the app closes. Cores [2] could not be read and are not "
            "kept. Put remembered values back writes them again.",
        )
        tab._restore_co()
        assert st.QMessageBox.information.call_args.args[1:] == (
            "Values put back",
            "Cores [0, 1, 3] have their remembered values again.",
        )

    def test_restore_success_rereads(self, tab):
        tab._smu = _smu(restore=(True, []))
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_called_once()

    def test_restore_partial_failure_still_rereads(self, tab):
        tab._smu = _smu(restore=(False, [1]))
        tab._restore_co()
        tab._smu.get_co_offset.assert_called()


class TestOneWriter:
    def test_every_write_waits_while_a_driver_holds_the_lock(self, tab):
        import corecycler.gui.smu_tab as st
        from corecycler.smu.writer_lock import co_writer

        tab._smu = _smu()
        tab._refresh_writes()
        with co_writer(record=True):
            tab._apply_changed()
            tab._reset_all_co()
            tab._restore_co()
        tab._smu.set_co_offset.assert_not_called()
        tab._smu.reset_all_co.assert_not_called()
        tab._smu.restore_co_offsets.assert_not_called()
        assert st.QMessageBox.warning.call_count == 3
        assert "another CO writer holds" in st.QMessageBox.warning.call_args.args[2]
        assert tab._set_changed_btn.isEnabled()

    def test_restore_is_refused_while_the_tuner_runs(self, tab):
        tab._smu = _smu()
        tab._tuner_active = True
        tab._restore_co()
        tab._smu.restore_co_offsets.assert_not_called()

    def test_the_controls_stay_locked_through_a_table_rebuild_a_backup_and_a_write(self, tab, monkeypatch):
        tab._smu = _smu()
        tab.set_tuner_running(True)
        tab._backup_co()
        assert not tab._restore_btn.isEnabled()
        tab._populate_table()
        assert not any(spin.isEnabled() for spin in tab._spinboxes.values())
        tab.set_tuner_running(False)
        monkeypatch.setattr(tab, "_set_changed", lambda _changed: tab.set_tuner_running(True))
        tab._spinboxes[0].setValue(-11)
        tab._apply_changed()
        assert not tab._set_changed_btn.isEnabled()
        assert not tab._reset_btn.isEnabled()


LOW_ONLY = ((0, 0, 0), (-30, -27, -12), (0, 0, 0), (0, 0, 0), (0, 0, 0))


def _shown_shaper(tab) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(spin.value() for spin in spins) for spins in tab._shaper_spins)


class TestCurveShaper:
    @pytest.mark.req("S1.gui")
    def test_the_table_shows_the_shaper_read_from_the_cpu(self, tab):
        tab._smu = _smu(shaper=LOW_ONLY)
        tab._fill_from_cpu()
        assert _shown_shaper(tab) == LOW_ONLY
        labels = [label.text() for label in tab._shaper_group.findChildren(QLabel)]
        assert {"-5 C", "50 C", "90 C", "Min", "Low", "Medium", "High", "Max"} <= set(labels)

    @pytest.mark.req("S1.gui")
    def test_write_shaper_writes_the_table_and_reads_it_back(self, tab):
        smu = _smu(shaper=LOW_ONLY)
        smu.set_curve_shaper.return_value = None
        tab._smu = smu
        tab._fill_from_cpu()
        tab._shaper_spins[4][2].setValue(-2)
        smu.get_curve_shaper.return_value = ((0, 0, 0), (-30, -27, -12), (0, 0, 0), (0, 0, 0), (0, 0, -2))
        tab._refresh_writes()
        tab._shaper_write_btn.click()
        smu.set_curve_shaper.assert_called_once_with(((0, 0, 0), (-30, -27, -12), (0, 0, 0), (0, 0, 0), (0, 0, -2)))
        assert tab._shaper_label.text().endswith("Max 0/0/-2")

    @pytest.mark.req("S1.gui")
    def test_shaper_to_0_writes_every_band_to_0(self, tab):
        smu = _smu(shaper=LOW_ONLY)
        smu.set_curve_shaper.return_value = None
        tab._smu = smu
        tab._refresh_writes()
        tab._shaper_zero_btn.click()
        smu.set_curve_shaper.assert_called_once_with(((0, 0, 0),) * 5)

    @pytest.mark.req("S1.gui")
    def test_a_refused_shaper_write_is_said_and_the_cpu_is_read_again(self, tab):
        import corecycler.gui.smu_tab as st

        smu = _smu(shaper=LOW_ONLY)
        smu.set_curve_shaper.return_value = "the SMU refused band Low"
        tab._smu = smu
        tab._refresh_writes()
        tab._shaper_zero_btn.click()
        assert "The Curve Shaper was not set: the SMU refused band Low." in str(st.QMessageBox.warning.call_args)
        assert _shown_shaper(tab) == LOW_ONLY

    @pytest.mark.req("S1.gui")
    def test_a_declined_confirmation_writes_nothing(self, tab, monkeypatch):
        smu = _smu(shaper=LOW_ONLY)
        tab._smu = smu
        monkeypatch.setattr(tab, "_confirm_write", lambda _w, _d: False)
        tab._refresh_writes()
        tab._shaper_zero_btn.click()
        smu.set_curve_shaper.assert_not_called()

    @pytest.mark.req("S1.gui")
    def test_the_shaper_is_locked_while_the_tuner_runs(self, tab):
        smu = _smu(shaper=LOW_ONLY)
        tab._smu = smu
        tab.set_tuner_running(True)
        assert not tab._shaper_write_btn.isEnabled() and not tab._shaper_zero_btn.isEnabled()
        assert not tab._shaper_spins[0][0].isEnabled()
        tab._zero_shaper()
        smu.set_curve_shaper.assert_not_called()

    @pytest.mark.req("S1.gui")
    def test_without_a_driver_the_shaper_write_says_so(self, tab):
        import corecycler.gui.smu_tab as st

        tab._smu = None
        tab._zero_shaper()
        assert "The ryzen_smu driver is not available." in str(st.QMessageBox.warning.call_args)


@pytest.mark.req("S1.gui")
def test_the_shaper_keeps_its_natural_height_and_the_co_table_takes_the_rest(tab):
    from PySide6.QtWidgets import QApplication

    tab.resize(960, 1400)
    tab.show()
    QApplication.processEvents()
    assert tab._shaper_group.height() == tab._shaper_group.sizeHint().height()
    assert tab._table.height() > tab._shaper_group.height()


@pytest.mark.req("S1.gui")
def test_the_shaper_read_line_wraps_instead_of_widening_the_tab(tab):
    tab._smu = _smu(shaper=LOW_ONLY)
    tab._fill_from_cpu()
    said = tab._shaper_label.text()
    assert said.startswith("Min 0/0/0, Low -30/-27/-12")
    assert tab._shaper_label.minimumSizeHint().width() < tab._shaper_label.fontMetrics().horizontalAdvance(said)


@pytest.mark.req("S1.gui")
def test_the_shaper_read_line_and_buttons_sit_beside_the_grid_not_under_it(tab):
    from PySide6.QtWidgets import QApplication

    tab.resize(960, 1400)
    tab.show()
    QApplication.processEvents()

    def box(widget):
        return widget.geometry().translated(widget.parentWidget().mapTo(tab, widget.parentWidget().rect().topLeft()))

    grid_right = box(tab._shaper_spins[0][2]).right()
    grid_bottom = box(tab._shaper_spins[-1][2]).bottom()
    for widget in (tab._shaper_label, tab._shaper_write_btn, tab._shaper_zero_btn):
        assert box(widget).left() > grid_right
        assert box(widget).bottom() <= grid_bottom


@pytest.mark.req("S1.gui")
def test_each_shaper_box_sits_under_its_temperature_and_beside_its_band_and_the_value_columns_get_the_width(tab):
    from PySide6.QtWidgets import QApplication

    tab.resize(960, 1400)
    tab.show()
    QApplication.processEvents()
    labels = {label.text(): label for label in tab._shaper_group.findChildren(QLabel)}

    def centre(widget):
        return widget.mapTo(tab, widget.rect().center())

    for row, band in enumerate(("Min", "Low", "Medium", "High", "Max")):
        for column, point in enumerate(("-5 C", "50 C", "90 C")):
            spin = tab._shaper_spins[row][column]
            assert abs(centre(spin).x() - centre(labels[point]).x()) <= 2
            assert abs(centre(spin).y() - centre(labels[band]).y()) <= 2
            assert centre(labels[band]).x() < centre(spin).x()
            assert centre(labels[point]).y() < centre(tab._shaper_spins[0][column]).y()
    right = tab._shaper_spins[0][2].mapTo(tab, tab._shaper_spins[0][2].rect().topRight()).x()
    assert right < tab.width() / 2
    widths = [tab._table.columnWidth(column) for column in range(tab._table.columnCount())]
    assert min(widths[2], widths[3]) > 2 * max(widths[0], widths[1])


CURVE_CONTROLS = {
    "Fill from CPU": "reads every core's value and the Shaper into the table and Value to set",
    "Set changed": "after a yes, writes each core whose Value to set differs from the CPU, then reads them back",
    "Set all to 0": "after a yes, sets every core to 0, one write per core when the bulk reset is refused",
    "Remember current values": "keeps the readable cores' values and names them",
    "Put remembered values back": "after a yes, writes the remembered values back and names the cores",
    "Save values to file": "saves Value to set to the chosen file",
    "Load values from file": "puts the chosen file's values into Value to set",
    "Write Shaper": "after a yes, writes the Shaper grid and reads it back",
    "Shaper to 0": "after a yes, writes every Shaper band as 0/0/0 and reads it back",
    "QTableCornerButton": "Qt's, hidden with the row header, so nothing reaches it",
}
CURVE_INPUTS = {
    "_spinboxes": "Value to set, one per core: what Set changed writes and Save values to file keeps",
    "_shaper_spins": "the Shaper grid, five bands by three temperatures: what Write Shaper writes",
}


def _saved_to(monkeypatch, tmp_path):
    import corecycler.gui.smu_tab as st
    from corecycler.config import settings as settings_mod

    dialog = MagicMock()
    dialog.getSaveFileName.return_value = (str(tmp_path / "co.json"), "")
    dialog.getOpenFileName.return_value = (str(tmp_path / "co.json"), "")
    monkeypatch.setattr(st, "QFileDialog", dialog)
    saved = []
    monkeypatch.setattr(settings_mod, "save_co_profile", lambda offsets, path, **kw: saved.append(offsets))
    monkeypatch.setattr(settings_mod, "load_co_profile", lambda path: {0: -7})
    return saved


CURVE_REACTIONS = {
    "Fill from CPU": (lambda t, saved: None, lambda t, saved: t._smu.get_co_offset.called),
    "Set changed": (
        lambda t, saved: t._spinboxes[0].setValue(-20),
        lambda t, saved: (0, -20) in [c.args for c in t._smu.set_co_offset.call_args_list],
    ),
    "Set all to 0": (lambda t, saved: None, lambda t, saved: t._smu.reset_all_co.called),
    "Remember current values": (lambda t, saved: None, lambda t, saved: t._smu.backup_co_offsets.called),
    "Put remembered values back": (lambda t, saved: None, lambda t, saved: t._smu.restore_co_offsets.called),
    "Save values to file": (lambda t, saved: None, lambda t, saved: saved != []),
    "Load values from file": (lambda t, saved: None, lambda t, saved: t._spinboxes[0].value() == -7),
    "Write Shaper": (lambda t, saved: None, lambda t, saved: t._smu.set_curve_shaper.called),
    "Shaper to 0": (
        lambda t, saved: None,
        lambda t, saved: ((0, 0, 0),) * 5 in [c.args[0] for c in t._smu.set_curve_shaper.call_args_list],
    ),
}


@pytest.mark.req("T23.contract.curve")
def test_every_control_of_the_curve_tuner_tab_is_listed_with_its_reaction(tab):
    from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox

    buttons = {b.text() or b.metaObject().className() for b in tab.findChildren(QAbstractButton)}
    assert buttons == set(CURVE_CONTROLS)
    spins = {*tab.findChildren(QAbstractSpinBox)}
    assert spins == {*tab._spinboxes.values(), *(spin for row in tab._shaper_spins for spin in row)}
    assert tab.findChildren(QComboBox) == []
    assert set(CURVE_REACTIONS) == set(CURVE_CONTROLS) - {"QTableCornerButton"}


@pytest.mark.req("T23.contract.curve")
@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("name", sorted(CURVE_REACTIONS))
def test_a_curve_tuner_button_reacts_only_through_its_connection(tab, monkeypatch, tmp_path, name, connected):
    from PySide6.QtWidgets import QPushButton

    tab._smu = _smu()
    tab._smu.set_curve_shaper.return_value = None
    saved = _saved_to(monkeypatch, tmp_path)
    prepare, reacted = CURVE_REACTIONS[name]
    prepare(tab, saved)
    button = next(b for b in tab.findChildren(QPushButton) if b.text() == name)
    button.setEnabled(True)
    if not connected:
        button.clicked.disconnect()
    button.click()
    assert bool(reacted(tab, saved)) is connected
