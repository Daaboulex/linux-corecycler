"""Visual per-core grid widget — CCD-aware vertical layout showing test status."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from corecycler.gui.style import duration_str, font_mono, scheduler_phase_label, state_label, theme, tuner_phase_label

if TYPE_CHECKING:
    from corecycler.engine.scheduler import CoreTestStatus
    from corecycler.engine.topology import CPUTopology, PhysicalCore


def ccd_groups(topology: CPUTopology) -> list[tuple[str, list[PhysicalCore]]]:
    """The cores by CCD in CCD order, each group with its heading; cores whose CCD was not read come last."""
    groups: dict[int | None, list[PhysicalCore]] = {}
    for core in sorted(topology.cores.values(), key=lambda c: c.core_id):
        groups.setdefault(core.ccd, []).append(core)
    ordered = sorted(groups.items(), key=lambda group: (group[0] is None, group[0]))
    return [(f"CCD {'unread' if ccd is None else ccd}", cores) for ccd, cores in ordered]


CELL_HEIGHT_NORMAL = 22
CELL_HEIGHT_ACTIVE = 38

_STATUS_WIDTH = 92
_TIME_WIDTH = 48


class CoreCell(QWidget):
    """Single core display cell — fixed slots so text never shifts or clips."""

    def __init__(self, core_id: int, ccd: int | None = None, has_vcache: bool = False) -> None:
        super().__init__()
        self.core_id = core_id
        self.ccd = ccd
        self.has_vcache = has_vcache
        self._state = "pending"
        self._scheduler_phase = ""
        self._tuner_phase: str | None = None
        self._tuner_value: int | None = None
        self._errors: int = 0
        self._elapsed: float = 0

        self.setFixedHeight(CELL_HEIGHT_NORMAL)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 1, 4, 1)
        outer.setSpacing(0)

        row1 = QHBoxLayout()
        row1.setSpacing(0)

        header = f"C{core_id}"
        if has_vcache:
            header += "V"

        self._header_label = QLabel(header)
        self._header_label.setFont(font_mono(8, bold=True))
        self._header_label.setFixedWidth(32)
        self._header_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._header_label)

        self._status_label = QLabel(state_label("pending"))
        self._status_label.setFont(font_mono(7))
        self._status_label.setFixedWidth(_STATUS_WIDTH)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._status_label)

        self._detail_label = QLabel("")
        self._detail_label.setFont(font_mono(7))
        self._detail_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._detail_label, 1)

        self._time_label = QLabel("")
        self._time_label.setFont(font_mono(7))
        self._time_label.setFixedWidth(_TIME_WIDTH)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row1.addWidget(self._time_label)

        outer.addLayout(row1)

        self._telemetry_label = QLabel("")
        self._telemetry_label.setFont(font_mono(7))
        self._telemetry_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._telemetry_label.setVisible(False)
        outer.addWidget(self._telemetry_label)

        self._apply_state_style()

    def _refresh_labels(self) -> None:
        if self._tuner_phase:
            text = tuner_phase_label(self._tuner_phase)
        elif self._state == "testing" and self._scheduler_phase:
            text = scheduler_phase_label(self._scheduler_phase)
        else:
            text = state_label(self._state)
        if self._errors > 0:
            text += f" ({self._errors}e)"
        self._status_label.setText(text)
        self._time_label.setText(duration_str(self._elapsed) if self._elapsed > 0 else "")
        self._detail_label.setText(f"tuner {self._tuner_value}" if self._tuner_value is not None else "")

    def update_status(self, status: CoreTestStatus) -> None:
        self._errors = status.errors
        self._elapsed = status.elapsed_seconds
        self._scheduler_phase = status.current_phase or ""

        prev_state = self._state
        if status.state == "passed" and status.errors > 0:
            self._state = "warned"
        else:
            self._state = status.state

        self._refresh_labels()

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
        freq_mhz: float | None = None,
        temp_c: float | None = None,
        vcore_v: float | None = None,
        stretch_pct: float | None = None,
        tuner_value: int | None = None,
        tuner_phase: str | None = None,
    ) -> None:
        self._tuner_value = tuner_value
        self._tuner_phase = tuner_phase
        self._refresh_labels()

        if self._state in ("testing", "queued", "backoff"):
            parts = []
            if freq_mhz is not None:
                parts.append(f"{freq_mhz:.0f}MHz")
            if stretch_pct is not None:
                parts.append(f"S:{stretch_pct:.1f}%")
            if temp_c is not None:
                parts.append(f"{temp_c:.0f}C")
            if vcore_v is not None:
                parts.append(f"{vcore_v:.4f}V")
            self._telemetry_label.setText("  ".join(parts))

    def _apply_state_style(self) -> None:
        bg, fg, border = theme.STATE_COLORS.get(self._state, theme.STATE_COLORS["pending"])
        border_width = "2px" if self._state in ("testing", "failed", "warned") else "1px"
        self.setStyleSheet(
            f"CoreCell {{ background-color: {bg}; border: {border_width} solid {border}; "
            f"border-radius: 3px; }}"
            f" QLabel {{ color: {fg}; background: transparent; }}"
        )


class CoreGridWidget(QWidget):
    """Vertical list of CoreCells grouped by CCD."""

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
        for cell in self._cells.values():
            cell.deleteLater()
        self._cells.clear()

        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())

        for heading, cores in ccd_groups(topology):
            vcache_str = " (V-Cache)" if any(core.has_vcache for core in cores) else ""
            ccd_label = QLabel(f"{heading}{vcache_str}")
            ccd_label.setFont(font_mono(8, bold=True))
            ccd_label.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM}; padding: 1px 4px;")
            ccd_label.setFixedHeight(18)
            self._layout.addWidget(ccd_label)

            for core in cores:
                cell = CoreCell(core_id=core.core_id, ccd=core.ccd, has_vcache=core.has_vcache)
                self._cells[core.core_id] = cell
                self._layout.addWidget(cell)

        self._layout.addStretch()

    def update_core_status(self, core_id: int, status: CoreTestStatus) -> None:
        cell = self._cells.get(core_id)
        if cell:
            cell.update_status(status)

    def update_core_telemetry(
        self,
        core_id: int,
        freq_mhz: float | None = None,
        temp_c: float | None = None,
        vcore_v: float | None = None,
        stretch_pct: float | None = None,
        tuner_value: int | None = None,
        tuner_phase: str | None = None,
    ) -> None:
        cell = self._cells.get(core_id)
        if cell:
            cell.update_telemetry(freq_mhz, temp_c, vcore_v, stretch_pct, tuner_value, tuner_phase)

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                self._clear_layout(item.layout())
