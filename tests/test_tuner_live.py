from __future__ import annotations

import json
import signal
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from corecycler.engine import detector as kernel
from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.history.db import HistoryDB
from corecycler.tuner import live, live_hw
from corecycler.tuner.live import Identity, LiveRig, Store, stored_from_json, stored_to_json
from corecycler.tuner.machine.types import (
    Close,
    CrashEvidenceRead,
    Evidence,
    Group,
    Klass,
    Lifecycle,
    MachineConfig,
    Mode,
    PauseReason,
    PauseRequest,
    Report,
    Stage,
    Stored,
)
from corecycler.tuner.machine.types import (
    TestSpec as Spec,
)
from corecycler.tuner.orders import Method
from tests._tuner_world import Resume, fresh, run_to_rest, start

if TYPE_CHECKING:
    from pathlib import Path

CPUS = ((0, (0, 16)), (1, (1, 17)), (8, (8, 24)))
DOMAINS = ((0, 0), (1, 0), (8, 1))
EDGES = {0: -6, 1: -2, 8: -4}
IDENTITY = Identity("bios-1", "cpu-1", None)


def config(**changes) -> MachineConfig:
    base = MachineConfig(
        Mode.CO,
        Method.ALTERNATE,
        (0, 1, 8),
        max_offset=-8,
        k_1t=2,
        k_2t=1,
        wait_between_cores_s=0.0,
        cool_min_s=0.0,
        transient_window_s=60.0,
        soak_seconds=60.0,
    )
    return replace(base, **changes)


@dataclass
class FakeHardware:
    co: dict[int, int] = field(default_factory=lambda: {0: 0, 1: 0, 8: 0})
    boot: str = "boot-a"
    clean: bool = False
    refuse: int = 0
    capped: bool = False
    records: kernel.JournalHarvest = field(default_factory=lambda: kernel.JournalHarvest((), (), None))
    harvests: list = field(default_factory=list)

    def harvest(self, boot_id, since_ns, next_boot):
        self.harvests.append((boot_id, since_ns, next_boot))
        return self.records

    def read_co(self, core):
        return self.co.get(core)

    def write_co(self, core, value):
        if self.refuse:
            self.refuse -= 1
            return "refused for the test"
        self.co[core] = value
        return None

    def temperature(self):
        return 50.0

    def frequency_capped(self):
        return self.capped

    def boot_id(self):
        return self.boot

    def boot_ended_clean(self, boot_id):
        return self.clean

    def asleep_seconds(self):
        return 0.0


@dataclass
class FakeRunner:
    hardware: FakeHardware
    raise_once: bool = False
    seen: list = field(default_factory=list)
    evidence: tuple = ()
    read_ns: int | None = None

    def run(self, spec: Spec, stop: threading.Event) -> Report:
        self.seen.append(spec)
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("runner broke")
        live_value = self.hardware.co[spec.core]
        assert live_value == spec.value
        failed = live_value < EDGES[spec.core]
        return Report(Group.SILICON if failed else Group.PASS, 1.0, evidence=self.evidence, kernel_read_ns=self.read_ns)


OPENED: list[HistoryDB] = []


def database(path) -> HistoryDB:
    db = HistoryDB(path)
    OPENED.append(db)
    return db


@pytest.fixture(autouse=True)
def close_databases():
    yield
    while OPENED:
        OPENED.pop().close()


def rig(tmp_path: Path, hardware: FakeHardware | None = None, runner=None, db: HistoryDB | None = None) -> LiveRig:
    hardware = hardware or FakeHardware()
    return LiveRig(
        hardware=hardware,
        runner=runner or FakeRunner(hardware),
        store=Store(db or database(":memory:"), tmp_path / "state", IDENTITY),
        cpus_of=CPUS,
        domain_of=DOMAINS,
        say=lambda line: None,
    )


def snapshots() -> list:
    fake = fresh(3, [-3, -1, -2], 2, max_offset=-5, k_1t=2, k_2t=1, wait_between_cores_s=0.0, endurance=True)
    fake.stacks = (((0, 1), (0, 1), 3),)
    fake.edges_memory = {0: -1, 1: -1, 2: -1}
    start(fake)
    seen = []
    crashed = False
    while fake.progress():
        if fake.db is not None:
            seen.append(fake.db)
        in_live1 = fake.state.flow is not None and fake.state.flow.stage is Stage.LIVE1
        if in_live1 and fake.worker is not None and not crashed:
            crashed = True
            fake.crash()
            fake.post(Resume(1))
            seen.append(fake.db)
    return seen


