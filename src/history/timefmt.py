"""Local-timezone formatting for stored UTC timestamps.

History timestamps are stored as canonical UTC ISO-8601 (see
``HistoryDB._now_iso``). Storage stays UTC; only display converts to the
system local timezone. Exports remain UTC for portability and are not
routed through here.
"""

from __future__ import annotations

from datetime import UTC, datetime

_DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
_DATE_FMT = "%Y-%m-%d"


def format_local(iso: str, *, date_only: bool = False) -> str:
    """Format a stored ISO-8601 timestamp in the system local timezone.

    Accepts the UTC-aware strings written by ``HistoryDB._now_iso`` (e.g.
    ``2026-06-15T18:30:00+00:00``). A naive string (no offset) is assumed to
    be UTC — matching the storage convention — so legacy rows localize
    correctly instead of being misread as local wall-clock. Returns an empty
    string for empty input, and falls back to a raw slice if the value cannot
    be parsed, so a display path never raises on malformed data.
    """
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso[:10] if date_only else iso[:19].replace("T", " ")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone()
    return local.strftime(_DATE_FMT if date_only else _DATETIME_FMT)
