"""Shared pytest fixtures for CoreCycler tests."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine import containment
from corecycler.engine.backends.base import StressBackend, StressConfig, StressMode
from corecycler.engine.execution import Supervisor
from corecycler.engine.topology import CPUTopology, PhysicalCore

QT_INSTALLED = importlib.util.find_spec("PySide6") is not None
if QT_INSTALLED:
    importlib.import_module("PySide6")


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


CPUINFO_ZEN5_9900X_HARVESTED = _gen_cpuinfo(
    26,
    0x44,
    "AMD Ryzen 9 9900X 12-Core Processor",
    [(i, 0) for i in range(6)] + [(i, 0) for i in range(8, 14)],
)


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
    for pc in topo.cores.values():
        ccd = 0 if pc.core_id < 4 else 1
        object.__setattr__(pc, "ccd", ccd)
    return topo


@pytest.fixture
def mock_backend():
    """A controllable mock StressBackend."""

    class ControllableMockBackend(StressBackend):
        name = "mock"

        def __init__(self):
            self.prepared_dirs: list[Path] = []
            self.commands_generated: list[list[str]] = []
            self._available = True

        def is_available(self) -> bool:
            return self._available

        def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
            cmd = ["echo", "mock-stress"]
            self.commands_generated.append(cmd)
            return cmd

        def findings(self, output: str, work_dir: Path) -> tuple:
            return ()

        def get_supported_modes(self) -> list[StressMode]:
            return [StressMode.SSE, StressMode.AVX, StressMode.AVX2]

        def prepare(self, work_dir: Path, config: StressConfig) -> None:
            work_dir.mkdir(parents=True, exist_ok=True)
            self.prepared_dirs.append(work_dir)

    return ControllableMockBackend()


@pytest.fixture(autouse=True)
def tool_search_roots(monkeypatch):
    """External-tool resolution must not depend on what the dev box installed.

    Clears every CORECYCLER_<TOOL>_BIN override and the configured paths, and
    owns the well-known search roots: they start empty, so a real ~/y-cruncher
    on a developer's machine can never decide a test. A test that needs
    discovery appends the roots it wants to the yielded list.
    """
    from corecycler.config import tools

    for key in tools.TOOLS:
        monkeypatch.delenv(tools.env_var(key), raising=False)
    tools.set_configured_paths({})
    roots: list = []
    monkeypatch.setattr(tools, "search_roots", lambda: tuple(roots))
    yield roots
    tools.set_configured_paths({})


@pytest.fixture(autouse=True)
def writer_dir(monkeypatch, tmp_path_factory):
    from corecycler.smu import writer_lock

    folder = tmp_path_factory.mktemp("data")
    monkeypatch.setattr(writer_lock, "data_dir", lambda: folder)
    return folder


@pytest.fixture(autouse=True)
def heavy_lock(monkeypatch, tmp_path_factory):
    from corecycler.engine import heavy_test_lock

    path = tmp_path_factory.mktemp("runtime") / "heavy-test.lock"
    monkeypatch.setattr(heavy_test_lock, "heavy_test_lock_path", lambda uid: path)
    return path


@pytest.fixture(autouse=True)
def no_machine_check_settle(monkeypatch):
    from corecycler.engine import detector

    monkeypatch.setattr(detector, "mce_settle_s", lambda: 0.0)


@pytest.fixture(autouse=True)
def no_system_bus(monkeypatch):
    from corecycler.gui import tuner_tab

    monkeypatch.setattr(tuner_tab, "power_profile", lambda: None)


@pytest.fixture(autouse=True)
def no_widget_outlives_its_test():
    """Delete every top-level widget a test leaves, here on the main thread.

    A test double that records a widget (a MagicMock's call history, a bound slot) keeps its tree alive
    only through a reference cycle; garbage collection then frees it at an arbitrary moment, and a
    later test's PySide call that lists widgets crashes on the freed pointers (the theme segfault).
    """
    yield
    if not hasattr(sys.modules.get("PySide6"), "__path__"):
        return
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch):
    """A modal dialog in a test hangs the suite forever -- fail loudly instead.

    Tests that exercise the missing-tool prompt patch ensure_tool, _run_dialog
    or _browse themselves, and tests of a dialog stub QMessageBox or the dialog;
    anything else reaching a real Qt modal is a bug in the test, and a named
    failure beats a stuck run.
    """
    try:
        from corecycler.gui import tool_prompt
    except ImportError:
        return

    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "a test reached the real modal tool prompt -- patch "
            "tool_prompt.ensure_tool (or _run_dialog/_browse) in that test"
        )

    monkeypatch.setattr(tool_prompt, "_run_dialog", refuse)
    monkeypatch.setattr(tool_prompt, "_browse", refuse)

    from PySide6.QtWidgets import QDialog, QMessageBox

    def refuse_modal(*_args, **_kwargs):
        raise AssertionError("a test opened a real modal dialog: stub QMessageBox or the dialog in that test")

    for name in ("information", "warning", "question", "critical"):
        monkeypatch.setattr(QMessageBox, name, refuse_modal)
    monkeypatch.setattr(QMessageBox, "exec", refuse_modal)
    monkeypatch.setattr(QDialog, "exec", refuse_modal)


@pytest.fixture
def exec_tmp_path(tmp_path):
    """A temp dir where a chmod +x file really is executable.

    Tool resolution asks the kernel (os.access X_OK), which honours a noexec
    mount -- and /tmp is noexec on some systems, so pytest's tmp_path cannot
    hold a test binary there. Fall back to a scratch dir beside the repo.
    """
    import os
    import tempfile

    probe = tmp_path / ".exec-probe"
    probe.write_text("")
    probe.chmod(0o700)
    executable = os.access(probe, os.X_OK)
    probe.unlink()
    if executable:
        yield tmp_path
        return
    with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".exec-scratch-") as scratch:
        yield Path(scratch)


@pytest.fixture
def on_path(monkeypatch):
    """Declare exactly which tool names PATH resolves, and to what."""
    from corecycler.config import tools

    def install(mapping: dict[str, str]):
        monkeypatch.setattr(tools.shutil, "which", lambda name: mapping.get(name))

    return install


@pytest.fixture
def fake_git(exec_tmp_path, monkeypatch):
    """Put first on PATH a git that runs the given Python program: the package build's sandbox has no git."""
    import os

    def install(program: str):
        git = exec_tmp_path / "git"
        git.write_text(f"#!{sys.executable}\n{program}")
        git.chmod(0o755)
        monkeypatch.setenv("PATH", f"{exec_tmp_path}{os.pathsep}{os.environ['PATH']}")

    return install


@pytest.fixture(autouse=True)
def machine(monkeypatch, tmp_path_factory):
    from tests._machine import installed

    return installed(tmp_path_factory.mktemp("machine"), monkeypatch)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_cpu_time: the payload's CPU time is read from the machine, not faked by payloads_load_every_lane_cpu",
    )


@pytest.fixture(autouse=True)
def payloads_load_every_lane_cpu(request, monkeypatch):
    if request.node.get_closest_marker("real_cpu_time"):
        return
    launched = []
    launch = Supervisor._launch

    def recorded(self, run, config):
        launched.append(run)
        return launch(self, run, config)

    def busy_since_launch(launched_as):
        def read(source):
            owners = [run for run in launched if launched_as(run, source)]
            if not owners:
                raise AssertionError(f"no lane was launched as {source!r}: a test reading CPU time is real_cpu_time")
            return len(owners[-1].lane.cpus) * (time.monotonic() - owners[-1].started_at)

        return read

    monkeypatch.setattr(Supervisor, "_launch", recorded)
    monkeypatch.setattr(
        containment,
        "process_group_cpu_seconds",
        busy_since_launch(lambda run, pid: run.proc is not None and run.proc.pid == pid),
    )
    monkeypatch.setattr(containment, "scope_cpu_seconds", busy_since_launch(lambda run, cgroup: run.cgroup == cgroup))
