"""The missing-backend dialog: it must explain, offer, record -- never guess.

A scanned candidate is only ever run after the user designates it, because
CoreCycler runs as root; the dialog is that designation.
"""

from __future__ import annotations

import json
import sys as _sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.config import tools
from corecycler.gui import tool_prompt

_REAL_RUN_DIALOG = tool_prompt._run_dialog
_REAL_BROWSE = tool_prompt._browse


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point the recorded-paths file at a scratch directory."""
    recorded = tmp_path / "config" / "tool-paths.json"
    monkeypatch.setattr(tools, "paths_file", lambda: recorded)
    return recorded


@pytest.fixture
def candidate(exec_tmp_path, tool_search_roots):
    home = exec_tmp_path / "home"
    binary = _executable(home / "y-cruncher" / "y-cruncher")
    tool_search_roots.append(home)
    return binary


class TestExplanation:
    def test_names_the_reason_and_the_candidate(self, on_path, candidate):
        on_path({})
        _qapp()
        text = tool_prompt.explain("y-cruncher", tools.discover("y-cruncher"))
        assert "not found on PATH" in text
        assert str(candidate) in text

    def test_without_a_candidate_it_names_the_package_and_the_variable(self, on_path):
        on_path({})
        _qapp()
        text = tool_prompt.explain("y-cruncher", [])
        assert "numberworld.org" in text
        assert "CORECYCLER_Y_CRUNCHER_BIN" in text

    def test_as_root_it_names_the_sudo_path_scrub(self, on_path, monkeypatch):
        on_path({})
        _qapp()
        monkeypatch.setattr(tool_prompt.os, "geteuid", lambda: 0)
        assert tools.SUDO_PATH_NOTE in tool_prompt.explain("y-cruncher", [])


class TestEnsureTool:
    def test_a_resolvable_tool_never_prompts(self, on_path, monkeypatch):
        on_path({"stress-ng": "/usr/bin/stress-ng"})
        monkeypatch.setattr(tool_prompt, "_ask", lambda *a: pytest.fail("prompted for a tool already on PATH"))
        assert tool_prompt.ensure_tool(None, "stress-ng") is True

    def test_using_the_candidate_records_it(self, on_path, candidate, config_dir, monkeypatch):
        on_path({})
        _qapp()
        monkeypatch.setattr(tool_prompt, "_ask", lambda parent, key: candidate)
        assert tool_prompt.ensure_tool(None, "y-cruncher") is True
        assert tools.resolve("y-cruncher").path == candidate
        assert json.loads(config_dir.read_text()) == {"y-cruncher": str(candidate)}
        tools.set_configured_paths({})
        assert tools.load_configured_paths() == {"y-cruncher": str(candidate)}

    def test_cancelling_leaves_nothing_recorded(self, on_path, candidate, config_dir, monkeypatch):
        on_path({})
        _qapp()
        monkeypatch.setattr(tool_prompt, "_ask", lambda parent, key: None)
        assert tool_prompt.ensure_tool(None, "y-cruncher") is False
        assert tools.configured_paths() == {}

    def test_a_chosen_path_that_is_not_executable_is_reported(self, on_path, tmp_path, config_dir, monkeypatch):
        on_path({})
        _qapp()
        plain = tmp_path / "not-a-binary"
        plain.write_text("")
        monkeypatch.setattr(tool_prompt, "_ask", lambda parent, key: plain)
        warned: list[str] = []

        class Reporting:
            @staticmethod
            def warning(parent, title, text):
                warned.append(text)

        monkeypatch.setattr(tool_prompt, "QMessageBox", Reporting)
        assert tool_prompt.ensure_tool(None, "y-cruncher") is False
        assert "not an executable file" in warned[0]


class TestAskWiring:
    """The real dialog is built; only the blocking exec is stood in for."""

    def _press(self, monkeypatch, label: str | None):
        def pick(box):
            if label is None:
                return None
            return next((b for b in box.buttons() if label in b.text()), None)

        monkeypatch.setattr(tool_prompt, "_run_dialog", pick)

    def test_use_this_returns_the_first_candidate(self, on_path, candidate, monkeypatch):
        on_path({})
        _qapp()
        self._press(monkeypatch, "Use this")
        assert tool_prompt._ask(None, "y-cruncher") == candidate

    def test_without_candidates_there_is_nothing_to_use(self, on_path, monkeypatch):
        on_path({})
        _qapp()
        self._press(monkeypatch, "Use this")
        assert tool_prompt._ask(None, "y-cruncher") is None

    def test_browse_returns_the_selected_file(self, on_path, tmp_path, monkeypatch):
        on_path({})
        _qapp()
        self._press(monkeypatch, "Browse")
        monkeypatch.setattr(tool_prompt, "_browse", lambda parent, key: str(tmp_path / "x"))
        assert tool_prompt._ask(None, "y-cruncher") == str(tmp_path / "x")

    def test_browse_cancelled_returns_nothing(self, on_path, monkeypatch):
        on_path({})
        _qapp()
        self._press(monkeypatch, "Browse")
        monkeypatch.setattr(tool_prompt, "_browse", lambda parent, key: None)
        assert tool_prompt._ask(None, "y-cruncher") is None

    def test_cancel_returns_nothing(self, on_path, monkeypatch):
        on_path({})
        _qapp()
        self._press(monkeypatch, "Cancel")
        assert tool_prompt._ask(None, "y-cruncher") is None

    def test_browse_maps_an_empty_selection_to_nothing(self, monkeypatch):
        class Cancelled:
            @staticmethod
            def getOpenFileName(*a, **k):
                return ("", "")

        monkeypatch.setattr(tool_prompt, "QFileDialog", Cancelled)
        assert _REAL_BROWSE(None, "y-cruncher") is None

    def test_browse_returns_what_the_file_dialog_gave(self, monkeypatch):
        class Picked:
            @staticmethod
            def getOpenFileName(*a, **k):
                return ("/opt/y-cruncher/y-cruncher", "")

        monkeypatch.setattr(tool_prompt, "QFileDialog", Picked)
        assert _REAL_BROWSE(None, "y-cruncher") == "/opt/y-cruncher/y-cruncher"

    def test_run_dialog_shows_the_box_then_reports_the_click(self):
        class Box:
            shown = False

            def exec(self):
                Box.shown = True

            def clickedButton(self):
                return "chosen"

        assert _REAL_RUN_DIALOG(Box()) == "chosen"
        assert Box.shown is True
