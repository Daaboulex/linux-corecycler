"""CPU topology detection — cores, CCDs, CCXs, SMT, X3D V-Cache identification."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CPUINFO = Path("/proc/cpuinfo")
SYSFS_CPU = Path("/sys/devices/system/cpu")


@dataclass(frozen=True, slots=True)
class LogicalCPU:
    logical_id: int
    physical_core: int
    package_id: int
    core_cpus: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PhysicalCore:
    core_id: int
    ccd: int | None
    ccx: int | None
    logical_cpus: tuple[int, ...]
    has_vcache: bool = False


@dataclass(slots=True)
class CPUTopology:
    model_name: str = ""
    vendor: str = ""
    family: int = 0
    model: int = 0
    stepping: int = 0
    physical_cores: int = 0
    logical_cpus_count: int = 0
    smt_enabled: bool = False
    ccds: int = 0
    is_x3d: bool = False
    vcache_ccd: int | None = None
    cpus_all_online: bool = True
    cores: dict[int, PhysicalCore] = field(default_factory=dict)
    logical_map: dict[int, LogicalCPU] = field(default_factory=dict)


def common_cpu_flags() -> tuple[str, frozenset[str]]:
    text = CPUINFO.read_text()
    vendor = re.search(r"^vendor_id[ \t]*:[ \t]*(\S+)", text, re.MULTILINE)
    flag_sets = [frozenset(flags.split()) for flags in re.findall(r"^flags[ \t]*:(.*)$", text, re.MULTILINE)]
    return (vendor[1] if vendor else "", frozenset.intersection(*flag_sets) if flag_sets else frozenset())


def detect_topology() -> CPUTopology:
    topo = CPUTopology()
    _parse_cpuinfo(topo)
    _parse_sysfs(topo)
    _detect_ccd_layout(topo)
    _detect_x3d(topo)
    return topo


def _field_int(line: str) -> int | None:
    """Extract the integer after the colon in a "key: value" line.

    Returns None for a missing colon or a non-numeric value, so a malformed or
    non-x86 /proc/cpuinfo line is skipped rather than crashing topology detection.
    """
    parts = line.split(":", 1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[1].strip())
    except ValueError:
        return None


def _l3_id_sort_key(item: tuple[str, list[int]]) -> tuple[int, int, str]:
    """Order L3 groups by the numeric id from the sysfs cache `id` file.

    A plain integer sorts by value (the normal case). A malformed, empty, or
    non-decimal id (a transient zero-byte read, a hex string) sorts last by its
    string form instead of crashing CCD detection with ValueError on int().
    """
    l3_id = item[0]
    if l3_id.lstrip("-").isdigit():
        return (0, int(l3_id), "")
    return (1, 0, l3_id)


def _parse_cpuinfo(topo: CPUTopology) -> None:
    if not CPUINFO.exists():
        return
    text = CPUINFO.read_text()

    cores_seen: dict[int, list[int]] = {}
    current_proc = -1
    current_core = -1
    current_pkg = 0

    for line in text.splitlines():
        if line.startswith("processor"):
            v = _field_int(line)
            if v is not None:
                current_proc = v
        elif line.startswith("core id"):
            v = _field_int(line)
            if v is not None:
                current_core = v
        elif line.startswith("physical id"):
            v = _field_int(line)
            if v is not None:
                current_pkg = v
        elif line.startswith("model name") and not topo.model_name:
            parts = line.split(":", 1)
            if len(parts) > 1:
                topo.model_name = parts[1].strip()
        elif line.startswith("vendor_id") and not topo.vendor:
            parts = line.split(":", 1)
            if len(parts) > 1:
                topo.vendor = parts[1].strip()
        elif line.startswith("cpu family") and topo.family == 0:
            topo.family = _field_int(line) or 0
        elif line.startswith("model\t") and topo.model == 0:
            topo.model = _field_int(line) or 0
        elif line.startswith("stepping") and topo.stepping == 0:
            topo.stepping = _field_int(line) or 0
        elif line == "":
            if current_proc >= 0 and current_core >= 0:
                cores_seen.setdefault(current_core, []).append(current_proc)
                topo.logical_map[current_proc] = LogicalCPU(
                    logical_id=current_proc,
                    physical_core=current_core,
                    package_id=current_pkg,
                    core_cpus=(),
                )
            current_proc = -1
            current_core = -1

    if current_proc >= 0 and current_core >= 0:
        cores_seen.setdefault(current_core, []).append(current_proc)
        topo.logical_map[current_proc] = LogicalCPU(
            logical_id=current_proc,
            physical_core=current_core,
            package_id=current_pkg,
            core_cpus=(),
        )

    topo.physical_cores = len(cores_seen)
    topo.logical_cpus_count = sum(len(v) for v in cores_seen.values())
    topo.smt_enabled = any(len(v) > 1 for v in cores_seen.values())

    for logical_id, lcpu in list(topo.logical_map.items()):
        siblings = tuple(sorted(cores_seen.get(lcpu.physical_core, [logical_id])))
        topo.logical_map[logical_id] = LogicalCPU(
            logical_id=lcpu.logical_id,
            physical_core=lcpu.physical_core,
            package_id=lcpu.package_id,
            core_cpus=siblings,
        )


def _parse_cpu_ranges(text: str) -> set[int]:
    """Parse a sysfs CPU list ("0-15,32-47") into a set of CPU ids.

    A malformed part is skipped rather than crashing, so a transient bad read
    degrades to a smaller set instead of taking topology detection down.
    """
    cpus: set[int] = set()
    for part in text.strip().split(","):
        try:
            if "-" in part:
                lo, hi = part.split("-", 1)
                cpus.update(range(int(lo), int(hi) + 1))
            elif part:
                cpus.add(int(part))
        except ValueError:
            continue
    return cpus


def _parse_sysfs(topo: CPUTopology) -> None:
    """Read sysfs for additional topology info (online/present status)."""
    if not SYSFS_CPU.exists():
        return
    online_path = SYSFS_CPU / "online"
    present_path = SYSFS_CPU / "present"
    online: set[int] | None = None
    if online_path.exists():
        online = _parse_cpu_ranges(online_path.read_text())
        if topo.logical_cpus_count == 0 and online:
            topo.logical_cpus_count = len(online)
    if online is not None and present_path.exists():
        present = _parse_cpu_ranges(present_path.read_text())
        if present and online != present:
            topo.cpus_all_online = False


def _detect_ccd_layout(topo: CPUTopology) -> None:
    """Detect CCD assignment for each core using L3 cache topology."""
    l3_groups: dict[str, list[int]] = {}

    for core_id in sorted(topo.logical_map.keys()):
        lcpu = topo.logical_map[core_id]
        first_logical = lcpu.logical_id if lcpu.logical_id == min(lcpu.core_cpus) else None
        if first_logical is None:
            continue

        cache_dir = SYSFS_CPU / f"cpu{first_logical}" / "cache"
        if not cache_dir.exists():
            continue
        for idx_dir in sorted(cache_dir.iterdir()):
            level_file = idx_dir / "level"
            if level_file.exists() and level_file.read_text().strip() == "3":
                id_file = idx_dir / "id"
                if id_file.exists():
                    l3_id = id_file.read_text().strip()
                    l3_groups.setdefault(l3_id, []).append(lcpu.physical_core)
                break

    ccd_map: dict[int, int] = {}
    for ccd_idx, (_l3_id, core_ids) in enumerate(sorted(l3_groups.items(), key=_l3_id_sort_key)):
        for cid in core_ids:
            ccd_map[cid] = ccd_idx

    topo.ccds = len(l3_groups) if l3_groups else 1

    seen_cores: set[int] = set()
    for lcpu in topo.logical_map.values():
        pc = lcpu.physical_core
        if pc in seen_cores:
            continue
        seen_cores.add(pc)
        topo.cores[pc] = PhysicalCore(
            core_id=pc,
            ccd=ccd_map.get(pc),
            ccx=None,
            logical_cpus=lcpu.core_cpus,
        )


def _detect_x3d(topo: CPUTopology) -> None:
    """Detect X3D processors and identify V-Cache CCD."""
    topo.is_x3d = "x3d" in topo.model_name.lower()

    if not topo.is_x3d or topo.ccds < 2:
        if topo.is_x3d and topo.ccds == 1:
            topo.vcache_ccd = 0
            for pc in topo.cores.values():
                if pc.ccd == 0:
                    object.__setattr__(pc, "has_vcache", True)
        return

    ccd_l3_sizes: dict[int, int] = {}
    for core in topo.cores.values():
        if core.ccd is None:
            continue
        if core.ccd in ccd_l3_sizes:
            continue
        first_cpu = core.logical_cpus[0]
        cache_dir = SYSFS_CPU / f"cpu{first_cpu}" / "cache"
        if not cache_dir.exists():
            continue
        for idx_dir in sorted(cache_dir.iterdir()):
            level_file = idx_dir / "level"
            if level_file.exists() and level_file.read_text().strip() == "3":
                size_file = idx_dir / "size"
                if size_file.exists():
                    size_str = size_file.read_text().strip()
                    m = re.match(r"(\d+)([KMG])?", size_str)
                    if m:
                        val = int(m.group(1))
                        unit = m.group(2) or "K"
                        multiplier = {"K": 1, "M": 1024, "G": 1048576}
                        ccd_l3_sizes[core.ccd] = val * multiplier[unit]
                break

    known = {core.ccd for core in topo.cores.values() if core.ccd is not None}
    if not ccd_l3_sizes or set(ccd_l3_sizes) != known:
        return
    largest = max(ccd_l3_sizes.values())
    holders = [ccd for ccd, size in ccd_l3_sizes.items() if size == largest]
    if len(holders) != 1:
        return
    topo.vcache_ccd = holders[0]
    for pc in topo.cores.values():
        if pc.ccd == topo.vcache_ccd:
            object.__setattr__(pc, "has_vcache", True)


def get_first_logical_cpu(topo: CPUTopology, physical_core: int) -> int:
    """Get the first (non-SMT) logical CPU for a physical core."""
    core = topo.cores.get(physical_core)
    if core and core.logical_cpus:
        return core.logical_cpus[0]
    return physical_core


def get_physical_core_list(topo: CPUTopology) -> list[int]:
    """Get sorted list of physical core IDs."""
    return sorted(topo.cores.keys())
