"""CRITICAL safety invariant tests.

These tests verify that:
- The SMU driver gracefully handles missing ryzen_smu module
- CO values are bounds-checked against generation limits
- Scheduler kills processes cleanly on stop
- Error detector reports an unreadable kernel log instead of crashing
- Settings don't write to unexpected paths
- Backend prepare doesn't clobber files outside work_dir
"""

from __future__ import annotations

import os
import signal
import struct
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from test_execution import (
    DEAF_TO_SIGTERM,
    FakeBackend,
    _child,
    holds_the_deadline_until,
    lane,
    make_supervisor,
    run_one,
)

from corecycler.engine import execution
from corecycler.engine.backends.base import StressConfig
from corecycler.engine.backends.mprime import MprimeBackend
from corecycler.engine.detector import ErrorDetector
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig, TestState
from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.smu.commands import CPUGeneration, encode_co_arg, get_commands
from corecycler.smu.driver import RyzenSMU


class TestSMUDriverSafety:
    def test_is_available_missing_sysfs(self):
        """RyzenSMU.is_available must return False when driver not loaded."""
        assert RyzenSMU.is_available(Path("/nonexistent/path")) is False

    def test_is_available_missing_smu_args(self, tmp_path):
        """Directory exists but smu_args missing."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        assert RyzenSMU.is_available(smu_dir) is False

    def test_is_available_checks_both_conditions(self, tmp_path):
        """Both dir and smu_args must exist."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "smu_args").write_bytes(b"\x00" * 24)
        assert RyzenSMU.is_available(smu_dir) is True

    def test_a_driver_directory_it_may_not_read_is_unavailable_not_an_error(self, tmp_path):
        if os.geteuid() == 0:
            pytest.skip("root reads past file permissions")
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "smu_args").write_bytes(b"\x00" * 24)
        smu_dir.chmod(0)
        try:
            assert RyzenSMU.is_available(smu_dir) is False
        finally:
            smu_dir.chmod(0o755)


class TestCOBoundsChecking:
    @pytest.mark.parametrize(
        "gen,min_val,max_val",
        [
            (CPUGeneration.ZEN3_VERMEER, -30, 30),
            (CPUGeneration.ZEN3D_WARHOL, -30, 30),
            (CPUGeneration.ZEN4_RAPHAEL, -50, 30),
            (CPUGeneration.ZEN5_GRANITE_RIDGE, -50, 10),
        ],
    )
    def test_co_range_enforced(self, gen, min_val, max_val, tmp_path):
        """set_co_offset must reject values outside the generation's range."""
        cmds = get_commands(gen)
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "smu_args").write_bytes(struct.pack("<6I", 0, 0, 0, 0, 0, 0))
        (smu_dir / "rsmu_cmd").write_bytes(struct.pack("<I", 1))
        (smu_dir / "mp1_smu_cmd").write_bytes(struct.pack("<I", 1))

        smu = RyzenSMU(cmds, smu_dir)

        with pytest.raises(ValueError, match="out of range"):
            smu.set_co_offset(0, min_val - 1)

        with pytest.raises(ValueError, match="out of range"):
            smu.set_co_offset(0, max_val + 1)

        for bound in (min_val, max_val):
            smu.set_co_offset(0, bound)
            sent = struct.unpack("<I", (smu_dir / "smu_args").read_bytes()[:4])[0]
            assert sent == encode_co_arg(0, bound, gen), f"the bound {bound} never reached the SMU"

    def test_extreme_values_rejected(self, tmp_path):
        """Absurdly large CO values must be rejected."""
        cmds = get_commands(CPUGeneration.ZEN5_GRANITE_RIDGE)
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        (smu_dir / "smu_args").write_bytes(struct.pack("<6I", 0, 0, 0, 0, 0, 0))
        (smu_dir / "rsmu_cmd").write_bytes(struct.pack("<I", 1))

        smu = RyzenSMU(cmds, smu_dir)

        with pytest.raises(ValueError):
            smu.set_co_offset(0, -1000)
        with pytest.raises(ValueError):
            smu.set_co_offset(0, 1000)

    def test_all_valid_core_ids(self, tmp_path):
        """Encoding should not crash for any core ID 0-31."""
        for gen in [CPUGeneration.ZEN3_VERMEER, CPUGeneration.ZEN5_GRANITE_RIDGE]:
            for core_id in range(32):
                result = encode_co_arg(core_id, 0, gen)
                assert isinstance(result, int)


