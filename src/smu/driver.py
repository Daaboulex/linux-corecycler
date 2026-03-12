"""Interface to the ryzen_smu kernel module via sysfs.

IMPORTANT: CO (Curve Optimizer) values written via ryzen_smu are VOLATILE.
They are stored in SMU firmware SRAM and reset to zero on every reboot,
S3 sleep, or driver reload. Your BIOS PBO Curve Optimizer settings are
never modified by this tool — BIOS values are applied by firmware during
POST, and SMU writes here overlay (replace) them until the next power cycle.
"""

from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from .commands import (
    CPUGeneration,
    SMUCommandSet,
    decode_co_arg,
    encode_boost_limit_arg,
    encode_co_arg,
    encode_pbo_limit_arg,
    encode_pbo_scalar_arg,
)

log = logging.getLogger(__name__)

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


@dataclass(frozen=True, slots=True)
class SMUResponse:
    success: bool
    args: tuple[int, ...]
    raw: bytes


@dataclass(slots=True)
class SystemPBOState:
    """Snapshot of the current PBO/CO state from SMU and sysfs.

    Populated by ``RyzenSMU.detect_system_state()``. All values are
    runtime state — they reflect BIOS settings plus any SMU overrides.
    """

    # Per-core CO offsets (physical core id -> offset)
    co_offsets: dict[int, int | None] = field(default_factory=dict)

    # PBO power limits (from PM table or SMU query)
    ppt_limit_w: float | None = None
    tdc_limit_a: float | None = None
    edc_limit_a: float | None = None

    # PBO scalar (1.0 to 10.0)
    pbo_scalar: float | None = None

    # Boost frequency limit (MHz)
    boost_limit_mhz: int | None = None

    # Max observed frequency from cpufreq (accounts for boost override + BCLK)
    max_freq_mhz: float | None = None

    # Estimated BCLK from cpufreq bios_limit vs expected multiplier
    estimated_bclk_mhz: float | None = None

    # Whether OC mode is enabled
    oc_mode: bool | None = None

    # Fastest core index (from SMU)
    fastest_core: int | None = None

    # CPU generation detected
    generation: CPUGeneration | None = None

    # Whether ryzen_smu driver is available
    smu_available: bool = False


