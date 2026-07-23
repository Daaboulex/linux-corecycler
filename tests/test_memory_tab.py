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

    return MemoryTab()


class TestToolDetection:
    def test_no_tools_reports_none_installed(self):
        with patch("corecycler.gui.memory_tab.shutil.which", return_value=None):
            tab = _tab()
            assert tab._detect_available_tools() == ["(none installed)"]

    def test_both_tools_detected(self):
        with patch("corecycler.gui.memory_tab.shutil.which", lambda name: "/usr/bin/" + name):
            tab = _tab()
            tools = tab._detect_available_tools()
        assert "stressapptest" in tools
        assert "stress-ng --vm" in tools


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
        tab.memory_stress_done.connect(lambda ok: seen.append(ok))
        with patch("corecycler.gui.memory_tab.QMessageBox.information"):
            tab._on_stress_done(True, "Status: PASS")
        assert seen == [True]
        assert tab._stress_btn.isEnabled()
        assert not tab._stop_btn.isEnabled()