def test_every_stored_state_of_a_run_survives_the_session_file_round_trip():
    stored = snapshots()
    assert len(stored) > 20
    expected = {
        Stage.SEARCH,
        Stage.LIVE1,
        Stage.BISECT,
        Stage.STEADY,
        Stage.TRANSIENT,
        Stage.ENDURANCE,
        Stage.MEMORY,
        Stage.SOAK,
    }
    assert {s.flow.stage for s in stored if s.flow is not None} >= expected
    assert stored[-1].status == "completed"
    assert any(s.flow.live1.back is Stage.MEMORY for s in stored if s.flow is not None and s.flow.live1 is not None)
    for st in stored:
        assert stored_from_json(json.loads(json.dumps(stored_to_json(st)))) == st


def rows(r: LiveRig, sql: str, params: tuple = ()) -> list:
    return [dict(row) for row in r.store.db._execute_raw(sql, params).fetchall()]


def test_the_store_keeps_the_session_in_the_database_and_the_stock_marker_apart(tmp_path):
    store = Store(database(tmp_path / "history.db"), tmp_path / "s", IDENTITY)
    st = snapshots()[-1]
    assert store.load(st.session_id) is None
    store.save(st, "boot-a")
    assert store.load(st.session_id) == (st, "boot-a", None)
    reopened = Store(database(tmp_path / "history.db"), tmp_path / "s", IDENTITY)
    assert reopened.load(st.session_id) == (st, "boot-a", None)
    session = reopened.db.get_tuner_session(st.session_id)
    assert session is None
    assert reopened.new_session_id() == st.session_id + 1
    assert store.marker_boot() is None
    store.write_marker("boot-a")
    assert store.marker_boot() == "boot-a"
    store.clear_marker()
    store.clear_marker()
    assert store.marker_boot() is None


def test_a_live_rig_finds_every_edge_and_leaves_the_final_values_applied(tmp_path):
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    final = r.run_until_rest(0.01)
    assert final.lifecycle is Lifecycle.COMPLETED
    assert {c.core: c.value for c in final.cores} == EDGES
    assert r.hardware.co == EDGES
    assert {spec.klass for spec in r.runner.seen} == {Klass.T1, Klass.T2}


def test_a_close_mid_run_then_a_resume_in_the_same_boot_continues_without_a_penalty(tmp_path):
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    for _ in range(5):
        r.step(0.01)
    r.signals.append(Close())
    r.step(0.01)
    assert r.state.lifecycle is Lifecycle.IDLE
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}
    again = rig(tmp_path, r.hardware, db=r.store.db)
    again.resume(1)
    final = again.run_until_rest(0.01)
    assert final.lifecycle is Lifecycle.COMPLETED
    assert all(c.crash_count == 0 for c in final.cores)


def test_a_reboot_during_a_test_convicts_the_tested_core_on_resume(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    tested = r.worker
    in_test = next(c for c in r.state.cores if c.in_test)
    hardware.boot = "boot-b"
    hardware.co = {0: 0, 1: 0, 8: 0}
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    assert tested is not None
    assert again.state.core(in_test.core).crash_count == 1
    assert again.run_until_rest(0.01).lifecycle is Lifecycle.COMPLETED


def test_a_refused_write_pauses_with_the_reason(tmp_path):
    hardware = FakeHardware(refuse=0)
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    hardware.refuse = 1
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.SMU_WRITE


def broken(*_):
    raise sqlite3.OperationalError("disk I/O error")


def test_a_database_that_cannot_be_written_puts_the_cores_at_stock(tmp_path, monkeypatch):
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    monkeypatch.setattr(r.store, "save", broken)
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert r.store.marker_boot() == "boot-a"
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}


def test_a_merged_session_resumes_under_its_new_id(tmp_path):
    first = rig(tmp_path / "a", db=database(tmp_path / "a" / "history.db"))
    first.start(config(), 1, 7, True)
    first.signals.append(PauseRequest())
    first.run_until_rest(0.01)
    target = database(tmp_path / "b.db")
    own = target.create_tuner_session("{}", "bios-1", "cpu-1", boot_id="boot-a")
    target.merge_from(tmp_path / "a" / "history.db")
    store = Store(target, tmp_path / "b", IDENTITY)
    loaded, _boot, _read = store.load(own + 1)
    assert loaded.session_id == own + 1
    assert loaded.cores == first.state.cores


