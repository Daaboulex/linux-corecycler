from __future__ import annotations

import bisect
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from subprocess import CompletedProcess
from typing import TYPE_CHECKING

from corecycler.engine import detector

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    import pytest

    from corecycler.history.db import HistoryDB

REBOOT_S = 60
UPTIME_S = 3600
USERSPACE_S = 2
HOST = "fake-host"
JOURNAL_DIR = "/var/log/journal"
BOOT_LINE = "Linux version 7.2.9"
JOURNAL_STARTED = "Journal started"
JOURNAL_STOPPED = "Journal stopped"
JOURNALD = "systemd-journald"
DAEMON_FACILITY = 3
JSON_FIELD_MAX = 4096
_BOOT_ID = re.compile(r"[0-9a-f]{32}")
_BOOT_OFFSET = re.compile(r"[+-]?\d+")
_MATCHED_FIELDS = frozenset({"_TRANSPORT", "SYSLOG_FACILITY"})


@dataclass(frozen=True, slots=True)
class Entry:
    mono_us: int
    realtime_us: int
    message: str
    identifier: str
    facility: int

    @property
    def transport(self) -> str:
        return "kernel" if self.identifier == "kernel" else "driver"

    def field(self, name: str) -> str:
        return self.transport if name == "_TRANSPORT" else str(self.facility)


@dataclass(slots=True)
class Boot:
    id: str
    entries: list[Entry] = field(default_factory=list)


class _Refused(Exception):
    pass


def core_bank_record(cpu: int, *, corrected: bool = True) -> list[str]:
    if corrected:
        return [
            "[Hardware Error]: Corrected error, no action required.",
            f"[Hardware Error]: CPU:{cpu} (1a:44:0) MC0_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: "
            "0xdc204000000d0175",
            "[Hardware Error]: IPID: 0x000000b000000000, Syndrome: 0x000000021a17010a",
            "[Hardware Error]: Load Store Unit Ext. Error Code: 13",
            "[Hardware Error]: cache level: L1, tx: DATA, mem-tx: EV",
        ]
    return [
        "[Hardware Error]: System Fatal error.",
        f"[Hardware Error]: CPU:{cpu} (1a:44:0) MC1_STATUS[Over|UE|MiscV|AddrV|PCC|-|SyndV|-|-|-|-]: "
        "0xbc00000000010135",
        "[Hardware Error]: IPID: 0x000100b000000000",
        "[Hardware Error]: Instruction Fetch Unit Ext. Error Code: 5",
    ]


def this_boot() -> str:
    return uuid.UUID(detector.BOOT_ID_PATH.read_text().strip()).hex


def journaled(db: HistoryDB, session_id: int, core_id: int, value: int, *, survived: bool = False) -> int:
    return db.journal_co_intent(
        session_id, core_id, value, survived, boot_id=this_boot(), mono_ns=detector.monotonic_ns()
    )


def _short_unix(entries: Iterable[Entry]) -> str:
    return "".join(
        f"{entry.realtime_us // 1_000_000}.{entry.realtime_us % 1_000_000:06d} {HOST} kernel: {entry.message}\n"
        for entry in entries
    )


