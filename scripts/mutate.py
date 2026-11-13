#!/usr/bin/env python3
"""Mutation check: prove the tests can fail.

Coverage answers "did this line run". It cannot answer "would a test notice if
this line were wrong" -- a test that executes a line and asserts nothing about
it scores identically to one that pins its behaviour. This applies one small
semantic change at a time and reports every mutant that no test caught. A
survivor is a line the suite executes but does not check.

With --src it rewrites the given files in place (restoring them afterwards).
With --staged or --commit it mutates only the lines that diff adds under src/
and scripts/, in a throwaway copy of exactly that content (the index, or the commit), and runs the
test files the same change touches unless --tests names others.

    python3 scripts/mutate.py --src src/corecycler/smu/commands.py \\
        --tests tests/test_smu_commands.py --max 60
    python3 scripts/mutate.py --staged
    python3 scripts/mutate.py --commit HEAD

The operators, each a line of the report with the mutants it made, those that survived and the
seconds they took:
- swap comparison, swap and/or, swap arithmetic, drop not, change constant, delete statement;
- drop operand: one operand of an and or an or;
- force condition: an if, elif, conditional or comprehension test True and False, a while test
  False only; a constant test, if TYPE_CHECKING and a test inside an f-string are left;
- narrow membership: on the right of in or not in, one element of a literal, one operand of a |
  union, or one member of a name bound to a module-level frozenset, set or tuple literal (in the
  module, or through one from corecycler... import) written {m for m in NAME if m != MEMBER},
  when the module names the member;
- swap arms: two adjacent arms of an if/elif chain, or two sibling ifs whose bodies end in return
  or raise, when either test is a mutated line; not when their bodies are identical or their tests
  compare one expression by is or == with two different constants or attributes;
- revert hunk: a hunk in a function whose added lines are one simple statement, or one if, elif or
  while line holding its whole test, put back to the lines it removed, when that parses and changes
  more than string text and variable annotations (not with --src); a hunk at import, of other lines,
  or inside a longer statement is not reverted;
- unwrap key: a comparison whose sides call one function with one argument compares the arguments;
- force argument: a keyword argument True and False where a function the module defines at its
  top level annotates that parameter bool.
Not made: a bit dropped from a mask (& 3 to & 1), and an is comparison widened to the other
members of its enum.
"""

from __future__ import annotations

import argparse
import ast
import math
import os
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import cache, reduce
from itertools import chain
from pathlib import Path
from time import monotonic

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
    operator: str
    description: str
    source: str
    span: tuple[int, int] = (0, 0)
    at_import: bool = False


@dataclass(frozen=True)
class Hunk:
    start: int
    count: int
    removed: tuple[str, ...]


def _exits(node: ast.stmt) -> bool:
    return isinstance(node, ast.If) and not node.orelse and isinstance(node.body[-1], (ast.Return, ast.Raise))


def _same(first: list[ast.stmt], second: list[ast.stmt]) -> bool:
    return [ast.dump(statement) for statement in first] == [ast.dump(statement) for statement in second]


def _subject(test: ast.expr) -> tuple[str, str] | None:
    """What a test compares by is or == and the constant or attribute it compares it with, as dumps."""
    match test:
        case ast.Compare(left=left, ops=[ast.Is() | ast.Eq()], comparators=[ast.Constant() | ast.Attribute() as right]):
            return ast.dump(left), ast.dump(right)
    return None


def _union(node: ast.expr) -> list[ast.expr]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return [*_union(node.left), *_union(node.right)]
    return [node]


def _joined(left: ast.expr, right: ast.expr) -> ast.expr:
    return ast.BinOp(left, ast.BitOr(), right)


def _sole_call(node: ast.expr) -> str | None:
    """The function a call with one positional argument and nothing else calls, as a dump."""
    match node:
        case ast.Call(func=func, args=[argument], keywords=[]) if not isinstance(argument, ast.Starred):
            return ast.dump(func)
    return None


