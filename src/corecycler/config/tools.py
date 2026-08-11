"""External tool resolution -- the one place CoreCycler locates a binary.

PATH alone cannot find these tools. sudo replaces PATH with the sudoers
``secure_path`` on Debian/Ubuntu/Mint, Fedora and Arch, so a directory the user
added to PATH in their shell is already gone by the time CoreCycler runs as
root; a desktop launcher never sees a shell PATH at all; and mprime and
y-cruncher ship as tarballs extracted wherever the user likes, so they are
never on PATH to begin with.

Resolution order, most explicit first: the ``CORECYCLER_<TOOL>_BIN``
environment variable, the path recorded in ``tool-paths.json``, then PATH. An
explicit path that is not an executable file is REFUSED with the reason, never
silently replaced by some other binary.

``discover`` scans a bounded set of well-known extraction directories to
SUGGEST candidates. Nothing it finds is ever executed until it has been
recorded as the configured path: CoreCycler runs as root, and silently running
a binary found in $HOME is precisely the escalation ``secure_path`` exists to
prevent.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .paths import atomic_write, user_home

log = logging.getLogger(__name__)

BACKEND = "backend"
CORE = "core"
OPTIONAL = "optional"

ORIGIN_ENV = "env"
ORIGIN_CONFIG = "config"
ORIGIN_PATH = "path"
ORIGIN_ABSENT = "absent"

ENV_PREFIX = "CORECYCLER_"
ENV_SUFFIX = "_BIN"

SUDO_PATH_NOTE = (
    "Running as root: sudo replaces PATH with the sudoers secure_path, so a "
    "directory added to PATH in your shell is not visible here."
)

_ENV_SANITIZE = re.compile(r"[^A-Z0-9]+")


@dataclass(frozen=True, slots=True)
class ExternalTool:
    """One external binary, the names it goes by, and where it is extracted."""

    key: str
    kind: str
    package: str
    names: tuple[str, ...]
    globs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Resolution:
    """Where a tool resolved from, or why it did not."""

    key: str
    path: Path | None
    origin: str
    problem: str | None = None


TOOLS: dict[str, ExternalTool] = {
    tool.key: tool
    for tool in (
        ExternalTool(
            key="mprime",
            kind=BACKEND,
            package="mprime (mersenne.org tarball, AUR mprime-bin, or pkgs.mprime)",
            names=("mprime",),
            globs=("mprime", "mprime*/mprime", "p95*/mprime"),
        ),
        ExternalTool(
            key="y-cruncher",
            kind=BACKEND,
            package="y-cruncher (numberworld.org tarball, AUR y-cruncher, or pkgs.y-cruncher)",
            names=("y-cruncher", "y_cruncher"),
            globs=("y-cruncher", "y-cruncher*/y-cruncher", "y_cruncher*/y-cruncher"),
        ),
        ExternalTool(
            key="stress-ng",
            kind=BACKEND,
            package="stress-ng",
            names=("stress-ng",),
        ),
        ExternalTool(
            key="stressapptest",
            kind=BACKEND,
            package="stressapptest",
            names=("stressapptest",),
        ),
        ExternalTool(
            key="systemd-run",
            kind=CORE,
            package="systemd",
            names=("systemd-run",),
        ),
        ExternalTool(
            key="setpriv",
            kind=CORE,
            package="util-linux",
            names=("setpriv",),
        ),
        ExternalTool(
            key="dmidecode",
            kind=OPTIONAL,
            package="dmidecode",
            names=("dmidecode",),
        ),
        ExternalTool(
            key="dmesg",
            kind=OPTIONAL,
            package="util-linux",
            names=("dmesg",),
        ),
        ExternalTool(
            key="journalctl",
            kind=OPTIONAL,
            package="systemd",
            names=("journalctl",),
        ),
        ExternalTool(
            key="notify-send",
            kind=OPTIONAL,
            package="libnotify",
            names=("notify-send",),
        ),
    )
}

_CONFIGURED: dict[str, str] = {}


def paths_file() -> Path:
    """Where a designated tool path is recorded.

    Its own file, not settings.json: these paths are machine-local, and one
    writer per artifact means a window saving its settings can never drop a
    binary the user just designated.
    """
    return user_home() / ".config" / "corecycler" / "tool-paths.json"


def env_var(key: str) -> str:
    """Name of the environment variable that pins this tool's path."""
    return ENV_PREFIX + _ENV_SANITIZE.sub("_", key.upper()) + ENV_SUFFIX


