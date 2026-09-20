"""One live memory budget for every stressapptest instance the app launches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

OS_AND_APP_HEADROOM_PERCENT = 25

_KIB_PER_MIB = 1024
_BYTES_PER_MIB = 1024 * 1024


class MemoryBudgetUnavailable(RuntimeError):
    """The machine's memory numbers could not be read; nothing is launched on a guess."""


@dataclass(frozen=True, slots=True)
class MemoryBudget:
    total_mb: int
    available_mb: int
    cgroup_limit_mb: int | None
    cgroup_used_mb: int | None
    ceiling_mb: int
    headroom_mb: int
    usable_mb: int
    instances: int
    per_instance_mb: int
    minimum_per_instance_mb: int

    @property
    def fits(self) -> bool:
        return self.per_instance_mb >= self.minimum_per_instance_mb

    def describe(self) -> str:
        if self.cgroup_limit_mb is None:
            cgroup = "cgroup limit none"
        else:
            cgroup = f"cgroup limit {self.cgroup_limit_mb} MB ({self.cgroup_used_mb} MB in use)"
        return (
            f"total {self.total_mb} MB, available {self.available_mb} MB, {cgroup}, "
            f"headroom {self.headroom_mb} MB ({OS_AND_APP_HEADROOM_PERCENT} percent of the "
            f"{self.ceiling_mb} MB ceiling), usable {self.usable_mb} MB, "
            f"{self.instances} x {self.per_instance_mb} MB "
            f"(stressapptest minimum {self.minimum_per_instance_mb} MB each)"
        )


def memory_budget(
    instances: int,
    minimum_per_instance_mb: int,
    *,
    proc_base: Path | None = None,
    cgroup_base: Path | None = None,
) -> MemoryBudget:
    if instances < 1:
        raise ValueError(f"a memory budget needs at least one instance, got {instances}")
    proc = proc_base or Path("/proc")
    total_mb, available_mb = _read_meminfo(proc / "meminfo")
    limit_mb, used_mb = _cgroup_memory(proc / "self" / "cgroup", cgroup_base or Path("/sys/fs/cgroup"))
    ceiling_mb = available_mb if limit_mb is None else min(available_mb, max(0, limit_mb - used_mb))
    headroom_mb = ceiling_mb * OS_AND_APP_HEADROOM_PERCENT // 100
    usable_mb = ceiling_mb - headroom_mb
    return MemoryBudget(
        total_mb=total_mb,
        available_mb=available_mb,
        cgroup_limit_mb=limit_mb,
        cgroup_used_mb=used_mb,
        ceiling_mb=ceiling_mb,
        headroom_mb=headroom_mb,
        usable_mb=usable_mb,
        instances=instances,
        per_instance_mb=usable_mb // instances,
        minimum_per_instance_mb=minimum_per_instance_mb,
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


def _cgroup_memory(self_cgroup: Path, cgroup_base: Path) -> tuple[int | None, int | None]:
    try:
        lines = self_cgroup.read_text().splitlines()
    except OSError:
        return None, None
    own = next((line[3:].strip() for line in lines if line.startswith("0::")), None)
    if own is None:
        return None, None
    tightest: tuple[int, Path] | None = None
    node = cgroup_base / own.lstrip("/")
    while True:
        limit = _cgroup_int(node / "memory.max")
        if limit is not None and (tightest is None or limit < tightest[0]):
            tightest = (limit, node)
        if node == cgroup_base or node.parent == node:
            break
        node = node.parent
    if tightest is None:
        return None, None
    limit_bytes, node = tightest
    used_bytes = _cgroup_int(node / "memory.current") or 0
    return limit_bytes // _BYTES_PER_MIB, used_bytes // _BYTES_PER_MIB


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
