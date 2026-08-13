"""No except may silently swallow -- it must surface the failure or say it is deliberate.

Two rules, one intent: a reader must never have to guess whether a swallowed
failure was meant to be swallowed.

1. A bare ``except:`` or ``except Exception/BaseException:`` that leaves no trace
   of the failure hides an unexpected error. A handler "surfaces" when it
   re-raises, logs, emits the error on a Qt signal, prints it, or otherwise
   references the caught exception.
2. A TYPED handler whose whole body is ``pass`` must be written as
   ``contextlib.suppress(...)``. The two forms are identical to the interpreter
   and opposite to a reader: ``suppress`` states that the failure is expected
   and acceptable, while ``except X: pass`` reads the same whether the author
   meant it or wrote the wrong type. Both real cases this rule was written for
   caught exceptions that could not occur while the reachable failure went
   unguarded, and neither was visible as anything but a normal handler.

``continue`` bodies are exempt: skipping one malformed row inside a loop is
visible from its context. A ``__del__`` finalizer is exempt because surfacing is
unsafe during interpreter shutdown.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical"})


def _is_blind(handler: ast.ExceptHandler) -> bool:
    typ = handler.type
    if typ is None:
        return True
    names: list[str] = []
    if isinstance(typ, ast.Name):
        names = [typ.id]
    elif isinstance(typ, ast.Tuple):
        names = [e.id for e in typ.elts if isinstance(e, ast.Name)]
    return any(n in ("Exception", "BaseException") for n in names)


def _surfaces(handler: ast.ExceptHandler) -> bool:
    bound = handler.name
    for node in ast.walk(handler):
        if isinstance(node, ast.Raise):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and (func.attr in _LOG_METHODS or func.attr == "emit"):
                return True
            if isinstance(func, ast.Name) and func.id == "print":
                return True
        if bound and isinstance(node, ast.Name) and node.id == bound:
            return True
    return False


def _finalizer_handlers(tree: ast.Module) -> set[ast.ExceptHandler]:
    exempt: set[ast.ExceptHandler] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "__del__":
            for sub in ast.walk(node):
                if isinstance(sub, ast.ExceptHandler):
                    exempt.add(sub)
    return exempt


def test_no_silent_blind_except():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        exempt = _finalizer_handlers(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node not in exempt and _is_blind(node) and not _surfaces(node):
                offenders.append(f"{path.relative_to(SRC.parent)}:{node.lineno}")
    assert not offenders, (
        "Blind except that does not surface the failure (silent swallow) -- re-raise, log it, "
        "emit/print it, or use contextlib.suppress:\n  " + "\n  ".join(offenders)
    )


def test_no_typed_pass_swallow():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        exempt = _finalizer_handlers(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ExceptHandler)
                and node not in exempt
                and not _is_blind(node)
                and len(node.body) == 1
                and isinstance(node.body[0], ast.Pass)
            ):
                offenders.append(f"{path.relative_to(SRC.parent)}:{node.lineno}")
    assert not offenders, (
        "A typed handler whose whole body is `pass` cannot be told apart from one that "
        "catches the wrong exception type. Write a deliberate suppression as "
        "contextlib.suppress(...), or surface the failure:\n  " + "\n  ".join(offenders)
    )