def _literal_sets(tree: ast.Module) -> dict[str, list[ast.expr]]:
    """The members of each module-level name bound to a frozenset, set or tuple literal."""
    found: dict[str, list[ast.expr]] = {}
    for statement in tree.body:
        match statement:
            case (
                ast.Assign(targets=[ast.Name(id=name)], value=value)
                | ast.AnnAssign(target=ast.Name(id=name), value=value)
            ):
                match value:
                    case ast.Call(
                        func=ast.Name(id="frozenset" | "set" | "tuple"),
                        args=[ast.Set() | ast.Tuple() | ast.List() as literal],
                        keywords=[],
                    ) | (ast.Set() | ast.Tuple() as literal):
                        found[name] = [member for member in literal.elts if not isinstance(member, ast.Starred)]
    return found


@cache
def _imported_sets(module: Path) -> dict[str, list[ast.expr]]:
    return _literal_sets(ast.parse(module.read_text())) if module.is_file() else {}


def _names(node: ast.AST) -> set[str]:
    return {name.id for name in ast.walk(node) if isinstance(name, ast.Name)}


def _named_sets(tree: ast.Module, path: Path | None) -> dict[str, list[ast.expr]]:
    """The members a membership may drop from each name the module binds to a literal set, its own or one
    imported from the corecycler module that defines it, keeping only members whose names the module names."""
    package = None if path is None else next((up.parent for up in path.parents if up.name == "corecycler"), None)
    found: dict[str, list[ast.expr]] = {}
    for statement in tree.body:
        match statement:
            case ast.ImportFrom(module=str(module), names=aliases) if package is not None and module.startswith(
                "corecycler."
            ):
                sets = _imported_sets(package / f"{module.replace('.', '/')}.py")
                found.update({alias.asname or alias.name: sets[alias.name] for alias in aliases if alias.name in sets})
    found.update(_literal_sets(tree))
    known = _names(tree)
    return {name: [member for member in members if _names(member).issubset(known)] for name, members in found.items()}


def _flags(tree: ast.Module) -> dict[str, set[str]]:
    """The parameters annotated bool that each function the module defines at its top level takes by keyword."""
    return {
        statement.name: {
            parameter.arg
            for parameter in [*statement.args.args, *statement.args.kwonlyargs]
            if isinstance(parameter.annotation, ast.Name) and parameter.annotation.id == "bool"
        }
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _inert(tree: ast.Module) -> set[int]:
    """Nodes whose value cannot change behaviour: decorator arguments (a
    dataclass(slots=...) flip is invisible to every real path), dunder
    configuration like __test__, which addresses the test runner, not the code,
    and a return of None that ends a function, which falling off the end repeats."""
    inert: set[int] = set()
    for node in ast.walk(tree):
        for decorator in getattr(node, "decorator_list", []):
            inert.update(id(sub) for sub in ast.walk(decorator))
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and isinstance(last := node.body[-1], ast.Return)
            and (last.value is None or (isinstance(last.value, ast.Constant) and last.value.value is None))
        ):
            inert.add(id(last))
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id.startswith("__") and t.id.endswith("__") for t in node.targets
        ):
            inert.update(id(sub) for sub in ast.walk(node.value))
    return inert


