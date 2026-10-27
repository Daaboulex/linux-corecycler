"""Live monitoring tab — package overview + per-core frequency/temp view."""

from __future__ import annotations

from collections import deque
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
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

from corecycler.gui.style import theme
from corecycler.gui.widgets.charts import LiveChart
from corecycler.gui.widgets.core_grid import ccd_groups
from corecycler.monitor.frequency import fastest, read_max_frequency
from corecycler.smu.driver import physical_ccds

MAX_FREQ_HISTORY = 60  # equivalent-mutant: a sparkline length chosen for the eye, a sample more is unseen
_IDLE_USAGE_PCT = 5


if TYPE_CHECKING:
    from corecycler.monitor.hwmon import Sensors
    from corecycler.monitor.tick import Tick
    from corecycler.smu.pmtable import PMTableData


class CoreFreqBar(QWidget):
    """Compact per-core frequency bar with sparkline history."""

    def __init__(self, core_id: int, label: str, max_freq: float = 6000) -> None:
        super().__init__()
        self.core_id = core_id
        self._label = label
        self._max_freq = max_freq
        self._freq: float | None = None
        self._boost_ceiling_mhz: float = 0
        self._temp: float | None = None
        self._usage_pct: float | None = None
        self._stretch_pct: float | None = None
        self._core_watts: float | None = None
        self._under_test: bool = False
        self._history: deque[float] = deque(maxlen=MAX_FREQ_HISTORY)
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_active(self, active: bool) -> None:
        if self._under_test != active:
            self._under_test = active
            self.update()

    def update_data(
        self,
        freq: float | None,
        temp: float | None = None,
        usage_pct: float | None = None,
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
            self._boost_ceiling_mhz = eff_max_mhz
        if freq is not None:
            self._history.append(freq)
        self.update()

    @staticmethod
    def _freq_text(freq: float | None, eff_max: float) -> str:
        """Per-core frequency readout, always unit-labelled.

        An idle core (freq <= 0) shows "-- MHz" with no ceiling, because
        "--/<max>" reads as a false live maximum. A live core shows its current
        MHz, with the boost ceiling appended only when it is known.
        """
        if freq is None or freq <= 0:
            return "  -- MHz"
        if eff_max > 0:
            return f"{freq:.0f}/{eff_max:.0f}MHz"
        return f"{freq:.0f}MHz"

    def _fill_ratio(self) -> float:
        return min((self._freq or 0) / self._max_freq, 1.0)

    def _columns(self) -> list[tuple[str, str]]:
        usage_str = " --%" if self._usage_pct is None else f"{self._usage_pct:3.0f}%"
        busy = self._usage_pct is not None and self._usage_pct > 50
        usage_color = theme.COLOR_PASS if busy else theme.COLOR_MUTED

        freq_str = self._freq_text(self._freq, self._boost_ceiling_mhz)
        freq_color = theme.COLOR_ACTIVE

        if self._stretch_pct is not None and self._usage_pct is not None and self._usage_pct > _IDLE_USAGE_PCT:
            stretch_str = f"S:{self._stretch_pct:4.1f}%"
            if self._stretch_pct > 3.0:
                stretch_color = theme.CHART_TEMP
            elif self._stretch_pct > 1.0:
                stretch_color = theme.COLOR_WARN_SOFT
            else:
                stretch_color = theme.COLOR_MUTED_DARK
        else:
            stretch_str = " " * len(f"S:{0:4.1f}%")  # equivalent-mutant: the sample digit does not change the width
            stretch_color = theme.COLOR_MUTED_DARK

        if self._core_watts is not None:
            watts_str = f"{self._core_watts:5.1f}W"
            watts_color = theme.COLOR_WARN_SOFT
        else:
            watts_str = " " * len(f"{0:5.1f}W")  # equivalent-mutant: the sample digit does not change the width
            watts_color = theme.COLOR_MUTED_DARK

        if self._temp is not None:
            temp_str = f"{self._temp:3.0f}C"
            if self._temp >= 85:
                temp_color = theme.CHART_TEMP
            elif self._temp >= 70:
                temp_color = theme.COLOR_WARN_SOFT
            else:
                temp_color = theme.COLOR_MUTED
        else:
            temp_str = " --C"
            temp_color = theme.COLOR_MUTED

        return [
            (usage_str, usage_color),
            (freq_str, freq_color),
            (stretch_str, stretch_color),
            (watts_str, watts_color),
            (temp_str, temp_color),
        ]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        bg = QColor(theme.BG_ACTIVE_TINT) if self._under_test else QColor(theme.BG_PANEL_DARK)
        painter.fillRect(0, 0, w, h, bg)

        label_w = 40
        label_color = QColor(theme.COLOR_ACTIVE) if self._under_test else QColor(theme.COLOR_TEXT_DIM)
        painter.setPen(label_color)
        painter.setFont(QFont("monospace", 7, QFont.Weight.Bold))
        painter.drawText(4, 0, label_w - 4, h, Qt.AlignmentFlag.AlignVCenter, self._label)

        bar_x = label_w
        text_columns_w = 260  # equivalent-mutant: a pixel either way keeps every column inside the bar
        bar_w = w - label_w - text_columns_w
        if bar_w > 0 and self._max_freq > 0:
            fill_ratio = self._fill_ratio()

            if fill_ratio < 0.5:
                color = QColor(theme.COLOR_BLUE_DEEP)
            elif fill_ratio < 0.8:
                color = QColor(theme.COLOR_ACTIVE)
            else:
                color = QColor(theme.COLOR_PASS)

            painter.fillRect(bar_x, 3, bar_w, h - 6, QColor(theme.BG_PANEL))
            fill_w = int(bar_w * fill_ratio)
            if fill_w > 0:
                painter.fillRect(bar_x, 3, fill_w, h - 6, color)

            if self._boost_ceiling_mhz:
                eff_ratio = min(self._boost_ceiling_mhz / self._max_freq, 1.0)
                marker_x = int(bar_x + bar_w * eff_ratio)
                pen = QPen(QColor(theme.COLOR_WARN_SOFT), 1, Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.drawLine(marker_x, 3, marker_x, h - 3)

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

        text_x = bar_x + bar_w + 4 if bar_w > 0 else label_w
        mono = QFont("monospace", 7)
        painter.setFont(mono)
        fm = painter.fontMetrics()
        col_gap = fm.horizontalAdvance(" ")

        cols = self._columns()
        cx = text_x
        for text, color in cols:
            tw = fm.horizontalAdvance(text)
            painter.setPen(QColor(color))
            painter.drawText(cx, 0, tw + col_gap, h, Qt.AlignmentFlag.AlignVCenter, text)
            cx += tw + col_gap

        border_color = QColor(theme.COLOR_ACTIVE) if self._under_test else QColor(theme.BORDER_DARKER)
        border_width = 2 if self._under_test else 1
        painter.setPen(QPen(border_color, border_width))
        painter.drawRect(0, 0, w - 1, h - 1)

        painter.end()


STALE = " (stale)"


class MonitorTab(QWidget):
    """Live system monitoring with package charts + per-core view toggle."""

    _NORMAL_STYLE = "font: bold 11px monospace; padding: 2px;"

    @staticmethod
    def _stale_style() -> str:
        return f"font: bold 11px monospace; padding: 2px; color: {theme.COLOR_MUTED_DARK};"

    def __init__(self, sensors: Sensors, power_available: bool, topology=None, smu=None) -> None:
        super().__init__()
        self._topology = topology
        self._smu = smu
        self._sensors = sensors
        self._per_core_bars: dict[int, CoreFreqBar] = {}
        self._per_core_visible = False
        self._last_read: dict[QLabel, str] = {}

        self._setup_ui()

        if not self._sensors.available():
            self._tctl_label.setText("Tctl: N/A")
        has_voltage = self._sensors.vcore_source is not None
        if not has_voltage:
            self._vcore_label.setText("Vcore: no sensor")
        if not power_available:
            self._power_label.setText("Package: unreadable")

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

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

        self._ppt_label = QLabel("PPT: --/--W")
        self._tdc_label = QLabel("TDC: --/--A")
        self._edc_label = QLabel("EDC: --/--A")

        row0_labels = [
            self._tctl_label,
            self._vcore_label,
            self._power_label,
            self._max_freq_label,
        ]
        for i, label in enumerate(row0_labels):
            label.setStyleSheet("font: bold 11px monospace; padding: 2px;")
            values_layout.addWidget(label, 0, i)
        for i, label in enumerate((self._ppt_label, self._tdc_label, self._edc_label)):
            label.setStyleSheet("font: bold 11px monospace; padding: 2px;")
            values_layout.addWidget(label, 2, i)

        top_bar.addWidget(values_group, 1)

        self._toggle_btn = QPushButton("Per-Core View")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setFixedSize(110, 36)
        self._toggle_btn.setStyleSheet(
            "QPushButton { padding: 6px; } "
            f"QPushButton:checked {{ background: {theme.BG_SELECTED}; "
            f"color: {theme.COLOR_ON_SELECTED}; border: 1px solid {theme.COLOR_ACTIVE}; }}"
        )
        self._toggle_btn.toggled.connect(self._toggle_view)
        top_bar.addWidget(self._toggle_btn)

        layout.addLayout(top_bar)

        self._charts_widget = QWidget()
        charts_layout = QGridLayout(self._charts_widget)
        charts_layout.setContentsMargins(0, 0, 0, 0)
        charts_layout.setSpacing(4)

        self._freq_chart = LiveChart("Frequency", "MHz", 0, 6000, "CHART_FREQ")
        self._temp_chart = LiveChart("Temperature", "°C", 0, 100, "CHART_TEMP")
        self._power_chart = LiveChart("Package Power", "W", 0, 250, "CHART_POWER")
        self._voltage_chart = LiveChart("Vcore", "V", 0.5, 1.6, "CHART_VOLT")

        charts_layout.addWidget(self._freq_chart, 0, 0)
        charts_layout.addWidget(self._temp_chart, 0, 1)
        charts_layout.addWidget(self._power_chart, 1, 0)
        charts_layout.addWidget(self._voltage_chart, 1, 1)

        layout.addWidget(self._charts_widget)

        self._per_core_scroll = QScrollArea()
        self._per_core_scroll.setWidgetResizable(True)
        self._per_core_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._per_core_scroll.setStyleSheet("QScrollArea { border: none; }")

        per_core_container = QWidget()
        self._per_core_layout = QVBoxLayout(per_core_container)
        self._per_core_layout.setContentsMargins(0, 0, 0, 0)
        self._per_core_layout.setSpacing(2)

        self._build_per_core_bars([])

        self._per_core_layout.addStretch()
        self._per_core_scroll.setWidget(per_core_container)
        self._per_core_scroll.setVisible(False)
        layout.addWidget(self._per_core_scroll)

        max_freq = read_max_frequency()
        if max_freq:
            self._max_freq_label.setText(f"Max Boost: {max_freq:.0f}MHz")
            self._freq_chart.max_val = max_freq * 1.1
            self._max_core_freq = max_freq
        else:
            self._max_freq_label.setText("Max Boost: unread")
            self._max_core_freq = 6000

    def _build_per_core_bars(self, cpus: list[int]) -> None:
        self._per_core_bars.clear()

        if self._topology:
            for heading, cores in ccd_groups(self._topology):
                vcache_str = " (V-Cache)" if any(c.has_vcache for c in cores) else ""
                header = QLabel(f"{heading}{vcache_str}")
                header.setFont(QFont("monospace", 8, QFont.Weight.Bold))
                header.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM}; padding: 1px 4px;")
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
            for cpu_id in cpus:
                bar = CoreFreqBar(cpu_id, f"CPU{cpu_id}", max_freq=6000)
                self._per_core_bars[cpu_id] = bar
                self._per_core_layout.addWidget(bar)

    def _toggle_view(self, checked: bool) -> None:
        self._per_core_visible = checked
        self._charts_widget.setVisible(not checked)
        self._per_core_scroll.setVisible(checked)
        self._toggle_btn.setText("Package View" if checked else "Per-Core View")

    def _show_read(self, label: QLabel, text: str | None) -> None:
        if text is not None:
            self._last_read[label] = text
            label.setStyleSheet(self._NORMAL_STYLE)
            label.setText(text)
        elif label in self._last_read:
            label.setStyleSheet(self._stale_style())
            label.setText(self._last_read[label] + STALE)

    def on_tick(self, tick: Tick) -> None:
        clocks = tick.clocks
        if self._topology is None and not self._per_core_bars and clocks:
            self._rebuild_bars(sorted(clocks))
        ceilings = tick.ceilings
        freqs = {cpu: mhz for cpu, mhz in clocks.items() if mhz is not None}

        if freqs:
            max_freq = max(freqs.values())
            current_max = getattr(self, "_max_core_freq", 6000)
            if max_freq > current_max:
                self._max_core_freq = max_freq
                self._freq_chart.max_val = max_freq * 1.1
        self._freq_chart.add_value(max(freqs.values()) if freqs else None)

        sensed = tick.sensed
        tctl = sensed.tctl
        self._show_read(self._tctl_label, None if tctl is None else f"Tctl: {tctl:.1f}°C")
        self._temp_chart.add_value(tctl)
        ccd_of = physical_ccds(self._smu, self._topology) if self._topology else {}
        for ccd_idx, temp in sensed.tccd:
            if temp is None:
                if ccd_idx in self._ccd_temp_labels:
                    self._show_read(self._ccd_temp_labels[ccd_idx], None)
                continue
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
                for core_id, core in self._topology.cores.items():
                    if ccd_of[core_id] == ccd_idx and core.has_vcache:
                        vcache_tag = " VC"
                        break
            self._show_read(self._ccd_temp_labels[ccd_idx], f"CCD{ccd_idx}{vcache_tag}: {temp:.1f}°C")
        core_vcore = self._sensors.vcore(sensed)
        self._show_read(self._vcore_label, None if core_vcore is None else f"Vcore: {core_vcore:.4f}V")
        self._voltage_chart.add_value(core_vcore)

        watts = tick.package_watts
        self._show_read(self._power_label, None if watts is None else f"Package: {watts:.1f}W")
        self._power_chart.add_value(watts)

        usage_data = tick.usage
        stretch_data = tick.stretch
        power_data = tick.core_watts

        ccd_temps = {ccd: temp for ccd, temp in sensed.tccd if temp is not None}

        if self._per_core_bars:
            if self._topology:
                for core_id, bar in self._per_core_bars.items():
                    core_info = self._topology.cores.get(core_id)
                    if core_info and core_info.logical_cpus:
                        logical_cpu = core_info.logical_cpus[0]
                        cpu_freq = fastest(clocks, core_info.logical_cpus)
                        eff_max = fastest(ceilings, core_info.logical_cpus) or 0
                        core_temp = ccd_temps.get(ccd_of[core_id])
                        loads = [usage_data[lc] for lc in core_info.logical_cpus if lc in usage_data]
                        bar.update_data(
                            cpu_freq,
                            core_temp,
                            usage_pct=min(sum(loads), 100.0) if loads else None,
                            stretch_pct=stretch_data.get(core_id),
                            core_watts=power_data.get(logical_cpu),
                            eff_max_mhz=eff_max,
                        )

                    mcf = getattr(self, "_max_core_freq", 6000)
                    if mcf:
                        bar._max_freq = mcf * 1.05
            else:
                for cpu_id, bar in self._per_core_bars.items():
                    bar.update_data(freqs.get(cpu_id), None, usage_pct=usage_data.get(cpu_id))

        self._show_power_limits(tick.pm)

    def _show_power_limits(self, pm: PMTableData | None) -> None:
        if pm is None:
            self._ppt_label.setText("PPT: N/A")
            self._tdc_label.setText("TDC: N/A")
            self._edc_label.setText("EDC: N/A")
            return

        def fmt(label, name, value, limit, unit):
            if limit > 0 and value is None:
                label.setText(f"{name}: unread/{limit:.0f}{unit}")
            elif limit > 0:
                pct = (value / limit) * 100 if value >= 0 else 0
                label.setText(f"{name}: {value:.0f}/{limit:.0f}{unit} ({pct:.0f}%)")
            else:
                label.setText(f"{name}: N/A")

        fmt(self._ppt_label, "PPT", pm.ppt_value_w, pm.ppt_limit_w, "W")
        fmt(self._tdc_label, "TDC", pm.tdc_value_a, pm.tdc_limit_a, "A")
        fmt(self._edc_label, "EDC", pm.edc_value_a, pm.edc_limit_a, "A")

    def set_topology(self, topology) -> None:
        self._topology = topology
        self._rebuild_bars([])

    def _rebuild_bars(self, cpus: list[int]) -> None:
        for bar in self._per_core_bars.values():
            bar.deleteLater()
        self._per_core_bars.clear()
        while self._per_core_layout.count():
            item = self._per_core_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._build_per_core_bars(cpus)
        self._per_core_layout.addStretch()

    def set_active_core(self, core_id: int | None) -> None:
        """Highlight the core currently being tested (None to clear)."""
        for cid, bar in self._per_core_bars.items():
            bar.set_active(cid == core_id)
