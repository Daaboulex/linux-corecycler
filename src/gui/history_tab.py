"""History browser tab — browse past runs grouped by tuning context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tuner import persistence as tp
from tuner.state import TunerPhase, TunerSession

if TYPE_CHECKING:
    from history.db import HistoryDB, RunRecord, TuningContextRecord

_COLOR_PASS = "#4caf50"
_COLOR_FAIL = "#f44336"
_COLOR_WARN = "#ffa726"
_COLOR_ACTIVE = "#4fc3f7"
_COLOR_MUTED = "#888"
_FONT_MONO = QFont("monospace", 9)

_STATUS_COLORS = {
    "completed": _COLOR_PASS,
    "crashed": _COLOR_FAIL,
    "stopped": _COLOR_WARN,
    "running": _COLOR_ACTIVE,
    "aborted": _COLOR_WARN,
    "paused": _COLOR_MUTED,
    "validating": _COLOR_ACTIVE,
}


class HistoryTab(QWidget):
    """Tab showing historical test runs grouped by tuning context."""

    load_profile_requested = Signal(object)  # dict[int, int]

    # View modes
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
        self._tuner_sessions: list[TunerSession] = []
        self._selected_tuner_session: TunerSession | None = None
        self._initial_load = True
        self._setup_ui()
        if db:
            self.refresh()

    def set_db(self, db: HistoryDB) -> None:
        self._db = db
        self.refresh()

    def set_bios_warning(self, old_version: str, new_version: str) -> None:
        """Show BIOS change warning in the summary bar."""
        self._bios_warning = f"BIOS changed: {old_version} -> {new_version}"
        self._bios_label.setText(self._bios_warning)
        self._bios_label.setVisible(True)

    # ------------------------------------------------------------------
    # UI setup
    # ------------------------------------------------------------------

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # --- Summary bar ---
        summary_group = QGroupBox("")
        summary_layout = QHBoxLayout(summary_group)

        self._total_label = QLabel("Test Runs: 0")
        self._completed_label = QLabel("Completed: 0")
        self._completed_label.setStyleSheet(f"color: {_COLOR_PASS}; font-weight: bold;")
        self._crashed_label = QLabel("Crashed: 0")
        self._crashed_label.setStyleSheet(f"color: {_COLOR_FAIL}; font-weight: bold;")
        self._stopped_label = QLabel("Stopped: 0")
        self._stopped_label.setStyleSheet(f"color: {_COLOR_WARN}; font-weight: bold;")

        for w in (self._total_label, self._completed_label, self._crashed_label, self._stopped_label):
            w.setFont(QFont("monospace", 10))
            summary_layout.addWidget(w)

        self._bios_label = QLabel("")
        self._bios_label.setFont(_FONT_MONO)
        self._bios_label.setStyleSheet(f"color: {_COLOR_WARN}; font-weight: bold; padding-left: 8px;")
        self._bios_label.setVisible(False)
        summary_layout.addWidget(self._bios_label)

        summary_layout.addStretch()

        self._view_toggle = QPushButton("All Runs")
        self._view_toggle.setCheckable(True)
        self._view_toggle.setToolTip("Toggle between grouped (by tuning context) and flat view")
        self._view_toggle.clicked.connect(self._toggle_view)
        summary_layout.addWidget(self._view_toggle)

        self._tuner_toggle = QPushButton("Tuner Sessions")
        self._tuner_toggle.setCheckable(True)
        self._tuner_toggle.setToolTip("Show auto-tuner session history")
        self._tuner_toggle.clicked.connect(self._toggle_tuner_view)
        summary_layout.addWidget(self._tuner_toggle)

        self._delete_btn = QPushButton("Delete Selected")
        self._delete_btn.setEnabled(False)
        self._delete_btn.clicked.connect(self._delete_selected)
        summary_layout.addWidget(self._delete_btn)

        self._compare_btn = QPushButton("Compare Selected")
        self._compare_btn.setEnabled(False)
        self._compare_btn.setToolTip("Select at least 2 test runs to compare results side-by-side")
        self._compare_btn.clicked.connect(self._compare_selected)
        summary_layout.addWidget(self._compare_btn)

        layout.addWidget(summary_group)

        # --- Two-section vertical splitter: top (context+runs) / bottom (detail+log) ---
        splitter = QSplitter(Qt.Orientation.Vertical)

        # === Top section: context table + runs table ===
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)

        # Context table (hidden in "All Runs" mode, auto-sized to rows)
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

        # Runs table (stretches to fill remaining top space)
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
        self._runs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._runs_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._runs_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._runs_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._runs_table.customContextMenuRequested.connect(self._show_context_menu)
        self._runs_table.selectionModel().selectionChanged.connect(self._on_run_selection_changed)
        top_layout.addWidget(self._runs_table)

        splitter.addWidget(top_widget)

        # === Bottom section: detail info + core results + events log ===
        self._detail_widget = QWidget()
        detail_layout = QVBoxLayout(self._detail_widget)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(2)

        self._detail_info = QLabel("Select a run to view details")
        self._detail_info.setFont(_FONT_MONO)
        self._detail_info.setStyleSheet(f"color: {_COLOR_MUTED}; padding: 2px;")
        detail_layout.addWidget(self._detail_info)

        # Tuner session action buttons (hidden by default, shown for tuner sessions)
        self._tuner_actions_row = QWidget()
        tuner_actions_layout = QHBoxLayout(self._tuner_actions_row)
        tuner_actions_layout.setContentsMargins(0, 0, 0, 0)
        self._load_co_btn = QPushButton("Load to CO Tab")
        self._load_co_btn.setToolTip("Load confirmed CO offsets into the Curve Optimizer tab")
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
        cr_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        cr_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        cr_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        cr_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        cr_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        cr_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        cr_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        cr_header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)
        cr_header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)
        self._core_results_table.setColumnWidth(0, 50)
        self._core_results_table.setColumnWidth(1, 40)
        self._core_results_table.setColumnWidth(2, 50)
        self._core_results_table.setColumnWidth(3, 60)
        self._core_results_table.setColumnWidth(4, 80)
        self._core_results_table.setColumnWidth(5, 80)
        self._core_results_table.setColumnWidth(6, 60)
        self._core_results_table.setColumnWidth(7, 120)
        self._core_results_table.setAlternatingRowColors(True)
        self._core_results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._core_results_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._core_results_table.setMaximumHeight(300)
        detail_layout.addWidget(self._core_results_table)

        # Events log (stretches to fill remaining bottom space)
        self._events_log = QPlainTextEdit()
        self._events_log.setReadOnly(True)
        self._events_log.setPlaceholderText("Select a run or session to view per-core results, events, and logs")
        self._events_log.setFont(_FONT_MONO)
        self._events_log.setMaximumBlockCount(2000)
        detail_layout.addWidget(self._events_log)

        splitter.addWidget(self._detail_widget)
        self._detail_widget.setVisible(False)

        self._splitter = splitter
        splitter.setChildrenCollapsible(False)
        layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # View toggle
    # ------------------------------------------------------------------

    @Slot()
    def _toggle_view(self) -> None:
        if self._view_toggle.isChecked():
            self._view_mode = self.VIEW_ALL
            self._tuner_toggle.setChecked(False)
        else:
            self._view_mode = self.VIEW_GROUPED
        self._apply_view_mode()

    @Slot()
    def _toggle_tuner_view(self) -> None:
        if self._tuner_toggle.isChecked():
            self._view_mode = self.VIEW_TUNER
            self._view_toggle.setChecked(False)
        else:
            self._view_mode = self.VIEW_GROUPED
        self._apply_view_mode()

    def _update_toggle_labels(self) -> None:
        """Update toggle button labels to reflect current view mode.

        Called after every view mode change and after summary updates
        so the labels stay consistent.
        """
        tuner_count = len(self._tuner_sessions)
        count_suffix = f" ({tuner_count})" if tuner_count > 0 else ""

        if self._view_mode == self.VIEW_ALL:
            self._view_toggle.setText("All Runs ✓")
            self._tuner_toggle.setText(f"Tuner Sessions{count_suffix}")
        elif self._view_mode == self.VIEW_TUNER:
            self._view_toggle.setText("All Runs")
            self._tuner_toggle.setText(f"Tuner Sessions{count_suffix} ✓")
        else:
            self._view_toggle.setText("All Runs")
            self._tuner_toggle.setText(f"Tuner Sessions{count_suffix}")

    def _apply_view_mode(self) -> None:
        self._update_toggle_labels()

        # Reload data to ensure we have fresh content for the new view
        if self._db:
            self._reload_data()
            self._update_summary()

        if self._view_mode == self.VIEW_TUNER:
            self._context_table.setVisible(False)
            self._populate_tuner_sessions()
        elif self._view_mode == self.VIEW_ALL:
            self._context_table.setVisible(False)
            self._populate_runs_table(self._runs)
        else:
            self._context_table.setVisible(True)
            self._populate_context_table()
            # Auto-select first context so runs are always visible
            if self._context_table.rowCount() > 0:
                self._context_table.selectRow(0)
                return
            else:
                self._runs_table.setRowCount(0)
        self._clear_detail()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @Slot()
    def refresh(self) -> None:
        if not self._db:
            return

        # On first load, pick the best default view based on available data.
        # Must run BEFORE _refresh_preserve_context so the view mode is set
        # before any table population happens.
        if self._initial_load:
            self._initial_load = False
            self._reload_data()
            if self._tuner_sessions:
                self._view_mode = self.VIEW_TUNER
                self._tuner_toggle.setChecked(True)
                self._view_toggle.setChecked(False)
            self._update_summary()
            self._apply_view_mode()
            return

        self._refresh_preserve_context()

    def _refresh_preserve_context(self) -> None:
        """Refresh data but keep the currently selected context and detail view."""
        if not self._db:
            return

        # remember which context was selected
        selected_ctx_id = None
        if self._view_mode == self.VIEW_GROUPED:
            ctx_rows = sorted(
                {idx.row() for idx in self._context_table.selectionModel().selectedRows()}
            )
            if ctx_rows and ctx_rows[0] < len(self._contexts):
                selected_ctx_id = self._contexts[ctx_rows[0]].id

        self._reload_data()
        self._update_summary()

        if self._view_mode == self.VIEW_GROUPED and selected_ctx_id is not None:
            # Rebuild context table but restore selection without clearing detail
            self._populate_context_table()
            for i, ctx in enumerate(self._contexts):
                if ctx.id == selected_ctx_id:
                    self._context_table.selectRow(i)
                    # Force refresh — selectRow may not fire signal if same row index
                    self._on_context_selected()
                    return
            # Context was deleted (all its runs gone) — fall through to full refresh
        elif self._view_mode == self.VIEW_ALL:
            self._populate_runs_table(self._runs)
            self._clear_detail()
            return
        elif self._view_mode == self.VIEW_TUNER:
            self._populate_tuner_sessions()
            return

        # Fallback: full view mode reset
        self._apply_view_mode()

    def _reload_data(self) -> None:
        # Auto-clean orphaned contexts (0 runs)
        if self._db:
            self._db.delete_orphaned_contexts()

        self._runs = self._db.list_runs(limit=500)
        self._contexts = self._db.list_contexts()

        # Group runs by context_id
        self._context_runs.clear()
        for run in self._runs:
            self._context_runs.setdefault(run.context_id, []).append(run)

        # Load tuner sessions
        self._tuner_sessions = self._load_tuner_sessions()

    def _update_summary(self) -> None:
        counts = self._db.get_status_counts() if self._db else {}
        total = sum(counts.values())
        completed = counts.get("completed", 0)
        crashed = counts.get("crashed", 0)
        stopped = counts.get("stopped", 0)
        self._total_label.setText(f"Runs: {total}")
        self._completed_label.setText(f"Completed: {completed}")
        self._crashed_label.setText(f"Crashed: {crashed}")
        self._stopped_label.setText(f"Stopped: {stopped}")
        # Update toggle labels (incorporates session count + current view mode)
        self._update_toggle_labels()

    # ------------------------------------------------------------------
    # Context table
    # ------------------------------------------------------------------

    def _populate_context_table(self) -> None:
        # Include "Ungrouped" if there are runs without a context
        ungrouped = self._context_runs.get(None, [])
        row_count = len(self._contexts) + (1 if ungrouped else 0)
        self._context_table.setRowCount(row_count)

        # Pre-compute which contexts have a BIOS change from their chronological
        # predecessor.  self._contexts is newest-first, so the chronological
        # predecessor of index i is index i+1.  We mark the *newer* context
        # (the one that introduced the change) with the indicator.
        bios_changed_set: set[int] = set()
        for i in range(len(self._contexts) - 1):
            if self._contexts[i].bios_version != self._contexts[i + 1].bios_version:
                bios_changed_set.add(i)

        row = 0

        for idx, ctx in enumerate(self._contexts):
            runs = self._context_runs.get(ctx.id, [])
            co_summary = _co_summary(ctx.co_offsets_json)
            scalar_str = f"{ctx.pbo_scalar:.1f}" if ctx.pbo_scalar is not None else "-"
            best = _best_result(runs)

            # Detect BIOS change — mark the newer context that introduced the change
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

            # Color based on best pass rate
            has_failures = any(r.cores_failed > 0 for r in runs if r.status == "completed")
            all_pass = any(r.cores_failed == 0 and r.status == "completed" for r in runs)
            row_color = _COLOR_FAIL if has_failures and not all_pass else (
                _COLOR_WARN if has_failures else (_COLOR_PASS if all_pass else _COLOR_MUTED)
            )

            for col, (text, align) in enumerate(items):
                cell = _item(str(text), align)
                if col == 4:
                    cell.setForeground(QColor(row_color))
                if col == 0 and bios_changed:
                    cell.setForeground(QColor(_COLOR_WARN))
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
                cell.setForeground(QColor(_COLOR_MUTED))
                self._context_table.setItem(row, col, cell)

        # Auto-size context table height to fit rows (capped at 200px)
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
        # Cap at 300px — enough for ~10 rows, rest goes to events log
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
            # Ungrouped row
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

    def _add_context_note(self, ctx: TuningContextRecord) -> None:
        if not self._db or ctx.id is None:
            return
        text, ok = QInputDialog.getText(
            self, "Context Note", "Note:", text=ctx.notes or ""
        )
        if ok:
            self._db.update_context_notes(ctx.id, text)
            self._refresh_preserve_context()

    # ------------------------------------------------------------------
    # Runs table
    # ------------------------------------------------------------------

    def _populate_runs_table(self, runs: list[RunRecord]) -> None:
        self._displayed_runs = runs

        # Always reset column headers (tuner sessions view may have changed them)
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
            date_str = run.started_at[:19].replace("T", " ") if run.started_at else ""
            result_str = f"{run.cores_passed}P/{run.cores_failed}F" if run.status == "completed" else ""
            duration_str = _format_duration(run.total_seconds) if run.total_seconds > 0 else ""
            cores_str = str(run.total_cores) if run.total_cores else ""
            bios_str = run.bios_version if hasattr(run, "bios_version") and run.bios_version else ""

            items = [
                (date_str, Qt.AlignmentFlag.AlignLeft),
                (run.backend, Qt.AlignmentFlag.AlignCenter),
                (run.stress_mode, Qt.AlignmentFlag.AlignCenter),
                (result_str, Qt.AlignmentFlag.AlignCenter),
                (duration_str, Qt.AlignmentFlag.AlignCenter),
                (run.status.capitalize(), Qt.AlignmentFlag.AlignCenter),
                (cores_str, Qt.AlignmentFlag.AlignCenter),
                (bios_str, Qt.AlignmentFlag.AlignCenter),
            ]

            status_color = _STATUS_COLORS.get(run.status, _COLOR_MUTED)

            for col, (text, align) in enumerate(items):
                item = _item(str(text), align)
                if col == 5:
                    item.setForeground(QColor(status_color))
                elif run.cores_failed > 0 and run.status == "completed":
                    item.setForeground(QColor(_COLOR_FAIL))
                self._runs_table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Selection & detail
    # ------------------------------------------------------------------

    @Slot()
    def _on_run_selection_changed(self) -> None:
        rows = self._selected_run_rows()

        if self._view_mode == self.VIEW_TUNER:
            # Tuner session selection
            self._delete_btn.setEnabled(len(rows) >= 1)
            self._compare_btn.setEnabled(False)
            if len(rows) == 1 and rows[0] < len(self._tuner_sessions):
                self._show_tuner_session_detail(self._tuner_sessions[rows[0]])
            elif len(rows) == 0:
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
        return sorted({idx.row() for idx in self._runs_table.selectionModel().selectedRows()})

    def _show_run_detail(self, run: RunRecord) -> None:
        if not self._db or run.id is None:
            return

        self._selected_tuner_session = None
        self._tuner_actions_row.setVisible(False)
        self._expand_detail()

        # Info line
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
        self._detail_info.setStyleSheet("color: #aaa; padding: 2px;")

        # Core results
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

            result_text = "PASS" if r.passed else ("FAIL" if r.passed is not None else "...")
            color_map = {"PASS": _COLOR_PASS, "FAIL": _COLOR_FAIL, "...": _COLOR_ACTIVE}
            result_color = color_map.get(result_text, _COLOR_MUTED)

            row_items = [
                (str(r.core_id), Qt.AlignmentFlag.AlignCenter),
                (str(r.ccd) if r.ccd is not None else "-", Qt.AlignmentFlag.AlignCenter),
                (str(r.cycle + 1), Qt.AlignmentFlag.AlignCenter),
                (result_text, Qt.AlignmentFlag.AlignCenter),
                (_format_duration(r.elapsed_seconds), Qt.AlignmentFlag.AlignCenter),
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
                    cell.setForeground(QColor(_COLOR_FAIL))
                # Add tooltip on error column so full message is visible on hover
                if col == 8 and r.error_message:
                    cell.setToolTip(r.error_message)
                self._core_results_table.setItem(row, col, cell)

        self._auto_size_core_results_table()

        # Events log + context info
        lines: list[str] = []

        # Show tuning context info if available
        if run.context_id and self._db:
            ctx = self._db.get_context(run.context_id)
            if ctx:
                lines.append("── Tuning Context ──")
                if ctx.bios_version:
                    lines.append(f"  BIOS: {ctx.bios_version}")
                co_summary = _co_summary(ctx.co_offsets_json)
                if co_summary and co_summary != "none":
                    lines.append(f"  CO:   {co_summary}")
                    try:
                        offsets = json.loads(ctx.co_offsets_json)
                        if offsets:
                            for core_id in sorted(offsets, key=int):
                                lines.append(f"         Core {core_id}: {offsets[core_id]}")
                    except (json.JSONDecodeError, TypeError):
                        pass
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
                ts = e.timestamp[:19].replace("T", " ") if e.timestamp else ""
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
                temps = [s.temp_c for s in core_samples if s.temp_c]
                vcores = [s.vcore_v for s in core_samples if s.vcore_v]
                parts = [f"  Core {cid}: {len(core_samples)} samples"]
                if freqs:
                    parts.append(f"    Freq: {min(freqs):.0f}-{max(freqs):.0f} MHz")
                if eff_maxes:
                    eff_max = max(eff_maxes)
                    parts.append(f"    Boost ceiling: {eff_max:.0f} MHz")
                    # clock stretch: worst deficit between actual and max
                    if freqs:
                        min_freq = min(freqs)
                        stretch_pct = (1.0 - min_freq / eff_max) * 100.0
                        if stretch_pct > 5.0:
                            parts.append(f"    Clock stretch: {stretch_pct:.1f}% (min {min_freq:.0f} vs max {eff_max:.0f})")
                        else:
                            parts.append(f"    Clock stretch: none ({stretch_pct:.1f}%)")
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
        except (json.JSONDecodeError, TypeError):
            lines.append(run.settings_json)

        self._events_log.setPlainText("\n".join(lines))

    # ------------------------------------------------------------------
    # Tuner sessions
    # ------------------------------------------------------------------

    def _load_tuner_sessions(self) -> list[TunerSession]:
        if not self._db:
            return []
        return self._db.list_tuner_sessions(limit=100)

    def _populate_tuner_sessions(self) -> None:
        sessions = self._tuner_sessions
        self._runs_table.setColumnCount(7)
        self._runs_table.setHorizontalHeaderLabels(
            ["Date", "Status", "CPU", "Cores", "Confirmed", "Duration", "BIOS"]
        )
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
        for row, sess in enumerate(sessions):
            if not self._db:
                continue

            # Count cores
            core_states = tp.load_core_states(self._db, sess.id)
            total = len(core_states)
            confirmed = sum(1 for cs in core_states.values() if cs.phase == TunerPhase.CONFIRMED)

            date_str = sess.created_at[:19].replace("T", " ") if sess.created_at else ""

            items = [
                (date_str, Qt.AlignmentFlag.AlignLeft),
                (sess.status.capitalize(), Qt.AlignmentFlag.AlignCenter),
                (sess.cpu_model[:30] if sess.cpu_model else "-", Qt.AlignmentFlag.AlignLeft),
                (str(total), Qt.AlignmentFlag.AlignCenter),
                (f"{confirmed}/{total}", Qt.AlignmentFlag.AlignCenter),
                (_format_duration_from_timestamps(sess.created_at, sess.updated_at), Qt.AlignmentFlag.AlignCenter),
                (sess.bios_version or "-", Qt.AlignmentFlag.AlignCenter),
            ]

            status_color = _STATUS_COLORS.get(sess.status, _COLOR_MUTED)
            for col, (text, align) in enumerate(items):
                cell = _item(str(text), align)
                if col == 1:
                    cell.setForeground(QColor(status_color))
                elif col == 4 and total > 0 and confirmed == total:
                    cell.setForeground(QColor(_COLOR_PASS))
                self._runs_table.setItem(row, col, cell)

        # Auto-select latest session so detail is visible immediately
        if sessions:
            self._runs_table.selectRow(0)

    def _show_tuner_session_detail(self, sess: TunerSession) -> None:
        if not self._db or sess.id is None:
            return

        self._selected_tuner_session = sess
        # Only show Load to CO button if the session has confirmed cores
        core_states = tp.load_core_states(self._db, sess.id)
        has_confirmed = any(cs.phase == TunerPhase.CONFIRMED for cs in core_states.values())
        self._tuner_actions_row.setVisible(True)
        self._load_co_btn.setEnabled(has_confirmed)
        self._expand_detail()

        # Info line
        try:
            cfg = json.loads(sess.config_json)
        except (json.JSONDecodeError, TypeError):
            cfg = {}

        info_parts = [
            sess.cpu_model or "Unknown CPU",
            f"coarse={cfg.get('coarse_step', '?')} fine={cfg.get('fine_step', '?')}",
            f"search={cfg.get('search_duration_seconds', '?')}s confirm={cfg.get('confirm_duration_seconds', '?')}s",
            f"max={cfg.get('max_offset', '?')}",
        ]
        if sess.bios_version:
            info_parts.append(f"BIOS {sess.bios_version}")
        self._detail_info.setText("  |  ".join(info_parts))
        self._detail_info.setStyleSheet("color: #aaa; padding: 2px;")

        # Core states table
        core_states = tp.load_core_states(self._db, sess.id)
        test_log = tp.get_test_log(self._db, sess.id)

        # Count tests per core
        tests_per_core: dict[int, int] = {}
        last_result_per_core: dict[int, bool] = {}
        for entry in test_log:
            cid = entry["core_id"]
            tests_per_core[cid] = tests_per_core.get(cid, 0) + 1
            last_result_per_core[cid] = bool(entry["passed"])

        self._core_results_table.setColumnCount(7)
        self._core_results_table.setHorizontalHeaderLabels(
            ["Core", "Phase", "Current Offset", "Best Offset", "Tests", "Last Result", "Confirm Attempts"]
        )
        cr_header = self._core_results_table.horizontalHeader()
        cr_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for i in range(7):
            if i != 1:  # Phase column stretches
                cr_header.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self._core_results_table.setColumnWidth(0, 50)
        self._core_results_table.setColumnWidth(2, 100)
        self._core_results_table.setColumnWidth(3, 80)
        self._core_results_table.setColumnWidth(4, 50)
        self._core_results_table.setColumnWidth(5, 80)
        self._core_results_table.setColumnWidth(6, 120)

        sorted_cores = sorted(core_states.keys())
        self._core_results_table.setRowCount(len(sorted_cores))

        phase_colors = {
            TunerPhase.NOT_STARTED: "#666",
            TunerPhase.COARSE_SEARCH: "#b4b432",
            TunerPhase.FINE_SEARCH: "#c8c832",
            TunerPhase.SETTLED: "#c89632",
            TunerPhase.CONFIRMING: "#3296c8",
            TunerPhase.CONFIRMED: "#32b432",
            TunerPhase.FAILED_CONFIRM: "#c86432",
        }

        for row_idx, core_id in enumerate(sorted_cores):
            cs = core_states[core_id]
            num_tests = tests_per_core.get(core_id, 0)
            last = last_result_per_core.get(core_id)
            last_str = "PASS" if last else ("FAIL" if last is not None else "-")

            row_items = [
                (str(core_id), Qt.AlignmentFlag.AlignCenter),
                (cs.phase.upper(), Qt.AlignmentFlag.AlignCenter),
                (str(cs.current_offset), Qt.AlignmentFlag.AlignCenter),
                (str(cs.best_offset) if cs.best_offset is not None else "-", Qt.AlignmentFlag.AlignCenter),
                (str(num_tests), Qt.AlignmentFlag.AlignCenter),
                (last_str, Qt.AlignmentFlag.AlignCenter),
                (str(cs.confirm_attempts), Qt.AlignmentFlag.AlignCenter),
            ]

            color = phase_colors.get(cs.phase, _COLOR_MUTED)
            for col, (text, align) in enumerate(row_items):
                cell = _item(text, align)
                if col == 1:
                    cell.setForeground(QColor(color))
                elif col == 5:
                    if last_str == "PASS":
                        cell.setForeground(QColor(_COLOR_PASS))
                    elif last_str == "FAIL":
                        cell.setForeground(QColor(_COLOR_FAIL))
                self._core_results_table.setItem(row_idx, col, cell)

        self._auto_size_core_results_table()

        # Events log — show test log entries
        lines: list[str] = []
        lines.append("── Tuner Configuration ──")
        lines.append(json.dumps(cfg, indent=2))
        lines.append("")

        if test_log:
            lines.append("── Test Log ──")
            for entry in test_log:
                ts = entry.get("tested_at", "")[:19].replace("T", " ")
                result = "PASS" if entry["passed"] else "FAIL"
                dur = f"{entry.get('duration_seconds', 0):.1f}s" if entry.get("duration_seconds") else "-"
                err = entry.get("error_message", "")
                err_str = f" — {err}" if err else ""
                lines.append(
                    f"  {ts}  Core {entry['core_id']}  "
                    f"offset {entry['offset_tested']}  "
                    f"[{entry.get('phase', '?')}] {result}  {dur}{err_str}"
                )

        # Profile summary
        profile = tp.get_best_profile(self._db, sess.id)
        if profile:
            lines.append("")
            lines.append("── Confirmed CO Profile ──")
            for cid in sorted(profile):
                lines.append(f"  Core {cid}: {profile[cid]}")

        self._events_log.setPlainText("\n".join(lines))

    @Slot()
    def _on_load_co_profile(self) -> None:
        """Load the selected tuner session's best CO profile into the CO tab."""
        if not self._db or self._selected_tuner_session is None:
            return
        profile = tp.get_best_profile(self._db, self._selected_tuner_session.id)
        if not profile:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "No Profile",
                "This tuner session has no confirmed CO offsets to load."
            )
            return
        self.load_profile_requested.emit(profile)

    def _expand_detail(self) -> None:
        """Show the detail section and split space evenly with the top."""
        self._detail_widget.setVisible(True)
        # Give top 50%, bottom 50%
        total = sum(self._splitter.sizes())
        if total > 0:
            self._splitter.setSizes([total // 2, total - total // 2])

    def _clear_detail(self) -> None:
        self._detail_info.setText("Select a run to view details")
        self._detail_info.setStyleSheet(f"color: {_COLOR_MUTED}; padding: 2px;")
        self._core_results_table.setRowCount(0)
        self._events_log.clear()
        self._tuner_actions_row.setVisible(False)
        self._selected_tuner_session = None
        self._detail_widget.setVisible(False)

    # ------------------------------------------------------------------
    # Context menu (runs table)
    # ------------------------------------------------------------------

    @Slot()
    def _show_context_menu(self, pos) -> None:
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

    def _export_json(self, row: int) -> None:
        from history.export import export_run_json_file

        displayed = getattr(self, "_displayed_runs", self._runs)
        if row >= len(displayed):
            return
        run = displayed[row]
        if not self._db or run.id is None:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export JSON", f"run_{run.id}.json", "JSON (*.json)"
        )
        if path:
            dlg = _ExportOptionsDialog(self)
            if dlg.exec() == QDialog.DialogCode.Accepted:
                export_run_json_file(
                    self._db,
                    run.id,
                    Path(path),
                    include_events=dlg.include_events,
                    include_telemetry=dlg.include_telemetry,
                )

    def _export_csv(self, row: int) -> None:
        from history.export import export_run_csv_file

        displayed = getattr(self, "_displayed_runs", self._runs)
        if row >= len(displayed):
            return
        run = displayed[row]
        if not self._db or run.id is None:
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", f"run_{run.id}.csv", "CSV (*.csv)"
        )
        if path:
            export_run_csv_file(self._db, run.id, Path(path))

    def _export_bulk_csv(self, rows: list[int]) -> None:
        from history.export import export_runs_bulk_csv_file

        if not self._db:
            return

        displayed = getattr(self, "_displayed_runs", self._runs)
        run_ids = [displayed[r].id for r in rows if r < len(displayed) and displayed[r].id is not None]
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", "runs_comparison.csv", "CSV (*.csv)"
        )
        if path:
            export_runs_bulk_csv_file(self._db, run_ids, Path(path))

    @Slot()
    def _delete_selected(self) -> None:
        if self._view_mode == self.VIEW_TUNER:
            rows = self._selected_run_rows()
            if rows:
                self._delete_tuner_sessions(rows)
            return

        # In grouped view, allow deleting contexts directly from context table
        if self._view_mode == self.VIEW_GROUPED:
            ctx_rows = sorted({idx.row() for idx in self._context_table.selectionModel().selectedRows()})
            if ctx_rows and not self._selected_run_rows():
                # User selected contexts, not runs — delete contexts
                self._delete_contexts(ctx_rows)
                return

        rows = self._selected_run_rows()
        if rows:
            self._delete_runs(rows)

    def _delete_runs(self, rows: list[int]) -> None:
        if not self._db:
            return

        displayed = getattr(self, "_displayed_runs", self._runs)
        count = len(rows)
        # Build description of what we're deleting
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
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for run_id in run_ids:
            self._db.delete_run(run_id)

        # Clean up orphaned contexts (no remaining runs)
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

        # Count total associated runs for an explicit warning
        total_runs = sum(
            len(self._context_runs.get(cid, []))
            for cid in ctx_ids
        )

        reply = QMessageBox.question(
            self,
            "Delete Contexts",
            f"This will permanently delete {len(ctx_ids)} context(s) and ALL "
            f"{total_runs} associated test run(s). This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
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
        for row in rows:
            if row < len(self._tuner_sessions) and self._tuner_sessions[row].id is not None:
                session_ids.append(self._tuner_sessions[row].id)

        if not session_ids:
            return

        reply = QMessageBox.question(
            self,
            "Delete Tuner Sessions",
            f"Delete {len(session_ids)} tuner session(s) and all associated data?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for sid in session_ids:
            self._db.delete_tuner_session(sid)

        # Clean up contexts that no longer have any runs or sessions
        self._db.delete_orphaned_contexts()
        self._refresh_preserve_context()

    # ------------------------------------------------------------------
    # Compare mode
    # ------------------------------------------------------------------

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
        self._detail_info.setStyleSheet(f"color: {_COLOR_ACTIVE}; padding: 2px;")

        col_headers = ["Core"]
        for run, _ in run_data:
            label = run.started_at[:10] if run.started_at else f"Run {run.id}"
            col_headers.extend([f"{label} Result", f"{label} Duration"])

        self._core_results_table.setColumnCount(len(col_headers))
        self._core_results_table.setHorizontalHeaderLabels(col_headers)
        cr_header = self._core_results_table.horizontalHeader()
        cr_header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        cr_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self._core_results_table.setColumnWidth(0, 50)

        self._core_results_table.setRowCount(len(sorted_cores))

        for row_idx, core_id in enumerate(sorted_cores):
            self._core_results_table.setItem(
                row_idx, 0, _item(str(core_id), Qt.AlignmentFlag.AlignCenter)
            )

            for run_idx, (run, results) in enumerate(run_data):
                core_result = next((r for r in results if r.core_id == core_id), None)
                col_base = 1 + run_idx * 2

                if core_result:
                    result_text = "PASS" if core_result.passed else ("FAIL" if core_result.passed is not None else "...")
                    result_item = _item(result_text, Qt.AlignmentFlag.AlignCenter)
                    color = _COLOR_PASS if core_result.passed else (_COLOR_FAIL if core_result.passed is not None else _COLOR_ACTIVE)
                    result_item.setForeground(QColor(color))
                    dur_item = _item(_format_duration(core_result.elapsed_seconds), Qt.AlignmentFlag.AlignCenter)
                else:
                    result_item = _item("-", Qt.AlignmentFlag.AlignCenter)
                    dur_item = _item("-", Qt.AlignmentFlag.AlignCenter)

                self._core_results_table.setItem(row_idx, col_base, result_item)
                self._core_results_table.setItem(row_idx, col_base + 1, dur_item)

        lines = ["── Run Comparison ──"]
        for run, results in run_data:
            label = run.started_at[:19].replace("T", " ") if run.started_at else f"Run {run.id}"
            passed = sum(1 for r in results if r.passed)
            failed = sum(1 for r in results if r.passed is False)
            lines.append(f"  {label}:  {run.backend}/{run.stress_mode}  {passed}P/{failed}F  {_format_duration(run.total_seconds)}")
        self._events_log.setPlainText("\n".join(lines))


# ---------------------------------------------------------------------------
# Export options dialog
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item(text: str, alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(alignment)
    return item


def _format_duration(seconds: float) -> str:
    if seconds <= 0:
        return ""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m}m {s:02d}s"


def _format_duration_from_timestamps(start: str, end: str) -> str:
    """Calculate duration from ISO timestamp strings."""
    from datetime import datetime, timezone
    try:
        t_start = datetime.fromisoformat(start)
        t_end = datetime.fromisoformat(end)
        delta = (t_end - t_start).total_seconds()
        return _format_duration(delta)
    except (ValueError, TypeError):
        return "-"


def _co_summary(co_json: str) -> str:
    """Summarize CO offsets JSON for display in context table."""
    try:
        offsets = json.loads(co_json)
    except (json.JSONDecodeError, TypeError):
        return "none"
    if not offsets:
        return "none"

    values = list(offsets.values())
    if all(v == values[0] for v in values):
        return f"all {values[0]}"

    lo, hi = min(values), max(values)
    return f"mixed [{lo}..{hi}]"


def _best_result(runs: list[RunRecord]) -> str:
    """Best pass rate among completed runs."""
    completed = [r for r in runs if r.status == "completed" and r.total_cores > 0]
    if not completed:
        return "-"
    best = max(completed, key=lambda r: r.cores_passed / r.total_cores)
    return f"{best.cores_passed}/{best.total_cores}"
