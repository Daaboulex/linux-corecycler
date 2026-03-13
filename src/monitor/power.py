"""Package power monitoring via RAPL sysfs."""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

RAPL_BASE = Path("/sys/class/powercap/intel-rapl")


class PowerMonitor:
    """Read package power from RAPL (works on AMD too despite the 'intel' name).

    RAPL energy_uj is root-only on many systems.  If sysfs is not readable,
    falls back gracefully (is_available() returns False).
    """

    def __init__(self) -> None:
        self._package_path: Path | None = None
        self._last_energy_uj: int | None = None
        self._last_time: float | None = None
        self._find_package()

    def _find_package(self) -> None:
        """Find a readable RAPL package energy counter."""
        # Try primary path first
        pkg0 = RAPL_BASE / "intel-rapl:0" / "energy_uj"
        if self._try_read(pkg0):
            self._package_path = pkg0
            return

        # Scan all RAPL domains for a readable "package" counter
        if RAPL_BASE.exists():
            for rapl_dir in sorted(RAPL_BASE.parent.glob("intel-rapl*")):
                energy = rapl_dir / "energy_uj"
                name = rapl_dir / "name"
                if (
                    name.exists()
                    and "package" in name.read_text().strip().lower()
                    and self._try_read(energy)
                ):
                    self._package_path = energy
                    return

        if pkg0.exists():
            log.info("RAPL energy_uj exists but is not readable (needs root)")
        else:
            log.debug("RAPL sysfs not found")

    @staticmethod
    def _try_read(path: Path) -> bool:
        """Check if a sysfs file exists AND is readable."""
        try:
            int(path.read_text().strip())
            return True
        except (OSError, ValueError, PermissionError):
            return False

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
                delta = energy_uj - self._last_energy_uj
                if delta < 0:
                    delta += 2**32  # 32-bit counter wraparound
                watts = (delta / 1_000_000) / dt
                self._last_energy_uj = energy_uj
                self._last_time = now
                return watts

        self._last_energy_uj = energy_uj
        self._last_time = now
        return None
