"""Per-core CPU frequency monitoring."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

CPUFREQ_BASE = Path("/sys/devices/system/cpu")


def _mhz(path: Path) -> float | None:
    try:
        return int(path.read_text().strip()) / 1000
    except OSError, ValueError:
        return None


def _cpufreq(root: Path) -> list[tuple[int, Path]]:
    if not root.is_dir():
        return []
    return sorted(
        (int(cpu.name[3:]), cpu / "cpufreq")
        for cpu in root.glob("cpu[0-9]*")
        if cpu.name[3:].isdigit() and (cpu / "cpufreq" / "scaling_cur_freq").is_file()
    )


class Clocks:
    """One scan of every CPU's cpufreq directory, kept: a read opens one file per CPU.

    scaling_cur_freq is the one clock source: readable without root, and on a
    kernel with CONFIG_CPUFREQ_ARCH_CUR_FREQ the APERF/MPERF clock of the
    last tick. scaling_max_freq is the boost ceiling the CPU may reach now.
    A value that cannot be read is None.
    """

    def __init__(self, root: Path = CPUFREQ_BASE) -> None:
        cpus = _cpufreq(root)
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
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"unreadable: {exc.strerror}"


def _ceiling(path: Path) -> float | str:
    try:
        text = path.read_text().strip()
    except OSError as exc:
        return f"unreadable: {exc.strerror}"
    return int(text) / 1000 if text.isdecimal() else f"unreadable: {text!r} is not a number"


@dataclass(frozen=True, slots=True)
class PowerState:
    """The settings that can change a verdict without touching a core: read from every CPU before every test.

    A setting this machine has no file for is None. A read that fails is "unreadable: <reason>", in the value or,
    per CPU, in split. A setting its CPUs read differently is None and named in split. The scaling maximum is read
    from every CPU that has cpufreq, so it is never absent: one that no CPU could read is None and named in split.
    ceilings keeps each CPU's own scaling maximum, None where it could not be read.
    """

    profile: str | None = None
    governor: str | None = None
    epp: str | None = None
    boost: str | None = None
    max_mhz: float | None = None
    top_mhz: float | None = None
    split: tuple[tuple[str, str], ...] = ()
    ceilings: tuple[tuple[int, float | None], ...] = ()

    def named(self) -> tuple[tuple[str, str], ...]:
        """The settings a session records at Start and compares before each test."""
        differs = dict(self.split)
        pairs = (
            ("platform profile", self.profile),
            ("governor", self.governor),
            ("EPP", self.epp),
            ("boost", self.boost),
            ("scaling maximum MHz", None if self.max_mhz is None else f"{self.max_mhz:.0f}"),
        )
        return tuple((name, shown) for name, value in pairs if (shown := differs.get(name, value)) is not None)


def cpu_ranges(cpus: list[int]) -> str:
    spans: list[list[int]] = []
    for cpu in cpus:
        if spans and cpu == spans[-1][1] + 1:
            spans[-1][1] = cpu
        else:
            spans.append([cpu, cpu])
    return ",".join(str(first) if first == last else f"{first}-{last}" for first, last in spans)


def _agreed[T](read: list[tuple[int, T | None]], shown: Callable[[T], str]) -> tuple[T | None, str | None]:
    if all(value is None for _, value in read):
        return None, None
    groups: dict[T | None, list[int]] = {}
    for cpu, value in read:
        groups.setdefault(value, []).append(cpu)
    if len(groups) == 1:
        return next(iter(groups)), None
    return None, "; ".join(
        f"{'absent' if value is None else shown(value)} on cpus {cpu_ranges(cpus)}" for value, cpus in groups.items()
    )


def _shown_mhz(ceiling: float | str) -> str:
    return ceiling if isinstance(ceiling, str) else f"{ceiling:.0f}"


def _top(folder: Path) -> float | None:
    top = folder / "amd_pstate_max_freq"
    return _mhz(top if top.exists() else folder / "cpuinfo_max_freq")


def read_power_state(root: Path = CPUFREQ_BASE, profile: Path = PLATFORM_PROFILE) -> PowerState:
    cpus = _cpufreq(root)
    governor, governors = _agreed([(cpu, _text(folder / "scaling_governor")) for cpu, folder in cpus], str)
    epp, epps = _agreed([(cpu, _text(folder / "energy_performance_preference")) for cpu, folder in cpus], str)
    ceilings = [(cpu, _ceiling(folder / "scaling_max_freq")) for cpu, folder in cpus]
    ceiling, differing = _agreed(ceilings, _shown_mhz)
    if isinstance(ceiling, str):
        ceiling, differing = None, ceiling
    split = (("governor", governors), ("EPP", epps), ("scaling maximum MHz", differing))
    return PowerState(
        profile=_text(profile),
        governor=governor,
        epp=epp,
        boost=_text(root / "cpufreq" / "boost"),
        max_mhz=ceiling,
        top_mhz=max((mhz for _, folder in cpus if (mhz := _top(folder)) is not None), default=None),
        split=tuple((name, text) for name, text in split if text is not None),
        ceilings=tuple((cpu, None if isinstance(mhz, str) else mhz) for cpu, mhz in ceilings),
    )
