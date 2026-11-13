"""The shared heavy-test lock: every heavy test tool one user runs holds it for its whole heavy run, so no two of
them ever load the machine at once. docs/usage.md gives tools outside this repository the same path and protocol."""

from __future__ import annotations

import fcntl
import os
from datetime import datetime
from io import FileIO
from typing import BinaryIO

from corecycler.config.paths import hand_back, heavy_test_lock_path, invoking_uid


class HeavyTestRefused(Exception):
    pass


def take_heavy_test_lock(tool: str) -> BinaryIO:
    """Take the lock for ``tool`` and write its line: the returned file holds the lock until it is closed or the
    process ends. Refuses naming the holder's line, or the error, whenever the lock is not taken."""
    path = heavy_test_lock_path(invoking_uid())
    try:
        lock = FileIO(path, "r+", opener=_private)
    except OSError as exc:
        raise HeavyTestRefused(f"the shared heavy-test lock {path} cannot be taken: {exc.strerror}") from None
    hand_back(path)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.truncate()
        lock.write(f"{tool} {os.getpid()} {datetime.now().astimezone().isoformat(timespec='seconds')}\n".encode())
    except BlockingIOError:
        refusal = f"another heavy test holds the shared lock: {_holder(lock)}"
    except OSError as exc:
        refusal = f"the shared heavy-test lock {path} cannot be taken: {exc.strerror}"
    else:
        return lock
    lock.close()
    raise HeavyTestRefused(refusal)


def _private(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW, 0o600)


def _holder(lock: BinaryIO) -> str:
    try:
        line = lock.read().decode().partition("\n")[0].strip()
    except (OSError, UnicodeDecodeError) as exc:
        return f"its holder line cannot be read ({exc})"
    return line or "its holder line is empty"