def test_a_deleted_session_s_id_is_never_handed_out_again(tmp_path):
    r = rig(tmp_path)
    r.start(config(), r.store.new_session_id(), 7, True)
    r.signals.append(PauseRequest())
    r.run_until_rest(0.01)
    r.store.db.delete_tuner_session(1)
    assert r.store.new_session_id() == 2


def test_a_database_that_fails_every_write_still_comes_to_rest_at_stock(tmp_path, monkeypatch):
    r = rig(tmp_path)
    events = []

    def event(*_):
        events.append(1)
        if len(events) > 20:
            raise RuntimeError("the rig kept writing events to a broken database")
        raise sqlite3.OperationalError("disk I/O error")

    r.start(config(), 1, 7, True)
    for name in ("save", "journal_intent", "test_started"):
        monkeypatch.setattr(r.store, name, broken)
    monkeypatch.setattr(r.store, "event", event)
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}


def test_every_test_leaves_one_row_that_its_verdict_completes(tmp_path):
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    tests = rows(r, "SELECT * FROM tuner_tests WHERE session_id=1 ORDER BY id")
    assert [(t["core"], t["value"], t["klass"], t["stage"]) for t in tests] == [
        (s.core, s.value, s.klass.value, s.stage.value) for s in r.runner.seen
    ]
    assert all(t["ended_at"] is not None and t["seconds_run"] == 1.0 for t in tests)
    assert {t["verdict"] for t in tests} == {"pass", "silicon"}
    assert [(json.loads(t["cpus_json"]), json.loads(t["vector_json"])) for t in tests] == [
        (list(s.cpus), [list(pair) for pair in s.vector]) for s in r.runner.seen
    ]


def test_a_reboot_during_a_test_leaves_that_test_s_row_open(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    in_test = next(c for c in r.state.cores if c.in_test)
    open_rows = rows(r, "SELECT core, value FROM tuner_tests WHERE ended_at IS NULL")
    assert open_rows == [{"core": in_test.core, "value": in_test.value}]


@pytest.mark.parametrize(("evidence", "survived"), [((), 1), (tuple(Evidence(c, True) for c in (0, 1, 8)), 0)])
def test_a_test_that_comes_back_marks_its_values_survived_unless_a_corrected_check_named_the_core(
    tmp_path, evidence, survived
):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, FakeRunner(hardware, evidence=evidence))
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    first = r.runner.seen[0]
    marks = rows(r, "SELECT survived FROM tuner_co_journal WHERE core_id=? AND value=?", (first.core, first.value))
    assert marks == [{"survived": survived}]


def in_flight(r: LiveRig):
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    return next(c for c in r.state.cores if c.in_test)


def resume_read(tmp_path, hardware, db, monkeypatch) -> tuple[CrashEvidenceRead, LiveRig, list[str]]:
    reads = []
    real = live.transition

    def spy(state, event, world):
        if isinstance(event, CrashEvidenceRead):
            reads.append(event)
        return real(state, event, world)

    monkeypatch.setattr(live, "transition", spy)
    said = []
    again = rig(tmp_path, hardware, db=db)
    again.say = said.append
    again.resume(1)
    assert len(reads) == 1
    return reads[0], again, said


