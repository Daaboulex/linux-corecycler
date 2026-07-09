"""Tests for local-timezone display formatting of stored UTC timestamps."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

import pytest

from history.timefmt import format_local


@pytest.fixture
def fixed_tz():
    """Pin the process timezone so local conversion is deterministic.

    Yields a setter that installs a tz (POSIX TZ string) and restores the
    original on teardown. Requires a platform with ``time.tzset`` (POSIX).
    """
    saved = os.environ.get("TZ")

    def _set(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    yield _set

    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def test_empty_input_returns_empty():
    assert format_local("") == ""


def test_utc_aware_converts_to_local_tokyo(fixed_tz):
    # 2026-06-15T00:30:00Z -> 09:30:00 in Tokyo (UTC+9, no DST).
    fixed_tz("JST-9")
    assert format_local("2026-06-15T00:30:00+00:00") == "2026-06-15 09:30:00"


def test_utc_aware_negative_offset(fixed_tz):
    # 23:30Z at UTC-4 (POSIX EST4) -> 19:30 local, same day.
    fixed_tz("EST4")
    assert format_local("2026-06-15T23:30:00+00:00") == "2026-06-15 19:30:00"


def test_date_only(fixed_tz):
    fixed_tz("JST-9")
    # 22:00Z at UTC+9 -> 07:00 next local day; date_only must reflect that date.
    assert format_local("2026-06-15T22:00:00+00:00", date_only=True) == "2026-06-16"


def test_naive_string_assumed_utc(fixed_tz):
    # A legacy naive string (no offset) must be treated as UTC, not local.
    fixed_tz("JST-9")
    assert format_local("2026-06-15T00:30:00") == "2026-06-15 09:30:00"


def test_unparsable_falls_back_to_slice():
    assert format_local("not-a-timestamp-but-long-enough") == "not-a-timestamp-but"
    assert format_local("not-a-ts", date_only=True) == "not-a-ts"


def test_roundtrips_to_same_instant(fixed_tz):
    # Property: the localized display denotes the same instant as the input.
    fixed_tz("EST4")
    iso = "2026-12-15T18:45:00+00:00"
    shown = format_local(iso)
    local = datetime.strptime(shown, "%Y-%m-%d %H:%M:%S").astimezone()
    assert local.astimezone(UTC) == datetime.fromisoformat(iso)
