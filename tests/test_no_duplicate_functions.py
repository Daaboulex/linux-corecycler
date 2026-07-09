"""Copy-paste guard: no two functions in src/ may share an identical body.

An identical AST body (docstrings stripped, positions ignored) is a
copy-paste that will drift — one copy gets the fix, the other keeps the bug.
Extract it to a shared helper instead. Trivial bodies (fewer than
MIN_STATEMENTS statements) are exempt: one-line delegations are idiom, not
duplication.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
MIN_STATEMENTS = 3


def _body_fingerprint(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    body = list(fn.body)
    # strip a leading docstring — identical logic with different docs is still a dup
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    if len(body) < MIN_STATEMENTS:
        return None
    return "\n".join(ast.dump(stmt, annotate_fields=True, include_attributes=False)
                     for stmt in body)


def _collect() -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fp = _body_fingerprint(node)
                if fp is None:
                    continue
                where = f"{path.relative_to(SRC.parent)}:{node.lineno} ({node.name})"
                seen.setdefault(fp, []).append(where)
    return seen


def test_no_duplicate_function_bodies():
    dups = {fp: locs for fp, locs in _collect().items() if len(locs) > 1}
    assert not dups, (
        "Duplicated function bodies found — extract a shared helper:\n"
        + "\n".join("  == " + " | ".join(locs) for locs in dups.values())
    )
