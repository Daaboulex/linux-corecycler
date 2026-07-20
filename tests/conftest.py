"""Shared pytest fixtures for CoreCycler tests."""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Mock PySide6 if not installed — allows running state machine tests without Qt.
# TunerEngine inherits QObject and uses Signal/Slot, but the state machine logic
# (_advance_core, _apply_crash_penalty, etc.) is pure Python and testable without Qt.
if "PySide6" not in sys.modules:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        _qt = ModuleType("PySide6")
        _qtcore = ModuleType("PySide6.QtCore")

        class _FakeSignal:
            def __init__(self, *args, **kwargs):
                self._slots = []
            def emit(self, *args):
                for slot in self._slots:
                    slot(*args)
            def connect(self, slot):
                self._slots.append(slot)
            def disconnect(self, slot=None):
                if slot is None:
                    self._slots.clear()
                elif slot in self._slots:
                    self._slots.remove(slot)

        class _FakeQObject:
            pass

        class _FakeQThread:
            def __init__(self, *args, **kwargs):
                pass
            def start(self):
                pass
            def wait(self, *args):
                return True
            def isRunning(self):
                return False
            def terminate(self):
                pass
            def deleteLater(self):
                pass

        class _FakeQTimer:
            @staticmethod
            def singleShot(ms, func):
                func()

        _qtcore.QObject = _FakeQObject
        _qtcore.QThread = _FakeQThread
        _qtcore.QTimer = _FakeQTimer
        _qtcore.Signal = _FakeSignal
        _qtcore.Slot = lambda *a, **k: (lambda f: f)
        _qt.QtCore = _qtcore

        sys.modules["PySide6"] = _qt
        sys.modules["PySide6.QtCore"] = _qtcore

from corecycler.engine.backends.base import StressBackend, StressConfig, StressMode
from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.smu.commands import CPUGeneration, SMUCommandSet

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


def _gen_cpuinfo(family: int, model: int, name: str, cores: list[tuple[int, int]]) -> str:
    """Generate mock /proc/cpuinfo text.

    cores: list of (core_id, physical_id) tuples. Each gets one processor entry.
    """
    lines = []
    for proc_id, (core_id, phys_id) in enumerate(cores):
        lines.append(
            f"processor\t: {proc_id}\n"
            f"vendor_id\t: AuthenticAMD\n"
            f"cpu family\t: {family}\n"
            f"model\t\t: {model}\n"
            f"model name\t: {name}\n"
            f"stepping\t: 2\n"
            f"core id\t\t: {core_id}\n"
            f"physical id\t: {phys_id}\n"
        )
    return "\n".join(lines) + "\n"


# Zen 1 Summit Ridge (1700) — family 23, model 1, 8 cores single CCD
CPUINFO_ZEN1_SUMMIT_RIDGE = _gen_cpuinfo(
    23, 0x01, "AMD Ryzen 7 1700 Eight-Core Processor",
    [(i, 0) for i in range(8)],
)

# Zen+ Pinnacle Ridge (2600) — family 23, model 8, 6 cores
CPUINFO_ZEN_PLUS = _gen_cpuinfo(
    23, 0x08, "AMD Ryzen 5 2600 Six-Core Processor",
    [(i, 0) for i in range(6)],
)

# Zen 3 Cezanne APU (5700G) — family 25, model 0x50, 8 cores single CCD
CPUINFO_ZEN3_CEZANNE_APU = _gen_cpuinfo(
    25, 0x50, "AMD Ryzen 7 5700G with Radeon Graphics",
    [(i, 0) for i in range(8)],
)

# Zen 4 X3D single-CCD (7800X3D) — family 25, model 0x61, 8 cores
CPUINFO_ZEN4_7800X3D = _gen_cpuinfo(
    25, 0x61, "AMD Ryzen 7 7800X3D 8-Core Processor",
    [(i, 0) for i in range(8)],
)

