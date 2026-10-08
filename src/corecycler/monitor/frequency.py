"""Per-core CPU frequency monitoring."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

CPUFREQ_BASE = Path("/sys/devices/system/cpu")


def _mhz(path: Path) -> float | None:
    try:
        return int(path.read_text().strip()) / 1000
    except OSError, ValueError:
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


PLATFORM_PROFILE = Path("/sys/firmware/acpi/platform_profile")


def _text(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


@dataclass(frozen=True, slots=True)
class PowerState:
    """The settings that can change a verdict without touching a core: read before every test.

    A setting this machine does not have is None.
    """

    profile: str | None = None
    governor: str | None = None
    epp: str | None = None
    boost: str | None = None
    max_mhz: float | None = None
    top_mhz: float | None = None

    def named(self) -> tuple[tuple[str, str], ...]:
        """The settings a session records at Start and compares before each test."""
        pairs = (
            ("platform profile", self.profile),
            ("governor", self.governor),
            ("EPP", self.epp),
            ("boost", self.boost),
            ("scaling maximum MHz", None if self.max_mhz is None else f"{self.max_mhz:.0f}"),
        )
        return tuple((name, value) for name, value in pairs if value is not None)


def read_power_state(root: Path = CPUFREQ_BASE, profile: Path = PLATFORM_PROFILE) -> PowerState:
    cpufreq = root / "cpu0" / "cpufreq"
    top = cpufreq / "amd_pstate_max_freq"
    return PowerState(
        profile=_text(profile),
        governor=_text(cpufreq / "scaling_governor"),
        epp=_text(cpufreq / "energy_performance_preference"),
        boost=_text(root / "cpufreq" / "boost"),
        max_mhz=_mhz(cpufreq / "scaling_max_freq"),
        top_mhz=_mhz(top if top.exists() else cpufreq / "cpuinfo_max_freq"),
    )
