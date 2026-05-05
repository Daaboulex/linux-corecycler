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

from history.db import TuningContextRecord

if TYPE_CHECKING:
    from history.db import HistoryDB
    from smu.driver import RyzenSMU

log = logging.getLogger(__name__)

BIOS_VERSION_PATH = Path("/sys/class/dmi/id/bios_version")


def read_bios_version(path: Path = BIOS_VERSION_PATH) -> str:
    """Read BIOS version from DMI sysfs. Returns '' if unavailable."""
    try:
        if path.exists():
            return path.read_text().strip()
    except OSError:
        log.debug("Could not read BIOS version from %s", path)
    return ""


def compute_co_hash(offsets: dict[int, int | None]) -> str:
    """Deterministic SHA-256 of CO offsets dict.

    None values are excluded (unknown cores don't affect identity).
    Order-independent: {0: -30, 1: -20} == {1: -20, 0: -30}.
    """
    clean = {k: v for k, v in offsets.items() if v is not None}
    if not clean:
        return ""
    payload = json.dumps(sorted(clean.items()), separators=(",", ":"))
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

    if smu is not None and num_cores > 0:
        try:
            co_offsets = smu.get_all_co_offsets(num_cores)
        except Exception:
            log.warning("Failed to read CO offsets from SMU", exc_info=True)

        try:
            pbo_scalar = smu.get_pbo_scalar()
        except Exception:
            log.debug("Failed to read PBO scalar", exc_info=True)

        try:
            boost_limit = smu.get_boost_limit()
        except Exception:
            log.debug("Failed to read boost limit", exc_info=True)

    co_hash = compute_co_hash(co_offsets)
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
    )


def find_or_create_context(db: HistoryDB, ctx: TuningContextRecord) -> int:
    """Find an existing context matching (co_hash, bios_version), or create one.

    Returns the context id.
    """
    existing = db.get_context_by_hash(ctx.co_hash, ctx.bios_version)
    if existing is not None:
        return existing.id
    return db.create_context(ctx)


def detect_bios_change(
    db: HistoryDB,
    bios_path: Path = BIOS_VERSION_PATH,
) -> tuple[bool, str, str]:
    """Check if BIOS version changed since the most recent tuning context.

    Returns (changed, old_version, current_version).
    If no previous contexts exist, returns (False, '', current).
    """
    current = read_bios_version(bios_path)
    contexts = db.list_contexts(limit=1)
    if not contexts:
        return False, "", current
    old = contexts[0].bios_version
    return old != current, old, current