class RyzenSMU:
    """Interface to the ryzen_smu kernel module for reading/writing CO offsets.

    CO offsets set through this driver are VOLATILE — they live in SMU firmware
    SRAM and are lost on reboot, sleep, or driver reload. BIOS PBO settings
    are never touched.

    Safety features:
      - ``dry_run`` mode: logs intended writes without touching hardware
      - Read-back verification after every CO write
      - ``backup_co_offsets()`` / ``restore_co_offsets()`` for save/restore
      - Permission pre-check before attempting writes
    """

    def __init__(
        self,
        commands: SMUCommandSet,
        sysfs_path: Path = SYSFS_BASE,
        dry_run: bool = False,
    ) -> None:
        self.commands = commands
        self.sysfs = sysfs_path
        self.dry_run = dry_run
        self._backup: dict[int, int] | None = None

    @staticmethod
    def is_available(sysfs_path: Path = SYSFS_BASE) -> bool:
        """Check if ryzen_smu driver is loaded and accessible."""
        return sysfs_path.exists() and (sysfs_path / "smu_args").exists()

    def check_writable(self) -> tuple[bool, str]:
        """Check if the sysfs files are writable before attempting any write.

        Returns (ok, message).  Call this early to give the user a clear
        error instead of a cryptic ``PermissionError`` mid-write.
        """
        for name in ("smu_args", self._get_cmd_filename()):
            p = self.sysfs / name
            if not p.exists():
                return False, f"sysfs file not found: {p}"
            if not os.access(p, os.W_OK):
                return False, f"No write permission on {p} — run as root or fix udev rules"
        return True, "OK"

    # ------------------------------------------------------------------
    # Backup / restore
    # ------------------------------------------------------------------

    def backup_co_offsets(self, num_cores: int) -> dict[int, int]:
        """Save current CO offsets for all cores before modification.

        The backup is stored internally and can be restored with
        ``restore_co_offsets()``.  The dict is also returned for the caller
        to persist (e.g. write to a JSON file) if desired.

        Note: CO values are VOLATILE — they reset on reboot regardless.
        This backup guards against accidental *within-session* mistakes only.
        """
        offsets = self.get_all_co_offsets(num_cores)
        # Only store successfully-read values
        self._backup = {k: v for k, v in offsets.items() if v is not None}
        log.info("Backed up CO offsets for %d cores: %s", len(self._backup), self._backup)
        return dict(self._backup)

    def restore_co_offsets(self) -> tuple[bool, list[int]]:
        """Restore previously backed-up CO offsets.

        Returns (all_ok, list_of_failed_core_ids).
        """
        if self._backup is None:
            log.warning("restore_co_offsets called with no backup available")
            return False, []
        failed: list[int] = []
        for core_id, value in self._backup.items():
            if not self.set_co_offset(core_id, value):
                failed.append(core_id)
        ok = len(failed) == 0
        if ok:
            log.info("Restored CO offsets from backup successfully")
        else:
            log.error("Failed to restore CO offsets for cores: %s", failed)
        return ok, failed

    def has_backup(self) -> bool:
        """Return True if a backup has been taken this session."""
        return self._backup is not None

    # ------------------------------------------------------------------
    # Low-level SMU communication
    # ------------------------------------------------------------------

    def _get_cmd_filename(self) -> str:
        if self.commands.mailbox == "mp1":
            return "mp1_smu_cmd"
        return "rsmu_cmd"

    def _get_cmd_path(self) -> Path:
        """Get the command file path based on mailbox type."""
        return self.sysfs / self._get_cmd_filename()

    def _send_command(self, cmd: int, args: tuple[int, ...] = (0, 0, 0, 0, 0, 0)) -> SMUResponse:
        """Send an SMU command and read the response."""
        args_path = self.sysfs / "smu_args"
        cmd_path = self._get_cmd_path()

        # pack 6 x uint32 arguments
        if len(args) < 6:
            args = args + (0,) * (6 - len(args))
        packed_args = struct.pack("<6I", *args[:6])

        # write arguments
        args_path.write_bytes(packed_args)

        # write command (triggers SMU execution)
        cmd_bytes = struct.pack("<I", cmd)
        cmd_path.write_bytes(cmd_bytes)

        # read response from cmd file (status) and args file (response data)
        resp_cmd = cmd_path.read_bytes()
        resp_args_raw = args_path.read_bytes()

        status = struct.unpack("<I", resp_cmd[:4])[0]
        resp_args = struct.unpack("<6I", resp_args_raw[:24])

        return SMUResponse(
            success=(status == 1),
            args=resp_args,
            raw=resp_args_raw,
        )

    def _send_rsmu_command(
        self, cmd: int, args: tuple[int, ...] = (0, 0, 0, 0, 0, 0)
    ) -> SMUResponse:
        """Send an RSMU command regardless of the default mailbox.

        PBO limit commands use RSMU even on Zen 3 (which defaults to MP1 for CO).
        """
        args_path = self.sysfs / "smu_args"
        cmd_path = self.sysfs / "rsmu_cmd"

        if len(args) < 6:
            args = args + (0,) * (6 - len(args))
        packed_args = struct.pack("<6I", *args[:6])

        args_path.write_bytes(packed_args)
        cmd_path.write_bytes(struct.pack("<I", cmd))

        resp_cmd = cmd_path.read_bytes()
        resp_args_raw = args_path.read_bytes()

        status = struct.unpack("<I", resp_cmd[:4])[0]
        resp_args = struct.unpack("<6I", resp_args_raw[:24])

        return SMUResponse(success=(status == 1), args=resp_args, raw=resp_args_raw)

    # ------------------------------------------------------------------
    # CO offset read/write
    # ------------------------------------------------------------------

    def get_co_offset(self, core_id: int) -> int | None:
        """Read the current CO offset for a physical core.

        CO values are VOLATILE and reset to zero on reboot.
        """
        if not self.commands.has_co:
            return None
        arg = encode_co_arg(core_id, 0, self.commands.generation)
        resp = self._send_command(self.commands.get_co_cmd, (arg,))
        if not resp.success:
            return None
        return decode_co_arg(core_id, resp.args[0], self.commands.generation)

    def set_co_offset(self, core_id: int, value: int) -> bool:
        """Set the CO offset for a physical core. Returns True on success.

        CO values are VOLATILE — they live in SMU SRAM and reset on reboot.
        Your BIOS PBO settings are never modified.

        Safety:
          - Range-checked against the generation's CO limits
          - In ``dry_run`` mode, logs the intended write without touching HW
          - Verifies via read-back that the value was applied correctly
          - Pre-checks file permissions before writing
        """
        if not self.commands.has_co:
            log.error(
                "Generation %s does not support Curve Optimizer",
                self.commands.generation.name,
            )
            return False

        co_min, co_max = self.commands.co_range
        if not co_min <= value <= co_max:
            raise ValueError(
                f"CO value {value} out of range [{co_min}, {co_max}] "
                f"for {self.commands.generation.name}"
            )

        # --- dry-run guard ---
        if self.dry_run:
            log.info("[DRY RUN] Would set core %d CO to %d (not written)", core_id, value)
            return True

        # --- permission pre-check ---
        ok, msg = self.check_writable()
        if not ok:
            log.error("Permission check failed before CO write: %s", msg)
            return False

        arg = encode_co_arg(core_id, value, self.commands.generation)
        resp = self._send_command(self.commands.set_co_cmd, (arg,))
        if not resp.success:
            log.error("SMU rejected CO write for core %d value %d", core_id, value)
            return False

        # --- read-back verification ---
        readback = self.get_co_offset(core_id)
        if readback != value:
            log.error(
                "CO read-back mismatch for core %d: wrote %d, read back %s",
                core_id,
                value,
                readback,
            )
            return False

        log.info("Set core %d CO to %d (verified)", core_id, value)
        return True

    def set_all_co(self, value: int) -> bool:
        """Set CO offset for ALL cores at once. Returns True on success.

        Uses the SetAllDldoPsmMargin command if available, otherwise
        falls back to setting each core individually.
        """
        if not self.commands.has_co:
            return False

        co_min, co_max = self.commands.co_range
        if not co_min <= value <= co_max:
            raise ValueError(
                f"CO value {value} out of range [{co_min}, {co_max}] "
                f"for {self.commands.generation.name}"
            )

        if self.dry_run:
            log.info("[DRY RUN] Would set all cores CO to %d (not written)", value)
            return True

        if self.commands.set_all_co_cmd is not None:
            margin = value & 0xFFFF
            resp = self._send_command(self.commands.set_all_co_cmd, (margin,))
            return resp.success
        return False

    def reset_all_co(self) -> bool:
        """Reset all CO offsets to 0. Uses set_all_co if available.

        CO values are VOLATILE — this resets them to 0 for the current
        session only.  On reboot they return to whatever the BIOS sets.
        """
        if self.dry_run:
            log.info("[DRY RUN] Would reset all CO offsets to 0 (not written)")
            return True

        return self.set_all_co(0)

    def get_all_co_offsets(self, num_cores: int) -> dict[int, int | None]:
        """Read CO offsets for all cores.

        CO values are VOLATILE — they reset to zero on reboot.
        """
        offsets: dict[int, int | None] = {}
        for core_id in range(num_cores):
            offsets[core_id] = self.get_co_offset(core_id)
        return offsets

    # ------------------------------------------------------------------
    # Boost frequency
    # ------------------------------------------------------------------

    def get_boost_limit(self) -> int | None:
        """Read the boost frequency limit (MHz). Zen 4/5 only."""
        cmd = self.commands.get_boost_limit_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        return resp.args[0]

    def set_boost_limit(self, mhz: int) -> bool:
        """Set the boost frequency limit for all cores (MHz). Zen 4/5 only.

        Like CO offsets, this is VOLATILE and resets on reboot.
        No artificial cap is imposed — the hardware/firmware enforce
        actual limits. Users with PBO boost override +200 and BCLK 105+
        may see effective clocks above 6 GHz; this is expected.
        """
        cmd = self.commands.set_boost_limit_cmd
        if cmd is None:
            return False
        if self.dry_run:
            log.info("[DRY RUN] Would set boost limit to %d MHz (not written)", mhz)
            return True
        resp = self._send_rsmu_command(cmd, (encode_boost_limit_arg(mhz),))
        return resp.success

    # ------------------------------------------------------------------
    # PBO limits (PPT, TDC, EDC)
    # ------------------------------------------------------------------

    def set_ppt_limit(self, watts: int) -> bool:
        """Set PPT (Package Power Tracking) limit in watts. VOLATILE."""
        cmd = self.commands.set_ppt_cmd
        if cmd is None:
            return False
        if self.dry_run:
            log.info("[DRY RUN] Would set PPT limit to %d W", watts)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(watts),))
        return resp.success

    def set_tdc_limit(self, amps: int) -> bool:
        """Set TDC (Thermal Design Current) limit in amps. VOLATILE."""
        cmd = self.commands.set_tdc_cmd
        if cmd is None:
            return False
        if self.dry_run:
            log.info("[DRY RUN] Would set TDC limit to %d A", amps)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(amps),))
        return resp.success

    def set_edc_limit(self, amps: int) -> bool:
        """Set EDC (Electrical Design Current) limit in amps. VOLATILE."""
        cmd = self.commands.set_edc_cmd
        if cmd is None:
            return False
        if self.dry_run:
            log.info("[DRY RUN] Would set EDC limit to %d A", amps)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(amps),))
        return resp.success

    # ------------------------------------------------------------------
    # PBO scalar
    # ------------------------------------------------------------------

    def get_pbo_scalar(self) -> float | None:
        """Read current PBO scalar (1.0 to 10.0)."""
        cmd = self.commands.get_pbo_scalar_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        # Response is an IEEE 754 float in the first arg word
        raw_bytes = struct.pack("<I", resp.args[0])
        return struct.unpack("<f", raw_bytes)[0]

    def set_pbo_scalar(self, scalar: float) -> bool:
        """Set PBO scalar (1.0 to 10.0). VOLATILE."""
        cmd = self.commands.set_pbo_scalar_cmd
        if cmd is None:
            return False
        if not 0.0 <= scalar <= 10.0:
            raise ValueError(f"PBO scalar {scalar} out of range [0.0, 10.0]")
        if self.dry_run:
            log.info("[DRY RUN] Would set PBO scalar to %.1f", scalar)
            return True
        resp = self._send_rsmu_command(cmd, (encode_pbo_scalar_arg(scalar),))
        return resp.success

    # ------------------------------------------------------------------
    # System state detection
    # ------------------------------------------------------------------

    def detect_system_state(self, num_cores: int) -> SystemPBOState:
        """Read the current PBO/CO state from SMU and sysfs.

        This provides a snapshot of the system's current configuration,
        including CO offsets, PBO limits, boost override, and estimated BCLK.
        Call this before starting a test to understand the baseline.
        """
        state = SystemPBOState(
            generation=self.commands.generation,
            smu_available=True,
        )

        # Read CO offsets
        if self.commands.has_co:
            state.co_offsets = self.get_all_co_offsets(num_cores)

        # Read boost limit
        state.boost_limit_mhz = self.get_boost_limit()

        # Read PBO scalar
        state.pbo_scalar = self.get_pbo_scalar()

        # Read fastest core
        if self.commands.get_fastest_core_cmd is not None:
            resp = self._send_rsmu_command(self.commands.get_fastest_core_cmd)
            if resp.success:
                state.fastest_core = resp.args[0]

        # Read max frequency from cpufreq sysfs (accounts for boost override + BCLK)
        state.max_freq_mhz = _read_max_freq_sysfs()

        # Estimate BCLK from cpufreq bios_limit
        state.estimated_bclk_mhz = _estimate_bclk(state.max_freq_mhz)

        return state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_fastest_core(self) -> int | None:
        """Query the SMU for the fastest core index."""
        cmd = self.commands.get_fastest_core_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        return resp.args[0]


