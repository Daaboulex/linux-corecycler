"""Safe read-only MSR (Model-Specific Register) access for AMD Zen CPUs.

Reads from /dev/cpu/N/msr — requires root and the 'msr' kernel module.
ALL operations are strictly read-only. No MSR writes are ever performed.
"""

from __future__ import annotations

import contextlib
import os
import struct
import time
from dataclasses import dataclass

MSR_MPERF = 0xE7  # Maximum Performance, counting at the reference clock
MSR_PWR_UNIT = 0xC0010299  # RAPL power unit (energy scale factor)
MSR_CORE_ENERGY = 0xC001029A
MSR_PKG_ENERGY = 0xC001029B
MSR_DEVICE = "/dev/cpu/0/msr"
_ENERGY_COUNTER_SPAN = 1 << 32


def msr_problem(device: str = MSR_DEVICE) -> str | None:
    try:
        fd = os.open(device, os.O_RDONLY)
        try:
            os.pread(fd, 8, MSR_MPERF)
        finally:
            os.close(fd)
    except OSError as exc:
        return f"{device}: {exc.strerror}"
    return None


@dataclass(slots=True)
class CorePowerReading:
    """Per-core power consumption from RAPL MSR."""

    core_id: int
    watts: float


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
        self._fd_by_cpu: dict[int, int] = {}
        self._core_energy_prev: dict[int, _EnergySnapshot] = {}
        self._pkg_energy_prev: _EnergySnapshot | None = None
        self._joules_per_tick: float | None = None
        self._available: bool | None = None

    def is_available(self) -> bool:
        """Check if MSR reads are possible (root + msr module loaded)."""
        if self._available is None:
            self._available = msr_problem() is None
        return self._available

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

            prev = self._core_energy_prev.get(cpu_id)
            if prev is not None and prev.energy_raw > 0 and prev.timestamp > 0:
                dt = now - prev.timestamp
                if dt > 0:
                    de = raw - prev.energy_raw
                    if de < 0:
                        de += _ENERGY_COUNTER_SPAN
                    watts = (de * unit) / dt
                    results[cpu_id] = CorePowerReading(core_id=cpu_id, watts=watts)

            self._core_energy_prev[cpu_id] = _EnergySnapshot(energy_raw=raw, timestamp=now)

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
                    de += _ENERGY_COUNTER_SPAN
                watts = (de * unit) / dt
                self._pkg_energy_prev = _EnergySnapshot(energy_raw=raw, timestamp=now)
                return watts

        self._pkg_energy_prev = _EnergySnapshot(energy_raw=raw, timestamp=now)
        return None

    def close(self) -> None:
        """Close all cached file descriptors."""
        for fd in self._fd_by_cpu.values():
            with contextlib.suppress(OSError):
                os.close(fd)
        self._fd_by_cpu.clear()
        self._core_energy_prev.clear()
        self._pkg_energy_prev = None

    def _get_fd(self, cpu_id: int) -> int | None:
        """Get or open a read-only fd for /dev/cpu/N/msr."""
        if cpu_id in self._fd_by_cpu:
            return self._fd_by_cpu[cpu_id]
        try:
            fd = os.open(f"/dev/cpu/{cpu_id}/msr", os.O_RDONLY)
            self._fd_by_cpu[cpu_id] = fd
            return fd
        except OSError, PermissionError:
            return None

    def _read_msr(self, cpu_id: int, msr_addr: int) -> int | None:
        """Read a single 64-bit MSR value. Returns None on failure."""
        fd = self._get_fd(cpu_id)
        if fd is None:
            return None
        try:
            data = os.pread(fd, 8, msr_addr)
            return struct.unpack("Q", data)[0]
        except OSError, struct.error:
            return None

    def _get_energy_unit(self) -> float | None:
        """Read AMD energy unit from MSR_PWR_UNIT (cached)."""
        if self._joules_per_tick is not None:
            return self._joules_per_tick
        raw = self._read_msr(0, MSR_PWR_UNIT)
        if raw is None:
            return None
        esu = (raw >> 8) & 0x1F
        self._joules_per_tick = 1.0 / (1 << esu)
        return self._joules_per_tick

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            self._fd_by_cpu = {}
