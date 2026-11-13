from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from corecycler.tuner.machine.types import AllCore, CoreRecord, FailContext, MachineConfig

ENTRY_MARGIN_STEPS = 3


def round_members(cores: tuple[CoreRecord, ...], cfg: MachineConfig, cursor: AllCore) -> tuple[int, ...]:
    floors = dict(cursor.floors)
    return tuple(
        r.core
        for r in cores
        if r.core not in cursor.frozen
        and r.value > floors.get(r.core, r.value)
        and r.inside(r.value - cfg.fine_step, cfg.max_offset)
    )


def steady_class(ctx: FailContext | None) -> bool:
    return ctx is not None and ctx.steady
