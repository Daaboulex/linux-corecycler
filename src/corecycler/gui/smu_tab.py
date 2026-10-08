"""Curve Optimizer tab: reads and writes each core's Curve Optimizer value through ryzen_smu."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
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

from corecycler.config.paths import user_home
from corecycler.gui.style import theme
from corecycler.smu.commands import CPUGeneration, SMUCommandSet, detect_generation, get_commands
from corecycler.smu.driver import RyzenSMU, core_map_blocked
from corecycler.smu.writer_lock import WriterBusy, co_writer

if TYPE_CHECKING:
    from corecycler.engine.topology import CPUTopology


SET_ALL = "Set all"
SET_ZERO = "Set all to 0"
UNREADABLE = "unreadable"


def generation_text(generation: CPUGeneration) -> str:
    family, *rest = generation.name.split("_")
    named = family.replace("ZEN", "Zen ") if family.startswith("ZEN") else family.capitalize()
    return " ".join([named, *(word.capitalize() for word in rest)])


class SMUTab(QWidget):
    def __init__(self, topology: CPUTopology | None = None) -> None:
        super().__init__()
        self._topology = topology
        self._smu: RyzenSMU | None = None
        self._commands: SMUCommandSet | None = None
        self._tuner_active = False
        self._setup_ui()

        if topology:
            self.set_topology(topology)

    @property
    def smu(self) -> RyzenSMU | None:
        return self._smu

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        self._profile_banner = QLabel("")
        self._profile_banner.setStyleSheet(
            f"background: {theme.BG_SELECTED}; color: {theme.COLOR_ON_SELECTED}; "
            f"padding: 8px; border-radius: 4px; font: 11px monospace;"
        )
        self._profile_banner.setVisible(False)
        layout.addWidget(self._profile_banner)

        status_group = QGroupBox("Driver")
        status_layout = QHBoxLayout(status_group)

        self._status_label = QLabel("Looking for the ryzen_smu driver")
        self._status_label.setFont(QFont("monospace", 10))
        status_layout.addWidget(self._status_label)

        self._gen_label = QLabel("")
        self._gen_label.setFont(QFont("monospace", 9))
        status_layout.addWidget(self._gen_label)

        self._range_label = QLabel("")
        self._range_label.setFont(QFont("monospace", 9))
        status_layout.addWidget(self._range_label)

        layout.addWidget(status_group)

        co_group = QGroupBox("Curve Optimizer per core")
        co_layout = QVBoxLayout(co_group)

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels(["Core", "CCD", "Now on the CPU", "Value to set", ""])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        co_layout.addWidget(self._table)

        bulk_layout = QHBoxLayout()

        self._read_all_btn = QPushButton("Read from CPU")
        self._read_all_btn.clicked.connect(self._read_all_co)
        bulk_layout.addWidget(self._read_all_btn)

        self._apply_all_btn = QPushButton(SET_ALL)
        self._apply_all_btn.clicked.connect(self._apply_all_co)
        bulk_layout.addWidget(self._apply_all_btn)

        self._reset_btn = QPushButton(SET_ZERO)
        self._reset_btn.clicked.connect(self._reset_all_co)
        bulk_layout.addWidget(self._reset_btn)

        co_layout.addLayout(bulk_layout)

        safety_layout = QHBoxLayout()

        self._backup_btn = QPushButton("Remember current values")
        self._backup_btn.setToolTip("Keeps every core's value now on the CPU, until the app closes")
        self._backup_btn.clicked.connect(self._backup_co)
        safety_layout.addWidget(self._backup_btn)

        self._restore_btn = QPushButton("Put remembered values back")
        self._restore_btn.setToolTip("Writes the values kept by Remember current values")
        self._restore_btn.clicked.connect(self._restore_co)
        self._restore_btn.setEnabled(False)
        safety_layout.addWidget(self._restore_btn)

        self._dry_run_cb = QCheckBox("Practice mode (nothing is written)")
        self._dry_run_cb.setToolTip("Every write is logged and shown, but no value reaches the CPU")
        self._dry_run_cb.toggled.connect(self._on_dry_run_toggled)
        safety_layout.addWidget(self._dry_run_cb)

        co_layout.addLayout(safety_layout)

        profile_layout = QHBoxLayout()

        save_co_btn = QPushButton("Save values to file")
        save_co_btn.setToolTip("Saves the Value to set column to a JSON file")
        save_co_btn.clicked.connect(self._save_co_profile)
        profile_layout.addWidget(save_co_btn)

        load_co_btn = QPushButton("Load values from file")
        load_co_btn.setToolTip("Fills the Value to set column from a JSON file; nothing is written")
        load_co_btn.clicked.connect(self._load_co_profile)
        profile_layout.addWidget(load_co_btn)

        co_layout.addLayout(profile_layout)

        warn = QLabel(
            "Values set here last until the next reboot; the BIOS keeps its own. "
            "Writing needs the ryzen_smu kernel module and permission to use it."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.COLOR_ORANGE}; padding: 8px;")
        co_layout.addWidget(warn)

        layout.addWidget(co_group)

        self._spinboxes: dict[int, QSpinBox] = {}

    def set_topology(self, topology: CPUTopology) -> None:
        self._topology = topology

        gen = detect_generation(topology.family, topology.model, topology.model_name)
        self._commands = get_commands(gen)

        smu_available = self._commands is not None and RyzenSMU.is_available()
        has_co = self._commands is not None and self._commands.has_co

        if smu_available and has_co:
            self._smu = RyzenSMU(self._commands, dry_run=self._dry_run_cb.isChecked())
            self._smu.set_topology(topology)
            map_err = core_map_blocked(self._smu)
            if map_err is not None:
                self._status_label.setText("ryzen_smu driver connected, per-core values unavailable")
                self._status_label.setStyleSheet(f"color: {theme.COLOR_ORANGE};")
                self._status_label.setToolTip(map_err)
                self._gen_label.setText(f"CPU generation {generation_text(gen)}")
                self._range_label.setText(f"Writes are off: {map_err}")
                self._range_label.setWordWrap(True)
            else:
                self._status_label.setText("ryzen_smu driver connected")
                self._status_label.setStyleSheet(f"color: {theme.COLOR_PASS};")
                self._gen_label.setText(f"CPU generation {generation_text(gen)}")
                co_min, co_max = self._commands.co_range
                self._range_label.setText(f"Allowed values {co_min} to {co_max}")
        elif smu_available and not has_co:
            self._smu = RyzenSMU(self._commands, dry_run=self._dry_run_cb.isChecked())
            self._smu.set_topology(topology)
            self._status_label.setText("ryzen_smu driver connected")
            self._status_label.setStyleSheet(f"color: {theme.COLOR_ORANGE};")
            self._gen_label.setText(f"CPU generation {generation_text(gen)}")
            self._range_label.setText("This CPU generation has no Curve Optimizer")
        elif self._commands:
            self._status_label.setText("ryzen_smu driver not loaded")
            self._status_label.setStyleSheet(f"color: {theme.COLOR_FAIL};")
            self._gen_label.setText(f"CPU generation {generation_text(gen)}")
        else:
            self._status_label.setText(f"This CPU generation is not supported ({generation_text(gen)})")
            self._status_label.setStyleSheet(f"color: {theme.COLOR_ORANGE};")

        self._backup_btn.setEnabled(smu_available and has_co and core_map_blocked(self._smu) is None)
        self._populate_table()

    def _populate_table(self) -> None:
        if not self._topology:
            return

        cores = sorted(self._topology.cores.values(), key=lambda c: c.core_id)
        self._table.setRowCount(len(cores))
        self._spinboxes.clear()

        co_min = self._commands.co_range[0] if self._commands else -30
        co_max = self._commands.co_range[1] if self._commands else 30

        for row, core in enumerate(cores):
            self._table.setItem(row, 0, _item(str(core.core_id)))
            self._table.setItem(row, 1, _item(str(core.ccd) if core.ccd is not None else "-"))
            self._table.setItem(row, 2, _item("--"))

            spin = QSpinBox()
            spin.setRange(co_min, co_max)
            spin.setValue(0)
            self._spinboxes[core.core_id] = spin
            self._table.setCellWidget(row, 3, spin)

            apply_btn = QPushButton("Set")
            apply_btn.clicked.connect(lambda checked, cid=core.core_id: self._apply_single(cid))
            self._table.setCellWidget(row, 4, apply_btn)

        self._refresh_writes()
        if self._smu:
            self._read_all_co()

    def _on_dry_run_toggled(self, checked: bool) -> None:
        if self._smu:
            self._smu.dry_run = checked

        dry_style = (
            f"QPushButton {{ border: 2px dashed {theme.COLOR_ORANGE}; color: {theme.COLOR_ORANGE}; }}"
            if checked
            else ""
        )
        self._apply_all_btn.setStyleSheet(dry_style)
        self._reset_btn.setStyleSheet(dry_style)
        self._apply_all_btn.setText(f"{SET_ALL} (practice)" if checked else SET_ALL)
        self._reset_btn.setText(f"{SET_ZERO} (practice)" if checked else SET_ZERO)

    def _confirm_co_write(self, detail: str) -> bool:
        practice = " (practice mode, nothing is written)" if self._dry_run_cb.isChecked() else ""
        reply = QMessageBox.warning(
            self,
            f"Write Curve Optimizer values{practice}",
            f"{detail}\n\n"
            "They last until the next reboot and the BIOS settings do not change. "
            "A value that is too negative can crash the machine until it reboots.\n\n"
            "Write them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _read_all_co(self) -> None:
        if not self._smu or not self._topology:
            return
        if core_map_blocked(self._smu) is not None:
            return

        max_retries = 2
        for core_id, row in self._core_row_map().items():
            val = self._smu.get_co_offset(core_id)
            if val is None:
                for _ in range(max_retries):
                    val = self._smu.get_co_offset(core_id)
                    if val is not None:
                        break
            text = str(val) if val is not None else UNREADABLE
            self._table.setItem(row, 2, _item(text))
            if val is not None and core_id in self._spinboxes:
                self._spinboxes[core_id].setValue(val)

    def _refresh_writes(self) -> None:
        can_write = (
            not self._tuner_active
            and self._smu is not None
            and self._commands is not None
            and self._commands.has_co
            and core_map_blocked(self._smu) is None
        )
        self._apply_all_btn.setEnabled(can_write)
        self._reset_btn.setEnabled(can_write)
        self._restore_btn.setEnabled(can_write and self._smu.has_backup())
        for row in range(self._table.rowCount()):
            button = self._table.cellWidget(row, 4)
            if button is not None:
                button.setEnabled(can_write)
        for spin in self._spinboxes.values():
            spin.setEnabled(not self._tuner_active)
        self._apply_all_btn.setToolTip("Off while the Auto-Tuner runs" if self._tuner_active else "")

    def _refused_while_tuning(self) -> bool:
        if self._tuner_active:
            QMessageBox.warning(
                self, "Tuner running", "The Auto-Tuner is writing these values now. Writes wait until it stops."
            )
        return self._tuner_active

    def _write(self, action) -> None:
        try:
            with co_writer():
                action()
        except WriterBusy as busy:
            QMessageBox.warning(self, "Tuner running", f"{busy}: writes wait until the Auto-Tuner stops.")

    def _apply_single(self, core_id: int) -> None:
        if not self._smu:
            QMessageBox.warning(self, "No driver", "The ryzen_smu driver is not available.")
            return
        if self._refused_while_tuning():
            return

        spin = self._spinboxes.get(core_id)
        if not spin:
            return

        value = spin.value()

        if not self._confirm_co_write(f"Core {core_id} gets {value}."):
            return
        self._write(lambda: self._set_one(core_id, value))

    def _set_one(self, core_id: int, value: int) -> None:
        if self._smu.set_co_offset(core_id, value):
            row = self._core_row_map().get(core_id)
            if row is not None:
                self._table.setItem(row, 2, _item(str(value)))
        else:
            QMessageBox.warning(self, "Write failed", f"Core {core_id} could not be set.")

    def _apply_all_co(self) -> None:
        if not self._smu:
            QMessageBox.warning(self, "No driver", "The ryzen_smu driver is not available.")
            return
        if self._refused_while_tuning():
            return

        summary = ", ".join(f"C{cid} {spin.value()}" for cid, spin in sorted(self._spinboxes.items()))
        if not self._confirm_co_write(f"Every core gets its value to set:\n{summary}"):
            return

        self._apply_all_btn.setEnabled(False)
        self._reset_btn.setEnabled(False)
        try:
            self._write(self._set_all)
        finally:
            self._refresh_writes()

    def _set_all(self) -> None:
        failed = [
            core_id for core_id, spin in self._spinboxes.items() if not self._smu.set_co_offset(core_id, spin.value())
        ]
        if failed:
            QMessageBox.warning(self, "Write failed", f"These cores could not be set: {failed}.")
        else:
            self._read_all_co()
            self._profile_banner.setVisible(False)

    def _reset_all_co(self) -> None:
        if not self._smu:
            return
        if self._refused_while_tuning():
            return

        if not self._confirm_co_write("Every core gets 0."):
            return

        self._apply_all_btn.setEnabled(False)
        self._reset_btn.setEnabled(False)
        try:
            self._write(self._set_zero)
        finally:
            self._refresh_writes()

    def _set_zero(self) -> None:
        if not self._smu.reset_all_co():
            for core_id in self._spinboxes:
                self._smu.set_co_offset(core_id, 0)
        self._read_all_co()

    def _backup_co(self) -> None:
        if not self._smu or not self._topology:
            return

        num_cores = len(self._topology.cores)
        backup = self._smu.backup_co_offsets(num_cores)
        self._refresh_writes()
        QMessageBox.information(
            self,
            "Values remembered",
            f"The values of {len(backup)} cores are kept until the app closes. "
            "Put remembered values back writes them again.",
        )

    def _restore_co(self) -> None:
        if not self._smu or not self._smu.has_backup():
            QMessageBox.warning(self, "Nothing remembered", "Press Remember current values first.")
            return
        if self._refused_while_tuning():
            return

        if not self._confirm_co_write("Every core gets its remembered value back."):
            return
        self._write(self._set_backup)

    def _set_backup(self) -> None:
        ok, failed = self._smu.restore_co_offsets()
        if ok:
            self._read_all_co()
            QMessageBox.information(self, "Values put back", "Every core has its remembered value again.")
        else:
            QMessageBox.warning(self, "Write failed", f"These cores could not be put back: {failed}.")
            self._read_all_co()

    def set_tuner_running(self, running: bool) -> None:
        self._tuner_active = running
        self._refresh_writes()
        if not running and self._smu is not None:
            self._read_all_co()

    def update_current_co(self, core_id: int, offset: int) -> None:
        row_map = self._core_row_map()
        row = row_map.get(core_id)
        if row is not None:
            self._table.setItem(row, 2, _item(str(offset)))

    def _save_co_profile(self) -> None:
        from corecycler.config.settings import save_co_profile

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save values to file",
            str(user_home() / "co-profile.json"),
            "JSON (*.json)",
        )
        if not path:
            return

        offsets = {core_id: spin.value() for core_id, spin in self._spinboxes.items()}
        cpu_model = ""
        if self._topology:
            cpu_model = self._topology.model_name

        try:
            save_co_profile(offsets, Path(path), cpu_model=cpu_model, source="manual")
            QMessageBox.information(self, "Saved", f"The values are saved in {path}.")
        except Exception as e:
            QMessageBox.warning(self, "Not saved", f"The values could not be saved: {e}")

    def _load_co_profile(self) -> None:
        from corecycler.config.settings import load_co_profile

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load values from file",
            str(user_home()),
            "JSON (*.json)",
        )
        if not path:
            return

        try:
            profile = load_co_profile(Path(path))
        except Exception as e:
            QMessageBox.warning(self, "Not loaded", f"The file could not be read: {e}")
            return

        if not profile:
            QMessageBox.warning(self, "Empty", "The file holds no values.")
            return

        self.set_co_profile(profile, Path(path).name)

    def set_co_profile(self, profile: dict[int, int], source: str = "a tuning session") -> None:
        altered: list[str] = []
        for core_id, offset in profile.items():
            spin = self._spinboxes.get(core_id)
            if spin is None:
                altered.append(f"core {core_id}: this CPU has no such core")
                continue
            spin.setValue(offset)
            if spin.value() != offset:
                altered.append(f"core {core_id}: {offset} is outside the allowed values, {spin.value()} is set")
        self._profile_banner.setText(f"Values from {source} are in Value to set. Press {SET_ALL} to write them.")
        self._profile_banner.setVisible(True)
        if altered:
            QMessageBox.warning(
                self,
                "Some values changed",
                "Some values could not be taken as they were:\n" + "\n".join(altered),
            )

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
