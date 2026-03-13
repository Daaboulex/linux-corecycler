"""Hardware monitoring via hwmon/k10temp sysfs for temperature and voltage."""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from pathlib import Path

HWMON_BASE = Path("/sys/class/hwmon")

# Super I/O chips that can provide Vcore via analog input (in0 on most boards).
# Nuvoton NCT67xx: common on ASUS, MSI, ASRock boards
# ITE IT868x/IT866x/IT871x: common on Gigabyte boards
_SUPERIO_CHIPS = (
    "nct6799", "nct6798", "nct6797", "nct6796", "nct6795",
    "nct6793", "nct6792", "nct6791", "nct6779", "nct6776", "nct6775",
    "it8689", "it8688", "it8686", "it8665", "it8628", "it8625",
    "it8720", "it8728", "it8771", "it8772",
)


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
    _PREFERRED = ("zenpower", "zenpower3", "zenpower5", "k10temp")
    _FALLBACK = ("coretemp",)

    def __init__(self) -> None:
        self._hwmon_path: Path | None = None
        self._superio_path: Path | None = None  # fallback voltage from Super I/O
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
                elif name in self._FALLBACK and fallback is None:
                    fallback = hwmon_dir
                elif any(name.startswith(c) for c in _SUPERIO_CHIPS):
                    # Super I/O chip — use as voltage fallback (in0 = Vcore on most boards)
                    self._superio_path = hwmon_dir

        if self._hwmon_path is None and fallback is not None:
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

        # read voltage inputs (SVI2) from CPU driver
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

        # Fallback: read Vcore from Super I/O chip (in0 = Vcore on most boards)
        # Needed for Zen 5 which uses SVI3 — not yet supported by CPU drivers
        if data.vcore_v is None and self._superio_path is not None:
            in0 = self._superio_path / "in0_input"
            if in0.exists():
                with contextlib.suppress(ValueError, OSError):
                    data.vcore_v = int(in0.read_text().strip()) / 1000.0

        return data
