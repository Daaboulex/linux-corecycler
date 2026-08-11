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

from _contract_hw import require, require_privileged
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
        assert node, f"{c.name}: Ring B test names no node: {c.ring_b_test}"
        text = path.read_text()
        for part in node.split("::"):
            assert part in text, f"{c.name}: Ring B node missing: {c.ring_b_test} ({part})"


class TestTheNeverSilentSkipPolicy:
    """The guard that decides whether a live check is enforced or waved through."""

    def _hw(self, monkeypatch, *, contracts=False, privileged=False):
        import _contract_hw

        monkeypatch.setattr(_contract_hw, "HW_CONTRACTS", contracts)
        monkeypatch.setattr(_contract_hw, "HW_PRIVILEGED", privileged)

    def test_a_present_resource_is_never_in_the_way(self, monkeypatch):
        self._hw(monkeypatch, contracts=True, privileged=True)
        require(True, "present")
        require_privileged(True, "present")

    def test_an_absent_resource_skips_off_the_contract_machine(self, monkeypatch):
        self._hw(monkeypatch)
        with pytest.raises(pytest.skip.Exception):
            require(False, "no msr")

    def test_an_absent_resource_fails_loud_on_the_contract_machine(self, monkeypatch):
        self._hw(monkeypatch, contracts=True)
        with pytest.raises(pytest.fail.Exception, match="no msr"):
            require(False, "no msr")

    def test_a_privileged_resource_skips_until_the_run_holds_the_rights(self, monkeypatch):
        self._hw(monkeypatch, contracts=True)
        with pytest.raises(pytest.skip.Exception, match="privileged tier"):
            require_privileged(False, "needs root")

    def test_a_privileged_resource_fails_loud_under_its_own_flag(self, monkeypatch):
        self._hw(monkeypatch, contracts=True, privileged=True)
        with pytest.raises(pytest.fail.Exception, match="needs root"):
            require_privileged(False, "needs root")
