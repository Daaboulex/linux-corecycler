from __future__ import annotations

from dataclasses import dataclass

from corecycler.tuner.machine.types import FailContext

RING = 8
TAIL = 3
WORKING_SHARE = 0.85
ACTIVE_SHARE = 0.6


@dataclass(frozen=True, slots=True)
class Sample:
    running: bool
    mhz: tuple[tuple[int, float], ...]
    tctl: float | None


def working(sample: Sample) -> tuple[int, ...]:
    top = max((mhz for _, mhz in sample.mhz), default=0.0)
    if top <= 0:
        return ()
    return tuple(sorted(core for core, mhz in sample.mhz if mhz >= WORKING_SHARE * top))


def ring_cores(samples: list[Sample]) -> tuple[int, ...]:
    counts: dict[int, int] = {}
    for sample in samples:
        for core in working(sample):
            counts[core] = counts.get(core, 0) + 1
    return tuple(sorted(core for core, count in counts.items() if count >= 0.5 * len(samples)))


def band_clock(sample: Sample) -> float | None:
    top = max((mhz for _, mhz in sample.mhz), default=0.0)
    if top <= 0:
        return None
    active = [mhz for _, mhz in sample.mhz if mhz >= ACTIVE_SHARE * top]
    return sum(active) / len(active)


def context(ring: list[Sample], steady_load: bool) -> FailContext:
    if not ring:
        raise ValueError("a failure context needs at least one sample")
    last = ring[-1]
    tail = [sample for sample in ring[-TAIL:] if sample.running]
    cores = ring_cores(tail) if last.running and tail else ring_cores(ring)
    running = [sample for sample in ring if sample.running]
    best = max(running, key=lambda s: -1.0 if s.tctl is None else s.tctl) if running else last
    paused = not last.running
    return FailContext(steady_load and not paused, cores, paused, band_clock(best), best.tctl)
