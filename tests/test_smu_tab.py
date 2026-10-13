"""SMUTab CO write UI: profile clamping, tuner lock, live updates, round-trip.

The reported crash lived here; these exercise the write-path guards and the
clamp/adjust warnings that keep a hostile profile from silently writing.
"""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo():
    topo = CPUTopology(model_name="Test 8C", family=26, model=0x44, physical_cores=8, ccds=2)
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0 if cid < 4 else 1, ccx=None, logical_cpus=(cid, cid + 8))
    return topo


def _tab():
    _qapp()
    from corecycler.gui.smu_tab import SMUTab

    return SMUTab(_topo())


class TestProfileLoad:
    def test_valid_profile_populates_spinboxes(self):
        tab = _tab()
        tab.set_co_profile({0: -20, 1: -30, 7: -10})
        assert tab._spinboxes[0].value() == -20
        assert tab._spinboxes[7].value() == -10

    def test_out_of_range_offset_is_clamped_and_warns(self):
        tab = _tab()
        with patch("corecycler.gui.smu_tab.QMessageBox.warning") as warn:
            tab.set_co_profile({0: -200})
        assert tab._spinboxes[0].value() == tab._commands.co_range[0]
        assert warn.called

    def test_offset_for_absent_core_warns(self):
        tab = _tab()
        with patch("corecycler.gui.smu_tab.QMessageBox.warning") as warn:
            tab.set_co_profile({0: -10, 999: -10})
        assert warn.called
        assert tab._spinboxes[0].value() == -10


class TestTunerLock:
    def test_tuner_running_disables_writes(self):
        tab = _tab()
        tab._smu = MagicMock()
        tab._smu.is_available.return_value = True
        tab.set_tuner_running(True)
        assert tab._tuner_active is True
        assert not tab._set_changed_btn.isEnabled()
        assert not tab._reset_btn.isEnabled()
        for spin in tab._spinboxes.values():
            assert not spin.isEnabled()

    def test_tuner_stopped_reenables_writes(self):
        tab = _tab()
        tab._smu = MagicMock()
        tab._smu.is_available.return_value = True
        tab._smu.has_backup.return_value = False
        tab._smu.get_co_offset.return_value = -7
        tab._smu.commands.get_curve_shaper_cmd = None
        tab.set_tuner_running(True)
        assert tab._table.item(0, 2).text() == "--"
        tab.set_tuner_running(False)
        assert tab._tuner_active is False
        assert tab._set_changed_btn.isEnabled()
        assert not tab._restore_btn.isEnabled()
        assert tab._table.item(0, 2).text() == "-7"
        for spin in tab._spinboxes.values():
            assert spin.isEnabled()


class TestLiveUpdate:
    @pytest.mark.req("T22.F1")
    @pytest.mark.parametrize(("read", "shown"), [(-37, "-37"), (None, "unreadable")])
    def test_a_tuner_update_shows_the_co_read_from_the_cpu_never_the_tuner_s_value(self, read, shown):
        tab = _tab()
        tab._smu = MagicMock()
        tab._smu.get_co_offset.return_value = read
        tab._smu.core_map_error = None
        tab.read_core(3)
        tab._smu.get_co_offset.assert_called_once_with(3)
        assert tab._table.item(tab._core_row_map()[3], 2).text() == shown

    def test_without_a_driver_a_tuner_update_reads_nothing(self):
        tab = _tab()
        tab._smu = None
        tab.read_core(3)
        assert tab._table.item(tab._core_row_map()[3], 2).text() == "--"

    def test_core_row_map_matches_sorted_cores(self):
        tab = _tab()
        assert tab._core_row_map() == {cid: cid for cid in range(8)}


class TestReadBack:
    def test_read_all_shows_err_on_failed_read(self):
        tab = _tab()
        tab._smu = MagicMock()
        tab._smu.get_co_offset.return_value = None
        tab._smu.commands.get_curve_shaper_cmd = None
        tab._fill_from_cpu()
        assert tab._table.item(0, 2).text() == "unreadable"


class TestCoreMapRefusal:
    def test_map_error_disables_co_controls_and_reads(self, monkeypatch):
        import corecycler.gui.smu_tab as st
        from corecycler.smu.driver import SMUResponse

        monkeypatch.setattr(st.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True))
        monkeypatch.setattr(
            st.RyzenSMU,
            "_send_command",
            lambda self, cmd, args=(0,) * 6: SMUResponse(success=False, args=(0,) * 6, raw=b""),
        )
        _qapp()
        topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=6, ccds=1)
        for cid in range(6):
            topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0, ccx=None, logical_cpus=(cid,))
        tab = st.SMUTab(topo)
        assert tab._smu is not None
        assert tab._smu.core_map_error is not None
        assert not tab._set_changed_btn.isEnabled()
        assert not tab._reset_btn.isEnabled()
        assert not tab._backup_btn.isEnabled()
        assert "Writes are off: " in tab._range_label.text()
        assert tab._gen_label.text().startswith("CPU generation Zen ")
        tab._fill_from_cpu()
        assert tab._table.item(0, 2).text() == "--"


class TestPlainWords:
    ACRONYMS = frozenset({"CCD", "CO", "CPU", "BIOS", "SMU", "JSON"})

    @pytest.mark.req("UI2")
    def test_no_visible_text_keeps_a_code_name(self):
        import re

        from PySide6.QtWidgets import QAbstractButton, QGroupBox, QLabel

        tab = _tab()
        texts = [w.text() for w in tab.findChildren(QLabel) + tab.findChildren(QAbstractButton)]
        texts += [w.title() for w in tab.findChildren(QGroupBox)]
        texts += [tab._table.horizontalHeaderItem(c).text() for c in range(tab._table.columnCount())]
        for text in texts:
            assert "_" not in text.replace("ryzen_smu", ""), text
            shouting = set(re.findall(r"\b[A-Z][A-Z0-9]+\b", text)) - self.ACRONYMS
            assert not shouting, text

    @pytest.mark.parametrize(
        ("name", "said"),
        [("ZEN5_GRANITE_RIDGE", "Zen 5 Granite Ridge"), ("ZEN3D_WARHOL", "Zen 3D Warhol"), ("UNKNOWN", "Unknown")],
    )
    def test_a_generation_is_named_as_amd_names_it(self, name, said):
        from corecycler.gui.smu_tab import generation_text
        from corecycler.smu.commands import CPUGeneration

        assert generation_text(CPUGeneration[name]) == said

    def test_a_profile_s_source_is_named_in_the_banner(self):
        tab = _tab()
        tab.set_co_profile({0: -5})
        assert (
            tab._profile_banner.text()
            == "Values from a tuning session are in Value to set. Press Set changed to write them."
        )
