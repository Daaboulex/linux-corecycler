"""Main application window — tabs, toolbar, test control."""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from PySide6.QtCore import QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from corecycler.config.paths import ensure_work_dir, user_home
from corecycler.config.settings import load_settings, save_settings
from corecycler.engine.backends import get_backend, load_all
from corecycler.engine.backends.base import StressConfig, StressResult
from corecycler.engine.containment import lane_problem
from corecycler.engine.heavy_test_lock import HeavyTestRefused, take_heavy_test_lock
from corecycler.engine.scheduler import CoreScheduler, CoreTestStatus, SchedulerConfig
from corecycler.engine.topology import CPUTopology, detect_topology
from corecycler.gui.config_tab import ConfigTab
from corecycler.gui.history_tab import HistoryTab
from corecycler.gui.memory_tab import MemoryTab
from corecycler.gui.monitor_tab import MonitorTab
from corecycler.gui.results_tab import ResultsTab
from corecycler.gui.smu_tab import SMUTab
from corecycler.gui.style import theme
from corecycler.gui.tool_prompt import ensure_tool
from corecycler.gui.tuner_tab import TunerTab
from corecycler.gui.widgets.core_grid import CoreGridWidget
from corecycler.history.archive import archive_old_sessions
from corecycler.history.context import detect_bios_change
from corecycler.history.db import HistoryDB, adopt_legacy_root_db
from corecycler.history.logger import TestRunLogger
from corecycler.monitor.frequency import Clocks, fastest
from corecycler.monitor.hwmon import Sensors
from corecycler.monitor.msr import msr_problem
from corecycler.monitor.tick import Tick, Ticker, first_cpus
from corecycler.smu.driver import core_map_blocked, physical_ccds, smu_problem

log = logging.getLogger(__name__)


NARROWEST_STATUS = 120  # equivalent-mutant: a pixel either way keeps the warning readable and the window in the tile


def narrowable(label: QLabel) -> QLabel:
    label.setToolTip(label.text().strip())
    label.setMinimumWidth(NARROWEST_STATUS)
    return label


class TestWorker(QThread):
    """Worker thread that runs the core scheduler and lets the shared heavy-test lock go when the run ends."""

    core_started = Signal(int, int, arguments=["core_id", "cycle"])
    core_finished = Signal(int, object, arguments=["core_id", "result"])
    status_updated = Signal(int, object, arguments=["core_id", "status"])
    cycle_completed = Signal(int)
    # PySide6 crashes marshalling a dict, so the results travel as JSON.
    test_completed = Signal(str)
    thermal_throttled = Signal(float)
    stall_detected = Signal(int)
    phase_changed = Signal(int, str)
    crashed = Signal(str)

    def __init__(self, scheduler: CoreScheduler, lock: BinaryIO) -> None:
        super().__init__()
        self.scheduler = scheduler
        self._lock = lock

        self.scheduler.on_core_start = [lambda cid, cyc: self.core_started.emit(cid, cyc)]
        self.scheduler.on_core_finish = [lambda cid, res: self.core_finished.emit(cid, res)]
        self.scheduler.on_status_update = [lambda cid, st: self.status_updated.emit(cid, st)]
        self.scheduler.on_cycle_complete = [lambda cyc: self.cycle_completed.emit(cyc)]
        self.scheduler.on_test_complete = [
            lambda res: self.test_completed.emit(
                json.dumps(
                    {str(k): [{**asdict(r), "passed": r.passed} for r in v] for k, v in res.items()}, default=str
                )
            )
        ]
        self.scheduler.on_thermal_throttle = [self.thermal_throttled.emit]
        self.scheduler.on_stall_detected = [self.stall_detected.emit]
        self.scheduler.on_phase_change = [self.phase_changed.emit]

    def run(self) -> None:
        try:
            self.scheduler.run()
        except Exception as exc:
            log.exception("Test worker crashed")
            self.crashed.emit(str(exc))
        finally:
            self._lock.close()


