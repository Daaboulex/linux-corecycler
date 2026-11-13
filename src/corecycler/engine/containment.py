"""Kernel-enforced CPU containment for stress processes.

A cgroup cpuset (AllowedCPUs on a transient systemd scope) is a boundary the
contained process cannot widen with sched_setaffinity, unlike a taskset mask.
setpriv --pdeathsig closes the orphan chain: a scope outlives systemd-run, so
the payload must bind its lifetime to systemd-run itself, which our preexec
binds to the app.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from corecycler.config import tools
from corecycler.engine.backends.base import read_text
from corecycler.engine.memory_budget import unified_cgroup_path

log = logging.getLogger(__name__)

MECHANISM_USER = "systemd-user-scope"
MECHANISM_SYSTEM = "systemd-system-scope"

_PROBE_TIMEOUT = 10.0


class ContainmentUnavailable(RuntimeError):
    """No kernel boundary can be established; launching uncontained is refused."""


@dataclass(frozen=True, slots=True)
class Containment:
    prefix: list[str]
    unit: str
    scope: str


@dataclass(frozen=True, slots=True)
class Probe:
    mechanism: str | None
    lane_cgroup_parent: str | None
    problem: str | None = None


_probe_cache: dict[str, Probe] = {}


_unit_seq = 0


def _next_unit() -> str:
    global _unit_seq
    _unit_seq += 1
    return f"corecycler-{os.getpid()}-{_unit_seq}"


def payload_cgroup(pid: int, unit: str, *, proc_base: Path | None = None) -> str | None:
    """The payload's cgroup-v2 path once it has been placed into our scope.

    None until the 0:: line ends with the named scope — before placement it
    still shows the launcher's cgroup, which must never be judged."""
    base = proc_base or Path("/proc")
    try:
        text = (base / str(pid) / "cgroup").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::") and line.strip().endswith(f"/{unit}.scope"):
            return line.strip()[3:]
    return None


def scope_effective_cpus(cgroup_path: str, *, cgroup_base: Path | None = None) -> set[int] | None:
    base = cgroup_base or Path("/sys/fs/cgroup")
    try:
        text = (base / cgroup_path.lstrip("/") / "cpuset.cpus.effective").read_text()
    except OSError:
        return None
    return _parse_cpu_ranges(text.strip())


def scope_cpu_seconds(cgroup_path: str, *, cgroup_base: Path | None = None) -> float | None:
    base = cgroup_base or Path("/sys/fs/cgroup")
    try:
        text = (base / cgroup_path.lstrip("/") / "cpu.stat").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "usage_usec":
            return int(value) / 1_000_000 if value.isdecimal() else None
    return None


def scope_oom_kills(cgroup_path: str, *, cgroup_base: Path | None = None) -> int | None:
    base = cgroup_base or Path("/sys/fs/cgroup")
    try:
        text = (base / cgroup_path.lstrip("/") / "memory.events").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if key == "oom_kill":
            return int(value) if value.isdecimal() else None
    return None


def kill_scope(cgroup_path: str, *, cgroup_base: Path | None = None) -> None:
    """The cgroup.kill write fails ENODEV while systemd removes an emptied scope."""
    scope = (cgroup_base or Path("/sys/fs/cgroup")) / cgroup_path.lstrip("/")
    try:
        with contextlib.suppress(FileNotFoundError):
            (scope / "cgroup.kill").write_text("1")
    except OSError as exc:
        if exc.errno != errno.ENODEV:
            raise
        try:
            listed = (scope / "cgroup.procs").read_text()
        except OSError:
            listed = ""
        if listed:
            raise


