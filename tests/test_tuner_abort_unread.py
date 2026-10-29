from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from corecycler import cli
from corecycler.history.db import HistoryDB
from corecycler.smu import writer_lock
from corecycler.tuner import live_hw
from corecycler.tuner.machine.types import Lifecycle, PauseRequest
from tests._old_rows import seed_old_session
from tests._tuner_world import UNREADABLE_ROWS, save_machine_session
from tests.test_tuner_live import FakeHardware, config, in_flight, rig, rows

NO_BASE_CO, BASE_CO_ERROR = UNREADABLE_ROWS[0]
UNREAD_BASE_CO = f"session 1 could not be read: {BASE_CO_ERROR}"
CHECK = "so no CO was written: reboot or check the CO before tuning again"
ABORTED_EVENTS = "SELECT message FROM tuner_events WHERE session_id = 1 AND severity = 'aborted'"
STATUS = "SELECT status, machine_json FROM tuner_sessions WHERE id = 1"


@pytest.fixture
def db(tmp_path):
    opened = HistoryDB(tmp_path / "history.db")
    yield opened
    opened.close()


def held(tmp_path, db: HistoryDB, status: str) -> FakeHardware:
    hardware = FakeHardware()
    first = rig(tmp_path, hardware, db=db)
    if status == "paused":
        first.start(config(), 1, 7, True)
        first.post(PauseRequest())
        assert first.state.lifecycle is Lifecycle.PAUSED
    else:
        killed(first)
    return hardware


def killed(r):
    core = in_flight(r)
    r.worker.thread.join(5)
    assert r.hardware.co[core.core] == core.value != 0
    return core


def unreadable(db: HistoryDB, machine_json: str) -> str:
    db._execute_raw(f"UPDATE tuner_sessions SET machine_json = {machine_json} WHERE id = 1")
    return db._execute_raw("SELECT machine_json FROM tuner_sessions WHERE id = 1").fetchone()[0]


def recorded(hardware: FakeHardware) -> list:
    writes = []
    real = hardware.write_co
    hardware.write_co = lambda core, value: writes.append((core, value)) or real(core, value)
    return writes


def aborted(tmp_path, db: HistoryDB, hardware: FakeHardware, session: str = "1", ready=None):
    r = rig(tmp_path, hardware, db=db)
    said = []
    r.say = said.append
    if ready is not None:
        ready(r)
    args = live_hw.parse(["--state-dir", str(tmp_path / "state"), "--abort", "--session", session, "--write"])
    return live_hw._run(args, r, said.append), r, said


def journal_after(r, row: int) -> list:
    sql = "SELECT session_id, core_id, value, boot_id, confirmed FROM tuner_co_journal WHERE id > ? ORDER BY id"
    return rows(r, sql, (row,))


def last_journal_row(db: HistoryDB) -> int:
    return db._execute_raw("SELECT MAX(id) FROM tuner_co_journal").fetchone()[0]


@pytest.mark.req("UNREAD.abort")
@pytest.mark.parametrize(("machine_json", "error"), UNREADABLE_ROWS)
@pytest.mark.parametrize("status", ["paused", "running"])
def test_an_unreadable_held_session_of_an_earlier_boot_ends_aborted_unread_writing_no_co_and_frees_start(
    tmp_path, db, status, machine_json, error
):
    held(tmp_path, db, status)
    stored = unreadable(db, machine_json)
    rebooted = FakeHardware(co={0: -3, 1: 0, 8: 0}, boot="boot-b")
    writes = recorded(rebooted)
    code, r, said = aborted(tmp_path, db, rebooted)
    text = f"aborted unread: session 1 could not be read: {error}; nothing it wrote is resident in this boot"
    assert rows(r, STATUS) == [{"status": "aborted", "machine_json": stored}]
    assert rows(r, ABORTED_EVENTS) == [{"message": text}]
    assert (writes, rebooted.co) == ([], {0: -3, 1: 0, 8: 0})
    assert said == [f"aborted: {text}"]
    assert code == cli.EXIT_ABORTED_UNREAD
    unacknowledged = rig(tmp_path, FakeHardware(boot="boot-b"), db=db)
    unacknowledged.start(config(), 2, 7, False)
    assert unacknowledged.refused == (
        f"session 1 could not be read: {error}, so whether the CO moved since it started cannot be judged:"
        " acknowledge to start"
    )
    acknowledged = rig(tmp_path, FakeHardware(boot="boot-b"), db=db)
    acknowledged.start(config(), 2, 7, True)
    assert (acknowledged.refused, acknowledged.state.lifecycle) == (None, Lifecycle.RUNNING)


