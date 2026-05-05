"""Safe read-only MSR (Model-Specific Register) access for AMD Zen CPUs.

Reads from /dev/cpu/N/msr — requires root and the 'msr' kernel module.
ALL operations are strictly read-only. No MSR writes are ever performed.
"""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass

# AMD MSR addresses (read-only)
MSR_APERF = 0xE8  # Actual Performance — counts at actual core frequency
MSR_MPERF = 0xE7  # Maximum Performance — counts at TSC/reference frequency
MSR_PWR_UNIT = 0xC0010299  # RAPL power unit (energy scale factor)
MSR_CORE_ENERGY = 0xC001029A  # Per-core cumulative energy counter
MSR_PKG_ENERGY = 0xC001029B  # Package cumulative energy counter


@dataclass(slots=True)
class ClockStretchReading:
    """Result of APERF/MPERF sampling for one logical CPU."""

    cpu_id: int
    effective_mhz: float  # actual clock from APERF/MPERF ratio * base_clock
    ratio: float  # APERF_delta / MPERF_delta — 1.0 = no stretch, <0.97 = stretching
    stretch_pct: float  # (1 - ratio) * 100 — 0% = perfect, >3% = suspicious


@dataclass(slots=True)
class CorePowerReading:
    """Per-core power consumption from RAPL MSR."""

    core_id: int
    watts: float


@dataclass(slots=True)
class _PerfSnapshot:
    """Internal: previous APERF/MPERF readings for clock stretch delta."""

    aperf: int = 0
    mperf: int = 0


@dataclass(slots=True)
class _EnergySnapshot:
    """Internal: previous energy reading for power delta."""

    energy_raw: int = 0
    timestamp: float = 0.0


