"""Comprehensive tests for CPU topology detection."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.topology import (
    CPUTopology,
    LogicalCPU,
    PhysicalCore,
    _detect_ccd_layout,
    _detect_x3d,
    _parse_cpuinfo,
    _parse_sysfs,
    detect_topology,
    get_first_logical_cpu,
    get_physical_core_list,
)

sys.path.insert(0, str(Path(__file__).parent))
from conftest import (
    CPUINFO_DUAL_CCD_SMT,
    CPUINFO_INTEL_10CORE_SMT,
    CPUINFO_SINGLE_CCD_NO_SMT,
    CPUINFO_X3D_SINGLE_CCD,
    build_topology,
)


# ---------------------------------------------------------------------------
# Helper to run _parse_cpuinfo with fake data
# ---------------------------------------------------------------------------


def parse_cpuinfo_from_text(text: str) -> CPUTopology:
    topo = CPUTopology()
    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.read_text.return_value = text
    with patch("engine.topology.CPUINFO", mock_path):
        _parse_cpuinfo(topo)
    return topo


# ---------------------------------------------------------------------------
# _parse_cpuinfo tests
# ---------------------------------------------------------------------------


class TestParseCpuinfo:
    def test_dual_ccd_smt_core_count(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        assert topo.physical_cores == 8
        assert topo.logical_cpus_count == 16
        assert topo.smt_enabled is True

    def test_dual_ccd_smt_model_name(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        assert "9950X3D" in topo.model_name
        assert topo.vendor == "AuthenticAMD"
        assert topo.family == 26
        assert topo.model == 68
        assert topo.stepping == 2

    def test_dual_ccd_smt_logical_map(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        assert len(topo.logical_map) == 16
        for i in range(16):
            assert i in topo.logical_map

    def test_dual_ccd_smt_siblings(self):
        """Each physical core should have exactly 2 SMT siblings."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        for lcpu in topo.logical_map.values():
            assert len(lcpu.core_cpus) == 2
            assert lcpu.logical_id in lcpu.core_cpus

    def test_single_ccd_no_smt(self):
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        assert topo.physical_cores == 4
        assert topo.logical_cpus_count == 4
        assert topo.smt_enabled is False
        assert topo.vendor == "AuthenticAMD"
        assert topo.family == 25

    def test_single_ccd_no_smt_siblings(self):
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        for lcpu in topo.logical_map.values():
            assert len(lcpu.core_cpus) == 1

    def test_intel_with_smt(self):
        topo = parse_cpuinfo_from_text(CPUINFO_INTEL_10CORE_SMT)
        assert topo.physical_cores == 2
        assert topo.logical_cpus_count == 4
        assert topo.smt_enabled is True
        assert topo.vendor == "GenuineIntel"
        assert topo.family == 6

    def test_x3d_single_ccd(self):
        topo = parse_cpuinfo_from_text(CPUINFO_X3D_SINGLE_CCD)
        assert "7800X3D" in topo.model_name
        assert topo.physical_cores == 2
        assert topo.smt_enabled is True

    def test_empty_cpuinfo(self):
        topo = parse_cpuinfo_from_text("")
        assert topo.physical_cores == 0
        assert topo.logical_cpus_count == 0
        assert topo.smt_enabled is False

    def test_missing_cpuinfo(self):
        topo = CPUTopology()
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("engine.topology.CPUINFO", mock_path):
            _parse_cpuinfo(topo)
        assert topo.physical_cores == 0

    def test_no_trailing_blank_line(self):
        """Last entry without trailing blank should still be parsed."""
        text = "processor\t: 0\nvendor_id\t: AuthenticAMD\ncpu family\t: 25\nmodel\t\t: 33\nmodel name\t: Test\nstepping\t: 1\ncore id\t\t: 0\nphysical id\t: 0"
        topo = parse_cpuinfo_from_text(text)
        assert topo.physical_cores == 1
        assert 0 in topo.logical_map

    def test_package_id_preserved(self):
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        for lcpu in topo.logical_map.values():
            assert lcpu.package_id == 0

    def test_only_first_model_name_used(self):
        """Model name/vendor/family should only be set from the first processor entry."""
        text = CPUINFO_SINGLE_CCD_NO_SMT
        topo = parse_cpuinfo_from_text(text)
        assert "5800X" in topo.model_name
        assert topo.family == 25


