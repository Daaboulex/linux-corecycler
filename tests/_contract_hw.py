"""Ring B (live contract) test helpers -- the never-silent skip policy.

The ryzen hardware-contract CI step exports CORECYCLER_HW_CONTRACTS=1. There, an
absent resource is a HARD FAILURE: a drift-check that silently green-skips on the
very machine meant to run it would hide a gap (the owner's never-silent rule).
Off that runner (dev box, nix sandbox) the test skips with a reason.
"""

from __future__ import annotations

import os

import pytest

HW_CONTRACTS = os.environ.get("CORECYCLER_HW_CONTRACTS") == "1"


def require(resource_present: bool, reason: str) -> None:
    if resource_present:
        return
    if HW_CONTRACTS:
        pytest.fail(f"hardware-contract resource absent under CORECYCLER_HW_CONTRACTS=1: {reason}")
    pytest.skip(reason)
