"""PM (Power Monitoring) table reader for live telemetry."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


@dataclass(slots=True)
class PMTableData:
    """Parsed PM table telemetry values."""
    # per-core data
    core_frequency_mhz: dict[int, float] = field(default_factory=dict)
    core_voltage_v: dict[int, float] = field(default_factory=dict)
    core_temperature_c: dict[int, float] = field(default_factory=dict)
    core_power_w: dict[int, float] = field(default_factory=dict)
    core_c0_residency: dict[int, float] = field(default_factory=dict)

    # package-level
    package_power_w: float = 0.0
    soc_power_w: float = 0.0
    ppt_limit_w: float = 0.0
    tdc_limit_a: float = 0.0
    edc_limit_a: float = 0.0
    ppt_value_w: float = 0.0
    tdc_value_a: float = 0.0
    edc_value_a: float = 0.0
    tctl_c: float = 0.0
    tdie_c: float = 0.0

    raw_floats: list[float] = field(default_factory=list)


class PMTableReader:
    """Reads the SMU PM table for live telemetry data.

    NOTE: PM table layouts vary by CPU generation and SMU firmware version.
    The offsets below are approximate and may need adjustment. The raw float
    array is always available for manual inspection.
    """

    def __init__(self, num_cores: int = 16, sysfs_path: Path = SYSFS_BASE) -> None:
        self.num_cores = num_cores
        self.sysfs = sysfs_path

    def is_available(self) -> bool:
        pm_path = self.sysfs / "pm_table"
        return pm_path.exists()

    def read(self) -> PMTableData | None:
        """Read and parse the PM table. Returns None if unavailable."""
        pm_path = self.sysfs / "pm_table"
        if not pm_path.exists():
            return None

        try:
            raw = pm_path.read_bytes()
        except OSError:
            return None

        if len(raw) < 4:
            return None

        # PM table is an array of 32-bit floats
        num_floats = len(raw) // 4
        floats = list(struct.unpack(f"<{num_floats}f", raw[: num_floats * 4]))

        data = PMTableData(raw_floats=floats)

        # attempt to parse known offsets
        # these are APPROXIMATE for Granite Ridge — may need calibration
        self._parse_granite_ridge(data, floats)

        return data

    def _parse_granite_ridge(self, data: PMTableData, floats: list[float]) -> None:
        """Parse PM table with Granite Ridge (Zen 5) approximate offsets."""
        if len(floats) < 200:
            return

        # package telemetry (approximate offsets — verify with ryzen_monitor_ng)
        try:
            data.ppt_limit_w = floats[0]
            data.ppt_value_w = floats[1]
            data.tdc_limit_a = floats[2]
            data.tdc_value_a = floats[3]
            data.edc_limit_a = floats[4]
            data.edc_value_a = floats[5]

            data.tctl_c = floats[10] if len(floats) > 10 else 0.0
            data.tdie_c = floats[11] if len(floats) > 11 else 0.0

            data.package_power_w = floats[26] if len(floats) > 26 else 0.0
            data.soc_power_w = floats[28] if len(floats) > 28 else 0.0

            # per-core data typically starts around offset 100+
            # each core has ~10 float fields (freq, voltage, power, temp, residency, ...)
            core_base = 100
            core_stride = 10

            for core in range(min(self.num_cores, 32)):
                offset = core_base + core * core_stride
                if offset + core_stride > len(floats):
                    break
                data.core_frequency_mhz[core] = floats[offset]
                data.core_voltage_v[core] = floats[offset + 1]
                data.core_power_w[core] = floats[offset + 2]
                data.core_temperature_c[core] = floats[offset + 3]
                data.core_c0_residency[core] = floats[offset + 4]

        except IndexError:
            pass  # PM table smaller than expected, partial data is fine
