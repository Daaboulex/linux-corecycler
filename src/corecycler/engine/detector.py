"""Error detection — MCE (Machine Check Exceptions) and kernel crash lines.

The kernel log is the single source of truth. The legacy sysfs
/sys/devices/system/machinecheck/*/bank* files are MCA control registers
(constant enable masks), not error counters — they never change when an
error is logged, so counting them cannot detect anything.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from corecycler.config import tools

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MCEEvent:
    timestamp: float
    cpu: int
    bank: int
    message: str
    corrected: bool
    raw_ts: float = 0.0  # kernel/journal timestamp of the source line


@dataclass(slots=True)
class ErrorState:
    mce_events: list[MCEEvent] = field(default_factory=list)
    computation_errors: list[str] = field(default_factory=list)
    last_check_time: float = 0.0

    @property
    def has_errors(self) -> bool:
        return bool(self.mce_events or self.computation_errors)


# AMD Zen decoded MCA block: exactly one line carries CPU, bank and severity
# flags together — "[Hardware Error]: CPU:9 (1a:44:0) MC0_STATUS[Over|CE|...]".
# Only that line produces an event; the block's header ("Machine check events
# logged") and detail lines (Corrected error/Error Addr/IPID/Syndrome/unit)
# describe the same event and must not double-count it.
_AMD_STATUS_RE = re.compile(r"cpu[:\s]+(\d+).*?\bmc(\d+)_status\[([^\]]*)\]", re.IGNORECASE)
_CPU_RE = re.compile(r"\bcpu[:\s]+(\d+)", re.IGNORECASE)
_BANK_RE = re.compile(r"\bbank[:\s]+(\d+)", re.IGNORECASE)


def classify_mce_line(body: str) -> MCEEvent | None:
    """Classify one kernel message body (no timestamp prefix) into an event.

    Returns None for non-error lines, informational MCE lines, and the
    continuation lines of an AMD decoded block.
    """
    lower = body.lower()

    m = _AMD_STATUS_RE.search(lower)
    if m:
        flags = {t.strip() for t in m.group(3).split("|")}
        corrected = "ce" in flags and "ue" not in flags
        return MCEEvent(
            timestamp=time.time(),
            cpu=int(m.group(1)),
            bank=int(m.group(2)),
            message=body.strip(),
            corrected=corrected,
        )

    if _is_mce_error_line(lower):
        cpu_m = _CPU_RE.search(lower)
        bank_m = _BANK_RE.search(lower)
        corrected = bool(re.search(r"\bcorrected\b", lower)) and "uncorrect" not in lower
        return MCEEvent(
            timestamp=time.time(),
            cpu=int(cpu_m.group(1)) if cpu_m else -1,
            bank=int(bank_m.group(1)) if bank_m else -1,
            message=body.strip(),
            corrected=corrected,
        )

    if _is_kernel_error_line(lower):
        return MCEEvent(
            timestamp=time.time(),
            cpu=-1,
            bank=-1,
            message=body.strip(),
            corrected=False,
        )

    return None


class ErrorDetector:
    """Watches the kernel log for hardware errors during stress tests.

    ``check_mce()`` returns every NEW event since ``reset()`` exactly once
    (consume semantics) so callers can both fail the current test on the
    tested core's events and record other cores' events as evidence without
    re-processing duplicates on each poll.
    """

    # Minimum interval between dmesg subprocess calls (seconds).
    DMESG_MIN_INTERVAL: float = 5.0

    def __init__(self) -> None:
        self._dmesg_baseline_ts: float = 0.0  # raw monotonic timestamp
        self._last_dmesg_time: float = 0.0
        self._seen: set[tuple[float, int, int]] = set()

    def reset(self) -> None:
        """Start a new observation window — only lines newer than now count."""
        self._dmesg_baseline_ts = _get_dmesg_raw_timestamp()
        self._last_dmesg_time = 0.0
        self._seen.clear()

    def check_mce(self) -> list[MCEEvent]:
        """Return new MCE/kernel-error events since reset(), each exactly once.

        Rate-limited: within DMESG_MIN_INTERVAL of the previous subprocess
        call it returns [] instead of re-running dmesg; the event is delivered
        on the first poll after the interval elapses.
        """
        now = time.monotonic()
        if now - self._last_dmesg_time < self.DMESG_MIN_INTERVAL:
            return []
        self._last_dmesg_time = now

        # No baseline means old and new lines are indistinguishable — treating
        # boot-time history as fresh errors would fail every first test.
        if self._dmesg_baseline_ts <= 0:
            return []

        events: list[MCEEvent] = []
        try:
            # No --level filter: AMD decoded corrected-error lines are logged
            # below err/warn and a level filter silently hides them; the line
            # classifier is the filter.
            result = subprocess.run(
                [tools.command_name("dmesg"), "--time-format=raw"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return []
            for line in result.stdout.splitlines():
                ts_match = re.match(r"\s*\[?\s*([\d.]+)\]?\s?", line)
                if not ts_match:
                    continue
                try:
                    raw_ts = float(ts_match.group(1))
                except ValueError:
                    continue
                if raw_ts <= self._dmesg_baseline_ts:
                    continue
                event = classify_mce_line(line[ts_match.end() :])
                if event is None:
                    continue
                event.raw_ts = raw_ts
                key = (raw_ts, event.cpu, event.bank)
                if key in self._seen:
                    continue
                self._seen.add(key)
                events.append(event)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError) as exc:
            log.debug("dmesg check failed: %s", exc)
            return events
        log.debug(
            "dmesg poll: %d new event(s)%s",
            len(events),
            "".join(f" [cpu={e.cpu} bank={e.bank} ce={e.corrected}]" for e in events),
        )
        return events


def harvest_kernel_mce(since_utc_iso: str, timeout: float = 15.0) -> tuple[list[MCEEvent], bool]:
    """Read MCE/kernel-error events from the systemd journal since a UTC ISO
    timestamp — across reboots, so it covers the boot(s) a hard crash killed.

    Returns (events, harvest_ok). harvest_ok False means the journal could
    not be read at all (journalctl missing, unreadable, bad timestamp) — the
    caller must treat the crash as unattributed, never as a clean bill.
    """
    since = _iso_to_journal_since(since_utc_iso)
    if since is None:
        return [], False
    try:
        # _TRANSPORT=kernel, NOT -k: -k implies --boot (current boot only),
        # which silently hides the crashed boot's MCE lines — the entire
        # point of this harvest.
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

    events: list[MCEEvent] = []
    seen: set[tuple[float, int, int]] = set()
    for line in result.stdout.splitlines():
        # short-unix format: "1626382113.123456 host kernel: <message>"
        m = re.match(r"\s*([\d.]+)\s+\S+\s+kernel:\s?(.*)$", line)
        if not m:
            continue
        event = classify_mce_line(m.group(2))
        if event is None:
            continue
        try:
            event.raw_ts = float(m.group(1))
        except ValueError:
            event.raw_ts = 0.0
        key = (event.raw_ts, event.cpu, event.bank)
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return events, True


def last_boot_ended_cleanly(timeout: float = 15.0) -> bool:
    """True when the previous boot ended in an orderly shutdown.

    Journald writes a final "Journal stopped" record on every clean shutdown;
    a freeze or hard reset leaves the boot's journal without one. Fail closed:
    any read problem returns False — an unproven shutdown is treated as dirty.
    """
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
    """Convert an ISO-8601 timestamp to systemd's --since form, in UTC."""
    try:
        dt = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _is_mce_error_line(line_lower: str) -> bool:
    """Return True if a dmesg line is an actual MCE error, not informational.

    Excludes boot/info messages like:
    - "mce: CPU supports N MCE banks"
    - "Machine check events logged" (the AMD block header — the event itself
      is counted from the MCx_STATUS line of the same block)
    - "mce_cpu_quirks"
    - "mce: [Hardware Error]:" informational lines about MCE configuration
    """
    # Must mention MCE or machine check at all
    if "mce" not in line_lower and "machine check" not in line_lower:
        return False

    # Exclude known informational patterns
    info_patterns = [
        "cpu supports",
        "mce banks",
        "events logged",
        "mce_cpu_quirks",
        "mce: using",
        "mce: cmci",
        "mce_intel",
        "check polling timer",
    ]
    for pat in info_patterns:
        if pat in line_lower:
            return False

    # Real MCE error indicators
    error_indicators = [
        "hardware error",
        "bank ",  # "Bank N:" with actual bank data
        "machine check exception",
        "fatal machine check",
        "uncorrected",
        "corrected error",
        "mca:",
        "status:",
        "tsc:",
        "addr:",
        "misc:",
        "severity:",
    ]
    return any(ind in line_lower for ind in error_indicators)


def _is_kernel_error_line(line_lower: str) -> bool:
    """Return True if a dmesg line indicates a kernel crash, oops, or BUG.

    CO undervolting can cause kernel-level faults that manifest as oops or
    BUG traps rather than MCE events — especially under heavy instruction
    pressure with AVX/SSE workloads.
    """
    indicators = [
        "kernel panic",
        "oops:",
        "bug:",
        "bug at ",
        "general protection fault",
        "invalid opcode",
        "rip:",
        "call trace:",
        "kernel bug at",
    ]
    return any(ind in line_lower for ind in indicators)


def _get_dmesg_raw_timestamp() -> float:
    """Get the latest dmesg raw monotonic timestamp for baseline filtering."""
    with contextlib.suppress(subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError):
        result = subprocess.run(
            [tools.command_name("dmesg"), "--time-format=raw"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.strip().splitlines()
        if lines:
            ts_str = lines[-1].split()[0] if lines[-1] else ""
            try:
                return float(ts_str.strip("[]"))
            except ValueError:
                return 0.0
    return 0.0
