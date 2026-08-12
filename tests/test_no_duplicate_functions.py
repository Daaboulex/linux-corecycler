"""Copy-paste guard: no two functions in src/ may share a body that is
identical once local variable names are normalized away.

Exact-match fingerprinting misses the most common copy-paste: duplicate a
function, then rename its local variables. This gate renames every bound name
(arguments, assignment targets, loop and comprehension variables) to a
position token before fingerprinting, so a renamed copy still collides -- while
call targets, attribute names and literals are preserved, so two functions of
the same shape that call different helpers or use different constants stay
distinct. Trivial bodies (fewer than MIN_STATEMENTS statements) are exempt:
one-line delegations are idiom, not duplication.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
MIN_STATEMENTS = 3


def _bound_names(body: list[ast.stmt], signature_args: set[str]) -> set[str]:
    bound: set[str] = set(signature_args)
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.arg):
                bound.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                bound.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
    return bound


def _signature_args(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    a = fn.args
    names = {arg.arg for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _normalize_bindings(body: list[ast.stmt], signature_args: set[str]) -> ast.Module:
    bound = _bound_names(body, signature_args)
    mapping: dict[str, str] = {}

    def token(name: str) -> str:
        if name not in mapping:
            mapping[name] = f"_v{len(mapping)}"
        return mapping[name]

    class Rename(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name):
            if node.id in bound:
                return ast.copy_location(ast.Name(id=token(node.id), ctx=node.ctx), node)
            return node

        def visit_arg(self, node: ast.arg):
            node.arg = token(node.arg)
            node.annotation = None
            return node

    module = ast.parse(ast.unparse(ast.Module(body=list(body), type_ignores=[])))
    Rename().visit(module)
    return module


def body_fingerprint(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) < MIN_STATEMENTS:
        return None
    module = _normalize_bindings(body, _signature_args(fn))
    return ast.dump(module, annotate_fields=True, include_attributes=False)


def _collect() -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fp = body_fingerprint(node)
                if fp is None:
                    continue
                where = f"{path.relative_to(SRC.parent)}:{node.lineno} ({node.name})"
                seen.setdefault(fp, []).append(where)
    return seen


def test_no_duplicate_function_bodies():
    dups = {fp: locs for fp, locs in _collect().items() if len(locs) > 1}
    assert not dups, (
        "Duplicated function bodies found (identical after renaming local "
        "variables) -- extract a shared helper:\n"
        + "\n".join("  == " + " | ".join(locs) for locs in dups.values())
    )


def _fp_of(source: str) -> str | None:
    fn = ast.parse(source).body[0]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    return body_fingerprint(fn)


class TestGateCatchesRenamedCopies:
    def test_a_renamed_copy_collides(self):
        original = (
            "def a(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item\n"
            "    return total\n"
        )
        renamed = (
            "def b(values):\n"
            "    acc = 0\n"
            "    for value in values:\n"
            "        acc += value\n"
            "    return acc\n"
        )
        assert _fp_of(original) == _fp_of(renamed)

    def test_a_different_call_target_does_not_collide(self):
        one = (
            "def a(db, run_id, path):\n"
            "    text = export_run_csv(db, run_id)\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    path.write_text(text)\n"
        )
        two = (
            "def b(db, run_ids, path):\n"
            "    text = export_runs_bulk_csv(db, run_ids)\n"
            "    path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    path.write_text(text)\n"
        )
        assert _fp_of(one) != _fp_of(two)

    def test_a_different_literal_does_not_collide(self):
        one = "def a(x):\n    y = x + 1\n    z = y * 2\n    return z\n"
        two = "def b(x):\n    y = x + 1\n    z = y * 3\n    return z\n"
        assert _fp_of(one) != _fp_of(two)

    def test_a_short_body_is_exempt(self):
        assert _fp_of("def a(x):\n    return x + 1\n") is None
