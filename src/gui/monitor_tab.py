"""Live monitoring tab — frequency, temperature, voltage, power charts."""

from __future__ import annotations

import contextlib

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QGroupBox, QLabel, QVBoxLayout, QWidget

from gui.widgets.charts import LiveChart
from monitor.frequency import read_core_frequencies, read_max_frequency
from monitor.hwmon import HWMonReader
from monitor.power import PowerMonitor


class MonitorTab(QWidget):
    """Live system monitoring with charts."""

    def __init__(self) -> None:
        super().__init__()
        self._hwmon = HWMonReader()
        self._power = PowerMonitor()

        self._setup_ui()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        self._timer.start(1000)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # current values bar
        values_group = QGroupBox("Current Values")
        values_layout = QGridLayout(values_group)

        self._tctl_label = QLabel("Tctl: --°C")
        self._tdie_label = QLabel("Tdie: --°C")
        self._vcore_label = QLabel("Vcore: --V")
        self._power_label = QLabel("Package: --W")
        self._max_freq_label = QLabel("Max Boost: --MHz")

        labels = [
            self._tctl_label, self._tdie_label, self._vcore_label,
            self._power_label, self._max_freq_label,
        ]
        for i, label in enumerate(labels):
            label.setStyleSheet("font: bold 11px monospace; padding: 4px;")
            values_layout.addWidget(label, 0, i)

        layout.addWidget(values_group)

        # charts
        charts_layout = QGridLayout()

        self._freq_chart = LiveChart("Frequency", "MHz", 0, 6000, "#4fc3f7")
        self._temp_chart = LiveChart("Temperature", "°C", 0, 100, "#ff7043")
        self._power_chart = LiveChart("Package Power", "W", 0, 250, "#66bb6a")
        self._voltage_chart = LiveChart("Vcore", "V", 0.5, 1.6, "#ab47bc")

        charts_layout.addWidget(self._freq_chart, 0, 0)
        charts_layout.addWidget(self._temp_chart, 0, 1)
        charts_layout.addWidget(self._power_chart, 1, 0)
        charts_layout.addWidget(self._voltage_chart, 1, 1)

        layout.addLayout(charts_layout)

        # populate max boost
        max_freq = read_max_frequency()
        if max_freq:
            self._max_freq_label.setText(f"Max Boost: {max_freq:.0f}MHz")
            self._freq_chart.max_val = max_freq * 1.1

    def _update(self) -> None:
        with contextlib.suppress(Exception):
            self._do_update()

    def _do_update(self) -> None:
        # frequencies
        freqs = read_core_frequencies()
        if freqs:
            max_freq = max(freqs.values())
            self._freq_chart.add_value(max_freq)

        # hwmon
        hwmon_data = self._hwmon.read()
        if hwmon_data.tctl_c is not None:
            self._tctl_label.setText(f"Tctl: {hwmon_data.tctl_c:.1f}°C")
            self._temp_chart.add_value(hwmon_data.tctl_c)
        if hwmon_data.tdie_c is not None:
            self._tdie_label.setText(f"Tdie: {hwmon_data.tdie_c:.1f}°C")
        if hwmon_data.vcore_v is not None:
            self._vcore_label.setText(f"Vcore: {hwmon_data.vcore_v:.4f}V")
            self._voltage_chart.add_value(hwmon_data.vcore_v)

        # power
        watts = self._power.read_power_watts()
        if watts is not None:
            self._power_label.setText(f"Package: {watts:.1f}W")
            self._power_chart.add_value(watts)

    def start_monitoring(self) -> None:
        if not self._timer.isActive():
            self._timer.start(1000)

    def stop_monitoring(self) -> None:
        self._timer.stop()
