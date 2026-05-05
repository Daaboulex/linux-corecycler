"""Tests for history.context — tuning context capture and comparison."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from history.context import (
    TuningContextRecord,
    capture_system_context,
    compute_co_hash,
    detect_bios_change,
    find_or_create_context,
    read_bios_version,
)
from history.db import HistoryDB


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


class TestComputeCoHash:
    def test_deterministic(self):
        h1 = compute_co_hash({0: -30, 1: -20, 2: -25})
        h2 = compute_co_hash({0: -30, 1: -20, 2: -25})
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_order_independent(self):
        h1 = compute_co_hash({0: -30, 1: -20})
        h2 = compute_co_hash({1: -20, 0: -30})
        assert h1 == h2

    def test_different_values_different_hash(self):
        h1 = compute_co_hash({0: -30, 1: -20})
        h2 = compute_co_hash({0: -30, 1: -25})
        assert h1 != h2

    def test_empty_offsets(self):
        assert compute_co_hash({}) == ""

    def test_none_values_excluded(self):
        h1 = compute_co_hash({0: -30, 1: None, 2: -20})
        h2 = compute_co_hash({0: -30, 2: -20})
        assert h1 == h2

    def test_all_none_is_empty(self):
        assert compute_co_hash({0: None, 1: None}) == ""


class TestReadBiosVersion:
    def test_reads_from_file(self, tmp_path):
        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2101\n")
        assert read_bios_version(bios_file) == "2101"

    def test_missing_file(self, tmp_path):
        assert read_bios_version(tmp_path / "nonexistent") == ""

    def test_strips_whitespace(self, tmp_path):
        bios_file = tmp_path / "bios_version"
        bios_file.write_text("  2101  \n")
        assert read_bios_version(bios_file) == "2101"


class TestCaptureSystemContext:
    def test_without_smu(self, tmp_path):
        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2101")

        ctx = capture_system_context(smu=None, num_cores=0, bios_path=bios_file)
        assert ctx.bios_version == "2101"
        assert ctx.co_offsets_json == "{}"
        assert ctx.co_hash == ""
        assert ctx.pbo_scalar is None
        assert ctx.boost_limit_mhz is None

    def test_without_smu_no_bios(self, tmp_path):
        ctx = capture_system_context(
            smu=None, num_cores=0, bios_path=tmp_path / "missing"
        )
        assert ctx.bios_version == ""
        assert ctx.co_hash == ""


class TestFindOrCreateContext:
    def test_creates_new(self, db):
        ctx = TuningContextRecord(
            bios_version="2101",
            co_offsets_json='{"0":-30,"1":-20}',
            co_hash=compute_co_hash({0: -30, 1: -20}),
        )
        ctx_id = find_or_create_context(db, ctx)
        assert ctx_id > 0

        fetched = db.get_context(ctx_id)
        assert fetched.bios_version == "2101"

    def test_finds_existing(self, db):
        co_hash = compute_co_hash({0: -30, 1: -20})
        ctx1 = TuningContextRecord(
            bios_version="2101",
            co_offsets_json='{"0":-30,"1":-20}',
            co_hash=co_hash,
        )
        id1 = find_or_create_context(db, ctx1)

        ctx2 = TuningContextRecord(
            bios_version="2101",
            co_offsets_json='{"0":-30,"1":-20}',
            co_hash=co_hash,
        )
        id2 = find_or_create_context(db, ctx2)

        assert id1 == id2

    def test_different_bios_creates_new(self, db):
        co_hash = compute_co_hash({0: -30})
        id1 = find_or_create_context(
            db,
            TuningContextRecord(bios_version="2101", co_hash=co_hash),
        )
        id2 = find_or_create_context(
            db,
            TuningContextRecord(bios_version="2201", co_hash=co_hash),
        )
        assert id1 != id2

    def test_different_co_creates_new(self, db):
        id1 = find_or_create_context(
            db,
            TuningContextRecord(
                bios_version="2101",
                co_hash=compute_co_hash({0: -30}),
            ),
        )
        id2 = find_or_create_context(
            db,
            TuningContextRecord(
                bios_version="2101",
                co_hash=compute_co_hash({0: -40}),
            ),
        )
        assert id1 != id2


class TestDetectBiosChange:
    def test_no_previous_contexts(self, db, tmp_path):
        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2101")
        changed, old, current = detect_bios_change(db, bios_path=bios_file)
        assert changed is False
        assert old == ""
        assert current == "2101"

    def test_same_bios(self, db, tmp_path):
        db.create_context(TuningContextRecord(bios_version="2101"))

        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2101")
        changed, old, current = detect_bios_change(db, bios_path=bios_file)
        assert changed is False

    def test_different_bios(self, db, tmp_path):
        db.create_context(TuningContextRecord(bios_version="2101"))

        bios_file = tmp_path / "bios_version"
        bios_file.write_text("2201")
        changed, old, current = detect_bios_change(db, bios_path=bios_file)
        assert changed is True
        assert old == "2101"
        assert current == "2201"
