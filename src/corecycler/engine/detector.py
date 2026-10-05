"""Kernel hardware-error records, read whole from /dev/kmsg and from the systemd journal."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.config import tools

if TYPE_CHECKING:
    from collections.abc import Iterable

KMSG = Path("/dev/kmsg")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
KMSG_READ_SIZE = 8192
KMSG_MAX_READS = 4096
KERNEL_FACILITY = 0
JOURNALD = "systemd-journald"
JOURNAL_STOPPED = "Journal stopped"
STATUS_UC = 1 << 61
STATUS_DEFERRED = 1 << 44
CORE_BANK_TYPES: frozenset[tuple[int, int]] = frozenset(
    {(0xB0, 0x0), (0xB0, 0x10), (0xB0, 0x1), (0xB0, 0x2), (0xB0, 0x3), (0xB0, 0x5), (0xB0, 0x6)}
)
CORE_BANK_NAMES: frozenset[str] = frozenset(
    {"Load Store Unit", "Instruction Fetch Unit", "L2 Cache", "Decode Unit", "Execution Unit", "Floating Point Unit"}
)
_SEVERITIES: frozenset[str] = frozenset(
    {
        "System Fatal error.",
        "Uncorrected, software restartable error.",
        "Uncorrected, software containable error.",
        "Deferred error, no action required.",
        "Corrected error, no action required.",
    }
)
_HARDWARE_ERROR = re.compile(r"(?:mce: )?\[Hardware Error\]: (.*)")
_DECODER = re.compile(r"CPU:(\d+) \([0-9a-f]+:[0-9a-f]+:[0-9a-f]+\) MC(\d+)_STATUS\[[^\]]*\]: 0x([0-9a-f]{16})")
_FALLBACK = re.compile(r"CPU (\d+): Machine Check(?: Exception)?: [0-9a-f]+ Bank (\d+): ([0-9a-f]{16})")
_OPENERS_AND_LAST_LINES: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (_DECODER, ("cache level: ", "internal: ")),
    (_FALLBACK, ("PROCESSOR ",)),
)
_DECODER_IPID = re.compile(r"IPID: 0x([0-9a-f]{16})")
_FALLBACK_IPID = re.compile(r"TSC [0-9a-f]+ .*?\bIPID ([0-9a-f]+)")
_BANK_NAME = re.compile(r"(.+) Ext\. Error Code: ")
_TROUBLE = re.compile(
    r"(?:NMI )?(?:watchdog: )?BUG: soft lockup - CPU#\d+ stuck"
    r"|(?:NMI )?(?:watchdog: )?(?:CPU\d+: )?Watchdog detected hard LOCKUP on cpu \d+"
    r"|Kernel panic - not syncing"
    r"|Oops: "
    r"|kernel BUG at "
)
_KMSG_RECORD = re.compile(r"(\d+),\d+,(\d+),[^;]*;(.*)")
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_KERNEL_RECORDS = ("-o", "json", "--output-fields=MESSAGE", "_TRANSPORT=kernel", f"SYSLOG_FACILITY={KERNEL_FACILITY}")
_USERSPACE = ("_TRANSPORT=driver", "_TRANSPORT=journal", "_TRANSPORT=stdout", "_TRANSPORT=syslog")
_NOT_OPEN = "kernel log not open: read before reset"
_FLOOD = "kernel log flood: records left unread"


@dataclass(frozen=True, slots=True)
class CoreMachineCheck:
    cpu: int
    bank: int
    corrected: bool
    message: str
    raw_ts: float


@dataclass(frozen=True, slots=True)
class UnblamedMachineCheck:
    logged_by_cpu: int
    bank: int
    corrected: bool
    message: str
    raw_ts: float


@dataclass(frozen=True, slots=True)
class KernelTrouble:
    message: str
    raw_ts: float


KernelEvent = CoreMachineCheck | UnblamedMachineCheck | KernelTrouble


@dataclass(frozen=True, slots=True)
class KernelRead:
    events: tuple[KernelEvent, ...]
    problem: str | None


@dataclass(frozen=True, slots=True)
class BootEnd:
    clean: bool
    problem: str | None


@dataclass(frozen=True, slots=True)
class _JournalEntry:
    mono_us: int
    realtime_us: int
    message: str
    identifier: str | None


def bank_is_core(ipid: int | None, bank_name: str | None) -> bool:
    if ipid is not None:
        high = ipid >> 32
        return (high & 0xFFF, (high >> 16) & 0xFFFF) in CORE_BANK_TYPES
    return bank_name in CORE_BANK_NAMES


@dataclass(slots=True)
class _OpenRecord:
    cpu: int
    bank: int
    status: int
    message: str
    raw_ts: float
    lines: list[tuple[float, str]]
    last_line_starts: tuple[str, ...]
    ipid: int | None = None
    bank_name: str | None = None

    def attach(self, raw_ts: float, text: str, body: str) -> None:
        self.lines.append((raw_ts, text))
        if (ipid := _DECODER_IPID.match(body) or _FALLBACK_IPID.match(body)) is not None:
            self.ipid = int(ipid.group(1), 16)
        elif (name := _BANK_NAME.match(body)) is not None:
            self.bank_name = name.group(1)

    def event(self) -> CoreMachineCheck | UnblamedMachineCheck:
        corrected = not self.status & (STATUS_UC | STATUS_DEFERRED)
        if bank_is_core(self.ipid, self.bank_name):
            return CoreMachineCheck(
                cpu=self.cpu, bank=self.bank, corrected=corrected, message=self.message, raw_ts=self.raw_ts
            )
        return UnblamedMachineCheck(
            logged_by_cpu=self.cpu, bank=self.bank, corrected=corrected, message=self.message, raw_ts=self.raw_ts
        )


def _open_record(raw_ts: float, text: str, body: str) -> _OpenRecord | None:
    for opener, last_line_starts in _OPENERS_AND_LAST_LINES:
        if (opened := opener.match(body)) is not None:
            cpu, bank, status = opened.groups()
            return _OpenRecord(
                cpu=int(cpu),
                bank=int(bank),
                status=int(status, 16),
                message=text.strip(),
                raw_ts=raw_ts,
                lines=[(raw_ts, text)],
                last_line_starts=last_line_starts,
            )
    return None


def parse_kernel_records(
    lines: Iterable[tuple[float, str]], *, final: bool
) -> tuple[list[KernelEvent], list[tuple[float, str]]]:
    events: list[KernelEvent] = []
    record: _OpenRecord | None = None
    for raw_ts, text in lines:
        trouble = _TROUBLE.match(text) is not None
        hardware = _HARDWARE_ERROR.match(text)
        body = "" if hardware is None else hardware.group(1)
        opened = None if hardware is None else _open_record(raw_ts, text, body)
        if record is not None and (trouble or opened is not None or body in _SEVERITIES):
            events.append(record.event())
            record = None
        if trouble:
            events.append(KernelTrouble(message=text.strip(), raw_ts=raw_ts))
        elif opened is not None:
            record = opened
        elif record is not None and hardware is not None:
            record.attach(raw_ts, text, body)
            if body.startswith(record.last_line_starts):
                events.append(record.event())
                record = None
    if record is not None and final:
        events.append(record.event())
    return events, ([] if record is None or final else record.lines)


def _kmsg_lines(chunk: bytes) -> tuple[list[tuple[float, str]], list[str]]:
    lines: list[tuple[float, str]] = []
    malformed: list[str] = []
    for raw in chunk.decode("utf-8", errors="replace").split("\n"):
        if not raw or raw.startswith(" "):
            continue
        record = _KMSG_RECORD.fullmatch(raw)
        if record is None:
            malformed.append(raw)
        elif int(record.group(1)) >> 3 == KERNEL_FACILITY:
            lines.append((int(record.group(2)) / 1e6, record.group(3)))
    return lines, malformed


class ErrorDetector:
    def __init__(self, kmsg: Path | None = None) -> None:
        self._path = KMSG if kmsg is None else kmsg
        self._file: io.FileIO | None = None
        self._unopened = _NOT_OPEN
        self._carry: list[tuple[float, str]] = []

    def reset(self) -> None:
        self.close()
        try:
            self._file = io.FileIO(self._path, "r", opener=lambda path, flags: os.open(path, flags | os.O_NONBLOCK))
            self._file.seek(0, os.SEEK_END)
        except OSError as exc:
            self.close()
            self._unopened = f"kernel log {self._path} unreadable: {exc}"

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._carry = []
        self._unopened = _NOT_OPEN

    def read_new(self, *, final: bool = False) -> KernelRead:
        if self._file is None:
            return KernelRead((), self._unopened)
        lines = list(self._carry)
        problems: list[str] = []
        overran = False
        for _ in range(KMSG_MAX_READS):
            try:
                chunk = self._file.read(KMSG_READ_SIZE)
            except BrokenPipeError:
                problems.append("kernel log overran; records lost")
                if overran:
                    break
                overran = True
                continue
            except OSError as exc:
                problems.append(f"kernel log {self._path} read failed: {exc}")
                break
            if not chunk:
                break
            overran = False
            found, malformed = _kmsg_lines(chunk)
            lines.extend(found)
            problems.extend(f"kernel log {self._path} record malformed: {raw[:160]!r}" for raw in malformed)
        else:
            if final:
                problems.append(_FLOOD)
        events, self._carry = parse_kernel_records(lines, final=final)
        return KernelRead(tuple(events), problems[0] if problems else None)


def _clock_ns(clock: int) -> int:
    return time.clock_gettime_ns(clock)


def monotonic_ns() -> int:
    return _clock_ns(time.CLOCK_MONOTONIC)


def _journalctl(args: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [tools.command_name("journalctl"), "-q", "--no-pager", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def read_boot_id() -> str:
    raw = BOOT_ID_PATH.read_text().strip()
    if _BOOT_ID.fullmatch(raw) is None:
        raise ValueError(f"{BOOT_ID_PATH} holds {raw[:60]!r}, not a boot id")
    return raw.replace("-", "")


def _journal_text(args: list[str], timeout: float) -> tuple[str, str | None]:
    command = " ".join(["journalctl", *args])
    try:
        result = _journalctl(args, timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "", f"{command} failed: {exc}"
    if result.returncode != 0:
        return "", f"{command} exited {result.returncode}: {result.stderr.strip()[:200]}"
    return result.stdout, None


def _message(value: object) -> str:
    if isinstance(value, list):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    raise TypeError(f"MESSAGE is a {type(value).__name__}")


def _journal_entries(args: list[str], timeout: float) -> tuple[list[_JournalEntry], str | None]:
    text, problem = _journal_text(args, timeout)
    if problem is not None:
        return [], problem
    try:
        return [
            _JournalEntry(
                mono_us=int(entry["__MONOTONIC_TIMESTAMP"]),
                realtime_us=int(entry["__REALTIME_TIMESTAMP"]),
                message=_message(entry["MESSAGE"]),
                identifier=entry.get("SYSLOG_IDENTIFIER"),
            )
            for entry in map(json.loads, filter(None, text.splitlines()))
        ], None
    except (KeyError, TypeError, ValueError) as exc:
        return [], f"journalctl {' '.join(args)} printed an unreadable entry: {exc!r}"


def boot_end(boot_id: str, timeout: float = 15.0) -> BootEnd:
    if not boot_id:
        return BootEnd(clean=False, problem="the session's boot is not recorded")
    last, problem = _journal_entries(["-b", boot_id, "-n", "1", "-o", "json"], timeout)
    clean = len(last) == 1 and (last[0].identifier, last[0].message) == (JOURNALD, JOURNAL_STOPPED)
    return BootEnd(clean=clean, problem=problem)


def _next_boot(boot_id: str, timeout: float) -> tuple[str | None, str | None]:
    text, problem = _journal_text(["--list-boots", "-o", "json"], timeout)
    if problem is not None:
        return None, problem
    try:
        listed = [boot["boot_id"] for boot in sorted(json.loads(text), key=lambda boot: boot["index"])]
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"journalctl --list-boots printed an unreadable list: {exc!r}"
    if boot_id not in listed:
        return None, f"boot {boot_id} is not in the journal's boot list, so the boot after it cannot be named"
    later = listed[listed.index(boot_id) + 1 :]
    return (later[0] if later else None), None


def _before_userspace(boot_id: str, timeout: float) -> tuple[list[_JournalEntry], str | None]:
    first, problem = _journal_entries(
        ["-b", boot_id, "-n", "+1", "-o", "json", "--output-fields=MESSAGE", *_USERSPACE], timeout
    )
    if problem is not None:
        return [], problem
    records, problem = _journal_entries(["-b", boot_id, *_KERNEL_RECORDS], timeout)
    return [record for record in records if not first or record.mono_us < first[0].mono_us], problem


def _events(entries: Iterable[_JournalEntry]) -> list[KernelEvent]:
    events, _open = parse_kernel_records(((entry.realtime_us / 1e6, entry.message) for entry in entries), final=True)
    return events


def harvest_after(
    boot_id: str,
    after_us: int | None,
    *,
    next_boot: bool,
    before_mono_ns: int | None = None,
    timeout: float = 15.0,
) -> KernelRead:
    if not boot_id:
        return KernelRead((), "the session's boot is not recorded")
    events: list[KernelEvent] = []
    problems: list[str | None] = []
    if after_us is None:
        problems.append("the session's last write time is unreadable")
    else:
        dead, problem = _journal_entries(["-b", boot_id, *_KERNEL_RECORDS], timeout)
        events += _events(
            entry
            for entry in dead
            if entry.realtime_us > after_us and (before_mono_ns is None or entry.mono_us * 1000 < before_mono_ns)
        )
        problems.append(problem)
    if next_boot:
        following, problem = _next_boot(boot_id, timeout)
        problems.append(problem)
        if following is not None:
            early, problem = _before_userspace(following, timeout)
            events += _events(early)
            problems.append(problem)
    return KernelRead(tuple(events), "; ".join(filter(None, problems)) or None)


def kernel_log_problem(kmsg: Path | None = None, timeout: float = 15.0) -> str | None:
    path = KMSG if kmsg is None else kmsg
    try:
        os.close(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
    except OSError as exc:
        return f"the kernel log {path} cannot be read: {exc}"
    try:
        result = _journalctl(["-o", "short-unix", "-b", "0", "-n", "1", "_TRANSPORT=kernel"], timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"the kernel journal cannot be read: {exc}"
    if result.returncode != 0:
        reason = result.stderr.strip()[:200]
        return f"the kernel journal cannot be read: journalctl exited {result.returncode}: {reason}"
    if not result.stdout.strip():
        return "the kernel journal shows no kernel record for this boot, so it cannot be read"
    return None
