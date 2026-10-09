from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

INVENTORY = Path(__file__).with_name("requirements.toml")
ID_FORM = re.compile(r"[A-Z][A-Z0-9_]*(\.[A-Za-z0-9_]+)*")
TASK_FORM = re.compile(r"[TSHO][0-9]+[a-z]?")
PROOFS = ("built", "untested", "not built", "open")


@dataclass(frozen=True, slots=True)
class Requirement:
    id: str
    statement: str
    proof: str
    task: str | None = None


def load(path: Path = INVENTORY) -> tuple[Requirement, ...]:
    return tuple(Requirement(**entry) for entry in tomllib.loads(path.read_text())["requirement"])


REQUIREMENTS = load()


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
        if req.proof not in PROOFS:
            found.append(f"{req.id} has the proof {req.proof!r}, not one of {', '.join(PROOFS)}")
        elif req.proof == "built":
            if req.task is not None:
                found.append(f"{req.id} is built and still names the task {req.task}")
            if not proving:
                found.append(f"{req.id} is listed built but no test carries its marker")
        else:
            if req.task is None or not TASK_FORM.fullmatch(req.task):
                found.append(f"{req.id} is listed {req.proof} under {req.task!r}, which is not a task id")
            if proving:
                found.append(
                    f"{req.id} is listed {req.proof} ({req.task}) but {', '.join(proving)} prove it: mark it built"
                )
    for key, where in sorted(markers.items()):
        if key not in seen:
            found.append(f"{', '.join(where)} name {key}, which the inventory does not list")
    return found