@pytest.mark.req("UNREAD.abort")
@pytest.mark.parametrize(("machine_json", "error"), UNREADABLE_ROWS)
def test_an_unreadable_session_a_driver_killed_mid_test_left_sends_every_core_its_journal_names_to_stock(
    tmp_path, db, machine_json, error
):
    hardware = FakeHardware()
    killed(rig(tmp_path, hardware, db=db))
    stored = unreadable(db, machine_json)
    row = last_journal_row(db)
    code, r, said = aborted(tmp_path, db, hardware)
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert journal_after(r, row) == [
        {"session_id": 1, "core_id": core, "value": 0, "boot_id": "boot-a", "confirmed": 1} for core in (0, 1, 8)
    ]
    text = f"aborted unread: session 1 could not be read: {error}; cores 0, 1, 8 sent to stock"
    assert rows(r, STATUS) == [{"status": "aborted", "machine_json": stored}]
    assert rows(r, ABORTED_EVENTS) == [{"message": text}]
    assert said == [f"aborted: {text}"]
    assert code == cli.EXIT_ABORTED_UNREAD


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_whose_revert_write_is_refused_names_the_core_left_unknown(tmp_path, db):
    hardware = FakeHardware()
    core = killed(rig(tmp_path, hardware, db=db))
    unreadable(db, NO_BASE_CO)
    real = hardware.write_co
    hardware.write_co = lambda c, value: "refused for the test" if c == core.core else real(c, value)
    code, r, said = aborted(tmp_path, db, hardware)
    assert hardware.co == {0: 0, 1: 0, 8: 0} | {core.core: core.value}
    refused = f"core {core.core} could not be written back to 0 (refused for the test); its value is unknown"
    text = (
        f"aborted unread: {UNREAD_BASE_CO}; cores 0, 1, 8 sent to stock; the write to cores {core.core} was refused,"
        " so their CO is unknown: reboot or check the CO before tuning again"
    )
    kinds = "SELECT severity, message FROM tuner_events WHERE severity IN ('aborted', 'revert_failed') ORDER BY id"
    assert rows(r, kinds) == [
        {"severity": "revert_failed", "message": refused},
        {"severity": "aborted", "message": text},
    ]
    assert said == [f"revert_failed: {refused}", f"aborted: {text}"]
    assert rows(r, "SELECT status FROM tuner_sessions WHERE id = 1") == [{"status": "aborted"}]
    assert code == cli.EXIT_ABORTED_UNREAD


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_is_refused_while_reverting_the_co_is_and_leaves_the_session_held(tmp_path, db):
    hardware = FakeHardware()
    killed(rig(tmp_path, hardware, db=db))
    unreadable(db, NO_BASE_CO)
    hardware.revert_refusal = "another program holds the ryzen_smu files"
    writes = recorded(hardware)
    code, r, said = aborted(tmp_path, db, hardware)
    assert said == ["refused: another program holds the ryzen_smu files"]
    assert rows(r, "SELECT status FROM tuner_sessions WHERE id = 1") == [{"status": "running"}]
    assert (writes, rows(r, ABORTED_EVENTS)) == ([], [])
    assert code == live_hw.EXIT_REFUSED


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_writes_no_co_when_another_session_wrote_after_it_in_this_boot(tmp_path, db):
    hardware = FakeHardware()
    killed(rig(tmp_path, hardware, db=db))
    unreadable(db, NO_BASE_CO)
    db.journal_co_intent(seed_old_session(db), 1, -3, False, boot_id="boot-a", mono_ns=1)
    left = dict(hardware.co)
    writes = recorded(hardware)
    code, r, _ = aborted(tmp_path, db, hardware)
    assert (writes, hardware.co) == ([], left)
    assert rows(r, ABORTED_EVENTS) == [
        {"message": f"aborted unread: {UNREAD_BASE_CO}; another session wrote the CO after it in this boot, {CHECK}"}
    ]
    assert rows(r, "SELECT status FROM tuner_sessions WHERE id = 1") == [{"status": "aborted"}]
    assert code == cli.EXIT_ABORTED_UNREAD


def locked_journal(r) -> None:
    def refuse(*_):
        raise sqlite3.OperationalError("database is locked")

    r.store.journal_rows = refuse


