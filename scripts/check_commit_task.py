#!/usr/bin/env python3
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

NAMED = re.compile(r"^(Task|Req): (.+)$", re.MULTILINE)


def problems(message: str, plan: str, requirement_ids: set[str]) -> list[str]:
    named = [(kind, item.strip()) for kind, items in NAMED.findall(message) for item in items.split(",")]
    if not named:
        return ["the message names no Task: or Req: line"]
    found = []
    for kind, item in named:
        if kind == "Req" and item not in requirement_ids:
            found.append(f"Req: {item} is not in tests/requirement_inventory.py")
        task = rf"^(\| {re.escape(item)} \||- {re.escape(item)}[ ,:])"
        if kind == "Task" and not re.search(task, plan, re.MULTILINE):
            found.append(f"Task: {item} is not a task in scratch/PLAN.md")
    return found


def main(argv: list[str]) -> int:
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], capture_output=True, text=True, check=True
    ).stdout.strip()
    plan = Path(common).parent / "scratch" / "PLAN.md"
    if not plan.is_file():
        return 0
    sys.path.insert(0, str(Path.cwd()))
    from tests.requirement_inventory import REQUIREMENTS

    found = problems(Path(argv[1]).read_text(), plan.read_text(), {req.id for req in REQUIREMENTS})
    for problem in found:
        print(f"commit-msg: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
