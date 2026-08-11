"""Ring B (live contract) test helpers -- the never-silent skip policy.

Two tiers, because a live check can need the hardware, the privileges, or both:

    CORECYCLER_HW_CONTRACTS=1 pytest -m contract       # this machine has the hardware
    CORECYCLER_HW_PRIVILEGED=1 pytest -m contract      # and this process has the rights

Under the first, an absent hardware resource is a HARD FAILURE: a drift-check
that silently green-skips on the very machine meant to run it would hide a gap
(the owner's never-silent rule). Reading MSRs (CAP_SYS_RAWIO), dmidecode
(root) and the SMU mailbox (the corecycler group) additionally need privileges
that the self-hosted runner deliberately does not hold -- CI with SMU write
access could set a Curve Optimizer offset -- so those checks are fatal only
under the second flag, and otherwise skip naming exactly what is uncovered.
Without either variable every absent resource skips with its reason.
"""

from __future__ import annotations

import os

import pytest

HW_CONTRACTS = os.environ.get("CORECYCLER_HW_CONTRACTS") == "1"
HW_PRIVILEGED = os.environ.get("CORECYCLER_HW_PRIVILEGED") == "1"


def require(resource_present: bool, reason: str) -> None:
    if resource_present:
        return
    if HW_CONTRACTS:
        pytest.fail(f"hardware-contract resource absent under CORECYCLER_HW_CONTRACTS=1: {reason}")
    pytest.skip(reason)


def require_privileged(resource_present: bool, reason: str) -> None:
    """Like require, for what needs root, CAP_SYS_RAWIO or the corecycler group."""
    if resource_present:
        return
    if HW_PRIVILEGED:
        pytest.fail(f"privileged resource absent under CORECYCLER_HW_PRIVILEGED=1: {reason}")
    pytest.skip(f"{reason} [privileged tier: run as root with CORECYCLER_HW_PRIVILEGED=1]")
