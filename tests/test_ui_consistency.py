"""Tests for UI data consistency — CoreGridWidget telemetry pipeline fix."""

from __future__ import annotations

import re
import sys as _sys
import time
import types
from pathlib import Path
from types import MethodType
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.topology import CPUTopology, PhysicalCore
from tests._sensors import FakeSensors, a_tick


def _make_mock_mainwindow(**overrides):
    """Build a SimpleNamespace mock of MainWindow with minimal attributes.

    Uses the headless testing pattern from test_memory_monitor.py:
    SimpleNamespace + MethodType binding to avoid QApplication dependency.
    """

    ns = types.SimpleNamespace()
    ns._worker = overrides.get("_worker", MagicMock(isRunning=MagicMock(return_value=True)))
    ns._core_status_cache = overrides.get("_core_status_cache", {})
    ns._run_cores = overrides.get("_run_cores", 0)
    ns._run_rounds = overrides.get("_run_rounds", 0)
    ns._run_round = overrides.get("_run_round", 0)
    ns._rounds_done = overrides.get("_rounds_done", 0)
    ns._test_start_time = overrides.get("_test_start_time", time.monotonic())
    ns._results_tab = overrides.get("_results_tab", MagicMock())
    ns._config_tab = overrides.get("_config_tab", MagicMock())
    ns._config_tab.get_profile.return_value = MagicMock(cycle_count=1)
    ns._active_test_core = overrides.get("_active_test_core")
    ns._topology = overrides.get("_topology")
    ns._sensors = overrides.get("_sensors", FakeSensors())
    ns._telemetry_failing = False
    ns._core_grid = overrides.get("_core_grid", MagicMock())
    ns._core_telemetry = overrides.get("_core_telemetry", {})
    ns._settings = overrides.get("_settings", MagicMock(record_telemetry=False))
    ns._logger = overrides.get("_logger")
    ns._status_msg = overrides.get("_status_msg", MagicMock())
    ns._monitor_tab = overrides.get("_monitor_tab", MagicMock())
    ns._smu_tab = types.SimpleNamespace(smu=None)
    return ns


class TestUpdateElapsedNoNameError:
    def test_update_elapsed_no_name_error(self):
        """Calling _update_elapsed with _worker.isRunning()=True must NOT raise NameError."""
        from corecycler.gui.main_window import MainWindow

        ns = _make_mock_mainwindow()
        ns._update_elapsed = types.MethodType(MainWindow._update_elapsed, ns)
        ns._update_elapsed()
        ns._results_tab.update_summary.assert_called_once()


class TestOnTestCompletedFailsClosed:
    def test_malformed_results_payload_does_not_crash(self):
        """The _on_test_completed slot must fail closed on malformed/odd payloads."""
        import time

        from corecycler.gui.main_window import MainWindow

        ns = types.SimpleNamespace()
        ns._test_start_time = time.monotonic()
        ns._config_tab = MagicMock()
        ns._run_rounds = 1
        ns._rounds_done = 0
        ns._results_tab = MagicMock()
        ns._on_test_completed = types.MethodType(MainWindow._on_test_completed, ns)
        for bad in ("", "not json", "[1,2,3]", "null", "42", '{"0": "x"}', '{"0": [42]}', '{"0": [{"no_passed": 1}]}'):
            ns._on_test_completed(bad)


class TestCoreGridTelemetryFed:
    def test_core_grid_telemetry_fed(self):
        """_feed_core_grid_telemetry with _active_test_core=0 calls update_core_telemetry."""
        from corecycler.gui.main_window import MainWindow

        topo = CPUTopology()
        topo.cores = {0: PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))}

        core_grid_mock = MagicMock()
        sensors = FakeSensors(tctl=65.0, tccd=((0, 62.0),), vcore=1.25)
        ns = _make_mock_mainwindow(
            _active_test_core=0,
            _topology=topo,
            _sensors=sensors,
            _core_grid=core_grid_mock,
        )
        ns._feed_core_grid_telemetry = types.MethodType(MainWindow._feed_core_grid_telemetry, ns)
        ns._feed_core_grid_telemetry(a_tick(sensors, clocks={0: 5500.0}, stretch={0: 2.5}, core_watts={0: 15.0}))

        core_grid_mock.update_core_telemetry.assert_called_once_with(0, 5500.0, 62.0, 1.25, stretch_pct=2.5)
        assert (ns._core_telemetry[0]["max_stretch_pct"], ns._core_telemetry[0]["core_watts"]) == (2.5, 15.0)


