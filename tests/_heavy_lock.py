"""Another heavy test tool in its own process, taking the shared lock by the protocol docs/usage.md gives it."""

from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

GPU_LINE = "vkbasalt-faults 4242 2026-10-22T14:25:03+02:00"

HOLDS = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.ftruncate(fd, 0)
os.write(fd, bytes.fromhex(sys.argv[2]))
print(flush=True)
sys.stdin.read()
"""

TRIES = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.stdout.write(os.read(fd, 4096).decode())
    sys.exit(1)
"""


@contextmanager
def held_by_another_tool(path: Path, written: bytes) -> Iterator[None]:
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDS, str(path), written.hex()], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    try:
        assert holder.stdout.readline() == b"\n", "the other tool did not take the lock"
        yield
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def holder_seen_by_another_tool(path: Path) -> str | None:
    """None when another process takes the lock at once, else the line it finds there."""
    tried = subprocess.run(
        [sys.executable, "-c", TRIES, str(path)], capture_output=True, text=True, check=False, timeout=30
    )
    assert tried.returncode in (0, 1), tried.stderr
    return None if tried.returncode == 0 else tried.stdout


def said_by(line: str) -> tuple[str, int, datetime]:
    tool, pid, at = line.removesuffix("\n").split(" ")
    return tool, int(pid), datetime.fromisoformat(at)
