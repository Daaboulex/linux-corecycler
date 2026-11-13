from __future__ import annotations

import json
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


@pytest.mark.parametrize(
    ("message", "flags", "shown"),
    [("x" * 4087, [], "x" * 4087), ("x" * 4088, [], None), ("x" * 4088, ["--all"], "x" * 4088)],
    ids=["one-byte-under-with-its-name", "at-it-with-its-name", "at-it-with-all"],
)
def test_the_fake_journal_nulls_a_field_whose_name_and_value_pass_4096_bytes_unless_asked_for_all(
    machine, message, flags, shown
):
    machine.daemon_now(message, "systemd-coredump")
    result = machine.journalctl(["-b", machine.boot_id, "-n", "1", "-o", "json", *flags], 1)
    assert json.loads(result.stdout)["MESSAGE"] == shown
