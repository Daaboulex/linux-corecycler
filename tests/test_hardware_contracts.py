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
