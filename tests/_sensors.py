from __future__ import annotations

from corecycler.monitor.hwmon import SensorRead
from corecycler.monitor.tick import Tick

BOARD_VCORE = "nct6799:in0"


class FakeSensors:
    def __init__(self, tctl=60.0, tccd=((0, 61.0), (1, 59.0)), vcore=1.2, vcore_source=BOARD_VCORE):
        self.vcore_source = vcore_source
        self.give(tctl, tccd, vcore)

    def give(self, tctl=None, tccd=(), vcore=None):
        temps = [t for t in (tctl, *(t for _, t in tccd)) if t is not None]
        volts = () if self.vcore_source is None else ((self.vcore_source, vcore),)
        self.reading = SensorRead(hottest=max(temps, default=None), tctl=tctl, tccd=tccd, volts=volts)

    def available(self):
        return self.reading.tctl is not None

    def read(self):
        return self.reading

    def vcore(self, read):
        return None if self.vcore_source is None else dict(read.volts)[self.vcore_source]


class FakeClocks:
    def __init__(self, clocks=None, ceilings=None):
        self.clocks = clocks if clocks is not None else {}
        self.ceiling = ceilings if ceilings is not None else {}

    def read(self):
        return dict(self.clocks)

    def ceilings(self):
        return dict(self.ceiling)


def a_tick(sensors=None, **fields) -> Tick:
    quiet = {
        "clocks": {},
        "ceilings": {},
        "sensed": (sensors or FakeSensors()).read(),
        "package_watts": None,
        "usage": {},
        "stretch": {},
        "core_watts": {},
        "pm": None,
        "cores": (),
    }
    return Tick(**(quiet | fields))


class FakeTicker:
    def __init__(self, sensors, clocks):
        self.sensors = sensors
        self.clocks = clocks
        self.pm = PMAbsent()
        self.closed = False
        self.core_ids = None

    def power_available(self):
        return False

    def read(self, cpus, core_ids=()):
        self.core_ids = tuple(core_ids)
        return a_tick(self.sensors, clocks=self.clocks.read(), ceilings=self.clocks.ceilings())

    def close(self):
        self.closed = True


class PMAbsent:
    def is_available(self):
        return False
