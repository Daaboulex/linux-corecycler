"""Tests for the DIMM/memory monitoring module."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from monitor.memory import DIMMInfo, parse_dmidecode_output, SPD5118Reader
from smu.pmtable import PMTableData, compute_fclk_uclk_ratio

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

    def test_dimm_with_mb_size(self):
        """DIMMs reported in MB should still be parsed (rounded up to GB)."""
        text = SAMPLE_DMIDECODE.replace("32 GB", "512 MB")
        dimms = parse_dmidecode_output(text)
        assert len(dimms) == 2
        assert dimms[0].size_gb == 1  # 512MB rounds up to 1GB

    def test_dimm_with_gib_format(self):
        """dmidecode 3.7+ uses GiB instead of GB."""
        text = SAMPLE_DMIDECODE.replace("32 GB", "16 GiB")
        dimms = parse_dmidecode_output(text)
        assert len(dimms) == 2
        assert dimms[0].size_gb == 16


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


class TestFCLKUCLKRatio:
    def test_ratio_1_to_1(self):
        assert compute_fclk_uclk_ratio(2000.0, 2000.0) == (1, 1)

    def test_ratio_1_to_2(self):
        assert compute_fclk_uclk_ratio(1000.0, 2000.0) == (1, 2)

    def test_zero_fclk_returns_none(self):
        assert compute_fclk_uclk_ratio(0.0, 2000.0) is None

    def test_zero_uclk_returns_none(self):
        assert compute_fclk_uclk_ratio(2000.0, 0.0) is None

    def test_negative_returns_none(self):
        assert compute_fclk_uclk_ratio(-100.0, 2000.0) is None

    def test_unexpected_ratio_returns_none(self):
        assert compute_fclk_uclk_ratio(2000.0, 5000.0) is None

    def test_ratio_with_rounding(self):
        assert compute_fclk_uclk_ratio(2000.0, 2000.1) == (1, 1)

    def test_ratio_with_rounding_1_to_2(self):
        assert compute_fclk_uclk_ratio(1800.0, 3600.0) == (1, 2)


class TestPMTableDataMemoryFields:
    def test_defaults_memory_fields(self):
        data = PMTableData()
        assert data.fclk_mhz == 0.0
        assert data.uclk_mhz == 0.0
        assert data.mclk_mhz == 0.0
        assert data.vddcr_soc_v == 0.0
        assert data.vdd_mem_v == 0.0
        assert data.pm_table_version == 0
        assert data.is_calibrated is False

    def test_calibrated_data_fields(self):
        data = PMTableData(
            fclk_mhz=2000.0,
            uclk_mhz=2000.0,
            mclk_mhz=3000.0,
            vddcr_soc_v=1.25,
            vdd_mem_v=1.395,
            pm_table_version=0x620205,
            is_calibrated=True,
        )
        assert data.fclk_mhz == 2000.0
        assert data.is_calibrated is True
        ratio = compute_fclk_uclk_ratio(data.fclk_mhz, data.uclk_mhz)
        assert ratio == (1, 1)


class _MockLabel:
    """Lightweight mock of QLabel for headless testing of MemoryTab methods."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._stylesheet = ""

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text

    def styleSheet(self) -> str:
        return self._stylesheet

    def setStyleSheet(self, ss: str) -> None:
        self._stylesheet = ss


def _make_headless_tab():
    """Create a mock MemoryTab-like object with mock labels for headless testing.

    Avoids needing QApplication / pytest-qt by binding MemoryTab methods
    to a plain namespace object with mock label attributes.
    """
    import types

    from gui.memory_tab import MemoryTab

    tab = types.SimpleNamespace()
    tab._fclk_label = _MockLabel("FCLK: --")
    tab._uclk_label = _MockLabel("UCLK: --")
    tab._mclk_label = _MockLabel("MCLK: --")
    tab._ratio_label = _MockLabel("FCLK:UCLK --")
    tab._vdd_label = _MockLabel("VDD: --")
    tab._vddq_label = _MockLabel("VDDQ: --")
    tab._cal_label = _MockLabel("")
    tab._pm_reader = MagicMock()
    tab._pm_reader.is_available.return_value = True
    tab._spd_reader = MagicMock()
    tab._spd_reader.is_available.return_value = False
    # Bind MemoryTab methods to our namespace object
    tab._update_clock_labels = types.MethodType(MemoryTab._update_clock_labels, tab)
    tab._update_voltage_labels = types.MethodType(MemoryTab._update_voltage_labels, tab)
    tab._show_uncalibrated = types.MethodType(MemoryTab._show_uncalibrated, tab)
    tab._set_clocks_unavailable = types.MethodType(MemoryTab._set_clocks_unavailable, tab)
    tab._update_live_data = types.MethodType(MemoryTab._update_live_data, tab)
    tab._update_temperatures = types.MethodType(MemoryTab._update_temperatures, tab)
    tab._temp_labels = []
    return tab


