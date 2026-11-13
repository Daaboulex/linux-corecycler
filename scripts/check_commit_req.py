#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

NAMED = re.compile(r"^Req: (.+)$", re.MULTILINE)


def problems(message: str, requirement_ids: set[str]) -> list[str]:
    named = [item.strip() for items in NAMED.findall(message) for item in items.split(",")]
    return [f"Req: {item} is not in tests/requirement_inventory.py" for item in named if item not in requirement_ids]


def main(argv: list[str]) -> int:
    sys.path.insert(0, str(Path.cwd()))
    from tests.requirement_inventory import REQUIREMENTS

    found = problems(Path(argv[1]).read_text(), {req.id for req in REQUIREMENTS})
    for problem in found:
        print(f"commit-msg: {problem}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