class TestActiveTestCoreSetBySignal:
    def test_active_test_core_set_by_signal(self):
        """Calling _on_core_started(5, 0) sets _active_test_core = 5."""
        from corecycler.gui.main_window import MainWindow

        ns = _make_mock_mainwindow()
        ns._on_core_started = types.MethodType(MainWindow._on_core_started, ns)

        ns._on_core_started(5, 0)

        assert ns._active_test_core == 5


class TestNoCrossThreadSchedulerAccess:
    def test_no_cross_thread_scheduler_access(self):
        """src/gui/main_window.py must not contain 'scheduler._current_core'."""
        from pathlib import Path

        src = Path(__file__).resolve().parent.parent / "src" / "corecycler" / "gui" / "main_window.py"
        content = src.read_text()
        assert "scheduler._current_core" not in content


class TestFeedTelemetryNoopWhenNoActiveCore:
    def test_feed_telemetry_noop_when_no_active_core(self):
        """_feed_core_grid_telemetry with _active_test_core=None must not call update_core_telemetry."""
        from corecycler.gui.main_window import MainWindow

        core_grid_mock = MagicMock()
        ns = _make_mock_mainwindow(
            _active_test_core=None,
            _core_grid=core_grid_mock,
        )

        ns._feed_core_grid_telemetry = types.MethodType(MainWindow._feed_core_grid_telemetry, ns)
        ns._feed_core_grid_telemetry(a_tick())

        core_grid_mock.update_core_telemetry.assert_not_called()


_MONITOR_TAB_SRC = Path(__file__).resolve().parent.parent / "src" / "corecycler" / "gui" / "monitor_tab.py"


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


class TestMonitorTabSwallowsNothing:
    def test_the_monitor_tab_swallows_no_error(self):
        content = _MONITOR_TAB_SRC.read_text()
        assert "suppress(" not in content
        assert "except" not in content


class TestStalenessIndicator:
    def test_staleness_indicator(self):
        """The first missed read marks the last reading stale."""
        from corecycler.gui.monitor_tab import MonitorTab

        tab = types.SimpleNamespace()
        tab._sensors = FakeSensors()
        tab._topology = None
        tab._per_core_bars = {}
        tab._per_core_visible = False
        tab._last_read = {}

        tab._tctl_label = _MockStyleLabel("Tctl: 65.0\u00b0C")
        tab._vcore_label = _MockStyleLabel("Vcore: 1.2500V")
        tab._power_label = _MockStyleLabel("Package: 120.0W")
        tab._ccd_temp_labels = {}

        tab._freq_chart = MagicMock()
        tab._temp_chart = MagicMock()
        tab._voltage_chart = MagicMock()
        tab._power_chart = MagicMock()
        tab._max_freq_label = MagicMock()
        tab._show_power_limits = MagicMock()

        tab.on_tick = MethodType(MonitorTab.on_tick, tab)

        tab._NORMAL_STYLE = MonitorTab._NORMAL_STYLE
        tab._show_read = MethodType(MonitorTab._show_read, tab)
        tab._stale_style = MonitorTab._stale_style

        tab._sensors.give(tctl=65.0, vcore=1.25)
        tab.on_tick(a_tick(tab._sensors))
        tab._sensors.give()
        tab.on_tick(a_tick(tab._sensors))

        assert tab._tctl_label.styleSheet() == MonitorTab._stale_style(), (
            "tctl label should turn stale at the first missed read"
        )
        assert tab._tctl_label.text() == "Tctl: 65.0\u00b0C (stale)"
        assert tab._vcore_label.text() == "Vcore: 1.2500V (stale)"


