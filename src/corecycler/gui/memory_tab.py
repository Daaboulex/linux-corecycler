"""Memory information tab — DIMM details and DDR5 temperature monitoring."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from PySide6.QtCore import QThread, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
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

from corecycler.config import tools
from corecycler.engine import execution
from corecycler.engine.backends.base import OutcomeKind
from corecycler.engine.backends.stress_ng import StressNgBackend
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.engine.heavy_test_lock import HeavyTestRefused, take_heavy_test_lock
from corecycler.engine.memory_budget import memory_budget
from corecycler.gui.style import theme
from corecycler.monitor.memory import DIMMInfo, SPD5118Reader, read_dimm_info
from corecycler.smu.pmtable import compute_fclk_uclk_ratio

if TYPE_CHECKING:
    from corecycler.monitor.tick import Tick

log = logging.getLogger(__name__)

PART_NUMBER_COL = 6


PASS = "PASS"
FAIL = "FAIL"
STOPPED = "Stopped"
NO_VERDICT = "No verdict"


def memory_verdict(tool: str, output: str, returncode: int) -> tuple[str, str]:
    backend = StressapptestBackend() if tool == "stressapptest" else StressNgBackend()
    findings = backend.findings(output, Path())
    silicon = [signal.text for signal in findings if signal.outcome.kind is OutcomeKind.SILICON]
    if silicon:
        return FAIL, "\n".join(silicon)
    if findings:
        return NO_VERDICT, "\n".join(signal.text for signal in findings)
    clean = "Status: PASS" in output if tool == "stressapptest" else returncode == 0
    if clean:
        return PASS, output
    ended = f"killed by signal {-returncode}" if returncode < 0 else f"exit {returncode}"
    return NO_VERDICT, f"{output}\n{tool} ended with {ended} and reported no result"


def _dimm_temperature(address: str, temp: float | None) -> str:
    return f"DIMM at {address}: unread" if temp is None else f"DIMM at {address}: {temp:.1f}C"


def _dimm_summary(dimms: list[DIMMInfo]) -> str:
    total_gb = sum(d.size_gb for d in dimms)
    types = "/".join(sorted({d.mem_type for d in dimms if d.mem_type})) or "type unread"
    speeds = "/".join(f"{s} MT/s" for s in sorted({d.configured_speed_mt for d in dimms if d.configured_speed_mt}))
    widths = [(d.data_width, d.total_width) for d in dimms if d.data_width and d.total_width]
    ecc = "ECC unread" if not widths else "ECC" if any(total > data for data, total in widths) else "Non-ECC"
    ranks = "/".join(f"{r}R" for r in sorted({d.rank for d in dimms if d.rank}))
    return " ".join(part for part in (f"{len(dimms)} DIMMs | {total_gb} GB", types, speeds, ecc, ranks) if part)


def _spd_unread(reader: SPD5118Reader) -> str:
    if not reader.is_available():
        return "no spd5118 sensor is loaded"
    if not reader.eeprom_count:
        return "the spd5118 sensors expose no EEPROM"
    return "no SPD EEPROM could be read as DDR5"


def _clock(name: str, mhz: float) -> tuple[str, str]:
    return (f"{name}: {mhz:.0f} MHz", "") if mhz > 0 else (f"{name}: --", f"color: {theme.COLOR_MUTED};")


class _StressWorker(QThread):
    """Runs the memory stress test in the background and lets the shared heavy-test lock go when the run ends."""

    done = Signal(str, str)

    def __init__(self, tool: str, duration_minutes: int, lock: BinaryIO, parent=None) -> None:
        super().__init__(parent)
        self._tool = tool
        self._duration = duration_minutes
        self._lock = lock
        self._process: subprocess.Popen | None = None
        self._stopped = False

    def run(self) -> None:
        import contextlib
        import os
        import signal as sig

        try:
            seconds = self._duration * 60
            window = seconds + 60  # equivalent-mutant: the grace past the tool's own timeout is a choice
            if self._tool == "stressapptest":
                budget = memory_budget(1, StressapptestBackend.minimum_memory_mb(os.cpu_count() or 1))
                if not budget.fits:
                    self.done.emit(NO_VERDICT, f"Not enough free memory for stressapptest: {budget.describe()}")
                    return
                cmd = [
                    tools.command_name("stressapptest"),
                    "-W",
                    "-M",
                    str(budget.per_instance_mb),
                    "-s",
                    str(seconds),
                ]
            elif self._tool == "stress-ng --vm":
                cmd = [
                    tools.command_name("stress-ng"),
                    "--vm",
                    "1",
                    "--vm-bytes",
                    "75%",
                    "--verify",
                    "--timeout",
                    f"{seconds}s",
                ]
            else:
                self.done.emit(NO_VERDICT, f"Unknown tool: {self._tool}")
                return

            if self._stopped:
                self.done.emit(STOPPED, "")
                return
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=execution.make_preexec(),
            )
            if self._stopped:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(os.getpgid(self._process.pid), sig.SIGKILL)
            overran = False
            try:
                stdout, stderr = self._process.communicate(timeout=window)
            except subprocess.TimeoutExpired:
                overran = True
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(os.getpgid(self._process.pid), sig.SIGKILL)
                stdout, stderr = self._process.communicate()

            output = stdout + stderr
            if self._stopped:
                self.done.emit(STOPPED, output)
            elif overran:
                self.done.emit(NO_VERDICT, f"{self._tool} did not stop within {window} s and was killed")
            else:
                self.done.emit(*memory_verdict(self._tool, output, self._process.returncode))
        except Exception as e:
            self.done.emit(NO_VERDICT, str(e))
        finally:
            self._lock.close()

    def stop(self) -> None:
        self._stopped = True
        proc = self._process
        if proc is None or proc.poll() is not None:
            return
        import contextlib
        import os
        import signal as sig

        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), sig.SIGKILL)


class MemoryTab(QWidget):
    """Memory information tab showing DIMM details and live temperatures."""

    memory_stress_started = Signal()
    memory_stress_done = Signal()

    def __init__(self, pm_available: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dimms: list[DIMMInfo] = []
        self._spd_reader = SPD5118Reader()
        self._pm_available = pm_available
        self._stress_worker: _StressWorker | None = None
        self._external_test_running: bool = False
        self._setup_ui()
        self._load_dimm_info()
        self._update_spd_labels()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._mc_group = QGroupBox("Memory Controller (PM Table)")
        mc_layout = QVBoxLayout(self._mc_group)

        clk_row = QHBoxLayout()
        self._fclk_label = QLabel("FCLK: --")
        self._fclk_label.setFont(QFont("monospace", 10))
        self._uclk_label = QLabel("UCLK: --")
        self._uclk_label.setFont(QFont("monospace", 10))
        self._mclk_label = QLabel("MCLK: --")
        self._mclk_label.setFont(QFont("monospace", 10))
        self._ratio_label = QLabel("FCLK:UCLK --")
        self._ratio_label.setFont(QFont("monospace", 10, QFont.Weight.Bold))
        clk_row.addWidget(self._fclk_label)
        clk_row.addWidget(self._uclk_label)
        clk_row.addWidget(self._mclk_label)
        clk_row.addWidget(self._ratio_label)
        clk_row.addStretch()
        mc_layout.addLayout(clk_row)

        volt_row = QHBoxLayout()
        self._vdd_label = QLabel("VDD: --")
        self._vdd_label.setFont(QFont("monospace", 10))
        self._vddq_label = QLabel("VDDQ: --")
        self._vddq_label.setFont(QFont("monospace", 10))
        volt_row.addWidget(self._vdd_label)
        volt_row.addWidget(self._vddq_label)
        volt_row.addStretch()
        mc_layout.addLayout(volt_row)

        self._cal_label = QLabel("")
        self._cal_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 9px monospace;")
        mc_layout.addWidget(self._cal_label)

        self._mc_missing_label = QLabel("Requires ryzen_smu driver")
        self._mc_missing_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 10px monospace; padding: 8px;")
        self._mc_missing_label.setVisible(False)
        mc_layout.addWidget(self._mc_missing_label)

        if not self._pm_available:
            self._fclk_label.setVisible(False)
            self._uclk_label.setVisible(False)
            self._mclk_label.setVisible(False)
            self._ratio_label.setVisible(False)
            self._vdd_label.setVisible(False)
            self._vddq_label.setVisible(False)
            self._cal_label.setVisible(False)
            self._mc_missing_label.setVisible(True)

        layout.addWidget(self._mc_group)

        self._spd_group = QGroupBox("SPD Timings — JEDEC Base Profile (DDR5)")
        self._spd_group.setToolTip(
            "JEDEC base profile timings from SPD EEPROM. XMP/EXPO overclocking profiles are not stored in SPD."
        )
        spd_layout = QVBoxLayout(self._spd_group)
        self._primary_label = QLabel("Primary: --")
        self._primary_label.setFont(QFont("monospace", 10))
        self._secondary_label = QLabel("Secondary: --")
        self._secondary_label.setFont(QFont("monospace", 10))
        self._spd_unavailable_label = QLabel("")
        self._spd_unavailable_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 10px monospace; padding: 4px;")
        self._spd_unavailable_label.setVisible(False)
        spd_layout.addWidget(self._primary_label)
        spd_layout.addWidget(self._secondary_label)
        spd_layout.addWidget(self._spd_unavailable_label)
        layout.addWidget(self._spd_group)

        self._summary_label = QLabel("Loading DIMM information...")
        self._summary_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        self._summary_label.setWordWrap(True)
        layout.addWidget(self._summary_label)

        self._deps_label = QLabel("")
        self._deps_label.setStyleSheet(f"color: {theme.COLOR_MUTED}; font: 9px monospace;")
        self._deps_label.setWordWrap(True)
        layout.addWidget(self._deps_label)

        self._temp_group = QGroupBox("DIMM Temperatures (SPD5118)")
        QHBoxLayout(self._temp_group)
        self._temp_labels: list[QLabel] = []
        self._temp_group.setVisible(False)
        layout.addWidget(self._temp_group)

        self._dimm_table = QTableWidget()
        self._dimm_table.setColumnCount(12)
        self._dimm_table.setHorizontalHeaderLabels(
            [
                "Slot",
                "Size",
                "Type",
                "SPD Speed",
                "Running",
                "Manufacturer",
                "Part Number",
                "Serial",
                "Rank",
                "Form",
                "SPD Rated V",
                "Width",
            ]
        )
        header = self._dimm_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PART_NUMBER_COL, QHeaderView.ResizeMode.Stretch)
        self._dimm_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._dimm_table)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._load_dimm_info)
        layout.addWidget(refresh_btn)

        stress_group = QGroupBox("Memory Stress Test")
        stress_layout = QHBoxLayout(stress_group)

        stress_layout.addWidget(QLabel("Duration:"))
        self._stress_duration = QSpinBox()
        self._stress_duration.setRange(1, 60)
        self._stress_duration.setValue(5)
        self._stress_duration.setSuffix(" min")
        stress_layout.addWidget(self._stress_duration)

        stress_layout.addWidget(QLabel("Tool:"))
        self._stress_tool = QComboBox()
        self._refill_tools()
        stress_layout.addWidget(self._stress_tool)

        self._stress_btn = QPushButton("Run")
        self._stress_btn.clicked.connect(self._run_memory_stress)
        stress_layout.addWidget(self._stress_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            f"QPushButton {{ background: {theme.BTN_RED}; color: white; padding: 4px 10px; "
            f"border-radius: 3px; }} QPushButton:disabled {{ background: {theme.COLOR_MUTED_DARKER}; "
            f"color: {theme.COLOR_MUTED}; }}"
        )
        self._stop_btn.clicked.connect(self._stop_memory_stress)
        stress_layout.addWidget(self._stop_btn)

        self._stress_status = QLabel("")
        stress_layout.addWidget(self._stress_status)
        stress_layout.addStretch()

        layout.addWidget(stress_group)

    def _detect_available_tools(self) -> list[str]:
        """Detect which memory stress tools are installed."""
        available = []
        if tools.resolve("stressapptest").path:
            available.append("stressapptest")
        if tools.resolve("stress-ng").path:
            available.append("stress-ng --vm")
        if not available:
            available.append("(none installed)")
        return available

    def _load_dimm_info(self) -> None:
        self._dimms, why = read_dimm_info()
        self._populate_table()
        self._summary_label.setText(_dimm_summary(self._dimms) if self._dimms else f"No DIMM info: {why}")

        if self._spd_reader.is_available():
            temps = self._spd_reader.read_temperatures()
            self._temp_group.setVisible(True)
            temp_layout = self._temp_group.layout()
            for lbl in self._temp_labels:
                lbl.deleteLater()
            self._temp_labels.clear()
            for address, temp in zip(self._spd_reader.addresses, temps, strict=True):
                lbl = QLabel(_dimm_temperature(address, temp))
                lbl.setFont(QFont("monospace", 10))
                lbl.setStyleSheet("padding: 4px;")
                temp_layout.addWidget(lbl)
                self._temp_labels.append(lbl)

        deps = []
        for key in ("dmidecode", "stressapptest", "stress-ng"):
            deps.append(f"{key}: " + ("found" if tools.resolve(key).path else "missing"))
        deps.append("spd5118: " + ("available" if self._spd_reader.is_available() else "not loaded"))
        deps.append("ryzen_smu: " + ("available" if self._pm_available else "no PM table when the app started"))
        self._deps_label.setText("  |  ".join(deps))
        self._refill_tools()

    def _refill_tools(self) -> None:
        chosen = self._stress_tool.currentText()
        self._stress_tool.clear()
        self._stress_tool.addItems(self._detect_available_tools())
        self._stress_tool.setCurrentIndex(max(self._stress_tool.findText(chosen), 0))

    def _update_spd_labels(self) -> None:
        """Populate SPD timing labels from cached EEPROM data."""
        spd = self._spd_reader.spd_timings
        if spd is None:
            self._primary_label.setVisible(False)
            self._secondary_label.setVisible(False)
            self._spd_unavailable_label.setText(f"SPD Timings unavailable \u2014 {_spd_unread(self._spd_reader)}")
            self._spd_unavailable_label.setVisible(True)
            self._spd_group.setTitle("SPD Timings — JEDEC Base Profile (DDR5)")
            return

        self._primary_label.setVisible(True)
        self._secondary_label.setVisible(True)
        self._spd_unavailable_label.setVisible(False)

        self._spd_group.setTitle(f"SPD Timings — JEDEC Base Profile (DDR5) (DIMM at {spd.source})")

        self._primary_label.setText(f"Primary: {spd.tCL}-{spd.tRCD}-{spd.tRP}-{spd.tRAS}-{spd.tRC}")

        parts = []
        parts.append(f"tRFC1: {spd.tRFC1_ns}ns")
        parts.append(f"tRFCsb: {spd.tRFCsb_ns}ns")
        parts.append(f"tWR: {spd.tWR_ns:.0f}ns")
        self._secondary_label.setText("Secondary: " + "  ".join(parts))

    def _populate_table(self) -> None:
        self._dimm_table.setRowCount(len(self._dimms))
        for row, d in enumerate(self._dimms):
            items = [
                f"{d.locator} ({d.bank_locator})" if d.bank_locator else d.locator,
                f"{d.size_gb} GB",
                d.mem_type or "-",
                f"{d.speed_mt} MT/s" if d.speed_mt else "-",
                f"{d.configured_speed_mt} MT/s" if d.configured_speed_mt else "-",
                d.manufacturer or "-",
                d.part_number or "-",
                d.serial_number or "-",
                str(d.rank) if d.rank else "-",
                d.form_factor or "-",
                f"{d.configured_voltage:.2f}V" if d.configured_voltage else "-",
                f"{d.data_width}/{d.total_width} bit" if d.data_width else "-",
            ]
            for col, text in enumerate(items):
                self._dimm_table.setItem(row, col, QTableWidgetItem(text))

    def _update_temperatures(self) -> None:
        temps = self._spd_reader.read_temperatures()
        for label, address, temp in zip(self._temp_labels, self._spd_reader.addresses, temps, strict=True):
            label.setText(_dimm_temperature(address, temp))

    def on_tick(self, tick: Tick) -> None:
        if self._pm_available:
            pm_data = tick.pm
            if pm_data is not None and pm_data.is_calibrated:
                self._update_clock_labels(pm_data)
                self._update_voltage_labels(pm_data)
                if pm_data.offsets_verified:
                    self._cal_label.setText(f"PM Table v{pm_data.pm_table_version:#010x} \u2014 Verified")
                else:
                    self._cal_label.setText(
                        f"PM Table v{pm_data.pm_table_version:#010x} \u2014 Calibrated (community-sourced, unverified)"
                    )
            elif pm_data is not None:
                self._show_uncalibrated(pm_data)
            else:
                self._set_clocks_unavailable()

        if self._spd_reader.is_available():
            self._update_temperatures()

    def _update_clock_labels(self, pm_data) -> None:
        for label, name, mhz in (
            (self._fclk_label, "FCLK", pm_data.fclk_mhz),
            (self._uclk_label, "UCLK", pm_data.uclk_mhz),
            (self._mclk_label, "MCLK", pm_data.mclk_mhz),
        ):
            text, style = _clock(name, mhz)
            label.setText(text)
            label.setStyleSheet(style)
        ratio = compute_fclk_uclk_ratio(pm_data.fclk_mhz, pm_data.uclk_mhz)
        if ratio is not None:
            self._ratio_label.setText(f"FCLK:UCLK {ratio[0]}:{ratio[1]}")
            if ratio == (1, 1):
                self._ratio_label.setStyleSheet(f"color: {theme.COLOR_PASS};")
            else:
                self._ratio_label.setStyleSheet(f"color: {theme.COLOR_WARN_SOFT};")
        else:
            self._ratio_label.setText("FCLK:UCLK --")
            self._ratio_label.setStyleSheet("")

    def _update_voltage_labels(self, pm_data) -> None:
        if pm_data.vdd_mem_v > 0:
            self._vdd_label.setText(f"VDD: {pm_data.vdd_mem_v:.3f}V")
            self._vdd_label.setStyleSheet("")
        else:
            self._vdd_label.setText("VDD: --")
            self._vdd_label.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        if pm_data.vddq_v > 0:
            self._vddq_label.setText(f"VDDQ: {pm_data.vddq_v:.3f}V")
            self._vddq_label.setStyleSheet("")
        else:
            self._vddq_label.setText("VDDQ: --")
            self._vddq_label.setStyleSheet(f"color: {theme.COLOR_MUTED};")

    def _show_uncalibrated(self, pm_data) -> None:
        self._fclk_label.setText("FCLK: --")
        self._uclk_label.setText("UCLK: --")
        self._mclk_label.setText("MCLK: --")
        self._ratio_label.setText("FCLK:UCLK --")
        self._ratio_label.setStyleSheet("")
        self._vdd_label.setText("VDD: --")
        self._vddq_label.setText("VDDQ: --")
        for lbl in (self._fclk_label, self._uclk_label, self._mclk_label, self._vdd_label, self._vddq_label):
            lbl.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        self._cal_label.setText(
            f"PM Table v{pm_data.pm_table_version:#010x} \u2014 Uncalibrated ({len(pm_data.raw_floats)} floats)"
        )

    def _set_clocks_unavailable(self) -> None:
        for lbl in (self._fclk_label, self._uclk_label, self._mclk_label, self._vdd_label, self._vddq_label):
            lbl.setText(lbl.text().split(":")[0] + ": --")
            lbl.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        self._ratio_label.setText("FCLK:UCLK --")
        self._ratio_label.setStyleSheet(f"color: {theme.COLOR_MUTED};")
        self._cal_label.setText("")

    def set_test_running(self, running: bool) -> None:
        """Disable memory stress when another test is active."""
        self._external_test_running = running
        self._stress_btn.setEnabled(not running and not (self._stress_worker and self._stress_worker.isRunning()))

    def _run_memory_stress(self) -> None:
        if self._stress_worker is not None and self._stress_worker.isRunning():
            return
        tool = self._stress_tool.currentText()
        if tool == "(none installed)":
            QMessageBox.warning(
                self,
                "Not Found",
                "No memory stress tools installed.\nInstall stressapptest or stress-ng.",
            )
            return
        try:
            lock = take_heavy_test_lock("corecycler-gui")
        except HeavyTestRefused as refused:
            QMessageBox.warning(self, "Cannot Start", f"Cannot start: {refused}")
            return
        duration = self._stress_duration.value()
        self._stress_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._stress_duration.setEnabled(False)
        self._stress_tool.setEnabled(False)
        self._stress_status.setText(f"Running {tool} for {duration}min...")
        self._stress_worker = _StressWorker(tool, duration, lock, parent=self)
        self._stress_worker.done.connect(self._on_stress_done)
        self.memory_stress_started.emit()
        self._stress_worker.start()

    def _stop_memory_stress(self) -> None:
        """Stop the running memory stress test."""
        self._stop_btn.setEnabled(False)
        if self._stress_worker and self._stress_worker.isRunning():
            self._stress_status.setText("Stopping...")
            self._stress_worker.stop()

    def force_stop(self) -> None:
        """Kill any running memory stress test — called on app exit."""
        if self._stress_worker and self._stress_worker.isRunning():
            self._stress_worker.stop()
            self._stress_worker.wait(3000)
            if self._stress_worker.isRunning():
                self._stress_worker.terminate()

    @Slot(str, str)
    def _on_stress_done(self, verdict: str, output: str) -> None:
        self._stress_btn.setEnabled(not self._external_test_running)
        self._stop_btn.setEnabled(False)
        self._stress_duration.setEnabled(True)
        self._stress_tool.setEnabled(True)
        self._stress_status.setText(f"Result: {verdict}")
        self.memory_stress_done.emit()
        if verdict == STOPPED:
            return
        clean_output = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", output[-500:])
        QMessageBox.information(self, f"Memory Stress: {verdict}", clean_output)
