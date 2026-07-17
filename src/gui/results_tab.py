"""Results dashboard — per-core pass/fail table, error log, summary."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from gui.style import COLOR_FAIL, COLOR_PASS, PENDING_VALUE, STATE_COLORS, duration_str, state_label
from gui.widgets import table_item as _item

if TYPE_CHECKING:
    from engine.scheduler import CoreTestStatus


class ResultsTab(QWidget):
    """Results dashboard with per-core table and error log."""

    def __init__(self) -> None:
        super().__init__()
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # summary bar
        summary_group = QGroupBox("Summary")
        summary_layout = QHBoxLayout(summary_group)

        self._total_label = QLabel(f"Cores: {PENDING_VALUE}")
        self._passed_label = QLabel(f"Passed: {PENDING_VALUE}")
        self._passed_label.setStyleSheet(f"color: {COLOR_PASS}; font-weight: bold;")
        self._failed_label = QLabel(f"Failed: {PENDING_VALUE}")
        self._failed_label.setStyleSheet(f"color: {COLOR_FAIL}; font-weight: bold;")
        self._elapsed_label = QLabel("Start a test to see results")
        self._cycle_label = QLabel("")

        summary_widgets = [
            self._total_label, self._passed_label, self._failed_label,
            self._elapsed_label, self._cycle_label,
        ]
        for w in summary_widgets:
            w.setFont(QFont("monospace", 10))
            summary_layout.addWidget(w)

        layout.addWidget(summary_group)

        # splitter: table + log
        splitter = QSplitter(Qt.Orientation.Vertical)

        # results table
        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(
            ["Core", "CCD", "Status", "Errors", "Iterations", "Time", "Last Error"]
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._table.setColumnWidth(0, 60)
        self._table.setColumnWidth(1, 50)
        self._table.setColumnWidth(3, 60)
        self._table.setColumnWidth(4, 80)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        splitter.addWidget(self._table)

        # error log
        log_group = QGroupBox("Test Log")
        log_layout = QVBoxLayout(log_group)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(QFont("monospace", 9))
        self._log.setMaximumBlockCount(1000)
        log_layout.addWidget(self._log)
        splitter.addWidget(log_group)

        splitter.setSizes([400, 200])
        layout.addWidget(splitter)

        self._core_rows: dict[int, int] = {}  # core_id -> row index

    def init_cores(self, core_statuses: dict[int, CoreTestStatus]) -> None:
        """Initialize the table with core entries — clears previous results."""
        self._log.clear()
        self._table.setRowCount(len(core_statuses))
        self._core_rows.clear()

        for row, (core_id, status) in enumerate(sorted(core_statuses.items())):
            self._core_rows[core_id] = row
            self._set_row(row, core_id, status)

    def update_core(self, core_id: int, status: CoreTestStatus) -> None:
        """Update a single core's row."""
        row = self._core_rows.get(core_id)
        if row is None:
            return
        self._set_row(row, core_id, status)

    def add_error(self, core_id: int, message: str) -> None:
        """Add an error entry to the log."""
        self._log.appendPlainText(f"[Core {core_id}] ERROR: {message}")

    def add_log(self, core_id: int, message: str) -> None:
        """Add an informational entry to the log."""
        self._log.appendPlainText(f"[Core {core_id}] {message}")

    def update_summary(
        self,
        total: int = 0,
        passed: int = 0,
        failed: int = 0,
        elapsed: float = 0,
        cycle: int = 0,
        total_cycles: int = 0,
    ) -> None:
        self._total_label.setText(f"Cores: {total}")
        self._passed_label.setText(f"Passed: {passed}")
        self._failed_label.setText(f"Failed: {failed}")

        self._elapsed_label.setText(f"Elapsed: {duration_str(elapsed)}")
        self._cycle_label.setText(f"Cycle: {cycle}/{total_cycles}")

    def clear(self) -> None:
        self._table.setRowCount(0)
        self._core_rows.clear()
        self._log.clear()

    def _set_row(self, row: int, core_id: int, status: CoreTestStatus) -> None:
        self._table.setItem(row, 0, _item(str(core_id), Qt.AlignmentFlag.AlignCenter))
        ccd_text = str(status.ccd) if status.ccd is not None else "-"
        self._table.setItem(
            row, 1, _item(ccd_text, Qt.AlignmentFlag.AlignCenter)
        )

        status_item = _item(
            state_label(status.state), Qt.AlignmentFlag.AlignCenter
        )
        _, fg, _border = STATE_COLORS.get(status.state, STATE_COLORS["pending"])
        status_item.setForeground(QColor(fg))
        self._table.setItem(row, 2, status_item)

        errors_item = _item(str(status.errors), Qt.AlignmentFlag.AlignCenter)
        if status.errors > 0:
            errors_item.setForeground(QColor(COLOR_FAIL))
        self._table.setItem(row, 3, errors_item)

        self._table.setItem(row, 4, _item(str(status.iterations), Qt.AlignmentFlag.AlignCenter))

        self._table.setItem(row, 5, _item(duration_str(status.elapsed_seconds), Qt.AlignmentFlag.AlignCenter))

        error_item = _item(status.last_error or "-")
        if status.last_error:
            error_item.setToolTip(status.last_error)
        self._table.setItem(row, 6, error_item)
