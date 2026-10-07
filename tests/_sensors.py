from __future__ import annotations

from corecycler.monitor.hwmon import SensorRead

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