class TestSchedulerProcessSafety:
    def _make_scheduler(self, mock_backend, tmp_path, topo=None):
        if topo is None:
            topo = CPUTopology()
            topo.cores[0] = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))
        return CoreScheduler(
            topology=topo,
            backend=mock_backend,
            stress_config=StressConfig(),
            scheduler_config=SchedulerConfig(seconds_per_core=1),
            work_dir=tmp_path,
        )

    def test_stop_with_no_process(self, mock_backend, tmp_path):
        """stop() must not crash when no process is running."""
        sched = self._make_scheduler(mock_backend, tmp_path)
        sched.stop()
        assert sched.state == TestState.STOPPING

    def test_force_stop_with_no_process(self, mock_backend, tmp_path):
        """force_stop() must not crash when no process is running."""
        sched = self._make_scheduler(mock_backend, tmp_path)
        sched.force_stop()
        assert sched.state == TestState.STOPPING

    def test_kill_escalation(self, tmp_path):
        signals_sent = []
        real_killpg = os.killpg

        def recording_killpg(pgid, sig):
            signals_sent.append(sig)
            real_killpg(pgid, sig)

        one = lane(tmp_path)
        supervisor, _, _ = make_supervisor(
            FakeBackend(_child(DEAF_TO_SIGTERM)),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/ready"))),
        )
        with (
            patch.object(execution, "STOP_GRACE_SECONDS", 0.2),
            patch("os.killpg", side_effect=recording_killpg),
        ):
            verdict = run_one(supervisor, one, 2.0)

        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent
        assert signals_sent.index(signal.SIGTERM) < signals_sent.index(signal.SIGKILL)
        assert verdict is not None and not verdict.passed

    def test_kill_handles_vanished_process(self, tmp_path, caplog):
        supervisor, _, _ = make_supervisor(FakeBackend(_child("import time; time.sleep(0.3)")))
        with patch("os.killpg", side_effect=ProcessLookupError):
            verdict = run_one(supervisor, lane(tmp_path), 0.1)
        assert verdict is not None
        assert [r for r in caplog.records if r.levelname == "ERROR"] == []

    def test_kill_handles_os_error(self, tmp_path, caplog):
        supervisor, _, _ = make_supervisor(FakeBackend(_child("import time; time.sleep(0.3)")))
        with patch("os.killpg", side_effect=PermissionError("Operation not permitted")):
            verdict = run_one(supervisor, lane(tmp_path), 0.1)
        assert verdict is not None
        assert {r.levelname for r in caplog.records if "Operation not permitted" in r.getMessage()} == {"ERROR"}


class TestDetectorSafety:
    def test_a_missing_kernel_log_is_reported_not_raised(self, tmp_path):
        det = ErrorDetector(tmp_path / "no-kmsg")
        det.reset()
        read = det.read_new(final=True)
        assert read.events == ()
        assert "no-kmsg" in read.problem

    def test_a_kernel_log_that_is_a_directory_is_reported_not_raised(self, tmp_path):
        det = ErrorDetector(tmp_path)
        det.reset()
        read = det.read_new(final=True)
        assert read.events == ()
        assert str(tmp_path) in read.problem


