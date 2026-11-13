"""docs/controls.md lists every control the GUI offers with its reaction, equal to the tests' registries."""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from tests.test_config_tab import CONFIG_CONTROLS
from tests.test_gui_residual_edges import MEMORY_CONTROLS
from tests.test_history_tab_detail import HISTORY_CONTROLS
from tests.test_main_window_wiring import WINDOW_CONTROLS
from tests.test_monitor_tab import MONITOR_CONTROLS
from tests.test_results_tab import RESULTS_CONTROLS
from tests.test_smu_tab_actions import CURVE_CONTROLS
from tests.test_tuner_tab import TUNER_CONTROLS

DOC = Path(__file__).parent.parent / "docs" / "controls.md"
CORNER = "QTableCornerButton"


def _config_names() -> dict[str, str]:
    from PySide6.QtWidgets import QAbstractButton, QApplication

    from corecycler.gui.config_tab import ConfigTab

    QApplication.instance() or QApplication([])
    tab = ConfigTab(None)
    names = {}
    for attr in CONFIG_CONTROLS:
        widget = getattr(tab, attr)
        names[attr] = widget.accessibleName() or (widget.text() if isinstance(widget, QAbstractButton) else "")
    return names


def rendered() -> str:
    config = _config_names()
    tabs = (
        ("Window toolbar", WINDOW_CONTROLS, {}),
        ("Configuration", CONFIG_CONTROLS, config),
        ("Results", RESULTS_CONTROLS, {}),
        ("Monitor", MONITOR_CONTROLS, {}),
        ("Curve-Tuner", CURVE_CONTROLS, {}),
        ("Auto-Tuner", TUNER_CONTROLS, {}),
        ("History", HISTORY_CONTROLS, {}),
        ("Memory", MEMORY_CONTROLS, {}),
    )
    lines = [
        "# Controls",
        "",
        "Every control the GUI offers and what it does. tests/test_controls_doc.py keeps this list equal to the",
        "registries the tab tests check each control against.",
    ]
    for title, registry, names in tabs:
        lines += ["", f"## {title}", ""]
        for key, reaction in registry.items():
            name = "The table's corner button" if key == CORNER else names.get(key, key)
            lines.append(f"- {name}: {reaction}")
    return "\n".join(lines) + "\n"


@pytest.mark.req("T23.contract.doc")
def test_the_controls_page_lists_every_registered_control_with_its_reaction():
    assert DOC.read_text() == rendered(), "docs/controls.md differs from the registries; write rendered() into it"


@pytest.mark.req("T23.contract.doc")
def test_every_configuration_control_is_named_as_the_form_shows_it():
    assert all(name and not name.startswith("_") for name in _config_names().values())
