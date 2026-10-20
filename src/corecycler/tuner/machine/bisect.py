from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from corecycler.tuner.machine.types import BisectRun

if TYPE_CHECKING:
    from collections.abc import Mapping

    from corecycler.tuner.machine.types import Klass, Stage

Group = tuple[int, ...]


def halves(group: Group, domain_of: Mapping[int, int | None]) -> tuple[Group, ...]:
    if len(group) < 2:
        return ()
    ordered = tuple(sorted(group))
    domains = [domain_of.get(core) for core in ordered]
    if None not in domains and len(set(domains)) > 1:
        ccds = sorted(set(domains))
        lower_ccds = set(ccds[: len(ccds) // 2])
        lower = tuple(core for core in ordered if domain_of[core] in lower_ccds)
    else:
        lower = ordered[: len(ordered) // 2]
    upper = tuple(core for core in ordered if core not in lower)
    return tuple(part for part in (lower, upper) if part and part != ordered)


def begin(
    entry: Stage, lane: int, klass: Klass, values: Mapping[int, int], domain_of: Mapping[int, int | None]
) -> BisectRun:
    universe = tuple(sorted(core for core, value in values.items() if value != 0))
    children = halves(universe, domain_of)
    return BisectRun(
        entry=entry,
        lane=lane,
        klass=klass,
        universe=universe,
        values=tuple(sorted((core, values[core]) for core in universe)),
        queue=((), *children),
        results=((universe, False),),
        parents=tuple((child, universe) for child in children),
    )


def head(run: BisectRun) -> Group:
    return run.queue[0]


def current_group(run: BisectRun) -> Group:
    if run.rerun is not None:
        return run.rerun
    return run.queue[0] if run.queue else ()


def record(run: BisectRun, passed: bool, domain_of: Mapping[int, int | None]) -> BisectRun:
    group = head(run)
    queue = run.queue[1:]
    parents = run.parents
    if not passed and len(group) > 1:
        children = halves(group, domain_of)
        queue = (*queue, *children)
        parents = (*parents, *((child, group) for child in children))
    return replace(run, queue=queue, results=(*run.results, (group, passed)), parents=parents)


def resolve(run: BisectRun) -> Group:
    results = dict(run.results)
    children_of: dict[Group, list[Group]] = {}
    for child, parent in run.parents:
        children_of.setdefault(parent, []).append(child)
    failed = [
        group
        for group, passed in results.items()
        if not passed and group and all(results.get(child) is True for child in children_of.get(group, []))
    ]
    return min(failed, key=lambda group: (len(group), group[0]), default=())


def retest(run: BisectRun) -> BisectRun:
    return replace(run, queue=(run.universe,))


def retesting(run: BisectRun) -> bool:
    return run.queue == (run.universe,)


def slot_values(run: BisectRun, group: Group) -> dict[int, int]:
    values = dict(run.values)
    return {core: values[core] for core in group}
