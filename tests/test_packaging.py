"""Every top-level src module must be declared for packaging.

setuptools packages sub-packages via [tool.setuptools.packages.find], but
single-file top-level modules (main.py, cli.py, notify.py) ship ONLY if named
in py-modules. A module added there but omitted here is silently dropped from
the built wheel — the app then crashes at runtime importing it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_every_top_level_src_module_is_declared():
    src = _ROOT / "src"
    modules = {
        p.stem for p in src.glob("*.py") if p.name != "__init__.py"
    }
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    declared = set(pyproject["tool"]["setuptools"].get("py-modules", []))
    missing = modules - declared
    assert not missing, (
        f"top-level src modules not in pyproject py-modules (won't be packaged): "
        f"{sorted(missing)}"
    )
