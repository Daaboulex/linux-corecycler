"""The one dialog that turns "backend not found" into a resolved path.

A tool that is not on PATH is the normal case for mprime and y-cruncher, and
under sudo it is the normal case for anything the user added to PATH in their
shell. Rather than a dead end, the dialog states why the lookup failed, offers
any candidate found in the well-known extraction directories, and records the
choice in settings -- so the binary is run only after the user designates it,
never because a scan found it in $HOME.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtWidgets import QFileDialog, QMessageBox

from corecycler.config import tools

if TYPE_CHECKING:
    from pathlib import Path

    from PySide6.QtWidgets import QWidget

_TITLE = "Backend Not Found"


def ensure_tool(parent: QWidget, key: str) -> bool:
    """True when `key` resolves, asking the user for a path when it does not."""
    if tools.resolve(key).path is not None:
        return True
    chosen = _ask(parent, key)
    if chosen is None:
        return False
    tools.record_path(key, str(chosen))
    confirmed = tools.resolve(key)
    if confirmed.path is None:
        QMessageBox.warning(parent, _TITLE, f"{key}: {confirmed.problem}")
        return False
    return True


def _ask(parent: QWidget, key: str) -> Path | str | None:
    candidates = tools.discover(key)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(_TITLE)
    box.setText(explain(key, candidates))
    use_button = box.addButton("Use this", QMessageBox.ButtonRole.AcceptRole) if candidates else None
    browse_button = box.addButton("Browse...", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    clicked = _run_dialog(box)
    if use_button is not None and clicked is use_button:
        return candidates[0]
    if clicked is browse_button:
        return _browse(parent, key)
    return None


def _run_dialog(box: QMessageBox):
    """Show the dialog and report which button was pressed."""
    box.exec()
    return box.clickedButton()


def _browse(parent: QWidget, key: str) -> str | None:
    """Ask for the executable's location, or None when the user cancels."""
    selected, _ = QFileDialog.getOpenFileName(parent, f"Select the {key} executable")
    return selected or None


def explain(key: str, candidates: list[Path]) -> str:
    """The dialog text: why the lookup failed and what can be done about it."""
    resolution = tools.resolve(key)
    lines = [
        f"'{key}' was not found.",
        "",
        f"Reason: {resolution.problem}",
    ]
    if os.geteuid() == 0:
        lines += ["", tools.SUDO_PATH_NOTE]
    if candidates:
        lines += ["", "Found on this system:"]
        lines += [f"  {candidate}" for candidate in candidates]
        lines += ["", "Use it, browse for another, or cancel."]
    else:
        lines += [
            "",
            f"Install {tools.TOOLS[key].package}, browse for the executable, "
            f"or set {tools.env_var(key)}.",
        ]
    return "\n".join(lines)
