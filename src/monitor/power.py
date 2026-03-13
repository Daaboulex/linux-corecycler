"""Package power monitoring via RAPL sysfs with hwmon fallback."""

from __future__ import annotations

import contextlib
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

RAPL_BASE = Path("/sys/class/powercap/intel-rapl")
HWMON_BASE = Path("/sys/class/hwmon")

# Hwmon drivers that expose package power via power1_input (microwatts)
_POWER_HWMON_DRIVERS = ("zenpower", "zenpower3", "k10temp")


class PowerMonitor:
    """Read package power from RAPL or hwmon.

    Prefers RAPL energy_uj (delta-based, accurate) but falls back to
    hwmon power1_input (instantaneous microwatts from zenpower/k10temp).
    RAPL sysfs is root-only on many systems; hwmon is world-readable.
    """

    def __init__(self) -> None:
        self._package_path: Path | None = None
        self._hwmon_power_path: Path | None = None  # fallback: power1_input
        self._last_energy_uj: int | None = None
        self._last_time: float | None = None
        self._find_package()

    def _find_package(self) -> None:
        """Find a readable RAPL package energy counter, or hwmon fallback."""
        # Try primary RAPL path first
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

        # Fallback: hwmon power1_input from zenpower/k10temp (world-readable)
        if HWMON_BASE.exists():
            for hwmon_dir in sorted(HWMON_BASE.iterdir()):
                name_file = hwmon_dir / "name"
                if not name_file.exists():
                    continue
                name = name_file.read_text().strip()
                if name not in _POWER_HWMON_DRIVERS:
                    continue
                # Find a power input labeled as package/RAPL
                for pf in sorted(hwmon_dir.glob("power*_input")):
                    label_file = pf.parent / pf.name.replace("_input", "_label")
                    label = ""
                    if label_file.exists():
                        with contextlib.suppress(OSError):
                            label = label_file.read_text().strip().lower()
                    if "rapl" in label or "package" in label or not label:
                        if self._try_read(pf):
                            self._hwmon_power_path = pf
                            log.info("Using hwmon %s for package power (no root needed)", name)
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
        return self._package_path is not None or self._hwmon_power_path is not None

    def read_power_watts(self) -> float | None:
        """Read package power in watts.

        RAPL mode: delta between two energy readings (first call returns None).
        Hwmon mode: instantaneous reading in microwatts.
        """
        if self._package_path:
            return self._read_rapl()
        if self._hwmon_power_path:
            return self._read_hwmon_power()
        return None

    def _read_rapl(self) -> float | None:
        """Read power from RAPL energy counter (delta-based)."""
        try:
            energy_uj = int(self._package_path.read_text().strip())  # type: ignore[union-attr]
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

    def _read_hwmon_power(self) -> float | None:
        """Read instantaneous power from hwmon power*_input (microwatts)."""
        try:
            uw = int(self._hwmon_power_path.read_text().strip())  # type: ignore[union-attr]
            return uw / 1_000_000
        except (ValueError, OSError):
            return None