@pytest.mark.req("UNREAD.abort")
@pytest.mark.parametrize(
    ("corrupt", "ready", "why"),
    [
        (
            True,
            None,
            "session 1 is unreadable: core 1 has CO journal value 999, not an integer in the sane CO range -100..100",
        ),
        (False, locked_journal, "database is locked"),
    ],
    ids=["corrupt", "locked"],
)
def test_an_unread_abort_whose_co_journal_cannot_be_read_writes_no_co_and_says_to_check_it(
    tmp_path, db, corrupt, ready, why
):
    hardware = FakeHardware()
    killed(rig(tmp_path, hardware, db=db))
    unreadable(db, NO_BASE_CO)
    if corrupt:
        db._execute_raw("UPDATE tuner_co_journal SET value = 999 WHERE session_id = 1 AND core_id = 1")
    left = dict(hardware.co)
    writes = recorded(hardware)
    code, r, _ = aborted(tmp_path, db, hardware, ready=ready)
    assert (writes, hardware.co) == ([], left)
    assert rows(r, ABORTED_EVENTS) == [
        {"message": f"aborted unread: {UNREAD_BASE_CO}; its CO journal could not be read ({why}), {CHECK}"}
    ]
    assert code == cli.EXIT_ABORTED_UNREAD


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_whose_revert_cannot_be_journaled_is_refused_and_leaves_the_session_held(tmp_path, db):
    hardware = FakeHardware()
    killed(rig(tmp_path, hardware, db=db))
    unreadable(db, NO_BASE_CO)

    def full(r):
        def refuse(*_):
            raise sqlite3.OperationalError("disk full")

        r.store.journal_intent = refuse

    code, r, said = aborted(tmp_path, db, hardware, ready=full)
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert said == ["refused: the CO journal failed during a revert: disk full"]
    assert rows(r, "SELECT status FROM tuner_sessions WHERE id = 1") == [{"status": "running"}]
    assert rows(r, ABORTED_EVENTS) == []
    assert code == live_hw.EXIT_REFUSED


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_whose_event_cannot_be_written_stores_neither_and_leaves_the_session_held(tmp_path, db):
    hardware = held(tmp_path, db, "paused")
    unreadable(db, NO_BASE_CO)
    before = db._execute_raw("SELECT updated_at FROM tuner_sessions WHERE id = 1").fetchone()[0]

    def full(r):
        def refuse(*_):
            raise sqlite3.OperationalError("disk full")

        r.store.event = refuse

    code, r, said = aborted(tmp_path, db, hardware, ready=full)
    assert said == ["refused: session 1 could not be marked aborted: disk full"]
    assert rows(r, "SELECT status, updated_at FROM tuner_sessions WHERE id = 1") == [
        {"status": "paused", "updated_at": before}
    ]
    assert code == live_hw.EXIT_REFUSED


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_stamps_when_the_session_ended(tmp_path, db):
    held(tmp_path, db, "paused")
    unreadable(db, NO_BASE_CO)
    db._execute_raw("UPDATE tuner_sessions SET updated_at = '2000-01-01T00:00:00+00:00' WHERE id = 1")
    code, r, _ = aborted(tmp_path, db, FakeHardware(boot="boot-b"))
    [stamp] = rows(r, "SELECT updated_at FROM tuner_sessions WHERE id = 1")
    assert stamp["updated_at"] > "2000-01-01T00:00:00+00:00"
    assert code == cli.EXIT_ABORTED_UNREAD


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_marks_nothing_when_the_session_stopped_being_held_under_it(tmp_path, db):
    hardware = FakeHardware()
    killed(rig(tmp_path, hardware, db=db))
    unreadable(db, NO_BASE_CO)

    def raced(r):
        read = r.store.journal_rows

        def completed_meanwhile(session_id, boot):
            db._execute_raw("UPDATE tuner_sessions SET status = 'completed' WHERE id = 1")
            return read(session_id, boot)

        r.store.journal_rows = completed_meanwhile

    code, r, said = aborted(tmp_path, db, hardware, ready=raced)
    assert said == ["refused: session 1 could not be marked aborted: session 1 is no longer running or paused"]
    assert rows(r, "SELECT status FROM tuner_sessions WHERE id = 1") == [{"status": "completed"}]
    assert rows(r, ABORTED_EVENTS) == []
    assert code == live_hw.EXIT_REFUSED


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_clears_the_stock_marker_naming_the_session(tmp_path, db):
    hardware = held(tmp_path, db, "paused")
    unreadable(db, NO_BASE_CO)
    marker = tmp_path / "state" / "stock-marker.json"
    rig(tmp_path, hardware, db=db).store.write_marker("boot-a", 1, 5, (0, 1, 8))
    assert marker.exists()
    code, _, _ = aborted(tmp_path, db, hardware)
    assert not marker.exists()
    assert code == cli.EXIT_ABORTED_UNREAD


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_whose_stock_marker_cannot_be_read_ends_aborted_and_says_so(tmp_path, db):
    hardware = held(tmp_path, db, "paused")
    unreadable(db, NO_BASE_CO)
    marker = tmp_path / "state" / "stock-marker.json"
    marker.write_text("{")
    code, r, said = aborted(tmp_path, db, hardware)
    failed = (
        f"the stock marker could not be settled once the session was stored: the stock marker {marker} holds b'{{',"
        " in no shape a build of this tool writes"
    )
    assert said[-1] == f"database_failed: {failed}"
    assert rows(r, "SELECT message FROM tuner_events WHERE session_id = 1 AND severity = 'database_failed'") == [
        {"message": failed}
    ]
    assert rows(r, "SELECT status FROM tuner_sessions WHERE id = 1") == [{"status": "aborted"}]
    assert code == cli.EXIT_ABORTED_UNREAD


