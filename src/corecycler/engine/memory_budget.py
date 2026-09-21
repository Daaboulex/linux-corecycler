"""One live memory budget for every stressapptest instance the app launches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

OS_AND_APP_HEADROOM_MB = 1024
OS_AND_APP_HEADROOM_PERCENT = 10
PER_INSTANCE_COVERAGE_FLOOR_MB = 256

_KIB_PER_MIB = 1024
_BYTES_PER_MIB = 1024 * 1024
_CGROUP_LIMIT_FILES = ("memory.max", "memory.high")


class MemoryBudgetUnavailable(RuntimeError):
    """The machine's memory numbers could not be read; nothing is launched on a guess."""


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    total_mb: int
    available_mb: int
    cgroup_limit_mb: int | None
    cgroup_used_mb: int | None
    cgroup_limit_source: str | None
    ceiling_mb: int
    headroom_mb: int
    usable_mb: int
    instances: int
    per_instance_mb: int
    launch_minimum_mb: int
    minimum_per_instance_mb: int

    @property
    def fits(self) -> bool:
        return self.per_instance_mb >= self.minimum_per_instance_mb

    def describe(self) -> str:
        if self.cgroup_limit_mb is None:
            cgroup = "cgroup limit none"
        else:
            cgroup = (
                f"cgroup limit {self.cgroup_limit_mb} MB ({self.cgroup_limit_source}, {self.cgroup_used_mb} MB in use)"
            )
        return (
            f"total {self.total_mb} MB, available {self.available_mb} MB, {cgroup}, "
            f"headroom {self.headroom_mb} MB (the larger of {OS_AND_APP_HEADROOM_MB} MB and "
            f"{OS_AND_APP_HEADROOM_PERCENT} percent of the {self.ceiling_mb} MB ceiling), "
            f"usable {self.usable_mb} MB, {self.instances} x {self.per_instance_mb} MB "
            f"(floor {self.minimum_per_instance_mb} MB each: coverage floor "
            f"{PER_INSTANCE_COVERAGE_FLOOR_MB} MB, stressapptest minimum {self.launch_minimum_mb} MB)"
        )


def memory_budget(
    instances: int,
    launch_minimum_mb: int,
    *,
    lane_cgroup: str | None = None,
    proc_base: Path | None = None,
    cgroup_base: Path | None = None,
) -> MemoryBudget:
    if instances < 1:
        raise ValueError(f"a memory budget needs at least one instance, got {instances}")
    proc = proc_base or Path("/proc")
    total_mb, available_mb = _read_meminfo(proc / "meminfo")
    own = lane_cgroup if lane_cgroup is not None else _own_cgroup(proc / "self" / "cgroup")
    limit_mb, used_mb, source = _cgroup_memory(own, cgroup_base or Path("/sys/fs/cgroup"))
    ceiling_mb = available_mb if limit_mb is None else min(available_mb, max(0, limit_mb - used_mb))
    headroom_mb = max(OS_AND_APP_HEADROOM_MB, ceiling_mb * OS_AND_APP_HEADROOM_PERCENT // 100)
    usable_mb = max(0, ceiling_mb - headroom_mb)
    return MemoryBudget(
        total_mb=total_mb,
        available_mb=available_mb,
        cgroup_limit_mb=limit_mb,
        cgroup_used_mb=used_mb,
        cgroup_limit_source=source,
        ceiling_mb=ceiling_mb,
        headroom_mb=headroom_mb,
        usable_mb=usable_mb,
        instances=instances,
        per_instance_mb=usable_mb // instances,
        launch_minimum_mb=launch_minimum_mb,
        minimum_per_instance_mb=max(launch_minimum_mb, PER_INSTANCE_COVERAGE_FLOOR_MB),
    )


def _read_meminfo(path: Path) -> tuple[int, int]:
    try:
        text = path.read_text()
    except OSError as exc:
        raise MemoryBudgetUnavailable(f"{path} is unreadable: {exc}") from exc
    fields: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name in ("MemTotal", "MemAvailable"):
            fields[name] = _kib_field(path, name, rest)
    missing = [name for name in ("MemTotal", "MemAvailable") if name not in fields]
    if missing:
        raise MemoryBudgetUnavailable(f"{path} lacks {', '.join(missing)}")
    return fields["MemTotal"] // _KIB_PER_MIB, fields["MemAvailable"] // _KIB_PER_MIB


def _kib_field(path: Path, name: str, rest: str) -> int:
    parts = rest.split()
    if len(parts) != 2 or parts[1] != "kB" or not parts[0].isdigit():
        raise MemoryBudgetUnavailable(f"{path} has a malformed {name} line: {rest.strip()!r}")
    return int(parts[0])


def unified_cgroup_path(text: str) -> str | None:
    return next((line[3:].strip() for line in text.splitlines() if line.startswith("0::")), None)


def _own_cgroup(self_cgroup: Path) -> str | None:
    try:
        return unified_cgroup_path(self_cgroup.read_text())
    except OSError:
        return None


def _cgroup_memory(own: str | None, cgroup_base: Path) -> tuple[int | None, int | None, str | None]:
    if own is None:
        return None, None, None
    tightest: tuple[int, Path, str] | None = None
    node = cgroup_base / own.lstrip("/")
    while True:
        for name in _CGROUP_LIMIT_FILES:
            limit = _cgroup_int(node / name)
            if limit is not None and (tightest is None or limit < tightest[0]):
                tightest = (limit, node, name)
        if node == cgroup_base or node.parent == node:
            break
        node = node.parent
    if tightest is None:
        return None, None, None
    limit_bytes, node, name = tightest
    used_bytes = _cgroup_int(node / "memory.current") or 0
    relative = node.relative_to(cgroup_base).as_posix()
    source = f"{name} at /{'' if relative == '.' else relative}"
    return limit_bytes // _BYTES_PER_MIB, used_bytes // _BYTES_PER_MIB, source


def _cgroup_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if text == "max":
        return None
    if not text.isdigit():
        raise MemoryBudgetUnavailable(f"{path} holds {text!r}, expected a byte count or 'max'")
    return int(text)