def test_a_crash_read_names_the_unproven_values_resident_at_death(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    tested = in_flight(r)
    first_write = rows(r, "SELECT MIN(mono_ns) AS ns FROM tuner_co_journal WHERE session_id=1")[0]["ns"]
    hardware.boot = "boot-b"
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.suspects == (tested.core,)
    assert read.other_session_wrote is False
    assert hardware.harvests == [("boot-a", first_write, True)]


def test_a_crash_read_starts_where_the_last_test_s_kernel_read_stopped(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, FakeRunner(hardware, read_ns=123_456))
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    while r.worker is None:
        r.step(0.01)
    hardware.boot = "boot-b"
    resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert hardware.harvests == [("boot-a", 123_456, True)]


def stored_with_fb(fb: int | None) -> Stored:
    st = snapshots()[-1]
    return replace(st, session_id=1, cores=tuple(replace(c, fb=fb if c.core == 0 else c.fb) for c in st.cores))


def test_a_write_inside_a_core_s_survived_envelope_is_journaled_survived(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    proven = store.journal_intent(1, 0, -4, "boot-a", 1)
    store.journal_confirm(proven)
    store.db.journal_mark_survived(1, boot_id="boot-a")
    survived = {
        value: store.db._execute_raw("SELECT survived FROM tuner_co_journal WHERE id=?", (row,)).fetchone()[0]
        for value, row in ((v, store.journal_intent(1, 0, v, "boot-a", 2)) for v in (-3, -4, -5))
    }
    assert survived == {-3: 1, -4: 1, -5: 0}


def test_a_commit_retracts_survived_values_at_or_past_a_new_fail_bound(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    for value in (-2, -4):
        row = store.journal_intent(1, 0, value, "boot-a", 1)
        store.journal_confirm(row)
        store.db.journal_mark_survived(1, boot_id="boot-a")
    assert store.db.journal_envelope(1, 0) == -4
    store.save(stored_with_fb(-3), "boot-a")
    assert store.db.journal_envelope(1, 0) == -2


def test_another_session_writing_later_in_the_dead_boot_is_seen(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    other = r.store.db.create_tuner_session("{}", "", "", boot_id="boot-a")
    r.store.db.journal_co_intent(other, 1, -3, False, boot_id="boot-a", mono_ns=1)
    hardware.boot = "boot-b"
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.other_session_wrote is True


def test_machine_check_records_of_the_dead_boot_become_evidence(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    hardware.records = kernel.JournalHarvest(
        (
            kernel.CoreMachineCheck(16, 5, False, "core 0 bank 5", 1.0),
            kernel.UnblamedMachineCheck(3, 30, False, "data fabric", 2.0),
            kernel.KernelTrouble("oops", 3.0),
        ),
        (kernel.CoreMachineCheck(1, 5, True, "core 1 corrected", 0.5),),
        None,
    )
    hardware.boot = "boot-b"
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.evidence == (Evidence(0, False), Evidence(1, True))
    assert read.shared_uncorrected is True
    assert read.kernel_problem is None


def test_a_dead_boot_whose_kernel_log_cannot_be_read_is_resumed_and_named(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    hardware.records = kernel.JournalHarvest((), (), "no journal for boot-a")
    hardware.boot = "boot-b"
    read, again, said = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.kernel_problem == "no journal for boot-a"
    assert any(line.startswith("kernel_log_unread: ") and "no journal for boot-a" in line for line in said)
    assert rows(again, "SELECT count(*) AS n FROM tuner_events WHERE severity='kernel_log_unread'") == [{"n": 1}]


def test_a_resume_in_the_same_boot_reads_only_this_boot_s_records(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    r.signals.append(Close())
    r.step(0.01)
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.same_boot is True
    assert [(boot, next_boot) for boot, _, next_boot in hardware.harvests] == [("boot-a", False)]


def test_every_co_write_is_journaled_before_it_lands_and_confirmed_once_it_does(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    write = hardware.write_co
    pending = []

    def journaled_write(core, value):
        last = rows(r, "SELECT core_id, value, confirmed FROM tuner_co_journal ORDER BY id DESC LIMIT 1")
        pending.append(last == [{"core_id": core, "value": value, "confirmed": 0}])
        return write(core, value)

    hardware.write_co = journaled_write
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    assert pending and all(pending)
    journal = rows(r, "SELECT confirmed, boot_id FROM tuner_co_journal")
    assert len(journal) == len(pending)
    assert all(j == {"confirmed": 1, "boot_id": "boot-a"} for j in journal)


def test_a_refused_write_leaves_its_journal_row_unconfirmed(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    hardware.refuse = 1
    assert r.run_until_rest(0.01).pause.reason is PauseReason.SMU_WRITE
    assert rows(r, "SELECT count(*) AS n FROM tuner_co_journal WHERE confirmed=0") == [{"n": 1}]


def test_a_journal_that_cannot_be_written_stops_the_write_and_pauses_as_database(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    writes = []
    write = hardware.write_co
    hardware.write_co = lambda core, value: writes.append((core, value)) or write(core, value)
    monkeypatch.setattr(r.store, "journal_intent", broken)
    r.start(config(), 1, 7, True)
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert sorted(writes) == [(0, 0), (1, 0), (8, 0)]
    assert r.runner.seen == []


def test_a_test_row_that_cannot_be_written_starts_no_worker(tmp_path, monkeypatch):
    r = rig(tmp_path)
    monkeypatch.setattr(r.store, "test_started", broken)
    r.start(config(), 1, 7, True)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.DATABASE
    assert r.runner.seen == []


def test_a_write_whose_journal_cannot_be_confirmed_pauses_as_database(tmp_path, monkeypatch):
    r = rig(tmp_path)
    monkeypatch.setattr(r.store, "journal_confirm", broken)
    r.start(config(), 1, 7, True)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.DATABASE
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}


def pause_mid_test(r: LiveRig) -> None:
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    r.signals.append(PauseRequest())


@pytest.mark.parametrize("failing", ["journal_intent", "journal_confirm"])
def test_a_revert_whose_journal_fails_still_reverts_and_pauses_as_database(tmp_path, monkeypatch, failing):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    pause_mid_test(r)
    r.step(0.01)
    monkeypatch.setattr(r.store, failing, broken)
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert hardware.co == {0: 0, 1: 0, 8: 0}


def test_a_test_result_that_cannot_be_written_pauses_as_database(tmp_path, monkeypatch):
    r = rig(tmp_path)
    monkeypatch.setattr(r.store, "test_returned", broken)
    r.start(config(), 1, 7, True)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.DATABASE
    assert len(r.runner.seen) == 1


def test_a_late_report_for_an_old_test_changes_no_row(tmp_path):
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    before = rows(r, "SELECT * FROM tuner_tests ORDER BY id")
    r.inbox.put(live.WorkerReported(999, Report(Group.SILICON, 5.0)))
    assert r.step(0.01) is True
    assert rows(r, "SELECT * FROM tuner_tests ORDER BY id") == before


def test_a_session_that_cannot_be_read_back_at_resume_pauses_as_database(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    pause_mid_test(r)
    r.run_until_rest(0.01)
    again = rig(tmp_path, hardware, db=r.store.db)
    load = again.store.load
    reads = []

    def second_read_fails(session_id):
        reads.append(session_id)
        if len(reads) > 1:
            raise sqlite3.OperationalError("disk I/O error")
        return load(session_id)

    monkeypatch.setattr(again.store, "load", second_read_fails)
    again.resume(1)
    assert again.state.pause.reason is PauseReason.DATABASE
    assert reads == [1, 1]


def test_every_announcement_is_kept_as_a_session_event(tmp_path):
    said = []
    r = rig(tmp_path)
    r.say = said.append
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    events = rows(r, "SELECT severity, message, boot_id FROM tuner_events WHERE session_id=1 ORDER BY id")
    announced = [line for line in said if line.split(":")[0] in {"started", "completed"}]
    assert [
        f"{e['severity']}: {e['message']}" for e in events if e["severity"] in {"started", "completed"}
    ] == announced
    assert announced and all(e["boot_id"] == "boot-a" for e in events)


def test_a_start_that_is_refused_writes_no_session(tmp_path):
    hardware = FakeHardware(co={0: -5, 1: 0, 8: 0})
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, False)
    assert r.state.lifecycle is Lifecycle.IDLE
    assert rows(r, "SELECT count(*) AS n FROM tuner_sessions") == [{"n": 0}]


def test_a_runner_that_raises_pauses_as_internal(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, FakeRunner(hardware, raise_once=True))
    r.start(config(), 1, 7, True)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.INTERNAL
    tests = rows(r, "SELECT verdict, detail FROM tuner_tests")
    assert tests == [{"verdict": "internal_error", "detail": "the test runner raised RuntimeError('runner broke')"}]


def test_a_pause_signal_finishes_the_test_and_a_refusal_is_said(tmp_path):
    said = []
    r = rig(tmp_path)
    r.say = said.append
    r.start(config(), 1, 7, True)
    r.signals.append(PauseRequest())
    assert r.run_until_rest(0.01).pause.reason is PauseReason.USER
    r.start(config(), 2, 7, True)
    assert any(line.startswith("refused: a session is held") for line in said)


def test_the_log_names_every_test_and_its_verdict(tmp_path):
    said = []
    r = rig(tmp_path)
    r.say = said.append
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    tests = [line for line in said if line.startswith("test ")]
    verdicts = [line for line in said if line.startswith("verdict ")]
    assert tests and len(tests) == len(verdicts)
    assert all(t.split(":")[0][5:] == v.split(":")[0][8:] for t, v in zip(tests, verdicts, strict=True))
    assert any(" search 1t at " in t for t in tests) and any(": silicon after " in v for v in verdicts)


def test_a_resume_of_an_unknown_session_is_refused(tmp_path):
    said = []
    r = rig(tmp_path)
    r.say = said.append
    r.resume(9)
    assert said == ["refused: session 9 is not stored"]


def test_the_rig_waits_for_the_timer_when_nothing_arrives(tmp_path):
    r = rig(tmp_path)
    assert r.step(0.0) is False
    r.timer = (5, r.clock() + 60.0)
    assert r.step(0.0) is False


@dataclass
class FakeSmu:
    co: dict = field(default_factory=dict)
    accept: bool = True
    echo: int | None = None

    def get_co_offset(self, core):
        return self.echo if self.echo is not None else self.co.get(core, 0)

    def set_co_offset(self, core, value):
        if self.accept:
            self.co[core] = value
        return self.accept


def sysfs(tmp_path: Path, scaling: int, top: int, boost: str | None, pstate: bool = True) -> Path:
    freq = tmp_path / "cpu" / "cpu0" / "cpufreq"
    freq.mkdir(parents=True)
    (freq / "scaling_max_freq").write_text(str(scaling))
    (freq / ("amd_pstate_max_freq" if pstate else "cpuinfo_max_freq")).write_text(str(top))
    if boost is not None:
        (tmp_path / "cpu" / "cpufreq").mkdir()
        (tmp_path / "cpu" / "cpufreq" / "boost").write_text(boost)
    return tmp_path / "cpu"


def test_the_smu_adapter_writes_only_when_asked_and_checks_the_read_back():
    said = []
    dry = live_hw.SmuHardware(FakeSmu(), False, said.append)
    assert dry.write_co(3, -5) is None and dry.smu.co == {} and said == ["dry: core 3 would be set to -5"]
    real = live_hw.SmuHardware(FakeSmu(), True, said.append)
    assert real.write_co(3, -5) is None and real.read_co(3) == -5
    assert said[-1] == "core 3 set to -5, read back -5"
    assert "refused" in live_hw.SmuHardware(FakeSmu(accept=False), True, said.append).write_co(3, -5)
    assert "-4" in live_hw.SmuHardware(FakeSmu(echo=-4), True, said.append).write_co(3, -5)


@pytest.mark.parametrize(
    ("scaling", "top", "boost", "pstate", "capped"),
    [
        (4300000, 5756452, "1", True, True),
        (5756452, 5756452, "1", True, False),
        (5756452, 5756452, "0", True, True),
        (5700000, 5700000, None, False, False),
    ],
)
def test_a_frequency_cap_or_disabled_boost_is_seen(tmp_path, scaling, top, boost, pstate, capped):
    hw = live_hw.SmuHardware(FakeSmu(), False, print, sysfs(tmp_path, scaling, top, boost, pstate))
    assert hw.frequency_capped() is capped


def test_the_adapter_reads_the_kernel_facts_it_needs(monkeypatch):
    monkeypatch.setattr(live_hw.kernel, "read_boot_id", lambda: "b1")
    monkeypatch.setattr(live_hw.kernel, "boot_end", lambda boot: kernel.BootEnd(clean=boot == "b0", problem=None))
    monkeypatch.setattr(live_hw, "read_cpu_temperature", lambda: 61.0)
    hw = live_hw.SmuHardware(FakeSmu(), False, print)
    assert (hw.boot_id(), hw.boot_ended_clean("b0"), hw.boot_ended_clean("b9"), hw.temperature()) == (
        "b1",
        True,
        False,
        61.0,
    )
    clocks = {live_hw.time.CLOCK_BOOTTIME: 130.0, live_hw.time.CLOCK_MONOTONIC: 100.0}
    monkeypatch.setattr(live_hw.time, "clock_gettime", clocks.__getitem__)
    assert hw.asleep_seconds() == 30.0
    asked = []
    found = kernel.JournalHarvest((), (), None)
    monkeypatch.setattr(
        live_hw.kernel, "harvest_after", lambda boot, after, next_boot: asked.append((boot, after, next_boot)) or found
    )
    assert hw.harvest("b0", 5_000, True) is found
    assert hw.harvest("b1", None, False) is found
    assert asked == [("b0", kernel.HarvestFrom(mono_ns=5_000, realtime_us=None), True), ("b1", None, False)]


@pytest.mark.parametrize(
    ("outcome", "group"),
    [
        (Outcome.PASS, Group.PASS),
        (Outcome.COMPUTATION, Group.SILICON),
        (Outcome.THERMAL, Group.THERMAL),
        (Outcome.UNATTRIBUTED_MCE, Group.ENVIRONMENT),
        (Outcome.STALL, Group.APPARATUS),
    ],
)
def test_each_outcome_becomes_its_verdict_group(outcome, group):
    result = StressResult(core_id=0, outcome=outcome, detail="d", duration_seconds=2.0)
    report = live_hw.report_from(result, [], (0,), {0: 0})
    assert report.group is group
    assert report.detail == ("" if outcome is Outcome.PASS else f"{outcome.value}: d")


def test_machine_checks_on_other_cores_become_evidence_and_unblamed_corrected_ones_are_flagged():
    result = StressResult(core_id=0, outcome=Outcome.UNATTRIBUTED_MCE, detail="d", duration_seconds=1.0)
    observed = [
        kernel.CoreMachineCheck(cpu=8, bank=1, corrected=True, message="m", raw_ts=0.0),
        kernel.CoreMachineCheck(cpu=0, bank=1, corrected=False, message="own", raw_ts=0.0),
        kernel.CoreMachineCheck(cpu=99, bank=1, corrected=True, message="unknown cpu", raw_ts=0.0),
        kernel.UnblamedMachineCheck(logged_by_cpu=3, bank=5, corrected=True, message="u", raw_ts=0.0),
    ]
    report = live_hw.report_from(result, observed, (0, 16), {0: 0, 16: 0, 8: 8})
    assert [(e.core, e.corrected) for e in report.evidence] == [(8, True)]
    assert report.unblamed_corrected is True
    trouble = [*observed, kernel.KernelTrouble(message="oops", raw_ts=0.0)]
    assert live_hw.report_from(result, trouble, (0,), {0: 0, 8: 8}).unblamed_corrected is False


def test_the_runner_hands_each_class_its_own_tests_seconds_and_binary(tmp_path, monkeypatch):
    seen = []

    class FakeDetector:
        read_started_ns = 4_242

        def reset(self):
            seen.append("reset")

        def close(self):
            seen.append("close")

    class FakeSupervisor:
        def __init__(self, **kwargs):
            seen.append(kwargs["phase"])

        def run(self, lanes, cfg, duration):
            seen.append((lanes[0].cpus, cfg.tests, cfg.seconds_per_test, cfg.binary, duration))
            return {
                lanes[0].core_id: StressResult(
                    core_id=lanes[0].core_id, outcome=Outcome.PASS, detail="ok", duration_seconds=duration
                )
            }

    monkeypatch.setattr(live_hw.kernel, "ErrorDetector", FakeDetector)
    runner = live_hw.StressRunner(lambda: "backend", tmp_path, {0: 0, 16: 0}, FakeSupervisor)
    one = runner.run(Spec(Klass.T1, 0, -5, (16,), 60.0, ((0, -5),), Stage.SEARCH), threading.Event())
    two = runner.run(Spec(Klass.T2, 0, -5, (0, 16), 90.0, ((0, -5),), Stage.SEARCH), threading.Event())
    assert one.group is Group.PASS and two.group is Group.PASS
    assert one.kernel_read_ns == two.kernel_read_ns == 4_242
    assert ((16,), ("SFTv4", "FFTv4", "N63"), 20, None, 60.0) in seen
    assert ((0, 16), ("SFTv4", "N63", "VT3"), 30, "19-ZN2 ~ Kagari", 90.0) in seen
    assert seen.count("reset") == 2 and seen.count("close") == 2


def topology():
    cores = {core: SimpleNamespace(logical_cpus=cpus, ccd=dict(DOMAINS)[core]) for core, cpus in CPUS}
    return SimpleNamespace(cores=cores, model_name="cpu-1")


def test_the_command_line_starts_or_resumes_and_reports_where_it_came_to_rest(tmp_path):
    lines = []
    args = live_hw.parse(
        [
            "--state-dir",
            str(tmp_path / "s"),
            "--start",
            "--cores",
            "0,1,8",
            "--max-offset",
            "-8",
            "--k1",
            "1",
            "--k2",
            "1",
            "--write",
            "--acknowledge",
            "--wait-between-cores",
            "0",
        ]
    )
    db = database(tmp_path / "built.db")
    built = live_hw.build_rig(args, topology(), FakeSmu(), lambda: None, lines.append, db, IDENTITY)
    assert built.hardware.write is True and built.cpus_of == CPUS and built.domain_of == DOMAINS
    assert built.store.db is db
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.say = lines.append
    r.runner = FakeRunner(hardware)
    args.state_dir = tmp_path / "state"
    assert live_hw.run(args, r, lines.append) == 0
    assert "session 1" in lines
    assert any(line.startswith("rest: paused") or line.startswith("rest: completed") for line in lines)
    resumed = live_hw.parse(["--state-dir", str(tmp_path / "state"), "--session", "1", "--resume"])
    assert live_hw.run(resumed, rig(tmp_path, hardware, db=r.store.db), lines.append) == 0
    assert "refused: session 1 is not stored" not in lines


def test_the_identity_names_the_run_context_it_recorded(tmp_path, monkeypatch):
    from corecycler.history import context
    from corecycler.history.db import TuningContextRecord

    db = database(tmp_path / "h.db")
    monkeypatch.setattr(
        context, "capture_system_context", lambda smu, n: TuningContextRecord(bios_version="2401", co_hash="h")
    )
    identity = live_hw.read_identity(db, FakeSmu(), topology())
    assert (identity.bios_version, identity.cpu_model) == ("2401", "cpu-1")
    assert db.get_context(identity.context_id).bios_version == "2401"


def test_a_resume_names_its_session(capsys):
    with pytest.raises(SystemExit):
        live_hw.parse(["--state-dir", "s", "--resume"])
    assert "--resume needs --session" in capsys.readouterr().err


def test_signals_queue_the_owner_s_controls(tmp_path, monkeypatch):
    installed = {}
    monkeypatch.setattr(live_hw.signal, "signal", lambda number, handler: installed.__setitem__(number, handler))
    r = rig(tmp_path)
    live_hw.install_signals(r)
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGUSR1, signal.SIGUSR2):
        installed[number](number, None)
    assert [type(event).__name__ for event in r.signals] == ["Close", "Close", "PauseRequest", "Abort"]


def test_main_refuses_without_an_smu_and_runs_with_one(tmp_path, monkeypatch, capsys):
    import corecycler.cli
    import corecycler.engine.backends
    import corecycler.engine.topology

    monkeypatch.setattr(corecycler.engine.topology, "detect_topology", topology)
    monkeypatch.setattr(corecycler.cli, "_build_smu", lambda topo: None)
    monkeypatch.setattr(live_hw, "read_identity", lambda db, smu, topo: IDENTITY)
    argv = ["--state-dir", str(tmp_path / "s"), "--db", str(tmp_path / "h.db"), "--session", "1", "--resume"]
    assert live_hw.main(argv) == 2
    assert "unavailable" in capsys.readouterr().err
    monkeypatch.setattr(corecycler.cli, "_build_smu", lambda topo: FakeSmu())
    monkeypatch.setattr(corecycler.engine.backends, "load_all", lambda: None)
    monkeypatch.setattr(live_hw, "install_signals", lambda r: None)
    monkeypatch.setattr(live_hw.SmuHardware, "frequency_capped", lambda self: False)
    assert live_hw.main(argv) == 0
    assert "refused: session 1 is not stored" in capsys.readouterr().out


def test_the_live_module_exports_the_controls():
    assert {"Abort", "Close", "PauseRequest", "LiveRig", "Store"} <= set(live.__all__)
    assert run_to_rest


def test_a_revert_that_fails_is_said_and_the_core_left_unknown(tmp_path):
    said = []
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.say = said.append
    r.start(config(), 1, 7, True)
    hardware.refuse = 4
    r.run_until_rest(0.01)
    assert any(line.startswith("revert of core") for line in said)
    assert None in r.resident.values()


def test_a_sleep_between_tests_makes_the_rig_rewrite_every_core(tmp_path):
    hardware = FakeHardware()
    asleep = iter([0.0] * 3 + [600.0] * 10_000)
    hardware.asleep_seconds = lambda: next(asleep)
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    for _ in range(3):
        r.step(0.01)
    hardware.co = {0: -3, 1: -3, 8: -3}
    r.run_until_rest(0.01)
    assert r.state.lifecycle is Lifecycle.COMPLETED
    assert hardware.co == EDGES
