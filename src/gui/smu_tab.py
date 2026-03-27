"""SMU / Curve Optimizer tab — read/write per-core CO offsets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from smu.commands import SMUCommandSet, detect_generation, get_commands
from smu.driver import RyzenSMU

if TYPE_CHECKING:
    from engine.topology import CPUTopology


class SMUTab(QWidget):
    """Curve Optimizer read/write interface."""

    co_changed = Signal(int, int)  # core_id, new_value

    def __init__(self, topology: CPUTopology | None = None) -> None:
        super().__init__()
        self._topology = topology
        self._smu: RyzenSMU | None = None
        self._commands: SMUCommandSet | None = None
        self._setup_ui()

        if topology:
            self.set_topology(topology)

    @property
    def smu(self) -> RyzenSMU | None:
        """Expose the SMU driver instance for external use (e.g. history logger)."""
        return self._smu

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # profile banner (shown when CO offsets are loaded from a tuner session)
        self._profile_banner = QLabel("")
        self._profile_banner.setStyleSheet(
            "background: #1a3a5c; color: #4fc3f7; padding: 8px; "
            "border-radius: 4px; font: 11px monospace;"
        )
        self._profile_banner.setVisible(False)
        layout.addWidget(self._profile_banner)

        # status bar
        status_group = QGroupBox("SMU Status")
        status_layout = QHBoxLayout(status_group)

        self._status_label = QLabel("Checking ryzen_smu driver...")
        self._status_label.setFont(QFont("monospace", 10))
        status_layout.addWidget(self._status_label)

        self._gen_label = QLabel("")
        self._gen_label.setFont(QFont("monospace", 9))
        status_layout.addWidget(self._gen_label)

        self._range_label = QLabel("")
        self._range_label.setFont(QFont("monospace", 9))
        status_layout.addWidget(self._range_label)

        layout.addWidget(status_group)

        # CO table
        co_group = QGroupBox("Per-Core Curve Optimizer Offsets")
        co_layout = QVBoxLayout(co_group)

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["Core", "CCD", "Current CO", "New CO", "Apply"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setAlternatingRowColors(True)
        co_layout.addWidget(self._table)

        # bulk actions
        bulk_layout = QHBoxLayout()

        self._read_all_btn = QPushButton("Read All CO")
        self._read_all_btn.clicked.connect(self._read_all_co)
        bulk_layout.addWidget(self._read_all_btn)

        self._apply_all_btn = QPushButton("Apply All New Values")
        self._apply_all_btn.clicked.connect(self._apply_all_co)
        bulk_layout.addWidget(self._apply_all_btn)

        self._reset_btn = QPushButton("Reset All to 0")
        self._reset_btn.clicked.connect(self._reset_all_co)
        bulk_layout.addWidget(self._reset_btn)

        co_layout.addLayout(bulk_layout)

        # backup / restore / dry-run row
        safety_layout = QHBoxLayout()

        self._backup_btn = QPushButton("Backup Current CO")
        self._backup_btn.setToolTip(
            "Save current CO values so they can be restored later this session"
        )
        self._backup_btn.clicked.connect(self._backup_co)
        safety_layout.addWidget(self._backup_btn)

        self._restore_btn = QPushButton("Restore Backup")
        self._restore_btn.setToolTip("Restore CO values from the most recent backup")
        self._restore_btn.clicked.connect(self._restore_co)
        self._restore_btn.setEnabled(False)
        safety_layout.addWidget(self._restore_btn)

        self._dry_run_cb = QCheckBox("Dry Run")
        self._dry_run_cb.setToolTip(
            "When checked, CO writes are logged but NOT applied to hardware"
        )
        self._dry_run_cb.toggled.connect(self._on_dry_run_toggled)
        safety_layout.addWidget(self._dry_run_cb)

        co_layout.addLayout(safety_layout)

        # warning
        warn = QLabel(
            "\u26a0 CO offsets set via SMU are VOLATILE \u2014 they reset on reboot. "
            "Use BIOS for persistent values. Requires ryzen_smu kernel module and root access."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color: #ff9800; padding: 8px;")
        co_layout.addWidget(warn)

        layout.addWidget(co_group)

        self._spinboxes: dict[int, QSpinBox] = {}  # core_id -> spinbox

    def set_topology(self, topology: CPUTopology) -> None:
        self._topology = topology

        # detect CPU generation and get commands
        gen = detect_generation(topology.family, topology.model, topology.model_name)
        self._commands = get_commands(gen)

        smu_available = self._commands is not None and RyzenSMU.is_available()
        has_co = self._commands is not None and self._commands.has_co

        if smu_available and has_co:
            self._smu = RyzenSMU(self._commands, dry_run=self._dry_run_cb.isChecked())
            self._smu.set_topology(topology)
            self._status_label.setText("ryzen_smu: Connected")
            self._status_label.setStyleSheet("color: #4caf50;")
            self._gen_label.setText(f"Generation: {gen.name}")
            co_min, co_max = self._commands.co_range
            self._range_label.setText(f"CO Range: [{co_min}, {co_max}]")
        elif smu_available and not has_co:
            self._smu = RyzenSMU(self._commands, dry_run=self._dry_run_cb.isChecked())
            self._smu.set_topology(topology)
            self._status_label.setText("ryzen_smu: Connected (no CO support)")
            self._status_label.setStyleSheet("color: #ff9800;")
            self._gen_label.setText(f"Generation: {gen.name}")
            self._range_label.setText("CO: Not supported on this generation")
        elif self._commands:
            self._status_label.setText("ryzen_smu: Driver not loaded")
            self._status_label.setStyleSheet("color: #f44336;")
            self._gen_label.setText(f"Generation: {gen.name}")
        else:
            self._status_label.setText(f"Unsupported CPU generation: {gen.name}")
            self._status_label.setStyleSheet("color: #ff9800;")

        # Disable CO buttons if SMU is not available or generation lacks CO
        co_available = smu_available and has_co
        self._apply_all_btn.setEnabled(co_available)
        self._reset_btn.setEnabled(co_available)
        self._backup_btn.setEnabled(co_available)
        self._restore_btn.setEnabled(False)  # no backup yet

        self._populate_table()

    def _populate_table(self) -> None:
        if not self._topology:
            return

        smu_available = self._smu is not None

        cores = sorted(self._topology.cores.values(), key=lambda c: c.core_id)
        self._table.setRowCount(len(cores))
        self._spinboxes.clear()

        co_min = self._commands.co_range[0] if self._commands else -30
        co_max = self._commands.co_range[1] if self._commands else 30

        for row, core in enumerate(cores):
            self._table.setItem(row, 0, _item(str(core.core_id)))
            self._table.setItem(row, 1, _item(str(core.ccd) if core.ccd is not None else "-"))
            self._table.setItem(row, 2, _item("--"))  # current CO, read later

            spin = QSpinBox()
            spin.setRange(co_min, co_max)
            spin.setValue(0)
            self._spinboxes[core.core_id] = spin
            self._table.setCellWidget(row, 3, spin)

            apply_btn = QPushButton("Apply")
            apply_btn.setEnabled(smu_available)
            apply_btn.clicked.connect(lambda checked, cid=core.core_id: self._apply_single(cid))
            self._table.setCellWidget(row, 4, apply_btn)

        # auto-read if SMU available
        if self._smu:
            self._read_all_co()

    # ------------------------------------------------------------------
    # Dry-run toggle
    # ------------------------------------------------------------------

    def _on_dry_run_toggled(self, checked: bool) -> None:
        if self._smu:
            self._smu.dry_run = checked

        # visual feedback on write buttons
        dry_style = (
            "QPushButton { border: 2px dashed #ff9800; color: #ff9800; }"
            if checked
            else ""
        )
        self._apply_all_btn.setStyleSheet(dry_style)
        self._reset_btn.setStyleSheet(dry_style)
        self._apply_all_btn.setText("Apply All [DRY]" if checked else "Apply All New Values")
        self._reset_btn.setText("Reset All [DRY]" if checked else "Reset All to 0")

    # ------------------------------------------------------------------
    # Confirmation dialog (shared by all write paths)
    # ------------------------------------------------------------------

    def _confirm_co_write(self, detail: str) -> bool:
        """Show a confirmation dialog before any CO write.

        Returns True if the user confirmed, False otherwise.
        """
        dry_tag = " [DRY RUN — no actual write]" if self._dry_run_cb.isChecked() else ""
        reply = QMessageBox.warning(
            self,
            f"Confirm CO Write{dry_tag}",
            f"{detail}\n\n"
            "This will modify CPU voltage curve settings via the SMU.\n"
            "  \u2022 Values are VOLATILE and reset on reboot.\n"
            "  \u2022 Your BIOS PBO settings are NOT affected.\n"
            "  \u2022 Incorrect values may cause instability until next reboot.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    # ------------------------------------------------------------------
    # Read / Write actions
    # ------------------------------------------------------------------

    def _read_all_co(self) -> None:
        if not self._smu or not self._topology:
            return

        max_retries = 2
        for core_id, row in self._core_row_map().items():
            val = self._smu.get_co_offset(core_id)
            # retry individually on failure
            if val is None:
                for _ in range(max_retries):
                    val = self._smu.get_co_offset(core_id)
                    if val is not None:
                        break
            text = str(val) if val is not None else "ERR"
            self._table.setItem(row, 2, _item(text))
            if val is not None and core_id in self._spinboxes:
                self._spinboxes[core_id].setValue(val)

    def _apply_single(self, core_id: int) -> None:
        if not self._smu:
            QMessageBox.warning(self, "Error", "ryzen_smu driver not available")
            return

        spin = self._spinboxes.get(core_id)
        if not spin:
            return

        value = spin.value()

        if not self._confirm_co_write(f"Set core {core_id} CO offset to {value}."):
            return

        success = self._smu.set_co_offset(core_id, value)
        if success:
            row = self._core_row_map().get(core_id)
            if row is not None:
                self._table.setItem(row, 2, _item(str(value)))
            self.co_changed.emit(core_id, value)
        else:
            QMessageBox.warning(self, "Error", f"Failed to set CO for core {core_id}")

    def _apply_all_co(self) -> None:
        if not self._smu:
            QMessageBox.warning(self, "Error", "ryzen_smu driver not available")
            return

        summary = ", ".join(
            f"C{cid}={spin.value()}" for cid, spin in sorted(self._spinboxes.items())
        )
        if not self._confirm_co_write(f"Apply CO offsets to all cores:\n{summary}"):
            return

        failed = []
        for core_id, spin in self._spinboxes.items():
            value = spin.value()
            if not self._smu.set_co_offset(core_id, value):
                failed.append(core_id)

        if failed:
            QMessageBox.warning(self, "Error", f"Failed to set CO for cores: {failed}")
        else:
            self._read_all_co()
            self._profile_banner.setVisible(False)

    def _reset_all_co(self) -> None:
        if not self._smu:
            return

        if not self._confirm_co_write("Reset all Curve Optimizer offsets to 0."):
            return

        if self._smu.reset_all_co():
            self._read_all_co()
        else:
            # manual reset: set each core to 0
            for core_id in self._spinboxes:
                self._smu.set_co_offset(core_id, 0)
            self._read_all_co()

    # ------------------------------------------------------------------
    # Backup / Restore
    # ------------------------------------------------------------------

    def _backup_co(self) -> None:
        if not self._smu or not self._topology:
            return

        num_cores = len(self._topology.cores)
        backup = self._smu.backup_co_offsets(num_cores)
        self._restore_btn.setEnabled(True)
        QMessageBox.information(
            self,
            "Backup Complete",
            f"Saved CO offsets for {len(backup)} cores.\n"
            "Use 'Restore Backup' to revert within this session.\n\n"
            "Note: CO values are volatile and reset on reboot regardless.",
        )

    def _restore_co(self) -> None:
        if not self._smu or not self._smu.has_backup():
            QMessageBox.warning(self, "Error", "No backup available to restore.")
            return

        if not self._confirm_co_write("Restore CO offsets from backup."):
            return

        ok, failed = self._smu.restore_co_offsets()
        if ok:
            self._read_all_co()
            QMessageBox.information(self, "Restored", "CO offsets restored from backup.")
        else:
            QMessageBox.warning(
                self, "Partial Failure", f"Failed to restore CO for cores: {failed}"
            )
            self._read_all_co()

    def set_tuner_running(self, running: bool) -> None:
        """Disable CO write operations while the auto-tuner controls SMU.

        Reading is still allowed (informational). Writing would conflict
        with the tuner's CO isolation and validation offsets.
        """
        self._tuner_active = running

        # When re-enabling, check that SMU is still available
        smu_ok = self._smu is not None and self._smu.is_available() if hasattr(self, "_smu") else False
        write_enabled = not running and smu_ok

        self._apply_all_btn.setEnabled(write_enabled)
        self._reset_btn.setEnabled(write_enabled)
        self._restore_btn.setEnabled(write_enabled and bool(getattr(self, "_backup", None)))
        # Per-row Apply buttons
        for row in range(self._table.rowCount()):
            btn = self._table.cellWidget(row, 4)
            if btn is not None:
                btn.setEnabled(write_enabled)
        # Spinboxes
        for spin in self._spinboxes.values():
            spin.setEnabled(not running)  # editing is fine even without SMU

        if running:
            self._apply_all_btn.setToolTip("Disabled while auto-tuner is running")
        elif not smu_ok:
            self._apply_all_btn.setToolTip("SMU not available")
        else:
            self._apply_all_btn.setToolTip("")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _core_row_map(self) -> dict[int, int]:
        if not self._topology:
            return {}
        cores = sorted(self._topology.cores.keys())
        return {cid: row for row, cid in enumerate(cores)}


def _item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item
