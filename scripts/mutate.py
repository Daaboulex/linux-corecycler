#!/usr/bin/env python3
"""Mutation check: prove the tests can fail.

Coverage answers "did this line run". It cannot answer "would a test notice if
this line were wrong" -- a test that executes a line and asserts nothing about
it scores identically to one that pins its behaviour. This applies one small
semantic change at a time and reports every mutant that no test caught. A
survivor is a line the suite executes but does not check.

With --src it rewrites the given files in place (restoring them afterwards).
With --staged or --commit it mutates only the lines that diff adds, in a
throwaway copy of exactly that content (the index, or the commit), and runs the
test files the same change touches unless --tests names others.

    python3 scripts/mutate.py --src src/corecycler/smu/commands.py \\
        --tests tests/test_smu_commands.py --max 60
    python3 scripts/mutate.py --staged
    python3 scripts/mutate.py --commit HEAD
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import fit

REPO = Path(__file__).resolve().parent.parent

_COMPARE_SWAP = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}
_BINOP_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add}


@dataclass(frozen=True)
class Mutant:
    path: Path
    lineno: int
    description: str
    source: str
    span: tuple[int, int] = (0, 0)
    at_import: bool = False


def _inert(tree: ast.Module) -> set[int]:
    """Nodes whose value cannot change behaviour: decorator arguments (a
    dataclass(slots=...) flip is invisible to every real path) and dunder
    configuration like __test__, which addresses the test runner, not the code."""
    inert: set[int] = set()
    for node in ast.walk(tree):
        for decorator in getattr(node, "decorator_list", []):
            inert.update(id(sub) for sub in ast.walk(decorator))
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__") for t in node.targets
        ):
            inert.update(id(sub) for sub in ast.walk(node.value))
    return inert


class _Mutator(ast.NodeTransformer):
    def __init__(
        self, target: int, inert: set[int] | None = None, lines: set[int] | None = None, skip: set[int] | None = None
    ) -> None:
        self.target = target
        self.inert = inert or set()
        self.lines = lines
        self.skip = skip or set()
        self.seen = 0
        self.applied: str | None = None
        self.lineno = 0
        self.col_offset = 0
        self.end_lineno = 0

    def _take(self, node: ast.AST, description: str) -> bool:
        line = getattr(node, "lineno", 0)
        if id(node) in self.inert or line in self.skip or (self.lines is not None and line not in self.lines):
            return False
        hit = self.seen == self.target
        self.seen += 1
        if hit:
            self.applied = description
            self.lineno = getattr(node, "lineno", 0)
            self.col_offset = getattr(node, "col_offset", 0)
            self.end_lineno = getattr(node, "end_lineno", self.lineno)
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


EQUIVALENT = re.compile(r"#\s*equivalent-mutant:(.*)$")


def _reviewed(source: str) -> set[int]:
    reviewed = set()
    for number, line in enumerate(source.splitlines(), start=1):
        match = EQUIVALENT.search(line)
        if match is None:
            continue
        if not match.group(1).strip():
            raise ValueError(f"line {number} marks an equivalent mutant without saying why")
        reviewed.add(number)
    return reviewed


def _count(source: str, lines: set[int] | None = None) -> int:
    tree = ast.parse(source)
    counter = _Mutator(target=-1, inert=_inert(tree), lines=lines, skip=_reviewed(source))
    counter.visit(tree)
    return counter.seen


def _build(path: Path, original: str, index: int, lines: set[int] | None = None) -> Mutant | None:
    tree = ast.parse(original)
    mutator = _Mutator(target=index, inert=_inert(tree), lines=lines, skip=_reviewed(original))
    mutated = mutator.visit(tree)
    if mutator.applied is None:
        return None
    ast.fix_missing_locations(mutated)
    plain = ast.parse(original)
    span = _statement_span(plain, mutator.lineno, mutator.end_lineno)
    at_import = _at_import(plain, (mutator.lineno, mutator.col_offset))
    return Mutant(path, mutator.lineno, mutator.applied, ast.unparse(mutated), span, at_import)


def _at_import(tree: ast.Module, position: tuple[int, int]) -> bool:
    """Whether the code at position runs when its module is imported: outside every function and lambda
    body. Coverage credits such a line only to the test that first imported the module, so its mutant
    must run the whole selection."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bodies = node.body
        elif isinstance(node, ast.Lambda):
            bodies = [node.body]
        else:
            continue
        for body in bodies:
            if (body.lineno, body.col_offset) <= position < (body.end_lineno, body.end_col_offset):
                return False
    return True


