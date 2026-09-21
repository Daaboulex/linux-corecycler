"""The one live memory budget every stressapptest instance is sized from.

Every machine here is injected as /proc and cgroup trees; nothing reads the
box the suite runs on.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corecycler.engine.backends.base import StressConfig
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.engine.memory_budget import MemoryBudgetUnavailable, memory_budget

KIB_PER_GIB = 1024 * 1024
OWN_CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/app-corecycler.scope"
SMT_LANE_THREADS = 2


def _meminfo(total_kib: int, available_kib: int) -> str:
    return f"MemTotal:       {total_kib} kB\nMemFree:         1024 kB\nMemAvailable:   {available_kib} kB\n"


@pytest.fixture
def machine(mock_sysfs, tmp_path):
    def build(*, meminfo: str, cgroup: dict | None = None, self_cgroup: str = f"0::{OWN_CGROUP}\n"):
        proc_tree = {"meminfo": meminfo}
        if self_cgroup is not None:
            proc_tree["self"] = {"cgroup": self_cgroup}
        proc = mock_sysfs(proc_tree, base=tmp_path / "proc")
        cg = mock_sysfs(cgroup or {}, base=tmp_path / "cgroup")
        return {"proc_base": proc, "cgroup_base": cg}

    return build


def _budget(machine, total_gib, available_gib, instances, **kw):
    bases = machine(meminfo=_meminfo(total_gib * KIB_PER_GIB, available_gib * KIB_PER_GIB), **kw)
    return memory_budget(instances, StressapptestBackend.minimum_memory_mb(SMT_LANE_THREADS), **bases)


SIZED_MACHINES = [
    pytest.param(4, 3, 2, id="4 GB board, 2 cores"),
    pytest.param(8, 5, 4, id="8 GB laptop, 4 cores"),
    pytest.param(16, 12, 6, id="16 GB desktop, 6 cores"),
    pytest.param(32, 28, 8, id="32 GB, 8 cores (issue 17)"),
    pytest.param(64, 56, 16, id="64 GB, 16 cores"),
    pytest.param(128, 112, 32, id="128 GB, 32 cores"),
    pytest.param(256, 224, 64, id="256 GB, 64 cores"),
    pytest.param(2048, 1900, 256, id="2 TB server, 256 cores"),
]


class TestEveryMachine:
    @pytest.mark.parametrize(("total_gib", "available_gib", "instances"), SIZED_MACHINES)
    def test_the_instances_together_never_exceed_the_usable_share(self, machine, total_gib, available_gib, instances):
        budget = _budget(machine, total_gib, available_gib, instances)
        assert budget.fits
        assert budget.instances == instances
        assert budget.per_instance_mb * instances <= budget.usable_mb
        assert budget.usable_mb + budget.headroom_mb == budget.available_mb
        assert budget.total_mb == total_gib * 1024
        assert budget.available_mb == available_gib * 1024

    @pytest.mark.parametrize(("total_gib", "available_gib", "instances"), SIZED_MACHINES)
    def test_headroom_keeps_at_least_a_gigabyte_and_a_tenth_and_never_shrinks_on_a_bigger_box(
        self, machine, total_gib, available_gib, instances
    ):
        budget = _budget(machine, total_gib, available_gib, instances)
        assert budget.headroom_mb >= 1024
        assert budget.headroom_mb >= budget.ceiling_mb // 10
        assert budget.headroom_mb < budget.usable_mb
        smaller = _budget(machine, total_gib // 2, available_gib // 2, instances)
        assert smaller.headroom_mb <= budget.headroom_mb

    def test_a_large_box_keeps_more_headroom_than_a_small_one(self, machine):
        assert _budget(machine, 64, 56, 8).headroom_mb > _budget(machine, 16, 12, 8).headroom_mb

    def test_the_reporters_machine_gets_a_share_the_kernel_can_grant(self, machine, tmp_path, on_path):
        budget = _budget(machine, 32, 28, 8)
        share = budget.per_instance_mb
        assert budget.fits
        assert budget.headroom_mb >= 1024
        assert budget.headroom_mb >= budget.ceiling_mb // 10
        assert share * 8 + budget.headroom_mb <= budget.ceiling_mb < (share + 1) * 8 + budget.headroom_mb
        on_path({"stressapptest": "/usr/bin/stressapptest"})
        cmd = StressapptestBackend().get_command(StressConfig(threads=2, memory_mb=share), tmp_path)
        assert cmd[cmd.index("-M") + 1] == str(share)

    def test_a_single_instance_gets_the_whole_usable_share(self, machine):
        budget = _budget(machine, 16, 12, 1)
        assert budget.per_instance_mb == budget.usable_mb

    def test_a_starved_machine_does_not_fit(self, machine):
        bases = machine(meminfo=_meminfo(32 * KIB_PER_GIB, 40 * 1024))
        budget = memory_budget(8, StressapptestBackend.minimum_memory_mb(SMT_LANE_THREADS), **bases)
        assert not budget.fits
        assert budget.usable_mb == 0
        assert budget.per_instance_mb < budget.minimum_per_instance_mb
        text = budget.describe()
        assert f"8 x {budget.per_instance_mb} MB" in text
        assert f"floor {budget.minimum_per_instance_mb} MB" in text

    def test_a_share_that_covers_nothing_does_not_fit_even_though_stressapptest_would_launch(self, machine):
        budget = _budget(machine, 4, 2, 8)
        assert budget.per_instance_mb >= budget.launch_minimum_mb
        assert budget.per_instance_mb < budget.minimum_per_instance_mb
        assert not budget.fits

    def test_describe_names_every_number_a_log_reader_needs(self, machine):
        budget = _budget(machine, 32, 28, 8)
        text = budget.describe()
        for number in (budget.total_mb, budget.available_mb, budget.headroom_mb, budget.usable_mb):
            assert f"{number} MB" in text
        assert "cgroup limit none" in text
        assert f"8 x {budget.per_instance_mb} MB" in text
        assert f"coverage floor {budget.minimum_per_instance_mb} MB" in text
        assert f"stressapptest minimum {budget.launch_minimum_mb} MB" in text


class TestCgroupLimit:
    def _tree(self, **files_by_path):
        tree: dict = {}
        for path, files in files_by_path.items():
            node = tree
            for part in path.strip("/").split("/"):
                node = node.setdefault(part, {})
            node.update(files)
        return tree

    def test_an_ancestor_limit_caps_the_ceiling_by_what_is_left_under_it(self, machine):
        limit = 8 * 1024**3
        used = 2 * 1024**3
        user_slice = {"memory.max": f"{limit}\n", "memory.current": f"{used}\n"}
        cgroup = self._tree(**{"user.slice/user-1000.slice": user_slice})
        budget = _budget(machine, 64, 56, 16, cgroup=cgroup)
        assert budget.cgroup_limit_mb == 8192
        assert budget.cgroup_used_mb == 2048
        assert budget.ceiling_mb == 6144
        assert budget.per_instance_mb * 16 <= 6144 - budget.headroom_mb
        assert "cgroup limit 8192 MB (memory.max at /user.slice/user-1000.slice, 2048 MB in use)" in budget.describe()

    def test_a_high_watermark_below_the_hard_limit_is_the_bound(self, machine):
        user_slice = {
            "memory.max": f"{8 * 1024**3}\n",
            "memory.high": f"{3 * 1024**3}\n",
            "memory.current": f"{1024**3}\n",
        }
        cgroup = self._tree(**{"user.slice/user-1000.slice": user_slice})
        budget = _budget(machine, 64, 56, 16, cgroup=cgroup)
        assert budget.cgroup_limit_mb == 3072
        assert budget.ceiling_mb == 2048
        assert "memory.high at /user.slice/user-1000.slice" in budget.describe()

    def test_the_lanes_cgroup_is_walked_not_the_apps(self, machine):
        cgroup = self._tree(
            **{
                "user.slice/user-1000.slice": {"memory.max": f"{8 * 1024**3}\n", "memory.current": "0\n"},
                "system.slice": {"memory.max": f"{2 * 1024**3}\n", "memory.current": "0\n"},
            }
        )
        bases = machine(meminfo=_meminfo(64 * KIB_PER_GIB, 56 * KIB_PER_GIB), cgroup=cgroup)
        lanes = memory_budget(16, 5, lane_cgroup="/system.slice", **bases)
        app = memory_budget(16, 5, **bases)
        assert lanes.cgroup_limit_mb == 2048
        assert "memory.max at /system.slice" in lanes.describe()
        assert app.cgroup_limit_mb == 8192

    def test_lanes_at_the_root_see_only_the_root(self, machine):
        cgroup = self._tree(**{"user.slice/user-1000.slice": {"memory.max": f"{8 * 1024**3}\n"}})
        bases = machine(meminfo=_meminfo(64 * KIB_PER_GIB, 56 * KIB_PER_GIB), cgroup=cgroup)
        assert memory_budget(16, 5, lane_cgroup="/", **bases).cgroup_limit_mb is None

    def test_the_tightest_ancestor_wins(self, machine):
        cgroup = self._tree(
            **{
                "user.slice/user-1000.slice": {"memory.max": f"{8 * 1024**3}\n", "memory.current": "0\n"},
                "user.slice/user-1000.slice/user@1000.service/app.slice": {
                    "memory.max": f"{4 * 1024**3}\n",
                    "memory.current": f"{1024**3}\n",
                },
            }
        )
        budget = _budget(machine, 64, 56, 16, cgroup=cgroup)
        assert budget.cgroup_limit_mb == 4096
        assert budget.cgroup_used_mb == 1024

    def test_a_limit_with_no_usage_file_counts_nothing_as_used(self, machine):
        cgroup = self._tree(**{"user.slice": {"memory.max": f"{2 * 1024**3}\n"}})
        budget = _budget(machine, 16, 12, 4, cgroup=cgroup)
        assert budget.cgroup_limit_mb == 2048
        assert budget.cgroup_used_mb == 0
        assert budget.ceiling_mb == 2048

    def test_a_limit_below_what_is_used_leaves_nothing(self, machine):
        cgroup = self._tree(**{"user.slice": {"memory.max": f"{1024**3}\n", "memory.current": f"{2 * 1024**3}\n"}})
        budget = _budget(machine, 16, 12, 4, cgroup=cgroup)
        assert budget.ceiling_mb == 0
        assert not budget.fits

    def test_max_at_every_level_is_no_limit(self, machine):
        unlimited = {"memory.max": "max\n"}
        cgroup = self._tree(**{"user.slice": unlimited, "user.slice/user-1000.slice": unlimited})
        budget = _budget(machine, 16, 12, 4, cgroup=cgroup)
        assert budget.cgroup_limit_mb is None
        assert budget.ceiling_mb == budget.available_mb

    def test_a_limit_above_available_memory_changes_nothing(self, machine):
        cgroup = self._tree(**{"user.slice": {"memory.max": f"{64 * 1024**3}\n", "memory.current": "0\n"}})
        budget = _budget(machine, 16, 12, 4, cgroup=cgroup)
        assert budget.ceiling_mb == budget.available_mb

    def test_a_malformed_limit_refuses_the_budget(self, machine):
        cgroup = self._tree(**{"user.slice": {"memory.max": "lots\n"}})
        with pytest.raises(MemoryBudgetUnavailable, match="memory.max"):
            _budget(machine, 16, 12, 4, cgroup=cgroup)

    def test_without_a_unified_hierarchy_the_system_numbers_are_the_bound(self, machine):
        budget = _budget(machine, 16, 12, 4, self_cgroup="12:memory:/user.slice\n")
        assert budget.cgroup_limit_mb is None
        assert budget.ceiling_mb == budget.available_mb

    def test_without_a_cgroup_file_the_system_numbers_are_the_bound(self, machine):
        budget = _budget(machine, 16, 12, 4, self_cgroup=None)
        assert budget.cgroup_limit_mb is None

    def test_the_root_cgroup_is_walked_once(self, machine):
        cgroup = {"memory.max": f"{3 * 1024**3}\n", "memory.current": "0\n"}
        budget = _budget(machine, 16, 12, 4, cgroup=cgroup, self_cgroup="0::/\n")
        assert budget.cgroup_limit_mb == 3072
        assert budget.cgroup_limit_source == "memory.max at /"


class TestRefusals:
    def test_no_instances_is_refused(self, machine):
        bases = machine(meminfo=_meminfo(KIB_PER_GIB, KIB_PER_GIB))
        with pytest.raises(ValueError, match="instance"):
            memory_budget(0, 5, **bases)

    def test_a_missing_meminfo_field_names_it(self, machine):
        bases = machine(meminfo="MemTotal:       16000000 kB\n")
        with pytest.raises(MemoryBudgetUnavailable, match="MemAvailable"):
            memory_budget(1, 5, **bases)

    def test_a_malformed_meminfo_line_names_it(self, machine):
        bases = machine(meminfo="MemTotal:       lots kB\nMemAvailable:   1 kB\n")
        with pytest.raises(MemoryBudgetUnavailable, match="MemTotal"):
            memory_budget(1, 5, **bases)

    def test_an_unreadable_meminfo_is_refused(self, tmp_path):
        with pytest.raises(MemoryBudgetUnavailable, match="meminfo"):
            memory_budget(1, 5, proc_base=tmp_path / "absent", cgroup_base=tmp_path)


class TestAnyMachine:
    """A property sweep: whatever the box, the shares stay inside what the kernel can grant."""

    @given(
        total_mib=st.integers(min_value=1, max_value=4 * 1024 * 1024),
        available_share=st.integers(min_value=0, max_value=100),
        instances=st.integers(min_value=1, max_value=512),
        lane_threads=st.integers(min_value=1, max_value=8),
        limit_mib=st.one_of(st.none(), st.integers(min_value=0, max_value=4 * 1024 * 1024)),
        used_mib=st.integers(min_value=0, max_value=4 * 1024 * 1024),
    )
    @settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_the_invariants_hold_for_every_machine(
        self, tmp_path, total_mib, available_share, instances, lane_threads, limit_mib, used_mib
    ):
        available_mib = total_mib * available_share // 100
        root = Path(tempfile.mkdtemp(dir=tmp_path))
        proc = root / "proc"
        (proc / "self").mkdir(parents=True)
        (proc / "meminfo").write_text(_meminfo(total_mib * 1024, available_mib * 1024))
        (proc / "self" / "cgroup").write_text(f"0::{OWN_CGROUP}\n")
        cgroup = root / "cgroup"
        slice_dir = cgroup / "user.slice" / "user-1000.slice"
        slice_dir.mkdir(parents=True)
        if limit_mib is not None:
            (slice_dir / "memory.max").write_text(f"{limit_mib * 1024 * 1024}\n")
            (slice_dir / "memory.current").write_text(f"{used_mib * 1024 * 1024}\n")
        minimum = StressapptestBackend.minimum_memory_mb(lane_threads)

        budget = memory_budget(instances, minimum, proc_base=proc, cgroup_base=cgroup)

        assert budget.total_mb == total_mib
        assert budget.available_mb == available_mib
        assert budget.ceiling_mb <= budget.available_mb
        if limit_mib is None:
            assert budget.cgroup_limit_mb is None
            assert budget.ceiling_mb == budget.available_mb
        else:
            assert budget.cgroup_limit_mb == limit_mib
            assert budget.ceiling_mb <= max(0, limit_mib - used_mib)
        assert budget.headroom_mb >= 1024
        assert budget.headroom_mb >= budget.ceiling_mb // 10
        if budget.usable_mb > 0:
            assert budget.usable_mb + budget.headroom_mb == budget.ceiling_mb
        else:
            assert budget.ceiling_mb <= budget.headroom_mb
        assert budget.per_instance_mb * instances <= budget.usable_mb
        assert budget.usable_mb - budget.per_instance_mb * instances < instances
        assert budget.minimum_per_instance_mb >= 256
        assert budget.minimum_per_instance_mb >= minimum
        assert budget.fits == (budget.per_instance_mb >= budget.minimum_per_instance_mb)
        assert budget.fits is False or budget.per_instance_mb >= 256
        assert f"{instances} x {budget.per_instance_mb} MB" in budget.describe()
