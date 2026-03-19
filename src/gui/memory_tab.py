"""Memory information tab — DIMM details and DDR5 temperature monitoring."""

from __future__ import annotations

import logging
import shutil
import subprocess

from PySide6.QtCore import QThread, QTimer, Signal, Slot
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

from config.settings import load_settings
from monitor.memory import DIMMInfo, SPD5118Reader, SPDTimingData, read_dimm_info
from smu.pmtable import PMTableReader, compute_fclk_uclk_ratio

log = logging.getLogger(__name__)

PART_NUMBER_COL = 6


class _StressWorker(QThread):
    """Runs memory stress test in background."""
    done = Signal(bool, str)

    def __init__(self, tool: str, duration_minutes: int, parent=None) -> None:
        super().__init__(parent)
        self._tool = tool
        self._duration = duration_minutes
        self._process: subprocess.Popen | None = None

    def run(self) -> None:
        import contextlib
        import os
        import signal as sig
        try:
            seconds = self._duration * 60
            if self._tool == "stressapptest":
                # Limit to 75% of free memory to avoid "freepages < neededpages"
                free_mb = _get_free_memory_mb()
                mem_mb = max(256, int(free_mb * 0.75)) if free_mb else 1024
                cmd = ["stressapptest", "-W", "-M", str(mem_mb), "-s", str(seconds)]
            elif self._tool == "stress-ng --vm":
                cmd = ["stress-ng", "--vm", "1", "--vm-bytes", "75%", "--verify", "--timeout", f"{seconds}s"]
            else:
                self.done.emit(False, f"Unknown tool: {self._tool}")
                return

            def _make_preexec():
                os.setsid()
                # PR_SET_PDEATHSIG: kernel sends SIGKILL to this process if parent dies
                import ctypes
                import ctypes.util
                libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
                libc.prctl(1, sig.SIGKILL)  # PR_SET_PDEATHSIG

            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, preexec_fn=_make_preexec,
            )
            try:
                stdout, stderr = self._process.communicate(timeout=seconds + 60)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(os.getpgid(self._process.pid), sig.SIGKILL)
                stdout, stderr = self._process.communicate()

            if self._tool == "stressapptest":
                passed = "Status: PASS" in stdout
            else:
                passed = self._process.returncode in (0, -9, -15, 137, 143)
            output = (stdout + stderr)[-500:]
            self.done.emit(passed, output)
        except Exception as e:
            self.done.emit(False, str(e))

    def stop(self) -> None:
        """Kill the running stress process and its entire process group.

        Uses SIGTERM -> wait(3) -> SIGKILL -> wait(2) escalation pattern
        matching CoreScheduler._kill_current().
        """
        import contextlib
        import os
        import signal as sig
        proc = self._process
        if proc is None or proc.poll() is not None:
            return

        pid = proc.pid
        try:
            pgid = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            with contextlib.suppress(Exception):
                proc.wait(timeout=1)
            return

        # SIGTERM the whole process group
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(pgid, sig.SIGTERM)

        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Escalate to SIGKILL
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(pgid, sig.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)


