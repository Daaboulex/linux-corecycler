from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from corecycler.engine.backends.base import Outcome
from corecycler.engine.backends.ycruncher import ALL_TESTS
from corecycler.tuner.machine.types import Load

if TYPE_CHECKING:
    import random

HOT_PER_TEST_S = 60
BISECT_PER_TEST_S = 20
WARM_SLICE_S = 60.0
WARM_PER_TEST_S = 30
BURST_PER_TEST_S = 15
BURST_FIRST_RESTART_S = 60.0
IDLE_PER_TEST_S = 10
IDLE_MUST_FINISH_FROM_S = 360.0
HOT_MEMORY_CAP_MB = 16384
BURST_MEMORY_MB = 2048
IDLE_MEMORY_MB = 1024
HOT_HOLD_SPAN_C = 10.0
WARM_HOLD = (65.0, 55.0)
REST_MARGIN_C = 2.0
HOLD_CAP_S = 300.0


@dataclass(frozen=True, slots=True)
class Segment:
    cpus: tuple[int, ...]
    tests: tuple[str, ...]
    seconds_per_test: int
    memory_mb: int
    duration: float
    frozen: tuple[tuple[float, float], ...] = ()
    hold: tuple[float, float] | None = None
    hold_cap_s: float | None = None
    hold_on_tctl: bool = False
    hold_timeout: Outcome | None = None
    hold_in_wall_time: bool = False
    must_finish_a_test: bool = False


def warm_hold(rest_c: float, limit_c: float) -> tuple[float, float]:
    hot, cool = WARM_HOLD
    span = hot - cool
    release = min(max(cool, rest_c + REST_MARGIN_C), limit_c - HOT_HOLD_SPAN_C - span)
    return release + span, release


def frozen_at(segment: Segment, elapsed: float) -> bool:
    return any(start <= elapsed < end for start, end in segment.frozen)


def longest_rest(frozen: tuple[tuple[float, float], ...]) -> float:
    return max((end - start for start, end in frozen), default=0.0)


RECORD_TICK_S = 10.0


def record_freezes(seconds: float) -> tuple[tuple[float, float], ...]:
    intervals = []
    t = 0.0
    while True:
        remaining = seconds - t
        last = remaining <= RECORD_TICK_S
        start = t + (remaining - 2.0 if last else RECORD_TICK_S - 1.0)
        if start < t:
            break
        intervals.append((start, start + 1.0))
        if last:
            break
        t = start + 2.0
    return tuple(intervals)


def _alternate(rng: random.Random, duration: float, first: float, run: tuple, rest: tuple) -> tuple:
    intervals = []
    t = first
    while t < duration:
        stop = min(duration, t + rng.uniform(*rest))
        intervals.append((t, stop))
        t = stop + rng.uniform(*run)
    return tuple(intervals)


def _draws(n_cpus: int) -> list[int]:
    draws = []
    k = 2
    while k < n_cpus:
        draws.append(k)
        k *= 2
    return [*draws, n_cpus]


def hot_memory_mb(usable_mb: int) -> int:
    return max(1, min(HOT_MEMORY_CAP_MB, usable_mb // 2))


def plan(
    load: Load,
    seconds: float,
    cpus: tuple[int, ...],
    firsts: tuple[int, ...],
    usable_mb: int,
    rng: random.Random,
    limit_c: float,
    *,
    quick: bool,
    rest_c: float | None = None,
) -> tuple[Segment, ...]:
    hot_memory = hot_memory_mb(usable_mb)
    hot_hold = (limit_c, limit_c - HOT_HOLD_SPAN_C)
    if load is Load.HOT:
        return (
            Segment(
                cpus,
                ALL_TESTS,
                BISECT_PER_TEST_S if quick else HOT_PER_TEST_S,
                hot_memory,
                seconds,
                hold=hot_hold,
                hold_cap_s=HOLD_CAP_S,
                hold_timeout=Outcome.THERMAL,
            ),
        )
    if load is Load.WARM:
        if rest_c is None:
            raise ValueError("a warm plan has no rest temperature to set its release point from")
        warm = warm_hold(rest_c, limit_c)
        slices = []
        for index, test in enumerate(ALL_TESTS):
            remaining = seconds - index * WARM_SLICE_S
            if remaining <= 0:
                break
            slices.append(
                Segment(
                    cpus,
                    (test,),
                    WARM_PER_TEST_S,
                    hot_memory,
                    min(WARM_SLICE_S, remaining),
                    hold=warm,
                    hold_cap_s=HOLD_CAP_S,
                    hold_on_tctl=True,
                    hold_timeout=Outcome.HELD_TOO_LONG,
                    hold_in_wall_time=True,
                    must_finish_a_test=remaining >= WARM_SLICE_S,
                )
            )
        return tuple(slices)
    if load is Load.BURST:
        segments = []
        t = 0.0
        while t < seconds:
            length = min(seconds - t, BURST_FIRST_RESTART_S if not segments else rng.uniform(40.0, 80.0))
            start = rng.randrange(len(ALL_TESTS))
            draw = rng.choice(_draws(len(cpus)))
            chosen = cpus if draw >= len(cpus) else tuple(sorted(rng.sample(firsts, min(len(firsts), draw))))
            first = 3.0 if not segments else rng.choice((1.0, 2.0))
            frozen = _alternate(rng, length, first, (0.2, 3.0), (0.3, 4.0))
            segments.append(
                Segment(
                    chosen,
                    ALL_TESTS[start:],
                    BURST_PER_TEST_S,
                    BURST_MEMORY_MB,
                    length,
                    frozen,
                    hold=hot_hold,
                    hold_cap_s=HOLD_CAP_S,
                    hold_timeout=Outcome.THERMAL,
                )
            )
            t += length
        return tuple(segments)
    if load is Load.IDLE:
        count = min(len(firsts), rng.choice((1, 2, 4)))
        chosen = tuple(sorted(rng.sample(firsts, count)))
        start = rng.randrange(len(ALL_TESTS))
        frozen = _alternate(rng, seconds, rng.uniform(0.1, 0.4), (0.1, 0.4), (15.0, 40.0))
        return (
            Segment(
                chosen,
                ALL_TESTS[start:],
                IDLE_PER_TEST_S,
                IDLE_MEMORY_MB,
                seconds,
                frozen,
                hold=hot_hold,
                hold_cap_s=HOLD_CAP_S,
                hold_timeout=Outcome.THERMAL,
                must_finish_a_test=seconds >= IDLE_MUST_FINISH_FROM_S,
            ),
        )
    raise ValueError(f"{load} has no stage plan")
