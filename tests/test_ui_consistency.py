"""Tests for UI data consistency — CoreGridWidget telemetry pipeline fix."""

from __future__ import annotations

import time
import types
from unittest.mock import MagicMock, patch

import pytest

from engine.topology import PhysicalCore, CPUTopology


def _make_mock_mainwindow(**overrides):
    """Build a SimpleNamespace mock of MainWindow with minimal attributes.

    Uses the headless testing pattern from test_memory_monitor.py:
    SimpleNamespace + MethodType binding to avoid QApplication dependency.
    """
    from gui.main_window import MainWindow

    ns = types.SimpleNamespace()
    ns._worker = overrides.get("_worker", MagicMock(isRunning=MagicMock(return_value=True)))
    ns._core_status_cache = overrides.get("_core_status_cache", {})
    ns._cached_cycle = overrides.get("_cached_cycle", 0)
    ns._test_start_time = overrides.get("_test_start_time", time.monotonic())
    ns._results_tab = overrides.get("_results_tab", MagicMock())
    ns._config_tab = overrides.get("_config_tab", MagicMock())
    ns._config_tab.get_profile.return_value = MagicMock(cycle_count=1)
    ns._active_test_core = overrides.get("_active_test_core", None)
    ns._topology = overrides.get("_topology", None)
    ns._hwmon = overrides.get("_hwmon", MagicMock())
    ns._msr = overrides.get("_msr", MagicMock())
    ns._core_grid = overrides.get("_core_grid", MagicMock())
    ns._core_telemetry = overrides.get("_core_telemetry", {})
    ns._settings = overrides.get("_settings", MagicMock(record_telemetry=False))
    ns._logger = overrides.get("_logger", None)
    ns._status_msg = overrides.get("_status_msg", MagicMock())
    ns._monitor_tab = overrides.get("_monitor_tab", MagicMock())
    return ns


class TestUpdateElapsedNoNameError:
    def test_update_elapsed_no_name_error(self):
        """Calling _update_elapsed with _worker.isRunning()=True must NOT raise NameError."""
        from gui.main_window import MainWindow

        ns = _make_mock_mainwindow()
        # Bind _update_elapsed
        ns._update_elapsed = types.MethodType(MainWindow._update_elapsed, ns)
        # Bind a stub _feed_core_grid_telemetry to verify it's called
        feed_mock = MagicMock()
        ns._feed_core_grid_telemetry = feed_mock

        # Must not raise NameError (the original bug: `scheduler` undefined)
        ns._update_elapsed()

        # Verify _feed_core_grid_telemetry was called
        feed_mock.assert_called_once()


class TestCoreGridTelemetryFed:
    @patch("gui.main_window.read_core_frequencies", return_value={0: 5500.0})
    def test_core_grid_telemetry_fed(self, mock_freqs):
        """_feed_core_grid_telemetry with _active_test_core=0 calls update_core_telemetry."""
        from gui.main_window import MainWindow
        from monitor.hwmon import HWMonData

        topo = CPUTopology()
        topo.cores = {0: PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))}

        hwmon_mock = MagicMock()
        hwmon_mock.read.return_value = HWMonData(tctl_c=65.0, tccd_temps={1: 62.0}, vcore_v=1.25)

        msr_mock = MagicMock()
        msr_mock.is_available.return_value = True
        stretch_reading = MagicMock()
        stretch_reading.stretch_pct = 2.5
        msr_mock.read_clock_stretch.return_value = {0: stretch_reading}
        power_reading = MagicMock()
        power_reading.watts = 15.0
        msr_mock.read_core_power.return_value = {0: power_reading}

        core_grid_mock = MagicMock()

        ns = _make_mock_mainwindow(
            _active_test_core=0,
            _topology=topo,
            _hwmon=hwmon_mock,
            _msr=msr_mock,
            _core_grid=core_grid_mock,
        )

        ns._feed_core_grid_telemetry = types.MethodType(
            MainWindow._feed_core_grid_telemetry, ns,
        )
        ns._feed_core_grid_telemetry()

        # Must have been called with core_id=0 and freq > 0
        core_grid_mock.update_core_telemetry.assert_called_once()
        call_args = core_grid_mock.update_core_telemetry.call_args
        assert call_args[0][0] == 0  # core_id
        assert call_args[0][1] > 0  # freq_mhz


class TestActiveTestCoreSetBySignal:
    def test_active_test_core_set_by_signal(self):
        """Calling _on_core_started(5, 0) sets _active_test_core = 5."""
        from gui.main_window import MainWindow

        ns = _make_mock_mainwindow()
        ns._on_core_started = types.MethodType(MainWindow._on_core_started, ns)

        ns._on_core_started(5, 0)

        assert ns._active_test_core == 5


class TestNoCrossThreadSchedulerAccess:
    def test_no_cross_thread_scheduler_access(self):
        """src/gui/main_window.py must not contain 'scheduler._current_core'.

        This is the codebase audit test pattern from Phase 2 (test_no_bare_setsid_in_src).
        """
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "src" / "gui" / "main_window.py"
        content = src.read_text()
        assert "scheduler._current_core" not in content


class TestFeedTelemetryNoopWhenNoActiveCore:
    def test_feed_telemetry_noop_when_no_active_core(self):
        """_feed_core_grid_telemetry with _active_test_core=None must not call update_core_telemetry."""
        from gui.main_window import MainWindow

        core_grid_mock = MagicMock()
        ns = _make_mock_mainwindow(
            _active_test_core=None,
            _core_grid=core_grid_mock,
        )

        ns._feed_core_grid_telemetry = types.MethodType(
            MainWindow._feed_core_grid_telemetry, ns,
        )
        ns._feed_core_grid_telemetry()

        core_grid_mock.update_core_telemetry.assert_not_called()
