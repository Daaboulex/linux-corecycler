"""Ring A drift pins and the meta-tests that keep the contract inventory honest.

Each contract's hermetic pin runs here as its own parametrized test, so an
accidental edit to a pinned constant reds a named test. The meta-tests enforce
that every contract has a pin and that every live-verifiable contract wires an
existing Ring B test (no dormant drift seam).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from contract_inventory import CONTRACTS


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_ring_a_pin_holds(contract):
    contract.ring_a()


def test_contract_names_are_unique():
    names = [c.name for c in CONTRACTS]
    assert len(names) == len(set(names))


def test_every_contract_has_a_hermetic_pin():
    for c in CONTRACTS:
        assert callable(c.ring_a), f"{c.name}: no Ring A pin"


def test_no_dormant_drift_seam():
    tests_dir = Path(__file__).parent
    for c in CONTRACTS:
        if not c.live_verifiable:
            assert c.ring_b_test is None, f"{c.name}: not live-verifiable but names a Ring B test"
            continue
        assert c.ring_b_test is not None, f"{c.name}: live-verifiable but wires no Ring B test"
        rel, _, node = c.ring_b_test.partition("::")
        path = tests_dir / rel
        assert path.exists(), f"{c.name}: Ring B file missing: {rel}"
        assert node and node in path.read_text(), f"{c.name}: Ring B node missing: {c.ring_b_test}"
