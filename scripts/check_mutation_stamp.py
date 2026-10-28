#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def problem(changed: list[str], tree: str, stamp: str | None) -> str | None:
    if not any(name.startswith(("src/", "scripts/")) and name.endswith(".py") for name in changed):
        return None
    if stamp is not None and stamp.strip() == tree:
        return None
    return (
        "the staged source has no passing mutation run for this exact tree; "
        "run scripts/mutate.py --staged with the changed modules' tests, then commit"
    )


def main(argv: list[str]) -> int:
    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir"))
    if not (common.parent / "scratch").is_dir():
        return 0
    stamp = Path(_git("rev-parse", "--path-format=absolute", "--git-dir")) / "mutation-stamp"
    found = problem(
        _git("diff", "--cached", "--name-only").split(),
        _git("write-tree"),
        stamp.read_text() if stamp.is_file() else None,
    )
    if found is not None:
        print(f"commit-msg: {found}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
