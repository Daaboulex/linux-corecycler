from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from corecycler.history.db import SAMPLE_BLOCK_S
from corecycler.tuner.machine.types import FailContext

if TYPE_CHECKING:
    from corecycler.monitor.hwmon import CoreRead

SAMPLE_S = 0.1
WINDOW_S = 2.0
RING = round(WINDOW_S / SAMPLE_S)
BLOCK = round(SAMPLE_BLOCK_S / SAMPLE_S)
TAIL_S = 0.5
TAIL_GROUP = round(TAIL_S / SAMPLE_S)


@dataclass(frozen=True, slots=True)
class Sample:
    running: bool
    mhz: tuple[tuple[int, float], ...]
    tctl: float | None
    mono_ns: int = 0
    tccd: tuple[tuple[int, float | None], ...] = ()
    volts: tuple[tuple[str, float | None], ...] = ()
    watts: tuple[tuple[str, float | None], ...] = ()
    cores: tuple[CoreRead, ...] = ()


def band_clock(sample: Sample, cores: tuple[int, ...]) -> float | None:
    clocks = [mhz for core, mhz in sample.mhz if core in cores]
    return sum(clocks) / len(clocks) if clocks else None


def context(ring: list[Sample], steady_load: bool, assigned: tuple[int, ...], named: tuple[int, ...]) -> FailContext:
    if not ring:
        raise ValueError("a failure context needs at least one sample")
    last = ring[-1]
    running = [sample for sample in ring if sample.running]
    best = max(running, key=lambda s: -1.0 if s.tctl is None else s.tctl) if running else last
    paused = not last.running
    working = tuple(sorted(assigned))
    return FailContext(steady_load and not paused, working, paused, band_clock(best, named or working), best.tctl)
