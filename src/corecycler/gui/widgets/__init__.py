"""Shared GUI widgets and small table helpers."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem


def table_item(
    text: str, alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft
) -> QTableWidgetItem:
    """A non-editable-friendly QTableWidgetItem with alignment set."""
    item = QTableWidgetItem(text)
    item.setTextAlignment(alignment)
    return item
