"""MainWindow slot logic in isolation.

Constructing a real MainWindow would open/write the user's real history DB, so
its slots are exercised bound to a SimpleNamespace mock -- the pattern already
used in test_ui_consistency / test_memory_monitor. Focus: the fail-closed
results parser and the mutual-exclusion handlers.
"""

from __future__ import annotations

import json
import sys as _sys
import time
import types
from types import MethodType
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.gui.main_window import MainWindow


def _mw(**over):
    ns = types.SimpleNamespace()
    ns._results_tab = MagicMock()
    ns._config_tab = MagicMock()
    ns._config_tab.get_profile.return_value = MagicMock(cycle_count=2)
    ns._test_start_time = time.monotonic()
    ns._start_btn = MagicMock()
    ns._stop_btn = MagicMock()
    ns._tuner_tab = MagicMock()
    ns._tuner_tab.is_running = False
    ns._memory_tab = MagicMock()
    ns._smu_tab = MagicMock()
    ns._monitor_tab = MagicMock()
    ns._core_grid = MagicMock()
    ns._core_grid._cells = {0: None, 1: None}
    ns._elapsed_timer = MagicMock()
    ns._core_status_cache = {0: MagicMock(state="passed")}
    ns._cached_cycle = 3
    ns._active_test_core = 1
    ns._worker = None
    ns._status_msg = MagicMock()
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _call(name, ns, *args):
    return MethodType(getattr(MainWindow, name), ns)(*args)


class TestOnTestCompleted:
    def test_all_passed_reports_no_failed_cores(self):
        ns = _mw()
        _call("_on_test_completed", ns, json.dumps({"0": [{"passed": True}], "1": [{"passed": True}]}))
        ns._config_tab.set_failed_cores.assert_called_with([])

    def test_failed_core_is_reported(self):
        ns = _mw()
        _call("_on_test_completed", ns, json.dumps({"0": [{"passed": True}], "3": [{"passed": False}]}))
        ns._config_tab.set_failed_cores.assert_called_with([3])

    def test_malformed_json_is_ignored(self):
        ns = _mw()
        _call("_on_test_completed", ns, "not json at all")
        ns._results_tab.update_summary.assert_not_called()

    def test_non_dict_json_is_ignored(self):
        ns = _mw()
        _call("_on_test_completed", ns, "[1, 2, 3]")
        ns._results_tab.update_summary.assert_not_called()

    def test_hostile_entries_do_not_crash(self):
        ns = _mw()
        _call("_on_test_completed", ns, json.dumps({"0": "notalist", "1": [], "2": [{"passed": False}]}))
        ns._config_tab.set_failed_cores.assert_called_with([2])


class TestMutualExclusion:
    def test_tuner_running_locks_manual_and_memory(self):
        ns = _mw()
        _call("_on_tuner_running_changed", ns, True)
        ns._start_btn.setEnabled.assert_called_with(False)
        ns._smu_tab.set_tuner_running.assert_called_with(True)
        ns._memory_tab.set_test_running.assert_called_with(True)

    def test_tuner_stopped_releases_manual(self):
        ns = _mw()
        _call("_on_tuner_running_changed", ns, False)
        ns._start_btn.setEnabled.assert_called_with(True)
        ns._smu_tab.set_tuner_running.assert_called_with(False)

    def test_memory_stress_started_locks_starters(self):
        ns = _mw()
        _call("_on_memory_stress_started", ns)
        ns._start_btn.setEnabled.assert_called_with(False)
        ns._tuner_tab.set_test_running.assert_called_with(True)


class TestCleanup:
    def test_cleanup_resets_ui_and_clears_worker(self):
        ns = _mw(_worker=MagicMock())
        _call("_cleanup_worker", ns)
        ns._start_btn.setEnabled.assert_called_with(True)
        ns._stop_btn.setEnabled.assert_called_with(False)
        ns._tuner_tab.set_test_running.assert_called_with(False)
        ns._memory_tab.set_test_running.assert_called_with(False)
        ns._elapsed_timer.stop.assert_called_once()
        assert ns._worker is None
        assert ns._active_test_core is None
