from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from corecycler.monitor.cpu_usage import CPUUsageReader
from corecycler.monitor.msr import MSRReader
from corecycler.monitor.power import PowerMonitor
from corecycler.smu.pmtable import PMTableData, PMTableReader

if TYPE_CHECKING:
    from corecycler.engine.topology import CPUTopology
    from corecycler.monitor.frequency import Clocks
    from corecycler.monitor.hwmon import SensorRead, Sensors


@dataclass(frozen=True, slots=True)
class Tick:
    clocks: dict[int, float | None]
    ceilings: dict[int, float | None]
    sensed: SensorRead
    package_watts: float | None
    usage: dict[int, float]
    stretch: dict[int, float]
    core_watts: dict[int, float]
    pm: PMTableData | None


def first_cpus(topology: CPUTopology | None) -> list[int]:
    if topology is None:
        return []
    return [core.logical_cpus[0] for core in topology.cores.values() if core.logical_cpus]


class Ticker:
    def __init__(self, sensors: Sensors, clocks: Clocks) -> None:
        self.sensors = sensors
        self.clocks = clocks
        self.power = PowerMonitor()
        self.msr = MSRReader()
        self.usage = CPUUsageReader()
        self.pm = PMTableReader()

    def power_available(self) -> bool:
        return self.power.is_available() or self.msr.is_available()

    def read(self, cpus: list[int]) -> Tick:
        package = self.power.read_power_watts()
        if package is None and self.msr.is_available():
            package = self.msr.read_package_power()
        return Tick(
            clocks=self.clocks.read(),
            ceilings=self.clocks.ceilings(),
            sensed=self.sensors.read(),
            package_watts=package,
            usage=self.usage.read(),
            stretch={cpu: r.stretch_pct for cpu, r in self.msr.read_clock_stretch(cpus).items()},
            core_watts={cpu: r.watts for cpu, r in self.msr.read_core_power(cpus).items()},
            pm=self.pm.read(),
        )

    def close(self) -> None:
        self.msr.close()
