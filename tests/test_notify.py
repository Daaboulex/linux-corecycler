"""Desktop notification helper: best-effort, never raises."""

from __future__ import annotations

import subprocess
import sys as _sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.notify import desktop_notify


class TestDesktopNotify:
    def test_missing_binary_returns_false(self):
        with patch("corecycler.config.tools.shutil.which", return_value=None):
            assert desktop_notify("t", "b") is False

    def test_success_returns_true_and_passes_args(self):
        with (
            patch("corecycler.config.tools.shutil.which", return_value="/usr/bin/notify-send"),
            patch("corecycler.notify.subprocess.run", return_value=MagicMock(returncode=0)) as run,
        ):
            assert desktop_notify("Title", "Body", urgency="critical") is True
        argv = run.call_args[0][0]
        assert argv[0] == "/usr/bin/notify-send"
        assert "critical" in argv
        assert "Title" in argv and "Body" in argv

    def test_bad_urgency_falls_back_to_normal(self):
        with (
            patch("corecycler.config.tools.shutil.which", return_value="/usr/bin/notify-send"),
            patch("corecycler.notify.subprocess.run", return_value=MagicMock(returncode=0)) as run,
        ):
            desktop_notify("t", "b", urgency="bogus")
        assert "normal" in run.call_args[0][0]

    def test_nonzero_exit_returns_false(self):
        with (
            patch("corecycler.config.tools.shutil.which", return_value="/usr/bin/notify-send"),
            patch("corecycler.notify.subprocess.run", return_value=MagicMock(returncode=1)),
        ):
            assert desktop_notify("t", "b") is False

    def test_subprocess_error_swallowed(self):
        with (
            patch("corecycler.config.tools.shutil.which", return_value="/usr/bin/notify-send"),
            patch("corecycler.notify.subprocess.run", side_effect=OSError("boom")),
        ):
            assert desktop_notify("t", "b") is False

    def test_timeout_swallowed(self):
        with (
            patch("corecycler.config.tools.shutil.which", return_value="/usr/bin/notify-send"),
            patch("corecycler.notify.subprocess.run", side_effect=subprocess.TimeoutExpired("notify-send", 5)),
        ):
            assert desktop_notify("t", "b") is False