# Zen 4 X3D dual-CCD (7950X3D) — family 25, model 0x61, 16 cores (8+8)
CPUINFO_ZEN4_7950X3D = _gen_cpuinfo(
    25, 0x61, "AMD Ryzen 9 7950X3D 16-Core Processor",
    [(i, 0) for i in range(8)] + [(i, 0) for i in range(8)],
)

# Zen 4 Phoenix APU (7840U) — family 25, model 0x74, 8 cores
CPUINFO_ZEN4_PHOENIX_APU = _gen_cpuinfo(
    25, 0x74, "AMD Ryzen 7 7840U w/ Radeon 780M Graphics",
    [(i, 0) for i in range(8)],
)

# Zen 4 Storm Peak ThreadRipper (7980X) — family 25, model 0x18, 64 cores (8 CCDs)
CPUINFO_ZEN4_STORM_PEAK = _gen_cpuinfo(
    25, 0x18, "AMD Ryzen Threadripper PRO 7980X 64-Core Processor",
    [(i % 8, 0) for i in range(64)],
)

# Zen 5 harvested (9900X) — family 26, model 0x44, 12 cores (6+6)
# Kernel renumbers: CCD0 cores 0-5, CCD1 cores 8-13 (skipping 6,7)
CPUINFO_ZEN5_9900X_HARVESTED = _gen_cpuinfo(
    26, 0x44, "AMD Ryzen 9 9900X 12-Core Processor",
    [(i, 0) for i in range(6)] + [(i, 0) for i in range(8, 14)],
)

# Zen 5 Strix Point APU — family 26, model 0x24, 12 cores
CPUINFO_ZEN5_STRIX_POINT = _gen_cpuinfo(
    26, 0x24, "AMD Ryzen AI 9 HX 370",
    [(i, 0) for i in range(12)],
)

# Zen 5 Shimada Peak ThreadRipper — family 26, different SMU cmds
CPUINFO_ZEN5_SHIMADA_PEAK = _gen_cpuinfo(
    26, 0x44, "AMD Ryzen Threadripper 9980X 64-Core Processor",
    [(i % 8, 0) for i in range(64)],
)


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
    from corecycler.engine.topology import _parse_cpuinfo

    topo = CPUTopology()

    from unittest.mock import patch

    with patch("corecycler.engine.topology.CPUINFO", new_callable=lambda: MagicMock()):
        import corecycler.engine.topology as tmod

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
    for pc in sorted_cores:
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


@pytest.fixture(autouse=True)
def assume_rebooted(monkeypatch):
    """Resume-time crash detection is gated on an actual reboot since the
    session's last write (tuner.engine._rebooted_since). A test process never
    rebooted, so default the gate to "rebooted" here; tests covering the
    no-reboot path override it explicitly.
    """
    import corecycler.tuner.engine as engine_mod

    real = engine_mod._rebooted_since
    monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *a, **k: True)
    return real


@pytest.fixture(autouse=True)
def no_real_forensics(monkeypatch):
    """Resume runs a kernel-journal harvest (journalctl) on the host. Tests
    must stay hermetic: a dev machine's real journal could contain real MCE
    lines and nondeterministically penalize cores mid-test. Default to a
    clean, available harvest; forensics tests set engine._forensics directly.
    """
    import corecycler.tuner.engine as engine_mod

    monkeypatch.setattr(
        engine_mod, "harvest_kernel_mce", lambda since, timeout=15.0: ([], True)
    )


@pytest.fixture(autouse=True)
def assume_clean_shutdown(monkeypatch):
    """Resume probes the host journal to tell a freeze from a deliberate
    reboot (tuner.engine.last_boot_ended_cleanly). Default it to clean so the
    unattributed-incident gate stays quiet; incident tests override with False.
    """
    import corecycler.tuner.engine as engine_mod

    monkeypatch.setattr(
        engine_mod, "last_boot_ended_cleanly", lambda timeout=15.0: True
    )
