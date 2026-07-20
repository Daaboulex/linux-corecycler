"""Best-effort desktop notification via notify-send. Never raises."""

from __future__ import annotations

import logging
import shutil
import subprocess

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
    binary = shutil.which("notify-send")
    if binary is None:
        log.debug("notify-send not on PATH — skipping desktop notification")
        return False
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
