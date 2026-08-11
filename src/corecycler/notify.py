"""Best-effort desktop notification via notify-send. Never raises."""

from __future__ import annotations

import logging
import subprocess

from corecycler.config import tools

log = logging.getLogger(__name__)

_APP_NAME = "CoreCycler"
_VALID_URGENCY = ("low", "normal", "critical")


def desktop_notify(title: str, body: str, *, urgency: str = "normal") -> bool:
    """Show a desktop notification, returning True if it was dispatched.

    A missing notify-send, a broken D-Bus session, or a headless machine with
    no notification daemon is a normal condition, not an error: the function
    logs at debug and returns False so the caller carries on unaffected.
    """
    if urgency not in _VALID_URGENCY:
        urgency = "normal"
    resolution = tools.resolve("notify-send")
    if resolution.path is None:
        log.debug("notify-send unavailable (%s) — skipping desktop notification", resolution.problem)
        return False
    binary = str(resolution.path)
    try:
        result = subprocess.run(
            [binary, "--app-name", _APP_NAME, "--urgency", urgency, title, body],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("notify-send failed: %s", e)
        return False
    if result.returncode != 0:
        log.debug("notify-send exited %d", result.returncode)
        return False
    return True
