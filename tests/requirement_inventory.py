from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

ID_FORM = re.compile(r"[A-Z][A-Z0-9]*(\.[A-Za-z0-9_]+)*")
TASK_FORM = re.compile(r"[TSHO][0-9]+[a-z]?")


@dataclass(frozen=True, slots=True)
class Built:
    pass


@dataclass(frozen=True, slots=True)
class NotBuilt:
    task: str


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    statement: str
    proof: Built | NotBuilt


BUILT = Built()

REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        "GUI.SIGNAL_QUIT",
        "A stop signal schedules the GUI's quit on the event loop and never closes the window inside the slot it "
        "interrupted.",
        BUILT,
    ),
    Requirement(
        "TRUTH.TEMPERATURE_UNREAD",
        "An unreadable required CPU temperature is an UNVERIFIED result, never a thermal verdict, and a thermal stop "
        "names the temperature it read.",
        BUILT,
    ),
    Requirement(
        "TRUTH.REVERT_RESULT",
        "Every core a revert could not write back is named with its value and reason and stored as a revert_failed "
        "event; no text states that the cores reached their baseline.",
        BUILT,
    ),
    Requirement(
        "TRUTH.BOOT_END_UNREAD",
        "A boot whose end cannot be read is treated as a crash and recorded as that assumption with its reason, before "
        "anything the attribution announces.",
        BUILT,
    ),
    Requirement(
        "L01.12",
        "Every mode reads the BIOS Curve Shaper (RSMU 0x84) at Start with the band self-check where the generation has "
        "it, records it with the session's setup, and refuses Start when the read fails.",
        NotBuilt("S0"),
    ),
    Requirement(
        "U18",
        "A core's search starts from a hint, the newest completed session with the same setup and Shaper, and a failed "
        "hint restarts the walk from 0 with the hint as its fail bound (D20).",
        NotBuilt("T25"),
    ),
)


def scan(test_dir: Path) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    found: dict[str, list[str]] = {}
    unreadable: list[str] = []
    for path in sorted(test_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for decorator in node.decorator_list:
                if not _is_req(decorator):
                    continue
                where = f"{path.name}::{node.name}"
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found.setdefault(arg.value, []).append(where)
                    else:
                        unreadable.append(f"{where} line {decorator.lineno}")
    return {key: tuple(value) for key, value in found.items()}, tuple(unreadable)


def _is_req(decorator: ast.expr) -> bool:
    return (
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "req"
        and isinstance(decorator.func.value, ast.Attribute)
        and decorator.func.value.attr == "mark"
    )


def problems(
    requirements: tuple[Requirement, ...], markers: dict[str, tuple[str, ...]], unreadable: tuple[str, ...] = ()
) -> list[str]:
    found = [f"a req marker whose argument is not a literal string, at {where}" for where in unreadable]
    seen: set[str] = set()
    for req in requirements:
        if req.id in seen:
            found.append(f"{req.id} is listed twice")
        seen.add(req.id)
        if not ID_FORM.fullmatch(req.id):
            found.append(f"{req.id!r} is not a requirement id")
        if not req.statement.strip() or "\n" in req.statement:
            found.append(f"{req.id} needs a one-line statement")
        if '"' in req.statement:
            found.append(f"{req.id} quotes someone; a statement is technical only")
        proving = markers.get(req.id, ())
        match req.proof:
            case Built() if not proving:
                found.append(f"{req.id} is listed built but no test carries its marker")
            case NotBuilt(task=task) if not TASK_FORM.fullmatch(task):
                found.append(f"{req.id} is listed not built under {task!r}, which is not a task id")
            case NotBuilt(task=task) if proving:
                found.append(f"{req.id} is listed not built ({task}) but {', '.join(proving)} prove it: mark it built")
    for key, where in sorted(markers.items()):
        if key not in seen:
            found.append(f"{', '.join(where)} name {key}, which the inventory does not list")
    return found
