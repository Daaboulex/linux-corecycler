"""Shared pytest fixtures for CoreCycler tests."""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.backends.base import StressBackend, StressConfig, StressMode, StressResult
from engine.topology import CPUTopology, LogicalCPU, PhysicalCore
from smu.commands import CPUGeneration, SMUCommandSet

# ---------------------------------------------------------------------------
# Mock cpuinfo data for various CPU configurations
# ---------------------------------------------------------------------------

CPUINFO_DUAL_CCD_SMT = """\
processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 0
physical id\t: 0

processor\t: 1
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 1
physical id\t: 0

processor\t: 2
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 2
physical id\t: 0

processor\t: 3
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 3
physical id\t: 0

processor\t: 4
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 4
physical id\t: 0

processor\t: 5
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 5
physical id\t: 0

processor\t: 6
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 6
physical id\t: 0

processor\t: 7
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 7
physical id\t: 0

processor\t: 8
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 0
physical id\t: 0

processor\t: 9
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 1
physical id\t: 0

processor\t: 10
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 2
physical id\t: 0

processor\t: 11
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 3
physical id\t: 0

processor\t: 12
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 4
physical id\t: 0

processor\t: 13
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 5
physical id\t: 0

processor\t: 14
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 6
physical id\t: 0

processor\t: 15
vendor_id\t: AuthenticAMD
cpu family\t: 26
model\t\t: 68
model name\t: AMD Ryzen 9 9950X3D 16-Core Processor
stepping\t: 2
core id\t\t: 7
physical id\t: 0
"""

CPUINFO_SINGLE_CCD_NO_SMT = """\
processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 5800X 8-Core Processor
stepping\t: 2
core id\t\t: 0
physical id\t: 0

processor\t: 1
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 5800X 8-Core Processor
stepping\t: 2
core id\t\t: 1
physical id\t: 0

processor\t: 2
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 5800X 8-Core Processor
stepping\t: 2
core id\t\t: 2
physical id\t: 0

processor\t: 3
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 5800X 8-Core Processor
stepping\t: 2
core id\t\t: 3
physical id\t: 0
"""

CPUINFO_INTEL_10CORE_SMT = """\
processor\t: 0
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 167
model name\t: 11th Gen Intel(R) Core(TM) i9-10900K @ 3.70GHz
stepping\t: 1
core id\t\t: 0
physical id\t: 0

processor\t: 1
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 167
model name\t: 11th Gen Intel(R) Core(TM) i9-10900K @ 3.70GHz
stepping\t: 1
core id\t\t: 1
physical id\t: 0

processor\t: 2
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 167
model name\t: 11th Gen Intel(R) Core(TM) i9-10900K @ 3.70GHz
stepping\t: 1
core id\t\t: 0
physical id\t: 0

processor\t: 3
vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 167
model name\t: 11th Gen Intel(R) Core(TM) i9-10900K @ 3.70GHz
stepping\t: 1
core id\t\t: 1
physical id\t: 0
"""

