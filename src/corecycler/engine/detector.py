"""Kernel hardware-error records and user programs' fault lines, read whole from /dev/kmsg and the systemd journal."""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.config import tools

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence

KMSG = Path("/dev/kmsg")
MCE_POLL = Path("/sys/devices/system/machinecheck/machinecheck0/check_interval")
MCE_POLL_MAX_S = 5
MCE_POLL_KERNEL_DEFAULT_S = 300
MCE_TIMER_ROUNDING_S = 1
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
KMSG_READ_SIZE = 8192
KMSG_MAX_READS = 4096
PROGRAM_FAULTS_KEPT = 50  # equivalent-mutant: every test reads this constant rather than restating its value
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
_SEGFAULT = re.compile(r"(.+)\[(\d+)\]: segfault at ")
_TRAP = re.compile(r"traps: (.+)\[(\d+)\] (?:trap |general protection fault)")
_LIKELY_CPU = re.compile(r" likely on CPU (\d+) \(core (\d+), socket \d+\)$")
_KMSG_RECORD = re.compile(r"(\d+),\d+,(\d+),[^;]*;(.*)")
_BOOT_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_STORED_BOOT_ID = re.compile(r"[0-9a-f]{32}")
_JSON_WHOLE = ("-o", "json", "--all")
_KERNEL_RECORDS = (*_JSON_WHOLE, "--output-fields=MESSAGE", "_TRANSPORT=kernel", f"SYSLOG_FACILITY={KERNEL_FACILITY}")
_USERSPACE = ("_TRANSPORT=driver", "_TRANSPORT=journal", "_TRANSPORT=stdout", "_TRANSPORT=syslog")
_NOT_OPEN = "kernel log not open: read before reset"
RESET_REASON = "x86/amd: Previous system reset reason"
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


@dataclass(frozen=True, slots=True)
class ProgramFault:
    process: str
    pid: int
    cpu: int | None
    core: int | None
    message: str
    raw_ts: float

    @property
    def named(self) -> str:
        where = "no CPU named" if self.cpu is None else f"CPU {self.cpu}, core {self.core}"
        return f"{self.process} (pid {self.pid}), {where}"


@dataclass(frozen=True, slots=True)
class ProgramFaults:
    started_ns: int
    kept: tuple[ProgramFault, ...]
    dropped: int


KernelEvent = CoreMachineCheck | UnblamedMachineCheck | KernelTrouble | ProgramFault


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


@dataclass(frozen=True, slots=True)
class HarvestFrom:
    mono_ns: int | None
    realtime_us: int | None

    def precedes(self, entry: _JournalEntry) -> bool:
        if self.mono_ns is not None:
            return entry.mono_us * 1000 > self.mono_ns
        return self.realtime_us is None or entry.realtime_us > self.realtime_us


@dataclass(frozen=True, slots=True)
class JournalHarvest:
    dead_boot: tuple[KernelEvent, ...]
    next_boot: tuple[KernelEvent, ...]
    problem: str | None
    unfinished_from_ns: int | None = None


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


def _program_fault(raw_ts: float, text: str) -> ProgramFault | None:
    found = _SEGFAULT.match(text) or _TRAP.match(text)
    if found is None:
        return None
    process, pid = found.groups()
    likely = _LIKELY_CPU.search(text)
    return ProgramFault(
        process=process,
        pid=int(pid),
        cpu=None if likely is None else int(likely.group(1)),
        core=None if likely is None else int(likely.group(2)),
        message=text.strip(),
        raw_ts=raw_ts,
    )


def kept_faults[T](faults: Sequence[T]) -> tuple[tuple[T, ...], int]:
    kept = tuple(faults[-PROGRAM_FAULTS_KEPT:])
    return kept, len(faults) - len(kept)


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
        elif (fault := _program_fault(raw_ts, text)) is not None:
            events.append(fault)
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
        self.read_started_ns: int | None = None

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
        started_ns = monotonic_ns()
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
        if not problems:
            self.read_started_ns = started_ns
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


def _journal_read[T](args: list[str], timeout: float, read: Callable[[list], T], unread: T) -> tuple[T, str | None]:
    text, problem = _journal_text(args, timeout)
    if problem is not None:
        return unread, problem
    try:
        return read([json.loads(line) for line in filter(None, text.splitlines())]), None
    except (KeyError, TypeError, ValueError) as exc:
        return unread, f"journalctl {' '.join(args)} printed an unreadable entry: {exc!r}"


def _entries(rows: list) -> list[_JournalEntry]:
    return [
        _JournalEntry(
            mono_us=int(entry["__MONOTONIC_TIMESTAMP"]),
            realtime_us=int(entry["__REALTIME_TIMESTAMP"]),
            message=_message(entry["MESSAGE"]),
        )
        for entry in rows
    ]


def _journal_entries(args: list[str], timeout: float) -> tuple[list[_JournalEntry], str | None]:
    return _journal_read(args, timeout, _entries, [])


def _stopped_by_journald(rows: list) -> bool:
    (last,) = rows
    if not isinstance(last, dict):
        raise TypeError(f"an entry is a {type(last).__name__}")
    return last.get("SYSLOG_IDENTIFIER") == JOURNALD and _message(last["MESSAGE"]) == JOURNAL_STOPPED


