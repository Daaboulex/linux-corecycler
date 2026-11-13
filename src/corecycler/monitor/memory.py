"""DIMM and memory monitoring — dmidecode + SPD5118 hwmon."""

from __future__ import annotations

import logging
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

from corecycler.config import tools

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


DDR5_ROUNDING_TOLERANCE_PS = 30
SPD_DRAM_TYPE_DDR5 = 0x12


@dataclass(frozen=True, slots=True)
class SPDTimingData:
    """DDR5 timing parameters decoded from SPD EEPROM."""

    tCK_ps: int = 0
    freq_mt: int = 0
    tCL: int = 0
    tRCD: int = 0
    tRP: int = 0
    tRAS: int = 0
    tRC: int = 0
    tWR_ns: float = 0.0
    tRFC1_ns: int = 0
    tRFCsb_ns: int = 0
    source: str = ""


def decode_spd_timings(data: bytes, source: str = "") -> SPDTimingData | None:
    """Decode DDR5 SPD primary and secondary timings from raw EEPROM bytes.

    Returns None if data is too short, not DDR5, or has zero clock period.
    """
    if len(data) < 48:
        return None
    if data[2] != SPD_DRAM_TYPE_DDR5:
        return None

    tCK_ps = struct.unpack_from("<H", data, 20)[0]
    if tCK_ps == 0:
        return None

    freq_mt = int((1.0 / tCK_ps * 2e6 + 50) / 100) * 100

    def _ps_to_ck(ps_val: int) -> int:
        return (ps_val + tCK_ps - DDR5_ROUNDING_TOLERANCE_PS) // tCK_ps

    tCL = _ps_to_ck(struct.unpack_from("<H", data, 30)[0])
    tCL += tCL % 2
    tRCD = _ps_to_ck(struct.unpack_from("<H", data, 32)[0])
    tRP = _ps_to_ck(struct.unpack_from("<H", data, 34)[0])
    tRAS = _ps_to_ck(struct.unpack_from("<H", data, 36)[0])
    tRC = _ps_to_ck(struct.unpack_from("<H", data, 38)[0])

    tWR_ps = struct.unpack_from("<H", data, 40)[0]
    tWR_ns = tWR_ps / 1000.0

    tRFC1_ns = struct.unpack_from("<H", data, 42)[0]
    tRFCsb_ns = struct.unpack_from("<H", data, 46)[0]

    return SPDTimingData(
        tCK_ps=tCK_ps,
        freq_mt=freq_mt,
        tCL=tCL,
        tRCD=tRCD,
        tRP=tRP,
        tRAS=tRAS,
        tRC=tRC,
        tWR_ns=tWR_ns,
        tRFC1_ns=tRFC1_ns,
        tRFCsb_ns=tRFCsb_ns,
        source=source,
    )


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
            continue

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

        dimms.append(
            DIMMInfo(
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
                data_width=int(m.group(1)) if (m := re.match(r"(\d+)", fields.get("Data Width", ""))) else 0,
                total_width=int(m.group(1)) if (m := re.match(r"(\d+)", fields.get("Total Width", ""))) else 0,
            )
        )

    return dimms


DMIDECODE_TIMEOUT_S = 5


def read_dimm_info() -> tuple[list[DIMMInfo], str]:
    """The DIMMs dmidecode lists, parsed even when it exits non-zero, and when it lists none, why."""
    try:
        result = subprocess.run(
            [tools.command_name("dmidecode"), "-t", "memory"],
            capture_output=True,
            text=True,
            timeout=DMIDECODE_TIMEOUT_S,
        )
    except FileNotFoundError:
        return [], "dmidecode is not installed"
    except subprocess.TimeoutExpired:
        return [], f"dmidecode did not answer in {DMIDECODE_TIMEOUT_S} s"
    except OSError as e:
        return [], f"dmidecode could not run: {e}"
    dimms = parse_dmidecode_output(result.stdout)
    if dimms:
        return dimms, ""
    said = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
    if result.returncode != 0:
        return [], f"dmidecode exited {result.returncode}" + (f": {said}" if said else "")
    return [], "dmidecode listed no DIMMs" + (f": {said}" if said else "")


SPD5118_RANGE_C = (-40.0, 125.0)


def _spd_address(hwmon_dir: Path) -> str:
    """The SPD hub's i2c address, 0x50 for the device 15-0050, or the hwmon name when the device cannot be read."""
    try:
        return f"0x{int((hwmon_dir / 'device').resolve().name.partition('-')[2], 16):02x}"
    except OSError, ValueError:
        return hwmon_dir.name


def _spd_temperature(path: Path) -> float | None:
    try:
        temp_c = int(path.read_text().strip()) / 1000.0
    except ValueError, OSError:
        return None
    low, high = SPD5118_RANGE_C
    return temp_c if low <= temp_c <= high else None


class SPD5118Reader:
    """Read DDR5 DIMM temperatures from SPD5118 hwmon devices."""

    def __init__(self, hwmon_base: Path = HWMON_BASE) -> None:
        self._devices: list[Path] = []
        self.addresses: list[str] = []
        self._eeproms: list[tuple[str, Path]] = []
        self._spd_timings: SPDTimingData | None = None
        self._spd_loaded: bool = False
        self._scan(hwmon_base)

    def _scan(self, hwmon_base: Path) -> None:
        if not hwmon_base.exists():
            return
        found = []
        for hwmon_dir in hwmon_base.iterdir():
            try:
                name = (hwmon_dir / "name").read_text().strip()
            except OSError:
                continue
            if name == "spd5118":
                found.append((_spd_address(hwmon_dir), hwmon_dir))
        for address, hwmon_dir in sorted(found):
            self._devices.append(hwmon_dir)
            self.addresses.append(address)
            try:
                eeprom_path = (hwmon_dir / "device").resolve() / "eeprom"
            except OSError:
                continue
            if eeprom_path.exists():
                self._eeproms.append((address, eeprom_path))

    @property
    def spd_timings(self) -> SPDTimingData | None:
        """DDR5 timing data from first available EEPROM. Cached at first access."""
        if self._spd_loaded:
            return self._spd_timings
        self._spd_loaded = True
        for address, eeprom_path in self._eeproms:
            try:
                data = eeprom_path.read_bytes()
                result = decode_spd_timings(data, source=address)
                if result is not None:
                    self._spd_timings = result
                    return self._spd_timings
            except OSError:
                log.debug("Failed to read SPD EEPROM: %s", eeprom_path)
        return None

    def is_available(self) -> bool:
        return len(self._devices) > 0

    @property
    def eeprom_count(self) -> int:
        return len(self._eeproms)

    def read_temperatures(self) -> list[float | None]:
        """One reading per SPD5118 device, in device order (Celsius); None where it could not be read."""
        return [_spd_temperature(dev / "temp1_input") for dev in self._devices]