def _statement_span(tree: ast.Module, first: int, last: int) -> tuple[int, int]:
    spans = [
        (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt) and node.lineno <= first and (node.end_lineno or node.lineno) >= last
    ]
    return min(spans, key=lambda span: span[1] - span[0], default=(first, last))


SERIAL_UP_TO = 8


def _outcome(
    tests: list[str], timeout: int, root: Path = REPO, extra: list[str] | None = None, env: dict | None = None
) -> tuple[bool, str]:
    serial = len(tests) <= SERIAL_UP_TO and all("::" in test for test in tests)
    args = ["-x", "-q", "--no-header", "-p", "no:cacheprovider", "-m", "not slow", *(extra or []), *tests]
    return fit.run(args, timeout, root, env, most=1 if serial else None)


def _failure(tests: list[str], timeout: int, root: Path = REPO) -> str | None:
    passed, output = _outcome(tests, timeout, root)
    return None if passed else output


Cover = dict[str, dict[int, frozenset[str]]]
Durations = dict[str, float]
DURATION = re.compile(r"^\s*([\d.]+)s (?:setup|call|teardown) +(.+?)\s*$", re.MULTILINE)


def _durations(output: str) -> Durations:
    found: Durations = {}
    for seconds, test in DURATION.findall(output):
        found[test] = found.get(test, 0.0) + float(seconds)
    return found


def _baseline(tests: list[str], timeout: int, root: Path) -> tuple[Cover, Durations] | None:
    with tempfile.TemporaryDirectory(prefix="mutate-cover-") as scratch:
        data_file = Path(scratch) / "coverage"
        env = {**os.environ, "COVERAGE_FILE": str(data_file), "COVERAGE_CORE": "ctrace"}
        extra = ["--cov=src", "--cov-context=test", "--cov-report=", "--durations=0", "--durations-min=0"]
        passed, output = _outcome(tests, timeout, root, extra, env)
        if not passed:
            print(output)
            print("BASELINE FAILED: the test selection is already red or did not finish within --timeout")
            return None
        return _contexts(data_file, root), _durations(output)


def _contexts(data_file: Path, root: Path) -> Cover:
    from coverage import CoverageData

    data = CoverageData(basename=str(data_file))
    data.read()
    cover: Cover = {}
    for measured in data.measured_files():
        path = Path(measured)
        if not path.is_relative_to(root):
            continue
        by_line = data.contexts_by_lineno(measured)
        cover[str(path.relative_to(root))] = {
            line: frozenset(context.rpartition("|")[0] if "|" in context else context for context in contexts)
            for line, contexts in by_line.items()
        }
    return cover


SHORT_S = 2.0


def _tiers(mutant: Mutant, cover: Cover, durations: Durations, tests: list[str], root: Path) -> list[list[str]]:
    lines = cover.get(str(mutant.path.relative_to(root)), {})
    first, last = mutant.span
    contexts = frozenset().union(*(lines.get(number, frozenset()) for number in range(first, last + 1)))
    importers = lines[min(lines)] if lines else frozenset()
    if mutant.at_import or not contexts or "" in contexts or contexts <= importers:
        return [tests]
    short = sorted(test for test in contexts if durations.get(test, 0.0) <= SHORT_S)
    long = sorted(contexts.difference(short))
    return [tier for tier in (short, long) if tier]


HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def added_lines(diff: str) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    for part in re.split(r"^diff --git ", diff, flags=re.MULTILINE)[1:]:
        name = re.search(r"^\+\+\+ b/(\S+)$", part, re.MULTILINE)
        if name is None or not name.group(1).endswith(".py"):
            continue
        lines = set()
        for start, count in HUNK.findall(part):
            lines.update(range(int(start), int(start) + int(count or 1)))
        if lines:
            found[name.group(1)] = lines
    return found


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def _export(commit: str | None, into: Path) -> None:
    if commit is None:
        _git("checkout-index", "--all", f"--prefix={into}/")
        return
    archive = subprocess.run(["git", "archive", commit], cwd=REPO, capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive, check=True)


def importers(changed: list[str], test_dir: Path) -> list[str]:
    patterns = []
    for name in changed:
        module = name.removeprefix("src/").removesuffix(".py").replace("/", ".")
        parent, _, leaf = module.rpartition(".")
        patterns.append(re.compile(rf"\b{re.escape(module)}\b|^from {re.escape(parent)} import [^\n]*\b{leaf}\b", re.M))
    return [
        f"tests/{path.name}"
        for path in sorted(test_dir.glob("test_*.py"))
        if any(pattern.search(path.read_text()) for pattern in patterns)
    ]


def _diff_mode(commit: str | None, tests: list[str] | None, timeout: int) -> int:
    span = ("--cached",) if commit is None else (f"{commit}~1", commit)
    changed = added_lines(_git("diff", "-U0", "--no-color", *span, "--", "src/"))
    touched = _git("diff", "--name-only", "--diff-filter=AM", *span, "--", "tests/").split()
    if not changed:
        print("no source lines changed: nothing to mutate")
        return 0
    selection = (
        tests
        or [name for name in touched if Path(name).name.startswith("test_")]
        or importers(list(changed), REPO / "tests")
    )
    if not selection:
        print("no test file touches or imports the changed modules: name the tests that prove it with --tests")
        return 2
    tree = _git("write-tree").strip() if commit is None else None
    workdir = REPO / ".devshell"
    workdir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mutate-", dir=workdir) as copy:
        root = Path(copy)
        _export(commit, root)
        baseline = _baseline(selection, timeout, root)
        if baseline is None:
            return 2
        verdict = _mutate(
            [(root / name, lines) for name, lines in sorted(changed.items())], selection, timeout, root, 0, baseline
        )
    if verdict == 0 and tree is not None:
        stamp_path().write_text(tree + "\n")
    return verdict


def stamp_path() -> Path:
    return Path(_git("rev-parse", "--path-format=absolute", "--git-dir").strip()) / "mutation-stamp"


def _mutate(
    targets: list[tuple[Path, set[int] | None]],
    tests: list[str],
    timeout: int,
    root: Path,
    cap: int,
    baseline: tuple[Cover, Durations],
) -> int:
    killed, survived = 0, []
    for path, lines in targets:
        original = path.read_text()
        total = _count(original, lines)
        step = max(1, total // cap) if cap and cap < total else 1
        indices = list(range(0, total, step))
        print(f"== {path.relative_to(root)} ({len(indices)} of {total} mutants)")
        try:
            for index in indices:
                mutant = _build(path, original, index, lines)
                if mutant is None:
                    continue
                path.write_text(mutant.source)
                tiers = _tiers(mutant, *baseline, tests, root)
                if any(_failure(tier, timeout, root) is not None for tier in tiers):
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
        print(f"  SURVIVED {mutant.path.relative_to(root)}:{mutant.lineno}  {mutant.description}")
    return 1 if survived else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--src", action="append")
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--commit")
    parser.add_argument("--tests", nargs="+")
    parser.add_argument("--max", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if args.src is None:
        return _diff_mode(args.commit, args.tests, args.timeout)
    if not args.tests:
        parser.error("--src needs --tests")
    targets: list[tuple[Path, set[int] | None]] = []
    for entry in args.src:
        path = Path(entry).resolve()
        targets.extend((found, None) for found in (sorted(path.rglob("*.py")) if path.is_dir() else [path]))
    baseline = _baseline(args.tests, args.timeout, REPO)
    if baseline is None:
        return 2
    return _mutate(targets, args.tests, args.timeout, REPO, args.max, baseline)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except fit.Starved as starved:
        print(f"STARVED: {starved}; no verdict")
        raise SystemExit(3) from starved