def _boot_problem(boot_id: str) -> str | None:
    if not boot_id:
        return "the session's boot is not recorded"
    if _STORED_BOOT_ID.fullmatch(boot_id) is None:
        return f"the session's boot id {boot_id!r} is not a boot id"
    return None


def boot_end(boot_id: str, timeout: float = 15.0) -> BootEnd:
    if (problem := _boot_problem(boot_id)) is not None:
        return BootEnd(clean=False, problem=problem)
    clean, problem = _journal_read(["-b", boot_id, "-n", "1", *_JSON_WHOLE], timeout, _stopped_by_journald, False)
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
        ["-b", boot_id, "-n", "+1", *_JSON_WHOLE, "--output-fields=MESSAGE", *_USERSPACE], timeout
    )
    if problem is not None:
        return [], problem
    records, problem = _journal_entries(["-b", boot_id, *_KERNEL_RECORDS], timeout)
    return [record for record in records if not first or record.mono_us < first[0].mono_us], problem


def reset_reasons(boot_id: str, timeout: float = 15.0) -> tuple[tuple[str, ...], str | None]:
    early, problem = _before_userspace(boot_id, timeout)
    return tuple(entry.message for entry in early if entry.message.startswith(RESET_REASON)), problem


def _events(entries: Iterable[_JournalEntry]) -> tuple[KernelEvent, ...]:
    events, _open = parse_kernel_records(((entry.mono_us / 1e6, entry.message) for entry in entries), final=True)
    return tuple(events)


def stamp_ns(raw_ts: float) -> int:
    return round(raw_ts * 1_000_000) * 1000


def harvest_after(
    boot_id: str,
    after: HarvestFrom | None,
    *,
    next_boot: bool,
    through_mono_ns: int | None = None,
    timeout: float = 15.0,
) -> JournalHarvest:
    if (problem := _boot_problem(boot_id)) is not None:
        return JournalHarvest((), (), problem)
    dead_boot: tuple[KernelEvent, ...] = ()
    later: tuple[KernelEvent, ...] = ()
    unfinished: int | None = None
    problems: list[str | None] = []
    if after is None:
        problems.append("the session's last write time is unreadable")
    else:
        dead, problem = _journal_entries(["-b", boot_id, *_KERNEL_RECORDS], timeout)
        events, open_lines = parse_kernel_records(
            ((entry.mono_us / 1e6, entry.message) for entry in dead if after.precedes(entry)),
            final=through_mono_ns is None,
        )
        opener = stamp_ns(open_lines[0][0]) if open_lines else None
        unfinished = opener if opener is not None and opener <= through_mono_ns else None
        dead_boot = tuple(
            event
            for event in events
            if (through_mono_ns is None or stamp_ns(event.raw_ts) <= through_mono_ns)
            and (unfinished is None or stamp_ns(event.raw_ts) < unfinished)
        )
        problems.append(problem)
    if next_boot:
        following, problem = _next_boot(boot_id, timeout)
        problems.append(problem)
        if following is not None:
            early, problem = _before_userspace(following, timeout)
            later = _events(early)
            problems.append(problem)
    return JournalHarvest(dead_boot, later, "; ".join(filter(None, problems)) or None, unfinished)


def mce_poll_problem(path: Path = MCE_POLL) -> str | None:
    command = f"echo {MCE_POLL_MAX_S} | sudo tee {path}, or on NixOS services.corecycler.machineCheckPoll.enable"
    try:
        seconds = int(path.read_text().strip())
    except (OSError, ValueError) as exc:
        return f"machine-check poll interval unreadable: {exc}; set it: {command}"
    if seconds > MCE_POLL_MAX_S:
        return (
            f"machine-check poll every {seconds} s, above {MCE_POLL_MAX_S} s, so corrected errors log late: {command}"
        )
    return None


def mce_settle_s(path: Path = MCE_POLL) -> float:
    try:
        seconds = int(path.read_text().strip())
    except OSError, ValueError:
        seconds = MCE_POLL_MAX_S
    if seconds < 0:
        seconds = MCE_POLL_MAX_S
    return float(min(seconds, MCE_POLL_MAX_S) + MCE_TIMER_ROUNDING_S)


@contextmanager
def fast_mce_poll(say: Callable[[str], None], path: Path = MCE_POLL) -> Iterator[int]:
    try:
        before = int(path.read_text().strip())
    except (OSError, ValueError) as exc:
        say(f"machine-check poll not raised: {path} unreadable: {exc}")
        before = None
    raised = False
    if before is not None and before > MCE_POLL_MAX_S:
        try:
            path.write_text(f"{MCE_POLL_MAX_S}\n")
        except OSError as exc:
            say(f"machine-check poll stays at {before} s: writing {MCE_POLL_MAX_S} to {path} failed: {exc}")
        else:
            raised = True
            say(f"machine-check poll raised from {before} s to {MCE_POLL_MAX_S} s for this run")
    try:
        yield MCE_POLL_KERNEL_DEFAULT_S if before is None else before
    finally:
        if raised:
            try:
                path.write_text(f"{before}\n")
            except OSError as exc:
                say(f"machine-check poll stays at {MCE_POLL_MAX_S} s: writing {before} back to {path} failed: {exc}")
            else:
                say(f"machine-check poll back to {before} s")


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