def set_configured_paths(paths: dict[str, str]) -> None:
    """Install the user's recorded tool paths. Unknown keys are dropped."""
    _CONFIGURED.clear()
    _CONFIGURED.update({k: str(v) for k, v in paths.items() if k in TOOLS})


def configured_paths() -> dict[str, str]:
    return dict(_CONFIGURED)


def load_configured_paths() -> dict[str, str]:
    """Read the recorded paths from disk and install them. Never raises."""
    recorded: dict[str, str] = {}
    try:
        stored = json.loads(paths_file().read_text())
    except (OSError, ValueError) as exc:
        log.debug("no usable tool-paths file: %s", exc)
        stored = {}
    if isinstance(stored, dict):
        recorded = {k: v for k, v in stored.items() if isinstance(v, str)}
    else:
        log.warning("tool-paths file is not an object -- ignoring it")
    set_configured_paths(recorded)
    return configured_paths()


def record_path(key: str, path: str) -> None:
    """Record the binary the user designated for one tool, and install it."""
    recorded = load_configured_paths()
    recorded[key] = str(path)
    target = paths_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(recorded, indent=2))
    set_configured_paths(recorded)


def resolve(key: str) -> Resolution:
    """Locate one tool. Never runs it, never guesses past an explicit path."""
    tool = TOOLS.get(key)
    if tool is None:
        return Resolution(
            key=key,
            path=None,
            origin=ORIGIN_ABSENT,
            problem="not a registered external tool",
        )
    env_name = env_var(key)
    override = os.environ.get(env_name, "").strip()
    if override:
        return _explicit(key, override, ORIGIN_ENV, f"{env_name}={override!r}")
    configured = _CONFIGURED.get(key, "").strip()
    if configured:
        return _explicit(key, configured, ORIGIN_CONFIG, f"configured path {configured!r}")
    for name in tool.names:
        found = shutil.which(name)
        if found:
            return Resolution(key=key, path=Path(found), origin=ORIGIN_PATH)
    return Resolution(key=key, path=None, origin=ORIGIN_ABSENT, problem="not found on PATH")


def discover(key: str, limit: int = 8) -> list[Path]:
    """Executable candidates in the well-known extraction directories."""
    tool = TOOLS.get(key)
    if tool is None:
        return []
    found: list[Path] = []
    for root in search_roots():
        for pattern in tool.globs:
            for candidate in _sorted_matches(root, pattern):
                if not _is_executable(candidate):
                    continue
                found.append(candidate)
                if len(found) >= limit:
                    return found
    return found


def search_roots() -> tuple[Path, ...]:
    """Directories a downloaded stress tool is normally extracted into."""
    home = user_home()
    return (
        home,
        home / "Downloads",
        Path("/opt"),
        Path("/usr/local"),
        Path("/usr/local/lib"),
        Path("/usr/lib"),
    )


def report() -> list[Resolution]:
    """Resolve every known tool, in declaration order."""
    return [resolve(key) for key in TOOLS]


def unmet_requirements(resolutions: list[Resolution]) -> list[str]:
    """What is missing that CoreCycler cannot run without."""
    present = {r.key for r in resolutions if r.path is not None}
    unmet = [
        f"{r.key} is required and was {r.problem} ({TOOLS[r.key].package})"
        for r in resolutions
        if TOOLS[r.key].kind == CORE and r.key not in present
    ]
    if not any(TOOLS[key].kind == BACKEND for key in present):
        backends = ", ".join(key for key, tool in TOOLS.items() if tool.kind == BACKEND)
        unmet.append(f"no stress backend available -- install at least one of: {backends}")
    return unmet


def command_name(key: str) -> str:
    """The path to run this tool by.

    Falls back to the bare name when the tool is absent, so the launch fails at
    exec with the tool named -- which the scheduler classifies as a startup
    fault, never as core instability.
    """
    resolution = resolve(key)
    return str(resolution.path) if resolution.path else key


def _explicit(key: str, raw: str, origin: str, subject: str) -> Resolution:
    path = Path(raw)
    if _is_executable(path):
        return Resolution(key=key, path=path, origin=origin)
    return Resolution(
        key=key,
        path=None,
        origin=origin,
        problem=f"refused: {subject} is not an executable file",
    )


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _sorted_matches(root: Path, pattern: str) -> list[Path]:
    try:
        return sorted(root.glob(pattern), reverse=True)
    except OSError:
        return []
