from __future__ import annotations

import json

import pytest

from corecycler.history.archive import PHASES, STATUSES, archive_old_sessions, archive_path
from corecycler.history.db import OLD_TUNER_TABLES, HistoryDB
from tests._old_rows import seed_old_session
from tests._tuner_world import save_machine_session


@pytest.fixture
def db(tmp_path):
    d = HistoryDB(tmp_path / "history.db")
    yield d
    d.close()


@pytest.mark.req("D85", "E7.A")
def test_every_old_session_and_its_rows_are_archived_as_stored(db, tmp_path):
    first = seed_old_session(db, "completed", "hardened")
    second = seed_old_session(db, "quarantined", "fine_search")
    save_machine_session(db, tmp_path, "paused", session_id=second + 1)
    written = archive_old_sessions(db)
    assert written == archive_path(db.path) == tmp_path / "history.db.old-tuner-sessions.json"
    archive = json.loads(written.read_text())
    assert (archive["format"], archive["version"], archive["database"]) == (
        "corecycler old tuner sessions",
        1,
        str(db.path),
    )
    sessions = archive["tables"]["tuner_sessions"]
    assert [row["id"] for row in sessions["rows"]] == [first, second]
    assert "machine_json" in sessions["columns"]
    for table in OLD_TUNER_TABLES:
        assert [row["session_id"] for row in archive["tables"][table]["rows"]] == [first, second]
        stored = [
            dict(row) for row in db._execute_raw(f"SELECT * FROM {table} WHERE session_id IN (?, ?)", (first, second))
        ]
        assert archive["tables"][table]["rows"] == stored
    assert archive["glossary"] == {"phases": PHASES, "statuses": STATUSES}
    assert len(db._execute_raw("SELECT id FROM tuner_sessions").fetchall()) == 3


def test_nothing_is_written_without_old_sessions_or_a_second_time(db, tmp_path):
    save_machine_session(db, tmp_path, "paused")
    assert archive_old_sessions(db) is None
    assert not archive_path(db.path).exists()
    seed_old_session(db)
    written = archive_old_sessions(db)
    before = written.read_text()
    seed_old_session(db)
    assert archive_old_sessions(db) is None
    assert written.read_text() == before


def test_a_database_in_memory_is_not_archived():
    memory = HistoryDB(":memory:")
    seed_old_session(memory)
    assert archive_old_sessions(memory) is None
    memory.close()


def test_an_unwritable_folder_raises_and_the_rows_stay(db, monkeypatch):
    seed_old_session(db)

    def refused(path, content):
        raise PermissionError("read-only")

    monkeypatch.setattr("corecycler.history.archive.atomic_write", refused)
    with pytest.raises(PermissionError):
        archive_old_sessions(db)
    assert len(db._execute_raw("SELECT id FROM tuner_sessions").fetchall()) == 1
