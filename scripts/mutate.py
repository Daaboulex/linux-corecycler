#!/usr/bin/env python3
"""Mutation check: prove the tests can fail.

Coverage answers "did this line run". It cannot answer "would a test notice if
this line were wrong" -- a test that executes a line and asserts nothing about
it scores identically to one that pins its behaviour. This applies one small
semantic change at a time and reports every mutant that no test caught. A
survivor is a line the suite executes but does not check.

Deliberate, not a gate: it rewrites source files in place (restoring them
afterwards) and runs the test selection once per mutant, so it is minutes of
work and belongs to the dev loop, like re-recording a cassette.

    python3 scripts/mutate.py --src src/corecycler/smu/commands.py \\
        --tests tests/test_smu_commands.py --max 60
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

_COMPARE_SWAP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
_BINOP_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add}


@dataclass(frozen=True)
class Mutant:
    path: Path
    lineno: int
    description: str
    source: str


def _inert(tree: ast.Module) -> set[int]:
    """Nodes whose value cannot change behaviour: decorator arguments (a
    dataclass(slots=...) flip is invisible to every real path) and dunder
    configuration like __test__, which addresses the test runner, not the code."""
    inert: set[int] = set()
    for node in ast.walk(tree):
        for decorator in getattr(node, "decorator_list", []):
            inert.update(id(sub) for sub in ast.walk(decorator))
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__")
            for t in node.targets
        ):
            inert.update(id(sub) for sub in ast.walk(node.value))
    return inert


class _Mutator(ast.NodeTransformer):
    def __init__(self, target: int, inert: set[int] | None = None) -> None:
        self.target = target
        self.inert = inert or set()
        self.seen = 0
        self.applied: str | None = None
        self.lineno = 0

    def _take(self, node: ast.AST, description: str) -> bool:
        if id(node) in self.inert:
            return False
        hit = self.seen == self.target
        self.seen += 1
        if hit:
            self.applied = description
            self.lineno = getattr(node, "lineno", 0)
        return hit

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in _COMPARE_SWAP:
            new = _COMPARE_SWAP[type(node.ops[0])]
            if self._take(node, f"{type(node.ops[0]).__name__} -> {new.__name__}"):
                node.ops = [new()]
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        new = ast.Or if isinstance(node.op, ast.And) else ast.And
        if self._take(node, f"{type(node.op).__name__} -> {new.__name__}"):
            node.op = new()
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in _BINOP_SWAP:
            new = _BINOP_SWAP[type(node.op)]
            if self._take(node, f"{type(node.op).__name__} -> {new.__name__}"):
                node.op = new()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._take(node, "drop not"):
            return node.operand
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, bool):
            if self._take(node, f"{node.value} -> {not node.value}"):
                return ast.copy_location(ast.Constant(value=not node.value), node)
        elif isinstance(node.value, int) and self._take(node, f"{node.value} -> {node.value + 1}"):
            return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node


def _count(source: str) -> int:
    tree = ast.parse(source)
    counter = _Mutator(target=-1, inert=_inert(tree))
    counter.visit(tree)
    return counter.seen


def _build(path: Path, index: int) -> Mutant | None:
    original = path.read_text()
    tree = ast.parse(original)
    mutator = _Mutator(target=index, inert=_inert(tree))
    mutated = mutator.visit(tree)
    if mutator.applied is None:
        return None
    ast.fix_missing_locations(mutated)
    return Mutant(path, mutator.lineno, mutator.applied, ast.unparse(mutated))


def _run(tests: list[str], timeout: int) -> bool:
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-x", "-q", "--no-header",
            "-p", "no:cacheprovider", "-m", "not slow", f"--timeout={timeout}", *tests,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return result.returncode != 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", action="append", required=True)
    parser.add_argument("--tests", nargs="+", required=True)
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    targets: list[Path] = []
    for entry in args.src:
        path = Path(entry)
        targets.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])

    if _run(args.tests, args.timeout):
        print("BASELINE FAILED: the test selection is already red -- fix that first")
        return 2

    killed, survived = 0, []
    for path in targets:
        original = path.read_text()
        total = _count(original)
        step = max(1, total // args.max) if args.max and args.max < total else 1
        indices = list(range(0, total, step))
        print(f"== {path} ({len(indices)} of {total} mutants)")
        try:
            for index in indices:
                mutant = _build(path, index)
                if mutant is None:
                    continue
                path.write_text(mutant.source)
                if _run(args.tests, args.timeout):
                    killed += 1
                    print(".", end="", flush=True)
                else:
                    survived.append(mutant)
                    print("S", end="", flush=True)
        finally:
            path.write_text(original)
        print()

    total = killed + len(survived)
    score = (killed / total * 100) if total else 100.0
    print(f"\nmutants {total}  killed {killed}  survived {len(survived)}  score {score:.1f}%")
    for mutant in survived:
        print(f"  SURVIVED {mutant.path}:{mutant.lineno}  {mutant.description}")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
