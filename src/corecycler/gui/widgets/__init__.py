"""Shared GUI widgets and small table helpers."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QTableWidgetItem, QVBoxLayout, QWidget

from corecycler.gui.style import theme

LABEL_WIDTH = 120  # equivalent-mutant: a pixel either way keeps the window in the tile and every name whole


def table_item(text: str, alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft) -> QTableWidgetItem:
    """A non-editable-friendly QTableWidgetItem with alignment set."""
    item = QTableWidgetItem(text)
    item.setTextAlignment(alignment)
    return item


def spoken(seconds: float) -> str:
    total = round(seconds)
    if total < 60:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return " ".join(f"{count} {unit}" for count, unit in ((hours, "h"), (minutes, "min"), (secs, "s")) if count)


def note(text: str) -> QLabel:
    shown = QLabel(text)
    shown.setWordWrap(True)
    shown.setStyleSheet(f"color: {theme.COLOR_TEXT_DIM};")
    return shown


def setting_grid() -> QGridLayout:
    grid = QGridLayout()
    grid.setVerticalSpacing(12)
    grid.setColumnMinimumWidth(0, LABEL_WIDTH)
    grid.setColumnStretch(1, 1)
    return grid


def add_setting(grid: QGridLayout, row: int, label: str, widget: QWidget, unit: str, about: str) -> None:
    widget.setAccessibleName(label)
    name = QLabel(label)
    name.setWordWrap(True)
    name.setMinimumHeight(widget.sizeHint().height())
    grid.addWidget(name, row, 0, Qt.AlignmentFlag.AlignTop)
    field = QVBoxLayout()
    field.setSpacing(2)
    line = QHBoxLayout()
    line.addWidget(widget)
    if unit:
        shown = QLabel(unit)
        shown.setWordWrap(True)
        line.addWidget(shown)
        if unit == "s":

            def say(seconds: float, shown: QLabel = shown) -> None:
                shown.setText(f"s ({said})" if (said := spoken(seconds)) else "s")

            widget.valueChanged.connect(say)
            say(widget.value())
    line.addStretch(1)
    field.addLayout(line)
    field.addWidget(note(about))
    grid.addLayout(field, row, 1)
