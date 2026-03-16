"""Visual per-core grid widget — CCD-aware vertical layout showing test status."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

if TYPE_CHECKING:
    from engine.scheduler import CoreTestStatus
    from engine.topology import CPUTopology

# state -> (background color, text color, border color)
STATE_COLORS: dict[str, tuple[str, str, str]] = {
    "pending": ("#2d2d2d", "#888888", "#444"),
    "testing": ("#1a3a5c", "#4fc3f7", "#4fc3f7"),
    "passed": ("#1b3a1b", "#4caf50", "#4caf50"),
    "failed": ("#3a1b1b", "#f44336", "#f44336"),
    "skipped": ("#2d2d2d", "#555555", "#444"),
    # passed but had errors in earlier iterations — warning state
    "warned": ("#3a3a1b", "#ffb74d", "#ffb74d"),
}

CELL_HEIGHT_NORMAL = 22
CELL_HEIGHT_ACTIVE = 38  # taller to show telemetry details


class CoreCell(QWidget):
    """Single core display cell — compact horizontal row, expands when testing."""

    clicked = Signal(int)  # emits core_id

    def __init__(self, core_id: int, ccd: int | None = None, has_vcache: bool = False) -> None:
        super().__init__()
        self.core_id = core_id
        self.ccd = ccd
        self.has_vcache = has_vcache
        self._state = "pending"
        self._freq_mhz: float = 0
        self._temp_c: float = 0
        self._vcore_v: float | None = None
        self._errors: int = 0
        self._elapsed: float = 0

        self.setFixedHeight(CELL_HEIGHT_NORMAL)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 1, 4, 1)
        outer.setSpacing(0)

        # Row 1: core label + status + detail
        row1 = QHBoxLayout()
        row1.setSpacing(0)

        header = f"C{core_id}"
        if has_vcache:
            header += "V"

        self._header_label = QLabel(header)
        self._header_label.setFont(QFont("monospace", 8, QFont.Weight.Bold))
        self._header_label.setFixedWidth(32)
        self._header_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._header_label)

        self._status_label = QLabel("Pending")
        self._status_label.setFont(QFont("monospace", 7))
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row1.addWidget(self._status_label, 1)

        self._detail_label = QLabel("")
        self._detail_label.setFont(QFont("monospace", 7))
        self._detail_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._detail_label)

        outer.addLayout(row1)

        # Row 2: expanded telemetry (only visible when testing)
        self._telemetry_label = QLabel("")
        self._telemetry_label.setFont(QFont("monospace", 7))
        self._telemetry_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._telemetry_label.setVisible(False)
        outer.addWidget(self._telemetry_label)

        self._apply_state_style()

    def update_status(self, status: CoreTestStatus) -> None:
        self._errors = status.errors
        self._elapsed = status.elapsed_seconds

        prev_state = self._state
        # Determine visual state: "warned" = passed but had errors in prior iterations
        if status.state == "passed" and status.errors > 0:
            self._state = "warned"
        else:
            self._state = status.state

        state_text = status.state.capitalize()
        if status.state == "testing" and status.current_phase:
            state_text = status.current_phase.capitalize()
        if status.errors > 0:
            state_text += f" ({status.errors}err)"

        mins = int(status.elapsed_seconds // 60)
        secs = int(status.elapsed_seconds % 60)
        time_str = f"{mins}m{secs:02d}s" if status.elapsed_seconds > 0 else ""

        # When testing, combine phase + time in status label (detail_label is used for telemetry)
        if self._state == "testing":
            if time_str:
                self._status_label.setText(f"{state_text}  {time_str}")
            else:
                self._status_label.setText(state_text)
        else:
            self._status_label.setText(state_text)
            self._detail_label.setText(time_str)

        # Expand/collapse cell for testing state
        if self._state == "testing" and prev_state != "testing":
            self.setFixedHeight(CELL_HEIGHT_ACTIVE)
            self._telemetry_label.setVisible(True)
        elif self._state != "testing" and prev_state == "testing":
            self.setFixedHeight(CELL_HEIGHT_NORMAL)
            self._telemetry_label.setVisible(False)
            self._telemetry_label.setText("")

        self._apply_state_style()

    def update_telemetry(
        self,
        freq_mhz: float = 0,
        temp_c: float = 0,
        vcore_v: float | None = None,
        stretch_pct: float | None = None,
    ) -> None:
        self._freq_mhz = freq_mhz
        self._temp_c = temp_c
        self._vcore_v = vcore_v

        if self._state == "testing":
            # Row 1 right: keep clear (status has phase + time)
            # Row 2: all telemetry on the expanded line
            parts = []
            if freq_mhz > 0:
                parts.append(f"{freq_mhz:.0f}MHz")
            if stretch_pct is not None:
                parts.append(f"S:{stretch_pct:.1f}%")
            if temp_c > 0:
                parts.append(f"{temp_c:.0f}C")
            if vcore_v is not None:
                parts.append(f"{vcore_v:.4f}V")
            self._telemetry_label.setText("  ".join(parts))
            self._detail_label.setText("")
        else:
            # Non-testing: show last reading in detail label
            parts = []
            if freq_mhz > 0:
                parts.append(f"{freq_mhz:.0f}")
            if temp_c > 0:
                parts.append(f"{temp_c:.0f}C")
            if vcore_v is not None:
                parts.append(f"{vcore_v:.3f}V")
            if parts:
                self._detail_label.setText(" ".join(parts))

    def _apply_state_style(self) -> None:
        bg, fg, border = STATE_COLORS.get(self._state, STATE_COLORS["pending"])
        border_width = "2px" if self._state in ("testing", "failed", "warned") else "1px"
        self.setStyleSheet(
            f"CoreCell {{ background-color: {bg}; border: {border_width} solid {border}; "
            f"border-radius: 3px; }}"
            f" QLabel {{ color: {fg}; background: transparent; }}"
        )

    def mousePressEvent(self, event) -> None:
        self.clicked.emit(self.core_id)
        super().mousePressEvent(event)


class CoreGridWidget(QWidget):
    """Vertical list of CoreCells grouped by CCD."""

    core_clicked = Signal(int)

    def __init__(self, topology: CPUTopology | None = None) -> None:
        super().__init__()
        self._cells: dict[int, CoreCell] = {}
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(1)

        if topology:
            self.set_topology(topology)

    def set_topology(self, topology: CPUTopology) -> None:
        """Rebuild the vertical list from CPU topology."""
        # clear existing
        for cell in self._cells.values():
            cell.deleteLater()
        self._cells.clear()

        # clear layout
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

        # group cores by CCD
        ccd_groups: dict[int, list[int]] = {}
        for core in sorted(topology.cores.values(), key=lambda c: c.core_id):
            ccd = core.ccd if core.ccd is not None else 0
            ccd_groups.setdefault(ccd, []).append(core.core_id)

        for ccd_idx in sorted(ccd_groups.keys()):
            core_ids = ccd_groups[ccd_idx]

            # CCD header
            has_vcache = any(
                topology.cores[cid].has_vcache for cid in core_ids if cid in topology.cores
            )
            vcache_str = " (V-Cache)" if has_vcache else ""
            ccd_label = QLabel(f"CCD {ccd_idx}{vcache_str}")
            ccd_label.setFont(QFont("monospace", 8, QFont.Weight.Bold))
            ccd_label.setStyleSheet("color: #aaa; padding: 1px 4px;")
            ccd_label.setFixedHeight(18)
            self._layout.addWidget(ccd_label)

            # core cells in a vertical list
            for core_id in core_ids:
                core_info = topology.cores.get(core_id)
                cell = CoreCell(
                    core_id=core_id,
                    ccd=ccd_idx,
                    has_vcache=core_info.has_vcache if core_info else False,
                )
                cell.clicked.connect(self.core_clicked.emit)
                self._cells[core_id] = cell
                self._layout.addWidget(cell)

        self._layout.addStretch()

    def update_core_status(self, core_id: int, status: CoreTestStatus) -> None:
        cell = self._cells.get(core_id)
        if cell:
            cell.update_status(status)

    def update_core_telemetry(
        self,
        core_id: int,
        freq_mhz: float,
        temp_c: float,
        vcore_v: float | None = None,
        stretch_pct: float | None = None,
    ) -> None:
        cell = self._cells.get(core_id)
        if cell:
            cell.update_telemetry(freq_mhz, temp_c, vcore_v, stretch_pct)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())
