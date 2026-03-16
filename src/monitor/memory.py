"""DIMM and memory monitoring — dmidecode + SPD5118 hwmon."""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

HWMON_BASE = Path("/sys/class/hwmon")


@dataclass(frozen=True, slots=True)
class DIMMInfo:
    """Information about a single DIMM from dmidecode."""

    locator: str = ""
    bank_locator: str = ""
    size_gb: int = 0
    mem_type: str = ""
    speed_mt: int = 0
    configured_speed_mt: int = 0
    manufacturer: str = ""
    part_number: str = ""
    serial_number: str = ""
    rank: int = 0
    form_factor: str = ""
    configured_voltage: float = 0.0
    min_voltage: float = 0.0
    max_voltage: float = 0.0
    data_width: int = 0
    total_width: int = 0


def parse_dmidecode_output(text: str) -> list[DIMMInfo]:
    """Parse dmidecode -t memory output into DIMMInfo list."""
    dimms: list[DIMMInfo] = []
    blocks = re.split(r"Handle 0x[\dA-Fa-f]+, DMI type 17", text)

    for block in blocks[1:]:
        fields: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip()] = val.strip()

        size_str = fields.get("Size", "")
        size_gb = 0
        size_mb = 0
        # dmidecode 3.6 uses "GB"/"MB", dmidecode 3.7+ uses "GiB"/"MiB"
        size_num = re.match(r"(\d+)\s*(GiB|GB|MiB|MB)", size_str)
        if size_num:
            val = int(size_num.group(1))
            unit = size_num.group(2)
            if unit in ("GB", "GiB"):
                size_gb = val
            elif unit in ("MB", "MiB"):
                size_mb = val
                size_gb = (val + 1023) // 1024

        if size_gb == 0 and size_mb == 0:
            continue  # truly empty slot

        speed = 0
        speed_str = fields.get("Speed", "")
        m = re.match(r"(\d+)", speed_str)
        if m:
            speed = int(m.group(1))

        conf_speed = 0
        conf_speed_str = fields.get("Configured Memory Speed", "")
        m = re.match(r"(\d+)", conf_speed_str)
        if m:
            conf_speed = int(m.group(1))

        rank = 0
        rank_str = fields.get("Rank", "")
        if rank_str.isdigit():
            rank = int(rank_str)

        def _parse_voltage(s: str) -> float:
            m = re.match(r"([\d.]+)", s)
            return float(m.group(1)) if m else 0.0

        dimms.append(DIMMInfo(
            locator=fields.get("Locator", ""),
            bank_locator=fields.get("Bank Locator", ""),
            size_gb=size_gb,
            mem_type=fields.get("Type", ""),
            speed_mt=speed,
            configured_speed_mt=conf_speed,
            manufacturer=fields.get("Manufacturer", ""),
            part_number=fields.get("Part Number", "").strip(),
            serial_number=fields.get("Serial Number", ""),
            rank=rank,
            form_factor=fields.get("Form Factor", ""),
            configured_voltage=_parse_voltage(fields.get("Configured Voltage", "")),
            min_voltage=_parse_voltage(fields.get("Minimum Voltage", "")),
            max_voltage=_parse_voltage(fields.get("Maximum Voltage", "")),
            data_width=int(fields.get("Data Width", "0").split()[0]) if fields.get("Data Width") else 0,
            total_width=int(fields.get("Total Width", "0").split()[0]) if fields.get("Total Width") else 0,
        ))

    return dimms


def read_dimm_info() -> list[DIMMInfo]:
    """Read DIMM info via dmidecode. Requires root."""
    try:
        result = subprocess.run(
            ["dmidecode", "-t", "memory"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            log.warning("dmidecode exited with code %d: %s", result.returncode, result.stderr.strip())
        # Parse even on non-zero exit — some systems return 1 but still output data
        dimms = parse_dmidecode_output(result.stdout)
        if not dimms and result.stdout:
            log.debug("dmidecode produced output but no DIMMs parsed (stdout length: %d)", len(result.stdout))
        return dimms
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        log.debug("dmidecode not available: %s", e)
    return []


class SPD5118Reader:
    """Read DDR5 DIMM temperatures from SPD5118 hwmon devices."""

    def __init__(self, hwmon_base: Path = HWMON_BASE) -> None:
        self._devices: list[Path] = []
        self._scan(hwmon_base)

    def _scan(self, hwmon_base: Path) -> None:
        if not hwmon_base.exists():
            return
        for hwmon_dir in sorted(hwmon_base.iterdir()):
            name_file = hwmon_dir / "name"
            if name_file.exists():
                try:
                    name = name_file.read_text().strip()
                except OSError:
                    continue
                if name == "spd5118":
                    self._devices.append(hwmon_dir)

    def is_available(self) -> bool:
        return len(self._devices) > 0

    def read_temperatures(self) -> list[float]:
        """Read temperature from each SPD5118 device (Celsius)."""
        temps: list[float] = []
        for dev in self._devices:
            temp_file = dev / "temp1_input"
            if temp_file.exists():
                try:
                    raw = int(temp_file.read_text().strip())
                    temp_c = raw / 1000.0
                    if -40.0 <= temp_c <= 125.0:  # SPD5118 sensor range
                        temps.append(temp_c)
                except (ValueError, OSError):
                    pass
        return temps
