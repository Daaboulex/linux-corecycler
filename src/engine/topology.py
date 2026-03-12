"""CPU topology detection — cores, CCDs, CCXs, SMT, X3D V-Cache identification."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CPUINFO = Path("/proc/cpuinfo")
SYSFS_CPU = Path("/sys/devices/system/cpu")


@dataclass(frozen=True, slots=True)
class LogicalCPU:
    logical_id: int
    physical_core: int
    package_id: int
    core_cpus: tuple[int, ...]  # all logical CPUs sharing this physical core (SMT siblings)


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
    cores: dict[int, PhysicalCore] = field(default_factory=dict)
    logical_map: dict[int, LogicalCPU] = field(default_factory=dict)


def detect_topology() -> CPUTopology:
    topo = CPUTopology()
    _parse_cpuinfo(topo)
    _parse_sysfs(topo)
    _detect_ccd_layout(topo)
    _detect_x3d(topo)
    return topo


def _parse_cpuinfo(topo: CPUTopology) -> None:
    if not CPUINFO.exists():
        return
    text = CPUINFO.read_text()

    cores_seen: dict[int, list[int]] = {}  # physical_core -> [logical_ids]
    current_proc = -1
    current_core = -1
    current_pkg = 0

    for line in text.splitlines():
        if line.startswith("processor"):
            current_proc = int(line.split(":")[1].strip())
        elif line.startswith("core id"):
            current_core = int(line.split(":")[1].strip())
        elif line.startswith("physical id"):
            current_pkg = int(line.split(":")[1].strip())
        elif line.startswith("model name") and not topo.model_name:
            topo.model_name = line.split(":", 1)[1].strip()
        elif line.startswith("vendor_id") and not topo.vendor:
            topo.vendor = line.split(":")[1].strip()
        elif line.startswith("cpu family") and topo.family == 0:
            topo.family = int(line.split(":")[1].strip())
        elif line.startswith("model\t") and topo.model == 0:
            topo.model = int(line.split(":")[1].strip())
        elif line.startswith("stepping") and topo.stepping == 0:
            topo.stepping = int(line.split(":")[1].strip())
        elif line == "":
            if current_proc >= 0 and current_core >= 0:
                cores_seen.setdefault(current_core, []).append(current_proc)
                topo.logical_map[current_proc] = LogicalCPU(
                    logical_id=current_proc,
                    physical_core=current_core,
                    package_id=current_pkg,
                    core_cpus=(),  # filled later
                )
            current_proc = -1
            current_core = -1

    # handle last entry (no trailing blank line)
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

    # backfill core_cpus tuples
    for logical_id, lcpu in list(topo.logical_map.items()):
        siblings = tuple(sorted(cores_seen.get(lcpu.physical_core, [logical_id])))
        topo.logical_map[logical_id] = LogicalCPU(
            logical_id=lcpu.logical_id,
            physical_core=lcpu.physical_core,
            package_id=lcpu.package_id,
            core_cpus=siblings,
        )


def _parse_sysfs(topo: CPUTopology) -> None:
    """Read sysfs for additional topology info (cache, online status)."""
    if not SYSFS_CPU.exists():
        return
    # count online CPUs as sanity check
    online_path = SYSFS_CPU / "online"
    if online_path.exists():
        text = online_path.read_text().strip()
        # format: "0-31" or "0-15,32-47"
        total = 0
        for part in text.split(","):
            if "-" in part:
                lo, hi = part.split("-")
                total += int(hi) - int(lo) + 1
            else:
                total += 1
        if topo.logical_cpus_count == 0:
            topo.logical_cpus_count = total


def _detect_ccd_layout(topo: CPUTopology) -> None:
    """Detect CCD assignment for each core using L3 cache topology."""
    l3_groups: dict[str, list[int]] = {}  # l3_id -> [core_ids]

    for core_id in sorted(topo.logical_map.keys()):
        # find the first logical CPU for each physical core
        lcpu = topo.logical_map[core_id]
        # use the first logical CPU of this physical core
        first_logical = lcpu.logical_id if lcpu.logical_id == min(lcpu.core_cpus) else None
        if first_logical is None:
            continue

        # check L3 cache index
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

    # map L3 groups to CCD indices
    ccd_map: dict[int, int] = {}  # physical_core -> ccd_index
    for ccd_idx, (_l3_id, core_ids) in enumerate(sorted(l3_groups.items())):
        for cid in core_ids:
            ccd_map[cid] = ccd_idx

    topo.ccds = len(l3_groups) if l3_groups else 1

    # build PhysicalCore entries
    seen_cores: set[int] = set()
    for lcpu in topo.logical_map.values():
        pc = lcpu.physical_core
        if pc in seen_cores:
            continue
        seen_cores.add(pc)
        topo.cores[pc] = PhysicalCore(
            core_id=pc,
            ccd=ccd_map.get(pc),
            ccx=None,  # CCX detection needs more info, skip for now
            logical_cpus=lcpu.core_cpus,
        )


def _detect_x3d(topo: CPUTopology) -> None:
    """Detect X3D processors and identify V-Cache CCD."""
    name_lower = topo.model_name.lower()

    # X3D detection: model name contains "x3d" or known X3D part numbers
    x3d_patterns = ["x3d", "7800x3d", "7900x3d", "7950x3d", "9800x3d", "9900x3d", "9950x3d"]
    topo.is_x3d = any(pat in name_lower for pat in x3d_patterns)

    if not topo.is_x3d or topo.ccds < 2:
        if topo.is_x3d and topo.ccds == 1:
            # single CCD X3D (e.g., 7800X3D) — the one CCD has V-Cache
            topo.vcache_ccd = 0
            for pc in topo.cores.values():
                if pc.ccd == 0:
                    object.__setattr__(pc, "has_vcache", True)
        return

    # multi-CCD X3D: CCD0 has V-Cache (larger L3)
    # detect by comparing L3 sizes per CCD
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
                    # parse "96M" or "32768K" etc
                    m = re.match(r"(\d+)([KMG])?", size_str)
                    if m:
                        val = int(m.group(1))
                        unit = m.group(2) or "K"
                        multiplier = {"K": 1, "M": 1024, "G": 1048576}
                        ccd_l3_sizes[core.ccd] = val * multiplier.get(unit, 1)
                break

    if ccd_l3_sizes:
        # V-Cache CCD has the largest L3
        vcache_ccd = max(ccd_l3_sizes, key=lambda c: ccd_l3_sizes[c])
        topo.vcache_ccd = vcache_ccd
        for pc in topo.cores.values():
            if pc.ccd == vcache_ccd:
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
