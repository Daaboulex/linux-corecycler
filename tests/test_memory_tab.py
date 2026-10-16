"""MemoryTab: tool detection, stress-run guards, external-test lock."""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)


def _tab():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from corecycler.gui.memory_tab import MemoryTab

    return MemoryTab(False)


class TestToolDetection:
    def test_no_tools_reports_none_installed(self):
        with patch("corecycler.config.tools.shutil.which", return_value=None):
            tab = _tab()
            assert tab._detect_available_tools() == ["(none installed)"]

    def test_both_tools_detected(self):
        with patch("corecycler.config.tools.shutil.which", lambda name: "/usr/bin/" + name):
            tab = _tab()
            tools = tab._detect_available_tools()
        assert "stressapptest" in tools
        assert "stress-ng --vm" in tools

    @pytest.mark.req("T23.memory.tools")
    def test_refresh_offers_a_tool_found_since_the_tab_opened(self):
        with patch("corecycler.config.tools.shutil.which", return_value=None):
            tab = _tab()
        assert [tab._stress_tool.itemText(i) for i in range(tab._stress_tool.count())] == ["(none installed)"]
        with patch("corecycler.config.tools.shutil.which", lambda name: "/usr/bin/" + name):
            tab._load_dimm_info()
        offered = [tab._stress_tool.itemText(i) for i in range(tab._stress_tool.count())]
        assert offered == ["stressapptest", "stress-ng --vm"]

    @pytest.mark.req("T23.memory.tools")
    @pytest.mark.parametrize("chosen", ["stressapptest", "stress-ng --vm"])
    def test_refresh_keeps_the_chosen_tool_and_falls_back_to_the_first_when_it_is_gone(self, chosen):
        with patch("corecycler.config.tools.shutil.which", lambda name: "/usr/bin/" + name):
            tab = _tab()
            tab._stress_tool.setCurrentText(chosen)
            tab._load_dimm_info()
        assert tab._stress_tool.currentText() == chosen
        with patch(
            "corecycler.config.tools.shutil.which", lambda name: None if name == "stress-ng" else "/usr/bin/" + name
        ):
            tab._load_dimm_info()
        assert tab._stress_tool.currentText() == "stressapptest"


class TestStressGuards:
    def test_external_test_running_disables_run(self):
        tab = _tab()
        tab.set_test_running(True)
        assert not tab._stress_btn.isEnabled()
        tab.set_test_running(False)
        assert tab._stress_btn.isEnabled()

    def test_run_with_no_tool_warns_and_starts_nothing(self):
        tab = _tab()
        tab._stress_tool.clear()
        tab._stress_tool.addItem("(none installed)")
        with patch("corecycler.gui.memory_tab.QMessageBox.warning") as warn:
            tab._run_memory_stress()
        assert warn.called
        assert tab._stress_worker is None

    def test_run_is_reentrancy_guarded(self):
        tab = _tab()
        tab._stress_worker = MagicMock()
        tab._stress_worker.isRunning.return_value = True
        before = tab._stress_worker
        tab._run_memory_stress()
        assert tab._stress_worker is before

    def test_on_stress_done_reenables_and_signals(self, qtbot=None):
        tab = _tab()
        seen = []
        tab.memory_stress_done.connect(lambda: seen.append("done"))
        with patch("corecycler.gui.memory_tab.QMessageBox.information") as shown:
            tab._on_stress_done("PASS", "Status: PASS")
        assert seen == ["done"]
        assert tab._stress_status.text() == "Result: PASS"
        assert shown.call_args.args[1] == "Memory Stress: PASS"

    def test_a_stopped_run_says_stopped_without_a_result_box(self):
        tab = _tab()
        with patch("corecycler.gui.memory_tab.QMessageBox.information") as shown:
            tab._on_stress_done("Stopped", "")
        assert tab._stress_status.text() == "Result: Stopped"
        shown.assert_not_called()
        assert tab._stress_btn.isEnabled()
        assert not tab._stop_btn.isEnabled()