def _get_free_memory_mb() -> int | None:
    """Read available memory from /proc/meminfo in MB."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024  # kB → MB
    except (OSError, ValueError):
        pass
    return None


class MemoryTab(QWidget):
    """Memory information tab showing DIMM details and live temperatures."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._dimms: list[DIMMInfo] = []
        self._spd_reader = SPD5118Reader()
        self._pm_reader = PMTableReader()
        self._stress_worker: _StressWorker | None = None
        self._setup_ui()
        self._load_dimm_info()
        self._update_spd_labels()

        # Unified timer for PM table + DIMM temperature polling
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_live_data)
        if self._pm_reader.is_available() or self._spd_reader.is_available():
            settings = load_settings()
            self._update_timer.start(int(settings.poll_interval * 1000))

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Memory Controller group box (PM Table data)
        self._mc_group = QGroupBox("Memory Controller (PM Table)")
        mc_layout = QVBoxLayout(self._mc_group)

        # Clock row: FCLK, UCLK, MCLK, ratio
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

        # Voltage row: VDD, VDDQ
        volt_row = QHBoxLayout()
        self._vdd_label = QLabel("VDD: --")
        self._vdd_label.setFont(QFont("monospace", 10))
        self._vddq_label = QLabel("VDDQ: --")
        self._vddq_label.setFont(QFont("monospace", 10))
        volt_row.addWidget(self._vdd_label)
        volt_row.addWidget(self._vddq_label)
        volt_row.addStretch()
        mc_layout.addLayout(volt_row)

        # Calibration status
        self._cal_label = QLabel("")
        self._cal_label.setStyleSheet("color: #888; font: 9px monospace;")
        mc_layout.addWidget(self._cal_label)

        # Driver-missing message (hidden by default)
        self._mc_missing_label = QLabel("Requires ryzen_smu driver")
        self._mc_missing_label.setStyleSheet("color: #888; font: 10px monospace; padding: 8px;")
        self._mc_missing_label.setVisible(False)
        mc_layout.addWidget(self._mc_missing_label)

        if not self._pm_reader.is_available():
            # Hide clock/voltage rows, show driver-missing message
            self._fclk_label.setVisible(False)
            self._uclk_label.setVisible(False)
            self._mclk_label.setVisible(False)
            self._ratio_label.setVisible(False)
            self._vdd_label.setVisible(False)
            self._vddq_label.setVisible(False)
            self._cal_label.setVisible(False)
            self._mc_missing_label.setVisible(True)

        layout.addWidget(self._mc_group)

        # SPD Timings group box (DDR5 EEPROM data, cached at startup)
        self._spd_group = QGroupBox("SPD Timings (DDR5)")
        spd_layout = QVBoxLayout(self._spd_group)
        self._primary_label = QLabel("Primary: --")
        self._primary_label.setFont(QFont("monospace", 10))
        self._secondary_label = QLabel("Secondary: --")
        self._secondary_label.setFont(QFont("monospace", 10))
        self._spd_unavailable_label = QLabel("")
        self._spd_unavailable_label.setStyleSheet("color: #888; font: 10px monospace; padding: 4px;")
        self._spd_unavailable_label.setVisible(False)
        spd_layout.addWidget(self._primary_label)
        spd_layout.addWidget(self._secondary_label)
        spd_layout.addWidget(self._spd_unavailable_label)
        layout.addWidget(self._spd_group)

        self._summary_label = QLabel("Loading DIMM information...")
        self._summary_label.setFont(QFont("monospace", 11, QFont.Weight.Bold))
        layout.addWidget(self._summary_label)

        # Dependency status
        self._deps_label = QLabel("")
        self._deps_label.setStyleSheet("color: #888; font: 9px monospace;")
        layout.addWidget(self._deps_label)

        self._temp_group = QGroupBox("DIMM Temperatures (SPD5118)")
        temp_layout = QHBoxLayout(self._temp_group)
        self._temp_labels: list[QLabel] = []
        self._temp_group.setVisible(False)
        layout.addWidget(self._temp_group)

        self._dimm_table = QTableWidget()
        self._dimm_table.setColumnCount(12)
        self._dimm_table.setHorizontalHeaderLabels([
            "Slot", "Size", "Type", "SPD Speed", "Running",
            "Manufacturer", "Part Number", "Serial", "Rank",
            "Form", "SPD Rated V", "Width",
        ])
        header = self._dimm_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PART_NUMBER_COL, QHeaderView.ResizeMode.Stretch)
        self._dimm_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self._dimm_table)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._load_dimm_info)
        layout.addWidget(refresh_btn)

        # Stress test controls
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
        self._stress_tool.addItems(self._detect_available_tools())
        stress_layout.addWidget(self._stress_tool)

        self._stress_btn = QPushButton("Run")
        self._stress_btn.clicked.connect(self._run_memory_stress)
        stress_layout.addWidget(self._stress_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton { background: #b71c1c; color: white; padding: 4px 10px; "
            "border-radius: 3px; } QPushButton:disabled { background: #555; color: #888; }"
        )
        self._stop_btn.clicked.connect(self._stop_memory_stress)
        stress_layout.addWidget(self._stop_btn)

        self._stress_status = QLabel("")
        stress_layout.addWidget(self._stress_status)
        stress_layout.addStretch()

        layout.addWidget(stress_group)

    def _detect_available_tools(self) -> list[str]:
        """Detect which memory stress tools are installed."""
        tools = []
        if shutil.which("stressapptest"):
            tools.append("stressapptest")
        if shutil.which("stress-ng"):
            tools.append("stress-ng --vm")
        if not tools:
            tools.append("(none installed)")
        return tools

    def _load_dimm_info(self) -> None:
        self._dimms = read_dimm_info()
        self._populate_table()

        if self._dimms:
            total_gb = sum(d.size_gb for d in self._dimms)
            types = set(d.mem_type for d in self._dimms)
            speeds = set(d.configured_speed_mt for d in self._dimms if d.configured_speed_mt)
            type_str = "/".join(sorted(types)) if types else "Unknown"
            speed_str = "/".join(f"{s} MT/s" for s in sorted(speeds)) if speeds else ""
            # Detect ECC: total_width > data_width means ECC bits present
            has_ecc = any(d.total_width > d.data_width for d in self._dimms if d.total_width and d.data_width)
            ecc_str = "ECC" if has_ecc else "Non-ECC"
            rank_set = set(d.rank for d in self._dimms if d.rank)
            rank_str = f"{max(rank_set)}R" if rank_set else ""
            self._summary_label.setText(
                f"{len(self._dimms)} DIMMs | {total_gb} GB {type_str} {speed_str} {ecc_str} {rank_str}".rstrip()
            )
        else:
            self._summary_label.setText(
                "No DIMM info available (dmidecode requires root)"
            )

        if self._spd_reader.is_available():
            temps = self._spd_reader.read_temperatures()
            self._temp_group.setVisible(True)
            temp_layout = self._temp_group.layout()
            for lbl in self._temp_labels:
                lbl.deleteLater()
            self._temp_labels.clear()
            for i, temp in enumerate(temps):
                lbl = QLabel(f"DIMM {i}: {temp:.1f}C")
                lbl.setFont(QFont("monospace", 10))
                lbl.setStyleSheet("padding: 4px;")
                temp_layout.addWidget(lbl)
                self._temp_labels.append(lbl)

        # Update dependency status
        deps = []
        deps.append("dmidecode: " + ("found" if shutil.which("dmidecode") else "missing"))
        deps.append("stressapptest: " + ("found" if shutil.which("stressapptest") else "missing"))
        deps.append("stress-ng: " + ("found" if shutil.which("stress-ng") else "missing"))
        deps.append("spd5118: " + ("available" if self._spd_reader.is_available() else "not loaded"))
        deps.append("ryzen_smu: " + ("available" if self._pm_reader.is_available() else "not loaded"))
        self._deps_label.setText("  |  ".join(deps))

    def _update_spd_labels(self) -> None:
        """Populate SPD timing labels from cached EEPROM data."""
        spd = self._spd_reader.spd_timings
        if spd is None:
            self._primary_label.setVisible(False)
            self._secondary_label.setVisible(False)
            self._spd_unavailable_label.setText(
                "SPD Timings unavailable \u2014 spd5118 eeprom not exposed"
            )
            self._spd_unavailable_label.setVisible(True)
            self._spd_group.setTitle("SPD Timings (DDR5)")
            return

        self._primary_label.setVisible(True)
        self._secondary_label.setVisible(True)
        self._spd_unavailable_label.setVisible(False)

        dimm_num = spd.dimm_index + 1
        self._spd_group.setTitle(f"SPD Timings (DDR5) (DIMM {dimm_num})")

        self._primary_label.setText(
            f"Primary: {spd.tCL}-{spd.tRCD}-{spd.tRP}-{spd.tRAS}-{spd.tRC}"
        )

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
                d.mem_type,
                f"{d.speed_mt} MT/s" if d.speed_mt else "-",
                f"{d.configured_speed_mt} MT/s" if d.configured_speed_mt else "-",
                d.manufacturer,
                d.part_number,
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
        for i, temp in enumerate(temps):
            if i < len(self._temp_labels):
                self._temp_labels[i].setText(f"DIMM {i}: {temp:.1f}C")

    def _update_live_data(self) -> None:
        """Read PM table and DIMM temps in one tick."""
        # PM table (controller clocks + voltages)
        if self._pm_reader.is_available():
            pm_data = self._pm_reader.read()
            if pm_data is not None and pm_data.is_calibrated:
                self._update_clock_labels(pm_data)
                self._update_voltage_labels(pm_data)
                self._cal_label.setText(
                    f"PM Table v{pm_data.pm_table_version:#010x} \u2014 Verified"
                )
            elif pm_data is not None:
                self._show_uncalibrated(pm_data)
            else:
                self._set_clocks_unavailable()

        # DIMM temperatures (SPD5118 hwmon)
        if self._spd_reader.is_available():
            self._update_temperatures()

    def _update_clock_labels(self, pm_data) -> None:
        self._fclk_label.setText(f"FCLK: {pm_data.fclk_mhz:.0f} MHz")
        self._uclk_label.setText(f"UCLK: {pm_data.uclk_mhz:.0f} MHz")
        self._mclk_label.setText(f"MCLK: {pm_data.mclk_mhz:.0f} MHz")
        self._fclk_label.setStyleSheet("")
        self._uclk_label.setStyleSheet("")
        self._mclk_label.setStyleSheet("")
        ratio = compute_fclk_uclk_ratio(pm_data.fclk_mhz, pm_data.uclk_mhz)
        if ratio == (1, 1):
            self._ratio_label.setText("FCLK:UCLK 1:1")
            self._ratio_label.setStyleSheet("color: #4caf50;")  # green
        elif ratio == (1, 2):
            self._ratio_label.setText("FCLK:UCLK 1:2")
            self._ratio_label.setStyleSheet("color: #ffb74d;")  # yellow/amber
        else:
            self._ratio_label.setText("FCLK:UCLK ?")
            self._ratio_label.setStyleSheet("color: #ffb74d;")

    def _update_voltage_labels(self, pm_data) -> None:
        if pm_data.vdd_mem_v > 0:
            self._vdd_label.setText(f"VDD: {pm_data.vdd_mem_v:.3f}V")
            self._vdd_label.setStyleSheet("")
        else:
            self._vdd_label.setText("VDD: --")
            self._vdd_label.setStyleSheet("color: #888;")
        self._vddq_label.setText("VDDQ: --")
        self._vddq_label.setStyleSheet("color: #888;")

    def _show_uncalibrated(self, pm_data) -> None:
        self._fclk_label.setText("FCLK: --")
        self._uclk_label.setText("UCLK: --")
        self._mclk_label.setText("MCLK: --")
        self._ratio_label.setText("FCLK:UCLK --")
        self._ratio_label.setStyleSheet("")
        self._vdd_label.setText("VDD: --")
        self._vddq_label.setText("VDDQ: --")
        for lbl in (self._fclk_label, self._uclk_label, self._mclk_label,
                     self._vdd_label, self._vddq_label):
            lbl.setStyleSheet("color: #888;")
        self._cal_label.setText(
            f"PM Table v{pm_data.pm_table_version:#010x} \u2014 Uncalibrated "
            f"({len(pm_data.raw_floats)} floats)"
        )

    def _set_clocks_unavailable(self) -> None:
        for lbl in (self._fclk_label, self._uclk_label, self._mclk_label,
                     self._vdd_label, self._vddq_label):
            lbl.setText(lbl.text().split(":")[0] + ": --")
            lbl.setStyleSheet("color: #888;")
        self._ratio_label.setText("FCLK:UCLK --")
        self._ratio_label.setStyleSheet("color: #888;")
        self._cal_label.setText("")

    def _run_memory_stress(self) -> None:
        tool = self._stress_tool.currentText()
        if tool == "(none installed)":
            QMessageBox.warning(self, "Not Found", "No memory stress tools installed.\nInstall stressapptest or stress-ng.")
            return
        duration = self._stress_duration.value()
        self._stress_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._stress_duration.setEnabled(False)
        self._stress_tool.setEnabled(False)
        self._stress_status.setText(f"Running {tool} for {duration}min...")
        self._stress_worker = _StressWorker(tool, duration, parent=self)
        self._stress_worker.done.connect(self._on_stress_done)
        self._stress_worker.start()

    def _stop_memory_stress(self) -> None:
        """Stop the running memory stress test."""
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

    @Slot(bool, str)
    def _on_stress_done(self, passed: bool, output: str) -> None:
        self._stress_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._stress_duration.setEnabled(True)
        self._stress_tool.setEnabled(True)
        status = "PASS" if passed else "FAIL"
        self._stress_status.setText(f"Result: {status}")
        QMessageBox.information(self, f"Memory Stress: {status}", output[-500:])