class TestStalenessRecovery:
    def test_staleness_recovery(self):
        """After staleness, a successful read should remove grey styling."""
        from corecycler.gui.monitor_tab import MonitorTab

        tab = types.SimpleNamespace()
        tab._sensors = FakeSensors()
        tab._topology = None
        tab._per_core_bars = {}
        tab._per_core_visible = False
        tab._last_read = {}

        tab._tctl_label = _MockStyleLabel("Tctl: 65.0\u00b0C")
        tab._vcore_label = _MockStyleLabel("Vcore: 1.2500V")
        tab._power_label = _MockStyleLabel("Package: 120.0W")
        tab._ccd_temp_labels = {}

        tab._freq_chart = MagicMock()
        tab._temp_chart = MagicMock()
        tab._voltage_chart = MagicMock()
        tab._power_chart = MagicMock()
        tab._max_freq_label = MagicMock()
        tab._show_power_limits = MagicMock()

        tab.on_tick = MethodType(MonitorTab.on_tick, tab)
        tab._NORMAL_STYLE = MonitorTab._NORMAL_STYLE
        tab._show_read = MethodType(MonitorTab._show_read, tab)
        tab._stale_style = MonitorTab._stale_style

        tab._sensors.give(tctl=65.0, vcore=1.25)
        tab.on_tick(a_tick(tab._sensors))
        tab._sensors.give()
        tab.on_tick(a_tick(tab._sensors))

        assert tab._tctl_label.styleSheet() == MonitorTab._stale_style()

        tab._sensors.give(tctl=72.0, vcore=1.30)
        tab.on_tick(a_tick(tab._sensors))

        assert tab._tctl_label.styleSheet() != MonitorTab._stale_style(), (
            "tctl label should recover from grey after successful read"
        )
        assert tab._tctl_label.text() == "Tctl: 72.0\u00b0C"


class TestNoStrayDisplayConstants:
    """The display standard lives in gui/style.py alone; a hex literal in any
    other GUI file is a stray that can drift from the palette."""

    def test_no_hex_colors_outside_style(self):
        gui = Path(__file__).parent.parent / "src" / "corecycler" / "gui"
        offenders = []
        for f in sorted(gui.rglob("*.py")):
            if f.name == "style.py":
                continue
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b", line):
                    offenders.append(f"{f.name}:{i}: {line.strip()[:60]}")
        assert not offenders, "hex colors outside gui/style.py:\n" + "\n".join(offenders)


class TestColorsAreReadLiveNotFrozen:
    """A color imported by value freezes at import time, so a desktop that changes
    its color scheme is never followed. ``style.theme`` is the only way to read one."""

    def test_no_gui_file_imports_a_color_by_value(self):
        from PySide6.QtGui import QPalette

        from corecycler.gui import style

        colors = set(style.resolve(style.DARK, QPalette())) - {"scheme"}
        gui = Path(__file__).parent.parent / "src" / "corecycler" / "gui"
        offenders = []
        for f in sorted(gui.rglob("*.py")):
            if f.name == "style.py":
                continue
            text = f.read_text()
            for block in re.findall(
                r"from corecycler\.gui\.style import \(([^)]*)\)|"
                r"from corecycler\.gui\.style import ([^\n(]+)",
                text,
            ):
                imported = {n.strip() for n in (block[0] or block[1]).replace("\n", "").split(",")}
                for name in sorted(imported & colors):
                    offenders.append(f"{f.name}: {name}")
        assert not offenders, "colors imported by value instead of via theme:\n" + "\n".join(offenders)

    def test_the_gate_knows_what_a_color_is(self):
        from PySide6.QtGui import QPalette

        from corecycler.gui import style

        colors = set(style.resolve(style.DARK, QPalette()))
        assert {"COLOR_PASS", "STATE_COLORS", "BG_PANEL_DARK"} <= colors
        assert "duration_str" not in colors
