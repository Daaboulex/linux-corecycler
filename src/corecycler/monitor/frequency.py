"""Per-core CPU frequency monitoring."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

CPUFREQ_BASE = Path("/sys/devices/system/cpu")


def _mhz(path: Path) -> float | None:
    try:
        return int(path.read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


class Clocks:
    """One scan of every CPU's cpufreq directory, kept: a read opens one file per CPU.

    scaling_cur_freq is the one clock source: readable without root, and on a
    kernel with CONFIG_CPUFREQ_ARCH_CUR_FREQ the APERF/MPERF clock of the
    last tick. scaling_max_freq is the boost ceiling the CPU may reach now.
    A value that cannot be read is None.
    """

    def __init__(self, root: Path = CPUFREQ_BASE) -> None:
        cpus = (
            sorted(
                (int(cpu.name[3:]), cpu / "cpufreq")
                for cpu in root.glob("cpu[0-9]*")
                if cpu.name[3:].isdigit() and (cpu / "cpufreq" / "scaling_cur_freq").is_file()
            )
            if root.is_dir()
            else []
        )
        self.current = [(cpu, folder / "scaling_cur_freq") for cpu, folder in cpus]
        self.ceiling = [(cpu, folder / "scaling_max_freq") for cpu, folder in cpus]

    def read(self) -> dict[int, float | None]:
        return {cpu: _mhz(path) for cpu, path in self.current}

    def ceilings(self) -> dict[int, float | None]:
        return {cpu: _mhz(path) for cpu, path in self.ceiling}


def fastest(clocks: dict[int, float | None], cpus: Iterable[int]) -> float | None:
    """A core's clock: the faster of its CPUs that were read."""
    return max((mhz for cpu in cpus if (mhz := clocks.get(cpu)) is not None), default=None)


def read_max_frequency(cpu_id: int = 0) -> float | None:
    """Read the maximum boost frequency for a CPU (MHz)."""
    path = CPUFREQ_BASE / f"cpu{cpu_id}" / "cpufreq" / "cpuinfo_max_freq"
    if path.exists():
        with contextlib.suppress(ValueError, OSError):
            return int(path.read_text().strip()) / 1000.0
    return None
