"""Error detection — MCE (Machine Check Exceptions), stress output, dmesg."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(slots=True)
class MCEEvent:
    timestamp: float
    cpu: int
    bank: int
    message: str
    corrected: bool


@dataclass(slots=True)
class ErrorState:
    mce_events: list[MCEEvent] = field(default_factory=list)
    computation_errors: list[str] = field(default_factory=list)
    last_check_time: float = 0.0

    @property
    def has_errors(self) -> bool:
        return bool(self.mce_events or self.computation_errors)


class ErrorDetector:
    """Monitors for hardware errors (MCE) and computation errors during stress tests."""

    # Minimum interval between dmesg subprocess calls (seconds).
    DMESG_MIN_INTERVAL: float = 5.0

    def __init__(self) -> None:
        self._mce_baseline: int = 0
        self._mce_bank_baseline: dict[str, int] = {}  # "cpu:bank" -> count
        self._dmesg_baseline_ts: float = 0.0  # raw monotonic timestamp
        self._last_dmesg_time: float = 0.0
        self._last_dmesg_events: list[MCEEvent] = []

    def reset(self) -> None:
        """Reset error tracking — call before starting a new test run."""
        self._mce_baseline = self._count_mce_events()
        self._mce_bank_baseline = self._snapshot_mce_banks()
        self._dmesg_baseline_ts = _get_dmesg_raw_timestamp()
        self._last_dmesg_time = 0.0
        self._last_dmesg_events = []

    def check_mce(self, target_cpu: int | None = None) -> list[MCEEvent]:
        """Check for new MCE events since last reset, optionally filtered by CPU."""
        events: list[MCEEvent] = []

        # method 1: check sysfs machinecheck counters
        events.extend(self._check_sysfs_mce(target_cpu))

        # method 2: check dmesg for MCE messages (rate-limited)
        events.extend(self._check_dmesg_mce(target_cpu))

        return events

    def _check_sysfs_mce(self, target_cpu: int | None) -> list[MCEEvent]:
        """Check /sys/devices/system/machinecheck/ for new events.

        All sysfs reads are wrapped in try/except so a PermissionError
        or transient I/O error never crashes the detector.
        """
        events: list[MCEEvent] = []
        mce_base = Path("/sys/devices/system/machinecheck")

        try:
            if not mce_base.exists():
                return events
        except OSError:
            return events

        try:
            mce_dirs = sorted(mce_base.iterdir())
        except (OSError, PermissionError) as exc:
            log.debug("Cannot list %s: %s", mce_base, exc)
            return events

        for mce_dir in mce_dirs:
            if not mce_dir.name.startswith("machinecheck"):
                continue

            try:
                cpu_num = int(mce_dir.name.removeprefix("machinecheck"))
            except ValueError:
                continue

            if target_cpu is not None and cpu_num != target_cpu:
                continue

            # check bank error counts
            try:
                bank_files = sorted(mce_dir.glob("bank*"))
            except (OSError, PermissionError) as exc:
                log.debug("Cannot list bank files in %s: %s", mce_dir, exc)
                continue

            for bank_file in bank_files:
                try:
                    count = int(bank_file.read_text().strip())
                    match = re.search(r"\d+", bank_file.name)
                    bank_num = int(match.group()) if match else -1
                    # compare against baseline — only report NEW events
                    baseline_key = f"{cpu_num}:{bank_num}"
                    baseline_count = self._mce_bank_baseline.get(baseline_key, 0)
                    new_count = count - baseline_count
                    if new_count > 0:
                        events.append(
                            MCEEvent(
                                timestamp=time.time(),
                                cpu=cpu_num,
                                bank=bank_num,
                                message=(
                                    f"MCE bank {bank_num} error count: {count} "
                                    f"(+{new_count} since test start)"
                                ),
                                corrected=True,  # sysfs only shows corrected
                            )
                        )
                except PermissionError:
                    log.debug("Permission denied reading %s", bank_file)
                    continue
                except (ValueError, OSError, AttributeError):
                    continue

        return events

    def _check_dmesg_mce(self, target_cpu: int | None) -> list[MCEEvent]:
        """Parse dmesg for MCE messages since baseline.

        Rate-limited to at most one subprocess call per ``DMESG_MIN_INTERVAL``
        seconds to avoid spamming ``dmesg`` during tight poll loops.  Between
        calls the previous result set is returned.
        """
        now = time.monotonic()
        if now - self._last_dmesg_time < self.DMESG_MIN_INTERVAL:
            # Return cached results (already filtered for target_cpu at call time,
            # so we need to re-filter if the caller changed target).
            if target_cpu is None:
                return list(self._last_dmesg_events)
            return [e for e in self._last_dmesg_events if e.cpu == target_cpu or e.cpu == -1]

        self._last_dmesg_time = now
        events: list[MCEEvent] = []

        try:
            import subprocess

            result = subprocess.run(
                ["dmesg", "--time-format=raw", "--level=err,warn"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                self._last_dmesg_events = []
                return events

            for line in result.stdout.splitlines():
                if "mce" not in line.lower() and "machine check" not in line.lower():
                    continue

                # filter by baseline timestamp — only report NEW messages
                ts_match = re.match(r"\s*([\d.]+)", line)
                if ts_match and self._dmesg_baseline_ts > 0:
                    try:
                        msg_ts = float(ts_match.group(1))
                        if msg_ts <= self._dmesg_baseline_ts:
                            continue  # pre-existing message, skip
                    except ValueError:
                        pass

                # extract CPU number from MCE message
                cpu_match = re.search(r"CPU (\d+)", line)
                cpu_num = int(cpu_match.group(1)) if cpu_match else -1

                bank_match = re.search(r"Bank (\d+)", line)
                bank_num = int(bank_match.group(1)) if bank_match else -1

                lower = line.lower()
                corrected = (
                    bool(re.search(r"\bcorrected\b", lower))
                    and "uncorrect" not in lower
                )

                events.append(
                    MCEEvent(
                        timestamp=time.time(),
                        cpu=cpu_num,
                        bank=bank_num,
                        message=line.strip(),
                        corrected=corrected,
                    )
                )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError) as exc:
            log.debug("dmesg check failed: %s", exc)

        # Cache the full (unfiltered) result set
        self._last_dmesg_events = events

        # Return filtered for the requested CPU
        if target_cpu is None:
            return events
        return [e for e in events if e.cpu == target_cpu or e.cpu == -1]

    def _snapshot_mce_banks(self) -> dict[str, int]:
        """Capture per-CPU per-bank MCE counts as a baseline snapshot."""
        snapshot: dict[str, int] = {}
        mce_base = Path("/sys/devices/system/machinecheck")
        try:
            if not mce_base.exists():
                return snapshot
        except OSError:
            return snapshot

        try:
            mce_dirs = list(mce_base.iterdir())
        except (OSError, PermissionError):
            return snapshot

        for mce_dir in mce_dirs:
            if not mce_dir.name.startswith("machinecheck"):
                continue
            try:
                cpu_num = int(mce_dir.name.removeprefix("machinecheck"))
            except ValueError:
                continue
            try:
                bank_files = list(mce_dir.glob("bank*"))
            except (OSError, PermissionError):
                continue
            for bank_file in bank_files:
                try:
                    count = int(bank_file.read_text().strip())
                    match = re.search(r"\d+", bank_file.name)
                    bank_num = int(match.group()) if match else -1
                    snapshot[f"{cpu_num}:{bank_num}"] = count
                except (ValueError, OSError, PermissionError, AttributeError):
                    continue
        return snapshot

    def _count_mce_events(self) -> int:
        """Count total MCE events across all CPUs.

        Gracefully handles permission denied and I/O errors.
        """
        total = 0
        mce_base = Path("/sys/devices/system/machinecheck")
        try:
            if not mce_base.exists():
                return 0
        except OSError:
            return 0

        try:
            mce_dirs = list(mce_base.iterdir())
        except (OSError, PermissionError) as exc:
            log.debug("Cannot list %s: %s", mce_base, exc)
            return 0

        for mce_dir in mce_dirs:
            try:
                bank_files = list(mce_dir.glob("bank*"))
            except (OSError, PermissionError):
                continue
            for bank_file in bank_files:
                try:
                    total += int(bank_file.read_text().strip())
                except (ValueError, OSError, PermissionError):
                    continue
        return total


def _get_dmesg_raw_timestamp() -> float:
    """Get the latest dmesg raw monotonic timestamp for baseline filtering."""
    try:
        import subprocess

        result = subprocess.run(
            ["dmesg", "--time-format=raw"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.strip().splitlines()
        if lines:
            ts_str = lines[-1].split()[0] if lines[-1] else ""
            try:
                return float(ts_str)
            except ValueError:
                return 0.0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError, PermissionError):
        pass
    return 0.0
