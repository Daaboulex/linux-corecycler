"""Curve-Tuner tab: reads and writes each core's Curve Optimizer value and the Curve Shaper through ryzen_smu."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QHideEvent, QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
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
from corecycler.smu.driver import SHAPER_BAND_NAMES, SHAPER_MARGINS, RyzenSMU, core_map_blocked
from corecycler.smu.writer_lock import WriterBusy, co_writer
from corecycler.tuner.live_hw import shaper_read, shaper_words

if TYPE_CHECKING:
    from corecycler.engine.topology import CPUTopology


READ_EVERY_MS = 5000  # equivalent-mutant: a period chosen for the eye, no check can tell 5 s from 5.001 s
SET_CHANGED = "Set changed"
SHAPER_POINTS = ("-5 C", "50 C", "90 C")
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
        self._remembered: list[int] = []
        self._reader = QTimer(self)
        self._reader.timeout.connect(self._read_now)
        self._setup_ui()

        if topology:
            self.set_topology(topology)

    @property
    def smu(self) -> RyzenSMU | None:
        return self._smu

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._read_now()
        self._reader.start(READ_EVERY_MS)

    def hideEvent(self, event: QHideEvent) -> None:
        self._reader.stop()
        super().hideEvent(event)

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
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(["Core", "CCD", "Now on the CPU", "Value to set"])
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for column in (0, 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        co_layout.addWidget(self._table)

        bulk_layout = QHBoxLayout()

        self._fill_btn = QPushButton("Fill from CPU")
        self._fill_btn.setToolTip("Puts the values now on the CPU into Value to set and the Curve Shaper grid")
        self._fill_btn.clicked.connect(self._fill_from_cpu)
        bulk_layout.addWidget(self._fill_btn)

        self._set_changed_btn = QPushButton(SET_CHANGED)
        self._set_changed_btn.setToolTip("Writes each core whose Value to set differs from Now on the CPU")
        self._set_changed_btn.clicked.connect(self._apply_changed)
        bulk_layout.addWidget(self._set_changed_btn)

        self._reset_btn = QPushButton("Set all to 0")
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

        warn = QLabel("Values set here last until the next reboot; the BIOS keeps its own.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {theme.COLOR_ORANGE}; padding: 8px;")
        co_layout.addWidget(warn)

        layout.addWidget(co_group, 1)  # equivalent-mutant: the only stretching item, any factor fills

        self._shaper_group = QGroupBox("Curve Shaper")
        shaper_layout = QHBoxLayout(self._shaper_group)
        grid = QGridLayout()
        for column, point in enumerate(SHAPER_POINTS, start=1):
            grid.addWidget(QLabel(point), 0, column, Qt.AlignmentFlag.AlignCenter)
        low, high = SHAPER_MARGINS
        self._shaper_spins: list[list[QSpinBox]] = []
        for row, band in enumerate(SHAPER_BAND_NAMES, start=1):  # equivalent-mutant: an empty grid row takes no height
            grid.addWidget(QLabel(band), row, 0)
            spins = []
            for column in range(1, len(SHAPER_POINTS) + 1):
                spin = QSpinBox()
                spin.setRange(low, high)
                grid.addWidget(spin, row, column)
                spins.append(spin)
            self._shaper_spins.append(spins)
        shaper_layout.addLayout(grid)
        side = QVBoxLayout()
        shaper_layout.addLayout(side, 1)  # equivalent-mutant: the only stretching item, any factor fills
        self._shaper_label = QLabel("Not read yet")
        self._shaper_label.setWordWrap(True)
        self._shaper_label.setToolTip(
            "Each band's margins at -5 C, 50 C and 90 C as the CPU runs them now; "
            "a Shaper written here differs from the BIOS until the next reboot"
        )
        side.addWidget(self._shaper_label)
        shaper_buttons = QHBoxLayout()
        self._shaper_write_btn = QPushButton("Write Shaper")
        self._shaper_write_btn.clicked.connect(self._write_shaper)
        shaper_buttons.addWidget(self._shaper_write_btn)
        self._shaper_zero_btn = QPushButton("Shaper to 0")
        self._shaper_zero_btn.clicked.connect(self._zero_shaper)
        shaper_buttons.addWidget(self._shaper_zero_btn)
        side.addLayout(shaper_buttons)
        side.addStretch()
        layout.addWidget(self._shaper_group)

        self._spinboxes: dict[int, QSpinBox] = {}

    def set_topology(self, topology: CPUTopology) -> None:
        self._topology = topology

        gen = detect_generation(topology.family, topology.model, topology.model_name)
        self._commands = get_commands(gen)

        smu_available = self._commands is not None and RyzenSMU.is_available()
        has_co = self._commands is not None and self._commands.has_co

        if smu_available and has_co:
            self._smu = RyzenSMU(self._commands)
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
            self._smu = RyzenSMU(self._commands)
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

        self._refresh_writes()
        if self._smu:
            self._fill_from_cpu()

    def _confirm_write(self, what: str, detail: str) -> bool:
        reply = QMessageBox.warning(
            self,
            f"Write {what}",
            f"{detail}\n\n"
            "They last until the next reboot and the BIOS settings do not change. "
            "A value that is too negative can crash the machine until it reboots.\n\n"
            "Write them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _read_now(self) -> tuple[dict[int, int], tuple[tuple[int, int, int], ...] | None]:
        if not self._smu or not self._topology:
            return {}, None
        if core_map_blocked(self._smu) is not None:
            return {}, None

        max_retries = 2
        offsets = {}
        for core_id, row in self._core_row_map().items():
            val = self._smu.get_co_offset(core_id)
            if val is None:
                for _ in range(max_retries):
                    val = self._smu.get_co_offset(core_id)
                    if val is not None:
                        break
            text = str(val) if val is not None else UNREADABLE
            self._table.setItem(row, 2, _item(text))
            if val is not None:
                offsets[core_id] = val
        return offsets, self._read_shaper()

    def _fill_from_cpu(self) -> None:
        offsets, bands = self._read_now()
        for core_id, val in offsets.items():
            if core_id in self._spinboxes:
                self._spinboxes[core_id].setValue(val)
        if offsets:
            self._profile_banner.setVisible(False)
        self._fill_shaper(bands)

    def _read_shaper(self) -> tuple[tuple[int, int, int], ...] | None:
        bands, said = shaper_read(self._smu)
        self._shaper_label.setText(said)
        return bands

    def _fill_shaper(self, bands: tuple[tuple[int, int, int], ...] | None) -> None:
        for row, margins in enumerate(bands or ()):
            for column, margin in enumerate(margins):
                self._shaper_spins[row][column].setValue(margin)

    def _write_shaper(self) -> None:
        self._shaper_write(tuple(tuple(spin.value() for spin in spins) for spins in self._shaper_spins))

    def _zero_shaper(self) -> None:
        self._shaper_write(tuple((0, 0, 0) for _ in SHAPER_BAND_NAMES))

    def _shaper_write(self, bands: tuple[tuple[int, int, int], ...]) -> None:
        if not self._smu:
            QMessageBox.warning(self, "No driver", "The ryzen_smu driver is not available.")
            return
        if self._refused_while_tuning():
            return
        if not self._confirm_write("the Curve Shaper", f"The Curve Shaper becomes: {shaper_words(bands)}."):
            return
        self._write(lambda: self._set_shaper(bands))

    def _set_shaper(self, bands: tuple[tuple[int, int, int], ...]) -> None:
        problem = self._smu.set_curve_shaper(bands)
        if problem is not None:
            QMessageBox.warning(self, "Write failed", f"The Curve Shaper was not set: {problem}.")
        self._fill_shaper(self._read_shaper())

    def _refresh_writes(self) -> None:
        can_write = (
            not self._tuner_active
            and self._smu is not None
            and self._commands is not None
            and self._commands.has_co
            and core_map_blocked(self._smu) is None
        )
        self._set_changed_btn.setEnabled(can_write)
        self._reset_btn.setEnabled(can_write)
        self._restore_btn.setEnabled(can_write and self._smu.has_backup())
        for spin in self._spinboxes.values():
            spin.setEnabled(not self._tuner_active)
        self._set_changed_btn.setToolTip("Off while the Auto-Tuner runs" if self._tuner_active else "")
        can_shape = (
            not self._tuner_active
            and self._smu is not None
            and self._commands is not None
            and self._commands.set_curve_shaper_cmd is not None
        )
        self._shaper_write_btn.setEnabled(can_shape)
        self._shaper_zero_btn.setEnabled(can_shape)
        for spins in self._shaper_spins:
            for spin in spins:
                spin.setEnabled(not self._tuner_active)

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

    def _apply_changed(self) -> None:
        if not self._smu:
            QMessageBox.warning(self, "No driver", "The ryzen_smu driver is not available.")
            return
        if self._refused_while_tuning():
            return

        now, _bands = self._read_now()
        changed = {
            core_id: spin.value()
            for core_id, spin in sorted(self._spinboxes.items())
            if now.get(core_id) != spin.value()
        }
        if not changed:
            QMessageBox.information(self, "Nothing to set", "Every core is already at its Value to set.")
            return
        summary = ", ".join(f"C{core} {now.get(core, UNREADABLE)} to {value}" for core, value in changed.items())
        if not self._confirm_write("Curve Optimizer values", f"These cores change: {summary}"):
            return

        self._write(lambda: self._set_changed(changed))

    def _set_changed(self, changed: dict[int, int]) -> None:
        failed = [core_id for core_id, value in changed.items() if not self._smu.set_co_offset(core_id, value)]
        self._read_now()
        if failed:
            QMessageBox.warning(self, "Write failed", f"These cores could not be set: {failed}.")
        else:
            self._profile_banner.setVisible(False)

    def _reset_all_co(self) -> None:
        if not self._smu:
            return
        if self._refused_while_tuning():
            return

        if not self._confirm_write("Curve Optimizer values", "Every core gets 0."):
            return

        self._write(self._set_zero)

    def _set_zero(self) -> None:
        failed = []
        if not self._smu.reset_all_co():
            failed = [core_id for core_id in self._spinboxes if not self._smu.set_co_offset(core_id, 0)]
        self._fill_from_cpu()
        if failed:
            QMessageBox.warning(self, "Write failed", f"These cores could not be set to 0: {failed}.")

    def _backup_co(self) -> None:
        if not self._smu or not self._topology:
            return

        num_cores = len(self._topology.cores)
        backup = self._smu.backup_co_offsets(num_cores)
        self._remembered = sorted(backup)
        self._refresh_writes()
        unread = sorted(set(self._topology.cores) - set(backup))
        missed = f"Cores {unread} could not be read and are not kept. " if unread else ""
        QMessageBox.information(
            self,
            "Values remembered",
            f"The values of cores {self._remembered} are kept until the app closes. {missed}"
            "Put remembered values back writes them again.",
        )

    def _restore_co(self) -> None:
        if not self._smu or not self._smu.has_backup():
            QMessageBox.warning(self, "Nothing remembered", "Press Remember current values first.")
            return
        if self._refused_while_tuning():
            return

        if not self._confirm_write("Curve Optimizer values", "Every core gets its remembered value back."):
            return
        self._write(self._set_backup)

    def _set_backup(self) -> None:
        ok, failed = self._smu.restore_co_offsets()
        if ok:
            self._fill_from_cpu()
            again = f"Cores {self._remembered} have their remembered values again."
            QMessageBox.information(self, "Values put back", again)
        else:
            QMessageBox.warning(self, "Write failed", f"These cores could not be put back: {failed}.")
            self._fill_from_cpu()

    def set_tuner_running(self, running: bool) -> None:
        self._tuner_active = running
        self._refresh_writes()
        if not running and self._smu is not None:
            self._fill_from_cpu()

    def read_core(self, core_id: int) -> None:
        row = self._core_row_map().get(core_id)
        if row is None or self._smu is None or core_map_blocked(self._smu) is not None:
            return
        value = self._smu.get_co_offset(core_id)
        self._table.setItem(row, 2, _item(UNREADABLE if value is None else str(value)))

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
        self._profile_banner.setText(f"Values from {source} are in Value to set. Press {SET_CHANGED} to write them.")
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
