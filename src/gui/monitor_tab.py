"""Live monitoring tab — package overview + per-core frequency/temp view."""

from __future__ import annotations

import contextlib
from collections import deque

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gui.widgets.charts import LiveChart
from monitor.cpu_usage import CPUUsageReader
from monitor.frequency import (
    read_core_frequencies,
    read_core_frequencies_dual,
    read_max_frequency,
)
from monitor.hwmon import HWMonReader
from monitor.msr import MSRReader
from monitor.power import PowerMonitor

MAX_FREQ_HISTORY = 60  # 1 minute at 1s


class CoreFreqBar(QWidget):
    """Compact per-core frequency bar with sparkline history."""

    def __init__(self, core_id: int, label: str, max_freq: float = 6000) -> None:
        super().__init__()
        self.core_id = core_id
        self._label = label
        self._max_freq = max_freq
        self._freq: float = 0
        self._eff_max: float = 0  # per-core boost ceiling (scaling_max_freq)
        self._temp: float = 0
        self._usage_pct: float = 0
        self._stretch_pct: float | None = None
        self._core_watts: float | None = None
        self._is_active: bool = False  # currently being tested
        self._history: deque[float] = deque(maxlen=MAX_FREQ_HISTORY)
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_active(self, active: bool) -> None:
        if self._is_active != active:
            self._is_active = active
            self.update()

    def update_data(
        self,
        freq: float,
        temp: float = 0,
        usage_pct: float = 0,
        stretch_pct: float | None = None,
        core_watts: float | None = None,
        eff_max_mhz: float = 0,
    ) -> None:
        self._freq = freq
        self._temp = temp
        self._usage_pct = usage_pct
        self._stretch_pct = stretch_pct
        self._core_watts = core_watts
        if eff_max_mhz > 0:
            self._eff_max = eff_max_mhz
        self._history.append(freq)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        # background — highlighted if this core is being tested
        bg = QColor("#1a2a1a") if self._is_active else QColor("#1e1e1e")
        painter.fillRect(0, 0, w, h, bg)

        # label area
        label_w = 40
        label_color = QColor("#4fc3f7") if self._is_active else QColor("#aaa")
        painter.setPen(label_color)
        painter.setFont(QFont("monospace", 7, QFont.Weight.Bold))
        painter.drawText(4, 0, label_w - 4, h, Qt.AlignmentFlag.AlignVCenter, self._label)

        # frequency bar
        bar_x = label_w
        text_area_w = 260  # usage + actual/expected + stretch + watts + temp
        bar_w = w - label_w - text_area_w
        if bar_w > 0 and self._max_freq > 0:
            fill_ratio = min(self._freq / self._max_freq, 1.0)

            # color: blue at low, cyan at mid, green at high
            if fill_ratio < 0.5:
                color = QColor("#2196f3")
            elif fill_ratio < 0.8:
                color = QColor("#4fc3f7")
            else:
                color = QColor("#4caf50")

            # bar background
            painter.fillRect(bar_x, 3, bar_w, h - 6, QColor("#2a2a2a"))
            # filled portion
            fill_w = int(bar_w * fill_ratio)
            if fill_w > 0:
                painter.fillRect(bar_x, 3, fill_w, h - 6, color)

            # boost ceiling marker (per-core scaling_max_freq) — yellow dashed line
            if self._eff_max > 0:
                eff_ratio = min(self._eff_max / self._max_freq, 1.0)
                marker_x = int(bar_x + bar_w * eff_ratio)
                pen = QPen(QColor("#ffb74d"), 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawLine(marker_x, 3, marker_x, h - 3)

            # sparkline overlay
            if len(self._history) > 1:
                data = list(self._history)
                pen = QPen(QColor(255, 255, 255, 80), 1)
                painter.setPen(pen)
                n = len(data)
                for i in range(1, n):
                    x0 = bar_x + ((i - 1) / max(n - 1, 1)) * bar_w
                    x1 = bar_x + (i / max(n - 1, 1)) * bar_w
                    y0 = 3 + (1.0 - min(data[i - 1] / self._max_freq, 1.0)) * (h - 6)
                    y1 = 3 + (1.0 - min(data[i] / self._max_freq, 1.0)) * (h - 6)
                    painter.drawLine(int(x0), int(y0), int(x1), int(y1))

        # text values on the right
        text_x = bar_x + bar_w + 4 if bar_w > 0 else label_w
        painter.setFont(QFont("monospace", 7))

        parts = []
        parts.append(f"{self._usage_pct:3.0f}%")
        # Actual freq / boost ceiling
        if self._freq > 0:
            freq_str = f"{self._freq:.0f}"
        else:
            freq_str = "--"
        if self._eff_max > 0:
            parts.append(f"{freq_str}/{self._eff_max:.0f}")
        else:
            parts.append(f"{freq_str}MHz")
        # Only show stretch when core is active (>5% usage) — idle C-state noise is meaningless
        if self._stretch_pct is not None and self._usage_pct > 5:
            parts.append(f"S:{self._stretch_pct:.1f}%")
        if self._core_watts is not None:
            parts.append(f"{self._core_watts:.1f}W")
        if self._temp > 0:
            parts.append(f"{self._temp:.0f}C")

        freq_text = "  ".join(parts)

        # Color stretch % red if suspicious (only meaningful when active)
        if self._stretch_pct is not None and self._stretch_pct > 3.0 and self._usage_pct > 5:
            painter.setPen(QColor("#ff7043"))
        else:
            painter.setPen(QColor("#ccc"))
        painter.drawText(text_x, 0, w - text_x - 2, h, Qt.AlignmentFlag.AlignVCenter, freq_text)

        # border — highlighted if active
        border_color = QColor("#4fc3f7") if self._is_active else QColor("#333")
        border_width = 2 if self._is_active else 1
        painter.setPen(QPen(border_color, border_width))
        painter.drawRect(0, 0, w - 1, h - 1)

        painter.end()


class MonitorTab(QWidget):
    """Live system monitoring with package charts + per-core view toggle."""

    def __init__(self, topology=None) -> None:
        super().__init__()
        self._topology = topology
        self._hwmon = HWMonReader()
        self._power = PowerMonitor()
        self._msr = MSRReader()
        self._cpu_usage = CPUUsageReader()
        self._per_core_bars: dict[int, CoreFreqBar] = {}
        self._per_core_visible = False

        self._setup_ui()

        # Check data source availability and set initial labels
        if not self._hwmon.is_available():
            self._tctl_label.setText("Tctl: N/A")
        has_voltage = self._hwmon.is_available() and self._hwmon.read().vcore_v is not None
        if not has_voltage:
            self._vcore_label.setText("Vcore: N/A (SVI3 not supported)")
        has_power = self._power.is_available() or self._msr.is_available()
        if not has_power:
            self._power_label.setText("Package: N/A (needs root)")

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update)
        self._timer.start(1000)

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # top bar: current values + view toggle
        top_bar = QHBoxLayout()
        top_bar.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        values_group = QGroupBox("Current Values")
        values_layout = QGridLayout(values_group)
        values_layout.setContentsMargins(4, 4, 4, 4)

        self._tctl_label = QLabel("Tctl: --°C")
        self._ccd_temp_labels: dict[int, QLabel] = {}
        self._vcore_label = QLabel("Vcore: --V")
        self._power_label = QLabel("Package: --W")
        self._max_freq_label = QLabel("Max Boost: --MHz")

        row0_labels = [
            self._tctl_label,
            self._vcore_label,
            self._power_label,
            self._max_freq_label,
        ]
        for i, label in enumerate(row0_labels):
            label.setStyleSheet("font: bold 11px monospace; padding: 2px;")
            values_layout.addWidget(label, 0, i)

        top_bar.addWidget(values_group, 1)

        self._toggle_btn = QPushButton("Per-Core View")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setFixedSize(110, 36)
        self._toggle_btn.setStyleSheet(
            "QPushButton { padding: 6px; } "
            "QPushButton:checked { background: #1a3a5c; border: 1px solid #4fc3f7; }"
        )
        self._toggle_btn.toggled.connect(self._toggle_view)
        top_bar.addWidget(self._toggle_btn)

        layout.addLayout(top_bar)

        # package charts view
        self._charts_widget = QWidget()
        charts_layout = QGridLayout(self._charts_widget)
        charts_layout.setContentsMargins(0, 0, 0, 0)
        charts_layout.setSpacing(4)

        self._freq_chart = LiveChart("Frequency", "MHz", 0, 6000, "#4fc3f7")
        self._temp_chart = LiveChart("Temperature", "°C", 0, 100, "#ff7043")
        self._power_chart = LiveChart("Package Power", "W", 0, 250, "#66bb6a")
        self._voltage_chart = LiveChart("Vcore", "V", 0.5, 1.6, "#ab47bc")

        charts_layout.addWidget(self._freq_chart, 0, 0)
        charts_layout.addWidget(self._temp_chart, 0, 1)
        charts_layout.addWidget(self._power_chart, 1, 0)
        charts_layout.addWidget(self._voltage_chart, 1, 1)

        layout.addWidget(self._charts_widget)

        # per-core view (scrollable, hidden by default)
        self._per_core_scroll = QScrollArea()
        self._per_core_scroll.setWidgetResizable(True)
        self._per_core_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._per_core_scroll.setStyleSheet("QScrollArea { border: none; }")

        per_core_container = QWidget()
        self._per_core_layout = QVBoxLayout(per_core_container)
        self._per_core_layout.setContentsMargins(0, 0, 0, 0)
        self._per_core_layout.setSpacing(2)

        self._build_per_core_bars()

        self._per_core_layout.addStretch()
        self._per_core_scroll.setWidget(per_core_container)
        self._per_core_scroll.setVisible(False)
        layout.addWidget(self._per_core_scroll)

        # populate max boost
        max_freq = read_max_frequency()
        if max_freq:
            self._max_freq_label.setText(f"Max Boost: {max_freq:.0f}MHz")
            self._freq_chart.max_val = max_freq * 1.1
            self._max_core_freq = max_freq
        else:
            self._max_core_freq = 6000

    def _build_per_core_bars(self) -> None:
        """Create per-core frequency bars from topology or by scanning sysfs."""
        self._per_core_bars.clear()

        if self._topology:
            # group by CCD
            ccd_groups: dict[int, list] = {}
            for core in sorted(self._topology.cores.values(), key=lambda c: c.core_id):
                ccd = core.ccd if core.ccd is not None else 0
                ccd_groups.setdefault(ccd, []).append(core)

            for ccd_idx in sorted(ccd_groups.keys()):
                cores = ccd_groups[ccd_idx]
                vcache_str = " (V-Cache)" if any(c.has_vcache for c in cores) else ""
                header = QLabel(f"CCD {ccd_idx}{vcache_str}")
                header.setFont(QFont("monospace", 8, QFont.Weight.Bold))
                header.setStyleSheet("color: #aaa; padding: 1px 4px;")
                header.setFixedHeight(16)
                self._per_core_layout.addWidget(header)

                for core in cores:
                    label = f"C{core.core_id}"
                    if core.has_vcache:
                        label += "V"
                    bar = CoreFreqBar(core.core_id, label, max_freq=6000)
                    self._per_core_bars[core.core_id] = bar
                    self._per_core_layout.addWidget(bar)
        else:
            # fallback: create bars from current frequency readings
            freqs = read_core_frequencies()
            for cpu_id in sorted(freqs.keys()):
                bar = CoreFreqBar(cpu_id, f"C{cpu_id}", max_freq=6000)
                self._per_core_bars[cpu_id] = bar
                self._per_core_layout.addWidget(bar)

    def _toggle_view(self, checked: bool) -> None:
        self._per_core_visible = checked
        self._charts_widget.setVisible(not checked)
        self._per_core_scroll.setVisible(checked)
        self._toggle_btn.setText("Package View" if checked else "Per-Core View")

    def _update(self) -> None:
        with contextlib.suppress(Exception):
            self._do_update()

    def _do_update(self) -> None:
        # frequencies — read both actual and boost ceiling per-core
        dual_freqs = read_core_frequencies_dual()
        # Extract simple freq dict for chart + fallback
        freqs: dict[int, float] = {
            cpu_id: r.actual_mhz for cpu_id, r in dual_freqs.items()
        }
        # Also extract per-core boost ceilings
        eff_max_freqs: dict[int, float] = {
            cpu_id: r.effective_max_mhz for cpu_id, r in dual_freqs.items()
        }
        # Fallback to simple read if dual returned nothing
        if not freqs:
            freqs = read_core_frequencies()

        if freqs:
            max_freq = max(freqs.values())
            self._freq_chart.add_value(max_freq)

        # hwmon
        hwmon_data = self._hwmon.read()
        tctl = hwmon_data.tctl_c
        if tctl is not None:
            self._tctl_label.setText(f"Tctl: {tctl:.1f}°C")
            self._temp_chart.add_value(tctl)
        # Per-CCD temps — create labels dynamically on first appearance
        for tccd_idx in sorted(hwmon_data.tccd_temps):
            temp = hwmon_data.tccd_temps[tccd_idx]
            ccd_idx = tccd_idx - 1  # Tccd1 → CCD 0
            if ccd_idx not in self._ccd_temp_labels:
                label = QLabel()
                label.setStyleSheet("font: bold 11px monospace; padding: 2px;")
                values_group = self._tctl_label.parent()
                if values_group:
                    gl = values_group.layout()
                    if gl:
                        gl.addWidget(label, 1, len(self._ccd_temp_labels))
                self._ccd_temp_labels[ccd_idx] = label
            vcache_tag = ""
            if self._topology:
                for core in self._topology.cores.values():
                    if core.ccd == ccd_idx and core.has_vcache:
                        vcache_tag = " VC"
                        break
            self._ccd_temp_labels[ccd_idx].setText(
                f"CCD{ccd_idx}{vcache_tag}: {temp:.1f}°C"
            )
        if hwmon_data.vcore_v is not None:
            self._vcore_label.setText(f"Vcore: {hwmon_data.vcore_v:.4f}V")
            self._voltage_chart.add_value(hwmon_data.vcore_v)

        # power — sysfs RAPL (user-accessible) or MSR RAPL (root-only)
        watts = self._power.read_power_watts()
        if watts is not None:
            self._power_label.setText(f"Package: {watts:.1f}W")
            self._power_chart.add_value(watts)
        elif self._msr.is_available():
            # Fallback: read package energy from MSR RAPL
            pkg_power = self._msr.read_package_power()
            if pkg_power is not None:
                self._power_label.setText(f"Package: {pkg_power:.1f}W")
                self._power_chart.add_value(pkg_power)

        # CPU usage from /proc/stat
        usage_data = self._cpu_usage.read()  # logical cpu → usage %

        # MSR: clock stretch + per-core power (for per-core bars)
        stretch_data: dict[int, float] = {}  # logical cpu → stretch %
        power_data: dict[int, float] = {}  # logical cpu → watts
        if self._msr.is_available() and self._topology:
            all_cpus = []
            for core_info in self._topology.cores.values():
                if core_info.logical_cpus:
                    all_cpus.append(core_info.logical_cpus[0])
            if all_cpus:
                stretch_readings = self._msr.read_clock_stretch(all_cpus)
                for cpu_id, reading in stretch_readings.items():
                    stretch_data[cpu_id] = reading.stretch_pct
                power_readings = self._msr.read_core_power(all_cpus)
                for cpu_id, reading in power_readings.items():
                    power_data[cpu_id] = reading.watts

        # Build CCD → temp mapping from hwmon tccd_temps
        # k10temp: Tccd1 = CCD index 1, but topology CCD indices start at 0
        ccd_temps: dict[int, float] = {}
        for tccd_idx, temp in hwmon_data.tccd_temps.items():
            ccd_temps[tccd_idx - 1] = temp  # Tccd1 → CCD 0, Tccd2 → CCD 1

        # update per-core bars (even if hidden, so history accumulates)
        if self._per_core_bars and freqs:
            if self._topology:
                for core_id, bar in self._per_core_bars.items():
                    core_info = self._topology.cores.get(core_id)
                    if core_info and core_info.logical_cpus:
                        logical_cpu = core_info.logical_cpus[0]
                        cpu_freq = freqs.get(logical_cpu, 0)
                        eff_max = eff_max_freqs.get(logical_cpu, 0)
                        # Use per-CCD temp if available, fall back to Tctl
                        ccd = core_info.ccd if core_info.ccd is not None else 0
                        core_temp = ccd_temps.get(ccd, tctl or 0)
                        # Sum usage across SMT siblings for this physical core
                        core_usage = sum(
                            usage_data.get(lc, 0) for lc in core_info.logical_cpus
                        )
                        bar.update_data(
                            cpu_freq,
                            core_temp,
                            usage_pct=min(core_usage, 100.0),
                            stretch_pct=stretch_data.get(logical_cpu),
                            core_watts=power_data.get(logical_cpu),
                            eff_max_mhz=eff_max,
                        )

                    if self._max_core_freq:
                        bar._max_freq = self._max_core_freq * 1.05
            else:
                for cpu_id, bar in self._per_core_bars.items():
                    bar.update_data(
                        freqs.get(cpu_id, 0),
                        tctl or 0,
                        usage_pct=usage_data.get(cpu_id, 0),
                    )

    def set_topology(self, topology) -> None:
        """Update topology and rebuild per-core bars."""
        self._topology = topology
        # clear existing bars
        for bar in self._per_core_bars.values():
            bar.deleteLater()
        self._per_core_bars.clear()
        # clear layout (skip stretch)
        while self._per_core_layout.count():
            item = self._per_core_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._build_per_core_bars()
        self._per_core_layout.addStretch()

    def set_active_core(self, core_id: int | None) -> None:
        """Highlight the core currently being tested (None to clear)."""
        for cid, bar in self._per_core_bars.items():
            bar.set_active(cid == core_id)

    def start_monitoring(self) -> None:
        if not self._timer.isActive():
            self._timer.start(1000)

    def stop_monitoring(self) -> None:
        self._timer.stop()
