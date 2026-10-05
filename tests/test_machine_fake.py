from __future__ import annotations

import uuid

import pytest

UNKNOWN_BOOT = "0123456789abcdef0123456789abcdef"
REFUSALS = {
    "unknown": "No journal boot entry found for the specified boot ({}+0).",
    "dashed": "Failed to add match '{}': Invalid argument",
}


@pytest.mark.parametrize(
    "form",
    [["-n", "1", "-o", "json"], ["-o", "json", "--output-fields=MESSAGE", "_TRANSPORT=kernel"]],
    ids=["last-entry", "kernel-records"],
)
@pytest.mark.parametrize("kind", sorted(REFUSALS))
def test_the_fake_journal_refuses_a_boot_it_does_not_hold(machine, form, kind):
    boot = str(uuid.UUID(machine.boot_id)) if kind == "dashed" else UNKNOWN_BOOT
    result = machine.journalctl(["-b", boot, *form], 1)
    assert (result.returncode, result.stdout, result.stderr) == (1, "", REFUSALS[kind].format(boot) + "\n")