def _read(value: float | None, spec: str, unit: str) -> str:
    return "unread" if value is None else f"{value:{spec}}{unit}"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CoreCycler")

        load_all()

        self._settings = load_settings()
        self._topology: CPUTopology | None = None
        self._worker: TestWorker | None = None
        self._closing = False
        self._test_start_time: float = 0
        self._sensors = Sensors()
        self._ticker = Ticker(self._sensors, Clocks())
        self._telemetry_failing = False
        self._core_telemetry: dict[int, dict] = {}
        self._core_status_cache: dict[int, CoreTestStatus] = {}
        self._active_test_core: int | None = None
        self._crash: str | None = None
        self._logger: TestRunLogger | None = None

        self._history_db: HistoryDB | None = None
        self._history_problem: str | None = None
        if self._settings.record_history:
            try:
                self._history_db = HistoryDB()
                try:
                    adopted = adopt_legacy_root_db(self._history_db)
                    if adopted:
                        log.info("Adopted legacy root history: %s", adopted)
                except Exception:
                    log.exception("Legacy root history adoption failed — continuing")
                try:
                    archived = archive_old_sessions(self._history_db)
                    if archived is not None:
                        log.info("Old tuner sessions archived to %s", archived)
                except Exception:
                    log.exception("Archiving the old tuner sessions failed; their rows stay in the database")
                recovered = self._history_db.recover_incomplete_runs()
                if recovered:
                    for run_id, started_at in recovered:
                        log.info("Recovered stale session id=%d started_at=%s, marked as crashed", run_id, started_at)
                if self._settings.history_retention_days > 0:
                    cutoff = datetime.now(UTC) - timedelta(days=self._settings.history_retention_days)
                    self._history_db.purge_before(cutoff.isoformat())
                self._bios_warning: str | None = None
                try:
                    self._bios_warning = detect_bios_change(self._history_db)
                    if self._bios_warning is not None:
                        log.info(self._bios_warning)
                except Exception:
                    log.exception("Failed to detect a BIOS change")
            except Exception as e:
                log.exception("Failed to initialize history database")
                self._history_db = None
                self._history_problem = f"History is off: {e}"

        self._detect_cpu()
        self._setup_ui()
        self._setup_toolbar()
        self._setup_status_bar()
        self._show_saved_profile()
        self._setup_timer()

        self.resize(self._settings.window_width, self._settings.window_height)

    def _detect_cpu(self) -> None:
        self._topology = detect_topology()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)

        cpu_label = QLabel(self._topology.model_name if self._topology else "Unknown CPU")
        cpu_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        cpu_label.setStyleSheet("padding: 4px 6px;")
        cpu_label.setMaximumWidth(200)
        cpu_label.setWordWrap(True)
        left.addWidget(cpu_label)

        if self._topology:
            info_parts = [f"{self._topology.physical_cores}C/{self._topology.logical_cpus_count}T"]
            if self._topology.ccds > 1:
                info_parts.append(f"{self._topology.ccds} CCDs")
            if self._topology.is_x3d:
                info_parts.append("X3D V-Cache")
            if self._topology.smt_enabled:
                info_parts.append("SMT")
            info_label = QLabel(" | ".join(info_parts))
            info_label.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM}; padding: 0 6px;")
            info_label.setWordWrap(True)
            left.addWidget(info_label)

        self._core_grid = CoreGridWidget(self._topology)
        self._core_grid.setMaximumWidth(200)
        left.addWidget(self._core_grid)

        main_layout.addLayout(left, stretch=0)

        self._tabs = QTabWidget()
        self._tabs.setContentsMargins(0, 0, 0, 0)
        self._tabs.setDocumentMode(True)

        self._config_tab = ConfigTab(self._topology)
        self._tabs.addTab(self._config_tab, "Configuration")

        self._results_tab = ResultsTab()
        self._tabs.addTab(self._results_tab, "Results")

        self._smu_tab = SMUTab(self._topology)
        self._monitor_tab = MonitorTab(
            self._sensors, self._ticker.power_available(), topology=self._topology, smu=self._smu_tab.smu
        )
        self._tabs.addTab(self._monitor_tab, "Monitor")
        self._tabs.addTab(self._smu_tab, "Curve-Tuner")

        smu = self._smu_tab.smu
        writable = smu is not None and smu.commands.has_co and core_map_blocked(smu) is None
        self._tuner_tab = TunerTab(
            self._history_db,
            self._topology,
            smu.commands.co_range if writable else None,
            smu.get_co_offset if writable else None,
        )
        self._tuner_tab.tuner_running_changed.connect(self._on_tuner_running_changed)
        self._tuner_tab.tuner_core_testing.connect(self._on_tuner_core_update)
        self._tuner_tab.tuner_core_elapsed.connect(self._on_tuner_core_elapsed)
        self._tuner_tab.tuner_core_info.connect(self._on_tuner_core_info)
        self._tabs.addTab(self._tuner_tab, "Auto-Tuner")

        self._history_tab = HistoryTab(self._history_db)
        if getattr(self, "_bios_warning", None) is not None:
            self._history_tab.set_bios_warning(self._bios_warning)
        self._history_tab.load_profile_requested.connect(self._on_load_co_profile)
        self._tabs.addTab(self._history_tab, "History")

        self._memory_tab = MemoryTab(self._ticker.pm.is_available())
        self._memory_tab.memory_stress_started.connect(self._on_memory_stress_started)
        self._memory_tab.memory_stress_done.connect(self._on_memory_stress_done)
        self._tabs.addTab(self._memory_tab, "Memory")

        main_layout.addWidget(self._tabs, stretch=2)

    def _setup_toolbar(self) -> None:
        toolbar = QToolBar("Test Control")
        toolbar.setMovable(False)
        toolbar.toggleViewAction().setVisible(False)
        self.addToolBar(toolbar)

        self._start_btn = QPushButton("Start test")
        self._start_btn.setFixedHeight(36)
        self._start_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BTN_GREEN}; color: white; padding: 0 16px; "
            "border-radius: 4px; font-weight: bold; font-size: 13px; } "
            f"QPushButton:hover {{ background: {theme.COLOR_PASS_DARK}; }} "
            f"QPushButton:disabled {{ background: {theme.BORDER_DIM}; color: {theme.COLOR_MUTED}; }}"
        )
        self._start_btn.clicked.connect(self._start_test)
        toolbar.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop test")
        self._stop_btn.setFixedHeight(36)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BTN_RED}; color: white; padding: 0 16px; "
            "border-radius: 4px; font-weight: bold; font-size: 13px; } "
            f"QPushButton:hover {{ background: {theme.COLOR_FAIL_DARK}; }} "
            f"QPushButton:disabled {{ background: {theme.BORDER_DIM}; color: {theme.COLOR_MUTED}; }}"
        )
        self._stop_btn.clicked.connect(self._stop_test)
        toolbar.addWidget(self._stop_btn)

        toolbar.addSeparator()

        save_action = QAction("Save Profile", self)
        save_action.triggered.connect(self._save_profile)
        toolbar.addAction(save_action)

        load_action = QAction("Load Profile", self)
        load_action.triggered.connect(self._load_profile)
        toolbar.addAction(load_action)

    def _show_saved_profile(self) -> None:
        refused = self._config_tab.set_profile(self._settings.active_profile)
        if refused:
            self._status_msg.setText("Saved profile not loaded: " + "; ".join(refused))

    def _setup_status_bar(self) -> None:
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_msg = QLabel("Ready")
        self._status_bar.addWidget(self._status_msg)

        problems = [
            f"{name} {problem}"
            for name, problem in (("MSR", msr_problem()), ("SMU", smu_problem()))
            if problem is not None
        ]
        if problems:
            priv_label = QLabel("  At start: " + "; ".join(problems))
            priv_label.setStyleSheet(f"color: {theme.COLOR_WARN_SOFT}; font: 10px monospace;")
            self._status_bar.addPermanentWidget(narrowable(priv_label))
        if self._history_problem is not None:
            history_label = QLabel(f"  {self._history_problem}")
            history_label.setStyleSheet(f"color: {theme.COLOR_FAIL}; font: 10px monospace;")
            self._status_bar.addPermanentWidget(narrowable(history_label))

    def show_style_fallback(self, problem: str | None) -> None:
        if problem is None:
            return
        label = QLabel(f"  {problem}: running in Fusion")
        label.setStyleSheet(f"color: {theme.COLOR_WARN_SOFT}; font: 10px monospace;")
        self._status_bar.addPermanentWidget(narrowable(label))

    def _setup_timer(self) -> None:
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start(int(self._settings.poll_interval * 1000))

    def _on_tick(self) -> None:
        tick = self._ticker.read(first_cpus(self._topology), self._topology.cores if self._topology else ())
        self._monitor_tab.on_tick(tick)
        self._memory_tab.on_tick(tick)
        self._tuner_tab.refresh()
        self._feed_core_grid_telemetry(tick)

    def _start_test(self) -> None:
        if not self._topology:
            QMessageBox.warning(self, "Error", "CPU topology not detected")
            return

        if (
            hasattr(self, "_memory_tab")
            and self._memory_tab._stress_worker
            and self._memory_tab._stress_worker.isRunning()
        ):
            QMessageBox.warning(
                self, "Memory Stress Active", "A memory stress test is running. Stop it before starting a core test."
            )
            return
        problem = lane_problem()
        if problem is not None:
            QMessageBox.warning(self, "Cannot Start", f"Cannot start: {problem}")
            return

        waiting = self._tuner_tab.waiting_session()
        if waiting is not None:
            reply = QMessageBox.question(
                self,
                "Active Tuner Session",
                f"Tuning session {waiting.session_id} is {waiting.status}.\n\n"
                "Manual stress tests don't modify CO offsets, but you may want to "
                "resume or abort the tuner session first.\n\n"
                "Continue with manual test anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        profile = self._config_tab.get_profile()
        if profile.cores_to_test == []:
            QMessageBox.warning(self, "No cores", "Tick at least one core to test on the Configuration tab.")
            return

        backend = self._get_backend(profile.backend)
        if not backend:
            return

        if not backend.is_available() and not ensure_tool(self, profile.backend):
            return

        stress_config = StressConfig(
            mode=profile.get_stress_mode(),
            fft_preset=profile.get_fft_preset(),
            fft_min=profile.fft_min,
            fft_max=profile.fft_max,
            threads=profile.threads,
        )

        scheduler_config = SchedulerConfig(
            seconds_per_core=profile.seconds_per_core,
            cores_to_test=profile.cores_to_test,
            stop_on_error=profile.stop_on_error,
            cycle_count=profile.cycle_count,
            max_temperature=profile.max_temperature,
            variable_load=profile.variable_load,
            idle_stability_test=profile.idle_stability_test,
            idle_between_cores=profile.idle_between_cores,
        )

        try:
            work_dir = ensure_work_dir(self._settings.work_dir)
        except OSError as e:
            QMessageBox.warning(
                self,
                "Work directory unavailable",
                f"Could not create the stress work directory: {e}",
            )
            return
        try:
            scheduler = CoreScheduler(
                topology=self._topology,
                backend=backend,
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=work_dir,
            )
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to initialize scheduler: {e}")
            return
        try:
            lock = take_heavy_test_lock("corecycler-gui")
        except HeavyTestRefused as refused:
            QMessageBox.warning(self, "Cannot Start", f"Cannot start: {refused}")
            return

        self._results_tab.init_cores(scheduler.core_status)

        self._core_status_cache.clear()
        self._run_cores = len(scheduler.core_status)
        self._run_rounds = scheduler_config.cycle_count
        self._run_round = 0
        self._rounds_done = 0
        self._active_test_core = None
        self._crash = None
        self._worker = TestWorker(scheduler, lock)
        self._worker.core_started.connect(self._on_core_started)
        self._worker.core_finished.connect(self._on_core_finished)
        self._worker.status_updated.connect(self._on_status_updated)
        self._worker.status_updated.connect(self._on_status_cached)
        self._worker.cycle_completed.connect(self._on_cycle_completed)
        self._worker.cycle_completed.connect(self._on_cycle_cached)
        self._worker.test_completed.connect(self._on_test_completed)
        self._worker.crashed.connect(self._on_worker_crashed)
        self._worker.finished.connect(self._on_worker_finished)

        self._logger = None
        if self._settings.record_history and self._history_db and self._topology:
            try:
                smu = self._smu_tab.smu if hasattr(self, "_smu_tab") else None
                self._logger = TestRunLogger(self._history_db, self._topology, profile, smu=smu)
                self._worker.core_started.connect(self._logger.on_core_started)
                self._worker.core_finished.connect(self._logger.on_core_finished)
                self._worker.status_updated.connect(self._logger.on_status_updated)
                self._worker.cycle_completed.connect(self._logger.on_cycle_completed)
                self._worker.test_completed.connect(self._logger.on_test_completed)
                self._worker.thermal_throttled.connect(self._logger.record_thermal_event)
                self._worker.stall_detected.connect(self._logger.record_stall_event)
                self._worker.phase_changed.connect(self._logger.record_phase_change)
            except Exception:
                log.exception("Failed to create history logger")
                self._logger = None

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._tuner_tab.set_test_running(True)
        self._memory_tab.set_test_running(True)
        self._test_start_time = time.monotonic()
        self._elapsed_timer.start(1000)
        self._tabs.setCurrentWidget(self._results_tab)
        self._status_msg.setText("Testing...")

        self._worker.start()

    def _stop_test(self) -> None:
        if not self._worker:
            return
        self._stop_btn.setEnabled(False)
        self._status_msg.setText("Stopping...")

        self._release_logger()
        self._disconnect_caches()
        self._core_telemetry.clear()

        self._worker.scheduler.stop()

    def _release_logger(self) -> None:
        logger, self._logger = self._logger, None
        if logger is None:
            return
        # disconnect before the stop is recorded: a queued signal would reach a released logger
        if self._worker is not None:
            for signal, slot in (
                (self._worker.core_started, logger.on_core_started),
                (self._worker.core_finished, logger.on_core_finished),
                (self._worker.status_updated, logger.on_status_updated),
                (self._worker.cycle_completed, logger.on_cycle_completed),
                (self._worker.test_completed, logger.on_test_completed),
            ):
                with contextlib.suppress(RuntimeError):
                    signal.disconnect(slot)
        for core_id, peaks in self._core_telemetry.items():
            try:
                logger.update_core_telemetry_peaks(
                    core_id,
                    peak_freq_mhz=peaks["max_freq"],
                    max_temp_c=peaks["max_temp"],
                    min_vcore_v=peaks["min_vcore"],
                    max_vcore_v=peaks["max_vcore"],
                )
            except Exception:
                log.exception("Failed to record telemetry peaks of core %d", core_id)
        try:
            logger.on_test_stopped()
        except Exception:
            log.exception("Failed to record test stop in history")

    def _disconnect_caches(self) -> None:
        for signal, slot in (
            (self._worker.status_updated, self._on_status_cached),
            (self._worker.cycle_completed, self._on_cycle_cached),
        ):
            with contextlib.suppress(RuntimeError):
                signal.disconnect(slot)

    def _get_backend(self, name: str):
        try:
            return get_backend(name)
        except KeyError:
            QMessageBox.warning(self, "Error", f"Unknown backend: {name}")
            return None

    @Slot(int, int)
    def _on_core_started(self, core_id: int, cycle: int) -> None:
        self._status_msg.setText(f"Testing core {core_id} (cycle {cycle + 1})")
        self._run_round = cycle + 1
        self._monitor_tab.set_active_core(core_id)
        self._active_test_core = core_id

    @Slot(int, object)
    def _on_status_cached(self, core_id: int, status: CoreTestStatus) -> None:
        """Cache core status from worker thread — signal/slot is thread-safe."""
        self._core_status_cache[core_id] = status

    @Slot(int)
    def _on_cycle_cached(self, cycle: int) -> None:
        """Count the rounds the scheduler completed; the summary reads it."""
        self._rounds_done = cycle + 1

    @Slot(int, object)
    def _on_core_finished(self, core_id: int, result: StressResult) -> None:
        status = self._core_status_cache.get(core_id)
        if status:
            self._core_grid.update_core_status(core_id, status)
            self._results_tab.update_core(core_id, status)

        if result and not result.passed:
            self._results_tab.add_error(core_id, result.detail)

        t = self._core_telemetry.pop(core_id, None)
        if t:
            extra_parts = []
            if t["min_vcore"] is not None and t["max_vcore"] is not None:
                extra_parts.append(f"Vcore: {t['min_vcore']:.4f}-{t['max_vcore']:.4f}V")
            if t["max_stretch_pct"] > 0.5:
                extra_parts.append(f"Stretch: {t['max_stretch_pct']:.1f}%")
            if t.get("core_watts") is not None:
                extra_parts.append(f"Power: {t['core_watts']:.1f}W")
            extra = ("  " + "  ".join(extra_parts)) if extra_parts else ""
            state = "PASS" if (result and result.passed) else "FAIL"
            peak = _read(t["max_freq"], ".0f", " MHz")
            hottest = _read(t["max_temp"], ".1f", "C")
            self._results_tab.add_log(core_id, f"[{state}] Peak: {peak}, Max temp: {hottest}{extra}")

            if self._logger:
                try:
                    self._logger.update_core_telemetry_peaks(
                        core_id,
                        peak_freq_mhz=t["max_freq"],
                        max_temp_c=t["max_temp"],
                        min_vcore_v=t["min_vcore"],
                        max_vcore_v=t["max_vcore"],
                    )
                except Exception:
                    log.exception("Failed to record telemetry peaks")

    @Slot(int, object)
    def _on_status_updated(self, core_id: int, status: CoreTestStatus) -> None:
        self._core_grid.update_core_status(core_id, status)

    @Slot(int)
    def _on_cycle_completed(self, cycle: int) -> None:
        self._status_msg.setText(f"Cycle {cycle + 1} complete")

    @Slot(str)
    def _on_test_completed(self, results_json: str) -> None:
        try:
            results = json.loads(results_json)
        except json.JSONDecodeError, TypeError:
            return
        if not isinstance(results, dict):
            return

        def _all_passed(r_list) -> bool:
            return (
                isinstance(r_list, list)
                and bool(r_list)
                and all(isinstance(r, dict) and r.get("passed") for r in r_list)
            )

        tested = {cid: r_list for cid, r_list in results.items() if isinstance(r_list, list) and r_list}
        total = len(tested)
        passed = sum(1 for r_list in tested.values() if _all_passed(r_list))
        failed = total - passed
        elapsed = time.monotonic() - self._test_start_time

        self._results_tab.update_summary(
            total=total,
            passed=passed,
            failed=failed,
            elapsed=elapsed,
            cycle=self._rounds_done,
            total_cycles=self._run_rounds,
        )

        failed_cores = []
        for cid, r_list in results.items():
            if isinstance(r_list, list) and r_list and not _all_passed(r_list):
                try:
                    failed_cores.append(int(cid))
                except ValueError, TypeError:
                    continue
        self._config_tab.set_failed_cores(failed_cores)

    def _on_worker_crashed(self, message: str) -> None:
        if self._closing:
            return
        self._crash = message
        self._status_msg.setText(f"Test worker crashed: {message}")

    def _on_worker_finished(self) -> None:
        if self._closing:
            return
        was_stopping = not self._stop_btn.isEnabled()
        self._cleanup_worker()
        if self._logger:
            try:
                self._logger.write_telemetry()
            except Exception:
                log.exception("Failed to write the run's last telemetry to history")
        self._logger = None
        self._history_tab.refresh()
        if self._crash is not None:
            self._status_msg.setText(f"Test worker crashed: {self._crash}")
        elif was_stopping:
            self._status_msg.setText("Test stopped")
        else:
            self._status_msg.setText("Test complete")

    def _cleanup_worker(self) -> None:
        """Reset UI state after worker finishes or crashes."""
        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._tuner_tab.set_test_running(False)
        self._memory_tab.set_test_running(False)
        self._monitor_tab.set_active_core(None)
        self._elapsed_timer.stop()
        self._core_status_cache.clear()
        self._active_test_core = None
        self._worker = None

    @Slot(bool)
    def _on_tuner_running_changed(self, running: bool) -> None:
        """Mutual exclusion: disable manual test Start and CO writes when tuner is active."""
        self._start_btn.setEnabled(not running)
        self._smu_tab.set_tuner_running(running)
        self._memory_tab.set_test_running(running)

    @Slot(int, str)
    def _on_tuner_core_update(self, core_id: int, state: str) -> None:
        """Update core grid when auto-tuner changes core state."""
        status = CoreTestStatus(core_id=core_id, state=state)
        self._core_grid.update_core_status(core_id, status)

    @Slot(int, float)
    def _on_tuner_core_elapsed(self, core_id: int, elapsed: float) -> None:
        """Update core grid with live elapsed time during tuner tests."""
        status = CoreTestStatus(core_id=core_id, state="testing", elapsed_seconds=elapsed)
        self._core_grid.update_core_status(core_id, status)

    @Slot(int, int, str)
    def _on_tuner_core_info(self, core_id: int, tuner_value: int, phase: str) -> None:
        self._core_grid.update_core_telemetry(core_id, tuner_value=tuner_value, tuner_phase=phase)
        self._smu_tab.read_core(core_id)

    @Slot()
    def _on_memory_stress_started(self) -> None:
        """Set all cores to memory stress state; lock the other test starters."""
        self._start_btn.setEnabled(False)
        self._tuner_tab.set_test_running(True)
        for core_id in self._core_grid._cells:
            status = CoreTestStatus(core_id=core_id, state="mem_stress")
            self._core_grid.update_core_status(core_id, status)

    @Slot()
    def _on_memory_stress_done(self) -> None:
        """Reset cores to pending; release the other test starters."""
        self._start_btn.setEnabled(
            (self._worker is None or not self._worker.isRunning()) and not self._tuner_tab.is_running
        )
        self._tuner_tab.set_test_running(False)
        for core_id in self._core_grid._cells:
            status = CoreTestStatus(core_id=core_id, state="pending")
            self._core_grid.update_core_status(core_id, status)

    @Slot(object)
    def _on_load_co_profile(self, profile) -> None:
        """Load CO profile from history into the Curve-Tuner tab."""
        self._smu_tab.set_co_profile(profile)
        self._tabs.setCurrentWidget(self._smu_tab)

    def _update_elapsed(self) -> None:
        if not self._worker:
            return
        if not self._worker.isRunning():
            self._cleanup_worker()
            self._status_msg.setText("Test stopped (worker exited unexpectedly)")
            return
        elapsed = time.monotonic() - self._test_start_time
        cache = self._core_status_cache
        passed = sum(1 for s in cache.values() if s.state == "passed")
        failed = sum(1 for s in cache.values() if s.state == "failed")

        self._results_tab.update_summary(
            total=self._run_cores,
            passed=passed,
            failed=failed,
            elapsed=elapsed,
            cycle=self._run_round,
            total_cycles=self._run_rounds,
        )

    def _feed_core_grid_telemetry(self, tick: Tick) -> None:
        current_core = self._active_test_core
        if current_core is None:
            return

        core_info = self._topology.cores.get(current_core) if self._topology else None
        if not core_info:
            return

        freq = fastest(tick.clocks, core_info.logical_cpus)
        ceiling = fastest(tick.ceilings, core_info.logical_cpus)
        stretch_pct = tick.stretch.get(current_core)
        core_read = next((read for read in tick.cores if read[0] == current_core), None)
        core_watts = tick.core_watts.get(core_info.logical_cpus[0])
        reading = tick.sensed
        temp = dict(reading.tccd).get(physical_ccds(self._smu_tab.smu, self._topology)[current_core])
        core_vcore = self._sensors.vcore(reading)

        self._core_grid.update_core_telemetry(
            current_core,
            freq,
            temp,
            core_vcore,
            stretch_pct=stretch_pct,
        )

        if self._logger and self._settings.record_telemetry:
            try:
                self._logger.record_telemetry_sample(
                    current_core, freq, temp, core_vcore, effective_max_mhz=ceiling, core_read=core_read
                )
            except sqlite3.Error as exc:
                if not self._telemetry_failing:
                    log.warning("telemetry samples are not being recorded: %s", exc)
                self._telemetry_failing = True
            else:
                if self._telemetry_failing:
                    log.info("telemetry samples are recorded again")
                self._telemetry_failing = False

        if current_core not in self._core_telemetry:
            self._core_telemetry[current_core] = {
                "max_freq": None,
                "max_stretch_pct": 0.0,
                "core_watts": None,
                "max_temp": None,
                "last_vcore": None,
                "min_vcore": None,
                "max_vcore": None,
            }
        t = self._core_telemetry[current_core]
        if freq is not None:
            t["max_freq"] = freq if t["max_freq"] is None else max(t["max_freq"], freq)
        if stretch_pct is not None and stretch_pct > t["max_stretch_pct"]:
            t["max_stretch_pct"] = stretch_pct
        if core_watts is not None:
            t["core_watts"] = core_watts
        if temp is not None:
            t["max_temp"] = temp if t["max_temp"] is None else max(t["max_temp"], temp)
        if core_vcore is not None:
            t["last_vcore"] = core_vcore
            if t["min_vcore"] is None or core_vcore < t["min_vcore"]:
                t["min_vcore"] = core_vcore
            if t["max_vcore"] is None or core_vcore > t["max_vcore"]:
                t["max_vcore"] = core_vcore

    def _save_profile(self) -> None:
        from corecycler.config.settings import save_profile

        path, _ = QFileDialog.getSaveFileName(self, "Save Profile", str(user_home()), "JSON (*.json)")
        if path:
            try:
                profile = self._config_tab.get_profile()
                save_profile(profile, Path(path))
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save profile: {e}")

    def _load_profile(self) -> None:
        from corecycler.config.settings import load_profile

        path, _ = QFileDialog.getOpenFileName(self, "Load Profile", str(user_home()), "JSON (*.json)")
        if path:
            try:
                profile = load_profile(Path(path))
                refused = self._config_tab.set_profile(profile)
                if refused:
                    QMessageBox.warning(self, "Profile not loaded", "\n".join(refused))
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to load profile: {e}")

    def closeEvent(self, event) -> None:
        self._closing = True
        manual_running = self._worker and self._worker.isRunning()
        memory_running = (
            hasattr(self, "_memory_tab")
            and self._memory_tab._stress_worker is not None
            and self._memory_tab._stress_worker.isRunning()
        )

        if manual_running or memory_running:
            reply = QMessageBox.question(
                self,
                "Test Running",
                "A test is still running. Stop and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                self._closing = False
                event.ignore()
                return

        if manual_running:
            self._release_logger()
            self._disconnect_caches()
            # the finished handler's queued delivery would land after the database closes
            with contextlib.suppress(RuntimeError, TypeError):
                self._worker.finished.disconnect(self._on_worker_finished)

            self._worker.scheduler.force_stop()
            if not self._worker.wait(5000):
                self._worker.terminate()
                self._worker.wait(2000)

        if hasattr(self, "_memory_tab"):
            self._memory_tab.force_stop()

        self._settings.window_width = self.width()
        self._settings.window_height = self.height()
        save_settings(self._settings)

        self._tick_timer.stop()
        self._ticker.close()
        if self._history_db:
            self._history_db.close()
        event.accept()
