"""Package power monitoring via RAPL sysfs."""

from __future__ import annotations

from pathlib import Path
import time

RAPL_BASE = Path("/sys/class/powercap/intel-rapl")


class PowerMonitor:
    """Read package power from RAPL (works on AMD too despite the 'intel' name)."""

    def __init__(self) -> None:
        self._package_path: Path | None = None
        self._last_energy_uj: int | None = None
        self._last_time: float | None = None
        self._find_package()

    def _find_package(self) -> None:
        """Find the RAPL package-0 energy counter."""
        # RAPL on AMD: /sys/class/powercap/intel-rapl:0/energy_uj
        pkg0 = RAPL_BASE / "intel-rapl:0" / "energy_uj"
        if pkg0.exists():
            self._package_path = pkg0
            return

        # alternative path
        for rapl_dir in sorted(RAPL_BASE.parent.glob("intel-rapl*")):
            energy = rapl_dir / "energy_uj"
            name = rapl_dir / "name"
            if energy.exists() and name.exists():
                if "package" in name.read_text().strip().lower():
                    self._package_path = energy
                    return

    def is_available(self) -> bool:
        return self._package_path is not None

    def read_power_watts(self) -> float | None:
        """Read instantaneous package power in watts.

        Uses delta between two energy readings. First call returns None
        (needs two readings to compute delta).
        """
        if not self._package_path:
            return None

        try:
            energy_uj = int(self._package_path.read_text().strip())
        except (ValueError, OSError):
            return None

        now = time.monotonic()

        if self._last_energy_uj is not None and self._last_time is not None:
            dt = now - self._last_time
            if dt > 0:
                # handle counter wraparound (32-bit or 64-bit)
                delta = energy_uj - self._last_energy_uj
                if delta < 0:
                    # assume 32-bit wraparound
                    delta += 2**32
                watts = (delta / 1_000_000) / dt
                self._last_energy_uj = energy_uj
                self._last_time = now
                return watts

        self._last_energy_uj = energy_uj
        self._last_time = now
        return None
