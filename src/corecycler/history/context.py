"""Tuning context utilities — BIOS version, CO snapshots, session grouping.

A "tuning context" captures the system state that determines whether two
test runs are comparable: BIOS version + CO offsets + PBO settings.  Runs
under the same context form a tuning session.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.history.db import TuningContextRecord
from corecycler.smu import pmtable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from corecycler.history.db import HistoryDB
    from corecycler.smu.driver import RyzenSMU

log = logging.getLogger(__name__)

BIOS_VERSION_PATH = Path("/sys/class/dmi/id/bios_version")
UNREADABLE = "unreadable: "


def read_bios_version(path: Path = BIOS_VERSION_PATH) -> str:
    """The BIOS version from DMI sysfs: '' on a machine without one, "unreadable: <reason>" when the read fails."""
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"{UNREADABLE}{exc.strerror}"


def compute_co_hash(
    offsets: dict[int, int | None],
    limits: tuple[float | None, float | None, float | None] | None = None,
) -> str:
    """Deterministic SHA-256 identity of CO offsets plus PBO power limits.

    None values are excluded (unknown cores don't affect identity).
    Order-independent: {0: -30, 1: -20} == {1: -20, 0: -30}.
    Power limits (PPT/TDC/EDC) are part of the stability environment, so they
    are part of the identity; when none are available the payload stays
    byte-identical to the pre-v13 CO-only form so existing contexts keep
    their identity.
    """
    clean = {k: v for k, v in offsets.items() if v is not None}
    have_limits = limits is not None and any(v is not None for v in limits)
    if not clean and not have_limits:
        return ""
    payload_obj: object = sorted(clean.items())
    if have_limits:
        payload_obj = {
            "co": sorted(clean.items()),
            "limits": [None if v is None else round(v, 1) for v in limits],
        }
    payload = json.dumps(payload_obj, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def capture_system_context(
    smu: RyzenSMU | None = None,
    num_cores: int = 0,
    bios_path: Path = BIOS_VERSION_PATH,
) -> TuningContextRecord:
    """Snapshot the current tuning state.

    If SMU is unavailable, returns a context with BIOS version only.
    """
    bios = read_bios_version(bios_path)

    co_offsets: dict[int, int | None] = {}
    pbo_scalar: float | None = None
    boost_limit: int | None = None
    ppt = tdc = edc = None

    if smu is not None and num_cores > 0:
        co_offsets = smu.get_all_co_offsets(num_cores)
        pbo_scalar = smu.get_pbo_scalar()
        boost_limit = smu.get_boost_limit()
        ppt, tdc, edc = pmtable.read_power_limits()
        unread = sorted(core for core, value in co_offsets.items() if value is None)
        if unread and smu.commands.has_co:
            log.warning(
                "Tuning context: the CO of cores %s could not be read, so they are left out of its identity", unread
            )

    co_hash = compute_co_hash(co_offsets, (ppt, tdc, edc))
    co_json = json.dumps(
        {k: v for k, v in co_offsets.items() if v is not None},
        separators=(",", ":"),
    )

    return TuningContextRecord(
        bios_version=bios,
        co_offsets_json=co_json,
        co_hash=co_hash,
        pbo_scalar=pbo_scalar,
        boost_limit_mhz=boost_limit,
        ppt_limit_w=ppt,
        tdc_limit_a=tdc,
        edc_limit_a=edc,
    )


def find_or_create_context(db: HistoryDB, ctx: TuningContextRecord) -> int | None:
    """The id of the context matching (co_hash, bios_version), created when new; None when the BIOS version could
    not be read, which is no context of its own."""
    if ctx.bios_version.startswith(UNREADABLE):
        return None
    existing = db.get_context_by_hash(ctx.co_hash, ctx.bios_version)
    if existing is not None:
        return existing.id
    return db.create_context(ctx)


def detect_bios_change(db: HistoryDB, bios_path: Path = BIOS_VERSION_PATH) -> str | None:
    """The History warning about the BIOS version against the newest tuning context, or None when there is none."""
    current = read_bios_version(bios_path)
    if current.startswith(UNREADABLE):
        return f"BIOS version {current}, so a change cannot be judged"
    contexts = db.list_contexts()
    if not contexts or contexts[0].bios_version == current:
        return None
    earlier = last_recorded_bios(contexts)
    if earlier == current:
        return None
    if bios_change_judged(earlier, current):
        return f"BIOS changed: {earlier} -> {current}"
    if earlier:
        return f"BIOS version absent, {earlier} recorded before, so a change cannot be judged"
    return f"BIOS version {current}, none recorded before, so a change cannot be judged"


def last_recorded_bios(contexts: Sequence[TuningContextRecord]) -> str:
    """The newest BIOS version among contexts given newest first, skipping any that recorded none; "" if none did."""
    return next((ctx.bios_version for ctx in contexts if ctx.bios_version), "")


def bios_change_judged(earlier: str, now: str) -> bool:
    """Whether two recorded BIOS versions show a change: never when either one is missing."""
    return bool(earlier and now) and earlier != now
