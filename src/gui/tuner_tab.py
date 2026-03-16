"""Auto-Tuner tab — automated PBO Curve Optimizer search UI."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from engine.backends.base import FFTPreset, StressMode
from tuner.config import TunerConfig
from tuner.engine import TunerEngine
from tuner import persistence as tp

if TYPE_CHECKING:
    from engine.backends.base import StressBackend
    from engine.topology import CPUTopology
    from history.db import HistoryDB
    from smu.driver import RyzenSMU

log = logging.getLogger(__name__)

# Phase colors
PHASE_COLORS = {
    "not_started": QColor(100, 100, 100),
    "coarse_search": QColor(180, 180, 50),
    "fine_search": QColor(200, 200, 50),
    "settled": QColor(200, 150, 50),
    "confirming": QColor(50, 150, 200),
    "confirmed": QColor(50, 180, 50),
    "failed_confirm": QColor(200, 100, 50),
}


class TunerTab(QWidget):
    """Auto-Tuner tab for the main window."""

    # Emitted when tuner starts/stops so MainWindow can disable manual test
    tuner_running_changed = Signal(bool)
    tuner_core_testing = Signal(int, str)  # core_id, state ("testing"/"passed"/"failed"/etc)
    tuner_core_elapsed = Signal(int, float)  # core_id, elapsed_seconds

    def __init__(
        self,
        db: HistoryDB | None,
        topology: CPUTopology | None,
        smu: RyzenSMU | None,
        backend_factory=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._db = db
        self._topology = topology
        self._smu = smu
        self._backend_factory = backend_factory
        self._engine: TunerEngine | None = None
        self._selected_core: int | None = None
        self._pending_resume_id: int | None = None

        self._tuner_timer = QTimer(self)
        self._tuner_timer.timeout.connect(self._tick_tuner)
        self._active_test_core: int | None = None
        self._test_start_time: float = 0

        self._setup_ui()
        self._check_resume()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Status bar
        status_layout = QHBoxLayout()
        self._status_label = QLabel("Status: IDLE")
        self._status_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        status_layout.addWidget(self._status_label)

        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #aaa;")
        status_layout.addWidget(self._progress_label)
        status_layout.addStretch()
        layout.addLayout(status_layout)

        # Main splitter: config+table on top, log on bottom
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Top section
        top = QWidget()
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)

        # Config panel — no scroll, just a plain container
        self._config_container = QWidget()
        config_inner = QVBoxLayout(self._config_container)
        config_inner.setContentsMargins(0, 0, 0, 0)
        config_inner.setSpacing(8)
        self._build_config_panel(config_inner)
        top_layout.addWidget(self._config_container)

        # Action buttons
        btn_layout = QHBoxLayout()
        self._start_btn = QPushButton("Start Tuning")
        self._start_btn.setStyleSheet(
            "QPushButton { background: #1b5e20; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:disabled { background: #555; color: #888; }"
        )
        self._start_btn.clicked.connect(self._on_start)
        btn_layout.addWidget(self._start_btn)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self._on_pause)
        btn_layout.addWidget(self._pause_btn)

        self._resume_btn = QPushButton("Resume")
        self._resume_btn.setEnabled(False)
        self._resume_btn.clicked.connect(self._on_resume)
        btn_layout.addWidget(self._resume_btn)

        self._abort_btn = QPushButton("Abort")
        self._abort_btn.setEnabled(False)
        self._abort_btn.setStyleSheet(
            "QPushButton { background: #b71c1c; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: bold; }"
            "QPushButton:disabled { background: #555; color: #888; }"
        )
        self._abort_btn.clicked.connect(self._on_abort)
        btn_layout.addWidget(self._abort_btn)

        btn_layout.addStretch()

        self._validate_btn = QPushButton("Validate Profile")
        self._validate_btn.setEnabled(False)
        self._validate_btn.clicked.connect(self._on_validate)
        btn_layout.addWidget(self._validate_btn)

        self._export_btn = QPushButton("Export Profile")
        self._export_btn.setEnabled(False)
        self._export_btn.clicked.connect(self._on_export)
        btn_layout.addWidget(self._export_btn)

        top_layout.addLayout(btn_layout)

        # Core status table
        self._core_table = QTableWidget()
        self._core_table.setColumnCount(7)
        self._core_table.setHorizontalHeaderLabels([
            "Core", "CCD", "Phase", "Current Offset", "Best Offset",
            "Tests Run", "Last Result",
        ])
        self._core_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._core_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._core_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._core_table.currentCellChanged.connect(self._on_core_selected)
        top_layout.addWidget(self._core_table)

        splitter.addWidget(top)

        # Bottom: test log
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        log_header = QHBoxLayout()
        log_label = QLabel("Test Log")
        log_label.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        log_header.addWidget(log_label)

        self._log_filter_label = QLabel("(all cores)")
        self._log_filter_label.setStyleSheet("color: #aaa;")
        log_header.addWidget(self._log_filter_label)
        log_header.addStretch()

        clear_log_btn = QPushButton("Clear")
        clear_log_btn.setFixedWidth(60)
        clear_log_btn.clicked.connect(lambda: self._log_table.setRowCount(0))
        log_header.addWidget(clear_log_btn)

        bottom_layout.addLayout(log_header)

        self._log_table = QTableWidget()
        self._log_table.setColumnCount(7)
        self._log_table.setHorizontalHeaderLabels([
            "Time", "Core", "Offset", "Phase", "Result", "Duration", "Error",
        ])
        self._log_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._log_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        bottom_layout.addWidget(self._log_table)

        splitter.addWidget(bottom)
        splitter.setSizes([400, 200])
        layout.addWidget(splitter)

    def _build_config_panel(self, parent_layout: QVBoxLayout) -> None:
        # Two-column layout: search params on left, test settings on right
        columns = QHBoxLayout()

        # --- Left column: Search Parameters ---
        search_group = QGroupBox("Search Parameters")
        search_layout = QFormLayout(search_group)
        search_layout.setSpacing(6)

        self._start_offset_spin = QSpinBox()
        self._start_offset_spin.setRange(-60, 30)
        self._start_offset_spin.setValue(0)
        self._start_offset_spin.setToolTip(
            "Starting CO value for all cores (0 = BIOS baseline)"
        )
        search_layout.addRow("Start offset:", self._start_offset_spin)

        self._inherit_current_check = QCheckBox("Inherit current CO from SMU")
        self._inherit_current_check.setToolTip(
            "Read current CO offsets from SMU at session start and use them\n"
            "as starting points instead of the fixed start offset above.\n"
            "Useful for incremental tuning from an existing baseline."
        )
        search_layout.addRow("", self._inherit_current_check)

        self._coarse_step_spin = QSpinBox()
        self._coarse_step_spin.setRange(1, 15)
        self._coarse_step_spin.setValue(5)
        self._coarse_step_spin.setToolTip(
            "Step size during coarse search phase (bigger = faster but less precise)"
        )
        search_layout.addRow("Coarse step:", self._coarse_step_spin)

        self._fine_step_spin = QSpinBox()
        self._fine_step_spin.setRange(1, 5)
        self._fine_step_spin.setValue(1)
        self._fine_step_spin.setToolTip(
            "Step size during fine search phase (1 = test every value)"
        )
        search_layout.addRow("Fine step:", self._fine_step_spin)

        self._max_offset_spin = QSpinBox()
        self._max_offset_spin.setRange(-60, 60)
        self._max_offset_spin.setValue(-50)
        self._max_offset_spin.setToolTip(
            "Most aggressive offset to try (auto-clamped to CPU generation range)"
        )
        search_layout.addRow("Max offset:", self._max_offset_spin)

        self._max_retries_spin = QSpinBox()
        self._max_retries_spin.setRange(0, 5)
        self._max_retries_spin.setValue(2)
        self._max_retries_spin.setToolTip(
            "How many times to retry confirmation before backing off"
        )
        search_layout.addRow("Confirm retries:", self._max_retries_spin)

        self._stretch_threshold_spin = QDoubleSpinBox()
        self._stretch_threshold_spin.setRange(0.0, 20.0)
        self._stretch_threshold_spin.setSingleStep(0.5)
        self._stretch_threshold_spin.setValue(3.0)
        self._stretch_threshold_spin.setSuffix("%")
        self._stretch_threshold_spin.setToolTip(
            "Clock stretch threshold — if APERF/MPERF stretch exceeds this %\n"
            "during a test, mark it as FAIL even if stress test passed.\n"
            "0 = disabled. 3% = recommended. Requires root (MSR access)."
        )
        search_layout.addRow("Stretch threshold:", self._stretch_threshold_spin)

        self._order_combo = QComboBox()
        self._order_combo.addItems(["sequential", "round_robin", "weakest_first", "ccd_alternating", "ccd_round_robin"])
        self._order_combo.setToolTip(
            "sequential: finish each core before moving to next\n"
            "round_robin: cycle through all cores, one test each\n"
            "weakest_first: prioritize cores closest to settling\n"
            "ccd_alternating: alternate between CCDs (catches thermal interactions)\n"
            "ccd_round_robin: rotate one test per core, alternating CCDs (cool-down time)"
        )
        search_layout.addRow("Test order:", self._order_combo)

        columns.addWidget(search_group)

        # --- Right column: Stress Test + Timing ---
        right_col = QVBoxLayout()

        stress_group = QGroupBox("Stress Test")
        stress_layout = QFormLayout(stress_group)
        stress_layout.setSpacing(6)

        self._backend_combo = QComboBox()
        self._backend_combo.addItems(["mprime", "stress-ng", "y-cruncher"])
        self._backend_combo.setToolTip(
            "mprime: Prime95 CLI — gold standard for CO testing (most sensitive)\n"
            "stress-ng: general-purpose — good fallback\n"
            "y-cruncher: multi-algorithm — supplementary testing"
        )
        stress_layout.addRow("Backend:", self._backend_combo)

        self._mode_combo = QComboBox()
        for mode in StressMode:
            if mode != StressMode.CUSTOM:
                self._mode_combo.addItem(mode.name)
        self._mode_combo.setCurrentText("SSE")
        self._mode_combo.setToolTip(
            "SSE: highest single-core boost — most sensitive for CO testing\n"
            "AVX/AVX2: different execution units — good for supplementary testing"
        )
        stress_layout.addRow("Mode:", self._mode_combo)

        self._fft_combo = QComboBox()
        for preset in FFTPreset:
            if preset != FFTPreset.CUSTOM:
                self._fft_combo.addItem(preset.name)
        self._fft_combo.setCurrentText("SMALL")
        self._fft_combo.setToolTip(
            "SMALL: 36K-248K — fastest CO failure detection, FPU-bound\n"
            "LARGE: 426K-8192K — tests memory controller interaction\n"
            "HEAVY: 4K-1344K — broadest coverage of FPU paths"
        )
        stress_layout.addRow("FFT preset:", self._fft_combo)

        right_col.addWidget(stress_group)

        timing_group = QGroupBox("Timing")
        timing_layout = QFormLayout(timing_group)
        timing_layout.setSpacing(6)

        self._search_dur_spin = QSpinBox()
        self._search_dur_spin.setRange(10, 600)
        self._search_dur_spin.setValue(60)
        self._search_dur_spin.setSuffix("s")
        self._search_dur_spin.setToolTip(
            "Seconds per core during coarse/fine search (60s is sufficient for most failures)"
        )
        timing_layout.addRow("Search duration:", self._search_dur_spin)

        self._confirm_dur_spin = QSpinBox()
        self._confirm_dur_spin.setRange(30, 1800)
        self._confirm_dur_spin.setValue(300)
        self._confirm_dur_spin.setSuffix("s")
        self._confirm_dur_spin.setToolTip(
            "Seconds per core for confirmation run (longer = higher confidence)"
        )
        timing_layout.addRow("Confirm duration:", self._confirm_dur_spin)

        right_col.addWidget(timing_group)

        columns.addLayout(right_col)
        parent_layout.addLayout(columns)

        # Defaults button
        btn_row = QHBoxLayout()
        defaults_btn = QPushButton("Load Defaults")
        defaults_btn.clicked.connect(self._load_defaults)
        btn_row.addWidget(defaults_btn)
        btn_row.addStretch()
        parent_layout.addLayout(btn_row)

    def _get_config(self) -> TunerConfig:
        return TunerConfig(
            start_offset=self._start_offset_spin.value(),
            coarse_step=self._coarse_step_spin.value(),
            fine_step=self._fine_step_spin.value(),
            max_offset=self._max_offset_spin.value(),
            search_duration_seconds=self._search_dur_spin.value(),
            confirm_duration_seconds=self._confirm_dur_spin.value(),
            max_confirm_retries=self._max_retries_spin.value(),
            stretch_threshold_pct=self._stretch_threshold_spin.value(),
            inherit_current=self._inherit_current_check.isChecked(),
            test_order=self._order_combo.currentText(),
            backend=self._backend_combo.currentText(),
            stress_mode=self._mode_combo.currentText(),
            fft_preset=self._fft_combo.currentText(),
        )

    def _load_defaults(self) -> None:
        cfg = TunerConfig()
        self._start_offset_spin.setValue(cfg.start_offset)
        self._coarse_step_spin.setValue(cfg.coarse_step)
        self._fine_step_spin.setValue(cfg.fine_step)
        self._max_offset_spin.setValue(cfg.max_offset)
        self._search_dur_spin.setValue(cfg.search_duration_seconds)
        self._confirm_dur_spin.setValue(cfg.confirm_duration_seconds)
        self._max_retries_spin.setValue(cfg.max_confirm_retries)
        self._stretch_threshold_spin.setValue(cfg.stretch_threshold_pct)
        self._inherit_current_check.setChecked(cfg.inherit_current)
        self._order_combo.setCurrentText(cfg.test_order)
        self._backend_combo.setCurrentText(cfg.backend)
        self._mode_combo.setCurrentText(cfg.stress_mode)
        self._fft_combo.setCurrentText(cfg.fft_preset)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_start(self) -> None:
        if not self._db or not self._topology:
            QMessageBox.warning(self, "Error", "Database or topology not available")
            return

        if not self._smu or not self._smu.is_available():
            QMessageBox.warning(
                self,
                "SMU Not Available",
                "The ryzen_smu kernel module is not loaded.\n\n"
                "The auto-tuner requires SMU access to write Curve Optimizer values.\n"
                "Load the module with: sudo modprobe ryzen_smu",
            )
            return

        backend = self._get_backend()
        if backend is None:
            return

        config = self._get_config()
        self._engine = TunerEngine(
            db=self._db,
            topology=self._topology,
            smu=self._smu,
            backend=backend,
            config=config,
        )
        self._wire_engine()

        self._set_running_state(True)
        self._engine.start()

        # Initialize table with all cores
        for core_id in self._engine.core_states:
            self._update_core_row(core_id)

    def _on_pause(self) -> None:
        if self._engine:
            self._engine.pause()
            self._pause_btn.setEnabled(False)
            self._resume_btn.setEnabled(True)

    def _on_resume(self) -> None:
        # Determine which session to resume
        resume_id = None
        if self._engine and self._engine.session_id:
            resume_id = self._engine.session_id
        elif self._pending_resume_id is not None:
            resume_id = self._pending_resume_id

        if resume_id is None:
            return

        # Validate dependencies are still available
        if not self._smu or not self._smu.is_available():
            QMessageBox.warning(
                self,
                "SMU Not Available",
                "The ryzen_smu kernel module is not loaded.\n\n"
                "The auto-tuner requires SMU access to write Curve Optimizer values.\n"
                "Load the module with: sudo modprobe ryzen_smu",
            )
            return

        # Create engine if needed (cold start resume)
        if self._engine is None:
            if not self._db or not self._topology:
                QMessageBox.warning(self, "Error", "Database or topology not available")
                return
            backend = self._get_backend()
            if backend is None:
                return
            self._engine = TunerEngine(
                db=self._db,
                topology=self._topology,
                smu=self._smu,
                backend=backend,
            )
            self._wire_engine()

        self._pending_resume_id = None
        self._set_running_state(True)
        self._engine.resume(resume_id)

        # Initialize table with all cores
        for core_id in self._engine.core_states:
            self._update_core_row(core_id)

    def _on_abort(self) -> None:
        if self._engine:
            self._engine.abort()
            self._set_running_state(False)

    def _on_validate(self) -> None:
        if not self._engine or not self._engine.session_id:
            return
        backend = self._get_backend()
        if backend is None:
            return
        self._set_running_state(True)
        self._engine.validate_profile(self._engine.session_id)

    def _on_export(self) -> None:
        if not self._engine or not self._engine.session_id or not self._db:
            return
        profile = tp.get_best_profile(self._db, self._engine.session_id)
        if not profile:
            QMessageBox.information(self, "Export", "No confirmed cores to export")
            return

        text = json.dumps(profile, indent=2, sort_keys=True)
        QMessageBox.information(
            self, "Confirmed CO Profile",
            f"Copy this profile:\n\n{text}",
        )

    # ------------------------------------------------------------------
    # Engine signals
    # ------------------------------------------------------------------

    def _wire_engine(self) -> None:
        if not self._engine:
            return
        self._engine.core_state_changed.connect(self._on_core_state_changed)
        self._engine.test_completed.connect(self._on_test_completed)
        self._engine.session_completed.connect(self._on_session_completed)
        self._engine.status_changed.connect(self._on_status_changed)
        self._engine.progress_updated.connect(self._on_progress_updated)
        self._engine.log_message.connect(self._on_log_message)

    @Slot(int, str, int)
    def _on_core_state_changed(self, core_id: int, phase: str, offset: int) -> None:
        self._update_core_row(core_id)
        # Map tuner phases to core grid visual states
        phase_to_grid = {
            "coarse_search": "testing",
            "fine_search": "testing",
            "confirming": "testing",
            "confirmed": "passed",
            "settled": "pending",
            "failed_confirm": "failed",
            "not_started": "pending",
        }
        grid_state = phase_to_grid.get(phase, "pending")
        self.tuner_core_testing.emit(core_id, grid_state)

        # Track active test for elapsed timer
        if grid_state == "testing":
            import time
            self._active_test_core = core_id
            self._test_start_time = time.monotonic()
            if not self._tuner_timer.isActive():
                self._tuner_timer.start(1000)
        elif core_id == self._active_test_core:
            self._active_test_core = None

    @Slot(int, int, bool)
    def _on_test_completed(self, core_id: int, offset: int, passed: bool) -> None:
        self._update_core_row(core_id)
        self._add_log_entry(core_id, offset, passed)

    @Slot(str)
    def _on_session_completed(self, profile_json: str) -> None:
        import json
        profile = json.loads(profile_json) if profile_json else {}
        self._set_running_state(False)
        self._validate_btn.setEnabled(bool(profile))
        self._export_btn.setEnabled(bool(profile))

    @Slot(str)
    def _on_status_changed(self, status: str) -> None:
        self._status_label.setText(f"Status: {status.upper()}")

    @Slot(int, int)
    def _on_progress_updated(self, done: int, total: int) -> None:
        self._progress_label.setText(f"{done}/{total} cores confirmed")

    @Slot(str)
    def _on_log_message(self, msg: str) -> None:
        log.info("[tuner] %s", msg)

    def _tick_tuner(self) -> None:
        if self._active_test_core is not None:
            import time
            elapsed = time.monotonic() - self._test_start_time
            self.tuner_core_elapsed.emit(self._active_test_core, elapsed)
        elif self._engine is None or self._engine.status == "idle":
            self._tuner_timer.stop()

    # ------------------------------------------------------------------
    # Table updates
    # ------------------------------------------------------------------

    def _update_core_row(self, core_id: int) -> None:
        if not self._engine:
            return
        cs = self._engine.core_states.get(core_id)
        if cs is None:
            return

        # Find or create row
        row = self._find_core_row(core_id)
        if row < 0:
            row = self._core_table.rowCount()
            self._core_table.insertRow(row)

        core_info = self._topology.cores.get(core_id) if self._topology else None
        ccd = core_info.ccd if core_info else None

        items = [
            str(core_id),
            str(ccd) if ccd is not None else "-",
            cs.phase.upper(),
            str(cs.current_offset),
            str(cs.best_offset) if cs.best_offset is not None else "-",
            str(self._count_tests(core_id)),
            self._last_result(core_id),
        ]

        color = PHASE_COLORS.get(cs.phase, QColor(100, 100, 100))
        for col, text in enumerate(items):
            item = QTableWidgetItem(text)
            item.setForeground(color)
            self._core_table.setItem(row, col, item)

    def _find_core_row(self, core_id: int) -> int:
        for row in range(self._core_table.rowCount()):
            item = self._core_table.item(row, 0)
            if item and item.text() == str(core_id):
                return row
        return -1

    def _count_tests(self, core_id: int) -> int:
        if not self._db or not self._engine or not self._engine.session_id:
            return 0
        entries = tp.get_test_log(self._db, self._engine.session_id, core_id=core_id)
        return len(entries)

    def _last_result(self, core_id: int) -> str:
        if not self._db or not self._engine or not self._engine.session_id:
            return "-"
        entries = tp.get_test_log(self._db, self._engine.session_id, core_id=core_id)
        if not entries:
            return "-"
        return "PASS" if entries[-1]["passed"] else "FAIL"

    def _add_log_entry(self, core_id: int, offset: int, passed: bool) -> None:
        if not self._db or not self._engine or not self._engine.session_id:
            return
        entries = tp.get_test_log(self._db, self._engine.session_id, core_id=core_id)
        if not entries:
            return
        entry = entries[-1]

        # If a core is selected, only show entries for that core
        if self._selected_core is not None and core_id != self._selected_core:
            return

        row = self._log_table.rowCount()
        self._log_table.insertRow(row)
        items = [
            entry.get("tested_at", "")[:19],
            str(core_id),
            str(offset),
            entry.get("phase", ""),
            "PASS" if passed else "FAIL",
            f"{entry.get('duration_seconds', 0):.1f}s" if entry.get("duration_seconds") else "-",
            entry.get("error_message", "") or "",
        ]
        color = QColor(50, 180, 50) if passed else QColor(200, 50, 50)
        for col, text in enumerate(items):
            item = QTableWidgetItem(text)
            if col == 4:  # Result column
                item.setForeground(color)
            self._log_table.setItem(row, col, item)

        self._log_table.scrollToBottom()

    @Slot(int, int, int, int)
    def _on_core_selected(self, row: int, col: int, prev_row: int, prev_col: int) -> None:
        item = self._core_table.item(row, 0)
        if item:
            self._selected_core = int(item.text())
            self._log_filter_label.setText(f"(core {self._selected_core})")
            self._refresh_log_table()
        else:
            self._selected_core = None
            self._log_filter_label.setText("(all cores)")
            self._refresh_log_table()

    def _refresh_log_table(self) -> None:
        """Rebuild the log table based on the selected core filter."""
        self._log_table.setRowCount(0)
        if not self._db or not self._engine or not self._engine.session_id:
            return

        entries = tp.get_test_log(
            self._db, self._engine.session_id,
            core_id=self._selected_core,
        )
        for entry in entries:
            row = self._log_table.rowCount()
            self._log_table.insertRow(row)
            passed = bool(entry["passed"])
            items = [
                entry.get("tested_at", "")[:19],
                str(entry["core_id"]),
                str(entry["offset_tested"]),
                entry.get("phase", ""),
                "PASS" if passed else "FAIL",
                f"{entry.get('duration_seconds', 0):.1f}s" if entry.get("duration_seconds") else "-",
                entry.get("error_message", "") or "",
            ]
            color = QColor(50, 180, 50) if passed else QColor(200, 50, 50)
            for col_idx, text in enumerate(items):
                item = QTableWidgetItem(text)
                if col_idx == 4:
                    item.setForeground(color)
                self._log_table.setItem(row, col_idx, item)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_running_state(self, running: bool) -> None:
        self._start_btn.setEnabled(not running)
        self._pause_btn.setEnabled(running)
        self._resume_btn.setEnabled(False)
        self._abort_btn.setEnabled(running)
        self._config_container.setEnabled(not running)
        self.tuner_running_changed.emit(running)

    def _get_backend(self) -> StressBackend | None:
        if self._backend_factory:
            backend = self._backend_factory(self._backend_combo.currentText())
        else:
            name = self._backend_combo.currentText()
            match name:
                case "mprime":
                    from engine.backends.mprime import MprimeBackend
                    backend = MprimeBackend()
                case "stress-ng":
                    from engine.backends.stress_ng import StressNgBackend
                    backend = StressNgBackend()
                case "y-cruncher":
                    from engine.backends.ycruncher import YCruncherBackend
                    backend = YCruncherBackend()
                case _:
                    QMessageBox.warning(self, "Error", f"Unknown backend: {name}")
                    return None

        if backend and not backend.is_available():
            QMessageBox.warning(
                self,
                "Backend Not Found",
                f"'{self._backend_combo.currentText()}' is not installed or not on PATH.\n\n"
                "Install it or select a different backend.",
            )
            return None
        return backend

    def _check_resume(self) -> None:
        """Check for active tuner sessions on startup."""
        if not self._db:
            return
        active = tp.get_active_session(self._db)
        if active:
            self._status_label.setText(
                f"Status: RECOVERABLE SESSION #{active.id} — click Resume to continue"
            )
            self._resume_btn.setEnabled(True)
            self._pending_resume_id = active.id

    def set_test_running(self, running: bool) -> None:
        """Called by MainWindow to disable tuner Start when manual test is active."""
        if running:
            self._start_btn.setEnabled(False)
            self._start_btn.setToolTip("Manual test is running")
        else:
            self._start_btn.setEnabled(True)
            self._start_btn.setToolTip("")

    def force_stop(self) -> None:
        """Force-stop the tuner engine and its worker — called on app exit."""
        if self._engine:
            self._engine.abort()

    @property
    def is_running(self) -> bool:
        return self._engine is not None and self._engine.status in ("running", "validating")
