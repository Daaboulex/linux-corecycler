"""Main application window — tabs, toolbar, test control."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

from config.settings import load_settings, save_settings
from engine.backends.base import StressConfig, StressResult
from engine.backends.mprime import MprimeBackend
from engine.backends.stress_ng import StressNgBackend
from engine.backends.ycruncher import YCruncherBackend
from engine.scheduler import CoreScheduler, CoreTestStatus, SchedulerConfig
from engine.topology import CPUTopology, detect_topology
from gui.config_tab import ConfigTab
from gui.history_tab import HistoryTab
from gui.monitor_tab import MonitorTab
from gui.results_tab import ResultsTab
from gui.smu_tab import SMUTab
from gui.tuner_tab import TunerTab
from gui.memory_tab import MemoryTab
from gui.widgets.core_grid import CoreGridWidget
from history.context import detect_bios_change
from history.db import HistoryDB
from history.logger import TestRunLogger
from monitor.frequency import read_core_frequencies
from monitor.hwmon import HWMonReader
from monitor.msr import MSRReader

log = logging.getLogger(__name__)


class TestWorker(QThread):
    """Worker thread that runs the core scheduler."""

    core_started = Signal(int, int)  # core_id, cycle
    core_finished = Signal(int, object)  # core_id, StressResult
    status_updated = Signal(int, object)  # core_id, CoreTestStatus
    cycle_completed = Signal(int)
    test_completed = Signal(str)  # JSON-encoded results — avoids PySide6 dict marshalling crash

    def __init__(self, scheduler: CoreScheduler) -> None:
        super().__init__()
        self.scheduler = scheduler

        # wire callbacks
        self.scheduler.on_core_start = [lambda cid, cyc: self.core_started.emit(cid, cyc)]
        self.scheduler.on_core_finish = [lambda cid, res: self.core_finished.emit(cid, res)]
        self.scheduler.on_status_update = [lambda cid, st: self.status_updated.emit(cid, st)]
        self.scheduler.on_cycle_complete = [lambda cyc: self.cycle_completed.emit(cyc)]
        self.scheduler.on_test_complete = [
            lambda res: self.test_completed.emit(
                json.dumps({
                    str(k): [asdict(r) for r in v]
                    for k, v in res.items()
                })
            )
        ]

    def run(self) -> None:
        self.scheduler.run()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("CoreCycler")
        self.setMinimumSize(1000, 700)

        self._settings = load_settings()
        self._topology: CPUTopology | None = None
        self._worker: TestWorker | None = None
        self._test_start_time: float = 0
        self._hwmon = HWMonReader()
        self._msr = MSRReader()
        self._core_telemetry: dict[int, dict] = {}  # core_id -> {max_freq, max_temp, last_vcore}
        self._core_status_cache: dict[int, CoreTestStatus] = {}
        self._cached_cycle: int = 0
        self._active_test_core: int | None = None
        self._logger: TestRunLogger | None = None

        # History database
        self._history_db: HistoryDB | None = None
        if self._settings.record_history:
            try:
                self._history_db = HistoryDB()
                recovered = self._history_db.recover_incomplete_runs()
                if recovered:
                    for run_id, started_at in recovered:
                        log.info("Recovered stale session id=%d started_at=%s, marked as crashed", run_id, started_at)
                # Purge old runs
                if self._settings.history_retention_days > 0:
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        days=self._settings.history_retention_days
                    )
                    self._history_db.purge_before(cutoff.isoformat())
                # Check for BIOS version changes
                self._bios_changed = False
                self._bios_old = ""
                self._bios_current = ""
                try:
                    changed, old, current = detect_bios_change(self._history_db)
                    if changed:
                        self._bios_changed = True
                        self._bios_old = old
                        self._bios_current = current
                        log.info("BIOS version changed: %s -> %s", old, current)
                except Exception:
                    log.debug("Failed to detect BIOS change", exc_info=True)
            except Exception:
                log.exception("Failed to initialize history database")
                self._history_db = None

        self._detect_cpu()
        self._setup_ui()
        self._setup_toolbar()
        self._setup_status_bar()
        self._setup_timer()

        self.resize(self._settings.window_width, self._settings.window_height)

    def _detect_cpu(self) -> None:
        self._topology = detect_topology()

    def _setup_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)

        # left: core grid
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
            info_label.setStyleSheet("color: #aaa; padding: 0 6px;")
            left.addWidget(info_label)

        self._core_grid = CoreGridWidget(self._topology)
        self._core_grid.setMaximumWidth(200)
        left.addWidget(self._core_grid)

        main_layout.addLayout(left, stretch=0)

        # right: tabs — align with CPU header on the left
        self._tabs = QTabWidget()
        self._tabs.setContentsMargins(0, 0, 0, 0)
        self._tabs.setDocumentMode(True)

        self._config_tab = ConfigTab(self._topology)
        self._config_tab.set_profile(self._settings.active_profile)
        self._tabs.addTab(self._config_tab, "Configuration")

        self._results_tab = ResultsTab()
        self._tabs.addTab(self._results_tab, "Results")

        self._monitor_tab = MonitorTab(topology=self._topology)
        self._tabs.addTab(self._monitor_tab, "Monitor")

        self._smu_tab = SMUTab(self._topology)
        self._tabs.addTab(self._smu_tab, "Curve Optimizer")

        smu = self._smu_tab.smu if hasattr(self._smu_tab, "smu") else None
        self._tuner_tab = TunerTab(self._history_db, self._topology, smu)
        self._tuner_tab.tuner_running_changed.connect(self._on_tuner_running_changed)
        self._tuner_tab.tuner_core_testing.connect(self._on_tuner_core_update)
        self._tuner_tab.tuner_core_elapsed.connect(self._on_tuner_core_elapsed)
        self._tuner_tab.tuner_core_info.connect(self._on_tuner_core_info)
        self._tabs.addTab(self._tuner_tab, "Auto-Tuner")

        self._history_tab = HistoryTab(self._history_db)
        if getattr(self, "_bios_changed", False):
            self._history_tab.set_bios_warning(self._bios_old, self._bios_current)
        self._history_tab.load_profile_requested.connect(self._on_load_co_profile)
        self._tabs.addTab(self._history_tab, "History")

        self._memory_tab = MemoryTab()
        self._memory_tab.memory_stress_started.connect(self._on_memory_stress_started)
        self._memory_tab.memory_stress_done.connect(self._on_memory_stress_done)
        self._tabs.addTab(self._memory_tab, "Memory")

        self._tabs.currentChanged.connect(self._on_tab_changed)
        main_layout.addWidget(self._tabs, stretch=2)

    def _setup_toolbar(self) -> None:
        toolbar = QToolBar("Test Control")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        self._start_btn = QPushButton("▶ Start Test")
        self._start_btn.setFixedHeight(36)
        self._start_btn.setStyleSheet(
            "QPushButton { background: #1b5e20; color: white; padding: 0 16px; "
            "border-radius: 4px; font-weight: bold; font-size: 13px; } "
            "QPushButton:hover { background: #2e7d32; } "
            "QPushButton:disabled { background: #444; color: #777; }"
        )
        self._start_btn.clicked.connect(self._start_test)
        toolbar.addWidget(self._start_btn)

        self._stop_btn = QPushButton("⏹ Stop")
        self._stop_btn.setFixedHeight(36)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton { background: #b71c1c; color: white; padding: 0 16px; "
            "border-radius: 4px; font-weight: bold; font-size: 13px; } "
            "QPushButton:hover { background: #c62828; } "
            "QPushButton:disabled { background: #444; color: #777; }"
        )
        self._stop_btn.clicked.connect(self._stop_test)
        toolbar.addWidget(self._stop_btn)

        toolbar.addSeparator()

        # profile management
        save_action = QAction("Save Profile", self)
        save_action.triggered.connect(self._save_profile)
        toolbar.addAction(save_action)

        load_action = QAction("Load Profile", self)
        load_action.triggered.connect(self._load_profile)
        toolbar.addAction(load_action)

    def _setup_status_bar(self) -> None:
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_msg = QLabel("Ready")
        self._status_bar.addWidget(self._status_msg)

        # Check actual device access rather than euid — supports udev/group-based
        # permissions without requiring root
        missing: list[str] = []
        try:
            fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
            os.close(fd)
        except (OSError, PermissionError):
            missing.append("MSR (clock stretch, per-core power, package power)")
        if not Path("/sys/kernel/ryzen_smu_drv/smu_args").exists() or not os.access(
            "/sys/kernel/ryzen_smu_drv/smu_args", os.W_OK
        ):
            missing.append("Curve Optimizer (SMU)")
        if missing:
            priv_label = QLabel(
                "  ⚠ " + " and ".join(missing) + " unavailable — "
                "check device permissions or run as root"
            )
            priv_label.setStyleSheet("color: #ffb74d; font: 10px monospace;")
            self._status_bar.addPermanentWidget(priv_label)

    def _setup_timer(self) -> None:
        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.timeout.connect(self._update_elapsed)

    def _start_test(self) -> None:
        if not self._topology:
            QMessageBox.warning(self, "Error", "CPU topology not detected")
            return

        # Check if memory stress is running
        if hasattr(self, '_memory_tab') and self._memory_tab._stress_worker and self._memory_tab._stress_worker.isRunning():
            QMessageBox.warning(self, "Memory Stress Active",
                "A memory stress test is running. Stop it before starting a core test.")
            return

        # Warn if an active/paused tuner session exists — running a manual test
        # won't corrupt CO offsets (manual tests don't touch SMU), but the user
        # should be aware their tuner session is waiting.
        if self._history_db:
            from tuner import persistence as _tp
            active = _tp.get_active_session(self._history_db)
            if active:
                reply = QMessageBox.question(
                    self, "Active Tuner Session",
                    f"A tuner session is {active.status} (started {active.created_at[:10]}).\n\n"
                    "Manual stress tests don't modify CO offsets, but you may want to "
                    "resume or abort the tuner session first.\n\n"
                    "Continue with manual test anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        profile = self._config_tab.get_profile()

        # select backend
        backend = self._get_backend(profile.backend)
        if not backend:
            return

        if not backend.is_available():
            QMessageBox.warning(
                self,
                "Backend Not Found",
                f"'{profile.backend}' is not installed or not on PATH.\n\n"
                "Install it or select a different backend.",
            )
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

        work_dir = Path(self._settings.work_dir)
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

        # init results tab
        self._results_tab.init_cores(scheduler.core_status)

        # create worker
        self._core_status_cache.clear()
        self._cached_cycle = 0
        self._active_test_core = None
        self._worker = TestWorker(scheduler)
        self._worker.core_started.connect(self._on_core_started)
        self._worker.core_finished.connect(self._on_core_finished)
        self._worker.status_updated.connect(self._on_status_updated)
        self._worker.status_updated.connect(self._on_status_cached)
        self._worker.cycle_completed.connect(self._on_cycle_completed)
        self._worker.cycle_completed.connect(self._on_cycle_cached)
        self._worker.test_completed.connect(self._on_test_completed)
        self._worker.finished.connect(self._on_worker_finished)

        # History logger
        self._logger = None
        if self._settings.record_history and self._history_db and self._topology:
            try:
                smu = self._smu_tab.smu if hasattr(self, '_smu_tab') else None
                self._logger = TestRunLogger(self._history_db, self._topology, profile, smu=smu)
                self._worker.core_started.connect(self._logger.on_core_started)
                self._worker.core_finished.connect(self._logger.on_core_finished)
                self._worker.status_updated.connect(self._logger.on_status_updated)
                self._worker.cycle_completed.connect(self._logger.on_cycle_completed)
                self._worker.test_completed.connect(self._logger.on_test_completed)
            except Exception:
                log.exception("Failed to create history logger")
                self._logger = None

        # UI state — mutual exclusion with tuner and memory stress
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

        # Disconnect logger from worker signals BEFORE stopping — prevents
        # half-torn-down logger from receiving queued signals during shutdown
        if self._logger and self._worker:
            with contextlib.suppress(RuntimeError):
                self._worker.core_started.disconnect(self._logger.on_core_started)
                self._worker.core_finished.disconnect(self._logger.on_core_finished)
                self._worker.status_updated.disconnect(self._logger.on_status_updated)
                self._worker.cycle_completed.disconnect(self._logger.on_cycle_completed)
                self._worker.test_completed.disconnect(self._logger.on_test_completed)

        # Disconnect thread-safety cache signals
        if self._worker:
            with contextlib.suppress(RuntimeError):
                self._worker.status_updated.disconnect(self._on_status_cached)
                self._worker.cycle_completed.disconnect(self._on_cycle_cached)

        if self._logger:
            # Save any accumulated peak telemetry before stopping
            for core_id, t in self._core_telemetry.items():
                if t["max_freq"] > 0:
                    try:
                        self._logger.update_core_telemetry_peaks(
                            core_id,
                            peak_freq_mhz=t["max_freq"],
                            max_temp_c=t["max_temp"],
                            min_vcore_v=t["min_vcore"],
                            max_vcore_v=t["max_vcore"],
                        )
                    except Exception:
                        pass
            try:
                self._logger.on_test_stopped()
            except Exception:
                log.exception("Failed to record test stop in history")
            self._logger = None
        self._core_telemetry.clear()

        # Signal the scheduler to stop — worker thread will finish naturally
        # and _on_worker_finished will handle UI cleanup
        self._worker.scheduler.stop()

    def _get_backend(self, name: str):
        match name:
            case "mprime":
                return MprimeBackend()
            case "stress-ng":
                return StressNgBackend()
            case "y-cruncher":
                return YCruncherBackend()
            case _:
                QMessageBox.warning(self, "Error", f"Unknown backend: {name}")
                return None

    @Slot(int, int)
    def _on_core_started(self, core_id: int, cycle: int) -> None:
        self._status_msg.setText(f"Testing core {core_id} (cycle {cycle + 1})")
        self._monitor_tab.set_active_core(core_id)
        self._active_test_core = core_id

    @Slot(int, object)
    def _on_status_cached(self, core_id: int, status: CoreTestStatus) -> None:
        """Cache core status from worker thread — signal/slot is thread-safe."""
        self._core_status_cache[core_id] = status

    @Slot(int)
    def _on_cycle_cached(self, cycle: int) -> None:
        """Cache cycle number from worker thread — signal/slot is thread-safe."""
        self._cached_cycle = cycle

    @Slot(int, object)
    def _on_core_finished(self, core_id: int, result: StressResult) -> None:
        status = self._core_status_cache.get(core_id)
        if status:
            self._core_grid.update_core_status(core_id, status)
            self._results_tab.update_core(core_id, status)

        if result and not result.passed:
            self._results_tab.add_error(core_id, result.error_message or "Unknown error")

        # log telemetry summary for this core
        t = self._core_telemetry.pop(core_id, None)
        if t and t["max_freq"] > 0:
            extra_parts = []
            if t["min_vcore"] is not None and t["max_vcore"] is not None:
                extra_parts.append(f"Vcore: {t['min_vcore']:.4f}-{t['max_vcore']:.4f}V")
            if t["max_stretch_pct"] > 0.5:
                extra_parts.append(f"Stretch: {t['max_stretch_pct']:.1f}%")
            if t.get("core_watts") is not None:
                extra_parts.append(f"Power: {t['core_watts']:.1f}W")
            extra = ("  " + "  ".join(extra_parts)) if extra_parts else ""
            state = "PASS" if (result and result.passed) else "FAIL"
            self._results_tab.add_log(
                core_id,
                f"[{state}] Peak: {t['max_freq']:.0f} MHz, "
                f"Max temp: {t['max_temp']:.1f}C{extra}",
            )

            # Record peak telemetry in history
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
        results = json.loads(results_json)
        # Keys are stringified core_ids, values are lists of result dicts
        total = len(results)
        passed = sum(
            1
            for r_list in results.values()
            if r_list and all(r["passed"] for r in r_list)
        )
        failed = total - passed
        elapsed = time.monotonic() - self._test_start_time

        profile = self._config_tab.get_profile()
        self._results_tab.update_summary(
            total=total,
            passed=passed,
            failed=failed,
            elapsed=elapsed,
            cycle=profile.cycle_count,
            total_cycles=profile.cycle_count,
        )

        # enable "Retest Failed" button with the list of failed cores
        failed_cores = [
            int(cid)
            for cid, r_list in results.items()
            if r_list and not all(r["passed"] for r in r_list)
        ]
        self._config_tab.set_failed_cores(failed_cores)

    def _on_worker_finished(self) -> None:
        was_stopping = not self._stop_btn.isEnabled()
        self._cleanup_worker()

        # Process any pending queued signals from the worker thread before
        # discarding the logger — cross-thread signals use QueuedConnection
        # and may still be in the event queue.
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()

        self._logger = None
        self._history_tab.refresh()
        if was_stopping:
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
        self._cached_cycle = 0
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
    def _on_tuner_core_info(self, core_id: int, co_offset: int, phase: str) -> None:
        """Pass CO offset and tuner phase to core grid for sidebar display."""
        self._core_grid.update_core_telemetry(core_id, co_offset=co_offset, tuner_phase=phase)

    @Slot()
    def _on_memory_stress_started(self) -> None:
        """Set all cores to memory stress state in sidebar."""
        for core_id in self._core_grid._cells:
            status = CoreTestStatus(core_id=core_id, state="mem_stress")
            self._core_grid.update_core_status(core_id, status)

    @Slot(bool)
    def _on_memory_stress_done(self, passed: bool) -> None:
        """Reset all cores to pending after memory stress."""
        for core_id in self._core_grid._cells:
            status = CoreTestStatus(core_id=core_id, state="pending")
            self._core_grid.update_core_status(core_id, status)

    @Slot(object)
    def _on_load_co_profile(self, profile) -> None:
        """Load CO profile from history into Curve Optimizer tab."""
        self._smu_tab.set_co_profile(profile)
        self._tabs.setCurrentWidget(self._smu_tab)

    @Slot(int)
    def _on_tab_changed(self, index: int) -> None:
        """Refresh data when switching tabs."""
        widget = self._tabs.widget(index)
        # Auto-refresh CO values when switching to Curve Optimizer tab
        if widget is self._smu_tab and hasattr(self._smu_tab, '_read_all_co'):
            if self._tuner_tab.is_running:
                self._smu_tab._read_all_co()

    def _update_elapsed(self) -> None:
        if not self._worker:
            return
        # crash watchdog: if thread object exists but is no longer running
        if not self._worker.isRunning():
            self._cleanup_worker()
            self._status_msg.setText("Test stopped (worker exited unexpectedly)")
            return
        elapsed = time.monotonic() - self._test_start_time
        cache = self._core_status_cache
        total = len(cache)
        passed = sum(1 for s in cache.values() if s.state == "passed")
        failed = sum(1 for s in cache.values() if s.state == "failed")

        profile = self._config_tab.get_profile()
        self._results_tab.update_summary(
            total=total,
            passed=passed,
            failed=failed,
            elapsed=elapsed,
            cycle=self._cached_cycle + 1,
            total_cycles=profile.cycle_count,
        )

        # feed per-core telemetry to the grid for the active core
        self._feed_core_grid_telemetry()

    def _feed_core_grid_telemetry(self) -> None:
        """Read freq/temp/voltage/stretch and push to the active core's grid cell.

        Uses ``_active_test_core`` (set by ``_on_core_started`` signal handler
        in the GUI thread) instead of reading scheduler state directly
        across threads -- this is the Phase 2 signal/slot cache pattern.
        """
        current_core = self._active_test_core
        if current_core is None:
            return

        core_info = self._topology.cores.get(current_core) if self._topology else None
        if not core_info:
            return

        logical_cpu = core_info.logical_cpus[0]

        # per-core frequency
        freqs = read_core_frequencies()
        freq = freqs.get(logical_cpu, 0)

        # MSR-based clock stretch detection (APERF/MPERF ratio)
        stretch_pct: float | None = None
        if self._msr.is_available():
            stretch_readings = self._msr.read_clock_stretch([logical_cpu])
            stretch_reading = stretch_readings.get(logical_cpu)
            if stretch_reading:
                stretch_pct = stretch_reading.stretch_pct

        # Per-core power from MSR RAPL
        core_watts: float | None = None
        if self._msr.is_available():
            power_readings = self._msr.read_core_power([logical_cpu])
            power_reading = power_readings.get(logical_cpu)
            if power_reading:
                core_watts = power_reading.watts

        # temperature (prefer per-CCD temp over package Tctl)
        hwmon_data = self._hwmon.read()
        ccd = core_info.ccd if core_info.ccd is not None else 0
        # k10temp: Tccd1 -> CCD 0 (index offset)
        temp = hwmon_data.tccd_temps.get(ccd + 1, hwmon_data.tctl_c or 0)
        vcore = hwmon_data.vcore_v

        self._core_grid.update_core_telemetry(
            current_core, freq, temp, vcore, stretch_pct=stretch_pct,
        )

        # Record telemetry sample in history
        if self._logger and self._settings.record_telemetry:
            try:
                self._logger.record_telemetry_sample(
                    current_core, freq, temp, vcore, effective_max_mhz=None,
                )
            except Exception:
                pass  # don't spam logs every second

        # track peak telemetry per core for the log
        if current_core not in self._core_telemetry:
            self._core_telemetry[current_core] = {
                "max_freq": 0.0,
                "max_stretch_pct": 0.0,
                "core_watts": None,
                "max_temp": 0.0,
                "last_vcore": None,
                "min_vcore": None,
                "max_vcore": None,
            }
        t = self._core_telemetry[current_core]
        if freq > t["max_freq"]:
            t["max_freq"] = freq
        if stretch_pct is not None and stretch_pct > t["max_stretch_pct"]:
            t["max_stretch_pct"] = stretch_pct
        if core_watts is not None:
            t["core_watts"] = core_watts
        if temp > t["max_temp"]:
            t["max_temp"] = temp
        if vcore is not None:
            t["last_vcore"] = vcore
            if t["min_vcore"] is None or vcore < t["min_vcore"]:
                t["min_vcore"] = vcore
            if t["max_vcore"] is None or vcore > t["max_vcore"]:
                t["max_vcore"] = vcore

    def _save_profile(self) -> None:
        from config.settings import save_profile

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Profile", str(Path.home()), "JSON (*.json)"
        )
        if path:
            try:
                profile = self._config_tab.get_profile()
                save_profile(profile, Path(path))
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to save profile: {e}")

    def _load_profile(self) -> None:
        from config.settings import load_profile

        path, _ = QFileDialog.getOpenFileName(
            self, "Load Profile", str(Path.home()), "JSON (*.json)"
        )
        if path:
            try:
                profile = load_profile(Path(path))
                self._config_tab.set_profile(profile)
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to load profile: {e}")

    def closeEvent(self, event) -> None:
        # Check if ANYTHING is running (manual test OR tuner OR memory stress)
        manual_running = self._worker and self._worker.isRunning()
        tuner_running = self._tuner_tab.is_running
        memory_running = hasattr(self, '_memory_tab') and self._memory_tab._stress_worker is not None and self._memory_tab._stress_worker.isRunning()

        if manual_running or tuner_running or memory_running:
            reply = QMessageBox.question(
                self,
                "Test Running",
                "A test is still running. Stop and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return

        # Stop manual test worker
        if manual_running:
            # Disconnect logger BEFORE stopping worker — prevents queued signals
            # from writing to the DB after we close it
            if self._logger and self._worker:
                with contextlib.suppress(RuntimeError):
                    self._worker.core_started.disconnect(self._logger.on_core_started)
                    self._worker.core_finished.disconnect(self._logger.on_core_finished)
                    self._worker.status_updated.disconnect(self._logger.on_status_updated)
                    self._worker.cycle_completed.disconnect(self._logger.on_cycle_completed)
                    self._worker.test_completed.disconnect(self._logger.on_test_completed)
                # Mark the run as stopped before closing DB
                try:
                    self._logger.on_test_stopped()
                except Exception:
                    pass
                self._logger = None

            # Disconnect thread-safety cache signals
            with contextlib.suppress(RuntimeError):
                self._worker.status_updated.disconnect(self._on_status_cached)
                self._worker.cycle_completed.disconnect(self._on_cycle_cached)

            self._worker.scheduler.force_stop()
            if not self._worker.wait(5000):
                self._worker.terminate()
                self._worker.wait(2000)

        # Stop auto-tuner
        if tuner_running:
            self._tuner_tab.force_stop()

        # Stop memory stress test if running
        if hasattr(self, '_memory_tab'):
            self._memory_tab.force_stop()

        # Process any remaining queued signals before closing DB
        from PySide6.QtCore import QCoreApplication
        QCoreApplication.processEvents()

        # save window size
        self._settings.window_width = self.width()
        self._settings.window_height = self.height()
        save_settings(self._settings)

        self._monitor_tab.stop_monitoring()
        if self._msr:
            self._msr.close()
        if self._history_db:
            self._history_db.close()
        event.accept()
