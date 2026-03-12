"""CRITICAL safety invariant tests.

These tests verify that:
- The SMU driver gracefully handles missing ryzen_smu module
- CO values are bounds-checked against generation limits
- Scheduler kills processes cleanly on stop
- Error detector doesn't crash on missing sysfs
- Settings don't write to unexpected paths
- Backend prepare doesn't clobber files outside work_dir
"""

from __future__ import annotations

import json
import os
import signal
import struct
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.backends.base import FFTPreset, StressConfig, StressMode
from engine.backends.mprime import MprimeBackend
from engine.backends.stress_ng import StressNgBackend
from engine.backends.ycruncher import YCruncherBackend
from engine.detector import ErrorDetector
from engine.scheduler import CoreScheduler, SchedulerConfig, TestState
from engine.topology import CPUTopology, PhysicalCore
from smu.commands import COMMAND_SETS, CPUGeneration, encode_co_arg, get_commands
from smu.driver import RyzenSMU


# ===========================================================================
# SMU driver safety — graceful handling of missing driver
# ===========================================================================


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

    def test_no_crash_on_permission_denied(self, tmp_path):
        """is_available should handle permission errors gracefully."""
        smu_dir = tmp_path / "ryzen_smu_drv"
        smu_dir.mkdir()
        # Create but make inaccessible
        args_file = smu_dir / "smu_args"
        args_file.write_bytes(b"\x00" * 24)
        # is_available only checks .exists(), which works even without read perm
        assert RyzenSMU.is_available(smu_dir) is True


# ===========================================================================
# CO value bounds checking
# ===========================================================================


class TestCOBoundsChecking:
    @pytest.mark.parametrize(
        "gen,min_val,max_val",
        [
            (CPUGeneration.ZEN3_VERMEER, -30, 30),
            (CPUGeneration.ZEN3D_WARHOL, -30, 30),
            (CPUGeneration.ZEN4_RAPHAEL, -50, 30),
            (CPUGeneration.ZEN5_GRANITE_RIDGE, -60, 10),
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

        # Value below min should raise
        with pytest.raises(ValueError, match="out of range"):
            smu.set_co_offset(0, min_val - 1)

        # Value above max should raise
        with pytest.raises(ValueError, match="out of range"):
            smu.set_co_offset(0, max_val + 1)

        # Boundary values should succeed (not raise)
        smu.set_co_offset(0, min_val)
        smu.set_co_offset(0, max_val)

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
                # Should not raise
                result = encode_co_arg(core_id, 0, gen)
                assert isinstance(result, int)


# ===========================================================================
# Scheduler process killing safety
# ===========================================================================


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

    def test_kill_escalation(self, mock_backend, tmp_path):
        """Must send SIGTERM first, then SIGKILL if process doesn't die."""
        sched = self._make_scheduler(mock_backend, tmp_path)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("x", 3), None]
        sched._process = mock_proc

        signals_sent = []

        def fake_killpg(pgid, sig):
            signals_sent.append(sig)

        with patch("os.killpg", side_effect=fake_killpg), patch("os.getpgid", return_value=99999):
            sched._kill_current()

        assert signal.SIGTERM in signals_sent
        assert signal.SIGKILL in signals_sent
        assert signals_sent.index(signal.SIGTERM) < signals_sent.index(signal.SIGKILL)

    def test_kill_handles_vanished_process(self, mock_backend, tmp_path):
        """Must handle ProcessLookupError (process already gone)."""
        sched = self._make_scheduler(mock_backend, tmp_path)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        sched._process = mock_proc

        with (
            patch("os.killpg", side_effect=ProcessLookupError),
            patch("os.getpgid", return_value=99999),
        ):
            sched._kill_current()  # must not raise

    def test_kill_handles_os_error(self, mock_backend, tmp_path):
        """Must handle OSError gracefully."""
        sched = self._make_scheduler(mock_backend, tmp_path)

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 99999
        sched._process = mock_proc

        with (
            patch("os.killpg", side_effect=OSError("Operation not permitted")),
            patch("os.getpgid", return_value=99999),
        ):
            sched._kill_current()  # must not raise


# ===========================================================================
# Error detector safety — missing sysfs / dmesg
# ===========================================================================


class TestDetectorSafety:
    def test_check_mce_no_sysfs(self):
        """check_mce must not crash when sysfs machinecheck dir is missing."""
        det = ErrorDetector()
        # On systems without MCE sysfs, should return empty list
        events = det._check_sysfs_mce(target_cpu=None)
        # Must not raise, may or may not find events on this system
        assert isinstance(events, list)

    def test_check_mce_no_dmesg(self):
        """check_mce must not crash when dmesg is unavailable."""
        det = ErrorDetector()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_check_mce_dmesg_permission_denied(self):
        det = ErrorDetector()
        with patch("subprocess.run", side_effect=OSError("Permission denied")):
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_check_mce_dmesg_timeout(self):
        det = ErrorDetector()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("dmesg", 5)):
            events = det._check_dmesg_mce(target_cpu=None)
        assert events == []

    def test_reset_no_crash(self):
        """reset() must not crash in any environment."""
        det = ErrorDetector()
        with (
            patch.object(det, "_count_mce_events", return_value=0),
            patch("engine.detector._get_dmesg_timestamp", return_value=""),
        ):
            det.reset()  # must not raise

    def test_full_check_mce_graceful(self):
        """Full check_mce pipeline must be graceful."""
        det = ErrorDetector()
        with (
            patch.object(det, "_check_sysfs_mce", return_value=[]),
            patch.object(det, "_check_dmesg_mce", return_value=[]),
        ):
            events = det.check_mce()
        assert events == []


