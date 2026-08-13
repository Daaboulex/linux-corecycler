"""The wheel ships exactly one top-level package: corecycler.

site-packages is a global namespace shared by every package in a merged
environment (a Nix profile, a venv). A flat top-level module like cli.py
collides there with any other application shipping the same name — a Nix
home profile holding corecycler and hermes-agent (which also ships a flat
cli.py) failed to build on exactly that. Everything lives under the
corecycler package; nothing installs flat.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_src_holds_exactly_the_corecycler_package():
    build_litter = ("__pycache__", ".egg-info")
    entries = {p.name for p in (_ROOT / "src").iterdir() if not p.name.endswith(build_litter)}
    assert entries == {"corecycler"}, f"stray top-level entries in src/: {sorted(entries - {'corecycler'})}"


def test_no_flat_py_modules_declared():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    setuptools_cfg = pyproject.get("tool", {}).get("setuptools", {})
    assert "py-modules" not in setuptools_cfg, "flat py-modules reintroduce the site-packages collision"


def test_entry_points_live_inside_the_package():
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    for name, target in pyproject["project"]["scripts"].items():
        assert target.startswith("corecycler."), f"{name} = {target} points outside the corecycler package"
