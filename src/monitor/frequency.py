"""Per-core CPU frequency monitoring."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

CPUFREQ_BASE = Path("/sys/devices/system/cpu")


def read_core_frequencies() -> dict[int, float]:
    """Read current frequency (MHz) for each logical CPU.

    Prefers ``cpuinfo_cur_freq`` (actual hardware frequency reported by the
    driver) over ``scaling_cur_freq`` (governor *target* frequency).  On
    amd_pstate active, scaling_cur_freq is the EPP target which can be lower
    than the actual boost clock the CPU is running at.  Falls back to
    /proc/cpuinfo ``cpu MHz`` which also reflects actual frequency.
    """
    freqs: dict[int, float] = {}

    if not CPUFREQ_BASE.exists():
        return _read_from_proc()

    for cpu_dir in sorted(CPUFREQ_BASE.iterdir()):
        if not cpu_dir.name.startswith("cpu") or not cpu_dir.name[3:].isdigit():
            continue
        cpu_id = int(cpu_dir.name[3:])

        # Prefer cpuinfo_cur_freq (actual HW frequency) over scaling_cur_freq (target)
        freq_file = cpu_dir / "cpufreq" / "cpuinfo_cur_freq"
        if not freq_file.exists():
            freq_file = cpu_dir / "cpufreq" / "scaling_cur_freq"
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


@dataclass(slots=True)
class CoreFreqReading:
    """Per-core frequency reading with actual vs effective max for stretch detection."""

    actual_mhz: float  # current APERF/MPERF-derived frequency
    effective_max_mhz: float  # scaling_max_freq — what this core *should* reach under load


def read_core_frequencies_dual() -> dict[int, CoreFreqReading]:
    """Read actual frequency and effective max for each logical CPU.

    On amd_pstate active, both scaling_cur_freq and cpuinfo_avg_freq reflect
    APERF/MPERF-derived actual frequency (they're identical).  The
    ``scaling_max_freq`` is the per-core boost ceiling.

    During a stress test, if actual << effective_max, the core is clock
    stretching — a sign of CO instability or power/thermal limiting.
    """
    result: dict[int, CoreFreqReading] = {}

    if not CPUFREQ_BASE.exists():
        return result

    for cpu_dir in sorted(CPUFREQ_BASE.iterdir()):
        if not cpu_dir.name.startswith("cpu") or not cpu_dir.name[3:].isdigit():
            continue
        cpu_id = int(cpu_dir.name[3:])
        cpufreq = cpu_dir / "cpufreq"

        # Actual frequency (prefer cpuinfo_cur_freq → cpuinfo_avg_freq → scaling_cur_freq)
        actual = None
        for fname in ("cpuinfo_cur_freq", "cpuinfo_avg_freq", "scaling_cur_freq"):
            f = cpufreq / fname
            if f.exists():
                try:
                    actual = int(f.read_text().strip()) / 1000.0
                    break
                except (ValueError, OSError):
                    continue

        # Effective max (scaling_max_freq — boost ceiling for this core)
        eff_max = None
        max_file = cpufreq / "scaling_max_freq"
        if max_file.exists():
            try:
                eff_max = int(max_file.read_text().strip()) / 1000.0
            except (ValueError, OSError):
                pass

        if actual is not None and eff_max is not None:
            result[cpu_id] = CoreFreqReading(actual_mhz=actual, effective_max_mhz=eff_max)

    return result


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
