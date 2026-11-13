"""Filesystem identity for persistent state — always the INVOKING user.

Under ``sudo corecycler``, ``Path.home()`` is ``/root``, which silently splits
the history database and settings into a second root-owned tree (sudo and
non-sudo runs then see different data). All persistent app state resolves
through :func:`user_home`, and :func:`hand_back` hands root-created
files back to the user so a later non-sudo run can still write them.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

log = logging.getLogger(__name__)


def user_home() -> Path:
    """Home directory of the invoking user — SUDO_USER-aware under sudo."""
    if os.geteuid() == 0:
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and sudo_user != "root":
            import pwd

            with contextlib.suppress(KeyError):
                return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def data_dir() -> Path:
    return user_home() / ".local" / "share" / "corecycler"


def heartbeat_path(uid: int) -> Path:
    """The heartbeat the hang watch reads, in the user's runtime tmpfs: no boot outlives it, HOME never moves it."""
    return Path("/run/user") / str(uid) / "corecycler" / "heartbeat.json"


def heavy_test_lock_path(uid: int) -> Path:
    """The lock every heavy test tool this user runs takes, in the user's runtime tmpfs: no boot outlives it, HOME
    never moves it, and tools outside this repository take the same file."""
    return Path("/run/user") / str(uid) / "heavy-test.lock"


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    hand_back(path)


def invoking_uid() -> int:
    """Uid of the user who started the run — SUDO_UID-aware under sudo."""
    if os.geteuid() == 0:
        sudo_uid = os.environ.get("SUDO_UID", "")
        if sudo_uid.isdigit():
            return int(sudo_uid)
    return os.geteuid()


def resolve_work_dir(configured: str = "") -> Path:
    """The stress work root: configured path, else a per-user default.

    The default is never a shared world-writable location — a root-owned
    /tmp/corecycler left by one sudo run made every later plain run unable to
    write its backend config. The runtime directory is used only when it belongs
    to the INVOKING user, like every other path here: under sudo it is root's
    own private one, which is Qt's to keep its runtime files in, not a work root
    the user could reach afterwards."""
    if configured:
        return Path(configured)
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime:
        root = Path(runtime)
        with contextlib.suppress(OSError):
            if root.is_dir() and root.stat().st_uid == invoking_uid():
                return root / "corecycler" / "work"
    return user_home() / ".cache" / "corecycler" / "work"


def ensure_work_dir(configured: str = "") -> Path:
    work = resolve_work_dir(configured)
    work.mkdir(parents=True, exist_ok=True)
    hand_back(work)
    return work


def child_environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)}


def hand_back(path: Path) -> None:
    """Under sudo, give the invoking user what root created on the way to ``path``.

    Only root-owned entries between the nearest directory the user already
    owns and ``path`` change hands, top down, and no symlink is followed, so
    nothing outside the user's own tree (``/``, ``/tmp``, ``/root``, a system
    directory picked in a save dialog) is ever handed over. No-op unless
    running as root under sudo. A failed chown is logged, never raised: a
    root-owned file is strictly less bad than losing the write before it.
    """
    if os.geteuid() != 0:
        return
    uid = os.environ.get("SUDO_UID", "")
    gid = os.environ.get("SUDO_GID", "")
    if not (uid.isdigit() and gid.isdigit()) or not path.exists():
        return
    owner = int(uid)
    chain = []
    for entry in (path, *path.parents):
        if entry.lstat().st_uid == owner:
            break
        chain.append(entry)
    else:
        return
    for entry in reversed(chain):
        if entry.lstat().st_uid != 0:
            continue
        try:
            os.chown(entry, owner, int(gid), follow_symlinks=False)
        except OSError as exc:
            log.warning("could not hand %s back to uid %d: %s", entry, owner, exc)


class HandedBackLog(RotatingFileHandler):
    def doRollover(self) -> None:
        super().doRollover()
        hand_back(Path(self.baseFilename))