def process_group_cpu_seconds(pgid: int, *, proc_base: Path | None = None) -> float | None:
    base = proc_base or Path("/proc")
    ticks: int | None = None
    for stat_path in base.glob("[0-9]*/stat"):
        try:
            fields = read_text(stat_path).rpartition(")")[2].split()
            if int(fields[2]) != pgid:
                continue
            utime, stime, cutime, cstime = (int(value) for value in fields[11:15])
        except OSError, ValueError, IndexError:
            continue
        ticks = (ticks or 0) + utime + stime + cutime + cstime
    return None if ticks is None else ticks / os.sysconf("SC_CLK_TCK")


def cpu_list(cpus: set[int] | tuple[int, ...] | list[int]) -> str:
    return ",".join(str(c) for c in sorted(set(cpus)))


def _systemd_run_path() -> str | None:
    resolution = tools.resolve("systemd-run")
    return str(resolution.path) if resolution.path else None


def _probe_mechanism() -> Probe:
    systemd_run = _systemd_run_path()
    if systemd_run is None:
        return Probe(None, None)
    user_mode = os.geteuid() != 0
    cmd = [systemd_run, "--scope", "--quiet", "--collect", "-p", "AllowedCPUs=0"]
    if user_mode:
        cmd.insert(1, "--user")
    cmd += ["--", "cat", "/proc/self/cgroup", "/proc/self/status"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("containment probe failed to run: %s", exc)
        return Probe(None, None)
    if result.returncode != 0:
        log.warning(
            "containment probe exited %d: %s",
            result.returncode,
            (result.stderr or result.stdout).strip()[:200],
        )
        return Probe(None, None)
    parent = _parent_of(unified_cgroup_path(result.stdout))
    allowed = _allowed_cpus(result.stdout)
    if allowed != {0}:
        return Probe(None, parent, _not_enforced(allowed, parent, user_mode))
    return Probe(MECHANISM_USER if user_mode else MECHANISM_SYSTEM, parent)


def _allowed_cpus(text: str) -> set[int] | None:
    for line in text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return _parse_cpu_ranges(line.partition(":")[2].strip())
    return None


def _not_enforced(
    allowed: set[int] | None, parent: str | None, user_mode: bool, cgroup_base: Path | None = None
) -> str:
    ran = "an unreadable set of CPUs" if allowed is None else f"CPUs {cpu_list(allowed)}"
    controllers = _controllers(parent, cgroup_base)
    manager = "this user's systemd (user@.service)" if user_mode else "systemd"
    return (
        f"a test scope limited to CPU 0 ran on {ran}: AllowedCPUs is not enforced because {manager} does not "
        f"delegate the cpuset controller (the lane cgroup offers: {controllers}); add cpuset to its "
        "DelegateControllers, or run as root, so a stress test cannot run on cores outside its lane"
    )


def _parent_of(cgroup_path: str | None) -> str | None:
    if cgroup_path is None:
        return None
    parent = cgroup_path.rsplit("/", 1)[0]
    return parent or "/"


def _probe(*, refresh: bool = False) -> Probe:
    key = f"euid={os.geteuid()}"
    if refresh or key not in _probe_cache:
        _probe_cache[key] = _probe_mechanism()
    return _probe_cache[key]


def _controllers(parent: str | None, cgroup_base: Path | None) -> str:
    if parent is None:
        return "unknown, the probe reported no cgroup"
    path = (cgroup_base or Path("/sys/fs/cgroup")) / parent.lstrip("/") / "cgroup.controllers"
    try:
        return path.read_text().strip() or "none"
    except OSError as exc:
        return f"unreadable ({exc.strerror})"


def available_mechanism(*, refresh: bool = False) -> str | None:
    return _probe(refresh=refresh).mechanism


def containment_problem(*, refresh: bool = False) -> str | None:
    probe = _probe(refresh=refresh)
    if probe.mechanism is not None:
        return None
    return probe.problem or "no systemd scope could be started (the probe failed), so no stress test can be contained"


def lane_problem() -> str | None:
    return containment_problem() or memory_accounting_problem()


def lane_cgroup_parent() -> str | None:
    """The cgroup a stress scope is created under, read from the probe scope itself."""
    return _probe().lane_cgroup_parent


def memory_accounting_problem(*, cgroup_base: Path | None = None) -> str | None:
    unseen = "so an OOM kill inside a stress lane could not be seen"
    parent = lane_cgroup_parent()
    if parent is None:
        return f"the containment probe found no cgroup v2 path for the stress lanes, {unseen}"
    path = (cgroup_base or Path("/sys/fs/cgroup")) / parent.lstrip("/") / "cgroup.subtree_control"
    try:
        controllers = path.read_text().split()
    except OSError as exc:
        return f"the lane cgroup {parent} cannot be read ({exc}), {unseen}"
    if "memory" not in controllers:
        return (
            f"the lane cgroup {parent} does not give its scopes the memory controller "
            f"(its cgroup.subtree_control lists: {' '.join(controllers) or 'nothing'}), {unseen}"
        )
    return None


def contain(cpus: set[int] | tuple[int, ...] | list[int]) -> Containment:
    """Command prefix that runs the payload inside a named AllowedCPUs scope, and that scope's cgroup path.

    Raises ContainmentUnavailable instead of ever returning a weaker prefix.
    """
    cpuset = sorted(set(cpus))
    if not cpuset or any((not isinstance(c, int)) or c < 0 for c in cpuset):
        raise ContainmentUnavailable(f"invalid CPU set {cpus!r}")
    mechanism = available_mechanism()
    if mechanism is None:
        raise ContainmentUnavailable(f"{containment_problem()}; refusing to launch an uncontained stress process")
    systemd_run = _systemd_run_path()
    if systemd_run is None:
        raise ContainmentUnavailable("systemd-run disappeared after probe")
    unit = _next_unit()
    prefix = [systemd_run]
    if mechanism == MECHANISM_USER:
        prefix.append("--user")
    prefix += [
        "--scope",
        "--quiet",
        "--collect",
        "--unit",
        unit,
        "-p",
        f"AllowedCPUs={cpu_list(cpuset)}",
    ]
    setpriv = tools.resolve("setpriv")
    if setpriv.path is None:
        raise ContainmentUnavailable("setpriv is required and was not found (util-linux)")
    prefix += ["--", str(setpriv.path), "--pdeathsig", "SIGKILL", "--"]
    parent = lane_cgroup_parent()
    if parent is None:
        raise ContainmentUnavailable(
            "the containment probe found no cgroup v2 path for its scope, so a stress scope could not be "
            "found or killed; refusing to launch"
        )
    return Containment(prefix=prefix, unit=unit, scope=f"{parent.rstrip('/')}/{unit}.scope")


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
                for line in read_text(status_path).splitlines():
                    if line.startswith("Cpus_allowed_list:"):
                        allowed |= _parse_cpu_ranges(line.split(":", 1)[1].strip())
                        break
            except OSError, ValueError:
                continue
    return allowed


def own_scheduling() -> tuple[int, int]:
    return os.getpriority(os.PRIO_PROCESS, 0), os.sched_getscheduler(0)


def tree_scheduling(pid: int, *, proc_base: Path | None = None) -> list[tuple[str, int, int, int]]:
    base = proc_base or Path("/proc")
    found = []
    for member in _process_tree(pid, base):
        try:
            tasks = list((base / str(member) / "task").iterdir())
        except OSError:
            continue
        for task in tasks:
            try:
                stat = read_text(task / "stat")
            except OSError:
                continue
            fields = stat[stat.rfind(")") + 1 :].split()
            name = stat[stat.find("(") + 1 : stat.rfind(")")]
            try:
                found.append((name, int(task.name), int(fields[16]), int(fields[38])))
            except IndexError, ValueError:
                continue
    return found


def _process_tree(root_pid: int, base: Path) -> list[int]:
    children: dict[int, list[int]] = {}
    try:
        for entry in base.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = read_text(entry / "stat")
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