class _Mutator(ast.NodeTransformer):
    def __init__(
        self, tree: ast.Module, source: str, path: Path | None, target: int | None, lines: set[int] | None
    ) -> None:
        self.target = target
        self.inert = _inert(tree)
        self.lines = lines
        self.skip = _reviewed(source)
        self.members = _named_sets(tree, path)
        self.flags = _flags(tree)
        self.seen = 0
        self.depth = 0
        self.formatted = 0

    def _take(self, node: ast.AST, operator: str, description: str, region: tuple[int, int] | None = None) -> bool:
        line = getattr(node, "lineno", 0)
        if id(node) in self.inert or line in self.skip or (self.lines is not None and line not in self.lines):
            return False
        hit = self.seen == self.target
        self.seen += 1
        if hit:
            self.operator = operator
            self.applied = description
            self.lineno = getattr(node, "lineno", 0)
            self.col_offset = getattr(node, "col_offset", 0)
            self.region = region or (self.lineno, getattr(node, "end_lineno", self.lineno))
        return hit

    def generic_visit(self, node: ast.AST) -> ast.AST:
        super().generic_visit(node)
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list):
                for index in range(len(block) - 1):
                    first, second = block[index], block[index + 1]
                    if _exits(first) and _exits(second) and self._swapped(first, second):
                        block[index], block[index + 1] = second, first
        return node

    def _swapped(self, first: ast.If, second: ast.If) -> bool:
        one, two = _subject(first.test), _subject(second.test)
        excluded = _same(first.body, second.body) or (one and two and one[0] == two[0] and one[1] != two[1])
        tests = [arm.test for arm in (first, second) if self.lines is None or arm.test.lineno in self.lines]
        region = (first.lineno, first.end_lineno)
        return not excluded and bool(tests) and self._take(tests[0], "swap arms", "swap arms", region)

    def _forced(self, test: ast.expr, values: tuple[bool, ...] = (True, False)) -> ast.expr:
        if (
            isinstance(test, ast.Constant)
            or self.formatted
            or (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
        ):
            return test
        for value in values:
            if self._take(test, "force condition", f"condition -> {value}"):
                return ast.copy_location(ast.Constant(value), test)
        return test

    def visit_If(self, node: ast.If) -> ast.AST:
        self.generic_visit(node)
        node.test = self._forced(node.test)
        if len(node.orelse) == 1 and isinstance(after := node.orelse[0], ast.If) and self._swapped(node, after):
            node.test, after.test = after.test, node.test
            node.body, after.body = after.body, node.body
        return node

    def visit_IfExp(self, node: ast.IfExp) -> ast.AST:
        self.generic_visit(node)
        node.test = self._forced(node.test)
        return node

    def visit_While(self, node: ast.While) -> ast.AST:
        self.generic_visit(node)
        node.test = self._forced(node.test, (False,))
        return node

    def visit_comprehension(self, node: ast.comprehension) -> ast.AST:
        self.generic_visit(node)
        node.ifs = [self._forced(test) for test in node.ifs]
        return node

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        self.formatted += 1
        self.generic_visit(node)
        self.formatted -= 1
        return node

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Name):
            for keyword in node.keywords:
                if keyword.arg in self.flags.get(node.func.id, ()) and not isinstance(keyword.value, ast.Constant):
                    for value in (True, False):
                        if self._take(keyword.value, "force argument", f"{keyword.arg} -> {value}"):
                            keyword.value = ast.copy_location(ast.Constant(value), keyword.value)
        return node

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if len(node.ops) == 1 and type(node.ops[0]) in _COMPARE_SWAP:
            new = _COMPARE_SWAP[type(node.ops[0])]
            if self._take(node, "swap comparison", f"{type(node.ops[0]).__name__} -> {new.__name__}"):
                node.ops = [new()]
        if isinstance(node.ops[0], (ast.In, ast.NotIn)):
            node.comparators = [self._narrowed(node.comparators[0])]
        if len(node.ops) == 1 and (key := _sole_call(node.left)) is not None and key == _sole_call(node.comparators[0]):
            left, right = node.left, node.comparators[0]
            if self._take(node, "unwrap key", f"unwrap {ast.unparse(left.func)}"):
                node.left, node.comparators = left.args[0], [right.args[0]]
        return node

    def _narrowed(self, right: ast.expr) -> ast.expr:
        if isinstance(right, (ast.Set, ast.Tuple, ast.List)):
            for index, element in enumerate(right.elts):
                if self._take(element, "narrow membership", f"drop {ast.unparse(element)}"):
                    del right.elts[index]
        else:
            operands = _union(right)
            for index, operand in enumerate(operands):
                others = [*operands[:index], *operands[index + 1 :]]
                if others and self._take(operand, "narrow membership", f"drop {ast.unparse(operand)}"):
                    return reduce(_joined, others)
                if isinstance(operand, ast.Name):
                    for member in self.members.get(operand.id, ()):
                        text = ast.unparse(member)
                        if self._take(operand, "narrow membership", f"drop {text} from {operand.id}"):
                            narrowed = ast.parse(f"{{m for m in {operand.id} if m != {text}}}", mode="eval").body
                            return reduce(_joined, [*operands[:index], narrowed, *operands[index + 1 :]])
        return right

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        new = ast.Or if isinstance(node.op, ast.And) else ast.And
        if self._take(node, "swap and/or", f"{type(node.op).__name__} -> {new.__name__}"):
            node.op = new()
        for index, operand in enumerate(node.values):
            if self._take(operand, "drop operand", f"drop {type(node.op).__name__} operand {index + 1}"):
                kept = [*node.values[:index], *node.values[index + 1 :]]
                return kept[0] if len(kept) == 1 else ast.copy_location(ast.BoolOp(node.op, kept), node)
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in _BINOP_SWAP:
            new = _BINOP_SWAP[type(node.op)]
            if self._take(node, "swap arithmetic", f"{type(node.op).__name__} -> {new.__name__}"):
                node.op = new()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._take(node, "drop not", "drop not"):
            return node.operand
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if isinstance(node.value, bool):
            if self._take(node, "change constant", f"{node.value} -> {not node.value}"):
                return ast.copy_location(ast.Constant(value=not node.value), node)
        elif isinstance(node.value, int) and self._take(node, "change constant", f"{node.value} -> {node.value + 1}"):
            return ast.copy_location(ast.Constant(value=node.value + 1), node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.AST:
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def _delete(self, node: ast.stmt) -> ast.AST:
        """A statement that does work inside a function becomes pass. Imports, declarations, bare
        constants and code that runs at import are left: deleting them is a NameError at first use or
        nothing at all, at the price of the whole selection."""
        self.generic_visit(node)
        declares = isinstance(node, ast.AnnAssign) and node.value is None
        constant = isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
        if self.depth and not declares and not constant and self._take(node, "delete statement", "delete statement"):
            return ast.copy_location(ast.Pass(), node)
        return node

    visit_Expr = visit_Assign = visit_AugAssign = visit_AnnAssign = _delete
    visit_Return = visit_Raise = visit_Delete = visit_Break = visit_Continue = _delete


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


def _walk(source: str, target: int | None, lines: set[int] | None, path: Path | None) -> tuple[_Mutator, ast.AST]:
    tree = ast.parse(source)
    mutator = _Mutator(tree, source, path, target, lines)
    return mutator, mutator.visit(tree)


def _count(source: str, lines: set[int] | None = None, path: Path | None = None) -> int:
    return _walk(source, None, lines, path)[0].seen


def _build(path: Path, original: str, index: int, lines: set[int] | None = None) -> Mutant:
    mutator, mutated = _walk(original, index, lines, path)
    ast.fix_missing_locations(mutated)
    plain = ast.parse(original)
    span = _statement_span(plain, *mutator.region)
    at_import = _at_import(plain, (mutator.lineno, mutator.col_offset))
    return Mutant(path, mutator.lineno, mutator.operator, mutator.applied, ast.unparse(mutated), span, at_import)


def _behaviour(tree: ast.AST) -> str:
    """The tree as a dump without its string text, an f-string's literal parts included, and its variable
    annotations, which postponed evaluation never runs."""
    for node in ast.walk(tree):
        match node:
            case ast.Constant(value=str()):
                node.value = ""
            case ast.AnnAssign():
                node.annotation = None
    return ast.dump(tree)


def _reverts(path: Path, source: str, hunks: list[Hunk]) -> list[Mutant]:
    """For each hunk in a function whose added lines are one simple statement, or one if, elif or while line holding
    its whole test, the source with the lines it removed put back, when that parses and differs from the source by
    more than string text and variable annotations."""
    lines = source.split("\n")
    plain = ast.parse(source)
    statements = {
        (node.lineno, node.end_lineno)
        for node in ast.walk(plain)
        if isinstance(node, ast.stmt) and not hasattr(node, "body")
    }
    headers = {
        (node.lineno, node.lineno)
        for node in ast.walk(plain)
        if isinstance(node, (ast.If, ast.While)) and node.test.end_lineno == node.lineno
    }
    new = _behaviour(ast.parse(source))
    skip = _reviewed(source)
    found = []
    for hunk in hunks:
        span = (hunk.start, hunk.start + hunk.count - 1)
        first = lines[hunk.start - 1]
        position = (hunk.start, len(first) - len(first.lstrip()))
        if not hunk.removed or span not in statements | headers or hunk.start in skip or _at_import(plain, position):
            continue
        reverted = "\n".join([*lines[: hunk.start - 1], *hunk.removed, *lines[hunk.start - 1 + hunk.count :]])
        try:
            tree = ast.parse(reverted)
        except SyntaxError:
            continue
        mutated = ast.unparse(tree)
        if _behaviour(tree) != new:
            found.append(Mutant(path, hunk.start, "revert hunk", "revert hunk", mutated, span))
    return found


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


LATE = "the test selection did not finish within"
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
        extra = [
            "--cov=src",
            "--cov=scripts",
            "--cov-context=test",
            "--cov-report=",
            "--durations=0",
            "--durations-min=0",
        ]
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


LIMIT_FLOOR_S = 30  # equivalent-mutant: a start-up allowance chosen from measured runs that every test reads by name
LIMIT_FACTOR = 5  # equivalent-mutant: a margin chosen from measured runs that every test reads by name


def _limit(tier: list[str], durations: Durations, timeout: int) -> int:
    """Seconds a tier may run before its mutant counts as hung, and so killed: a multiple of its tests'
    baseline seconds summed as one worker would run them, at least a floor for pytest's own start, never
    over --timeout. A tier holding a test the baseline did not time keeps --timeout."""
    if not tier or any(test not in durations for test in tier):
        return timeout
    return min(timeout, max(LIMIT_FLOOR_S, math.ceil(LIMIT_FACTOR * sum(durations[test] for test in tier))))


HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


def hunks(diff: str) -> dict[str, list[Hunk]]:
    """Each hunk that adds lines to a Python file: the first added line, how many, and the lines it removed."""
    found: dict[str, list[Hunk]] = {}
    for part in re.split(r"^diff --git ", diff, flags=re.MULTILINE)[1:]:
        name = re.search(r"^\+\+\+ b/(\S+)$", part, re.MULTILINE)
        if name is None or not name.group(1).endswith(".py"):
            continue
        read: list[tuple[int, int, list[str]]] = []
        for line in part.split("\n"):
            if header := HUNK.match(line):
                read.append((int(header[1]), int(header[2] or 1), []))
            if read and line.startswith("-"):
                read[-1][2].append(line[1:])
        adding = [Hunk(start, count, tuple(removed)) for start, count, removed in read if count]
        if adding:
            found[name.group(1)] = adding
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
    changed = hunks(_git("diff", "-U0", "--no-color", *span, "--", "src/", "scripts/"))
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
    targets: list[tuple[Path, list[Hunk] | None]],
    tests: list[str],
    timeout: int,
    root: Path,
    cap: int,
    baseline: tuple[Cover, Durations],
) -> int:
    killed, survived, timed_out = 0, [], []
    made: Counter[str] = Counter()
    spent: defaultdict[str, float] = defaultdict(float)
    for path, changes in targets:
        original = path.read_text()
        lines = None if changes is None else {n for hunk in changes for n in range(hunk.start, hunk.start + hunk.count)}
        reverts = [] if changes is None else _reverts(path, original, changes)
        total = _count(original, lines, path)
        step = max(1, total // cap) if cap and cap < total else 1
        indices = list(range(0, total, step))
        print(f"== {path.relative_to(root)} ({len(indices) + len(reverts)} of {total + len(reverts)} mutants)")
        try:
            for mutant in chain((_build(path, original, index, lines) for index in indices), reverts):
                path.write_text(mutant.source)
                started = monotonic()
                tiers = _tiers(mutant, *baseline, tests, root)
                failed = next(
                    (
                        (output, limit)
                        for tier in tiers
                        if (output := _failure(tier, limit := _limit(tier, baseline[1], timeout), root)) is not None
                    ),
                    None,
                )
                made[mutant.operator] += 1
                spent[mutant.operator] += monotonic() - started
                if failed is not None:
                    killed += 1
                    late = LATE in failed[0]
                    if late:
                        timed_out.append((mutant, failed[1]))
                    print("T" if late else ".", end="", flush=True)
                else:
                    survived.append(mutant)
                    print("S", end="", flush=True)
        finally:
            path.write_text(original)
        print()

    total = killed + len(survived)
    score = (killed / total * 100) if total else 100.0
    print(f"\nmutants {total}  killed {killed}  survived {len(survived)}  score {score:.1f}%")
    lost = Counter(mutant.operator for mutant in survived)
    for operator in sorted(made):
        print(f"  {operator:<17} made {made[operator]:4d}  survived {lost[operator]:3d}  {spent[operator]:7.1f} s")
    for mutant in survived:
        print(f"  SURVIVED {mutant.path.relative_to(root)}:{mutant.lineno}  {mutant.description}")
    for mutant, limit in timed_out:
        print(f"  TIMED OUT {mutant.path.relative_to(root)}:{mutant.lineno}  {mutant.description} (limit {limit} s)")
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
