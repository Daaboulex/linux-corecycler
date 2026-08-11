"""Kernel-enforced CPU containment for stress processes.

A cgroup cpuset (AllowedCPUs on a transient systemd scope) is a boundary the
contained process cannot widen with sched_setaffinity, unlike a taskset mask.
setpriv --pdeathsig closes the orphan chain: a scope outlives systemd-run, so
the payload must bind its lifetime to systemd-run itself, which our preexec
binds to the app.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from corecycler.config import tools

log = logging.getLogger(__name__)

MECHANISM_USER = "systemd-user-scope"
MECHANISM_SYSTEM = "systemd-system-scope"

_PROBE_TIMEOUT = 10.0

_probe_cache: dict[str, str | None] = {}


class ContainmentUnavailable(RuntimeError):
    """No kernel boundary can be established; launching uncontained is refused."""


def cpu_list(cpus: set[int] | tuple[int, ...] | list[int]) -> str:
    return ",".join(str(c) for c in sorted(set(cpus)))


def _systemd_run_path() -> str | None:
    resolution = tools.resolve("systemd-run")
    return str(resolution.path) if resolution.path else None


def _probe_mechanism() -> str | None:
    systemd_run = _systemd_run_path()
    if systemd_run is None:
        return None
    user_mode = os.geteuid() != 0
    cmd = [systemd_run, "--scope", "--quiet", "--collect", "-p", "AllowedCPUs=0"]
    if user_mode:
        cmd.insert(1, "--user")
    cmd += ["--", "true"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("containment probe failed to run: %s", exc)
        return None
    if result.returncode != 0:
        log.warning(
            "containment probe exited %d: %s",
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return None
    return MECHANISM_USER if user_mode else MECHANISM_SYSTEM


def available_mechanism(*, refresh: bool = False) -> str | None:
    key = f"euid={os.geteuid()}"
    if refresh or key not in _probe_cache:
        _probe_cache[key] = _probe_mechanism()
    return _probe_cache[key]


def contain(cpus: set[int] | tuple[int, ...] | list[int]) -> list[str]:
    """Command prefix that runs the payload inside an AllowedCPUs cpuset.

    Raises ContainmentUnavailable instead of ever returning a weaker prefix.
    """
    cpuset = sorted(set(cpus))
    if not cpuset or any((not isinstance(c, int)) or c < 0 for c in cpuset):
        raise ContainmentUnavailable(f"invalid CPU set {cpus!r}")
    mechanism = available_mechanism()
    if mechanism is None:
        raise ContainmentUnavailable(
            "no cgroup cpuset mechanism available (systemd-run scope probe failed); "
            "refusing to launch an uncontained stress process"
        )
    systemd_run = _systemd_run_path()
    if systemd_run is None:
        raise ContainmentUnavailable("systemd-run disappeared after probe")
    prefix = [systemd_run]
    if mechanism == MECHANISM_USER:
        prefix.append("--user")
    prefix += ["--scope", "--quiet", "--collect", "-p", f"AllowedCPUs={cpu_list(cpuset)}"]
    setpriv = tools.resolve("setpriv")
    if setpriv.path is None:
        raise ContainmentUnavailable("setpriv is required and was not found (util-linux)")
    prefix += ["--", str(setpriv.path), "--pdeathsig", "SIGKILL", "--"]
    return prefix


def observed_tree_cpus(pid: int, *, proc_base: Path | None = None) -> set[int]:
    """Every CPU any thread of pid's process tree is currently allowed on.

    Observation only, for the escape watchdog and the live hardware contract;
    enforcement is the cgroup's job.
    """
    base = proc_base or Path("/proc")
    allowed: set[int] = set()
    for task_pid in _process_tree(pid, base):
        task_dir = base / str(task_pid) / "task"
        try:
            tids = list(task_dir.iterdir())
        except OSError:
            continue
        for tid_dir in tids:
            status_path = tid_dir / "status"
            try:
                for line in status_path.read_text().splitlines():
                    if line.startswith("Cpus_allowed_list:"):
                        allowed |= _parse_cpu_ranges(line.split(":", 1)[1].strip())
                        break
            except (OSError, ValueError):
                continue
    return allowed


def _process_tree(root_pid: int, base: Path) -> list[int]:
    children: dict[int, list[int]] = {}
    try:
        for entry in base.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            rparen = stat.rfind(")")
            fields = stat[rparen + 1 :].split()
            if len(fields) < 2:
                continue
            try:
                ppid = int(fields[1])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(int(entry.name))
    except OSError:
        return [root_pid]
    tree = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        tree.append(pid)
        stack.extend(children.get(pid, []))
    return tree


def _parse_cpu_ranges(text: str) -> set[int]:
    cpus: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                lo, hi = part.split("-", 1)
                cpus.update(range(int(lo), int(hi) + 1))
            else:
                cpus.add(int(part))
        except ValueError:
            continue
    return cpus