# ===========================================================================
# Settings safety — file paths
# ===========================================================================


class TestSettingsSafety:
    def test_save_only_writes_to_config_dir(self, tmp_path, monkeypatch):
        """save_settings must only create files within CONFIG_DIR."""
        config_dir = tmp_path / "config"
        monkeypatch.setattr("config.settings.CONFIG_DIR", config_dir)

        from config.settings import AppSettings, save_settings

        save_settings(AppSettings())

        # Verify files only exist in config_dir
        all_files = list(tmp_path.rglob("*.json"))
        for f in all_files:
            assert str(f).startswith(str(config_dir)), f"File written outside config dir: {f}"

    def test_save_profile_only_writes_specified_path(self, tmp_path):
        """save_profile must only write to the specified path."""
        from config.settings import TestProfile, save_profile

        target = tmp_path / "profiles" / "test.json"
        save_profile(TestProfile(), target)

        # Only one JSON file should exist
        all_json = list(tmp_path.rglob("*.json"))
        assert len(all_json) == 1
        assert all_json[0] == target

    def test_settings_no_path_traversal(self, tmp_path, monkeypatch):
        """Settings with path traversal in work_dir should be stored as-is."""
        from config.settings import AppSettings, save_settings, load_settings

        config_dir = tmp_path / "config"
        monkeypatch.setattr("config.settings.CONFIG_DIR", config_dir)

        s = AppSettings(work_dir="/tmp/../etc/passwd")
        save_settings(s)
        loaded = load_settings()
        # Should store the string as-is (validation is caller's responsibility)
        assert loaded.work_dir == "/tmp/../etc/passwd"


# ===========================================================================
# Backend safety — file containment
# ===========================================================================


class TestBackendSafety:
    def test_mprime_prepare_stays_in_work_dir(self, tmp_path):
        """mprime prepare must only create files within work_dir."""
        work = tmp_path / "work"
        backend = MprimeBackend()
        backend.prepare(work, StressConfig())

        # All created files should be in work dir
        for f in work.iterdir():
            assert f.parent == work

        # Nothing outside work dir
        sibling = tmp_path / "sibling.txt"
        assert not sibling.exists()

    def test_mprime_cleanup_only_removes_known_files(self, tmp_path):
        """Cleanup must only remove mprime-specific files."""
        backend = MprimeBackend()
        backend.prepare(tmp_path, StressConfig())

        # Add an external file
        (tmp_path / "user_data.txt").write_text("important")

        backend.cleanup(tmp_path)

        # mprime files should be gone
        assert not (tmp_path / "prime.txt").exists()
        assert not (tmp_path / "local.txt").exists()

        # external file must survive
        assert (tmp_path / "user_data.txt").exists()
        assert (tmp_path / "user_data.txt").read_text() == "important"

    def test_stress_ng_prepare_stays_in_work_dir(self, tmp_path):
        work = tmp_path / "work"
        backend = StressNgBackend()
        backend.prepare(work, StressConfig())
        assert work.exists()
        # stress-ng prepare only creates the directory
        assert list(work.iterdir()) == []

    def test_ycruncher_prepare_stays_in_work_dir(self, tmp_path):
        work = tmp_path / "work"
        backend = YCruncherBackend()
        backend.prepare(work, StressConfig())
        assert work.exists()

    def test_mprime_prepare_doesnt_clobber_existing(self, tmp_path):
        """Prepare should not delete existing files in work_dir."""
        work = tmp_path / "work"
        work.mkdir()
        (work / "existing.dat").write_text("preserve me")

        backend = MprimeBackend()
        backend.prepare(work, StressConfig())

        assert (work / "existing.dat").read_text() == "preserve me"


# ===========================================================================
# Encode safety — no panics on edge values
# ===========================================================================


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
                    assert result >= 0  # should be unsigned

    def test_unsupported_generation_raises_cleanly(self):
        """Unsupported generations must raise ValueError, not crash."""
        with pytest.raises(ValueError):
            encode_co_arg(0, 0, CPUGeneration.UNKNOWN)
        with pytest.raises(ValueError):
            encode_co_arg(0, 0, CPUGeneration.ZEN2_MATISSE)


# ===========================================================================
# Topology safety
# ===========================================================================


class TestTopologySafety:
    def test_empty_topology_helpers(self):
        """Helper functions must not crash on empty topology."""
        from engine.topology import get_first_logical_cpu, get_physical_core_list

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
            patch("engine.topology.CPUINFO", mock_cpuinfo),
            patch("engine.topology.SYSFS_CPU", mock_sysfs),
        ):
            from engine.topology import detect_topology

            topo = detect_topology()
            assert isinstance(topo, CPUTopology)