class MSRReader:
    """Read-only MSR access for AMD Zen CPUs.

    Opens /dev/cpu/N/msr file descriptors on demand and caches them.
    Computes deltas between successive reads for rate-based metrics.

    Safety: this class NEVER writes to MSRs. All os.open() calls use O_RDONLY.
    """

    def __init__(self) -> None:
        self._fds: dict[int, int] = {}  # cpu_id → file descriptor
        self._perf_prev: dict[int, _PerfSnapshot] = {}  # clock stretch state
        self._energy_prev: dict[int, _EnergySnapshot] = {}  # per-core power state
        self._pkg_energy_prev: _EnergySnapshot | None = None  # package power state
        self._energy_unit: float | None = None  # joules per energy counter tick
        self._available: bool | None = None

    def is_available(self) -> bool:
        """Check if MSR reads are possible (root + msr module loaded)."""
        if self._available is not None:
            return self._available
        try:
            fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
            # Verify we can actually read an MSR
            os.pread(fd, 8, MSR_MPERF)
            os.close(fd)
            self._available = True
        except (OSError, PermissionError):
            self._available = False
        return self._available

    def read_clock_stretch(self, cpu_ids: list[int]) -> dict[int, ClockStretchReading]:
        """Read APERF/MPERF for given logical CPUs, return stretch ratios.

        First call per CPU establishes baseline and returns empty.
        Subsequent calls return the ratio over the interval since last read.
        """
        if not self.is_available():
            return {}

        now = time.monotonic()
        results: dict[int, ClockStretchReading] = {}

        for cpu_id in cpu_ids:
            aperf = self._read_msr(cpu_id, MSR_APERF)
            mperf = self._read_msr(cpu_id, MSR_MPERF)
            if aperf is None or mperf is None:
                continue

            prev = self._perf_prev.get(cpu_id)
            if prev is None or prev.mperf == 0:
                # First reading — store baseline
                self._perf_prev[cpu_id] = _PerfSnapshot(aperf=aperf, mperf=mperf)
                continue

            da = aperf - prev.aperf
            dm = mperf - prev.mperf

            # Handle 64-bit counter wraparound (extremely rare but possible)
            if da < 0:
                da += 1 << 64
            if dm < 0:
                dm += 1 << 64

            # Update stored values
            self._perf_prev[cpu_id] = _PerfSnapshot(aperf=aperf, mperf=mperf)

            if dm == 0:
                continue

            ratio = da / dm
            stretch_pct = max(0.0, (1.0 - ratio) * 100.0)

            # Estimate effective MHz: ratio * nominal max boost
            # (ratio > 1.0 is possible with turbo — clamp stretch to 0%)
            results[cpu_id] = ClockStretchReading(
                cpu_id=cpu_id,
                effective_mhz=0,  # caller fills from sysfs freq if needed
                ratio=ratio,
                stretch_pct=stretch_pct,
            )

        return results

    def read_core_power(self, cpu_ids: list[int]) -> dict[int, CorePowerReading]:
        """Read per-core power (watts) from AMD RAPL MSRs.

        Returns power for each CPU since the last call. First call returns empty.
        """
        if not self.is_available():
            return {}

        unit = self._get_energy_unit()
        if unit is None:
            return {}

        now = time.monotonic()
        results: dict[int, CorePowerReading] = {}

        for cpu_id in cpu_ids:
            raw = self._read_msr(cpu_id, MSR_CORE_ENERGY)
            if raw is None:
                continue

            prev = self._energy_prev.get(cpu_id)
            if prev is not None and prev.energy_raw > 0 and prev.timestamp > 0:
                dt = now - prev.timestamp
                if dt > 0:
                    de = raw - prev.energy_raw
                    if de < 0:
                        de += 1 << 32  # 32-bit energy counter wraparound
                    watts = (de * unit) / dt
                    results[cpu_id] = CorePowerReading(core_id=cpu_id, watts=watts)

            self._energy_prev[cpu_id] = _EnergySnapshot(energy_raw=raw, timestamp=now)

        return results

    def read_package_power(self) -> float | None:
        """Read package power (watts) from AMD RAPL MSR.

        First call returns None (establishes baseline).
        """
        if not self.is_available():
            return None
        unit = self._get_energy_unit()
        if unit is None:
            return None

        raw = self._read_msr(0, MSR_PKG_ENERGY)
        if raw is None:
            return None

        now = time.monotonic()
        prev = self._pkg_energy_prev
        if prev is not None and prev.energy_raw > 0 and prev.timestamp > 0:
            dt = now - prev.timestamp
            if dt > 0:
                de = raw - prev.energy_raw
                if de < 0:
                    de += 1 << 32
                watts = (de * unit) / dt
                self._pkg_energy_prev = _EnergySnapshot(energy_raw=raw, timestamp=now)
                return watts

        self._pkg_energy_prev = _EnergySnapshot(energy_raw=raw, timestamp=now)
        return None

    def close(self) -> None:
        """Close all cached file descriptors."""
        for fd in self._fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self._fds.clear()
        self._perf_prev.clear()
        self._energy_prev.clear()
        self._pkg_energy_prev = None

    def _get_fd(self, cpu_id: int) -> int | None:
        """Get or open a read-only fd for /dev/cpu/N/msr."""
        if cpu_id in self._fds:
            return self._fds[cpu_id]
        try:
            fd = os.open(f"/dev/cpu/{cpu_id}/msr", os.O_RDONLY)
            self._fds[cpu_id] = fd
            return fd
        except (OSError, PermissionError):
            return None

    def _read_msr(self, cpu_id: int, msr_addr: int) -> int | None:
        """Read a single 64-bit MSR value. Returns None on failure."""
        fd = self._get_fd(cpu_id)
        if fd is None:
            return None
        try:
            data = os.pread(fd, 8, msr_addr)
            return struct.unpack("Q", data)[0]
        except (OSError, struct.error):
            return None

    def _get_energy_unit(self) -> float | None:
        """Read AMD energy unit from MSR_PWR_UNIT (cached)."""
        if self._energy_unit is not None:
            return self._energy_unit
        raw = self._read_msr(0, MSR_PWR_UNIT)
        if raw is None:
            return None
        esu = (raw >> 8) & 0x1F
        self._energy_unit = 1.0 / (1 << esu)
        return self._energy_unit

    def __del__(self) -> None:
        self.close()