CPUINFO_X3D_SINGLE_CCD = """\
processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 7800X3D 8-Core Processor
stepping\t: 2
core id\t\t: 0
physical id\t: 0

processor\t: 1
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 7800X3D 8-Core Processor
stepping\t: 2
core id\t\t: 1
physical id\t: 0

processor\t: 2
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 7800X3D 8-Core Processor
stepping\t: 2
core id\t\t: 0
physical id\t: 0

processor\t: 3
vendor_id\t: AuthenticAMD
cpu family\t: 25
model\t\t: 33
model name\t: AMD Ryzen 7 7800X3D 8-Core Processor
stepping\t: 2
core id\t\t: 1
physical id\t: 0
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sysfs(tmp_path):
    """Factory to create mock sysfs directory trees."""

    def _make_sysfs_tree(structure: dict, base: Path | None = None) -> Path:
        root = base or tmp_path / "sysfs"
        root.mkdir(parents=True, exist_ok=True)
        _write_tree(root, structure)
        return root

    return _make_sysfs_tree


def _write_tree(base: Path, tree: dict) -> None:
    for name, content in tree.items():
        path = base / name
        if isinstance(content, dict):
            path.mkdir(parents=True, exist_ok=True)
            _write_tree(path, content)
        elif isinstance(content, bytes):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(content))


def build_topology(
    cpuinfo_text: str,
    num_ccds: int = 1,
    l3_sizes: dict[int, str] | None = None,
) -> CPUTopology:
    """Build a CPUTopology from mock cpuinfo text by parsing it with the real parser.

    This patches file I/O so the real _parse_cpuinfo works on our fake data.
    Does NOT call _parse_sysfs or _detect_ccd_layout (those need sysfs mocking).
    """
    from engine.topology import _parse_cpuinfo

    topo = CPUTopology()

    from unittest.mock import patch, PropertyMock
    from io import StringIO

    with patch("engine.topology.CPUINFO", new_callable=lambda: MagicMock()):
        import engine.topology as tmod

        orig_cpuinfo = tmod.CPUINFO
        mock_path = MagicMock()
        mock_path.exists.return_value = True
        mock_path.read_text.return_value = cpuinfo_text
        tmod.CPUINFO = mock_path
        try:
            _parse_cpuinfo(topo)
        finally:
            tmod.CPUINFO = orig_cpuinfo

    # build PhysicalCore entries manually (since we skip sysfs)
    # collect unique physical cores first, then assign CCDs
    core_lcpus: dict[int, tuple[int, ...]] = {}
    for lcpu in topo.logical_map.values():
        pc = lcpu.physical_core
        if pc not in core_lcpus:
            core_lcpus[pc] = lcpu.core_cpus

    sorted_cores = sorted(core_lcpus.keys())
    cores_per_ccd = max(1, len(sorted_cores) // num_ccds) if num_ccds > 1 else len(sorted_cores)
    for idx, pc in enumerate(sorted_cores):
        ccd = min(idx // cores_per_ccd, num_ccds - 1) if num_ccds > 1 else 0
        topo.cores[pc] = PhysicalCore(
            core_id=pc,
            ccd=None,
            ccx=None,
            logical_cpus=core_lcpus[pc],
        )

    topo.ccds = num_ccds
    return topo


@pytest.fixture
def topo_dual_ccd_x3d():
    """Topology fixture: 8-core dual-CCD X3D with SMT (16 logical)."""
    topo = build_topology(CPUINFO_DUAL_CCD_SMT, num_ccds=2)
    # assign CCD manually: cores 0-3 = CCD0, cores 4-7 = CCD1
    for pc in topo.cores.values():
        ccd = 0 if pc.core_id < 4 else 1
        object.__setattr__(pc, "ccd", ccd)
    return topo


@pytest.fixture
def topo_single_ccd():
    """Topology fixture: 4-core single CCD, no SMT."""
    return build_topology(CPUINFO_SINGLE_CCD_NO_SMT, num_ccds=1)


@pytest.fixture
def topo_intel():
    """Topology fixture: 2-core Intel with SMT (4 logical)."""
    return build_topology(CPUINFO_INTEL_10CORE_SMT, num_ccds=1)


@pytest.fixture
def mock_backend():
    """A controllable mock StressBackend."""

    class ControllableMockBackend(StressBackend):
        name = "mock"

        def __init__(self):
            self.should_pass = True
            self.error_message = None
            self.prepared_dirs: list[Path] = []
            self.cleaned_dirs: list[Path] = []
            self.commands_generated: list[list[str]] = []
            self._available = True

        def is_available(self) -> bool:
            return self._available

        def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
            cmd = ["echo", "mock-stress"]
            self.commands_generated.append(cmd)
            return cmd

        def parse_output(
            self, stdout: str, stderr: str, returncode: int
        ) -> tuple[bool, str | None]:
            return self.should_pass, self.error_message

        def get_supported_modes(self) -> list[StressMode]:
            return [StressMode.SSE, StressMode.AVX, StressMode.AVX2]

        def prepare(self, work_dir: Path, config: StressConfig) -> None:
            work_dir.mkdir(parents=True, exist_ok=True)
            self.prepared_dirs.append(work_dir)

        def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
            self.cleaned_dirs.append(work_dir)

    return ControllableMockBackend()


@pytest.fixture
def zen3_commands():
    return SMUCommandSet(
        generation=CPUGeneration.ZEN3_VERMEER,
        set_co_cmd=0x35,
        get_co_cmd=0x48,
        set_all_co_cmd=0x36,
        mailbox="mp1",
        co_range=(-30, 30),
        encoding_scheme="zen3",
    )


@pytest.fixture
def zen5_commands():
    return SMUCommandSet(
        generation=CPUGeneration.ZEN5_GRANITE_RIDGE,
        set_co_cmd=0x06,
        get_co_cmd=0xD5,
        set_all_co_cmd=0x07,
        mailbox="rsmu",
        co_range=(-60, 10),
        encoding_scheme="zen4_5",
        set_boost_limit_cmd=0x70,
        get_boost_limit_cmd=0x6E,
    )


@pytest.fixture
def mock_ryzen_smu_sysfs(tmp_path):
    """Create a mock ryzen_smu_drv sysfs tree that responds to reads/writes."""
    smu_dir = tmp_path / "ryzen_smu_drv"
    smu_dir.mkdir()

    # create sysfs files with default content
    (smu_dir / "smu_args").write_bytes(struct.pack("<6I", 0, 0, 0, 0, 0, 0))
    # status=1 means success
    (smu_dir / "rsmu_cmd").write_bytes(struct.pack("<I", 1))
    (smu_dir / "mp1_smu_cmd").write_bytes(struct.pack("<I", 1))
    (smu_dir / "pm_table").write_bytes(b"")

    return smu_dir
