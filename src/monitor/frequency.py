"""Per-core CPU frequency monitoring."""

from __future__ import annotations

import contextlib
from pathlib import Path

CPUFREQ_BASE = Path("/sys/devices/system/cpu")


def read_core_frequencies() -> dict[int, float]:
    """Read current frequency (MHz) for each logical CPU from cpufreq sysfs."""
    freqs: dict[int, float] = {}

    if not CPUFREQ_BASE.exists():
        return _read_from_proc()

    for cpu_dir in sorted(CPUFREQ_BASE.iterdir()):
        if not cpu_dir.name.startswith("cpu") or not cpu_dir.name[3:].isdigit():
            continue
        cpu_id = int(cpu_dir.name[3:])

        # prefer scaling_cur_freq (actual governor-reported frequency)
        freq_file = cpu_dir / "cpufreq" / "scaling_cur_freq"
        if not freq_file.exists():
            freq_file = cpu_dir / "cpufreq" / "cpuinfo_cur_freq"
        if not freq_file.exists():
            continue

        try:
            khz = int(freq_file.read_text().strip())
            freqs[cpu_id] = khz / 1000.0  # MHz
        except (ValueError, OSError):
            continue

    return freqs if freqs else _read_from_proc()


def _read_from_proc() -> dict[int, float]:
    """Fallback: read frequencies from /proc/cpuinfo."""
    freqs: dict[int, float] = {}
    proc_cpuinfo = Path("/proc/cpuinfo")
    if not proc_cpuinfo.exists():
        return freqs

    current_cpu = -1
    for line in proc_cpuinfo.read_text().splitlines():
        if line.startswith("processor"):
            current_cpu = int(line.split(":")[1].strip())
        elif line.startswith("cpu MHz") and current_cpu >= 0:
            with contextlib.suppress(ValueError):
                freqs[current_cpu] = float(line.split(":")[1].strip())

    return freqs


def read_max_frequency(cpu_id: int = 0) -> float | None:
    """Read the maximum boost frequency for a CPU (MHz)."""
    path = CPUFREQ_BASE / f"cpu{cpu_id}" / "cpufreq" / "cpuinfo_max_freq"
    if path.exists():
        try:
            return int(path.read_text().strip()) / 1000.0
        except (ValueError, OSError):
            pass
    return None


def read_min_frequency(cpu_id: int = 0) -> float | None:
    """Read the minimum frequency for a CPU (MHz)."""
    path = CPUFREQ_BASE / f"cpu{cpu_id}" / "cpufreq" / "cpuinfo_min_freq"
    if path.exists():
        try:
            return int(path.read_text().strip()) / 1000.0
        except (ValueError, OSError):
            pass
    return None
