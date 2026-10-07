from __future__ import annotations

from types import SimpleNamespace

from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.monitor import tick
from tests._sensors import FakeClocks, FakeSensors


class FakePower:
    def __init__(self, watts):
        self.watts = watts

    def is_available(self):
        return self.watts is not None

    def read_power_watts(self):
        return self.watts


class FakeMsr:
    def __init__(self, available, package=None):
        self.available = available
        self.package = package
        self.asked: list[list[int]] = []
        self.closed = False

    def is_available(self):
        return self.available

    def read_package_power(self):
        return self.package

    def read_clock_stretch(self, cpus):
        self.asked.append(list(cpus))
        return {cpu: SimpleNamespace(stretch_pct=1.5) for cpu in cpus} if self.available else {}

    def read_core_power(self, cpus):
        return {cpu: SimpleNamespace(watts=9.0) for cpu in cpus} if self.available else {}

    def close(self):
        self.closed = True


def ticker(watts=None, msr=None):
    t = tick.Ticker(FakeSensors(), FakeClocks({0: 5000.0}, {0: 5700.0}))
    t.power = FakePower(watts)
    t.msr = msr or FakeMsr(False)
    t.usage = SimpleNamespace(read=lambda: {0: 40.0})
    t.pm = SimpleNamespace(read=lambda: "pm", is_available=lambda: True)
    return t


def test_a_tick_holds_one_read_of_every_source():
    reading = ticker(watts=95.0, msr=FakeMsr(True)).read([0, 2])
    assert reading.clocks == {0: 5000.0}
    assert reading.ceilings == {0: 5700.0}
    assert reading.sensed == FakeSensors().read()
    assert reading.package_watts == 95.0
    assert reading.usage == {0: 40.0}
    assert reading.stretch == {0: 1.5, 2: 1.5}
    assert reading.core_watts == {0: 9.0, 2: 9.0}
    assert reading.pm == "pm"


def test_package_power_falls_back_to_the_msr_only_when_the_power_reader_has_none():
    assert ticker(watts=None, msr=FakeMsr(True, package=88.0)).read([]).package_watts == 88.0
    assert ticker(watts=95.0, msr=FakeMsr(True, package=88.0)).read([]).package_watts == 95.0
    assert ticker(watts=None, msr=FakeMsr(False, package=88.0)).read([]).package_watts is None


def test_power_is_available_from_either_source():
    assert ticker(watts=95.0).power_available()
    assert ticker(msr=FakeMsr(True)).power_available()
    assert not ticker().power_available()


def test_the_msr_is_read_on_each_core_s_first_cpu():
    topo = CPUTopology()
    topo.cores = {
        0: PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=(0, 16)),
        1: PhysicalCore(core_id=1, ccd=0, ccx=None, logical_cpus=(1, 17)),
        2: PhysicalCore(core_id=2, ccd=0, ccx=None, logical_cpus=()),
    }
    assert tick.first_cpus(topo) == [0, 1]
    assert tick.first_cpus(None) == []


def test_closing_the_ticker_closes_the_msr():
    msr = FakeMsr(True)
    t = ticker(msr=msr)
    t.close()
    assert msr.closed
