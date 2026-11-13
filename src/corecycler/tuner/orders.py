from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class Method(StrEnum):
    ALTERNATE = "alternate"
    SEQUENTIAL = "sequential"
    RANDOM = "random"


@dataclass(frozen=True, slots=True)
class RoundCursor:
    round_no: int = 0
    position: int = 0


def _domains(cores: Sequence[int], domain_of: Mapping[int, int | None]) -> list[list[int]]:
    groups: dict[int, list[int]] = {}
    for core in sorted(cores):
        groups.setdefault(domain_of[core], []).append(core)
    return sorted(groups.values(), key=lambda group: group[0])


def order_problem(method: Method, selected: Sequence[int], domain_of: Mapping[int, int | None]) -> str | None:
    if not selected:
        return "no cores are selected"
    if len(set(selected)) != len(selected):
        return "a core is selected twice"
    if method is not Method.ALTERNATE:
        return None
    unknown = sorted(core for core in selected if domain_of.get(core) is None)
    if unknown:
        return f"alternate needs every selected core's CCD; cores {unknown} have none"
    if len({domain_of[core] for core in selected}) < 2:
        return "alternate needs the selected cores on two or more CCDs; they sit on one, so use sequential"
    return None


def default_method(selected: Sequence[int], domain_of: Mapping[int, int | None]) -> Method:
    if order_problem(Method.ALTERNATE, selected, domain_of) is None:
        return Method.ALTERNATE
    return Method.SEQUENTIAL


def round_sequence(
    method: Method,
    cores: Sequence[int],
    domain_of: Mapping[int, int | None],
    seed: int,
    class_name: str,
    round_no: int,
) -> tuple[int, ...]:
    problem = order_problem(method, cores, domain_of)
    if problem is not None:
        raise ValueError(problem)
    ordered = sorted(cores)
    if method is Method.SEQUENTIAL:
        return tuple(ordered)
    if method is Method.RANDOM:
        rng = random.Random(f"{seed}:{class_name}:{round_no}")
        rng.shuffle(ordered)
        return tuple(ordered)
    groups = _domains(cores, domain_of)
    depth = max(len(group) for group in groups)
    return tuple(group[index] for index in range(depth) for group in groups if index < len(group))


def next_core(
    method: Method,
    cores: Sequence[int],
    domain_of: Mapping[int, int | None],
    seed: int,
    class_name: str,
    cursor: RoundCursor,
    owed: int | None,
) -> tuple[int, RoundCursor]:
    if owed is not None:
        if owed not in cores:
            raise ValueError(f"core {owed} is owed a test but is not one of the class's cores")
        return owed, cursor
    sequence = round_sequence(method, cores, domain_of, seed, class_name, cursor.round_no)
    if not 0 <= cursor.position < len(sequence):
        raise ValueError(f"round position {cursor.position} is outside a round of {len(sequence)} cores")
    core = sequence[cursor.position]
    if cursor.position + 1 == len(sequence):
        return core, RoundCursor(cursor.round_no + 1, 0)
    return core, RoundCursor(cursor.round_no, cursor.position + 1)
