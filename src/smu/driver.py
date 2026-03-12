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
from dataclasses import dataclass
from pathlib import Path

from .commands import CPUGeneration, SMUCommandSet, decode_co_arg, encode_co_arg

log = logging.getLogger(__name__)

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


@dataclass(frozen=True, slots=True)
class SMUResponse:
    success: bool
    args: tuple[int, ...]
    raw: bytes


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

    # ------------------------------------------------------------------
    # CO offset read/write
    # ------------------------------------------------------------------

    def get_co_offset(self, core_id: int) -> int | None:
        """Read the current CO offset for a physical core.

        CO values are VOLATILE and reset to zero on reboot.
        """
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

    def reset_all_co(self) -> bool:
        """Reset all CO offsets to 0. Only supported on some generations.

        CO values are VOLATILE — this resets them to 0 for the current
        session only.  On reboot they return to whatever the BIOS sets.
        """
        if self.dry_run:
            log.info("[DRY RUN] Would reset all CO offsets to 0 (not written)")
            return True

        if self.commands.reset_co_cmd is None:
            return False
        resp = self._send_command(self.commands.reset_co_cmd, (0,))
        return resp.success

    def get_all_co_offsets(self, num_cores: int) -> dict[int, int | None]:
        """Read CO offsets for all cores.

        CO values are VOLATILE — they reset to zero on reboot.
        """
        offsets: dict[int, int | None] = {}
        for core_id in range(num_cores):
            offsets[core_id] = self.get_co_offset(core_id)
        return offsets

    def get_boost_limit(self) -> int | None:
        """Read the boost frequency limit (MHz). Zen 4/5 only."""
        cmd = self.commands.get_boost_limit_cmd
        if cmd is None:
            return None
        resp = self._send_command(cmd)
        if not resp.success:
            return None
        return resp.args[0]

    def set_boost_limit(self, mhz: int) -> bool:
        """Set the boost frequency limit for all cores (MHz). Zen 4/5 only.

        Like CO offsets, this is VOLATILE and resets on reboot.
        """
        cmd = self.commands.set_boost_limit_cmd
        if cmd is None:
            return False
        if self.dry_run:
            log.info("[DRY RUN] Would set boost limit to %d MHz (not written)", mhz)
            return True
        resp = self._send_command(cmd, (mhz,))
        return resp.success
