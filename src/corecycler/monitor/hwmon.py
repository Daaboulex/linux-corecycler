"""Hardware monitoring via hwmon/k10temp sysfs for temperature and voltage."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

HWMON_BASE = Path("/sys/class/hwmon")

log = logging.getLogger(__name__)

SUPERIO_FAMILIES = ("nct6", "it8")
BOARD_CHIPS = (*SUPERIO_FAMILIES, "asusec", "asus_wmi_sensors")


CPU_DRIVERS = ("zenpower", "zenpower3", "zenpower5", "k10temp")
FALLBACK_CPU_DRIVER = "coretemp"


type CoreRead = tuple[int, float | None, float | None, float | None, float | None]


@dataclass(frozen=True, slots=True)
class SensorRead:
    hottest: float | None = None
    tctl: float | None = None
    tccd: tuple[tuple[int, float | None], ...] = ()
    volts: tuple[tuple[str, float | None], ...] = ()
    watts: tuple[tuple[str, float | None], ...] = ()
    cores: tuple[CoreRead, ...] = ()


def _milli(path: Path) -> float | None:
    try:
        return int(path.read_text().strip()) / 1000
    except OSError, ValueError:
        return None


def _label(value: Path) -> str:
    label = value.with_name(value.name.replace("_input", "_label"))
    if not label.is_file():
        return ""
    try:
        return label.read_text().strip()
    except OSError as exc:
        log.warning("hwmon label %s is unreadable, so its input is read without a name: %s", label, exc)
        return ""


class Sensors:
    """One scan of /sys/class/hwmon, kept: every read opens each input once.

    The CPU temperatures come from the first of CPU_DRIVERS present, else
    coretemp. ``hottest`` is the highest of all its temperature inputs, the
    value the thermal stop compares; ``tctl`` is the input labelled Tctl (the
    first input when none is labelled); ``tccd`` is each CCD's own input keyed
    from 0. Voltages are every input of the CPU driver, amdgpu and the Super
    I/O chips, named "chip:label" ("chip@bus:label" when two devices share a
    name). A value that cannot be read is None, never 0.
    """

    def __init__(self, root: Path = HWMON_BASE) -> None:
        devices = (
            sorted((d, (d / "name").read_text().strip()) for d in root.iterdir() if (d / "name").is_file())
            if root.is_dir()
            else []
        )
        by_name = dict((name, device) for device, name in reversed(devices))
        cpu = next((by_name[name] for name in (*CPU_DRIVERS, FALLBACK_CPU_DRIVER) if name in by_name), None)
        self.temps = sorted(cpu.glob("temp*_input"), key=lambda p: int(p.name[4:-6])) if cpu is not None else []
        labels = [_label(path) for path in self.temps]
        self.tctl = labels.index("Tctl") if "Tctl" in labels else (0 if self.temps else None)
        self.tccd = sorted(
            (int(found.group(1)) - 1, index)
            for index, label in enumerate(labels)
            if (found := re.fullmatch(r"Tccd(\d+)", label))
        )
        names = [name for _, name in devices]
        self.volts: list[tuple[str, Path]] = []
        own, labelled, board_in0 = [], [], []
        for device, name in devices:
            if device != cpu and name != "amdgpu" and not name.startswith(BOARD_CHIPS):
                continue
            prefix = f"{name}@{(device / 'device').resolve().name}" if names.count(name) > 1 else name
            for value in device.glob("in*_input"):
                label = _label(value)
                source = f"{prefix}:{label or value.name.removesuffix('_input')}"
                self.volts.append((source, value))
                if "vcore" in label.lower() or label.lower() in ("svi2_core", "svi2_vdd"):
                    (own if device == cpu else labelled).append(source)
                elif name.startswith(SUPERIO_FAMILIES) and value.name == "in0_input":
                    board_in0.append(source)
        self.volts.sort()
        self.vcore_source = next((min(tier) for tier in (own, labelled, board_in0) if tier), None)

    def available(self) -> bool:
        return bool(self.temps)

    def read(self) -> SensorRead:
        temps = [_milli(path) for path in self.temps]
        present = [t for t in temps if t is not None]
        return SensorRead(
            hottest=max(present, default=None),
            tctl=None if self.tctl is None else temps[self.tctl],
            tccd=tuple((ccd, temps[index]) for ccd, index in self.tccd),
            volts=tuple((source, _milli(path)) for source, path in self.volts),
        )

    def vcore(self, read: SensorRead) -> float | None:
        """The core rail as one source reports it, chosen once at the scan.

        The CPU driver's own core reading first, then any input labelled
        Vcore, then a Super I/O chip's in0, where most boards wire Vcore.
        """
        return None if self.vcore_source is None else dict(read.volts)[self.vcore_source]
