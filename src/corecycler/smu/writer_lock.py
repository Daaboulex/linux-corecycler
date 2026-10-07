from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from corecycler.config.paths import data_dir, hand_back

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


def start_time(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]


def _note(lock: TextIO, text: str) -> None:
    lock.seek(0)
    lock.truncate()
    lock.write(text)
    lock.flush()
