"""History browser tab — browse past runs grouped by tuning context."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from corecycler.engine import detector as kernel
from corecycler.gui.style import (
    SESSION_STATUS_LABELS,
    duration_str,
    font_mono,
    power_text,
    shown_status,
    span_str,
    stage_label,
    theme,
    tuner_phase_label,
    verdict_text,
)
from corecycler.gui.tuner_tab import ORDER_LABELS, THREADS, scope_text, settings_lines
from corecycler.gui.widgets import table_item as _item
from corecycler.history.context import bios_change_judged, last_recorded_bios
from corecycler.history.timefmt import format_local
from corecycler.smu.pmtable import stretch_pct
from corecycler.smu.writer_lock import running_driver
from corecycler.tuner.live import MHZ_BIN, ONE_CORE_STAGES, TEMP_BIN, own_peaks, read_stored
from corecycler.tuner.machine.types import Phase

if TYPE_CHECKING:
    from corecycler.history.db import HistoryDB, MachineSessionRecord, RunRecord, TuningContextRecord


VIEW_LABELS = {"grouped": "Stress tests by setup", "all": "All stress tests", "tuner": "Tuning sessions"}
SETUP_LABELS = {
    "app": "App",
    "bios": "BIOS",
    "curve_shaper": "Curve Shaper",
    "kernel": "Kernel",
    "microcode": "Microcode",
    "pbo": "PBO limits",
    "ryzen_smu": "ryzen_smu driver",
    "smu_firmware": "SMU firmware",
    "topology": "Topology",
}
ORIGINS = {"continue": "continued from", "restart": "started over from"}


def tested(test: dict) -> str:
    if test["stage"] in ONE_CORE_STAGES:
        threads = THREADS.get(test["klass"], test["klass"])
        return f"core {test['core']}, {stage_label(test['stage'])}, {threads}, at {test['value']}"
    return f"{stage_label(test['stage'])}, {scope_text(test)}"


def open_test_text(test: dict, newest: bool, boot: str, driver: bool) -> str:
    if test["boot_id"] != boot:
        return "no verdict: its boot ended"
    return "running" if newest and driver else "no verdict: the driver stopped"


def counts_html(counts: Counter[str]) -> str:
    order = [*SESSION_STATUS_LABELS, *sorted(set(counts) - set(SESSION_STATUS_LABELS))]
    return "&nbsp;&nbsp;&nbsp;".join(
        f'<span style="color: {theme.STATUS_COLORS.get(status, theme.COLOR_MUTED)};">'
        f"{SESSION_STATUS_LABELS.get(status, status)}: {counts[status]}</span>"
        for status in order
        if counts[status]
    )


def coverage_text(coverage_json: str | None, core: int) -> str:
    if coverage_json is None:
        return ""
    record = json.loads(coverage_json).get(str(core))
    if record is None:
        return ""
    cells, unreadable = record["cells"], record["unreadable_s"]
    said = (
        []
        if not cells
        else [
            f"covered {min(c[0] for c in cells)}-{max(c[0] for c in cells) + MHZ_BIN} MHz"
            f" at {min(c[1] for c in cells)}-{max(c[1] for c in cells) + TEMP_BIN} C"
            f" for {sum(c[2] for c in cells):.1f} s"
        ]
    )
    if unreadable:
        said.append(f"{unreadable:.1f} s unreadable")
    return "  " + ", ".join(said) if said else ""


class HistoryTab(QWidget):
    """Tab showing historical test runs grouped by tuning context."""

    load_profile_requested = Signal(object, arguments=["offsets_by_core"])

    VIEW_GROUPED = "grouped"
    VIEW_ALL = "all"
    VIEW_TUNER = "tuner"

    def __init__(self, db: HistoryDB | None = None) -> None:
        super().__init__()
        self._db = db
        self._runs: list[RunRecord] = []
        self._contexts: list[TuningContextRecord] = []
        self._context_runs: dict[int | None, list[RunRecord]] = {}
        self._view_mode = self.VIEW_GROUPED
        self._bios_warning: str = ""
        self._displayed_runs: list[RunRecord] = []
        self._tuner_sessions: list[MachineSessionRecord] = []
        self._selected_tuner_session: MachineSessionRecord | None = None
        self._initial_load = True
        self._setup_ui()
        if db:
            self.refresh()

    def set_bios_warning(self, warning: str) -> None:
        """Show the BIOS version warning in the summary bar."""
        self._bios_warning = warning
        self._bios_label.setText(self._bios_warning)
        self._bios_label.setVisible(True)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        summary_group = QGroupBox("")
        summary_rows = QVBoxLayout(summary_group)
        counts_row = QHBoxLayout()
        controls_row = QHBoxLayout()
        self._bios_label = QLabel("")
        summary_rows.addLayout(counts_row)
        summary_rows.addWidget(self._bios_label)
        summary_rows.addLayout(controls_row)

        self._total_label = QLabel(
            "Stress test runs: 0" if self._db else "History is off: no history database is open, so nothing is shown"
        )
        self._counts_label = QLabel("")
        self._counts_label.setTextFormat(Qt.TextFormat.RichText)
        self._counts_label.setStyleSheet("font-weight: bold;")

        for w in (self._total_label, self._counts_label):
            w.setFont(QFont("monospace", 10))
            counts_row.addWidget(w)

        self._bios_label.setFont(font_mono(9))
        self._bios_label.setStyleSheet(f"color: {theme.COLOR_WARN}; font-weight: bold;")
        self._bios_label.setVisible(False)
        counts_row.addStretch()

        controls_row.addWidget(QLabel("Show"))
        self._view_select = QComboBox()
        for mode, label in VIEW_LABELS.items():
            self._view_select.addItem(label, mode)
        self._view_select.currentIndexChanged.connect(self._on_view_selected)
        controls_row.addWidget(self._view_select)
        controls_row.addStretch()

        self._delete_btn = QPushButton("Delete Selected")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_selected)
        controls_row.addWidget(self._delete_btn)

        self._compare_btn = QPushButton("Compare Selected")
        self._compare_btn.setEnabled(False)
        self._compare_btn.setToolTip("Select at least 2 test runs to compare results side-by-side")
        self._compare_btn.clicked.connect(self._compare_selected)
        controls_row.addWidget(self._compare_btn)

        layout.addWidget(summary_group)

        splitter = QSplitter(Qt.Orientation.Vertical)

        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        self._context_table = QTableWidget()
        self._context_table.setColumnCount(6)
        self._context_table.setHorizontalHeaderLabels(
            ["BIOS", "CO Profile", "PBO Scalar", "Runs", "Best Result", "Notes"]
        )
        ctx_header = self._context_table.horizontalHeader()
        ctx_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        ctx_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        ctx_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        ctx_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        ctx_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self._context_table.setColumnWidth(0, 80)
        self._context_table.setColumnWidth(2, 80)
        self._context_table.setColumnWidth(3, 50)
        self._context_table.setColumnWidth(4, 100)
        self._context_table.setAlternatingRowColors(True)
        self._context_table.verticalHeader().setVisible(False)
        self._context_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._context_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._context_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._context_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._context_table.customContextMenuRequested.connect(self._show_context_table_menu)
        self._context_table.selectionModel().selectionChanged.connect(self._on_context_selected)
        self._context_table.clicked.connect(lambda: self._on_context_selected())
        self._context_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._context_table.setMaximumHeight(200)
        top_layout.addWidget(self._context_table)

        self._runs_table = QTableWidget()
        self._runs_table.setSortingEnabled(True)
        self._runs_table.setColumnCount(8)
        self._runs_table.setHorizontalHeaderLabels(
            ["Date", "Backend", "Mode", "Result", "Duration", "Status", "Cores", "BIOS"]
        )
        header = self._runs_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self._runs_table.setColumnWidth(0, 140)
        self._runs_table.setColumnWidth(1, 70)
        self._runs_table.setColumnWidth(2, 50)
        self._runs_table.setColumnWidth(3, 70)
        self._runs_table.setColumnWidth(4, 70)
        self._runs_table.setColumnWidth(5, 75)
        self._runs_table.setColumnWidth(6, 40)
        self._runs_table.setAlternatingRowColors(True)
        self._runs_table.verticalHeader().setVisible(False)
        self._runs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._runs_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._runs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._runs_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._runs_table.customContextMenuRequested.connect(self._show_context_menu)
        self._runs_table.selectionModel().selectionChanged.connect(self._on_run_selection_changed)
        top_layout.addWidget(self._runs_table)

        splitter.addWidget(top_widget)

        self._detail_widget = QWidget()
        detail_layout = QVBoxLayout(self._detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(2)

        self._detail_info = QLabel("Select a run to view details")
        self._detail_info.setFont(font_mono(9))
        self._detail_info.setStyleSheet(f"color: {theme.COLOR_MUTED}; padding: 2px;")
        detail_layout.addWidget(self._detail_info)

        self._tuner_actions_row = QWidget()
        tuner_actions_layout = QHBoxLayout(self._tuner_actions_row)
        tuner_actions_layout.setContentsMargins(0, 0, 0, 0)
        self._load_co_btn = QPushButton("Load these values into Curve-Tuner")
        self._load_co_btn.setToolTip("Fills the Curve-Tuner tab's Value to set column; nothing is written")
        self._load_co_btn.clicked.connect(self._on_load_co_profile)
        tuner_actions_layout.addWidget(self._load_co_btn)
        tuner_actions_layout.addStretch()
        self._tuner_actions_row.setVisible(False)
        detail_layout.addWidget(self._tuner_actions_row)

        self._core_results_table = QTableWidget()
        self._core_results_table.setColumnCount(9)
        self._core_results_table.setHorizontalHeaderLabels(
            ["Core", "CCD", "Cycle", "Result", "Duration", "Peak MHz", "Max C", "Vcore Range", "Error"]
        )
        cr_header = self._core_results_table.horizontalHeader()
        cr_header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        cr_header.setStretchLastSection(True)
        self._core_results_table.verticalHeader().setVisible(False)
        self._core_results_table.setAlternatingRowColors(True)
        self._core_results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._core_results_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._core_results_table.setMaximumHeight(300)
        detail_layout.addWidget(self._core_results_table)

        self._events_log = QPlainTextEdit()
        self._events_log.setReadOnly(True)
        self._events_log.setPlaceholderText("Select a run or session to view per-core results, events, and logs")
        self._events_log.setFont(font_mono(9))
        self._events_log.setMaximumBlockCount(2000)
        detail_layout.addWidget(self._events_log)

        splitter.addWidget(self._detail_widget)
        self._detail_widget.setVisible(False)

        self._splitter = splitter
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

    @Slot()
    def _on_view_selected(self) -> None:
        self._view_mode = self._view_select.currentData()
        self._apply_view_mode()

    def _update_toggle_labels(self) -> None:
        count = len(self._tuner_sessions)
        label = VIEW_LABELS[self.VIEW_TUNER]
        self._view_select.setItemText(
            self._view_select.findData(self.VIEW_TUNER), f"{label} ({count})" if count else label
        )
        self._view_select.blockSignals(True)
        self._view_select.setCurrentIndex(self._view_select.findData(self._view_mode))
        self._view_select.blockSignals(False)

    def _apply_view_mode(self) -> None:
        self._update_toggle_labels()

        if self._db:
            self._reload_data()
            self._update_summary()

        if self._view_mode == self.VIEW_TUNER:
            self._context_table.setVisible(False)
            self._populate_tuner_sessions()
            if self._tuner_sessions:
                return
        elif self._view_mode == self.VIEW_ALL:
            self._context_table.setVisible(False)
            self._populate_runs_table(self._runs)
        else:
            self._context_table.setVisible(True)
            self._populate_context_table()
            if self._context_table.rowCount() > 0:
                self._context_table.selectRow(0)
                return
            else:
                self._runs_table.setRowCount(0)
        self._clear_detail()

    @Slot()
    def refresh(self) -> None:
        if not self._db:
            return

        if self._initial_load:
            self._initial_load = False
            self._reload_data()
            if self._tuner_sessions:
                self._view_mode = self.VIEW_TUNER
            self._update_summary()
            self._apply_view_mode()
            return

        self._refresh_preserve_context()

    def _refresh_preserve_context(self) -> None:
        """Refresh data but keep the currently selected context and detail view."""
        if not self._db:
            return

        selected_ctx_id = None
        if self._view_mode == self.VIEW_GROUPED:
            ctx_rows = sorted({idx.row() for idx in self._context_table.selectionModel().selectedRows()})
            if ctx_rows and ctx_rows[0] < len(self._contexts):
                selected_ctx_id = self._contexts[ctx_rows[0]].id

        self._reload_data()
        self._update_summary()

        if self._view_mode == self.VIEW_GROUPED and selected_ctx_id is not None:
            self._populate_context_table()
            for i, ctx in enumerate(self._contexts):
                if ctx.id == selected_ctx_id:
                    self._context_table.selectRow(i)
                    # selectRow does not signal when the row is already selected
                    self._on_context_selected()
                    return
        elif self._view_mode == self.VIEW_ALL:
            self._populate_runs_table(self._runs)
            self._clear_detail()
            return
        elif self._view_mode == self.VIEW_TUNER:
            self._populate_tuner_sessions()
            return

        self._apply_view_mode()

    def _reload_data(self) -> None:
        self._runs = self._db.list_runs()
        self._contexts = self._db.list_contexts()

        self._context_runs.clear()
        for run in self._runs:
            self._context_runs.setdefault(run.context_id, []).append(run)

        self._tuner_sessions = self._load_tuner_sessions()

    def _update_summary(self) -> None:
        if self._view_mode == self.VIEW_TUNER:
            driver = running_driver() is not None
            counts = Counter(shown_status(s.status, driver) for s in self._tuner_sessions)
            self._total_label.setText(f"Tuning sessions: {counts.total()}")
        else:
            counts = Counter(self._db.get_status_counts() if self._db else {})
            self._total_label.setText(f"Stress test runs: {counts.total()}")
        self._counts_label.setText(counts_html(counts))
        self._update_toggle_labels()

    def _populate_context_table(self) -> None:
        ungrouped = self._context_runs.get(None, [])
        row_count = len(self._contexts) + (1 if ungrouped else 0)
        self._context_table.setRowCount(row_count)

        bios_changed_set = {
            i
            for i, newer in enumerate(self._contexts)
            if bios_change_judged(last_recorded_bios(self._contexts[i + 1 :]), newer.bios_version)
        }

        row = 0

        for idx, ctx in enumerate(self._contexts):
            runs = self._context_runs.get(ctx.id, [])
            co_summary = _co_summary(ctx.co_offsets_json)
            scalar_str = f"{ctx.pbo_scalar:.1f}" if ctx.pbo_scalar is not None else "-"
            best = _best_result(runs)

            bios_changed = idx in bios_changed_set

            bios_text = ctx.bios_version or "-"
            if bios_changed:
                bios_text += " *"

            items = [
                (bios_text, Qt.AlignmentFlag.AlignCenter),
                (co_summary, Qt.AlignmentFlag.AlignLeft),
                (scalar_str, Qt.AlignmentFlag.AlignCenter),
                (str(len(runs)), Qt.AlignmentFlag.AlignCenter),
                (best, Qt.AlignmentFlag.AlignCenter),
                (ctx.notes or "", Qt.AlignmentFlag.AlignLeft),
            ]

            has_failures = any(r.cores_failed > 0 for r in runs if r.status == "completed")
            all_pass = any(r.cores_failed == 0 and r.status == "completed" for r in runs)
            row_color = (
                theme.COLOR_FAIL
                if has_failures and not all_pass
                else (theme.COLOR_WARN if has_failures else (theme.COLOR_PASS if all_pass else theme.COLOR_MUTED))
            )

            for col, (text, align) in enumerate(items):
                cell = _item(str(text), align)
                if col == 4:
                    cell.setForeground(QColor(row_color))
                if col == 0 and bios_changed:
                    cell.setForeground(QColor(theme.COLOR_WARN))
                    cell.setToolTip("BIOS version changed from previous context")
                self._context_table.setItem(row, col, cell)
            row += 1

        if ungrouped:
            items = [
                ("-", Qt.AlignmentFlag.AlignCenter),
                ("(no context)", Qt.AlignmentFlag.AlignLeft),
                ("-", Qt.AlignmentFlag.AlignCenter),
                (str(len(ungrouped)), Qt.AlignmentFlag.AlignCenter),
                (_best_result(ungrouped), Qt.AlignmentFlag.AlignCenter),
                ("Legacy runs (before context tracking)", Qt.AlignmentFlag.AlignLeft),
            ]
            for col, (text, align) in enumerate(items):
                cell = _item(str(text), align)
                cell.setForeground(QColor(theme.COLOR_MUTED))
                self._context_table.setItem(row, col, cell)

        self._auto_size_context_table()

    def _auto_size_context_table(self) -> None:
        """Set context table max height to fit its content."""
        rc = self._context_table.rowCount()
        if rc == 0:
            self._context_table.setMaximumHeight(0)
            return
        row_h = self._context_table.rowHeight(0)
        if row_h < 10:
            row_h = 30
        header_h = self._context_table.horizontalHeader().height()
        if header_h < 10:
            header_h = 26
        self._context_table.setMaximumHeight(min(header_h + row_h * rc + 6, 200))

    def _auto_size_core_results_table(self) -> None:
        """Set core results table max height to fit its content."""
        rc = self._core_results_table.rowCount()
        if rc == 0:
            self._core_results_table.setMaximumHeight(0)
            return
        row_h = self._core_results_table.rowHeight(0)
        if row_h < 10:
            row_h = 30
        header_h = self._core_results_table.horizontalHeader().height()
        if header_h < 10:
            header_h = 26
        self._core_results_table.setMaximumHeight(min(header_h + row_h * rc + 6, 300))

    @Slot()
    def _on_context_selected(self) -> None:
        rows = sorted({idx.row() for idx in self._context_table.selectionModel().selectedRows()})
        if not rows:
            self._runs_table.setRowCount(0)
            return

        row = rows[0]
        if row < len(self._contexts):
            ctx = self._contexts[row]
            runs = self._context_runs.get(ctx.id, [])
        else:
            runs = self._context_runs.get(None, [])

        self._populate_runs_table(runs)
        self._clear_detail()

    @Slot()
    def _show_context_table_menu(self, pos) -> None:
        rows = sorted({idx.row() for idx in self._context_table.selectionModel().selectedRows()})
        if not rows or rows[0] >= len(self._contexts):
            return

        ctx = self._contexts[rows[0]]
        menu = QMenu(self)
        menu.addAction("Add Note...", lambda: self._add_context_note(ctx))
        menu.exec(self._context_table.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _add_context_note(self, ctx: TuningContextRecord) -> None:
        if not self._db or ctx.id is None:
            return
        text, ok = QInputDialog.getText(self, "Context Note", "Note:", text=ctx.notes or "")
        if ok:
            self._db.update_context_notes(ctx.id, text)
            self._refresh_preserve_context()

    def _populate_runs_table(self, runs: list[RunRecord]) -> None:
        self._displayed_runs = runs
        self._runs_table.setSortingEnabled(False)

        self._runs_table.setColumnCount(8)
        self._runs_table.setHorizontalHeaderLabels(
            ["Date", "Backend", "Mode", "Result", "Duration", "Status", "Cores", "BIOS"]
        )
        header = self._runs_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        self._runs_table.setColumnWidth(0, 140)
        self._runs_table.setColumnWidth(1, 70)
        self._runs_table.setColumnWidth(2, 50)
        self._runs_table.setColumnWidth(3, 70)
        self._runs_table.setColumnWidth(4, 70)
        self._runs_table.setColumnWidth(5, 75)
        self._runs_table.setColumnWidth(6, 40)

        self._runs_table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            date_str = format_local(run.started_at)
            result_str = f"{run.cores_passed}P/{run.cores_failed}F" if run.status == "completed" else ""
            dur_str = duration_str(run.total_seconds) if run.total_seconds > 0 else ""
            cores_str = str(run.total_cores) if run.total_cores else ""
            bios_str = run.bios_version if hasattr(run, "bios_version") and run.bios_version else ""

            items = [
                (date_str, Qt.AlignmentFlag.AlignLeft),
                (run.backend, Qt.AlignmentFlag.AlignCenter),
                (run.stress_mode, Qt.AlignmentFlag.AlignCenter),
                (result_str, Qt.AlignmentFlag.AlignCenter),
                (dur_str, Qt.AlignmentFlag.AlignCenter),
                (run.status.capitalize(), Qt.AlignmentFlag.AlignCenter),
                (cores_str, Qt.AlignmentFlag.AlignCenter),
                (bios_str, Qt.AlignmentFlag.AlignCenter),
            ]

            status_color = theme.STATUS_COLORS.get(run.status, theme.COLOR_MUTED)

            for col, (text, align) in enumerate(items):
                item = _item(str(text), align)
                if col == 5:
                    item.setForeground(QColor(status_color))
                elif run.cores_failed > 0 and run.status == "completed":
                    item.setForeground(QColor(theme.COLOR_FAIL))
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                self._runs_table.setItem(row, col, item)

        self._runs_table.setSortingEnabled(True)

    @Slot()
    def _on_run_selection_changed(self) -> None:
        rows = self._selected_run_rows()

        if self._view_mode == self.VIEW_TUNER:
            self._delete_btn.setEnabled(len(rows) >= 1)
            self._compare_btn.setEnabled(False)
            if len(rows) == 1 and rows[0] < len(self._tuner_sessions):
                self._show_tuner_session_detail(self._tuner_sessions[rows[0]])
            else:
                self._selected_tuner_session = None
                self._clear_detail()
            return

        self._delete_btn.setEnabled(len(rows) >= 1)
        self._compare_btn.setEnabled(len(rows) >= 2)

        displayed = getattr(self, "_displayed_runs", self._runs)
        if len(rows) == 1 and rows[0] < len(displayed):
            self._show_run_detail(displayed[rows[0]])
        elif len(rows) == 0:
            self._clear_detail()

    def _selected_run_rows(self) -> list[int]:
        indices: set[int] = set()
        for idx in self._runs_table.selectionModel().selectedRows():
            item = self._runs_table.item(idx.row(), 0)
            data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            indices.add(int(data) if data is not None else idx.row())
        return sorted(indices)

    def _show_run_detail(self, run: RunRecord) -> None:
        if not self._db or run.id is None:
            return

        self._selected_tuner_session = None
        self._tuner_actions_row.setVisible(False)
        self._expand_detail()

        info_parts = [
            run.cpu_model,
            f"{run.backend} / {run.stress_mode} / {run.fft_preset}",
            f"{run.seconds_per_core}s/core",
            f"{run.cycle_count} cycle(s)",
        ]
        if run.bios_version:
            info_parts.append(f"BIOS {run.bios_version}")
        if run.variable_load:
            info_parts.append("variable-load")
        if run.idle_stability_test > 0:
            info_parts.append(f"idle-test={run.idle_stability_test:.0f}s")
        self._detail_info.setText("  |  ".join(info_parts))
        self._detail_info.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM}; padding: 2px;")

        results = self._db.get_core_results(run.id)
        self._core_results_table.setColumnCount(9)
        self._core_results_table.setHorizontalHeaderLabels(
            ["Core", "CCD", "Cycle", "Result", "Duration", "Peak MHz", "Max C", "Vcore Range", "Error"]
        )
        self._core_results_table.setRowCount(len(results))
        for row, r in enumerate(results):
            vcore_str = ""
            if r.min_vcore_v is not None and r.max_vcore_v is not None:
                vcore_str = f"{r.min_vcore_v:.4f}-{r.max_vcore_v:.4f}V"

            unjudged = "..." if run.status == "running" else "no verdict"
            result_text = "PASS" if r.passed else ("FAIL" if r.passed is not None else unjudged)
            color_map = {"PASS": theme.COLOR_PASS, "FAIL": theme.COLOR_FAIL, "...": theme.COLOR_ACTIVE}
            result_color = color_map.get(result_text, theme.COLOR_MUTED)

            row_items = [
                (str(r.core_id), Qt.AlignmentFlag.AlignCenter),
                (str(r.ccd) if r.ccd is not None else "-", Qt.AlignmentFlag.AlignCenter),
                (str(r.cycle + 1), Qt.AlignmentFlag.AlignCenter),
                (result_text, Qt.AlignmentFlag.AlignCenter),
                (duration_str(r.elapsed_seconds), Qt.AlignmentFlag.AlignCenter),
                (f"{r.peak_freq_mhz:.0f}" if r.peak_freq_mhz else "-", Qt.AlignmentFlag.AlignCenter),
                (f"{r.max_temp_c:.1f}" if r.max_temp_c else "-", Qt.AlignmentFlag.AlignCenter),
                (vcore_str or "-", Qt.AlignmentFlag.AlignCenter),
                (r.error_message or "-", Qt.AlignmentFlag.AlignLeft),
            ]
            for col, (text, align) in enumerate(row_items):
                cell = _item(text, align)
                if col == 3:
                    cell.setForeground(QColor(result_color))
                elif r.passed is False:
                    cell.setForeground(QColor(theme.COLOR_FAIL))
                if col == 8 and r.error_message:
                    cell.setToolTip(r.error_message)
                self._core_results_table.setItem(row, col, cell)

        self._auto_size_core_results_table()

        lines: list[str] = []

        if run.context_id and self._db:
            ctx = self._db.get_context(run.context_id)
            if ctx:
                lines.append("── Tuning Context ──")
                if ctx.bios_version:
                    lines.append(f"  BIOS: {ctx.bios_version}")
                co_summary = _co_summary(ctx.co_offsets_json)
                if co_summary != "none":
                    lines.append(f"  CO:   {co_summary}")
                    offsets = _co_offsets(ctx.co_offsets_json)
                    if offsets:
                        try:
                            ordered = sorted(offsets, key=int)
                        except ValueError:
                            ordered = sorted(offsets, key=str)
                        for core_id in ordered:
                            lines.append(f"         Core {core_id}: {offsets[core_id]}")
                if ctx.pbo_scalar is not None:
                    lines.append(f"  PBO Scalar: {ctx.pbo_scalar:.1f}")
                if ctx.boost_limit_mhz is not None:
                    lines.append(f"  Boost Limit: {ctx.boost_limit_mhz} MHz")
                if ctx.notes:
                    lines.append(f"  Notes: {ctx.notes}")
                lines.append("")

        events = self._db.get_events(run.id)
        if events:
            lines.append("── Events ──")
            for e in events:
                ts = format_local(e.timestamp)
                core_str = f" [core {e.core_id}]" if e.core_id is not None else ""
                lines.append(f"{ts}{core_str} [{e.event_type}] {e.message}")

        samples = self._db.get_telemetry(run.id)
        if samples:
            if lines:
                lines.append("")
            lines.append("── Telemetry Summary ──")
            lines.append(f"Total samples: {len(samples)}")

            per_core: dict[int, list] = {}
            for s in samples:
                per_core.setdefault(s.core_id, []).append(s)
            for cid in sorted(per_core):
                core_samples = per_core[cid]
                freqs = [s.freq_mhz for s in core_samples if s.freq_mhz]
                eff_maxes = [s.effective_max_mhz for s in core_samples if s.effective_max_mhz]
                stretches = [
                    pct
                    for s in core_samples
                    if (pct := stretch_pct(s.pm_clock_mhz, s.pm_effective_mhz, s.pm_c0_pct)) is not None
                ]
                temps = [s.temp_c for s in core_samples if s.temp_c]
                vcores = [s.vcore_v for s in core_samples if s.vcore_v]
                parts = [f"  Core {cid}: {len(core_samples)} samples"]
                if freqs:
                    parts.append(f"    Freq: {min(freqs):.0f}-{max(freqs):.0f} MHz")
                if eff_maxes:
                    parts.append(f"    Boost ceiling: {max(eff_maxes):.0f} MHz")
                if stretches:
                    peak, middle, loaded = max(stretches), statistics.median(stretches), len(stretches)
                    parts.append(
                        f"    Clock stretch: peak {peak:.1f}% (median {middle:.1f}%) over {loaded} loaded samples"
                    )
                if temps:
                    parts.append(f"    Temp: {min(temps):.1f}-{max(temps):.1f} C")
                if vcores:
                    parts.append(f"    Vcore: {min(vcores):.4f}-{max(vcores):.4f}V")
                lines.append("\n".join(parts))

        if not lines:
            lines.append("No events or telemetry recorded.")

        lines.append("")
        lines.append("── Settings Snapshot ──")
        try:
            settings = json.loads(run.settings_json)
            lines.append(json.dumps(settings, indent=2))
        except json.JSONDecodeError, TypeError:
            lines.append(run.settings_json)

        self._events_log.setPlainText("\n".join(lines))

    def _load_tuner_sessions(self) -> list[MachineSessionRecord]:
        if not self._db:
            return []
        return self._db.list_machine_sessions()

    def _progress(self, sess: MachineSessionRecord) -> tuple[str, str, str | None, bool]:
        stored, unread = read_stored(self._db, sess.id)
        if stored is None:
            return "?", "unreadable", unread, False  # equivalent-mutant: never read for an unreadable row
        cores = stored.cores
        proven = sum(1 for record in cores if record.phase is Phase.PROVEN)
        return str(len(cores)), f"{proven}/{len(cores)}", None, bool(cores) and proven == len(cores)

    def _populate_tuner_sessions(self) -> None:
        sessions = self._tuner_sessions
        self._runs_table.setSortingEnabled(False)
        self._runs_table.setColumnCount(7)
        self._runs_table.setHorizontalHeaderLabels(["Date", "Status", "CPU", "Cores", "Proven", "Duration", "BIOS"])
        header = self._runs_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        self._runs_table.setColumnWidth(0, 150)
        self._runs_table.setColumnWidth(1, 90)
        self._runs_table.setColumnWidth(3, 60)
        self._runs_table.setColumnWidth(4, 80)
        self._runs_table.setColumnWidth(5, 100)

        self._runs_table.setRowCount(len(sessions))
        driver = running_driver() is not None
        for row, sess in enumerate(sessions):
            if not self._db:
                continue

            cores_text, confirmed_text, unreadable, all_confirmed = self._progress(sess)
            date_str = format_local(sess.created_at)
            shown = shown_status(sess.status, driver)

            items = [
                (date_str, Qt.AlignmentFlag.AlignLeft),
                (SESSION_STATUS_LABELS.get(shown, shown), Qt.AlignmentFlag.AlignCenter),
                (sess.cpu_model or "-", Qt.AlignmentFlag.AlignLeft),
                (cores_text, Qt.AlignmentFlag.AlignCenter),
                (confirmed_text, Qt.AlignmentFlag.AlignCenter),
                (span_str(sess.created_at, sess.updated_at), Qt.AlignmentFlag.AlignCenter),
                (sess.bios_version or "-", Qt.AlignmentFlag.AlignCenter),
            ]

            status_color = theme.STATUS_COLORS.get(shown, theme.COLOR_MUTED)
            for col, (text, align) in enumerate(items):
                cell = _item(str(text), align)
                if col == 1:
                    cell.setForeground(QColor(status_color))
                elif col == 4 and unreadable is not None:
                    cell.setForeground(QColor(theme.COLOR_FAIL))
                    cell.setToolTip(unreadable)
                elif col == 4 and all_confirmed:
                    cell.setForeground(QColor(theme.COLOR_PASS))
                if col == 0:
                    cell.setData(Qt.ItemDataRole.UserRole, row)
                self._runs_table.setItem(row, col, cell)

        if sessions:
            self._runs_table.selectRow(0)
            # selectRow does not signal when the row is already selected
            self._show_tuner_session_detail(sessions[0])

        self._runs_table.setSortingEnabled(True)

    def _show_tuner_session_detail(self, sess: MachineSessionRecord) -> None:
        if not self._db:
            return
        self._selected_tuner_session = sess
        self._tuner_actions_row.setVisible(True)
        self._expand_detail()
        saved, unread = read_stored(self._db, sess.id)
        if saved is None:
            self._load_co_btn.setEnabled(False)
            self._detail_info.setText(unread)
            self._detail_info.setStyleSheet(f"color: {theme.COLOR_FAIL}; padding: 2px;")
            self._core_results_table.setRowCount(0)
            self._events_log.setPlainText(unread)
            return
        self._load_co_btn.setEnabled(sess.status == "completed")
        cfg = saved.config
        info = [
            sess.cpu_model or "Unknown CPU",
            f"{ORDER_LABELS[cfg.method]}, deepest offset {cfg.max_offset}, {cfg.k_1t} and {cfg.k_2t} passes to prove",
        ]
        if sess.parent_session_id is not None:
            info.append(f"{ORIGINS.get(sess.origin, sess.origin)} session {sess.parent_session_id}")
        if sess.bios_version:
            info.append(f"BIOS {sess.bios_version}")
        info.append("power at start: " + (power_text(saved.power) or "none recorded"))
        self._detail_info.setText("  |  ".join(info))
        self._detail_info.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM}; padding: 2px;")
        tests = self._db.list_tuner_tests(sess.id)
        boot, driver = kernel.read_boot_id(), running_driver() is not None
        peaks: dict[int, float] = {}
        for found in own_peaks(tests):
            peaks[found["core"]] = max(found["mhz"], peaks.get(found["core"], found["mhz"]))
        per_core: dict[int, list[dict]] = {}
        for test in tests:
            if test["stage"] in ONE_CORE_STAGES:
                per_core.setdefault(test["core"], []).append(test)
        headers = ["Core", "Phase", "Value", "Failed at", "Tests", "Last result", "Peak MHz"]
        self._core_results_table.setColumnCount(len(headers))
        self._core_results_table.setHorizontalHeaderLabels(headers)
        self._core_results_table.setRowCount(len(saved.cores))
        for row, record in enumerate(saved.cores):
            own = per_core.get(record.core, [])
            last = next((verdict_text(test["verdict"]) for test in reversed(own) if test["verdict"]), "-")
            texts = (
                str(record.core),
                tuner_phase_label(record.phase),
                str(record.value),
                "-" if record.fb is None else str(record.fb),
                str(len(own)),
                last,
                f"{peaks[record.core]:.0f}" if record.core in peaks else "-",
            )
            for column, text in enumerate(texts):
                self._core_results_table.setItem(row, column, _item(text, Qt.AlignmentFlag.AlignCenter))
        self._auto_size_core_results_table()
        setup = [f"  {SETUP_LABELS.get(key, key)}: {value}" for key, value in saved.setup]
        single = sum(len(own) for own in per_core.values())
        lines = ["-- Settings --", *settings_lines(cfg), "", "-- Setup --", *setup, ""]
        lines.append(f"-- Tests: {single} single-core, {len(tests) - single} all-core or group --")
        lines += [
            f"  {format_local(test['started_at'])}  {tested(test)}: "
            + (
                verdict_text(test["verdict"])
                if test["verdict"]
                else open_test_text(test, test is tests[-1], boot, driver)
            )
            + (f"  {test['detail']}" if test["detail"] else "")
            + (coverage_text(test["coverage_json"], test["core"]) if test["stage"] in ONE_CORE_STAGES else "")
            for test in tests
        ]
        lines += ["", "-- Events --"]
        lines += [
            f"  {format_local(event['timestamp'])}  {event['severity']}: {event['message']}"
            for event in self._db.get_tuner_events(sess.id)
        ]
        lines += ["", "-- Boots --"]
        lines += [
            f"  {boot['boot_id']}  first seen {format_local(boot['first_seen'])}  BIOS {boot['bios_version']}"
            for boot in self._db.session_boots(sess.id)
        ]
        lines += ["", "-- Highest clock per core and range, every session --"]
        lines += [
            f"  core {row['core']}: {row['mhz']:.0f} MHz with the scaling maximum at "
            + ("unknown" if row["cap_mhz"] is None else f"{row['cap_mhz']:.0f} MHz")
            + f", boost {row['boost'] if row['boost'] is not None else 'unknown'}"
            for row in own_peaks(self._db.list_every_tuner_test())
        ]
        self._events_log.setPlainText("\n".join(lines))

    @Slot()
    def _on_load_co_profile(self) -> None:
        """Load the selected tuner session's best CO offsets into the Curve-Tuner tab."""
        if not self._db or self._selected_tuner_session is None:
            return
        session_id = self._selected_tuner_session.id
        saved, unread = read_stored(self._db, session_id)
        if saved is None:
            self._load_co_btn.setEnabled(False)
            self._detail_info.setText(unread)
            self._detail_info.setStyleSheet(f"color: {theme.COLOR_FAIL}; padding: 2px;")
            return
        self.load_profile_requested.emit({record.core: record.value for record in saved.cores})

    def _expand_detail(self) -> None:
        """Show the detail section and split space evenly with the top.

        Only forces the 50/50 split when the pane was hidden — re-splitting on
        every selection would stomp the user's manual splitter adjustment.
        """
        if self._detail_widget.isVisible():
            return
        self._detail_widget.setVisible(True)
        total = sum(self._splitter.sizes())
        # before its first layout pass the splitter reports no size
        if total <= 0:
            total = max(self._splitter.height(), self.height(), 400)
        self._splitter.setSizes([total // 2, total - total // 2])

    def _clear_detail(self) -> None:
        self._detail_info.setText("Select a run to view details")
        self._detail_info.setStyleSheet(f"color: {theme.COLOR_MUTED}; padding: 2px;")
        self._core_results_table.setRowCount(0)
        self._events_log.clear()
        self._tuner_actions_row.setVisible(False)
        self._selected_tuner_session = None
        self._detail_widget.setVisible(False)

    @Slot()
    def _show_context_menu(self, pos) -> None:
        if self._view_mode == self.VIEW_TUNER:
            return
        rows = self._selected_run_rows()
        if not rows:
            return

        menu = QMenu(self)
        if len(rows) == 1:
            menu.addAction("Export JSON...", lambda: self._export_json(rows[0]))
            menu.addAction("Export CSV...", lambda: self._export_csv(rows[0]))
            menu.addSeparator()
        elif len(rows) > 1:
            menu.addAction("Compare", self._compare_selected)
            menu.addAction("Export All as CSV...", lambda: self._export_bulk_csv(rows))
            menu.addSeparator()

        menu.addAction("Delete", lambda: self._delete_runs(rows))
        menu.exec(self._runs_table.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def _export_json(self, row: int) -> None:
        from corecycler.history.export import export_run_json_file

        displayed = getattr(self, "_displayed_runs", self._runs)
        if row >= len(displayed):
            return
        run = displayed[row]
        if not self._db or run.id is None:
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export JSON", f"run_{run.id}.json", "JSON (*.json)")
        if path:
            dlg = _ExportOptionsDialog(self)
            accepted = dlg.exec() == QDialog.DialogCode.Accepted
            events, telemetry = dlg.include_events, dlg.include_telemetry
            dlg.deleteLater()
            if accepted:
                export_run_json_file(
                    self._db,
                    run.id,
                    Path(path),
                    include_events=events,
                    include_telemetry=telemetry,
                )

    def _export_csv(self, row: int) -> None:
        from corecycler.history.export import export_run_csv_file

        displayed = getattr(self, "_displayed_runs", self._runs)
        if row >= len(displayed):
            return
        run = displayed[row]
        if not self._db or run.id is None:
            return

        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", f"run_{run.id}.csv", "CSV (*.csv)")
        if path:
            export_run_csv_file(self._db, run.id, Path(path))

    def _export_bulk_csv(self, rows: list[int]) -> None:
        from corecycler.history.export import export_runs_bulk_csv_file

        if not self._db:
            return

        displayed = getattr(self, "_displayed_runs", self._runs)
        run_ids = [displayed[r].id for r in rows if r < len(displayed) and displayed[r].id is not None]
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", "runs_comparison.csv", "CSV (*.csv)")
        if path:
            export_runs_bulk_csv_file(self._db, run_ids, Path(path))

    @Slot()
    def _delete_selected(self) -> None:
        if self._view_mode == self.VIEW_TUNER:
            rows = self._selected_run_rows()
            if rows:
                self._delete_tuner_sessions(rows)
            return

        if self._view_mode == self.VIEW_GROUPED:
            ctx_rows = sorted({idx.row() for idx in self._context_table.selectionModel().selectedRows()})
            if ctx_rows and not self._selected_run_rows():
                self._delete_contexts(ctx_rows)
                return

        rows = self._selected_run_rows()
        if rows:
            self._delete_runs(rows)

    def _delete_runs(self, rows: list[int]) -> None:
        if not self._db:
            return

        displayed = getattr(self, "_displayed_runs", self._runs)
        run_ids = []
        for row in rows:
            if row < len(displayed) and displayed[row].id is not None:
                run_ids.append(displayed[row].id)

        if not run_ids:
            return

        reply = QMessageBox.question(
            self,
            "Delete Runs",
            f"Delete {len(run_ids)} run(s) and all associated data?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for run_id in run_ids:
            self._db.delete_run(run_id)

        if self._db:
            self._db.delete_orphaned_contexts()

        self._refresh_preserve_context()

    def _delete_contexts(self, rows: list[int]) -> None:
        if not self._db:
            return
        ctx_ids = []
        for row in rows:
            if row < len(self._contexts):
                ctx_id = self._contexts[row].id
                if ctx_id is not None:
                    ctx_ids.append(ctx_id)
        if not ctx_ids:
            return
        held = [f"setup {cid} holds tuner sessions {ids}" for cid in ctx_ids if (ids := self._db.context_sessions(cid))]
        if held:
            QMessageBox.warning(
                self,
                "Setup Holds Tuner Sessions",
                "; ".join(held) + ": delete those under Tuning sessions first.",
            )
            return

        total_runs = sum(len(self._context_runs.get(cid, [])) for cid in ctx_ids)

        reply = QMessageBox.question(
            self,
            "Delete Contexts",
            f"This will permanently delete {len(ctx_ids)} context(s) and ALL "
            f"{total_runs} associated test run(s). This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for ctx_id in ctx_ids:
            self._db.delete_context_cascade(ctx_id)

        self._refresh_preserve_context()

    def _delete_tuner_sessions(self, rows: list[int]) -> None:
        if not self._db:
            return

        session_ids = []
        active = []
        driver = running_driver() is not None
        chosen = {self._tuner_sessions[row].id for row in rows if row < len(self._tuner_sessions)}
        parents = {
            sess.parent_session_id: sess.id
            for sess in self._tuner_sessions
            if sess.parent_session_id is not None and sess.id not in chosen
        }
        for row in rows:
            if row < len(self._tuner_sessions):
                sess = self._tuner_sessions[row]
                if sess.status == "running" and driver:
                    active.append(sess.id)
                elif sess.id in parents:
                    QMessageBox.warning(
                        self,
                        "Session Has a Successor",
                        f"Session {sess.id} carries on as session {parents[sess.id]}: delete that one first.",
                    )
                else:
                    session_ids.append(sess.id)

        if active:
            QMessageBox.warning(
                self,
                "Session Active",
                f"Session(s) {active} are mid-run: stop them in the Auto-Tuner tab before deleting.",
            )
        if not session_ids:
            return

        reply = QMessageBox.question(
            self,
            "Delete Tuner Sessions",
            f"Delete {len(session_ids)} tuner session(s) and all associated data?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for sid in session_ids:
            self._db.delete_tuner_session(sid)

        self._db.delete_orphaned_contexts()
        self._refresh_preserve_context()

    @Slot()
    def _compare_selected(self) -> None:
        rows = self._selected_run_rows()
        if len(rows) < 2 or not self._db:
            return

        displayed = getattr(self, "_displayed_runs", self._runs)
        run_data: list[tuple[RunRecord, list]] = []
        all_cores: set[int] = set()
        for row in rows:
            if row >= len(displayed):
                continue
            run = displayed[row]
            if run.id is not None:
                results = self._db.get_core_results(run.id)
                run_data.append((run, results))
                for r in results:
                    all_cores.add(r.core_id)

        if not run_data:
            return

        sorted_cores = sorted(all_cores)

        self._detail_info.setText(f"Comparing {len(run_data)} runs across {len(sorted_cores)} cores")
        self._detail_info.setStyleSheet(f"color: {theme.COLOR_ACTIVE}; padding: 2px;")

        col_headers = ["Core"]
        for run, _ in run_data:
            label = format_local(run.started_at, date_only=True) if run.started_at else f"Run {run.id}"
            col_headers.extend([f"{label} Result", f"{label} Duration"])

        self._core_results_table.setColumnCount(len(col_headers))
        self._core_results_table.setHorizontalHeaderLabels(col_headers)
        cr_header = self._core_results_table.horizontalHeader()
        cr_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        cr_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._core_results_table.setColumnWidth(0, 50)

        self._core_results_table.setRowCount(len(sorted_cores))

        for row_idx, core_id in enumerate(sorted_cores):
            self._core_results_table.setItem(row_idx, 0, _item(str(core_id), Qt.AlignmentFlag.AlignCenter))

            for run_idx, (_run, results) in enumerate(run_data):
                core_result = next((r for r in results if r.core_id == core_id), None)
                col_base = 1 + run_idx * 2

                if core_result:
                    result_text = (
                        "PASS" if core_result.passed else ("FAIL" if core_result.passed is not None else "...")
                    )
                    result_item = _item(result_text, Qt.AlignmentFlag.AlignCenter)
                    color = (
                        theme.COLOR_PASS
                        if core_result.passed
                        else (theme.COLOR_FAIL if core_result.passed is not None else theme.COLOR_ACTIVE)
                    )
                    result_item.setForeground(QColor(color))
                    dur_item = _item(duration_str(core_result.elapsed_seconds), Qt.AlignmentFlag.AlignCenter)
                else:
                    result_item = _item("-", Qt.AlignmentFlag.AlignCenter)
                    dur_item = _item("-", Qt.AlignmentFlag.AlignCenter)

                self._core_results_table.setItem(row_idx, col_base, result_item)
                self._core_results_table.setItem(row_idx, col_base + 1, dur_item)

        lines = ["── Run Comparison ──"]
        for run, results in run_data:
            label = format_local(run.started_at) if run.started_at else f"Run {run.id}"
            passed = sum(1 for r in results if r.passed)
            failed = sum(1 for r in results if r.passed is False)
            lines.append(
                f"  {label}:  {run.backend}/{run.stress_mode}  {passed}P/{failed}F  {duration_str(run.total_seconds)}"
            )
        self._events_log.setPlainText("\n".join(lines))


class _ExportOptionsDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Options")
        layout = QVBoxLayout(self)

        self._events_cb = QCheckBox("Include events log")
        self._events_cb.setChecked(True)
        layout.addWidget(self._events_cb)

        self._telemetry_cb = QCheckBox("Include telemetry samples")
        self._telemetry_cb.setChecked(False)
        layout.addWidget(self._telemetry_cb)

        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("Export")
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    @property
    def include_events(self) -> bool:
        return self._events_cb.isChecked()

    @property
    def include_telemetry(self) -> bool:
        return self._telemetry_cb.isChecked()


def _co_offsets(co_json: str) -> dict | None:
    """Parse a context's stored CO offsets: {} when none were recorded, None when the value is unreadable.

    The single parse for both the context table and the run detail: a stored
    value that is not a non-empty JSON object (a hand-edited row, a database
    merged from elsewhere) must degrade the display, never raise inside a slot.
    """
    try:
        offsets = json.loads(co_json)
    except json.JSONDecodeError, TypeError:
        return None
    return offsets if isinstance(offsets, dict) else None


def _co_summary(co_json: str) -> str:
    """Summarize CO offsets JSON for display in context table."""
    offsets = _co_offsets(co_json)
    if offsets is None:
        return "unreadable"
    if not offsets:
        return "none"

    values = list(offsets.values())
    if all(v == values[0] for v in values):
        return f"all {values[0]}"

    try:
        return f"mixed [{min(values)}..{max(values)}]"
    except TypeError:
        return "mixed"


def _best_result(runs: list[RunRecord]) -> str:
    """Best pass rate among completed runs."""
    completed = [r for r in runs if r.status == "completed" and r.total_cores > 0]
    if not completed:
        return "-"
    best = max(completed, key=lambda r: r.cores_passed / r.total_cores)
    return f"{best.cores_passed}/{best.total_cores}"
