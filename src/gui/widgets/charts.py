"""Simple live chart widgets for frequency, temperature, and power."""

from __future__ import annotations

from collections import deque

from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

MAX_POINTS = 120  # 2 minutes at 1s interval


class LiveChart(QWidget):
    """A simple scrolling line chart."""

    def __init__(
        self,
        title: str = "",
        unit: str = "",
        min_val: float = 0,
        max_val: float = 100,
        color: str = "#4fc3f7",
    ) -> None:
        super().__init__()
        self.title = title
        self.unit = unit
        self.min_val = min_val
        self.max_val = max_val
        self.color = QColor(color)
        self._data: deque[float] = deque(maxlen=MAX_POINTS)
        self._current: float = 0

        self.setMinimumSize(200, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def add_value(self, value: float) -> None:
        self._data.append(value)
        self._current = value
        self.update()

    def clear(self) -> None:
        self._data.clear()
        self._current = 0
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()

        # background
        painter.fillRect(0, 0, w, h, QColor("#1e1e1e"))

        # border
        painter.setPen(QPen(QColor("#333"), 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        margin_top = 20
        margin_bottom = 5
        margin_left = 5
        margin_right = 5
        chart_h = h - margin_top - margin_bottom
        chart_w = w - margin_left - margin_right

        # title + current value
        painter.setPen(QColor("#ccc"))
        painter.setFont(QFont("monospace", 8))
        val_text = f"{self.title}: {self._current:.1f} {self.unit}"
        painter.drawText(margin_left + 2, 14, val_text)

        if not self._data or chart_h <= 0 or chart_w <= 0:
            painter.end()
            return

        # draw line
        data = list(self._data)
        n = len(data)
        val_range = self.max_val - self.min_val
        if val_range <= 0:
            val_range = 1

        path = QPainterPath()
        last_x = margin_left
        for i, val in enumerate(data):
            x = margin_left + (i / max(n - 1, 1)) * chart_w
            y = margin_top + (1.0 - (val - self.min_val) / val_range) * chart_h
            y = max(margin_top, min(margin_top + chart_h, y))
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
            last_x = x

        painter.setPen(QPen(self.color, 2))
        if n == 1:
            # single point — draw a visible dot
            painter.drawEllipse(path.currentPosition(), 3, 3)
        else:
            painter.drawPath(path)

        # fill under curve
        fill_path = QPainterPath(path)
        fill_path.lineTo(last_x, margin_top + chart_h)
        fill_path.lineTo(margin_left, margin_top + chart_h)
        fill_path.closeSubpath()

        fill_color = QColor(self.color)
        fill_color.setAlpha(30)
        painter.fillPath(fill_path, fill_color)

        painter.end()
