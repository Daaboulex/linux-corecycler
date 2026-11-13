from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from corecycler.config.paths import data_dir, hand_back
from corecycler.engine.backends.base import read_text

if TYPE_CHECKING:
    from collections.abc import Iterator


class WriterBusy(Exception):
    pass


def lock_path() -> Path:
    return data_dir() / "tuner.lock"


@contextmanager
def co_writer(record: bool = False) -> Iterator[None]:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock:
        hand_back(path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WriterBusy(f"another CO writer holds {path}") from None
        if record:
            _note(lock, f"{os.getpid()} {start_time(os.getpid())}\n")
        try:
            yield
        finally:
            if record:
                _note(lock, "")


def running_driver() -> int | None:
    try:
        pid_text, started = lock_path().read_text().split()
        pid = int(pid_text)
    except OSError, ValueError:
        return None
    return pid if start_time(pid) == started else None


def process_state(pid: int) -> tuple[str, str] | None:
    """The state letter and start time /proc gives the process, read after the last parenthesis since its name may
    hold any byte, or None when it is gone or cannot be read."""
    try:
        fields = read_text(Path(f"/proc/{pid}/stat")).rpartition(")")[2].split()
    except OSError:
        return None
    return fields[0], fields[19]


def start_time(pid: int) -> str | None:
    state = process_state(pid)
    return None if state is None else state[1]


def _note(lock: TextIO, text: str) -> None:
    lock.seek(0)
    lock.truncate()
    lock.write(text)
    lock.flush()
