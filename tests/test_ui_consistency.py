"""Tests for UI data consistency — CoreGridWidget telemetry pipeline fix."""

from __future__ import annotations

import re
import sys as _sys
import time
import types
from pathlib import Path
from types import MethodType
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from engine.topology import CPUTopology, PhysicalCore
from monitor.hwmon import HWMonData


def _make_mock_mainwindow(**overrides):
    """Build a SimpleNamespace mock of MainWindow with minimal attributes.

    Uses the headless testing pattern from test_memory_monitor.py:
    SimpleNamespace + MethodType binding to avoid QApplication dependency.
    """

    ns = types.SimpleNamespace()
    ns._worker = overrides.get("_worker", MagicMock(isRunning=MagicMock(return_value=True)))
    ns._core_status_cache = overrides.get("_core_status_cache", {})
    ns._cached_cycle = overrides.get("_cached_cycle", 0)
    ns._test_start_time = overrides.get("_test_start_time", time.monotonic())
    ns._results_tab = overrides.get("_results_tab", MagicMock())
    ns._config_tab = overrides.get("_config_tab", MagicMock())
    ns._config_tab.get_profile.return_value = MagicMock(cycle_count=1)
    ns._active_test_core = overrides.get("_active_test_core")
    ns._topology = overrides.get("_topology")
    ns._hwmon = overrides.get("_hwmon", MagicMock())
    ns._msr = overrides.get("_msr", MagicMock())
    ns._core_grid = overrides.get("_core_grid", MagicMock())
    ns._core_telemetry = overrides.get("_core_telemetry", {})
    ns._settings = overrides.get("_settings", MagicMock(record_telemetry=False))
    ns._logger = overrides.get("_logger")
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

        # Must not raise NameError
        ns._update_elapsed()

        # Verify _feed_core_grid_telemetry was called
        feed_mock.assert_called_once()


class TestOnTestCompletedFailsClosed:
    def test_malformed_results_payload_does_not_crash(self):
        """The _on_test_completed slot must fail closed on malformed/odd payloads."""
        import time

        from gui.main_window import MainWindow

        ns = types.SimpleNamespace()
        ns._test_start_time = time.monotonic()
        ns._config_tab = MagicMock()
        ns._config_tab.get_profile.return_value = MagicMock(cycle_count=1)
        ns._results_tab = MagicMock()
        ns._on_test_completed = types.MethodType(MainWindow._on_test_completed, ns)
        for bad in ("", "not json", "[1,2,3]", "null", "42",
                    '{"0": "x"}', '{"0": [42]}', '{"0": [{"no_passed": 1}]}'):
            ns._on_test_completed(bad)  # must not raise


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
        """src/gui/main_window.py must not contain 'scheduler._current_core'."""
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


# ---------------------------------------------------------------------------
# Plan 06-02: MonitorTab poll_interval, staleness indicator, narrowed exceptions
# ---------------------------------------------------------------------------

_MONITOR_TAB_SRC = (
    Path(__file__).resolve().parent.parent / "src" / "gui" / "monitor_tab.py"
)