def _json_lines(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


def _matching(entries: list[Entry], matches: list[str]) -> list[Entry]:
    wanted: dict[str, set[str]] = {}
    for match in matches:
        name, _, value = match.partition("=")
        if name not in _MATCHED_FIELDS:
            raise AssertionError(f"the fake journal matches no field in {match!r}")
        wanted.setdefault(name, set()).add(value)
    return [entry for entry in entries if all(entry.field(name) in values for name, values in wanted.items())]


def _message_row(boot: Boot, entry: Entry, whole: bool) -> dict:
    return {
        "__REALTIME_TIMESTAMP": str(entry.realtime_us),
        "__MONOTONIC_TIMESTAMP": str(entry.mono_us),
        "_BOOT_ID": boot.id,
        "MESSAGE": entry.message if whole or len(b"MESSAGE=") + len(entry.message.encode()) < JSON_FIELD_MAX else None,
    }


class FakeMachine:
    def __init__(self, home: Path) -> None:
        self.kmsg_path = home / "kmsg"
        self.boot_id_path = home / "boot_id"
        self.kmsg_path.write_bytes(b"")
        self.boots: list[Boot] = []
        self.journal_readable = True
        self.served: list[list[str]] = []
        self._start(time.time_ns() // 1000 - UPTIME_S * 1_000_000)
        self.mono_ns = UPTIME_S * 1_000_000_000

    @property
    def boot(self) -> Boot:
        return self.boots[-1]

    @property
    def boot_id(self) -> str:
        return self.boot.id

    def reboot(self, *, clean: bool, rtc_offset_s: int = 0) -> None:
        if clean:
            self._log(JOURNAL_STOPPED, self.now_ns(), None, JOURNALD, DAEMON_FACILITY)
        down_us = max(self._realtime_us(self.mono_ns), time.time_ns() // 1000)
        self._start(down_us + (REBOOT_S + rtc_offset_s) * 1_000_000)

    def now_ns(self) -> int:
        wall_ns = (time.time_ns() // 1000 - self._btime_us) * 1000 - self.gap_ns
        return max(self.mono_ns, wall_ns, self.boot.entries[-1].mono_us * 1000)

    def kernel(self, message: str, *, mono_s: float, realtime: float | None = None, facility: int = 0) -> None:
        mono_ns = round(mono_s * 1_000_000_000)
        self.mono_ns = max(self.mono_ns, mono_ns)
        self._log(message, mono_ns, realtime, "kernel", facility)

    def kernel_now(self, *records: list[str], facility: int = 0) -> None:
        start_s = self.now_ns() / 1_000_000_000
        for offset_s, line in enumerate((line for record in records for line in record), start=1):
            self.kernel(line, mono_s=start_s + offset_s, facility=facility)

    def daemon_now(self, message: str, identifier: str) -> None:
        self._log(message, self.now_ns(), None, identifier, DAEMON_FACILITY)

    def crash_after(self, *records: list[str]) -> None:
        self.kernel_now(*records)
        self.reboot(clean=False)

    def sleep(self, seconds: float) -> None:
        self.gap_ns += round(seconds * 1_000_000_000)

    def advance(self, seconds: float) -> None:
        self.mono_ns += round(seconds * 1_000_000_000)

    def forget_boot(self, boot_id: str) -> None:
        if boot_id == self.boot_id:
            raise AssertionError("the running boot's journal is never rotated away")
        held = [boot for boot in self.boots if boot.id == boot_id]
        if not held:
            raise AssertionError(f"the journal holds no boot {boot_id}")
        self.boots.remove(held[0])

    def clock_ns(self, clock: int) -> int:
        if clock == time.CLOCK_MONOTONIC:
            return self.mono_ns
        if clock == time.CLOCK_BOOTTIME:
            return self.mono_ns + self.gap_ns
        raise AssertionError(f"the fake machine keeps no clock {clock}")

    def journalctl(self, args: list[str], timeout: float) -> CompletedProcess[str]:
        argv = ["journalctl", "-q", "--no-pager", *args]
        self.served.append(list(args))
        try:
            served = self._serve(args)
        except _Refused as refusal:
            return CompletedProcess(argv, 1, "", f"{refusal}\n")
        return CompletedProcess(argv, 0, served, "")

    def _serve(self, args: list[str]) -> str:
        if not self.journal_readable:
            raise _Refused(f"Failed to open {JOURNAL_DIR}: Permission denied")
        whole = "--all" in args
        match [arg for arg in args if arg != "--all"]:
            case ["-b", boot_id, "-n", "1", "-o", "json"]:
                boot = self._held(boot_id)
                return _json_lines(
                    {
                        **_message_row(boot, entry, whole),
                        "_TRANSPORT": entry.transport,
                        "SYSLOG_IDENTIFIER": entry.identifier,
                        "SYSLOG_FACILITY": str(entry.facility),
                    }
                    for entry in boot.entries[-1:]
                )
            case ["-b", boot_id, "-o", "json", "--output-fields=MESSAGE", *matches]:
                boot = self._held(boot_id)
                return _json_lines(_message_row(boot, entry, whole) for entry in _matching(boot.entries, matches))
            case ["-b", boot_id, "-n", "+1", "-o", "json", "--output-fields=MESSAGE", *matches]:
                boot = self._held(boot_id)
                return _json_lines(_message_row(boot, entry, whole) for entry in _matching(boot.entries, matches)[:1])
            case ["--list-boots", "-o", "json"]:
                last = len(self.boots) - 1
                listed = [
                    {
                        "index": index - last,
                        "boot_id": boot.id,
                        "first_entry": boot.entries[0].realtime_us,
                        "last_entry": boot.entries[-1].realtime_us,
                    }
                    for index, boot in enumerate(self.boots)
                ]
                return json.dumps(listed, separators=(",", ":")) + "\n"
            case ["-o", "short-unix", "-b", "0", "-n", "1", "_TRANSPORT=kernel"]:
                return _short_unix(_matching(self.boot.entries, ["_TRANSPORT=kernel"])[-1:])
        raise AssertionError(f"the fake journal serves no form {args}")

    def _held(self, boot_id: str) -> Boot:
        if _BOOT_ID.fullmatch(boot_id) is None:
            if _BOOT_OFFSET.fullmatch(boot_id) is not None:
                raise AssertionError(f"the fake journal serves boot ids here, not the offset {boot_id}")
            raise _Refused(f"Failed to add match '{boot_id}': Invalid argument")
        for boot in self.boots:
            if boot.id == boot_id:
                return boot
        raise _Refused(f"No journal boot entry found for the specified boot ({boot_id}+0).")

    def _start(self, btime_us: int) -> None:
        self.boots.append(Boot(uuid.uuid4().hex))
        self.mono_ns = 0
        self.gap_ns = 0
        self._btime_us = btime_us
        self._log(BOOT_LINE, 0, None, "kernel", 0)
        self._log(JOURNAL_STARTED, USERSPACE_S * 1_000_000_000, None, JOURNALD, DAEMON_FACILITY)
        self.boot_id_path.write_text(f"{uuid.UUID(self.boot_id)}\n")

    def _realtime_us(self, mono_ns: int) -> int:
        return self._btime_us + (mono_ns + self.gap_ns) // 1000

    def _log(self, message: str, mono_ns: int, realtime: float | None, identifier: str, facility: int) -> None:
        realtime_us = self._realtime_us(mono_ns) if realtime is None else round(realtime * 1_000_000)
        entry = Entry(
            mono_us=mono_ns // 1000, realtime_us=realtime_us, message=message, identifier=identifier, facility=facility
        )
        bisect.insort(self.boot.entries, entry, key=lambda held: held.mono_us)


def installed(home: Path, monkeypatch: pytest.MonkeyPatch) -> FakeMachine:
    machine = FakeMachine(home)
    monkeypatch.setattr(detector, "KMSG", machine.kmsg_path)
    monkeypatch.setattr(detector, "BOOT_ID_PATH", machine.boot_id_path)
    monkeypatch.setattr(detector, "_journalctl", machine.journalctl)
    monkeypatch.setattr(detector, "_clock_ns", machine.clock_ns)
    return machine
