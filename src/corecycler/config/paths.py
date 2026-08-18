"""Filesystem identity for persistent state — always the INVOKING user.

Under ``sudo corecycler``, ``Path.home()`` is ``/root``, which silently splits
the history database and settings into a second root-owned tree (sudo and
non-sudo runs then see different data). All persistent app state resolves
through :func:`user_home`, and :func:`fix_sudo_ownership` hands root-created
files back to the user so a later non-sudo run can still write them.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path


def user_home() -> Path:
    """Home directory of the invoking user — SUDO_USER-aware under sudo."""
    if os.geteuid() == 0:
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and sudo_user != "root":
            import pwd

            with contextlib.suppress(KeyError):
                return Path(pwd.getpwnam(sudo_user).pw_dir)
    return Path.home()


def atomic_write(path: Path, content: str) -> None:
    """Write via temp file + rename, then hand the file back to the user."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    fix_sudo_ownership(path.parent, path)


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
    fix_sudo_ownership(work.parent, work)
    return work


def fix_sudo_ownership(*paths: Path) -> None:
    """chown root-created state files/dirs back to the invoking user.

    No-op unless running as root under sudo. Never raises: ownership repair
    must not break the write that just succeeded — a root-owned file is
    strictly less bad than losing the data.
    """
    if os.geteuid() != 0:
        return
    uid = os.environ.get("SUDO_UID", "")
    gid = os.environ.get("SUDO_GID", "")
    if not (uid.isdigit() and gid.isdigit()):
        return
    for p in paths:
        with contextlib.suppress(OSError):
            if p.exists():
                os.chown(p, int(uid), int(gid))