class _MockStyleLabel:
    """Lightweight QLabel mock tracking text and stylesheet for headless tests."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._stylesheet = ""

    def setText(self, t: str) -> None:
        self._text = t

    def text(self) -> str:
        return self._text

    def setStyleSheet(self, ss: str) -> None:
        self._stylesheet = ss

    def styleSheet(self) -> str:
        return self._stylesheet


class TestMonitorTabUsesPollInterval:
    def test_monitor_tab_uses_poll_interval(self):
        """MonitorTab.__init__ timer.start() must use poll_interval, not hardcoded 1000."""
        content = _MONITOR_TAB_SRC.read_text()
        # Find MonitorTab class, then its __init__ method
        class_match = re.search(
            r"class MonitorTab\(.*?\n((?:(?!^class ).*\n)*)", content, re.MULTILINE
        )
        assert class_match is not None, "Could not find MonitorTab class"
        class_body = class_match.group(1)
        # Find __init__ within MonitorTab
        init_match = re.search(
            r"def __init__\(.*?\n(?:(?!    def ).*\n)*", class_body
        )
        assert init_match is not None, "Could not find __init__ in MonitorTab"
        init_body = init_match.group()
        assert "poll_interval" in init_body, (
            "MonitorTab.__init__ timer.start() should use poll_interval, not hardcoded 1000"
        )


class TestMonitorTabNarrowedException:
    def test_monitor_tab_narrowed_exception(self):
        """monitor_tab.py must use suppress(OSError, ...) not suppress(Exception)."""
        content = _MONITOR_TAB_SRC.read_text()
        assert "suppress(Exception)" not in content, (
            "monitor_tab.py still has overly broad suppress(Exception)"
        )
        # Should have narrowed exception handling for sysfs/procfs errors
        assert "OSError" in content, (
            "monitor_tab.py should handle OSError for sysfs/procfs failures"
        )


class TestStalenessIndicator:
    def test_staleness_indicator(self):
        """After 3 consecutive None hwmon reads, tctl label should turn grey."""
        from gui.monitor_tab import MonitorTab

        tab = types.SimpleNamespace()
        tab._hwmon = MagicMock()
        tab._power = MagicMock()
        tab._msr = MagicMock()
        tab._msr.is_available.return_value = False
        tab._cpu_usage = MagicMock()
        tab._cpu_usage.read.return_value = {}
        tab._topology = None
        tab._per_core_bars = {}
        tab._per_core_visible = False
        tab._hwmon_fail_count = 0
        tab._power_fail_count = 0

        # Mock labels
        tab._tctl_label = _MockStyleLabel("Tctl: 65.0\u00b0C")
        tab._vcore_label = _MockStyleLabel("Vcore: 1.2500V")
        tab._power_label = _MockStyleLabel("Package: 120.0W")
        tab._ccd_temp_labels = {}

        # Mock charts
        tab._freq_chart = MagicMock()
        tab._temp_chart = MagicMock()
        tab._voltage_chart = MagicMock()
        tab._power_chart = MagicMock()
        tab._max_freq_label = MagicMock()
        tab._update_power_limits = MagicMock()

        # hwmon returns None tctl for 3 consecutive reads
        tab._hwmon.read.return_value = HWMonData(tctl_c=None, tccd_temps={}, vcore_v=None)
        tab._power.read_power_watts.return_value = None

        # Bind _do_update
        tab._do_update = MethodType(MonitorTab._do_update, tab)

        # Access class constants
        tab._STALE_THRESHOLD = MonitorTab._STALE_THRESHOLD
        tab._NORMAL_STYLE = MonitorTab._NORMAL_STYLE
        tab._STALE_STYLE = MonitorTab._STALE_STYLE

        # 3 consecutive failure reads
        for _ in range(3):
            tab._do_update()

        # After 3 failures, label should be grey
        assert "color: #666" in tab._tctl_label.styleSheet(), (
            "tctl label should turn grey after 3 consecutive failures"
        )
        # Last-known text must be preserved (not cleared)
        assert "65.0" in tab._tctl_label.text(), (
            "tctl label should preserve last-known value"
        )


class TestStalenessRecovery:
    def test_staleness_recovery(self):
        """After staleness, a successful read should remove grey styling."""
        from gui.monitor_tab import MonitorTab

        tab = types.SimpleNamespace()
        tab._hwmon = MagicMock()
        tab._power = MagicMock()
        tab._msr = MagicMock()
        tab._msr.is_available.return_value = False
        tab._cpu_usage = MagicMock()
        tab._cpu_usage.read.return_value = {}
        tab._topology = None
        tab._per_core_bars = {}
        tab._per_core_visible = False
        tab._hwmon_fail_count = 0
        tab._power_fail_count = 0

        tab._tctl_label = _MockStyleLabel("Tctl: 65.0\u00b0C")
        tab._vcore_label = _MockStyleLabel("Vcore: 1.2500V")
        tab._power_label = _MockStyleLabel("Package: 120.0W")
        tab._ccd_temp_labels = {}

        tab._freq_chart = MagicMock()
        tab._temp_chart = MagicMock()
        tab._voltage_chart = MagicMock()
        tab._power_chart = MagicMock()
        tab._max_freq_label = MagicMock()
        tab._update_power_limits = MagicMock()

        tab._do_update = MethodType(MonitorTab._do_update, tab)
        tab._STALE_THRESHOLD = MonitorTab._STALE_THRESHOLD
        tab._NORMAL_STYLE = MonitorTab._NORMAL_STYLE
        tab._STALE_STYLE = MonitorTab._STALE_STYLE

        # First, trigger staleness with 3 failures
        tab._hwmon.read.return_value = HWMonData(tctl_c=None, tccd_temps={}, vcore_v=None)
        tab._power.read_power_watts.return_value = None
        for _ in range(3):
            tab._do_update()

        assert "color: #666" in tab._tctl_label.styleSheet()

        # Now a successful read
        tab._hwmon.read.return_value = HWMonData(tctl_c=72.0, tccd_temps={}, vcore_v=1.30)
        tab._do_update()

        # Grey should be removed, fail count reset
        assert "color: #666" not in tab._tctl_label.styleSheet(), (
            "tctl label should recover from grey after successful read"
        )
        assert tab._hwmon_fail_count == 0, "fail count should be reset on success"


class TestLoadToCOEnabledForHardened:
    """A session whose cores all reached HARDENED (the terminal phase with
    hardening tiers on — the default) must still offer Load to CO: hardened is
    confirmed plus extra stress. The gate only knowing CONFIRMED left every
    finished default-config session with a dead button."""

    def _detail_ns(self):
        ns = types.SimpleNamespace()
        ns._db = MagicMock()
        ns._selected_tuner_session = None
        ns._tuner_actions_row = MagicMock()
        ns._load_co_btn = MagicMock()
        ns._expand_detail = MagicMock()
        ns._detail_info = MagicMock()
        ns._core_results_table = MagicMock()
        ns._auto_size_core_results_table = MagicMock()
        ns._events_log = MagicMock()
        return ns

    def _run_detail(self, phases, best_offset=-10):
        from gui.history_tab import HistoryTab
        from tuner.state import CoreState, TunerPhase  # noqa: F401

        ns = self._detail_ns()
        sess = types.SimpleNamespace(
            id=1, config_json="{}", cpu_model="cpu", bios_version=None,
            created_at="2026-07-17T00:00:00+00:00",
        )
        states = {
            i: CoreState(core_id=i, phase=p, current_offset=-10,
                         best_offset=best_offset, baseline_offset=0)
            for i, p in enumerate(phases)
        }
        with patch("gui.history_tab.tp.load_core_states", return_value=states), \
                patch("gui.history_tab.tp.get_test_log", return_value=[]), \
                patch("gui.history_tab.tp.get_events", return_value=[]):
            MethodType(HistoryTab._show_tuner_session_detail, ns)(sess)
        return ns

    def test_all_hardened_session_enables_load(self):
        from tuner.state import TunerPhase

        ns = self._run_detail([TunerPhase.HARDENED] * 4)
        ns._load_co_btn.setEnabled.assert_called_with(True)

    def test_unfinished_session_with_values_enables_load(self):
        from tuner.state import TunerPhase

        ns = self._run_detail([TunerPhase.COARSE_SEARCH] * 4)
        ns._load_co_btn.setEnabled.assert_called_with(True)

    def test_session_without_values_disables_load(self):
        from tuner.state import TunerPhase

        ns = self._run_detail([TunerPhase.NOT_STARTED] * 4, best_offset=None)
        ns._load_co_btn.setEnabled.assert_called_with(False)


class TestNoStrayDisplayConstants:
    """The display standard lives in gui/style.py alone; a hex literal in any
    other GUI file is a stray that can drift from the palette."""

    def test_no_hex_colors_outside_style(self):
        gui = Path(__file__).parent.parent / "src" / "gui"
        offenders = []
        for f in sorted(gui.rglob("*.py")):
            if f.name == "style.py":
                continue
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", line):
                    offenders.append(f"{f.name}:{i}: {line.strip()[:60]}")
        assert not offenders, "hex colors outside gui/style.py:\n" + "\n".join(offenders)


class TestEngineInitiatedStops:
    """The engine pauses, aborts and quarantines ITSELF (thermal, apparatus,
    SMU faults, breaker) — the buttons must follow status_changed, or every
    self-stop strands the tab with Start greyed out and the config locked."""

    def _ns(self):
        ns = types.SimpleNamespace()
        for name in ("_start_btn", "_pause_btn", "_resume_btn", "_abort_btn",
                     "_config_container", "_status_label", "_progress_label",
                     "_tuner_timer"):
            setattr(ns, name, MagicMock())
        ns.tuner_running_changed = MagicMock()
        ns._active_test_core = 3
        ns._engine = None
        ns._validate_btn = MagicMock()
        ns._export_btn = MagicMock()
        return ns

    def test_engine_self_abort_reenables_ui(self):
        from gui.tuner_tab import TunerTab

        ns = self._ns()
        ns._set_running_state = MethodType(TunerTab._set_running_state, ns)
        MethodType(TunerTab._on_status_changed, ns)("idle")
        ns._start_btn.setEnabled.assert_called_with(True)
        ns._config_container.setEnabled.assert_called_with(True)
        ns._tuner_timer.stop.assert_called()
        assert ns._active_test_core is None
        ns.tuner_running_changed.emit.assert_called_with(False)

    def test_engine_quarantine_reenables_ui(self):
        from gui.tuner_tab import TunerTab

        ns = self._ns()
        ns._set_running_state = MethodType(TunerTab._set_running_state, ns)
        MethodType(TunerTab._on_status_changed, ns)("quarantined")
        ns._start_btn.setEnabled.assert_called_with(True)
        ns.tuner_running_changed.emit.assert_called_with(False)

    def test_engine_self_pause_enables_resume(self):
        from gui.tuner_tab import TunerTab

        ns = self._ns()
        MethodType(TunerTab._on_status_changed, ns)("paused")
        ns._resume_btn.setEnabled.assert_called_with(True)
        ns._pause_btn.setEnabled.assert_called_with(False)