class TestSettingsSafety:
    def test_save_only_writes_to_config_dir(self, tmp_path, monkeypatch):
        """save_settings must only create files within CONFIG_DIR."""
        config_dir = tmp_path / "config"
        monkeypatch.setattr("corecycler.config.settings.CONFIG_DIR", config_dir)

        from corecycler.config.settings import AppSettings, save_settings

        save_settings(AppSettings())

        all_files = list(tmp_path.rglob("*.json"))
        for f in all_files:
            assert str(f).startswith(str(config_dir)), f"File written outside config dir: {f}"

    def test_save_profile_only_writes_specified_path(self, tmp_path):
        """save_profile must only write to the specified path."""
        from corecycler.config.settings import TestProfile, save_profile

        target = tmp_path / "profiles" / "test.json"
        save_profile(TestProfile(), target)

        all_json = list(tmp_path.rglob("*.json"))
        assert len(all_json) == 1
        assert all_json[0] == target

    def test_settings_no_path_traversal(self, tmp_path, monkeypatch):
        """Settings with path traversal in work_dir should be stored as-is."""
        from corecycler.config.settings import AppSettings, load_settings, save_settings

        config_dir = tmp_path / "config"
        monkeypatch.setattr("corecycler.config.settings.CONFIG_DIR", config_dir)

        s = AppSettings(work_dir="/tmp/../etc/passwd")
        save_settings(s)
        loaded = load_settings()
        assert loaded.work_dir == "/tmp/../etc/passwd"


class TestBackendSafety:
    def test_mprime_prepare_stays_in_work_dir(self, tmp_path):
        """mprime prepare must only create files within work_dir."""
        work = tmp_path / "work"
        work.mkdir()
        before = set(tmp_path.rglob("*"))
        backend = MprimeBackend()
        backend.prepare(work, StressConfig())

        created = set(tmp_path.rglob("*")) - before
        assert created
        assert all(path.is_relative_to(work) for path in created), sorted(created - set(work.rglob("*")))

        sibling = tmp_path / "sibling.txt"
        assert not sibling.exists()

    def test_mprime_prepare_doesnt_clobber_existing(self, tmp_path):
        """Prepare should not delete existing files in work_dir."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "existing.dat").write_text("preserve me")

        backend = MprimeBackend()
        backend.prepare(work, StressConfig())

        assert (work / "existing.dat").read_text() == "preserve me"


class TestEncodeSafety:
    def test_encode_all_generations_all_cores(self):
        """Encoding must not crash for any supported generation and core 0-31."""
        for gen in [
            CPUGeneration.ZEN3_VERMEER,
            CPUGeneration.ZEN3D_WARHOL,
            CPUGeneration.ZEN4_RAPHAEL,
            CPUGeneration.ZEN5_GRANITE_RIDGE,
        ]:
            cmds = get_commands(gen)
            co_min, co_max = cmds.co_range
            for core_id in range(32):
                for value in [co_min, co_max, 0]:
                    result = encode_co_arg(core_id, value, gen)
                    assert isinstance(result, int)
                    assert result >= 0

    def test_unsupported_generation_raises_cleanly(self):
        """Unsupported generations must raise ValueError, not crash."""
        with pytest.raises(ValueError):
            encode_co_arg(0, 0, CPUGeneration.UNKNOWN)
        with pytest.raises(ValueError):
            encode_co_arg(0, 0, CPUGeneration.ZEN2_MATISSE)


class TestTopologySafety:
    def test_empty_topology_helpers(self):
        """Helper functions must not crash on empty topology."""
        from corecycler.engine.topology import get_first_logical_cpu, get_physical_core_list

        topo = CPUTopology()
        assert get_physical_core_list(topo) == []
        assert get_first_logical_cpu(topo, 0) == 0
        assert get_first_logical_cpu(topo, 999) == 999

    def test_detect_topology_never_crashes(self):
        """detect_topology must handle any environment without crashing."""
        mock_cpuinfo = MagicMock()
        mock_cpuinfo.exists.return_value = False
        mock_sysfs = MagicMock()
        mock_sysfs.exists.return_value = False

        with (
            patch("corecycler.engine.topology.CPUINFO", mock_cpuinfo),
            patch("corecycler.engine.topology.SYSFS_CPU", mock_sysfs),
        ):
            from corecycler.engine.topology import detect_topology

            topo = detect_topology()
            assert isinstance(topo, CPUTopology)