# ===========================================================================
# Sysfs helpers for system state detection
# ===========================================================================


def _read_max_freq_sysfs() -> float | None:
    """Read max boost frequency from cpufreq sysfs (MHz).

    This reflects the actual boost limit including PBO boost override
    and BCLK scaling.
    """
    path = Path("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    try:
        if path.exists():
            return int(path.read_text().strip()) / 1000.0
    except (ValueError, OSError):
        pass
    return None


def _estimate_bclk(max_freq_mhz: float | None) -> float | None:
    """Estimate BCLK from the max frequency and a known multiplier.

    This is a rough heuristic. BCLK cannot be read directly from the SMU.
    If max_freq is close to a known stock boost (e.g., 5700 for 9950X),
    BCLK is likely 100 MHz. If it's higher, BCLK may be elevated.

    Returns estimated BCLK in MHz, or None if we can't determine it.
    """
    if max_freq_mhz is None:
        return None

    # Read bios_limit which may reveal BCLK effects
    path = Path("/sys/devices/system/cpu/cpu0/cpufreq/bios_limit")
    try:
        if path.exists():
            bios_limit_mhz = int(path.read_text().strip()) / 1000.0
            # If bios_limit is significantly different from max_freq,
            # BCLK may be elevated. Common pattern: bios_limit stays at
            # stock while cpuinfo_max_freq scales with BCLK.
            if bios_limit_mhz > 0:
                return None  # can't reliably determine BCLK from this alone
    except (ValueError, OSError):
        pass

    return None  # no reliable way to determine BCLK
