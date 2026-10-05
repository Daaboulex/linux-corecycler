"""Kernel hardware-error records, read whole from /dev/kmsg and from the systemd journal."""

from __future__ import annotations

import io
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.config import tools

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger(__name__)

KMSG = Path("/dev/kmsg")
KMSG_READ_SIZE = 8192
KMSG_MAX_READS = 4096
KERNEL_FACILITY = 0
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
_JOURNAL_LINE = re.compile(r"\s*(\d+(?:\.\d+)?)\s+\S+\s+kernel:\s?(.*)")
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


def harvest_kernel_mce(since_utc_iso: str, timeout: float = 15.0) -> tuple[list[KernelEvent], bool]:
    since = _iso_to_journal_since(since_utc_iso)
    if since is None:
        return [], False
    try:
        result = subprocess.run(
            [
                tools.command_name("journalctl"),
                "-q",
                "--no-pager",
                "-o",
                "short-unix",
                "--since",
                since,
                "_TRANSPORT=kernel",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError) as exc:
        log.warning("kernel-journal harvest failed: %s", exc)
        return [], False
    if result.returncode != 0:
        log.warning(
            "journalctl exited %d during harvest: %s",
            result.returncode,
            result.stderr.strip()[:200],
        )
        return [], False
    lines = [
        (float(line.group(1)), line.group(2))
        for line in map(_JOURNAL_LINE.fullmatch, result.stdout.splitlines())
        if line is not None
    ]
    events, _open = parse_kernel_records(lines, final=True)
    return events, True


def kernel_log_problem(kmsg: Path | None = None, timeout: float = 15.0) -> str | None:
    path = KMSG if kmsg is None else kmsg
    try:
        os.close(os.open(path, os.O_RDONLY | os.O_NONBLOCK))
    except OSError as exc:
        return f"the kernel log {path} cannot be read: {exc}"
    try:
        result = subprocess.run(
            [
                tools.command_name("journalctl"),
                "-q",
                "--no-pager",
                "-o",
                "short-unix",
                "-b",
                "0",
                "-n",
                "1",
                "_TRANSPORT=kernel",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"the kernel journal cannot be read: {exc}"
    if result.returncode != 0:
        reason = result.stderr.strip()[:200]
        return f"the kernel journal cannot be read: journalctl exited {result.returncode}: {reason}"
    if not result.stdout.strip():
        return "the kernel journal shows no kernel record for this boot, so it cannot be read"
    return None


def last_boot_ended_cleanly(timeout: float = 15.0) -> bool:
    try:
        result = subprocess.run(
            [
                tools.command_name("journalctl"),
                "-q",
                "--no-pager",
                "-b",
                "-1",
                "-n",
                "25",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError) as exc:
        log.warning("clean-shutdown probe failed: %s", exc)
        return False
    if result.returncode != 0:
        return False
    return "Journal stopped" in result.stdout or "Shutting down." in result.stdout


def _iso_to_journal_since(iso_ts: str) -> str | None:
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
