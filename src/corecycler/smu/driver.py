"""Interface to the ryzen_smu kernel module via sysfs.

IMPORTANT: CO (Curve Optimizer) values written via ryzen_smu are VOLATILE.
They are stored in SMU firmware SRAM and reset to zero on every reboot,
S3 sleep, or driver reload. Your BIOS PBO Curve Optimizer settings are
never modified by this tool — BIOS values are applied by firmware during
POST, and SMU writes here overlay (replace) them until the next power cycle.
"""

from __future__ import annotations

import fcntl
import logging
import os
import struct
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .commands import (
    SMUCommandSet,
    decode_co_arg,
    encode_boost_limit_arg,
    encode_co_arg,
    encode_pbo_limit_arg,
    encode_pbo_scalar_arg,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

log = logging.getLogger(__name__)

SYSFS_BASE = Path("/sys/kernel/ryzen_smu_drv")


def smu_problem(sysfs: Path = SYSFS_BASE) -> str | None:
    args = sysfs / "smu_args"
    try:
        os.close(os.open(args, os.O_WRONLY))
    except OSError as exc:
        return f"{args}: {exc.strerror}"
    return None


_PBO_LIMIT_MIN: int = 1
_PBO_LIMIT_MAX: int = 2000

_SLOTS_PER_CCD: int = 8
_CCD_POSITIONS: int = 8

# Per-CCD fuse stride, from ZenStates-Core Cpu.cs and ryzen_monitor.
_CCD_FUSE_SHIFT: int = 25


def _check_pbo_limit(name: str, value: int) -> None:
    """Fail closed on a malformed PBO limit before it is encoded and written."""
    if not _PBO_LIMIT_MIN <= value <= _PBO_LIMIT_MAX:
        raise ValueError(f"{name} limit {value} out of sane range [{_PBO_LIMIT_MIN}, {_PBO_LIMIT_MAX}]")


SHAPER_BAND_NAMES = ("Min", "Low", "Medium", "High", "Max")
SHAPER_MARGINS = (-50, 30)
SHAPER_SET_BIT = 0x80


class CoreMapError(Exception):
    """The OS-core to SMU-slot mapping could not be discovered.

    Raised internally during ``RyzenSMU.set_topology`` and stored as
    ``core_map_error``; per-core CO operations then refuse instead of
    addressing slots that may belong to different or fused-off cores.
    """


def core_map_blocked(smu) -> str | None:
    """The SMU's core-map failure reason, or None when per-core CO is usable.

    Accepts None and duck-typed test doubles: only a real, non-empty ``str``
    (the type of ``RyzenSMU.core_map_error``) blocks.
    """
    err = getattr(smu, "core_map_error", None)
    if isinstance(err, str) and err:
        return err
    return None


def physical_ccds(smu: RyzenSMU | None, topology) -> dict[int, int | None]:
    """Each topology core's physical CCD, the CCD a TccdN temperature label numbers from 1.

    The topology ranks CCDs by L3 id, which is the physical number unless the generation names its CCD-disable
    fuses; there the driver's core map holds it, and a core a present map does not hold is None. Without a driver
    or with its map refused the topology's CCD stands: on Vermeer's two CCD positions it is either right or names a
    sensor hwmon does not expose, never another CCD's.
    """
    ranked = {core: info.ccd for core, info in topology.cores.items()}
    mapped = None if smu is None or smu.commands.ccd_fuse_addr is None else smu.core_map
    if mapped is None:
        return ranked
    return {core: mapped[core][0] if core in mapped else None for core in ranked}


@dataclass(frozen=True, slots=True)
class SMUResponse:
    success: bool
    args: tuple[int, ...]
    raw: bytes

    smu_available: bool = False


class RyzenSMU:
    """Interface to the ryzen_smu kernel module for reading/writing CO offsets.

    CO offsets set through this driver are VOLATILE — they live in SMU firmware
    SRAM and are lost on reboot, sleep, or driver reload. BIOS PBO settings
    are never touched.

    Safety features:
      - Read-back verification after every CO write
      - ``backup_co_offsets()`` / ``restore_co_offsets()`` for save/restore
      - Permission pre-check before attempting writes
    """

    def __init__(
        self,
        commands: SMUCommandSet,
        sysfs_path: Path = SYSFS_BASE,
    ) -> None:
        self.commands = commands
        self.sysfs = sysfs_path
        self._smu_lock = threading.Lock()
        self._backup: dict[int, int] | None = None
        self._legacy_ccd_by_core: dict[int, int] | None = None
        self._ccd_slot_by_core: dict[int, tuple[int, int]] | None = None
        self._ccd_slot_error: str | None = None
        self._known_core_ids: list[int] | None = None
        self._offline_hint = ""

    def set_topology(self, topology) -> None:
        """Discover how OS core ids map onto SMU (CCD, physical slot) addresses.

        The SMU addresses cores by physical slot within a CCD, counting
        fused-off cores. The kernel's /proc/cpuinfo "core id" (the APIC-ID
        decode from CPUID Fn8000_001E) is that physical numbering on most
        machines -- a fused-off core leaves a hole -- but some BIOS/AGESA
        builds renumber core ids contiguously on harvested parts instead
        (field-reported on a 5600X, issue #11), and the ids alone then hide
        which slots are fused off.

        Mapping rules, per L3 group (falling back to ``core_id // 8`` grouping
        when L3 info is absent):
          - The topology numbers CCDs by the rank of their L3 id, so a generation that names its
            CCD-disable fuses (Vermeer) has them read first and each group
            mapped onto its physical CCD, which every core-fuse read and CO
            command then uses: a 5600X with physical CCD 0 fused off shows its
            CCD 1 as group 0 (PR #23).
          - A full 8-core group maps to identity slots under any numbering
            scheme -- no core-fuse read.
          - A group with a hole INTERNAL to its own 8-slot window proves the
            physical numbering: slot = ``core_id % 8``, CCD from L3. Only
            internal holes count -- a hole BETWEEN windows proves nothing,
            because a per-CCD-compacting firmware keeps the window stride --
            and only while every present CPU is online, because a
            fully-offlined core fakes a hole.
          - Every other harvested group is ambiguous (its slots form the
            0..n-1 prefix, it spans windows, or its holes are untrusted): the
            CCD's core-disable fuse is read over SMN and the group's cores map
            onto the live slots in ascending order (the same order-preserving
            mapping the Windows tools build from that fuse). The CO read
            cannot stand in for it -- it answers on every in-range slot,
            fused-off ones included (issue #11).
          - A fuse that cannot be read or disagrees with the OS core count stores
            ``core_map_error`` and every per-core CO operation refuses --
            failing closed beats tuning a different core than the one under
            test. While discovery runs, ``core_map_error`` holds a sentinel so
            a concurrent reader refuses rather than falling open to legacy
            addressing.

        The whole discovery is gated on the generation's declared
        ``uniform_8core_ccds`` (classic 8-slot CCD/CCX layout, verified): a
        generation without it, and any L3 group larger than 8 cores (dense
        Zen 4c/5c CCX dies), keeps the legacy core_id-derived addressing
        bit-for-bit.
        """
        self._legacy_ccd_by_core = {}
        for core_id, core_info in topology.cores.items():
            if core_info.ccd is not None:
                self._legacy_ccd_by_core[core_id] = core_info.ccd
        self._ccd_slot_by_core = None
        self._ccd_slot_error = "core map discovery in progress"
        ids = sorted(topology.cores)
        self._known_core_ids = ids or None
        baseline = self._co_baseline_problem() if ids and self.commands.probe_co_baseline else None
        if baseline is not None:
            self._ccd_slot_error = baseline
            log.error("SMU core mapping unavailable: %s", baseline)
            return
        if not ids or not self.commands.has_co or not self.commands.uniform_8core_ccds:
            self._ccd_slot_error = None
            return
        groups = self._group_cores(topology, ids)
        if groups is None:
            self._ccd_slot_error = None
            return
        fully_online = getattr(topology, "cpus_all_online", True) is not False
        self._offline_hint = "" if fully_online else " (some present CPUs are offline — online all cores for CO tuning)"
        core_map: dict[int, tuple[int, int]] = {}
        try:
            physical = self._physical_ccds(groups)
            for group in sorted(groups):
                encode_ccd = physical[group]
                group_ids = groups[group]
                slots = self._derive_group_slots(group_ids, fully_online)
                if slots is None:
                    slots = self._fuse_group_slots(encode_ccd, len(group_ids))
                core_map.update((cid, (encode_ccd, slot)) for cid, slot in zip(group_ids, slots, strict=True))
        except CoreMapError as exc:
            self._ccd_slot_error = str(exc)
            log.error("SMU core mapping unavailable: %s", exc)
            return
        self._ccd_slot_by_core = core_map
        self._ccd_slot_error = None

    def _co_baseline_problem(self) -> str | None:
        """Why this generation's CO read cannot be trusted as a baseline, or None when one read is in range.

        An HX 370 on BIOS FA608WV.309 acknowledges the Strix Point CO read and returns 600 for every core (PR #21),
        so a CO write is sent whose read-back can never match and the backup can never be restored; one read of the
        lowest known core, addressed as get_co_offset would address it, decides. A read the pinned ryzen_smu reports
        as failed or an answer outside the CO range refuses; a read the firmware rejects comes back from that driver
        as 0 and passes.
        """
        ok, msg = self.check_writable()
        if not ok:
            return f"the {self.commands.generation.name} CO read has to be checked first: {msg}"
        core = self._known_core_ids[0]
        ccd = self._legacy_ccd_by_core.get(core)
        response = self._send_get_co(encode_co_arg(core, 0, self.commands.generation, ccd=ccd, slot=None))
        if not response.success:
            return (
                f"the {self.commands.generation.name} CO read failed -- per-core CO stays disabled until its "
                f"readback is verified"
            )
        value = decode_co_arg(core, response.args[0], self.commands.generation)
        low, high = self.commands.co_range
        if not low <= value <= high:
            return (
                f"the {self.commands.generation.name} CO read returned {value}, outside {low}..{high} -- per-core "
                f"CO stays disabled until its readback is verified; please report this output"
            )
        return None

    @staticmethod
    def _derive_group_slots(group_ids: list[int], holes_trusted: bool) -> list[int] | None:
        """Slots this group's numbering proves by itself; None means read the fuse.

        A full 8-core group is identity under any numbering scheme. Only a
        hole INTERNAL to the group's own 8-slot window proves physical
        numbering, and only while every present CPU is online (an offlined
        core fakes a hole). A 0..n-1 prefix is ambiguous, and a group spanning
        windows is renumbered -- a per-CCD-compacting firmware keeps the
        window stride, so a hole BETWEEN windows proves nothing.
        """
        windows = {cid // _SLOTS_PER_CCD for cid in group_ids}
        if len(windows) != 1:
            return None
        rel = [cid % _SLOTS_PER_CCD for cid in group_ids]
        if rel == list(range(len(group_ids))):
            return rel if len(group_ids) == _SLOTS_PER_CCD else None
        return rel if holes_trusted else None

    @staticmethod
    def _group_cores(topology, ids: list[int]) -> dict[int, list[int]] | None:
        """Group core ids by encode-CCD index; None for unmodeled dense layouts.

        L3-detected CCDs are used when every core has one (the CCD index the CO
        argument encodes); otherwise the ``core_id // 8`` window. Ids stay
        ascending within each group.
        """
        by_l3 = all(topology.cores[cid].ccd is not None for cid in ids)
        groups: dict[int, list[int]] = {}
        for cid in ids:
            key = topology.cores[cid].ccd if by_l3 else cid // _SLOTS_PER_CCD
            groups.setdefault(key, []).append(cid)
        if any(len(members) > _SLOTS_PER_CCD for members in groups.values()):
            return None
        return groups

    def check_smn_readable(self) -> tuple[bool, str]:
        """Whether the SMN node can be driven at all. Returns (ok, message).

        Reading an SMN register means WRITING its address to ``smn`` and
        reading the result back, so an SMN read needs write permission on a
        file ryzen_smu ships root-only.
        """
        path = self.sysfs / "smn"
        if not path.exists():
            return False, f"sysfs file not found: {path}"
        if not os.access(path, os.W_OK):
            return False, (
                f"no write permission on {path} — an SMN read is a write of the "
                f"address, and ryzen_smu ships this file root-only; grant it to "
                f"the corecycler group like the other SMU files, or run as root"
            )
        return True, "OK"

    def read_smn(self, address: int) -> int | None:
        """Read one 32-bit SMN register, or None if the read did not happen.

        The ryzen_smu ``smn`` node takes the address as a 4-byte write and
        hands the value back on the next read. A failed read leaves the
        driver's result register untouched, so a failure here must never
        decay into the previous read's value: it returns None.
        """
        path = self.sysfs / "smn"
        try:
            with self._held(path):
                path.write_bytes(struct.pack("<I", address))
                return struct.unpack("<I", path.read_bytes()[:4])[0]
        except (OSError, struct.error) as exc:
            log.debug("SMN read of %#010x failed: %s", address, exc)
            return None

    @contextmanager
    def _held(self, anchor: Path) -> Iterator[None]:
        with self._smu_lock, anchor.open("rb") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _physical_ccds(self, groups: dict[int, list[int]]) -> dict[int, int]:
        """Each OS L3 group's physical CCD: identity unless the generation names its CCD-disable fuses.

        The fuses are decoded as ryzen_smu's get_fuse_topology does: the enabled CCDs in bits 29:22 of the first
        word, CCDs 0 and 1 disabled by its bits 31:30 and CCDs 2 to 7 by bits 5:0 of the next. Groups map onto the
        active CCDs in ascending order; an unreadable fuse or a count that differs from the groups raises
        CoreMapError.
        """
        addr = self.commands.ccd_fuse_addr
        if addr is None:
            return {group: group for group in groups}
        ok, msg = self.check_smn_readable()
        if not ok:
            raise CoreMapError(
                f"Linux may number this CPU's CCDs apart from the SMU, so the CCD-disable fuses have to be read: "
                f"{msg} -- per-core CO stays disabled until then"
            )
        present, down = self.read_smn(addr), self.read_smn(addr + 4)
        if present is None or down is None:
            raise CoreMapError(
                "the CCD-disable fuse read failed on an otherwise writable smn file -- per-core CO stays disabled "
                "instead of writing to the wrong CCD; please report this output"
            )
        disabled = ((down & 0x3F) << 2) | ((present >> 30) & 3)
        active = [ccd for ccd in range(_CCD_POSITIONS) if ((present >> 22) & ~disabled) >> ccd & 1]
        if len(active) != len(groups):
            raise CoreMapError(
                f"the CCD-disable fuses show active CCDs {active} but the OS shows {len(groups)} CCD groups -- "
                f"per-core CO stays disabled instead of writing to the wrong CCD; please report this "
                f"output{self._offline_hint}"
            )
        return dict(zip(sorted(groups), active, strict=True))  # equivalent-mutant: the count check equals the lengths

    def _fuse_group_slots(self, encode_ccd: int, want: int) -> list[int]:
        """This CCD's live physical slots, from its SMN core-disable fuse.

        The fuse is the SMU's own record of which of the 8 slots exist (a set
        bit is a fused-off slot), and the only thing that resolves a renumbered
        core-id space -- the CO read answers on every in-range slot and so
        proves nothing (issue #11). Returns the live slots ascending when their
        count matches the ``want`` cores the OS reports for this CCD; raises
        CoreMapError, naming what to do about it, otherwise.
        """
        addr = self.commands.core_fuse_addr
        if addr is None:
            raise CoreMapError(
                f"core ids on this CPU are renumbered and no core-disable fuse "
                f"address is verified for {self.commands.generation.name}, so "
                f"the fused-off slots cannot be located -- per-core CO stays "
                f"disabled instead of writing to the wrong cores; please report "
                f"this output{self._offline_hint}"
            )
        if encode_ccd and self.commands.core_fuse_die_wide:
            raise CoreMapError(
                f"the {self.commands.generation.name} core-disable fuse covers the "
                f"whole die, but the topology shows CCD {encode_ccd} -- per-core CO "
                f"stays disabled instead of writing to the wrong cores; please "
                f"report this output"
            )
        ok, msg = self.check_smn_readable()
        if not ok:
            raise CoreMapError(
                f"core ids on this CPU are renumbered, so the SMU core-disable "
                f"fuse has to be read to find the fused-off slots: {msg} -- "
                f"per-core CO stays disabled until then"
            )
        fuse = self.read_smn(addr + (encode_ccd << _CCD_FUSE_SHIFT))
        if fuse is None:
            raise CoreMapError(
                f"core ids on this CPU are renumbered and the CCD-{encode_ccd} "
                f"core-disable fuse read failed on an otherwise writable smn "
                f"file -- per-core CO stays disabled instead of writing to the "
                f"wrong cores; please report this output"
            )
        disabled = (fuse >> self.commands.core_fuse_shift) & 0xFF
        live = [slot for slot in range(_SLOTS_PER_CCD) if not (disabled >> slot) & 1]
        if len(live) == want:
            return live
        raise CoreMapError(
            f"core ids on this CPU are renumbered and the CCD-{encode_ccd} "
            f"core-disable fuse disagrees with the OS: fuse {disabled:#04x} "
            f"leaves {len(live)} live slots {live} but the OS reports {want} "
            f"cores -- per-core CO stays disabled instead of writing to the "
            f"wrong cores; please report this output{self._offline_hint}"
        )

    @property
    def core_map(self) -> dict[int, tuple[int, int]] | None:
        """Discovered {core_id: (ccd, slot)} addressing map, if any."""
        return dict(self._ccd_slot_by_core) if self._ccd_slot_by_core is not None else None

    @property
    def core_map_error(self) -> str | None:
        """Why per-core CO is refused, or None when addressing is usable."""
        return self._ccd_slot_error

    def _co_address(self, core_id: int) -> tuple[int | None, int | None] | None:
        """(ccd, slot) for encode_co_arg, or None when the write must refuse.

        None means either the map discovery failed (core_map_error holds why)
        or the caller named a core the discovered map does not contain.
        """
        if self._ccd_slot_error is not None:
            return None
        if self._ccd_slot_by_core is not None:
            return self._ccd_slot_by_core.get(core_id)
        ccd = self._legacy_ccd_by_core.get(core_id) if self._legacy_ccd_by_core else None
        return (ccd, None)

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

    def backup_co_offsets(self, num_cores: int) -> dict[int, int]:
        """Save current CO offsets for all cores before modification.

        The backup is stored internally and can be restored with
        ``restore_co_offsets()``.  The dict is also returned for the caller
        to persist (e.g. write to a JSON file) if desired.

        Note: CO values are VOLATILE — they reset on reboot regardless.
        This backup guards against accidental *within-session* mistakes only.
        """
        offsets = self.get_all_co_offsets(num_cores)
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

    def _get_cmd_filename(self) -> str:
        if self.commands.mailbox == "mp1":
            return "mp1_smu_cmd"
        return "rsmu_cmd"

    def _send_get_co(self, arg: int) -> SMUResponse:
        """Send the CO read on its own mailbox.

        APU generations set CO via MP1 but read it back via RSMU
        (get_co_mailbox="rsmu"); everything else reads on the default mailbox.
        """
        mailbox = self.commands.get_co_mailbox or self.commands.mailbox
        if mailbox == "rsmu" and self.commands.mailbox != "rsmu":
            return self._send_rsmu_command(self.commands.get_co_cmd, (arg,))
        return self._send_command(self.commands.get_co_cmd, (arg,))

    def _get_cmd_path(self) -> Path:
        """Get the command file path based on mailbox type."""
        return self.sysfs / self._get_cmd_filename()

    def _send_command(self, cmd: int, args: tuple[int, ...] = (0, 0, 0, 0, 0, 0)) -> SMUResponse:
        """Send an SMU command and read the response."""
        return self._mailbox(self._get_cmd_path(), cmd, args, "SMU")

    def _mailbox(self, cmd_path: Path, cmd: int, args: tuple[int, ...], name: str) -> SMUResponse:
        args_path = self.sysfs / "smu_args"
        if len(args) < 6:
            args = args + (0,) * (6 - len(args))
        try:
            packed_args = struct.pack("<6I", *args[:6])
            with self._held(args_path):
                args_path.write_bytes(packed_args)
                cmd_path.write_bytes(struct.pack("<I", cmd))
                resp_cmd = cmd_path.read_bytes()
                resp_args_raw = args_path.read_bytes()
            status = struct.unpack("<I", resp_cmd[:4])[0]
            resp_args = struct.unpack("<6I", resp_args_raw[:24])
        except (OSError, struct.error) as exc:
            log.debug("%s command %#x failed: %s", name, cmd, exc)
            return SMUResponse(success=False, args=(0,) * 6, raw=b"")
        return SMUResponse(success=(status == 1), args=resp_args, raw=resp_args_raw)

    def _send_rsmu_command(self, cmd: int, args: tuple[int, ...] = (0, 0, 0, 0, 0, 0)) -> SMUResponse:
        """Send an RSMU command regardless of the default mailbox.

        PBO limit commands use RSMU even on Zen 3 (which defaults to MP1 for CO).
        """
        return self._mailbox(self.sysfs / "rsmu_cmd", cmd, args, "RSMU")

    def get_co_offset(self, core_id: int) -> int | None:
        """Read the current CO offset for a physical core.

        CO values are VOLATILE and reset to zero on reboot.
        """
        if not self.commands.has_co:
            return None
        addr = self._co_address(core_id)
        if addr is None:
            log.error(
                "CO read refused for core %d: %s",
                core_id,
                self._ccd_slot_error or "core is not in the discovered core map",
            )
            return None
        ccd, slot = addr
        arg = encode_co_arg(core_id, 0, self.commands.generation, ccd=ccd, slot=slot)
        if self.commands.get_co_by_slot:
            if arg >> 24:
                log.error(
                    "CO read refused for core %d: %s reads CO by bare slot, which cannot carry the CCD/CCX bits of %#x",
                    core_id,
                    self.commands.generation.name,
                    arg,
                )
                return None
            arg >>= 20
        resp = self._send_get_co(arg)
        if not resp.success:
            return None
        return decode_co_arg(core_id, resp.args[0], self.commands.generation)

    def set_co_offset(self, core_id: int, value: int) -> bool:
        """Set the CO offset for a physical core. Returns True on success.

        CO values are VOLATILE — they live in SMU SRAM and reset on reboot.
        Your BIOS PBO settings are never modified.

        Safety:
          - Range-checked against the generation's CO limits
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
            raise ValueError(f"CO value {value} out of range [{co_min}, {co_max}] for {self.commands.generation.name}")

        addr = self._co_address(core_id)
        if addr is None:
            log.error(
                "CO write refused for core %d: %s",
                core_id,
                self._ccd_slot_error or "core is not in the discovered core map",
            )
            return False

        ok, msg = self.check_writable()
        if not ok:
            log.error("Permission check failed before CO write: %s", msg)
            return False

        ccd, slot = addr
        arg = encode_co_arg(core_id, value, self.commands.generation, ccd=ccd, slot=slot)
        resp = self._send_command(self.commands.set_co_cmd, (arg,))
        if not resp.success:
            log.error("SMU rejected CO write for core %d value %d", core_id, value)
            return False

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

        Uses the SetAllDldoPsmMargin command if available.
        Returns False if the set-all command is not available for this generation.
        """
        if not self.commands.has_co:
            return False

        co_min, co_max = self.commands.co_range
        if not co_min <= value <= co_max:
            raise ValueError(f"CO value {value} out of range [{co_min}, {co_max}] for {self.commands.generation.name}")

        if self._ccd_slot_error is not None:
            log.error("set_all_co refused: %s", self._ccd_slot_error)
            return False

        if self.commands.set_all_co_cmd is not None:
            margin = value & 0xFFFF
            resp = self._send_command(self.commands.set_all_co_cmd, (margin,))
            if not resp.success:
                log.error("SMU rejected set_all_co with value %d", value)
                return False

            readback_core = self._known_core_ids[0] if self._known_core_ids else 0
            readback = self.get_co_offset(readback_core)
            if readback != value:
                log.error(
                    "set_all_co read-back mismatch: wrote %d, read back %s from core %d",
                    value,
                    readback,
                    readback_core,
                )
                return False
            return True
        return False

    def reset_all_co(self) -> bool:
        """Reset all CO offsets to 0 by delegating to ``set_all_co``.

        The delegate owns the whole refusal contract (range check, core-map
        guard, read-back), so a reset never reports success for a
        write ``set_all_co`` refuses.

        CO values are VOLATILE — this resets them to 0 for the current
        session only.  On reboot they return to whatever the BIOS sets.
        """
        return self.set_all_co(0)

    def get_all_co_offsets(self, num_cores: int) -> dict[int, int | None]:
        """Read CO offsets for all cores.

        With a loaded topology this iterates the machine's real core-id set
        (ids are not contiguous on gap-preserving harvested parts: a 5900X
        runs 0-5 and 8-13), and ``num_cores`` is only the fallback domain
        ``range(num_cores)`` for callers that never loaded one.

        CO values are VOLATILE — they reset to zero on reboot.
        """
        core_ids = self._known_core_ids if self._known_core_ids is not None else range(num_cores)
        return {core_id: self.get_co_offset(core_id) for core_id in core_ids}

    def get_curve_shaper(self) -> tuple[tuple[int, int, int], ...] | None:
        cmd = self.commands.get_curve_shaper_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        bands = [struct.unpack("<Bbbb", struct.pack("<I", word)) for word in resp.args[:5]]
        if not resp.success or [band[0] for band in bands] != list(range(5)):
            return None
        return tuple(band[1:] for band in bands)

    def set_curve_shaper(self, bands: tuple[tuple[int, int, int], ...]) -> str | None:
        cmd = self.commands.set_curve_shaper_cmd
        if cmd is None:
            return f"{self.commands.generation.name} has no Curve Shaper write"
        low, high = SHAPER_MARGINS
        if len(bands) != len(SHAPER_BAND_NAMES) or any(
            len(band) != 3 or not all(low <= margin <= high for margin in band) for band in bands
        ):
            raise ValueError(f"a Curve Shaper is five bands of three margins within {low}..{high}, not {bands}")
        ok, msg = self.check_writable()
        if not ok:
            return msg
        for index, margins in enumerate(bands):
            cold, warm, hot = (margin & 0xFF for margin in margins)
            resp = self._send_rsmu_command(cmd, ((hot << 24) | (warm << 16) | (cold << 8) | SHAPER_SET_BIT | index,))
            if not resp.success:
                return f"the SMU refused band {SHAPER_BAND_NAMES[index]}"
        back = self.get_curve_shaper()
        if back != tuple(tuple(band) for band in bands):
            return f"the read-back gave {back}"
        return None

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
        resp = self._send_rsmu_command(cmd, (encode_boost_limit_arg(mhz),))
        return resp.success

    def set_ppt_limit(self, watts: int) -> bool:
        """Set PPT (Package Power Tracking) limit in watts. VOLATILE.

        Range-checked and raises ValueError on a malformed value before any write.
        """
        cmd = self.commands.set_ppt_cmd
        if cmd is None:
            return False
        _check_pbo_limit("PPT", watts)
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(watts),))
        return resp.success

    def set_tdc_limit(self, amps: int) -> bool:
        """Set TDC (Thermal Design Current) limit in amps. VOLATILE.

        Range-checked and raises ValueError on a malformed value before any write.
        """
        cmd = self.commands.set_tdc_cmd
        if cmd is None:
            return False
        _check_pbo_limit("TDC", amps)
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(amps),))
        return resp.success

    def set_edc_limit(self, amps: int) -> bool:
        """Set EDC (Electrical Design Current) limit in amps. VOLATILE.

        Range-checked and raises ValueError on a malformed value before any write.
        """
        cmd = self.commands.set_edc_cmd
        if cmd is None:
            return False
        _check_pbo_limit("EDC", amps)
        resp = self._send_rsmu_command(cmd, (encode_pbo_limit_arg(amps),))
        return resp.success

    def get_pbo_scalar(self) -> float | None:
        """Read current PBO scalar (1.0 to 10.0)."""
        cmd = self.commands.get_pbo_scalar_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        raw_bytes = struct.pack("<I", resp.args[0])
        return struct.unpack("<f", raw_bytes)[0]

    def set_pbo_scalar(self, scalar: float) -> bool:
        """Set PBO scalar (1.0 to 10.0). VOLATILE."""
        cmd = self.commands.set_pbo_scalar_cmd
        if cmd is None:
            return False
        if not 0.0 <= scalar <= 10.0:
            raise ValueError(f"PBO scalar {scalar} out of range [0.0, 10.0]")
        resp = self._send_rsmu_command(cmd, (encode_pbo_scalar_arg(scalar),))
        return resp.success

    def get_fastest_core(self) -> int | None:
        """Query the SMU for the fastest core index."""
        cmd = self.commands.get_fastest_core_cmd
        if cmd is None:
            return None
        resp = self._send_rsmu_command(cmd)
        if not resp.success:
            return None
        return resp.args[0]
