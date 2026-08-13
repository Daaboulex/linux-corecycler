"""External-contract inventory -- the single enumerated list of every assumption
CoreCycler makes about hardware, external tools, and OS interfaces that can change
out from under it, each tied to the drift-test that re-verifies it.

Ring A pins are hermetic: they run in every gate (the nix sandbox included) and
catch an accidental EDIT to a pinned constant. Ring B live tests run the REAL
binary or hardware (marked slow + contract, run outside the sandbox on real
hardware) and catch REALITY drift -- a tool, kernel, or silicon change. A contract
that can be verified live must name a Ring B test; test_contracts.py fails on any
live-verifiable contract with no wired Ring B test, so no drift seam sits dormant.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, mock_open, patch

from corecycler.config import tools
from corecycler.engine import containment
from corecycler.engine.backends import BACKEND_REGISTRY, load_all
from corecycler.engine.execution import cpu_times as _cpu_times
from corecycler.monitor.memory import parse_dmidecode_output
from corecycler.monitor.msr import (
    MSR_APERF,
    MSR_CORE_ENERGY,
    MSR_MPERF,
    MSR_PKG_ENERGY,
    MSR_PWR_UNIT,
)
from corecycler.smu.commands import (
    COMMAND_SETS,
    CPUGeneration,
    decode_co_arg,
    detect_generation,
    encode_co_arg,
    get_commands,
)
from corecycler.smu.driver import RyzenSMU, SMUResponse
from corecycler.tuner.engine import _read_cpu_times

if TYPE_CHECKING:
    from collections.abc import Callable

_ZEN5 = CPUGeneration.ZEN5_GRANITE_RIDGE


@dataclass(frozen=True, slots=True)
class Contract:
    """One external assumption and how its drift is detected."""

    name: str
    kind: str
    source: str
    ring_a: Callable[[], None]
    live_verifiable: bool
    ring_b_test: str | None = None


def _pin_co_bit_layout() -> None:
    assert encode_co_arg(0, 0, _ZEN5) == 0
    assert encode_co_arg(8, 0, _ZEN5) == (1 << 28)
    assert encode_co_arg(1, 0, _ZEN5) == (1 << 20)
    assert encode_co_arg(15, 0, _ZEN5) == (1 << 28) | (7 << 20)
    for value in (-50, -30, -1, 0, 1, 10):
        arg = encode_co_arg(3, value, _ZEN5)
        assert decode_co_arg(3, arg, _ZEN5) == value


def _pin_detect_generation_map() -> None:
    cases = [
        (23, 0x71, "AMD Ryzen 9 3950X 16-Core Processor", CPUGeneration.ZEN2_MATISSE),
        (23, 0x31, "AMD Ryzen Threadripper 3960X", CPUGeneration.ZEN2_CASTLE_PEAK),
        (25, 0x08, "AMD Ryzen Threadripper PRO 5965WX", CPUGeneration.ZEN3_CHAGALL),
        (25, 0x18, "AMD Ryzen Threadripper 7970X", CPUGeneration.ZEN4_STORM_PEAK),
        (25, 0x21, "AMD Ryzen 9 5950X 16-Core Processor", CPUGeneration.ZEN3_VERMEER),
        (25, 0x21, "AMD Ryzen 5 5600X 6-Core Processor", CPUGeneration.ZEN3_VERMEER),
        (25, 0x21, "AMD Ryzen 7 5800X3D 8-Core Processor", CPUGeneration.ZEN3D_WARHOL),
        (25, 0x44, "AMD Ryzen 9 6900HX with Radeon Graphics", CPUGeneration.ZEN3_REMBRANDT),
        (25, 0x50, "AMD Ryzen 7 5700G with Radeon Graphics", CPUGeneration.ZEN3_CEZANNE),
        (25, 0x61, "AMD Ryzen 9 7950X3D 16-Core Processor", CPUGeneration.ZEN4_RAPHAEL),
        (25, 0x61, "AMD Ryzen 9 7945HX with Radeon Graphics", CPUGeneration.ZEN4_RAPHAEL),
        (25, 0x74, "AMD Ryzen 7 7840HS w/ Radeon 780M Graphics", CPUGeneration.ZEN4_PHOENIX),
        (25, 0x75, "AMD Ryzen 7 8845HS w/ Radeon 780M Graphics", CPUGeneration.ZEN4_PHOENIX),
        (25, 0x78, "AMD Ryzen 5 7540U w/ Radeon 740M Graphics", CPUGeneration.ZEN4_PHOENIX2),
        (25, 0x7C, "AMD Ryzen 5 PRO 215", CPUGeneration.ZEN4_PHOENIX2),
        (26, 0x44, "AMD Ryzen 9 9950X3D 16-Core Processor", CPUGeneration.ZEN5_GRANITE_RIDGE),
        (26, 0x24, "AMD Ryzen AI 9 HX 370", CPUGeneration.ZEN5_STRIX_POINT),
        (26, 0x60, "AMD Ryzen AI 7 350", CPUGeneration.ZEN5_STRIX_POINT),
        (26, 0x68, "AMD Ryzen AI 5 330", CPUGeneration.ZEN5_STRIX_POINT),
        (26, 0x70, "AMD Ryzen AI Max+ 395", CPUGeneration.ZEN5_STRIX_HALO),
        (26, 0x08, "AMD Ryzen Threadripper 9980X", CPUGeneration.ZEN5_SHIMADA_PEAK),
        (26, 0x08, "AMD Eng Sample 100-000001", CPUGeneration.ZEN5_SHIMADA_PEAK),
        (25, 0x35, "AMD Eng Sample 100-000002", CPUGeneration.UNKNOWN),
        (26, 0x80, "AMD Eng Sample 100-000003", CPUGeneration.UNKNOWN),
        (6, 0xA7, "13th Gen Intel(R) Core(TM) i9-13900K", CPUGeneration.UNKNOWN),
    ]
    for family, model, name, expected in cases:
        assert detect_generation(family, model, name) == expected, (family, model, name)
    for gen in CPUGeneration:
        if gen is not CPUGeneration.UNKNOWN:
            assert get_commands(gen) is not None, gen


def _pin_msr_addresses() -> None:
    assert MSR_APERF == 0xE8
    assert MSR_MPERF == 0xE7
    assert MSR_PWR_UNIT == 0xC0010299
    assert MSR_CORE_ENERGY == 0xC001029A
    assert MSR_PKG_ENERGY == 0xC001029B


def _pin_dmidecode_dimm_parse() -> None:
    sample = (
        "Handle 0x0001, DMI type 17, 92 bytes\nMemory Device\n"
        "\tSize: 32 GB\n\tLocator: DIMM 0\n\tType: DDR5\n"
        "\tSpeed: 6000 MT/s\n\tConfigured Memory Speed: 6000 MT/s\n"
    )
    dimms = parse_dmidecode_output(sample)
    assert len(dimms) == 1
    assert dimms[0].size_gb == 32
    assert dimms[0].speed_mt == 6000
    assert dimms[0].mem_type == "DDR5"


def _pin_zen5_co_range() -> None:
    for gen in (
        CPUGeneration.ZEN5_GRANITE_RIDGE,
        CPUGeneration.ZEN5_STRIX_POINT,
        CPUGeneration.ZEN5_SHIMADA_PEAK,
    ):
        cmds = get_commands(gen)
        assert cmds is not None
        assert cmds.co_range == (-50, 10), (gen.name, cmds.co_range)


def _pin_mprime_31x_config_keys() -> None:
    from corecycler.engine.backends.base import StressConfig, StressMode
    from corecycler.engine.backends.mprime import MprimeBackend

    expectations = {
        StressMode.SSE: {"CpuSupportsAVX=0", "CpuSupportsAVX512F=0"},
        StressMode.AVX: {"CpuSupportsAVX=1", "CpuSupportsAVX2=0", "CpuSupportsAVX512F=0"},
        StressMode.AVX2: {"CpuSupportsAVX2=1", "CpuSupportsFMA3=1", "CpuSupportsAVX512F=0"},
        StressMode.AVX512: {"CpuSupportsAVX2=1", "CpuSupportsAVX512F=1"},
    }
    with tempfile.TemporaryDirectory() as tmp:
        backend = MprimeBackend()
        for mode, wanted in expectations.items():
            backend.prepare(Path(tmp), StressConfig(mode=mode, threads=2))
            local = (Path(tmp) / "local.txt").read_text()
            prime = (Path(tmp) / "prime.txt").read_text()
            for line in wanted:
                assert line in local, (mode, line)
                assert line in prime, (mode, line)
            for content in (local, prime):
                assert "TortureWeak" not in content
                assert "CpuSupportsAVX512=" not in content
                assert "NumCPUs=1" in content
                assert "CoresPerTest=1" in content
            assert "EnableSetAffinity=0" in prime
    assert MprimeBackend.parse_version("Mersenne Prime Test Program: Linux64,Untrusted Prime95,v31.4,build 2") == "31.4"


def _pin_proc_cpus_allowed_list() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / "77" / "task" / "77"
        task.mkdir(parents=True)
        (task / "status").write_text("Name:\tstress\nCpus_allowed_list:\t0-1,4\n")
        assert containment.observed_tree_cpus(77, proc_base=Path(tmp)) == {0, 1, 4}


def _pin_proc_stat_cpu_fields() -> None:
    stat = "cpu  1 2 3 4 5 6 7 8\ncpu0 100 20 300 4000 50 6 7 8\n"
    for reader in (_cpu_times, _read_cpu_times):
        with patch("builtins.open", mock_open(read_data=stat)):
            assert reader(0) == (4050, 4491), reader.__module__


def _pin_apu_co_command_ids() -> None:
    cezanne = get_commands(CPUGeneration.ZEN3_CEZANNE)
    rembrandt = get_commands(CPUGeneration.ZEN3_REMBRANDT)
    phoenix = get_commands(CPUGeneration.ZEN4_PHOENIX)
    phoenix2 = get_commands(CPUGeneration.ZEN4_PHOENIX2)
    strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
    halo = get_commands(CPUGeneration.ZEN5_STRIX_HALO)
    for cmds in (cezanne, rembrandt, phoenix, phoenix2, strix, halo):
        assert cmds is not None
        assert cmds.mailbox == "mp1"
        assert cmds.get_co_mailbox == "rsmu"
    assert (cezanne.set_co_cmd, cezanne.set_all_co_cmd, cezanne.get_co_cmd) == (0x54, 0x55, 0xC3)
    for cmds in (rembrandt, phoenix, phoenix2, strix, halo):
        assert (cmds.set_co_cmd, cmds.set_all_co_cmd) == (0x4B, 0x4C)
    assert phoenix.get_co_cmd == 0xE1
    assert phoenix2.get_co_cmd == 0xE1
    assert rembrandt.get_co_cmd == 0x2F
    assert strix.get_co_cmd == 0xAF
    assert halo.get_co_cmd == 0xAF


def _pin_apu_pbo_command_ids() -> None:
    cezanne = get_commands(CPUGeneration.ZEN3_CEZANNE)
    phoenix = get_commands(CPUGeneration.ZEN4_PHOENIX)
    strix = get_commands(CPUGeneration.ZEN5_STRIX_POINT)
    for cmds in (cezanne, phoenix, strix):
        assert cmds is not None
        assert (
            cmds.set_ppt_cmd,
            cmds.set_tdc_cmd,
            cmds.set_edc_cmd,
            cmds.set_htc_cmd,
        ) == (0x33, 0x38, 0x3A, 0x37)
        assert cmds.get_pbo_scalar_cmd == 0xF
        assert cmds.get_boost_limit_cmd == 0x42
        assert (cmds.enable_oc_cmd, cmds.disable_oc_cmd) == (0x17, 0x18)
        assert cmds.is_overclockable_cmd == 0x82
        assert (
            cmds.transfer_table_cmd,
            cmds.get_dram_base_cmd,
            cmds.get_table_version_cmd,
        ) == (0x65, 0x66, 0x06)
        assert cmds.get_fastest_core_cmd is None
    assert cezanne.set_pbo_scalar_cmd == 0x3F
    assert cezanne.set_boost_limit_cmd is None
    rembrandt = get_commands(CPUGeneration.ZEN3_REMBRANDT)
    assert rembrandt is not None and rembrandt.co_range == (-30, 30)
    for cmds in (phoenix, strix):
        assert cmds.set_pbo_scalar_cmd == 0x3E
        assert cmds.set_boost_limit_cmd == 0x47
        assert cmds.get_ln2_mode_cmd == 0xC4


def _pin_desktop_pbo_command_ids() -> None:
    for gen in (CPUGeneration.ZEN4_RAPHAEL, CPUGeneration.ZEN5_GRANITE_RIDGE):
        cmds = get_commands(gen)
        assert cmds is not None
        assert (
            cmds.set_ppt_cmd,
            cmds.set_tdc_cmd,
            cmds.set_edc_cmd,
            cmds.set_htc_cmd,
        ) == (0x56, 0x57, 0x58, 0x59)
        assert cmds.get_fastest_core_cmd is None
        assert (
            cmds.transfer_table_cmd,
            cmds.get_dram_base_cmd,
            cmds.get_table_version_cmd,
        ) == (0x03, 0x04, 0x05)
    for gen in (CPUGeneration.ZEN2_MATISSE, CPUGeneration.ZEN3_VERMEER):
        cmds = get_commands(gen)
        assert cmds is not None
        assert cmds.get_fastest_core_cmd == 0x59


def _pin_core_slot_mapping() -> None:
    vermeer = get_commands(CPUGeneration.ZEN3_VERMEER)
    assert vermeer is not None and vermeer.uniform_8core_ccds
    assert vermeer.core_fuse_addr == 0x30081D98
    smu = RyzenSMU(vermeer, Path("/nonexistent"))
    topo = MagicMock()
    topo.cores = {c: MagicMock(ccd=0) for c in range(6)}
    fused_off = {2, 3}
    fuse_reads: list[int] = []
    co_calls: list[int] = []

    def responder(cmd, args=(0, 0, 0, 0, 0, 0)):
        co_calls.append(cmd)
        return SMUResponse(success=True, args=(0,) * 6, raw=b"")

    def read_smn(address: int) -> int:
        fuse_reads.append(address)
        return sum(1 << slot for slot in fused_off)

    smu._send_command = responder
    smu.check_smn_readable = lambda: (True, "OK")
    smu.read_smn = read_smn
    smu.set_topology(topo)
    assert co_calls == []
    assert fuse_reads == [vermeer.core_fuse_addr]
    assert smu.core_map == {0: (0, 0), 1: (0, 1), 2: (0, 4), 3: (0, 5), 4: (0, 6), 5: (0, 7)}
    fused = {gen: cmds.core_fuse_addr for gen, cmds in COMMAND_SETS.items() if cmds.core_fuse_addr}
    assert fused == {
        CPUGeneration.ZEN3_VERMEER: 0x30081D98,
        CPUGeneration.ZEN3_CHAGALL: 0x30081D98,
        CPUGeneration.ZEN3D_WARHOL: 0x30081D98,
        CPUGeneration.ZEN4_STORM_PEAK: 0x30081D98,
        CPUGeneration.ZEN4_RAPHAEL: 0x30081CD0,
        CPUGeneration.ZEN4_DRAGON_RANGE: 0x30081CD0,
        CPUGeneration.ZEN5_GRANITE_RIDGE: 0x304A03DC,
    }
    mapped = {gen for gen, cmds in COMMAND_SETS.items() if cmds.uniform_8core_ccds}
    assert mapped == {
        CPUGeneration.ZEN3_VERMEER,
        CPUGeneration.ZEN3_CHAGALL,
        CPUGeneration.ZEN3D_WARHOL,
        CPUGeneration.ZEN3_CEZANNE,
        CPUGeneration.ZEN3_REMBRANDT,
        CPUGeneration.ZEN4_RAPHAEL,
        CPUGeneration.ZEN4_DRAGON_RANGE,
        CPUGeneration.ZEN4_STORM_PEAK,
        CPUGeneration.ZEN4_PHOENIX,
        CPUGeneration.ZEN5_GRANITE_RIDGE,
        CPUGeneration.ZEN5_SHIMADA_PEAK,
    }


def _pin_external_tool_discovery() -> None:
    assert tools.env_var("y-cruncher") == "CORECYCLER_Y_CRUNCHER_BIN"
    assert tools.env_var("stress-ng") == "CORECYCLER_STRESS_NG_BIN"
    scanned = {key for key, tool in tools.TOOLS.items() if tool.globs}
    assert scanned == {"mprime", "y-cruncher"}, scanned
    assert tools.TOOLS["y-cruncher"].names == ("y-cruncher", "y_cruncher")
    for layout in (
        "y-cruncher/y-cruncher",
        "y-cruncher v0.8.7.9547-static/y-cruncher",
        "y-cruncher v0.8.6.9545-dynamic/y-cruncher",
    ):
        assert any(PurePath(layout).match(pattern) for pattern in tools.TOOLS["y-cruncher"].globs), layout
    backends = {key for key, tool in tools.TOOLS.items() if tool.kind == tools.BACKEND}
    assert backends == {"mprime", "y-cruncher", "stress-ng", "stressapptest"}
    load_all()
    assert set(BACKEND_REGISTRY) == backends, (
        "every registered stress backend needs a config.tools entry, or it can never resolve"
    )
    assert {key for key, tool in tools.TOOLS.items() if tool.kind == tools.CORE} == {
        "systemd-run",
        "setpriv",
    }


CONTRACTS: list[Contract] = [
    Contract(
        name="smu-co-bit-layout",
        kind="arch",
        source="ryzen_smu / ZenStates-Core CO arg layout: [31:28]=CCD [23:20]=core [15:0]=margin",
        ring_a=_pin_co_bit_layout,
        live_verifiable=False,
    ),
    Contract(
        name="detect-generation-cpuid-map",
        kind="arch",
        source="AMD CPUID Fn0000_0001 family/model + PPR codename ranges",
        ring_a=_pin_detect_generation_map,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_real_cpu_resolves_to_supported_generation",
    ),
    Contract(
        name="msr-register-addresses",
        kind="arch",
        source="AMD PPR Fam19h/1Ah APERF 0xE8 MPERF 0xE7 RAPL 0xC0010299/029A/029B",
        ring_a=_pin_msr_addresses,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_msr_reads_are_plausible",
    ),
    Contract(
        name="dmidecode-dimm-format",
        kind="os",
        source="dmidecode -t memory DMI type 17 field names (Size/Locator/Type/Speed)",
        ring_a=_pin_dmidecode_dimm_parse,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_dmidecode_parses_real_dimms",
    ),
    Contract(
        name="mprime-31x-config-keys",
        kind="tool",
        source=(
            "mprime 31.04 undoc.txt CpuSupports*/EnableSetAffinity, verified live "
            "2026-08-11: SSE flags -> Pentium4 type-1 FFT, AVX -> AVX FFT, AVX2 -> "
            "FMA3 FFT, AVX512F -> AVX-512 FFT; EnableSetAffinity=0 held every "
            "thread on the placed CPUs; TortureWeak is not an mprime option"
        ),
        ring_a=_pin_mprime_31x_config_keys,
        live_verifiable=True,
        ring_b_test=("test_backend_versions.py::test_each_mode_produces_its_own_fft_path"),
    ),
    Contract(
        name="proc-cpus-allowed-list",
        kind="os",
        source="proc(5) /proc/<pid>/task/<tid>/status Cpus_allowed_list, comma list with a-b ranges",
        ring_a=_pin_proc_cpus_allowed_list,
        live_verifiable=True,
        ring_b_test=("test_containment_live.py::test_a_contained_child_cannot_escape_its_cpuset"),
    ),
    Contract(
        name="proc-stat-cpu-fields",
        kind="os",
        source="proc(5) /proc/stat cpuN fields: [3]=idle [4]=iowait, the stall watchdog's basis",
        ring_a=_pin_proc_stat_cpu_fields,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_proc_stat_cpu_line_matches_the_pinned_fields",
    ),
    Contract(
        name="zen5-co-range",
        kind="arch",
        source="ZenStates-Core Utils.cs clamps Zen4+ CO to [-50,50]; 9950X3D read-back rejects -60",
        ring_a=_pin_zen5_co_range,
        live_verifiable=False,
    ),
    Contract(
        name="apu-co-command-ids",
        kind="arch",
        source=(
            "ZenStates-Core APUSettings1 (Cezanne: MP1 set 0x54/0x55, RSMU get 0xC3), "
            "APUSettings1_Phoenix (Rembrandt/Phoenix/Phoenix2: MP1 0x4B/0x4C, RSMU get "
            "0xE1, Rembrandt 0x2F), APUSettings1_Strix (Strix/Krackan: RSMU get 0xAF); "
            "corroborated by RyzenAdj lib/api.c"
        ),
        ring_a=_pin_apu_co_command_ids,
        live_verifiable=False,
    ),
    Contract(
        name="apu-pbo-command-ids",
        kind="arch",
        source=(
            "ZenStates-Core APUSettings1 RSMU block (SetSlowLimit 0x33 = sustained-"
            "PPT analogue, TDCVDD 0x38, EDCVDD 0x3A, TctlMax 0x37, GetPBOScalar 0xF, "
            "SetPBOScalar 0x3F Cezanne / 0x3E Phoenix+, boost get 0x42 / set-all 0x47 "
            "Phoenix+, OC 0x17/0x18/0x82, PM table 0x65/0x66/0x6); ids corroborated "
            "by RyzenAdj api.c RSMU retry paths (stapm 0x31, Rembrandt OC 0x17/0x18) "
            "and its PM-table commands 0x65/0x66/0x6"
        ),
        ring_a=_pin_apu_pbo_command_ids,
        live_verifiable=False,
    ),
    Contract(
        name="desktop-pbo-command-ids",
        kind="arch",
        source=(
            "ZenStates-Core Zen4Settings/Zen5Settings: RSMU SetFastLimit(=PPT) 0x56, "
            "TDCVDD 0x57, EDCVDD 0x58, SetTctlMax 0x59, PM table 0x3/0x4/0x5; "
            "GetFastestCoreofSocket exists ONLY on Zen2/Zen3 (RSMU 0x59) — on Zen4/5 "
            "0x59 is the thermal-limit SETTER, so no fastest-core command is wired "
            "there"
        ),
        ring_a=_pin_desktop_pbo_command_ids,
        live_verifiable=False,
    ),
    Contract(
        name="external-tool-discovery",
        kind="os",
        source=(
            "sudoers secure_path (shipped set on Debian/Ubuntu/Mint, Fedora and Arch; "
            "absent on NixOS) REPLACES PATH under sudo, and y-cruncher/mprime ship as "
            "tarballs -- packaged as a /usr/bin symlink into /usr/lib (AUR) or a wrapper "
            "(nixpkgs), extracted anywhere otherwise. Issue #12: y-cruncher on the user's "
            "PATH was invisible to a sudo run"
        ),
        ring_a=_pin_external_tool_discovery,
        live_verifiable=True,
        ring_b_test=("test_tool_discovery_live.py::test_every_tool_present_on_path_resolves_through_the_registry"),
    ),
    Contract(
        name="smu-core-slot-mapping",
        kind="arch",
        source=(
            "Issue #11 (5600X renumbered core ids) + ZenStates-Core Cpu.cs "
            "GetCpuTopology, corroborated by ryzen_monitor get_processor_topology: "
            "SMU CO addresses physical 8-slot CCDs including fused-off cores, and "
            "the per-CCD SMN core-disable fuse (CCD n at addr + (n << 25), low 8 "
            "bits, set bit = fused off) names the live slots, which pair with OS "
            "cores in ascending order. The CO read is NOT that signal -- it answers "
            "on every in-range slot, which is what issue #11 reported"
        ),
        ring_a=_pin_core_slot_mapping,
        live_verifiable=True,
        ring_b_test="test_hardware_contracts.py::test_core_slot_map_matches_the_live_core_disable_fuse",
    ),
]