class TestMemoryTabBehavior:
    def test_update_clock_labels_calibrated_1_to_1(self):
        tab = _make_headless_tab()
        pm_data = PMTableData(
            fclk_mhz=2000.0,
            uclk_mhz=2000.0,
            mclk_mhz=3000.0,
            vddcr_soc_v=1.25,
            vdd_mem_v=1.395,
            pm_table_version=0x620205,
            is_calibrated=True,
        )
        tab._update_clock_labels(pm_data)
        assert "2000" in tab._fclk_label.text()
        assert "2000" in tab._uclk_label.text()
        assert "3000" in tab._mclk_label.text()
        assert "1:1" in tab._ratio_label.text()
        assert "#4caf50" in tab._ratio_label.styleSheet()

    def test_update_clock_labels_1_to_2_ratio(self):
        tab = _make_headless_tab()
        pm_data = PMTableData(
            fclk_mhz=1800.0,
            uclk_mhz=3600.0,
            mclk_mhz=3600.0,
            is_calibrated=True,
        )
        tab._update_clock_labels(pm_data)
        assert "1:2" in tab._ratio_label.text()
        assert "#ffb74d" in tab._ratio_label.styleSheet()

    def test_show_uncalibrated_sets_dashes_and_label(self):
        tab = _make_headless_tab()
        pm_data = PMTableData(
            pm_table_version=0x99999999,
            is_calibrated=False,
            raw_floats=[0.0] * 100,
        )
        tab._show_uncalibrated(pm_data)
        assert "--" in tab._fclk_label.text()
        assert "--" in tab._uclk_label.text()
        assert "--" in tab._mclk_label.text()
        assert "--" in tab._vdd_label.text()
        assert "Uncalibrated" in tab._cal_label.text()
        assert "100 floats" in tab._cal_label.text()
        assert "#888" in tab._fclk_label.styleSheet()

    def test_set_clocks_unavailable_greys_out(self):
        tab = _make_headless_tab()
        tab._set_clocks_unavailable()
        assert "--" in tab._fclk_label.text()
        assert "--" in tab._uclk_label.text()
        assert "--" in tab._mclk_label.text()
        assert "--" in tab._ratio_label.text()
        assert "#888" in tab._fclk_label.styleSheet()
        assert "#888" in tab._ratio_label.styleSheet()
        assert tab._cal_label.text() == ""

    def test_update_live_data_calibrated_path(self):
        """Calibrated PM data updates clock labels with MHz values."""
        tab = _make_headless_tab()
        pm_data = PMTableData(
            fclk_mhz=2000.0,
            uclk_mhz=2000.0,
            mclk_mhz=3000.0,
            vddcr_soc_v=1.25,
            vdd_mem_v=1.395,
            pm_table_version=0x620205,
            is_calibrated=True,
        )
        tab._pm_reader.read.return_value = pm_data
        tab._update_live_data()
        assert "2000" in tab._fclk_label.text()
        assert "Verified" in tab._cal_label.text()

    def test_update_live_data_uncalibrated_path(self):
        """Uncalibrated PM data shows dashes and uncalibrated label."""
        tab = _make_headless_tab()
        pm_data = PMTableData(
            pm_table_version=0x99999999,
            is_calibrated=False,
            raw_floats=[0.0] * 50,
        )
        tab._pm_reader.read.return_value = pm_data
        tab._update_live_data()
        assert "--" in tab._fclk_label.text()
        assert "Uncalibrated" in tab._cal_label.text()

    def test_update_live_data_none_read_greys_out(self):
        """None from pm_reader.read() greys out all labels."""
        tab = _make_headless_tab()
        tab._pm_reader.read.return_value = None
        tab._update_live_data()
        assert "--" in tab._fclk_label.text()
        assert "#888" in tab._fclk_label.styleSheet()
        assert tab._cal_label.text() == ""

    def test_update_live_data_recovery_after_failure(self):
        """Labels recover after a failed read followed by a successful read."""
        tab = _make_headless_tab()
        # First: fail
        tab._pm_reader.read.return_value = None
        tab._update_live_data()
        assert "--" in tab._fclk_label.text()
        # Then: recover
        pm_data = PMTableData(
            fclk_mhz=2000.0,
            uclk_mhz=2000.0,
            mclk_mhz=3000.0,
            vdd_mem_v=1.395,
            pm_table_version=0x620205,
            is_calibrated=True,
        )
        tab._pm_reader.read.return_value = pm_data
        tab._update_live_data()
        assert "2000" in tab._fclk_label.text()
        assert "Verified" in tab._cal_label.text()
