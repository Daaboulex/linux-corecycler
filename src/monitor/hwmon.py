"""Hardware monitoring via hwmon/k10temp sysfs for temperature and voltage."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

HWMON_BASE = Path("/sys/class/hwmon")


@dataclass(slots=True)
class HWMonData:
    tctl_c: float | None = None
    tdie_c: float | None = None
    tccd_temps: dict[int, float] = field(default_factory=dict)  # CCD index -> temp
    vcore_v: float | None = None
    vsoc_v: float | None = None


class HWMonReader:
    """Read CPU temperatures and voltages from hwmon (k10temp/zenpower/coretemp)."""

    # Prefer AMD-specific drivers (richer data) over generic coretemp
    _PREFERRED = ("zenpower", "zenpower3", "k10temp")
    _FALLBACK = ("coretemp",)

    def __init__(self) -> None:
        self._hwmon_path: Path | None = None
        self._find_device()

    def _find_device(self) -> None:
        """Find a supported CPU hwmon device (prefer AMD drivers over coretemp)."""
        if not HWMON_BASE.exists():
            return

        fallback: Path | None = None
        for hwmon_dir in sorted(HWMON_BASE.iterdir()):
            name_file = hwmon_dir / "name"
            if name_file.exists():
                name = name_file.read_text().strip()
                if name in self._PREFERRED:
                    self._hwmon_path = hwmon_dir
                    return
                if name in self._FALLBACK and fallback is None:
                    fallback = hwmon_dir

        if fallback is not None:
            self._hwmon_path = fallback

    def is_available(self) -> bool:
        return self._hwmon_path is not None

    def read(self) -> HWMonData:
        data = HWMonData()
        if not self._hwmon_path:
            return data

        # read all temp inputs and their labels
        for temp_file in sorted(self._hwmon_path.glob("temp*_input")):
            try:
                temp_c = int(temp_file.read_text().strip()) / 1000.0
            except (ValueError, OSError):
                continue

            label_file = temp_file.parent / temp_file.name.replace("_input", "_label")
            label = ""
            if label_file.exists():
                with contextlib.suppress(OSError):
                    label = label_file.read_text().strip().lower()

            if "tctl" in label:
                data.tctl_c = temp_c
            elif "tdie" in label:
                data.tdie_c = temp_c
            elif "tccd" in label:
                # extract CCD number from label like "Tccd1", "Tccd2"
                m = re.search(r"tccd(\d+)", label)
                if m:
                    data.tccd_temps[int(m.group(1))] = temp_c
            elif not data.tctl_c:
                # fallback: first unlabeled temp is likely Tctl
                data.tctl_c = temp_c

        # read voltage inputs (SVI2)
        for in_file in sorted(self._hwmon_path.glob("in*_input")):
            try:
                mv = int(in_file.read_text().strip())
                voltage = mv / 1000.0
            except (ValueError, OSError):
                continue

            label_file = in_file.parent / in_file.name.replace("_input", "_label")
            label = ""
            if label_file.exists():
                with contextlib.suppress(OSError):
                    label = label_file.read_text().strip().lower()

            if "vsoc" in label or "svi2_vddnb" in label:
                data.vsoc_v = voltage
            elif "vcore" in label or "svi2_vdd" in label:
                data.vcore_v = voltage

        return data
