"""TunerTab lifecycle: the button-state matrix across engine status changes.

Every engine self-pause must leave Resume enabled (never a dead end), and every
terminal status must fully release the manual-test/config UI.
"""

from __future__ import annotations

import sys as _sys
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)


def _tab():
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from corecycler.gui.tuner_tab import TunerTab

    return TunerTab(db=None, topology=None, smu=None)


class TestRunningState:
    def test_running_sets_full_button_matrix(self):
        tab = _tab()
        tab._set_running_state(True)
        assert not tab._start_btn.isEnabled()
        assert tab._pause_btn.isEnabled()
        assert tab._abort_btn.isEnabled()
        assert not tab._resume_btn.isEnabled()
        assert not tab._validate_btn.isEnabled()
        assert not tab._export_btn.isEnabled()
        assert not tab._config_container.isEnabled()

    def test_running_emits_signal(self):
        tab = _tab()
        seen = []
        tab.tuner_running_changed.connect(lambda r: seen.append(r))
        tab._set_running_state(True)
        assert seen == [True]

    def test_stopped_reenables_start_and_config(self):
        tab = _tab()
        tab._set_running_state(True)
        tab._set_running_state(False)
        assert tab._start_btn.isEnabled()
        assert tab._config_container.isEnabled()
        assert not tab._pause_btn.isEnabled()


class TestStatusTransitions:
    def test_paused_enables_resume_only(self):
        tab = _tab()
        tab._set_running_state(True)
        tab._on_status_changed("paused")
        assert tab._resume_btn.isEnabled()
        assert not tab._pause_btn.isEnabled()
        assert tab._abort_btn.isEnabled()

    def test_running_disables_resume(self):
        tab = _tab()
        tab._on_status_changed("running")
        assert tab._pause_btn.isEnabled()
        assert not tab._resume_btn.isEnabled()
        assert tab._abort_btn.isEnabled()

    def test_validating_sets_label(self):
        tab = _tab()
        tab._on_status_changed("validating")
        assert "Validating" in tab._status_label.text()

    def test_idle_releases_ui(self):
        tab = _tab()
        tab._set_running_state(True)
        tab._on_status_changed("idle")
        assert tab._start_btn.isEnabled()

    def test_quarantined_releases_and_notifies(self):
        tab = _tab()
        tab._notify = MagicMock()
        tab._set_running_state(True)
        tab._on_status_changed("quarantined")
        assert tab._start_btn.isEnabled()
        assert tab._notify.called


class TestIsRunning:
    @pytest.mark.parametrize(
        "status,expected",
        [
            ("running", True), ("validating", True), ("hunting", True),
            ("paused", False), ("idle", False), ("quarantined", False),
        ],
    )
    def test_is_running_tracks_active_statuses(self, status, expected):
        tab = _tab()
        tab._engine = MagicMock()
        tab._engine.status = status
        assert tab.is_running is expected

    def test_is_running_false_without_engine(self):
        tab = _tab()
        tab._engine = None
        assert tab.is_running is False


class TestSetTestRunning:
    def test_manual_test_running_disables_start(self):
        tab = _tab()
        tab.set_test_running(True)
        assert not tab._start_btn.isEnabled()

    def test_manual_test_stopped_reenables_when_idle(self):
        tab = _tab()
        tab._engine = None
        tab.set_test_running(True)
        tab.set_test_running(False)
        assert tab._start_btn.isEnabled()

    def test_manual_test_stopped_keeps_start_disabled_if_paused_session(self):
        tab = _tab()
        tab._engine = MagicMock()
        tab._engine.status = "paused"
        tab.set_test_running(False)
        assert not tab._start_btn.isEnabled()
