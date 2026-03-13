"""Per-logical-CPU usage % from /proc/stat."""

from __future__ import annotations

from pathlib import Path


class CPUUsageReader:
    """Reads /proc/stat to compute per-CPU usage % between successive calls.

    First call establishes baseline and returns empty.
    Subsequent calls return usage % over the interval since last call.
    """

    def __init__(self) -> None:
        self._prev: dict[int, tuple[int, int]] = {}  # cpu_id → (busy, total)

    def read(self) -> dict[int, float]:
        """Return per-logical-CPU usage % (0-100). Empty on first call."""
        try:
            text = Path("/proc/stat").read_text()
        except OSError:
            return {}

        results: dict[int, float] = {}
        for line in text.splitlines():
            if not line.startswith("cpu") or line.startswith("cpu "):
                continue
            parts = line.split()
            # cpuN user nice system idle iowait irq softirq steal
            cpu_id = int(parts[0][3:])
            vals = [int(v) for v in parts[1:8]]
            idle = vals[3] + vals[4]  # idle + iowait
            total = sum(vals)
            busy = total - idle

            prev = self._prev.get(cpu_id)
            if prev is not None:
                db = busy - prev[0]
                dt = total - prev[1]
                if dt > 0:
                    results[cpu_id] = (db / dt) * 100.0

            self._prev[cpu_id] = (busy, total)

        return results
