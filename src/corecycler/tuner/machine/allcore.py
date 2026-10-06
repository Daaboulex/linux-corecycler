from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from corecycler.tuner.machine.types import FailContext

if TYPE_CHECKING:
    from corecycler.tuner.machine.types import AllCore, CoreRecord

BAND_BORDERS_MHZ = (4400, 5175, 5650)
COLUMN_SPLIT_C = 75.0
NEAR_BAND_MHZ = 150
NEAR_COLUMN_C = 5.0
OWNER_PRIORITY = (8, 0, 9, 10, 11, 13, 12, 2, 1, 3, 4, 5, 6, 7, 14, 15)


@dataclass(frozen=True, slots=True)
class Fix:
    cursor: AllCore
    core: int | None
    detail: str


def band(mhz: float | None) -> int:
    if mhz is None:
        return 2
    return 1 + sum(mhz >= border for border in BAND_BORDERS_MHZ)


def column(tctl: float | None) -> int:
    return 1 if tctl is None or tctl < COLUMN_SPLIT_C else 2


def _count(pairs: tuple, key) -> int:
    return dict(pairs).get(key, 0)


def _bump(pairs: tuple, key, by: int = 1) -> tuple:
    counts = dict(pairs)
    counts[key] = counts.get(key, 0) + by
    return tuple(sorted(counts.items(), key=lambda item: str(item[0])))


def type_key(ctx: FailContext, light: bool, kind: str) -> str:
    if light:
        return "light"
    mhz = 5000 if ctx.mhz is None else ctx.mhz
    near_band = any(abs(mhz - border) <= NEAR_BAND_MHZ for border in BAND_BORDERS_MHZ)
    near_column = ctx.tctl is not None and abs(ctx.tctl - COLUMN_SPLIT_C) <= NEAR_COLUMN_C
    return f"{kind}-{band(ctx.mhz)}{column(ctx.tctl)}{'b' if near_band else ''}{'c' if near_column else ''}"


def pick_core(cores: tuple[CoreRecord, ...], candidates, cursor: AllCore, owner_layout: bool) -> int | None:
    loosenable = [r.core for r in cores if r.core in candidates and r.value < 0]
    if not loosenable:
        return None

    def rank(core: int) -> tuple:
        order = OWNER_PRIORITY.index(core) if owner_layout and core in OWNER_PRIORITY else core
        return (_count(cursor.co_delta, core), order)

    return min(loosenable, key=rank)


def transient_fix(
    cursor: AllCore, ctx: FailContext, cores: tuple[CoreRecord, ...], selected: int, owner_layout: bool
) -> Fix:
    working = tuple(sorted(set(ctx.working)))
    light = ctx.paused or not working
    busy = () if light else working
    kind = "light" if light else ("step" if len(working) * 2 >= selected else "boost")
    key = type_key(ctx, light, kind)
    n = _count(cursor.t_types, key)
    cursor = replace(cursor, t_types=_bump(cursor.t_types, key))
    t_busy = cursor.t_busy
    for core in busy:
        t_busy = _bump(t_busy, f"{key}/{core}")
    cursor = replace(cursor, t_busy=t_busy)
    recurring = tuple(core for core in busy if _count(t_busy, f"{key}/{core}") >= 2)
    core_set = working if light else recurring
    candidates = ["core"] if light else []
    if kind == "boost" and core_set:
        candidates.append("core")
    for i in range(len(candidates)):
        if candidates[(n + i) % len(candidates)] == "core":
            core = pick_core(cores, core_set, cursor, owner_layout)
            if core is not None:
                return _loosened(cursor, core, f"{kind} failure, key {key}")
    core = pick_core(cores, tuple(r.core for r in cores), cursor, owner_layout)
    if core is None:
        return Fix(cursor, None, "nothing left to loosen")
    return _loosened(cursor, core, f"{kind} failure, key {key}, least loosened core")


def _loosened(cursor: AllCore, core: int, why: str) -> Fix:
    return Fix(replace(cursor, co_delta=_bump(cursor.co_delta, core), rung=cursor.rung + 1), core, why)


def tighten_items(cursor: AllCore) -> tuple[int, ...]:
    return tuple(sorted(core for core, delta in cursor.co_delta if delta > 0))


def steady_class(ctx: FailContext | None) -> bool:
    return ctx is not None and ctx.steady


def context_or_unknown(ctx: FailContext | None) -> FailContext:
    return ctx if ctx is not None else FailContext(steady=False, working=(), paused=True)
