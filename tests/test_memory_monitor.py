"""Tests for the DIMM/memory monitoring module."""

from __future__ import annotations

import pytest
from monitor.memory import DIMMInfo, parse_dmidecode_output, SPD5118Reader

SAMPLE_DMIDECODE = """\
# dmidecode 3.6
Handle 0x003D, DMI type 17, 92 bytes
Memory Device
\tTotal Width: 80 bits
\tData Width: 64 bits
\tSize: 32 GB
\tForm Factor: DIMM
\tLocator: DIMM 0
\tBank Locator: P0 CHANNEL A
\tType: DDR5
\tSpeed: 6000 MT/s
\tManufacturer: G Skill Intl
\tSerial Number: 00000000
\tAsset Tag: Not Specified
\tPart Number: F5-6000J3038F16G
\tRank: 2
\tConfigured Memory Speed: 6000 MT/s
\tMinimum Voltage: 1.1 V
\tMaximum Voltage: 1.1 V
\tConfigured Voltage: 1.1 V

Handle 0x003E, DMI type 17, 92 bytes
Memory Device
\tTotal Width: 80 bits
\tData Width: 64 bits
\tSize: 32 GB
\tForm Factor: DIMM
\tLocator: DIMM 1
\tBank Locator: P0 CHANNEL A
\tType: DDR5
\tSpeed: 6000 MT/s
\tManufacturer: G Skill Intl
\tSerial Number: 00000001
\tPart Number: F5-6000J3038F16G
\tRank: 2
\tConfigured Memory Speed: 6000 MT/s
\tMinimum Voltage: 1.1 V
\tMaximum Voltage: 1.1 V
\tConfigured Voltage: 1.1 V
"""


class TestParseDmidecode:
    def test_parses_two_dimms(self):
        dimms = parse_dmidecode_output(SAMPLE_DMIDECODE)
        assert len(dimms) == 2

    def test_dimm_fields(self):
        dimms = parse_dmidecode_output(SAMPLE_DMIDECODE)
        d = dimms[0]
        assert d.size_gb == 32
        assert d.mem_type == "DDR5"
        assert d.speed_mt == 6000
        assert d.manufacturer == "G Skill Intl"
        assert d.part_number == "F5-6000J3038F16G"
        assert d.rank == 2
        assert d.locator == "DIMM 0"
        assert d.configured_voltage == 1.1

    def test_empty_output(self):
        assert parse_dmidecode_output("") == []

    def test_no_memory_devices(self):
        assert parse_dmidecode_output("# dmidecode 3.6\nBIOS Information\n") == []


class TestSPD5118Reader:
    def test_finds_spd5118_devices(self, tmp_path):
        hwmon0 = tmp_path / "hwmon0"
        hwmon0.mkdir()
        (hwmon0 / "name").write_text("spd5118\n")
        (hwmon0 / "temp1_input").write_text("42500\n")

        reader = SPD5118Reader(hwmon_base=tmp_path)
        temps = reader.read_temperatures()
        assert len(temps) == 1
        assert abs(temps[0] - 42.5) < 0.01

    def test_no_spd5118_returns_empty(self, tmp_path):
        reader = SPD5118Reader(hwmon_base=tmp_path)
        assert reader.read_temperatures() == []