# ---------------------------------------------------------------------------
# _parse_sysfs tests
# ---------------------------------------------------------------------------


class TestParseSysfs:
    def test_missing_sysfs(self):
        topo = CPUTopology()
        mock_path = MagicMock()
        mock_path.exists.return_value = False
        with patch("engine.topology.SYSFS_CPU", mock_path):
            _parse_sysfs(topo)
        # should not crash

    def test_online_range_simple(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-7\n")
        topo = CPUTopology()
        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 8

    def test_online_range_multi(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-15,32-47\n")
        topo = CPUTopology()
        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 32

    def test_online_single_cpus(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0,1,2,3\n")
        topo = CPUTopology()
        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        assert topo.logical_cpus_count == 4

    def test_does_not_overwrite_existing_count(self, tmp_path):
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-3\n")
        topo = CPUTopology()
        topo.logical_cpus_count = 99
        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _parse_sysfs(topo)
        # should keep existing count (99), not overwrite
        assert topo.logical_cpus_count == 99


# ---------------------------------------------------------------------------
# _detect_ccd_layout tests
# ---------------------------------------------------------------------------


class TestDetectCCDLayout:
    def test_single_l3_group(self, tmp_path):
        """All cores sharing one L3 = 1 CCD."""
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)

        cpu_dir = tmp_path / "cpu"
        for i in range(4):
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            (cache_dir / "id").write_text("0")

        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)

        assert topo.ccds == 1
        assert len(topo.cores) == 4
        for pc in topo.cores.values():
            assert pc.ccd == 0

    def test_two_l3_groups(self, tmp_path):
        """Cores split across two L3 caches = 2 CCDs."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)

        cpu_dir = tmp_path / "cpu"
        for i in range(16):
            phys_core = topo.logical_map[i].physical_core
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            # cores 0-3 -> L3 id=0, cores 4-7 -> L3 id=1
            l3_id = "0" if phys_core < 4 else "1"
            (cache_dir / "id").write_text(l3_id)

        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)

        assert topo.ccds == 2
        for pc in topo.cores.values():
            expected_ccd = 0 if pc.core_id < 4 else 1
            assert pc.ccd == expected_ccd

    def test_no_cache_dirs(self, tmp_path):
        """If no cache sysfs exists, default to 1 CCD."""
        topo = parse_cpuinfo_from_text(CPUINFO_SINGLE_CCD_NO_SMT)
        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _detect_ccd_layout(topo)
        assert topo.ccds == 1


# ---------------------------------------------------------------------------
# _detect_x3d tests
# ---------------------------------------------------------------------------


class TestDetectX3D:
    @pytest.mark.parametrize(
        "model_name,expected",
        [
            ("AMD Ryzen 9 9950X3D", True),
            ("AMD Ryzen 7 7800X3D", True),
            ("AMD Ryzen 7 5800X3D", True),
            ("AMD Ryzen 9 9900X3D", True),
            ("AMD Ryzen 9 7950X3D", True),
            ("AMD Ryzen 9 9950X", False),
            ("AMD Ryzen 7 5800X", False),
            ("Intel Core i9-10900K", False),
        ],
    )
    def test_x3d_detection_by_name(self, model_name, expected):
        topo = CPUTopology(model_name=model_name)
        with patch("engine.topology.SYSFS_CPU", MagicMock()):
            _detect_x3d(topo)
        assert topo.is_x3d == expected

    def test_single_ccd_x3d_vcache(self, tmp_path):
        """Single CCD X3D: the one CCD should be marked as V-Cache."""
        topo = parse_cpuinfo_from_text(CPUINFO_X3D_SINGLE_CCD)
        topo.ccds = 1
        # Build cores from logical_map (parse_cpuinfo_from_text doesn't populate cores)
        seen: set[int] = set()
        for lcpu in topo.logical_map.values():
            pc = lcpu.physical_core
            if pc not in seen:
                seen.add(pc)
                topo.cores[pc] = PhysicalCore(
                    core_id=pc,
                    ccd=0,
                    ccx=None,
                    logical_cpus=lcpu.core_cpus,
                )

        with patch("engine.topology.SYSFS_CPU", MagicMock()):
            _detect_x3d(topo)

        assert topo.is_x3d is True
        assert topo.vcache_ccd == 0
        for pc in topo.cores.values():
            assert pc.has_vcache is True

    def test_dual_ccd_x3d_vcache_detection(self, tmp_path):
        """Dual CCD X3D: CCD with larger L3 should be V-Cache."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        topo.ccds = 2

        # Build cores from logical_map and assign CCDs
        seen: set[int] = set()
        for lcpu in topo.logical_map.values():
            pc = lcpu.physical_core
            if pc not in seen:
                seen.add(pc)
                ccd = 0 if pc < 4 else 1
                topo.cores[pc] = PhysicalCore(
                    core_id=pc,
                    ccd=ccd,
                    ccx=None,
                    logical_cpus=lcpu.core_cpus,
                )

        # Create sysfs with L3 sizes: CCD0 = 96M (V-Cache), CCD1 = 32M
        cpu_dir = tmp_path / "cpu"
        for core in topo.cores.values():
            first_cpu = core.logical_cpus[0]
            cache_dir = cpu_dir / f"cpu{first_cpu}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            size = "96M" if core.ccd == 0 else "32M"
            (cache_dir / "size").write_text(size)

        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _detect_x3d(topo)

        assert topo.is_x3d is True
        assert topo.vcache_ccd == 0
        for pc in topo.cores.values():
            if pc.ccd == 0:
                assert pc.has_vcache is True
            else:
                assert pc.has_vcache is False

    def test_dual_ccd_x3d_vcache_on_ccd1(self, tmp_path):
        """V-Cache on CCD1 (largest L3 wins)."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        topo.ccds = 2

        seen: set[int] = set()
        for lcpu in topo.logical_map.values():
            pc = lcpu.physical_core
            if pc not in seen:
                seen.add(pc)
                ccd = 0 if pc < 4 else 1
                topo.cores[pc] = PhysicalCore(
                    core_id=pc, ccd=ccd, ccx=None, logical_cpus=lcpu.core_cpus
                )

        cpu_dir = tmp_path / "cpu"
        for core in topo.cores.values():
            first_cpu = core.logical_cpus[0]
            cache_dir = cpu_dir / f"cpu{first_cpu}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            size = "32M" if core.ccd == 0 else "96M"
            (cache_dir / "size").write_text(size)

        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _detect_x3d(topo)

        assert topo.vcache_ccd == 1

    def test_l3_size_K_unit(self, tmp_path):
        """L3 sizes in K should be parsed correctly."""
        topo = parse_cpuinfo_from_text(CPUINFO_DUAL_CCD_SMT)
        topo.ccds = 2

        seen: set[int] = set()
        for lcpu in topo.logical_map.values():
            pc = lcpu.physical_core
            if pc not in seen:
                seen.add(pc)
                ccd = 0 if pc < 4 else 1
                topo.cores[pc] = PhysicalCore(
                    core_id=pc, ccd=ccd, ccx=None, logical_cpus=lcpu.core_cpus
                )

        cpu_dir = tmp_path / "cpu"
        for core in topo.cores.values():
            first_cpu = core.logical_cpus[0]
            cache_dir = cpu_dir / f"cpu{first_cpu}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            size = "98304K" if core.ccd == 0 else "32768K"
            (cache_dir / "size").write_text(size)

        with patch("engine.topology.SYSFS_CPU", cpu_dir):
            _detect_x3d(topo)

        assert topo.vcache_ccd == 0

    def test_non_x3d_skips_vcache_detection(self):
        topo = CPUTopology(model_name="AMD Ryzen 9 5950X", ccds=2)
        _detect_x3d(topo)
        assert topo.is_x3d is False
        assert topo.vcache_ccd is None


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_get_first_logical_cpu(self, topo_dual_ccd_x3d):
        topo = topo_dual_ccd_x3d
        for core_id, core in topo.cores.items():
            result = get_first_logical_cpu(topo, core_id)
            assert result == core.logical_cpus[0]

    def test_get_first_logical_cpu_missing_core(self):
        topo = CPUTopology()
        assert get_first_logical_cpu(topo, 999) == 999

    def test_get_physical_core_list(self, topo_dual_ccd_x3d):
        cores = get_physical_core_list(topo_dual_ccd_x3d)
        assert cores == sorted(cores)
        assert cores == sorted(set(cores))
        assert len(cores) == 8

    def test_get_physical_core_list_empty(self):
        topo = CPUTopology()
        assert get_physical_core_list(topo) == []


# ---------------------------------------------------------------------------
# Integration-style test using detect_topology with full mocking
# ---------------------------------------------------------------------------


class TestDetectTopologyIntegration:
    def test_with_mocked_cpuinfo_and_sysfs(self, tmp_path):
        """Full detect_topology with mocked /proc/cpuinfo and /sys."""
        mock_cpuinfo = MagicMock()
        mock_cpuinfo.exists.return_value = True
        mock_cpuinfo.read_text.return_value = CPUINFO_SINGLE_CCD_NO_SMT

        cpu_dir = tmp_path / "cpu"
        cpu_dir.mkdir()
        (cpu_dir / "online").write_text("0-3")
        for i in range(4):
            cache_dir = cpu_dir / f"cpu{i}" / "cache" / "index3"
            cache_dir.mkdir(parents=True)
            (cache_dir / "level").write_text("3")
            (cache_dir / "id").write_text("0")

        with (
            patch("engine.topology.CPUINFO", mock_cpuinfo),
            patch("engine.topology.SYSFS_CPU", cpu_dir),
        ):
            topo = detect_topology()

        assert topo.physical_cores == 4
        assert topo.logical_cpus_count == 4
        assert topo.smt_enabled is False
        assert topo.ccds == 1
        assert topo.is_x3d is False

    def test_completely_missing_everything(self):
        """Should not crash even if /proc/cpuinfo and /sys are missing."""
        mock_cpuinfo = MagicMock()
        mock_cpuinfo.exists.return_value = False
        mock_sysfs = MagicMock()
        mock_sysfs.exists.return_value = False

        with (
            patch("engine.topology.CPUINFO", mock_cpuinfo),
            patch("engine.topology.SYSFS_CPU", mock_sysfs),
        ):
            topo = detect_topology()

        assert topo.physical_cores == 0
        assert topo.logical_cpus_count == 0


# ---------------------------------------------------------------------------
# Dataclass tests
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_logical_cpu_frozen(self):
        lcpu = LogicalCPU(logical_id=0, physical_core=0, package_id=0, core_cpus=(0, 8))
        with pytest.raises(AttributeError):
            lcpu.logical_id = 1  # type: ignore[misc]

    def test_physical_core_frozen(self):
        pc = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0, 8))
        with pytest.raises(AttributeError):
            pc.core_id = 1  # type: ignore[misc]

    def test_physical_core_vcache_default(self):
        pc = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0,))
        assert pc.has_vcache is False

    def test_cpu_topology_defaults(self):
        topo = CPUTopology()
        assert topo.model_name == ""
        assert topo.vendor == ""
        assert topo.ccds == 0
        assert topo.is_x3d is False
        assert topo.vcache_ccd is None
        assert topo.cores == {}
        assert topo.logical_map == {}
