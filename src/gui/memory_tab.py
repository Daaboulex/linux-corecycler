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

from monitor.memory import DIMMInfo, SPD5118Reader, read_dimm_info

log = logging.getLogger(__name__)


class _StressWorker(QThread):
    """Runs memory stress test in background."""
    done = Signal(bool, str)

    def __init__(self, tool: str, duration_minutes: int, parent=None) -> None:
        super().__init__(parent)
        self._tool = tool
        self._duration = duration_minutes
        self._process: subprocess.Popen | None = None

    def run(self) -> None:
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

            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, preexec_fn=os.setsid,
            )
            try:
                stdout, stderr = self._process.communicate(timeout=seconds + 60)
            except subprocess.TimeoutExpired:
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
        """Kill the running stress process and its children."""
        import os
        import signal as sig
        if self._process and self._process.poll() is None:
            try:
                os.killpg(os.getpgid(self._process.pid), sig.SIGTERM)
            except (OSError, ProcessLookupError):
                pass


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
        self._stress_worker: _StressWorker | None = None
        self._setup_ui()
        self._load_dimm_info()

        if self._spd_reader.is_available():
            self._temp_timer = QTimer(self)
            self._temp_timer.timeout.connect(self._update_temperatures)
            self._temp_timer.start(2000)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

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
        self._dimm_table.setColumnCount(10)
        self._dimm_table.setHorizontalHeaderLabels([
            "Slot", "Size", "Type", "Speed", "Configured",
            "Manufacturer", "Part Number", "Rank", "Rated Voltage", "Width",
        ])
        self._dimm_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
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
            self._summary_label.setText(
                f"{len(self._dimms)} DIMMs | {total_gb} GB {type_str} {speed_str}"
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
        self._deps_label.setText("  |  ".join(deps))

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
                str(d.rank) if d.rank else "-",
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