@pytest.mark.req("UNREAD.abort")
def test_the_unread_abort_s_notification_never_says_every_owned_core_went_back(monkeypatch):
    monkeypatch.setattr(live_hw, "load_settings", lambda: SimpleNamespace(notify_on_completion=True))
    sent = []
    monkeypatch.setattr("corecycler.notify.desktop_notify", lambda title, body, urgency: sent.append((title, body)))
    live_hw.notify_outcome(cli.EXIT_ABORTED_UNREAD)
    [(title, body)] = sent
    assert title == "Tuning aborted unread"
    assert body.startswith("The session could not be read") and "every owned core" not in body


@pytest.mark.req("UNREAD.abort", "UNREAD.command")
@pytest.mark.parametrize(("machine_json", "error"), UNREADABLE_ROWS)
@pytest.mark.parametrize("status", ["completed", "quarantined", "aborted"])
def test_an_unreadable_session_that_is_not_held_is_refused_by_the_reader_as_before(
    tmp_path, db, status, machine_json, error
):
    save_machine_session(db, tmp_path, status)
    unreadable(db, machine_json)
    hardware = FakeHardware(co={0: -4, 1: 0, 8: 0})
    writes = recorded(hardware)
    code, r, said = aborted(tmp_path, db, hardware)
    assert said == [f"refused: session 1 could not be read: {error}"]
    assert rows(r, "SELECT status FROM tuner_sessions WHERE id = 1") == [{"status": status}]
    assert (writes, rows(r, ABORTED_EVENTS)) == ([], [])
    assert code == live_hw.EXIT_REFUSED


@pytest.mark.req("UNREAD.abort")
def test_an_unreadable_session_that_is_not_held_is_refused_beside_one_that_is(tmp_path, db):
    save_machine_session(db, tmp_path, "completed", session_id=1)
    unreadable(db, NO_BASE_CO)
    save_machine_session(db, tmp_path, "paused", session_id=2)
    code, r, said = aborted(tmp_path, db, FakeHardware())
    assert said == [f"refused: {UNREAD_BASE_CO}"]
    assert rows(r, "SELECT id, status FROM tuner_sessions ORDER BY id") == [
        {"id": 1, "status": "completed"},
        {"id": 2, "status": "paused"},
    ]
    assert code == live_hw.EXIT_REFUSED


@pytest.mark.req("UNREAD.abort")
def test_an_abort_of_a_session_not_stored_is_refused_as_before(tmp_path, db):
    held(tmp_path, db, "paused")
    unreadable(db, NO_BASE_CO)
    code, r, said = aborted(tmp_path, db, FakeHardware(), session="9")
    assert said == ["refused: session 9 is not stored"]
    assert rows(r, "SELECT id, status FROM tuner_sessions") == [{"id": 1, "status": "paused"}]
    assert rows(r, "SELECT message FROM tuner_events WHERE session_id = 9") == []
    assert code == live_hw.EXIT_REFUSED


@pytest.mark.req("UNREAD.abort")
def test_a_readable_abort_still_reads_the_session_and_sends_every_owned_core_to_its_baseline(tmp_path, db):
    hardware = held(tmp_path, db, "paused")
    hardware.co[0] = -4
    code, r, _ = aborted(tmp_path, db, hardware)
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert rows(r, ABORTED_EVENTS) == [{"message": "aborted on request; every owned core is sent back to its baseline"}]
    assert code == live_hw.EXIT_ABORTED


@pytest.mark.req("UNREAD.abort")
def test_an_unread_abort_waits_for_the_writer_lock_like_every_writer(tmp_path, db, monkeypatch):
    held(tmp_path, db, "paused")
    unreadable(db, NO_BASE_CO)
    monkeypatch.setattr(writer_lock, "lock_path", lambda: tmp_path / "tuner.lock")
    state = ["--db", str(db.path), "--state-dir", str(tmp_path / "state")]
    with writer_lock.co_writer():
        assert cli.cli_main(["abort", "1", *state]) == cli.EXIT_LOCKED
    assert db._execute_raw("SELECT status FROM tuner_sessions WHERE id = 1").fetchone()[0] == "paused"
