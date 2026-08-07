"""Ring B live contract tests -- run the REAL hardware or tools and assert our
readers/parsers still match reality. Marked slow + contract; the nix sandbox
deselects `slow` so it never runs them here. They run on real hardware via the
ryzen hardware-contract CI step (pytest -m contract, CORECYCLER_HW_CONTRACTS=1),
where an absent resource fails loud instead of skipping green.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.monitor.memory import parse_dmidecode_output
from corecycler.monitor.msr import MSRReader
from corecycler.smu.commands import CPUGeneration, detect_generation, get_commands

sys.path.insert(0, str(Path(__file__).parent))

from _contract_hw import require

pytestmark = [pytest.mark.slow, pytest.mark.contract]


def _read_cpuinfo() -> tuple[int, int, str] | None:
    p = Path("/proc/cpuinfo")
    if not p.exists():
        return None
    family: int | None = None
    model: int | None = None
    name = ""
    for line in p.read_text().splitlines():
        if line.startswith("cpu family") and family is None:
            family = int(line.split(":")[1])
        elif line.startswith("model name"):
            if not name:
                name = line.split(":", 1)[1].strip()
        elif line.startswith("model") and model is None:
            model = int(line.split(":")[1])
    if family is None or model is None:
        return None
    return family, model, name


def _is_amd_zen() -> bool:
    info = _read_cpuinfo()
    return info is not None and info[0] in (23, 25, 26)


def test_real_cpu_resolves_to_supported_generation():
    require(_is_amd_zen(), "requires a real AMD Zen CPU")
    info = _read_cpuinfo()
    assert info is not None
    family, model, name = info
    gen = detect_generation(family, model, name)
    assert gen is not CPUGeneration.UNKNOWN, f"unmapped CPU family={family} model={model} name={name!r}"
    assert get_commands(gen) is not None


def test_msr_reads_are_plausible():
    reader = MSRReader()
    require(
        reader.is_available(),
        "requires CAP_SYS_RAWIO on /dev/cpu/0/msr: the kernel gates msr_open() on the "
        "capability, so a udev group grant alone opens the file mode but still EPERMs",
    )
    unit = reader._get_energy_unit()
    assert unit is not None and 1e-6 < unit < 1e-2, f"implausible RAPL energy unit {unit}"
    reader.read_clock_stretch([0])
    time.sleep(0.1)
    stretch = reader.read_clock_stretch([0])
    reader.close()
    if 0 in stretch:
        assert 0.0 < stretch[0].ratio <= 1.6, f"implausible APERF/MPERF ratio {stretch[0].ratio}"


def test_dmidecode_parses_real_dimms():
    require(shutil.which("dmidecode") is not None, "requires dmidecode")
    result = subprocess.run(
        ["dmidecode", "-t", "memory"], capture_output=True, text=True, timeout=15
    )
    require(result.returncode == 0 and bool(result.stdout), "dmidecode -t memory needs root")
    dimms = parse_dmidecode_output(result.stdout)
    assert len(dimms) >= 1, "no DIMMs parsed from real dmidecode output"
    assert all(d.size_gb > 0 for d in dimms)


def test_proc_stat_cpu_line_matches_the_pinned_fields():
    """The stall watchdog reads idle+iowait from /proc/stat; drift here blinds it."""
    from corecycler.engine.parallel import _cpu_times

    require(Path("/proc/stat").exists(), "/proc/stat not readable")
    sample = _cpu_times(0)
    require(sample is not None, "no cpu0 line in /proc/stat")
    idle, total = sample
    assert 0 < idle < total
    fields = next(
        line.split() for line in Path("/proc/stat").read_text().splitlines()
        if line.startswith("cpu0 ")
    )
    assert len(fields) >= 6
    assert idle == int(fields[4]) + int(fields[5])


def test_core_slot_map_matches_the_live_core_disable_fuse():
    """The mapping's ground truth, on real silicon: this generation's
    core-disable fuse address decodes to exactly the physical slots the
    discovered map uses, per CCD. On a machine whose numbering proves itself
    the map comes from the core ids and the fuse is read independently, so a
    wrong fuse address for the die fails here rather than silently shipping."""
    require(_is_amd_zen(), "requires a real AMD Zen CPU")
    from corecycler.engine.topology import detect_topology
    from corecycler.smu.driver import RyzenSMU

    info = _read_cpuinfo()
    assert info is not None
    commands = get_commands(detect_generation(*info))
    require(commands is not None and commands.has_co, "requires a CO-capable generation")
    require(commands.uniform_8core_ccds, "requires a classic 8-slot-per-CCD die")
    require(
        commands.core_fuse_addr is not None,
        "requires a verified core-disable fuse address for this die",
    )
    require(RyzenSMU.is_available(), "requires the ryzen_smu module")
    smu = RyzenSMU(commands)
    smn_ok, smn_msg = smu.check_smn_readable()
    require(smn_ok, f"requires SMN access: {smn_msg}")
    topo = detect_topology()
    smu.set_topology(topo)
    assert smu.core_map_error is None, smu.core_map_error
    core_map = smu.core_map
    assert core_map is not None
    assert set(core_map) == set(topo.cores)
    per_ccd: dict[int, list[int]] = {}
    for _core_id, (ccd, slot) in sorted(core_map.items()):
        per_ccd.setdefault(ccd, []).append(slot)
    for ccd, mapped_slots in per_ccd.items():
        fuse = smu.read_smn(commands.core_fuse_addr + (ccd << 25))
        assert fuse is not None, f"CCD {ccd} core-disable fuse unreadable"
        live = [s for s in range(8) if not (fuse >> s) & 1]
        assert live == mapped_slots, (ccd, hex(fuse), live, mapped_slots)


def test_co_read_answers_on_every_slot_so_it_cannot_find_fused_off_cores():
    """Issue #11's falsified premise, pinned against real silicon: the CO read
    is NOT a liveness probe. Every in-range slot of a CCD answers it, so a
    harvested CCD cannot be resolved by asking the mailbox -- only by the
    fuse. If a die ever did discriminate here this fails and says so."""
    require(_is_amd_zen(), "requires a real AMD Zen CPU")
    from corecycler.engine.topology import detect_topology
    from corecycler.smu.commands import encode_co_arg
    from corecycler.smu.driver import RyzenSMU

    info = _read_cpuinfo()
    assert info is not None
    generation = detect_generation(*info)
    commands = get_commands(generation)
    require(commands is not None and commands.has_co, "requires a CO-capable generation")
    require(commands.uniform_8core_ccds, "requires a classic 8-slot-per-CCD die")
    require(RyzenSMU.is_available(), "requires the ryzen_smu module")
    smu = RyzenSMU(commands)
    require(smu.check_writable()[0], "requires ryzen_smu mailbox access")
    ccds = {c.ccd for c in detect_topology().cores.values() if c.ccd is not None}
    require(bool(ccds), "requires L3-detected CCDs")
    for ccd in sorted(ccds):
        answered = [
            slot
            for slot in range(8)
            if smu._send_get_co(
                encode_co_arg(0, 0, generation, ccd=ccd, slot=slot)
            ).success
        ]
        assert answered == list(range(8)), (ccd, answered)
