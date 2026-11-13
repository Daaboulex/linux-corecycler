from __future__ import annotations

import contextlib
import itertools
import json
import os
import queue
import signal
import sqlite3
import struct
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecycler.engine import detector as kernel
from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.execution import Heartbeat
from corecycler.engine.memory_budget import MemoryBudgetUnavailable
from corecycler.history.db import CoRead, HistoryDB, JournalRow
from corecycler.monitor.frequency import PowerState
from corecycler.monitor.hwmon import SensorRead
from corecycler.tuner import live, live_hw, stages, telemetry
from corecycler.tuner.live import (
    BootFacts,
    Identity,
    LiveRig,
    Store,
    charged_value,
    coverage,
    margins,
    own_peaks,
    stored_from_json,
    stored_to_json,
)
from corecycler.tuner.machine.session import _evidence_applied, transition
from corecycler.tuner.machine.types import (
    KNOB_FLOOR,
    Abort,
    AllCore,
    Announce,
    Close,
    ContinueOnSetup,
    CrashEvidenceRead,
    Evidence,
    FailContext,
    Group,
    Incident,
    InFlight,
    InternalError,
    KernelLogRead,
    Klass,
    Lifecycle,
    MachineConfig,
    Mode,
    Pause,
    PauseReason,
    PauseRequest,
    Phase,
    Report,
    RevertAll,
    SessionState,
    Stage,
    Stored,
    ValidateProfile,
)
from corecycler.tuner.machine.types import (
    TestSpec as Spec,
)
from corecycler.tuner.orders import Method
from tests._heavy_lock import GPU_LINE, held_by_another_tool, holder_seen_by_another_tool, said_by
from tests._old_rows import seed_old_session
from tests._tuner_world import UNREADABLE_ROWS, Resume, fresh, run_to_rest, save_machine_session, start
from tests.test_execution import FakeBackend, lane, make_supervisor, run_one, within
from tests.test_smu_core_map import pr23_5600x
from tests.test_tuner_slice_c import FAST, until_test

CPUS = ((0, (0, 16)), (1, (1, 17)), (8, (8, 24)))
DOMAINS = ((0, 0), (1, 0), (8, 1))
EDGES = {0: -6, 1: -2, 8: -4}
IDENTITY = Identity("bios-1", "cpu-1", None)
ONE_CCD = {0: 0, 1: 0}


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
    end_problem: str | None = None
    refuse: int = 0
    power_state: PowerState = field(default_factory=lambda: UNCAPPED)
    records: kernel.JournalHarvest = field(default_factory=lambda: kernel.JournalHarvest((), (), None))
    harvests: list = field(default_factory=list)
    boot_reads: int = 0
    refusal: str | None = None
    revert_refusal: str | None = None
    warnings: tuple = ()
    facts_asked: list = field(default_factory=list)
    setup_now: tuple = (("bios", "A"),)

    def setup(self, cpus_of, domain_of):
        return self.setup_now

    def boot_facts(self, boot_id):
        self.facts_asked.append(boot_id)
        return BOOT_FACTS

    def harvest(self, boot_id, since_ns, next_boot, through_ns):
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

    def power(self):
        return self.power_state

    def preflight(self):
        return self.refusal, self.warnings

    def revert_problem(self):
        return self.revert_refusal

    def boot_id(self):
        self.boot_reads += 1
        return self.boot

    def boot_end(self, boot_id):
        return kernel.BootEnd(clean=self.clean, problem=self.end_problem)

    def asleep_seconds(self):
        return 0.0


@dataclass
class FakeRunner:
    hardware: FakeHardware
    raise_once: bool = False
    seen: list = field(default_factory=list)
    evidence: tuple = ()
    read_ns: int | None = None
    speeds: tuple = ()
    group: Group | None = None
    program_faults: kernel.ProgramFaults | None = None

    def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
        self.seen.append(spec)
        if self.raise_once:
            self.raise_once = False
            raise RuntimeError("runner broke")
        live_value = self.hardware.co[spec.core]
        assert live_value == spec.value
        failed = live_value < EDGES[spec.core]
        return Report(
            self.group or (Group.SILICON if failed else Group.PASS),
            1.0,
            evidence=self.evidence,
            kernel_read_ns=self.read_ns,
            speeds=self.speeds,
            output="Error(s) encountered on logical core 0." if failed else "",
            program_faults=self.program_faults,
        )


BOOT_FACTS = BootFacts(
    bios_version="2401",
    fclk_mhz=2033.0,
    uclk_mhz=3000.0,
    mclk_mhz=3000.0,
    reset_reasons=("x86/amd: Previous system reset reason [0x00200800]: ACPI power state transition occurred",),
    reset_problem=None,
    igpu={
        "present": True,
        "address": "0000:7c:00.0",
        "device": "0x13c0",
        "driver": "amdgpu",
        "vram_total": "536870912",
    },
)
UNCAPPED = PowerState("balanced", "powersave", "balance_performance", "1", 5756.452, 5756.452)


def warm() -> SensorRead:
    return SensorRead(hottest=61.0, tctl=58.0)


def taken(core: int, n: int) -> telemetry.Sample:
    return telemetry.Sample(
        True,
        ((core, 5000.0 + n),),
        60.0 + n,
        mono_ns=1_000 + n,
        tccd=((0, 55.0 + n),),
        volts=(("sio_in0_vcore", 1.1),),
        watts=(("pm_table:package", 120.0 + n),),
        cores=((core, 5000.0 + n, 4990.0 + n, 100.0, 1.1),),
    )


@dataclass
class SamplingRunner(FakeRunner):
    hold: threading.Event | None = None

    def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
        for n in range(telemetry.TAIL_GROUP):
            sink(taken(spec.core, n))
        if self.hold is not None:
            self.hold.wait()
        return super().run(spec, stop, sink)


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


def rig(
    tmp_path: Path,
    hardware: FakeHardware | None = None,
    runner=None,
    db: HistoryDB | None = None,
    check_interval_s: int = kernel.MCE_POLL_KERNEL_DEFAULT_S,
    physical_ccd_of: tuple[tuple[int, int | None], ...] = DOMAINS,
) -> LiveRig:
    hardware = hardware or FakeHardware()
    return LiveRig(
        hardware=hardware,
        runner=runner or FakeRunner(hardware),
        store=Store(db or database(":memory:"), tmp_path / "state", IDENTITY),
        cpus_of=CPUS,
        domain_of=DOMAINS,
        physical_ccd_of=physical_ccd_of,
        co_range=(-50, 10),
        say=lambda line: None,
        check_interval_s=check_interval_s,
    )


@dataclass
class TickingClock:
    ns: int = 1_000_000_000_000

    def __call__(self) -> int:
        self.ns += 1_000
        return self.ns

    def advance(self, seconds: int) -> None:
        self.ns += seconds * 1_000_000_000


@pytest.fixture
def clock(monkeypatch) -> TickingClock:
    ticking = TickingClock()
    monkeypatch.setattr(time, "monotonic_ns", ticking)
    return ticking


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
    fake.post(ValidateProfile(1))
    while fake.progress():
        seen.append(fake.db)
    return seen


@pytest.mark.req(
    "C1",
    "KC.5",
    "L00.c.stored",
    "L01.c1",
    "L05.c.count",
    "L07.c",
    "L08.c",
    "L14.c",
    "L24.l24a.old",
    "L26.c.position",
    "L27.c",
)
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
    assert any(s.flow.validating for s in stored if s.flow is not None)
    assert any(s.flow.live1.back is Stage.MEMORY for s in stored if s.flow is not None and s.flow.live1 is not None)
    for st in stored:
        assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    assert all(st.setup == (("bios", "A"),) for st in stored)
    older = stored_to_json(stored[0])
    del older["setup"]
    with pytest.raises(KeyError, match="setup"):
        stored_from_json(older, continued=False)
    assert all(st.base_co == stored[0].base_co != () for st in stored)
    older = stored_to_json(stored[0])
    del older["base_co"]
    with pytest.raises(KeyError, match="base_co"):
        stored_from_json(older, continued=False)


@pytest.mark.req("T22.F11")
def test_incident_kinds_survive_the_session_file_and_an_older_count_reads_as_kinds_not_recorded():
    st = replace(snapshots()[0], incidents=(Incident.STOCK_MACHINE_CHECK, Incident.CRASH))
    assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    older = stored_to_json(st)
    older["incidents"] = 2
    assert stored_from_json(older, continued=False) == replace(st, incidents=(Incident.UNRECORDED, Incident.UNRECORDED))
    older["incidents"] = 0
    assert stored_from_json(older, continued=False) == replace(st, incidents=())
    older["incidents"] = -1
    with pytest.raises(ValueError, match="incidents"):
        stored_from_json(older, continued=False)


@pytest.mark.req("K12.reclaim.stored")
def test_a_reclaim_in_progress_survives_the_session_file_and_an_older_file_reads_as_none_in_progress():
    fake = fresh(1, [-6], 1, max_offset=-6, k_1t=1, wait_between_cores_s=0.0)
    start(fake)
    fake.fire()
    hung = fake.worker.spec.value
    fake.crash()
    fake.post(Resume(1))
    while fake.db.cores[0].reclaim_floor is None:
        assert fake.progress()
    st = fake.db
    assert st.cores[0].skipped == hung
    assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    older = stored_to_json(st)
    for core in older["cores"]:
        del core["skipped"], core["reclaim_floor"]
    none_in_progress = tuple(replace(r, skipped=None, reclaim_floor=None) for r in st.cores)
    assert stored_from_json(older, continued=False) == replace(st, cores=none_in_progress)


@pytest.mark.req("C1.one_off")
def test_a_one_off_mark_survives_the_session_file_and_an_older_file_reads_as_no_mark_and_resumes():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.STEADY:
        assert fake.progress()
    fake.deliver(Report(Group.SILICON, 1.0, context=FailContext(steady=True, working=(0, 1), paused=False)))
    while fake.db.flow.one_off is None:
        assert fake.progress()
    st = fake.db
    assert st.flow.one_off == (Stage.STEADY, ((0, -3), (1, -3)))
    assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    older = json.loads(json.dumps(stored_to_json(st)))
    del older["flow"]["one_off"]
    assert stored_from_json(older, continued=False) == replace(st, flow=replace(st.flow, one_off=None))
    fake.crash()
    fake.db = stored_from_json(older, continued=False)
    fake.post(Resume(1))
    run_to_rest(fake, 5000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


@pytest.mark.req("L12.failed.stored")
def test_failed_all_core_vectors_survive_the_session_file_and_a_resume_and_an_older_file_remembers_none():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.STEADY:
        assert fake.progress()
    failed = fake.worker.spec.vector
    fake.deliver(Report(Group.SILICON, 1.0))
    while fake.db.flow.one_off is None:
        assert fake.progress()
    st = fake.db
    assert st.failed_vectors == (failed,)
    assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    older = json.loads(json.dumps(stored_to_json(st)))
    del older["failed_vectors"], older["cleared_vectors"], older["clearable_vector"]
    assert stored_from_json(older, continued=False) == replace(
        st, failed_vectors=(), cleared_vectors=(), clearable_vector=None
    )
    fake.crash()
    fake.db = stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False)
    fake.post(Resume(1))
    run_to_rest(fake, 5000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED
    assert {r.core: r.value for r in fake.state.cores} == dict(failed)


@pytest.mark.req("L12.failed.stored")
@pytest.mark.parametrize(
    "written",
    [
        5,
        [[]],
        [[[0, 0], [1, 0]]],
        [[[9, -3]]],
        [[[0, -3]]],
        [[[1, -3], [0, -3]]],
        [[[0, "-3"], [1, -3]]],
        [[[0, True], [1, -3]]],
        [[[0, -3], [1]]],
        [[[0, 1], [1, -3]]],
    ],
    ids=[
        "not a list",
        "an empty vector",
        "every value 0",
        "a core the session does not own",
        "an owned core missing",
        "the cores out of order",
        "a value in text",
        "a value true",
        "a core with no value",
        "a value above 0",
    ],
)
def test_a_failed_vector_the_session_could_not_have_written_refuses_the_stored_session_naming_it(written):
    fake = fresh(2, [-6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    raw = json.loads(json.dumps(stored_to_json(fake.db)))
    raw["failed_vectors"] = written
    with pytest.raises(ValueError, match="failed_vectors"):
        stored_from_json(raw, continued=False)


def a_one_off_stored() -> tuple[Stored, Stored]:
    fake = fresh(2, [-6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.STEADY:
        assert fake.progress()
    failed = fake.worker.spec.vector
    fake.deliver(Report(Group.SILICON, 1.0))
    owed = fake.db
    while fake.db.flow.one_off is None:
        assert fake.progress()
    cleared = fake.db
    assert (owed.clearable_vector, owed.cleared_vectors) == (failed, ())
    assert (cleared.clearable_vector, cleared.cleared_vectors) == (None, (failed,))
    return owed, cleared


@pytest.mark.req("D118.stored")
def test_a_cleared_one_off_and_a_failure_still_clearable_survive_the_session_file_and_an_older_file_has_neither():
    for st in a_one_off_stored():
        raw = json.loads(json.dumps(stored_to_json(st)))
        assert stored_from_json(raw, continued=False) == st
        del raw["cleared_vectors"], raw["clearable_vector"]
        assert stored_from_json(raw, continued=False) == replace(st, cleared_vectors=(), clearable_vector=None)


@pytest.mark.req("D118.stored")
@pytest.mark.parametrize(
    ("name", "written"),
    [
        pytest.param("cleared_vectors", 5, id="cleared: not a list"),
        pytest.param("cleared_vectors", [[]], id="cleared: an empty vector"),
        pytest.param("cleared_vectors", [[[0, -3]]], id="cleared: an owned core missing"),
        pytest.param("cleared_vectors", [[[0, "-3"], [1, -3]]], id="cleared: a value in text"),
        pytest.param("cleared_vectors", [[[0, -3], [1, -3]], [[0, -3], [1, -3]]], id="cleared: a duplicate"),
        pytest.param("cleared_vectors", [[[0, -2], [1, -2]]], id="cleared: a vector not remembered"),
        pytest.param("clearable_vector", 5, id="clearable: not a vector"),
        pytest.param("clearable_vector", [], id="clearable: an empty vector"),
        pytest.param("clearable_vector", [[0, -3]], id="clearable: an owned core missing"),
        pytest.param("clearable_vector", [[0, -2], [1, -2]], id="clearable: a vector not remembered"),
        pytest.param("clearable_vector", [[0, -3], [1, -3]], id="clearable: a vector already cleared"),
    ],
)
def test_a_cleared_or_clearable_vector_the_session_could_not_have_written_refuses_the_session_naming_it(name, written):
    _, cleared = a_one_off_stored()
    assert cleared.failed_vectors == (((0, -3), (1, -3)),)
    raw = json.loads(json.dumps(stored_to_json(cleared)))
    raw[name] = written
    with pytest.raises(ValueError, match=name):
        stored_from_json(raw, continued=False)


@pytest.mark.req("D118.stored", "L12.failed.stored")
@pytest.mark.parametrize(
    ("name", "written"),
    [
        pytest.param("failed_vectors", [[[0.0, -3], [1, -3]]], id="failed: a core in floating point"),
        pytest.param("failed_vectors", [[[0, -3], [True, -3]]], id="failed: a core true"),
        pytest.param("failed_vectors", [[[0, -3], [1, -3]], [[0, -3], [1, -3]]], id="failed: a duplicate"),
        pytest.param("cleared_vectors", [[[0.0, -3], [1, -3]]], id="cleared: a core in floating point"),
        pytest.param("clearable_vector", [[0, -3], [True, -3]], id="clearable: a core true"),
    ],
)
def test_a_stored_vector_naming_a_core_the_session_could_not_have_written_refuses_it(name, written):
    owed, _ = a_one_off_stored()
    raw = json.loads(json.dumps(stored_to_json(owed)))
    raw[name] = written
    if name != "clearable_vector":
        raw["clearable_vector"] = None
    with pytest.raises(ValueError, match=name):
        stored_from_json(raw, continued=False)


@pytest.mark.req("ALLCORE.bisect.stored")
@pytest.mark.parametrize("steady", [True, False])
def test_how_a_bisect_tests_its_groups_survives_the_session_file_and_an_older_file_reads_it_from_its_entry(steady):
    fake = fresh(2, [-6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.STEADY:
        assert fake.progress()
    fake.deliver(Report(Group.SILICON, 1.0, context=FailContext(steady=steady, working=(0, 1), paused=not steady)))
    st = fake.db
    assert (st.flow.bisect.entry, st.flow.bisect.quick) == (Stage.STEADY, steady)
    assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    older = json.loads(json.dumps(stored_to_json(st)))
    del older["flow"]["bisect"]["quick"]
    quick = replace(st.flow.bisect, quick=True)
    assert stored_from_json(older, continued=False) == replace(st, flow=replace(st.flow, bisect=quick))
    older["flow"]["bisect"]["entry"] = Stage.TRANSIENT.value
    assert stored_from_json(older, continued=False).flow.bisect.quick is False
    older["flow"]["bisect"]["quick"] = "yes"
    with pytest.raises(ValueError, match="quick"):
        stored_from_json(older, continued=False)


@pytest.mark.req("KERNEL.old")
def test_a_carried_mark_survives_the_session_file_and_an_older_file_takes_it_from_its_origin_and_resumes():
    fake = fresh(2, [-6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.STEADY:
        assert fake.progress()
    fake.post(PauseRequest())
    fake.setup = (("bios", "B"),)
    fake.post(Resume(1))
    fake.post(ContinueOnSetup(2))
    st = fake.db
    assert (st.session_id, st.flow.carried) == (2, True)
    assert stored_from_json(json.loads(json.dumps(stored_to_json(st))), continued=False) == st
    older = json.loads(json.dumps(stored_to_json(st)))
    del older["flow"]["carried"]
    with pytest.raises(TypeError):
        stored_from_json(older)
    assert stored_from_json(older, continued=True) == st
    assert stored_from_json(older, continued=False) == replace(st, flow=replace(st.flow, carried=False))
    while fake.db.flow.live1 is None or fake.db.flow.live1.back is not Stage.FINALIZE:
        assert fake.progress()
    final_pass = fake.db
    assert stored_from_json(json.loads(json.dumps(stored_to_json(final_pass))), continued=False) == final_pass
    fake.crash()
    fake.db = stored_from_json(older, continued=True)
    fake.post(Resume(2))
    run_to_rest(fake, 5000)
    assert fake.state.lifecycle is Lifecycle.COMPLETED


def rows(r: LiveRig, sql: str, params: tuple = ()) -> list:
    return [dict(row) for row in r.store.db._execute_raw(sql, params).fetchall()]


def test_the_store_prunes_the_samples_of_tests_that_passed_more_than_their_kept_days_ago(tmp_path, monkeypatch):
    db = database(":memory:")
    pruned = []
    monkeypatch.setattr(db, "prune_tuner_samples", lambda verdict, before: pruned.append((verdict, before)))
    before = datetime.now(UTC) - timedelta(days=live.PASSED_SAMPLES_KEPT_DAYS)
    Store(db, tmp_path / "state", IDENTITY)
    after = datetime.now(UTC) - timedelta(days=live.PASSED_SAMPLES_KEPT_DAYS)
    [(verdict, cutoff)] = pruned
    assert verdict == Group.PASS.value
    assert before <= datetime.fromisoformat(cutoff) <= after


def test_the_store_hands_its_state_directory_back_to_the_invoking_user(tmp_path, monkeypatch):
    handed: list = []
    monkeypatch.setattr(live, "hand_back", handed.append)
    Store(database(":memory:"), tmp_path / "state", IDENTITY)
    assert handed == [tmp_path / "state"]


@pytest.mark.req("L18.c.marker")
def test_the_store_keeps_the_session_in_the_database_and_the_stock_marker_apart(tmp_path):
    store = Store(database(tmp_path / "history.db"), tmp_path / "s", IDENTITY)
    st = snapshots()[-1]
    assert store.load(st.session_id) == (None, None)
    store.save(st, "boot-a")
    assert store.load(st.session_id) == ((st, "boot-a", None), None)
    reopened = Store(database(tmp_path / "history.db"), tmp_path / "s", IDENTITY)
    assert reopened.load(st.session_id) == ((st, "boot-a", None), None)
    assert reopened.new_session_id() == st.session_id + 1
    assert store.marker_boot(st.session_id) == (None, False)
    store.write_marker("boot-a", st.session_id, 5_000, (0, 8))
    assert (
        store.marker_boot(st.session_id),
        store.marker_boot(st.session_id + 1),
        store.stock_marked("boot-a"),
        store.stock_marked("boot-b"),
    ) == (("boot-a", True), ("boot-a", False), (5_000, ((0, 0), (8, 0))), None)
    store.clear_marker(st.session_id + 1)
    assert store.marker_boot(st.session_id) == ("boot-a", True)
    store.clear_marker(st.session_id + 1, st.session_id)
    store.clear_marker(st.session_id)
    assert (store.marker_boot(st.session_id), store.stock_marked("boot-a")) == ((None, False), None)
    (tmp_path / "s" / "stock-marker.json").write_text(json.dumps({"boot": "boot-a"}))
    assert (store.marker_boot(st.session_id), store.stock_marked("boot-a")) == (("boot-a", False), None)


@pytest.mark.req("L18.c.marker.earlier")
def test_a_stock_marker_an_earlier_build_left_is_the_held_session_s_only_when_it_wrote_the_co_last_before_it(
    tmp_path,
):
    store = Store(database(tmp_path / "history.db"), tmp_path / "s", IDENTITY)
    st = replace(snapshots()[-1], status="running")
    first, second = st.session_id, st.session_id + 1
    store.save(st, "boot-a")
    marker = tmp_path / "s" / "stock-marker.json"

    def left(**fields) -> tuple[tuple[str | None, bool], tuple[str | None, bool]]:
        marker.write_text(json.dumps({"boot": "boot-a", **fields}))
        return store.marker_boot(first), store.marker_boot(second)

    store.journal_intent(first, 0, -4, "boot-a", 1_000)
    assert left(mono_ns=2_000, cores=[0, 8]) == (("boot-a", True), ("boot-a", False))
    assert left(mono_ns=1_000, cores=[0, 8]) == (("boot-a", True), ("boot-a", False))
    assert left(mono_ns=999, cores=[0, 8]) == (("boot-a", False), ("boot-a", False))
    assert left() == (("boot-a", False), ("boot-a", False))
    store.clear_marker(first)
    assert marker.exists()
    left(mono_ns=2_000, cores=[0, 8])
    store.clear_marker(second, first)
    assert not marker.exists()
    store.save(replace(st, session_id=second), "boot-a")
    assert left(mono_ns=2_000, cores=[0, 8]) == (("boot-a", False), ("boot-a", False))
    store.journal_intent(second, 8, -2, "boot-a", 1_500)
    assert left(mono_ns=2_000, cores=[0, 8]) == (("boot-a", False), ("boot-a", True))


@pytest.mark.req("L17.d.clean")
def test_a_live_rig_finds_every_edge_and_leaves_the_final_values_applied(tmp_path):
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    final = r.run_until_rest(0.01)
    assert r.hardware.boot_reads == 1
    assert final.lifecycle is Lifecycle.COMPLETED
    assert {c.core: c.value for c in final.cores} == EDGES
    assert r.hardware.co == EDGES
    assert {spec.klass for spec in r.runner.seen} == {Klass.T1, Klass.T2}


@pytest.mark.req("L18.d.8.baselines")
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


@pytest.mark.req("L00.h")
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


def quarantined_by_three_crashes(tmp_path) -> tuple[FakeHardware, LiveRig]:
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(max_offset=-40, starts=((0, -30), (1, -30), (8, -30))), 1, 7, True)
    for boot in ("boot-b", "boot-c", "boot-d"):
        while r.worker is None:
            r.step(0.01)
        hardware.boot, hardware.co = boot, {0: 0, 1: 0, 8: 0}
        r = rig(tmp_path, hardware, db=r.store.db)
        r.resume(1)
    assert r.state.lifecycle is Lifecycle.QUARANTINED
    return hardware, r


@pytest.mark.req("L18.b.quarantine", "L18.d.8.reengage")
def test_a_quarantined_session_resumes_only_when_reengaged_from_its_journal_s_survived_values(tmp_path):
    hardware, r = quarantined_by_three_crashes(tmp_path)
    r.store.db.journal_co_intent(1, 8, -1, True, boot_id="boot-d", mono_ns=1)
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    assert "--reengage" in again.refused
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1, True)
    assert (again.state.core(8).phase, again.state.core(8).value) == (Phase.PROVEN, -1)
    assert again.run_until_rest(0.01).lifecycle is Lifecycle.COMPLETED


@pytest.mark.req("T23.abort_interrupted", "L18.e.G9", "EVENTS.user.abort_stored")
@pytest.mark.parametrize("crashed", [True, False])
def test_abort_attributes_a_held_session_then_stops_it_without_running_a_test(tmp_path, crashed):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    in_test = next(c for c in r.state.cores if c.in_test)
    if crashed:
        hardware.boot = "boot-b"
    else:
        r.post(PauseRequest())
        r.run_until_rest(0.01)
    hardware.co = {0: -7, 1: -7, 8: -7}
    runner = FakeRunner(hardware)
    again = rig(tmp_path, hardware, runner=runner, db=r.store.db)
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--abort", "--session", "1", "--write"])
    assert live_hw.run(args, again, lambda line: None) == live_hw.EXIT_ABORTED
    assert again.state.core(in_test.core).crash_count == int(crashed)
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert runner.seen == []
    assert rows(again, "SELECT status FROM tuner_sessions WHERE id=1") == [{"status": "aborted"}]


@pytest.mark.req("TRUTH.BOOT_END_UNREAD")
def test_a_reboot_whose_end_cannot_be_read_is_treated_as_a_crash_and_recorded_as_such(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    in_test = next(c for c in r.state.cores if c.in_test)
    hardware.boot, hardware.co, hardware.end_problem = "boot-b", {0: 0, 1: 0, 8: 0}, "journalctl -b exited 1"
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    assert again.state.core(in_test.core).crash_count == 1
    assert rows(again, "SELECT message FROM tuner_events WHERE session_id=1 AND severity='boot_end_unread'") == [
        {"message": "the last boot's end could not be read, so it is treated as a crash: journalctl -b exited 1"}
    ]


def completed_then_rebooted(tmp_path, clean: bool) -> tuple[LiveRig, list[str]]:
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    assert r.run_until_rest(0.01).lifecycle is Lifecycle.COMPLETED
    hardware.boot, hardware.co, hardware.clean = "boot-b", {0: 0, 1: 0, 8: 0}, clean
    said: list[str] = []
    again = rig(tmp_path, hardware, db=r.store.db)
    again.say = said.append
    return again, said


@pytest.mark.parametrize("clean", [True, False])
def test_a_completed_profile_is_validated_in_a_later_boot_and_names_a_crash_of_its_own(tmp_path, clean):
    r, said = completed_then_rebooted(tmp_path, clean)
    r.validate(1)
    final = r.run_until_rest(0.01)
    assert final.lifecycle is Lifecycle.COMPLETED
    assert r.hardware.co == EDGES
    opening = next(line for line in said if line.startswith("validating: "))
    assert ("crashed" in opening) is not clean


@pytest.mark.req("L30.b.crash")
def test_a_crash_over_an_unproven_value_refuses_validation_naming_the_core(tmp_path):
    r, said = completed_then_rebooted(tmp_path, clean=False)
    r.store.journal_confirm(r.store.journal_intent(1, 0, EDGES[0] - 3, "boot-a", 0))
    r.validate(1)
    assert r.state.lifecycle is Lifecycle.IDLE
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}
    assert len(said) == 1 and "cores 0 held unproven values" in said[0]


def test_a_refused_write_pauses_with_the_reason(tmp_path):
    hardware = FakeHardware(refuse=0)
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    hardware.refuse = 1
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.SMU_WRITE


def broken(*_):
    raise sqlite3.OperationalError("disk I/O error")


@pytest.mark.req("INV.L23", "L23.a", "L23.d")
def test_a_database_that_cannot_be_written_puts_the_cores_at_stock(tmp_path, monkeypatch):
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    monkeypatch.setattr(r.store, "save", broken)
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert r.store.marker_boot(1) == ("boot-a", True)
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}


@pytest.mark.parametrize("held", [False, True])
def test_a_database_failure_that_cannot_itself_be_recorded_is_said_and_goes_no_further(tmp_path, monkeypatch, held):
    said = []
    r = rig(tmp_path)
    if held:
        r.start(config(), 1, 7, True)
        r.post(live.Abort())
    r.say = said.append
    monkeypatch.setattr(r.store, "event", broken)
    r.post(InternalError("the runner raised"))
    assert said[-3:] == [
        "internal_error: the runner raised",
        "database_failed: the event could not be written: disk I/O error",
        "the database failure could not be recorded: disk I/O error",
    ]


@pytest.mark.req("L01.G11")
def test_a_session_whose_first_write_to_the_database_fails_never_starts_and_touches_no_core(tmp_path, monkeypatch):
    r = rig(tmp_path, FakeHardware(co={0: -10, 1: 0, 8: 0}))
    monkeypatch.setattr(r.store, "save", broken)
    r.start(config(), 1, 7, True)
    assert (r.state.lifecycle, r.state.session_id) == (Lifecycle.IDLE, None)
    assert r.hardware.co == {0: -10, 1: 0, 8: 0}
    assert r.store.marker_boot(1) == (None, False)


def test_a_stock_marker_that_cannot_be_written_still_puts_the_cores_at_stock(tmp_path, monkeypatch):
    said = []
    r = rig(tmp_path)
    r.say = said.append
    r.start(config(), 1, 7, True)
    while not any(value for value in r.hardware.co.values()):
        r.step(0.01)
    monkeypatch.setattr(r.store, "save", broken)
    monkeypatch.setattr(r.store, "write_marker", lambda *marked: (_ for _ in ()).throw(OSError(28, "No space left")))
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}
    assert any("stock marker could not be written" in line and "No space left" in line for line in said)


def _database_failed(tmp_path, monkeypatch, hardware: FakeHardware) -> LiveRig:
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while not any(hardware.co.values()):
        r.step(0.01)
    monkeypatch.setattr(r.store, "save", broken)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.DATABASE
    monkeypatch.undo()
    return r


@pytest.mark.req("L18.c.marker.session")
def test_a_crash_in_a_session_started_after_another_s_database_failure_reads_no_lost_verdict(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = _database_failed(tmp_path, monkeypatch, hardware)
    r.post(live.Abort())
    r.start(config(), 2, 7, True)
    while r.worker is None:
        r.step(0.01)
    hardware.boot = "boot-b"
    read, again, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch, session_id=2)
    assert (read.dirty_end, read.stale_db, again.state.incidents) == (True, False, ())


STOCK_READS = "SELECT core_id, value FROM tuner_co_reads WHERE session_id = 1 AND mono_ns = ? ORDER BY core_id"


@pytest.mark.req("L18.c.marker.landed")
def test_an_abort_after_a_database_failure_stores_its_stock_moment_deletes_the_marker_and_counts_no_later_crash(
    tmp_path, monkeypatch
):
    hardware = FakeHardware()
    r = _database_failed(tmp_path, monkeypatch, hardware)
    moment, stock = r.store.stock_marked("boot-a")
    r.post(live.Abort())
    assert r.store.marker_boot(1) == (None, False)
    assert rows(r, STOCK_READS, (moment,)) == [{"core_id": core, "value": value} for core, value in stock]
    hardware.boot = "boot-b"
    read, again, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert (read.dirty_end, read.stale_db, again.state.incidents) == (True, False, ())


@pytest.mark.req("L18.c.marker.landed")
def test_an_abort_whose_commit_fails_leaves_the_marker_to_the_next_resume(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = _database_failed(tmp_path, monkeypatch, hardware)
    monkeypatch.setattr(r.store, "save", broken)
    r.post(live.Abort())
    monkeypatch.undo()
    assert r.store.marker_boot(1) == ("boot-a", True)
    hardware.boot = "boot-b"
    read, again, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert (read.stale_db, again.state.incidents) == (True, (Incident.CRASH,))


@pytest.mark.req("L18.c.marker.landed")
def test_another_session_s_abort_in_a_later_boot_keeps_the_marker_of_the_session_whose_database_failed(
    tmp_path, monkeypatch
):
    hardware = FakeHardware()
    db = database(tmp_path / "history.db")
    r = rig(tmp_path, hardware, db=db)
    r.start(config(), 1, 7, True)
    while not any(hardware.co.values()):
        r.step(0.01)
    r.post(live.Abort())
    r.start(config(), 2, 7, True)
    while r.worker is None:
        r.step(0.01)
    r.post(live.DatabaseFailed("injected"))
    assert r.state.pause.reason is PauseReason.DATABASE
    r.post(live.Close())
    hardware.boot, hardware.setup_now = "boot-b", (("bios", "B"),)
    other = rig(tmp_path, hardware, db=db)
    other.resume_for_abort(1, False)
    assert other.state.pause.reason is PauseReason.SETUP_CHANGED
    other.post(live.Abort())
    assert other.state.lifecycle is Lifecycle.ABORTED
    hardware.setup_now = (("bios", "A"),)
    read, again, _ = resume_read(tmp_path, hardware, db, monkeypatch, session_id=2)
    assert (read.stale_db, again.state.incidents) == (True, (Incident.CRASH,))


@pytest.mark.req("L18.c.marker.landed", "L18.d.5.marker", "U55")
def test_a_resume_that_pauses_deletes_its_marker_so_a_later_dirty_boot_counts_no_crash(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = _database_failed(tmp_path, monkeypatch, hardware)
    r.post(live.Close())
    hardware.setup_now = (("bios", "B"),)
    paused = rig(tmp_path, hardware, db=r.store.db)
    paused.resume(1)
    assert paused.run_until_rest(0.01).pause.reason is PauseReason.SETUP_CHANGED
    assert paused.store.marker_boot(1) == (None, False)
    hardware.setup_now, hardware.boot = (("bios", "A"),), "boot-b"
    read, again, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert (read.stale_db, again.state.incidents) == (False, ())


@pytest.mark.req("L18.c.marker.landed", "L18.d.5.marker", "U55")
def test_a_resume_carried_onto_a_new_kernel_stores_the_stock_moment_and_deletes_its_marker(tmp_path, monkeypatch):
    hardware = FakeHardware(setup_now=KERNEL_WAS)
    r = _database_failed(tmp_path, monkeypatch, hardware)
    moment, stock = r.store.stock_marked("boot-a")
    hardware.setup_now = KERNEL_NOW
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1, carry=True)
    assert [(row["id"], row["status"]) for row in lineage(again)] == [(1, "superseded"), (2, "running")]
    assert again.store.marker_boot(1) == (None, False)
    assert rows(again, STOCK_READS, (moment,)) == [{"core_id": core, "value": value} for core, value in stock]


@pytest.mark.req("L18.c.marker.session")
@pytest.mark.parametrize(
    ("owner", "marked", "now", "stale", "marked_now", "said"),
    [
        (1, "boot-a", "boot-b", True, False, False),
        (2, "boot-a", "boot-b", False, False, True),
        (1, "boot-a", "boot-a", True, True, False),
        (2, "boot-a", "boot-a", False, False, True),
        (1, "boot-b", "boot-b", False, True, False),
        (2, "boot-b", "boot-b", False, False, True),
        (2, "boot-z", "boot-b", False, False, False),
    ],
)
def test_a_resume_reads_the_stock_marker_of_its_own_session_alone(
    tmp_path, monkeypatch, owner, marked, now, stale, marked_now, said
):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    r.store.write_marker(marked, owner, time.monotonic_ns(), (0, 1, 8))
    hardware.boot = now
    read, _, lines = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert (read.stale_db, read.marked_now) == (stale, marked_now)
    assert any(f"the stock marker for boot {marked} is not taken as session 1's" in line for line in lines) is said


@pytest.mark.req("L18.c.marker.earlier")
@pytest.mark.parametrize("before", [True, False])
def test_a_resume_takes_an_earlier_build_s_marker_only_when_its_session_wrote_the_co_last_before_it(
    tmp_path, monkeypatch, before
):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    written = max(row.mono_ns for row in r.store.journal_rows(1, "boot-a"))
    moment = written + 1 if before else written - 1
    marker = {"boot": "boot-a", "mono_ns": moment, "cores": [0, 1, 8]}
    (tmp_path / "state" / "stock-marker.json").write_text(json.dumps(marker))
    hardware.boot = "boot-b"
    read, _, lines = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.stale_db is before
    assert any("the stock marker for boot boot-a is not taken as session 1's" in line for line in lines) is not before


@pytest.mark.req("L18.c.marker.earlier")
def test_an_earlier_build_s_marker_is_not_taken_by_a_session_that_is_not_held_though_it_wrote_last(
    tmp_path, monkeypatch
):
    hardware = FakeHardware()
    db = database(tmp_path / "history.db")
    r = rig(tmp_path, hardware, db=db)
    r.start(config(), 1, 7, True)
    while not any(hardware.co.values()):
        r.step(0.01)
    r.post(live.Abort())
    r.start(config(), 2, 7, True)
    assert r.store.journal_rows(2, "boot-a") == []
    marker = {"boot": "boot-a", "mono_ns": time.monotonic_ns(), "cores": [0, 1, 8]}
    (tmp_path / "state" / "stock-marker.json").write_text(json.dumps(marker))
    hardware.boot = "boot-b"
    read, _, lines = resume_read(tmp_path, hardware, db, monkeypatch)
    assert read.stale_db is False
    assert any("the stock marker for boot boot-a is not taken as session 1's" in line for line in lines)


UNREADABLE_MARKERS = [
    b"{",
    b"\xff",
    b"[]",
    b'{"session": 1, "mono_ns": 1, "cores": [0]}',
    b'{"boot": 7}',
    b'{"boot": "boot-a", "mono_ns": 1}',
    b'{"boot": "boot-a", "mono_ns": "late", "cores": [0, 1, 8]}',
    b'{"boot": "boot-a", "session": true, "mono_ns": 1, "cores": [0]}',
    b'{"boot": "boot-b", "session": 1, "mono_ns": 1, "cores": 5}',
    b'{"boot": "boot-a", "mono_ns": 1, "cores": ["0"]}',
]


@pytest.mark.req("L18.c.marker.unreadable")
@pytest.mark.parametrize("raw", UNREADABLE_MARKERS)
def test_a_stock_marker_in_no_shape_a_build_writes_pauses_the_resume_as_database_naming_it(tmp_path, raw):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    path = tmp_path / "state" / "stock-marker.json"
    path.write_bytes(raw)
    hardware.boot = "boot-b"
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    assert again.state.pause.reason is PauseReason.DATABASE
    assert f"the stock marker {path} holds {raw!r}, in no shape a build of this tool writes" in again.state.pause.detail
    assert hardware.co == {0: 0, 1: 0, 8: 0}


@pytest.mark.req("L18.c.marker.unreadable")
def test_a_stock_marker_in_no_shape_a_build_writes_met_once_a_session_is_stored_pauses_it_naming_it(tmp_path):
    r = rig(tmp_path)
    path = tmp_path / "state" / "stock-marker.json"
    path.write_bytes(b"{")
    r.start(config(), 1, 7, True)
    assert (r.state.session_id, r.state.pause.reason) == (1, PauseReason.DATABASE)
    assert f"the stock marker {path} holds {b'{'!r}" in r.state.pause.detail


def denied_on(monkeypatch, path: Path, method: str) -> str:
    real = getattr(Path, method)
    error = PermissionError(13, "Permission denied", str(path))

    def denied(self, *args, **kwargs):
        if self == path:
            raise error
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, denied)
    return str(error)


@pytest.mark.req("L18.c.marker.unreadable")
def test_a_stock_marker_that_cannot_be_read_once_a_session_is_stored_pauses_it_naming_it(tmp_path, monkeypatch):
    r = rig(tmp_path)
    path = tmp_path / "state" / "stock-marker.json"
    r.store.write_marker("boot-a", 9, 1, (0,))
    error = denied_on(monkeypatch, path, "read_bytes")
    r.start(config(), 1, 7, True)
    assert (r.state.session_id, r.state.pause.reason) == (1, PauseReason.DATABASE)
    assert f"the stock marker {path} could not be read: {error}" in r.state.pause.detail
    assert r.hardware.co == {0: 0, 1: 0, 8: 0}


@pytest.mark.req("L18.c.marker.unreadable")
def test_a_stock_marker_that_cannot_be_deleted_once_its_session_is_stored_pauses_it_naming_it(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = _database_failed(tmp_path, monkeypatch, hardware)
    path = tmp_path / "state" / "stock-marker.json"
    error = denied_on(monkeypatch, path, "unlink")
    r.resume(1)
    assert r.state.pause.reason is PauseReason.DATABASE
    assert f"the stock marker {path} could not be deleted: {error}" in r.state.pause.detail
    assert hardware.co == {0: 0, 1: 0, 8: 0}


def test_a_merged_session_resumes_under_its_new_id(tmp_path):
    first = rig(tmp_path / "a", db=database(tmp_path / "a" / "history.db"))
    first.start(config(), 1, 7, True)
    first.signals.append(PauseRequest())
    first.run_until_rest(0.01)
    target = database(tmp_path / "b.db")
    own = seed_old_session(target)
    target.merge_from(tmp_path / "a" / "history.db")
    store = Store(target, tmp_path / "b", IDENTITY)
    (loaded, _boot, _read), _unread = store.load(own + 1)
    assert loaded.session_id == own + 1
    assert loaded.cores == first.state.cores


def test_a_deleted_session_s_id_is_never_handed_out_again(tmp_path):
    r = rig(tmp_path)
    r.start(config(), r.store.new_session_id(), 7, True)
    r.signals.append(PauseRequest())
    r.run_until_rest(0.01)
    r.store.db.delete_tuner_session(1)
    assert r.store.new_session_id() == 2


@pytest.mark.req("L23.b")
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


@pytest.mark.req("PRELUDE.3")
def test_every_test_leaves_one_row_that_its_verdict_completes(tmp_path):
    r = rig(tmp_path)
    r.runner.speeds = (("SFTv4", 6.56e9, True), ("VT3", 2.37e9, False))
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    tests = rows(r, "SELECT * FROM tuner_tests WHERE session_id=1 ORDER BY id")
    assert [(t["core"], t["value"], t["klass"], t["stage"]) for t in tests] == [
        (s.core, s.value, s.klass.value, s.stage.value) for s in r.runner.seen
    ]
    assert all(t["ended_at"] is not None and t["seconds_run"] == 1.0 for t in tests)
    assert {t["verdict"] for t in tests} == {"pass", "silicon"}
    assert all(json.loads(t["speeds_json"]) == [["SFTv4", 6.56e9, True], ["VT3", 2.37e9, False]] for t in tests)
    assert all(json.loads(t["power_json"]) == json.loads(json.dumps(asdict(UNCAPPED))) for t in tests)
    assert {(t["verdict"], t["output"]) for t in tests} == {
        ("pass", ""),
        ("silicon", "Error(s) encountered on logical core 0."),
    }
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


@pytest.mark.req("PRELUDE.4b")
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


@pytest.mark.req("PRELUDE.4c")
@pytest.mark.parametrize("group", [Group.APPARATUS, Group.ENVIRONMENT])
def test_a_test_that_proves_nothing_marks_no_value_survived(tmp_path, group):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, FakeRunner(hardware, group=group))
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    first = r.runner.seen[0]
    marks = rows(r, "SELECT survived FROM tuner_co_journal WHERE core_id=? AND value=?", (first.core, first.value))
    assert marks and all(mark == {"survived": 0} for mark in marks)


def test_a_survival_mark_that_cannot_be_written_is_a_database_failure_naming_it(tmp_path):
    said = []
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.say = said.append

    def broken(session_id, boot, exclude):
        raise sqlite3.Error("disk full")

    r.store.mark_survived = broken
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    assert any(line.startswith("database_failed: the survival mark could not be written: disk full") for line in said)


@pytest.mark.req("L01.2.stored")
def test_a_new_start_is_refused_while_the_newest_session_is_held_and_allowed_once_it_is_not(tmp_path):
    db = database(tmp_path / "held.db")
    first = rig(tmp_path, FakeHardware(), db=db)
    first.start(config(), 1, 7, True)
    first.post(PauseRequest())
    assert first.state.lifecycle in (Lifecycle.PAUSING, Lifecycle.PAUSED)
    second = rig(tmp_path, FakeHardware(), db=db)
    second.start(config(), 2, 7, True)
    assert second.refused is not None and second.refused.startswith("session 1 is paused")
    first.post(live.Abort())
    third = rig(tmp_path, FakeHardware(), db=db)
    third.start(config(), 2, 7, True)
    assert third.refused is None


@pytest.mark.req("C14", "L01.15.changed")
def test_a_new_start_asks_about_a_core_whose_co_differs_from_the_newest_session_s_base_even_at_0(tmp_path):
    db = database(tmp_path / "changed.db")
    first = rig(tmp_path, FakeHardware(co={0: -10, 1: 0, 8: 0}), db=db)
    first.start(config(), 1, 7, True)
    first.post(live.Abort())
    second = rig(tmp_path, FakeHardware(), db=db)
    second.start(config(), 2, 7, False)
    assert second.refused == "the CO differs from when session 1 started: C0 -10 to 0: acknowledge to start"
    third = rig(tmp_path, FakeHardware(), db=db)
    third.start(config(), 2, 7, True)
    assert third.refused is None


@pytest.mark.req("C14")
@pytest.mark.parametrize(("machine_json", "error"), UNREADABLE_ROWS)
def test_a_start_over_a_newest_session_that_cannot_be_read_refuses_naming_it_and_writes_nothing(
    tmp_path, machine_json, error
):
    db = database(tmp_path / "unread.db")
    first = rig(tmp_path, FakeHardware(), db=db)
    first.start(config(), 1, 7, True)
    first.post(live.Abort())
    db._execute_raw(f"UPDATE tuner_sessions SET machine_json = {machine_json} WHERE id = 1")
    hardware = FakeHardware(co={0: 0, 1: 0, 8: -3})
    second = rig(tmp_path, hardware, db=db)
    second.start(config(), 2, 7, False)
    named = (
        "cores [8] read a CO other than 0 at Start, set by the BIOS or written since boot; session 1 could not be "
        f"read: {error}, so whether the CO moved since it started cannot be judged"
    )
    assert second.refused == f"{named}: acknowledge to start"
    assert second.state.lifecycle is Lifecycle.IDLE
    assert rows(second, "SELECT id FROM tuner_sessions") == [{"id": 1}]
    assert rows(second, "SELECT count(*) AS n FROM tuner_events WHERE session_id = 2") == [{"n": 0}]
    assert hardware.co == {0: 0, 1: 0, 8: -3}
    third = rig(tmp_path, hardware, db=db)
    third.start(config(), 2, 7, True)
    assert third.refused is None
    acknowledged = "SELECT message FROM tuner_events WHERE session_id = 2 AND severity = 'acknowledged'"
    assert rows(third, acknowledged) == [{"message": named}]


def completed_session(tmp_path, db: HistoryDB) -> Stored:
    first = rig(tmp_path, FakeHardware(), db=db)
    first.start(config(), 1, 7, True)
    assert first.run_until_rest(0.01).lifecycle is Lifecycle.COMPLETED
    return live.read_stored(db, 1)[0]


def starts_said(r: LiveRig, session_id: int) -> list:
    return rows(r, "SELECT message FROM tuner_events WHERE session_id=? AND severity='starts'", (session_id,))


@pytest.mark.req("L04.a.hint", "Q4.hint")
def test_a_new_session_starts_each_core_at_the_last_completed_value_and_its_stored_events_say_so(tmp_path):
    db = database(tmp_path / "hints.db")
    completed_session(tmp_path, db)
    r = rig(tmp_path, FakeHardware(), db=db)
    r.start(config(), 2, 7, True)
    assert {record.core: record.start for record in live.read_stored(db, 2)[0].cores} == EDGES
    assert starts_said(r, 2) == [
        {"message": "from session 1's values, core 0 at -6, core 1 at -2, core 8 at -4; any other core from 0"}
    ]
    assert r.run_until_rest(0.01).lifecycle is Lifecycle.COMPLETED
    first: dict[int, int] = {}
    for spec in r.runner.seen:
        first.setdefault(spec.core, spec.value)
    assert first == EDGES


@pytest.mark.req("U18")
@pytest.mark.parametrize("status", ["aborted", "quarantined", "superseded"])
def test_the_hint_comes_from_the_newest_completed_session_and_never_one_that_ended_otherwise(tmp_path, status):
    db = database(tmp_path / "hints.db")
    first = completed_session(tmp_path, db)
    r = rig(tmp_path, FakeHardware(), db=db)
    r.store.save(replace(first, session_id=2, cores=tuple(replace(c, value=-1) for c in first.cores)), "boot-a")
    r.store.save(
        replace(first, session_id=3, status=status, cores=tuple(replace(c, value=-3) for c in first.cores)), "boot-a"
    )
    r.start(config(), 4, 7, True)
    assert {record.core: record.start for record in r.state.cores} == {0: -1, 1: -1, 8: -1}


@pytest.mark.req("U18")
@pytest.mark.parametrize(("machine_json", "error"), UNREADABLE_ROWS)
def test_a_completed_session_that_cannot_be_read_ends_the_search_and_the_start_names_it(tmp_path, machine_json, error):
    db = database(tmp_path / "hints.db")
    first = completed_session(tmp_path, db)
    r = rig(tmp_path, FakeHardware(), db=db)
    save_machine_session(db, tmp_path, "completed", session_id=2)
    r.store.save(replace(first, session_id=3, status="aborted"), "boot-a")
    db._execute_raw(f"UPDATE tuner_sessions SET machine_json = {machine_json} WHERE id = 2")
    r.start(config(), 4, 7, True)
    assert {record.core: record.start for record in r.state.cores} == {0: 0, 1: 0, 8: 0}
    assert starts_said(r, 4) == [{"message": f"every core from 0; session 2 could not be read: {error}"}]


def in_flight(r: LiveRig):
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    return next(c for c in r.state.cores if c.in_test)


def resume_read(
    tmp_path, hardware, db, monkeypatch, check_interval_s: int = 0, session_id: int = 1
) -> tuple[CrashEvidenceRead, LiveRig, list[str]]:
    reads = []
    real = live.transition

    def spy(state, event, world):
        if isinstance(event, CrashEvidenceRead):
            reads.append(event)
        return real(state, event, world)

    monkeypatch.setattr(live, "transition", spy)
    said = []
    again = rig(tmp_path, hardware, db=db, check_interval_s=check_interval_s)
    again.say = said.append
    again.resume(session_id)
    assert len(reads) == 1
    return reads[0], again, said


def test_every_sample_a_test_takes_is_kept_under_its_row(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, SamplingRunner(hardware))
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    first = rows(r, "SELECT id, core FROM tuner_tests ORDER BY id LIMIT 1")[0]
    assert r.store.db.tuner_test_samples(first["id"]) == [
        {
            "running": True,
            "tctl": 60.0 + n,
            "mono_ns": 1_000 + n,
            "clocks": [[first["core"], 5000.0 + n]],
            "tccd": [[0, 55.0 + n]],
            "volts": [["sio_in0_vcore", 1.1]],
            "watts": [["pm_table:package", 120.0 + n]],
            "cores": [[first["core"], 5000.0 + n, 4990.0 + n, 100.0, 1.1]],
        }
        for n in range(telemetry.TAIL_GROUP)
    ]
    assert rows(r, "SELECT count(*) AS n FROM tuner_samples") == [{"n": 0}]


def test_a_stage_whose_crash_rule_reads_no_context_keeps_its_samples_in_memory_until_a_block(tmp_path):
    hardware = FakeHardware()
    hold = threading.Event()
    r = rig(tmp_path, hardware, SamplingRunner(hardware, hold=hold))
    r.start(config(), 1, 7, True)
    while len(r.store.unwritten) < telemetry.TAIL_GROUP:
        r.step(0.01)
    hold.set()
    assert r.test_row[2] is False
    assert rows(r, "SELECT count(*) AS n FROM tuner_samples") == [{"n": 0}]
    assert rows(r, "SELECT count(*) AS n FROM tuner_sample_blocks") == [{"n": 0}]


@pytest.mark.req("SC.2")
def test_the_store_writes_a_block_every_block_of_samples_and_at_the_test_s_end(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    row = store.db.insert_tuner_test(1, "boot-a", 1, "steady", "hot", "1t", 0, -3, "[]", "[0]", 60.0, "{}")

    def counts() -> tuple[int, int]:
        tables = ("tuner_samples", "tuner_sample_blocks")
        return tuple(store.db._execute_raw(f"SELECT count(*) FROM {table}").fetchone()[0] for table in tables)

    for n in range(telemetry.BLOCK - 1):
        store.sample(row, taken(0, n), durable=True)
    assert counts() == ((telemetry.BLOCK - 1) // telemetry.TAIL_GROUP * telemetry.TAIL_GROUP, 0)
    store.sample(row, taken(0, telemetry.BLOCK - 1), durable=True)
    assert counts() == (0, 1)
    store.sample(row, taken(0, telemetry.BLOCK), durable=False)
    assert counts() == (0, 1)
    store.test_returned("boot-a", row, Report(Group.PASS, 60.0), ONE_CCD)
    assert counts() == (0, 2)
    assert [s["mono_ns"] for s in store.db.tuner_test_samples(row)] == [1_000 + n for n in range(telemetry.BLOCK + 1)]


@pytest.mark.req("D88", "INV.L25")
def test_the_durable_tail_is_written_a_group_at_a_time_and_only_in_durable_tests(tmp_path, monkeypatch):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    row = store.db.insert_tuner_test(1, "boot-a", 1, "steady", "hot", "1t", 0, -3, "[]", "[0]", 60.0, "{}")
    writes = []
    real = store.db.insert_tuner_samples
    monkeypatch.setattr(
        store.db, "insert_tuner_samples", lambda test, samples: writes.append(len(samples)) or real(test, samples)
    )

    def tail() -> list[int]:
        return [n for (n,) in store.db._execute_raw("SELECT mono_ns FROM tuner_samples ORDER BY id").fetchall()]

    group = telemetry.TAIL_GROUP
    for n in range(group - 1):
        store.sample(row, taken(0, n), durable=True)
    assert tail() == []
    store.sample(row, taken(0, group - 1), durable=True)
    assert tail() == [1_000 + n for n in range(group)]
    for n in range(group, 2 * group):
        store.sample(row, taken(0, n), durable=False)
    assert tail() == [1_000 + n for n in range(group)]
    assert writes == [group]


@pytest.mark.req("CR.2", "D83")
def test_a_test_row_keeps_each_core_s_highest_clock_from_every_sample_it_took(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    row = store.db.insert_tuner_test(1, "boot-a", 1, "steady", "hot", "1t", 0, -3, "[]", "[0, 1]", 60.0, "{}")
    for n, clocks in enumerate((((0, 4100.0), (1, 4200.0)), ((0, 4290.0), (1, None)), ((0, 4150.0), (1, 4310.0)))):
        store.sample(row, telemetry.Sample(True, clocks, 60.0, mono_ns=n), durable=n == 2)
    store.test_returned("boot-a", row, Report(Group.PASS, 60.0), ONE_CCD)
    (saved,) = rows(SimpleNamespace(store=store), "SELECT peaks_json FROM tuner_tests WHERE id=?", (row,))
    assert json.loads(saved["peaks_json"]) == {"0": 4290.0, "1": 4310.0}
    assert own_peaks(store.db.list_tuner_tests(1)) == []


def test_a_test_without_samples_keeps_an_empty_peak_record(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    row = store.db.insert_tuner_test(1, "boot-a", 1, "search", None, "1t", 0, -3, "[]", "[0]", 60.0, "{}")
    store.test_returned("boot-a", row, Report(Group.PASS, 60.0), ONE_CCD)
    assert rows(SimpleNamespace(store=store), "SELECT peaks_json FROM tuner_tests") == [{"peaks_json": "{}"}]
    assert own_peaks(store.db.list_tuner_tests(1)) == []


def test_every_test_end_checkpoints_the_database(tmp_path, monkeypatch):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    returned, broken = (
        store.db.insert_tuner_test(1, "boot-a", n, "search", None, "1t", 0, -3, "[]", "[0]", 60.0, "{}") for n in (1, 2)
    )
    checkpoints = []
    monkeypatch.setattr(store.db, "checkpoint", lambda: checkpoints.append(1))
    store.test_returned("boot-a", returned, Report(Group.PASS, 60.0), ONE_CCD)
    store.test_broken(broken, "the test runner raised")
    assert len(checkpoints) == 2


def test_a_checkpoint_that_fails_at_a_test_end_pauses_as_database(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)

    def full() -> None:
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(r.store.db, "checkpoint", full)
    r.start(config(), 1, 7, True)
    final = r.run_until_rest(0.01)
    monkeypatch.undo()
    assert final.pause.reason is PauseReason.DATABASE
    assert hardware.co == {0: 0, 1: 0, 8: 0}


def test_the_first_test_of_a_rig_records_its_boot_once(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    boots = rows(r, "SELECT * FROM tuner_boots")
    assert len(rows(r, "SELECT id FROM tuner_tests")) > 1
    assert hardware.facts_asked == ["boot-a"]
    assert [(b["boot_id"], b["bios_version"], b["fclk_mhz"], b["uclk_mhz"], b["mclk_mhz"]) for b in boots] == [
        ("boot-a", "2401", 2033.0, 3000.0, 3000.0)
    ]
    assert json.loads(boots[0]["reset_reasons_json"]) == {"lines": list(BOOT_FACTS.reset_reasons), "problem": None}
    assert json.loads(boots[0]["igpu_json"]) == BOOT_FACTS.igpu


def test_a_boot_that_cannot_be_recorded_pauses_as_database(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    monkeypatch.setattr(r.store, "boot_seen", broken)
    r.start(config(), 1, 7, True)
    final = r.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert "this boot's facts" in final.pause.detail
    assert hardware.co == {0: 0, 1: 0, 8: 0}


def test_a_sample_that_cannot_be_written_pauses_as_database(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, SamplingRunner(hardware))
    monkeypatch.setattr(r.store, "sample", broken)
    r.start(config(), 1, 7, True)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.DATABASE
    assert hardware.co == {0: 0, 1: 0, 8: 0}


@pytest.mark.req("L25.h", "SC.4b")
def test_a_crash_s_durable_tail_is_packed_into_the_samples_its_test_left(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "ALLCORE", frozenset(Stage))
    hardware = FakeHardware()
    hold = threading.Event()
    r = rig(tmp_path, hardware, SamplingRunner(hardware, hold=hold))
    r.start(config(), 1, 7, True)
    deadline = time.monotonic() + 30
    while rows(r, "SELECT count(*) AS n FROM tuner_samples")[0]["n"] < telemetry.TAIL_GROUP:
        assert time.monotonic() < deadline, "the durable tail never reached one group"
        r.step(0.01)
    tested = next(c for c in r.state.cores if c.in_test)
    dead = rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NULL")[0]
    left = r.store.db.tuner_test_samples(dead["id"])
    hardware.boot = "boot-b"
    resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    hold.set()
    assert rows(r, "SELECT count(*) AS n FROM tuner_samples WHERE test_id=?", (dead["id"],))[0]["n"] == 0
    assert r.store.db.tuner_test_samples(dead["id"]) == left
    assert [s["mono_ns"] for s in left] == [taken(tested.core, n).mono_ns for n in range(telemetry.TAIL_GROUP)]


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


@pytest.mark.req("PRELUDE.4e")
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


def test_a_broken_test_keeps_the_samples_it_took(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    row = store.db.insert_tuner_test(1, "boot-a", 1, "search", None, "1t", 0, -3, "[]", "[0]", 60.0, "{}")
    store.sample(row, taken(0, 1), durable=False)
    store.test_broken(row, "the test runner raised")
    assert [s["mono_ns"] for s in store.db.tuner_test_samples(row)] == [1_001]


def test_samples_a_test_left_unwritten_go_into_its_block_when_the_next_test_samples(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    first, second = (
        store.db.insert_tuner_test(1, "boot-a", n, "search", None, "1t", 0, -3, "[]", "[0]", 60.0, "{}") for n in (1, 2)
    )
    store.sample(first, taken(0, 1), durable=False)
    store.sample(second, taken(0, 2), durable=False)
    assert [s["mono_ns"] for s in store.db.tuner_test_samples(first)] == [1_001]
    assert store.db.tuner_test_samples(second) == []


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


@pytest.mark.req("L18.d.4.1")
def test_another_session_writing_later_in_the_dead_boot_is_seen(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    other = seed_old_session(r.store.db)
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
        (kernel.CoreMachineCheck(1, 5, True, "core 1 corrected", 2.0),),
        None,
    )
    hardware.boot = "boot-b"
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.evidence == (Evidence(0, False, 0), Evidence(1, True))
    assert read.shared_uncorrected is True
    assert read.kernel_problem is None


def test_kernel_trouble_alone_in_the_dead_boot_is_read_without_raising_and_blames_nothing_shared(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    lockup = kernel.KernelTrouble("watchdog: BUG: soft lockup - CPU#3 stuck for 22s! [y-cruncher:4321]", 1.0)
    hardware.records = kernel.JournalHarvest((lockup,), (), None)
    hardware.boot = "boot-b"
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.dirty_end is True
    assert read.shared_uncorrected is False
    assert read.evidence == ()


@pytest.mark.req("D70")
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


WIREPLUMBER = "wireplumber[2884]: segfault at 0 ip 00007f6d4a3c2e17 sp 00007ffd2b8e6f40 error 4 in libasound.so.2.0.0"
FAULT_ON_CPU_23 = f"{WIREPLUMBER}[5ae17,7f6d4a368000+b6000] likely on CPU 23 (core 7, socket 0)"
TRAP_NAMING_NO_CPU = "traps: foo[12] general protection fault ip:401136 sp:7ffd2b8e6f40 error:0 in foo[401000+1000]"
HARVESTED_FAULTS = kernel.JournalHarvest(
    (kernel.ProgramFault("wireplumber", 2884, 23, 7, FAULT_ON_CPU_23, 689.5),),
    (kernel.ProgramFault("foo", 12, None, None, TRAP_NAMING_NO_CPU, 2.0),),
    None,
)


def program_fault_events(r: LiveRig, session: int) -> list[str]:
    return [e["message"] for e in r.store.db.get_tuner_events(session) if e["severity"] == "program_fault"]


@pytest.mark.req("PROGRAM_FAULT.evidence")
def test_a_test_s_program_faults_are_stored_as_read_with_its_start_and_shown_with_their_approximate_time(tmp_path):
    hardware = FakeHardware()
    fault = kernel.ProgramFault("wireplumber", 2884, 23, 7, FAULT_ON_CPU_23, 115.5)
    r = rig(tmp_path, hardware, FakeRunner(hardware, program_faults=kernel.ProgramFaults(100_000_000_000, (fault,), 3)))
    said: list[str] = []
    r.say = said.append
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    (ended,) = rows(r, "SELECT token, evidence_json FROM tuner_tests WHERE ended_at IS NOT NULL")
    assert json.loads(ended["evidence_json"])["program_faults"] == {
        "started_ns": 100_000_000_000,
        "started_clock": "CLOCK_MONOTONIC",
        "faults": [asdict(fault)],
        "dropped": 3,
    }
    shown = [line for line in said if line.startswith("program_fault: ")]
    assert shown[0] == f"program_fault: 3 earlier program faults in test {ended['token']} were not kept"
    assert shown[1].startswith(
        f"program_fault: wireplumber (pid 2884), CPU 23, core 7, about 15.5 s into test {ended['token']}: "
    )
    assert shown[1].endswith(FAULT_ON_CPU_23)


def test_a_test_without_a_program_fault_read_stores_none_and_shows_none(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    said: list[str] = []
    r.say = said.append
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    (ended,) = rows(r, "SELECT evidence_json FROM tuner_tests WHERE ended_at IS NOT NULL")
    assert json.loads(ended["evidence_json"])["program_faults"] is None
    assert any(line.startswith("verdict ") for line in said)
    assert not [line for line in said if line.startswith("program_fault: ")]


@pytest.mark.req("PROGRAM_FAULT.outside")
def test_the_program_faults_a_resume_reads_from_the_kernel_log_go_into_the_session_s_events(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    hardware.records = HARVESTED_FAULTS
    hardware.boot = "boot-b"
    read, again, said = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.evidence == ()
    assert read.shared_uncorrected is False
    on_cpu_23, naming_none = program_fault_events(again, 1)
    assert on_cpu_23.startswith("wireplumber (pid 2884), CPU 23, core 7, 689.5 s into its boot: ")
    assert on_cpu_23.endswith(FAULT_ON_CPU_23)
    assert naming_none.startswith("foo (pid 12), no CPU named, 2.0 s into its boot: ")
    assert naming_none.endswith(TRAP_NAMING_NO_CPU)
    assert [line.removeprefix("program_fault: ") for line in said if line.startswith("program_fault: ")] == [
        on_cpu_23,
        naming_none,
    ]


@pytest.mark.req("PROGRAM_FAULT.outside")
def test_a_second_resume_in_the_same_boot_announces_no_fault_the_first_announced(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    hardware.records = HARVESTED_FAULTS
    r.signals.append(Close())
    r.step(0.01)
    _, first, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    first.signals.append(Close())
    first.step(0.01)
    _, second, said = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert len(program_fault_events(second, 1)) == 2
    assert not [line for line in said if line.startswith("program_fault: ")]


def looping(pids: range) -> tuple[kernel.ProgramFault, ...]:
    return tuple(
        kernel.ProgramFault("loop", pid, None, None, f"traps: loop[{pid}] trap int3 ip:0 sp:0 error:0", float(pid))
        for pid in pids
    )


def told_by_two_resumes_in_one_boot(tmp_path, monkeypatch, first, second) -> tuple[list[str], list[str]]:
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    r.signals.append(Close())
    r.step(0.01)
    hardware.records = kernel.JournalHarvest(first, (), None)
    _, once, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    before = program_fault_events(once, 1)
    once.signals.append(Close())
    once.step(0.01)
    hardware.records = kernel.JournalHarvest(second, (), None)
    _, twice, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    return before, program_fault_events(twice, 1)[len(before) :]


@pytest.mark.req("PROGRAM_FAULT.outside")
def test_a_second_resume_announces_only_the_faults_after_the_last_one_told_and_no_stale_count(tmp_path, monkeypatch):
    kept = kernel.PROGRAM_FAULTS_KEPT
    first, again = looping(range(1000, 1000 + kept + 20)), looping(range(1000, 1000 + kept + 30))
    before, after = told_by_two_resumes_in_one_boot(tmp_path, monkeypatch, first, again)
    assert before[0] == "20 earlier program faults in that kernel log were not recorded"
    assert [line.partition(", no CPU")[0] for line in before[1:]] == [
        f"loop (pid {pid})" for pid in range(1020, 1000 + kept + 20)
    ]
    assert [line.partition(", no CPU")[0] for line in after] == [
        f"loop (pid {pid})" for pid in range(1000 + kept + 20, 1000 + kept + 30)
    ]


@pytest.mark.req("PROGRAM_FAULT.outside")
def test_a_later_resume_that_drops_as_many_faults_as_an_earlier_one_still_announces_its_count(tmp_path, monkeypatch):
    kept = kernel.PROGRAM_FAULTS_KEPT
    first, later = looping(range(1000, 1000 + kept + 20)), looping(range(2000, 2000 + kept + 20))
    before, after = told_by_two_resumes_in_one_boot(tmp_path, monkeypatch, first, later)
    assert before[0] == after[0] == "20 earlier program faults in that kernel log were not recorded"
    assert len(after) == kept + 1
    assert after[1].startswith("loop (pid 2020), no CPU named, ")


@pytest.mark.req("PROGRAM_FAULT.outside")
def test_a_resume_announces_the_latest_program_faults_and_counts_the_earlier_ones(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    flood = tuple(
        kernel.ProgramFault("loop", n, None, None, f"traps: loop[{n}] trap int3 ip:0 sp:0 error:0", float(n))
        for n in range(kernel.PROGRAM_FAULTS_KEPT + 2)
    )
    hardware.records = kernel.JournalHarvest(flood, (), None)
    hardware.boot = "boot-b"
    _, again, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    told = program_fault_events(again, 1)
    assert told[0] == "2 earlier program faults in that kernel log were not recorded"
    assert len(told) == kernel.PROGRAM_FAULTS_KEPT + 1
    assert told[1].startswith("loop (pid 2), no CPU named, 2.0 s into its boot: ")
    assert told[-1].startswith(f"loop (pid {kernel.PROGRAM_FAULTS_KEPT + 1}), no CPU named, ")


@pytest.mark.req("PROGRAM_FAULT.outside")
def test_the_program_faults_read_by_a_resume_that_carries_the_session_onto_a_new_kernel_go_to_the_child(tmp_path):
    hardware, r = paused_by_the_owner_on_the_old_kernel(tmp_path)
    hardware.setup_now = KERNEL_NOW
    hardware.records = HARVESTED_FAULTS
    again = rig(tmp_path, hardware, db=r.store.db)
    assert driven(tmp_path, "--resume", again) == live_hw.EXIT_COMPLETED
    assert program_fault_events(again, 1) == []
    assert len(program_fault_events(again, 2)) == 2


def test_a_resume_in_the_same_boot_reads_only_this_boot_s_records(tmp_path, monkeypatch):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    in_flight(r)
    r.signals.append(Close())
    r.step(0.01)
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.same_boot is True
    assert [(boot, next_boot) for boot, _, next_boot in hardware.harvests] == [("boot-a", False)]


@dataclass
class KernelLog(FakeHardware):
    logged: list = field(default_factory=list)
    problem: str | None = None
    arriving: list = field(default_factory=list)
    unfinished_from_ns: int | None = None

    def harvest(self, boot_id, since_ns, next_boot, through_ns):
        self.harvests.append((boot_id, since_ns, next_boot, through_ns))
        for core, corrected in self.arriving:
            self.machine_check(core, corrected)
        self.arriving = []
        if self.problem is not None:
            return kernel.JournalHarvest((), (), self.problem)
        found = tuple(
            event
            for boot, ns, event in self.logged
            if boot == boot_id and ns > since_ns and (through_ns is None or ns <= through_ns)
        )
        return kernel.JournalHarvest(found, (), None, self.unfinished_from_ns)

    def machine_check(self, core: int, corrected: bool) -> None:
        cpu = dict(CPUS)[core][0]
        self.log(lambda raw_ts: kernel.CoreMachineCheck(cpu, 5, corrected, f"cpu {cpu} bank 5", raw_ts))

    def program_fault(self, pid: int) -> None:
        message = f"traps: loop[{pid}] trap int3 ip:0 sp:0 error:0"
        self.log(lambda raw_ts: kernel.ProgramFault("loop", pid, None, None, message, raw_ts))

    def log(self, event_at) -> None:
        ns = time.monotonic_ns() // 1000 * 1000
        self.logged.append((self.boot, ns, event_at(ns / 1_000_000_000)))


def live1_in_flight(r: LiveRig) -> None:
    r.start(config(), 1, 7, True)
    while r.worker is None or r.state.flow.stage is not Stage.LIVE1:
        r.step(0.01)


def away(tmp_path, r: LiveRig, how: str) -> LiveRig:
    r.signals.append(PauseRequest() if how == "pause" else Close())
    r.step(0.01)
    return r if how == "pause" else rig(tmp_path, r.hardware, r.runner, r.store.db, r.check_interval_s)


@dataclass
class ArmedRunner(FakeRunner):
    armed: bool = False
    logged: int | None = None

    def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
        report = super().run(spec, stop, sink)
        if self.armed:
            self.armed = False
            self.logged = spec.core
            self.hardware.machine_check(spec.core, True)
            report = replace(report, evidence=(Evidence(spec.core, True),))
        self.read_ns = time.monotonic_ns()
        return replace(report, kernel_read_ns=self.read_ns)


def stepped_until(r: LiveRig, reached) -> None:
    for _ in range(1000):
        if reached():
            return
        r.step(0.01)
    raise AssertionError(f"not reached within 1000 steps: {r.state.lifecycle} {r.state.pause}")


def one_test(r: LiveRig) -> None:
    seen = len(r.runner.seen)
    stepped_until(r, lambda: len(r.runner.seen) > seen and r.worker is None)


def rested(r: LiveRig) -> SessionState:
    stepped_until(r, r.at_rest)
    return r.state


@pytest.mark.req("L18.d.3.once")
@pytest.mark.parametrize("how", ["pause", "close"])
@pytest.mark.parametrize("corrected", [True, False])
def test_one_machine_check_backs_its_core_off_once_however_often_the_session_resumes_in_its_boot(
    tmp_path, how, corrected
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    live1_in_flight(r)
    before = r.state.core(0).value
    hardware.machine_check(0, corrected)
    r = away(tmp_path, r, "close")
    r.resume(1)
    once = r.state.core(0)
    assert (once.value > before, once.fb) == (True, before)
    for _ in range(2):
        r = away(tmp_path, r, how)
        r.resume(1)
        assert r.state.lifecycle is Lifecycle.RUNNING
        assert (r.state.core(0).value, r.state.core(0).fb) == (once.value, once.fb)
    reads = hardware.harvests
    assert [since for _, since, _, _ in reads[1:]] == [through for _, _, _, through in reads[:-1]]


@pytest.mark.req("L18.d.3.once")
def test_a_crash_after_a_resume_in_the_same_boot_reads_only_the_records_that_resume_left(tmp_path, monkeypatch):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    live1_in_flight(r)
    hardware.machine_check(0, False)
    r = away(tmp_path, r, "close")
    r.resume(1)
    stepped_until(r, lambda: r.worker is not None)
    held = r.state.core(1).value
    hardware.machine_check(1, True)
    hardware.boot, hardware.co = "boot-b", {0: 0, 1: 0, 8: 0}
    read, _, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch)
    assert read.evidence == (Evidence(1, True, held),)
    dead, _, _, through = hardware.harvests[-1]
    assert (dead, through) == ("boot-a", None)


def stock_checks(r: LiveRig) -> int:
    return r.state.incidents.count(Incident.STOCK_MACHINE_CHECK)


@pytest.mark.req("L18.d.3.once")
def test_a_record_the_journal_takes_while_a_resume_reads_it_is_judged_once_by_the_next_resume(tmp_path, clock):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    live1_in_flight(r)
    before = (r.state.core(0).value, r.state.core(0).fb)
    hardware.arriving = [(0, True)]
    r = away(tmp_path, r, "close")
    clock.advance(400)
    r.resume(1)
    assert stock_checks(r) == 0
    for _ in range(2):
        r = away(tmp_path, r, "close")
        r.resume(1)
        assert (stock_checks(r), (r.state.core(0).value, r.state.core(0).fb)) == (1, before)


@pytest.mark.req("L18.d.3.unread")
def test_a_resume_whose_read_of_its_boot_s_kernel_log_failed_leaves_its_records_to_the_next_resume(tmp_path):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    live1_in_flight(r)
    before = r.state.core(0).value
    hardware.machine_check(0, True)
    hardware.problem = "journalctl -b exited 1"
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert r.state.core(0).value == before
    assert rows(r, "SELECT message FROM tuner_events WHERE session_id=1 AND severity='kernel_log_unread'") == [
        {
            "message": "this boot's kernel log could not be read, so this resume judged none of its records: "
            "journalctl -b exited 1"
        }
    ]
    hardware.problem = None
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert r.state.core(0).value > before


@pytest.mark.req("L18.d.3.once")
def test_a_test_after_a_resume_keeps_its_own_read_point(tmp_path):
    hardware = KernelLog()
    runner = ArmedRunner(hardware)
    r = rig(tmp_path, hardware, runner)
    live1_in_flight(r)
    r = away(tmp_path, r, "close")
    r.resume(1)
    runner.armed = True
    one_test(r)
    core = runner.logged
    after_test = (r.state.core(core).value, r.state.core(core).fb)
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert (r.state.core(core).value, r.state.core(core).fb) == after_test
    _, since, _, _ = hardware.harvests[-1]
    assert since == runner.read_ns


class Died(BaseException):
    pass


@pytest.mark.req("L18.d.3.verdict")
def test_a_death_between_a_test_s_return_and_its_verdict_s_commit_leaves_its_record_to_the_next_resume_once(
    tmp_path, monkeypatch
):
    hardware = KernelLog()
    runner = ArmedRunner(hardware)
    r = rig(tmp_path, hardware, runner)
    live1_in_flight(r)
    r = away(tmp_path, r, "close")
    r.resume(1)
    returned = r.store.test_returned

    def dies_before_the_verdict_commits(*args):
        returned(*args)

        def died(*_):
            raise Died

        monkeypatch.setattr(r.store, "save", died)

    monkeypatch.setattr(r.store, "test_returned", dies_before_the_verdict_commits)
    runner.armed = True
    with pytest.raises(Died):
        one_test(r)
    tested = runner.seen[-1]
    assert runner.logged == tested.core
    r = rig(tmp_path, hardware, runner, db=r.store.db)
    r.resume(1)
    once = r.state.core(tested.core)
    assert (once.value > tested.value, once.fb) == (True, tested.value)
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert (r.state.core(tested.core).value, r.state.core(tested.core).fb) == (once.value, once.fb)


@dataclass
class SteadyArmedRunner(ArmedRunner):
    def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
        self.armed = spec.stage is Stage.STEADY
        return super().run(spec, stop, sink)


@pytest.mark.req("L18.d.3.verdict")
def test_a_cut_all_core_test_s_ignored_report_leaves_its_record_to_the_resume_after_the_session_closes(tmp_path, clock):
    hardware = KernelLog()
    runner = SteadyArmedRunner(hardware)
    r = rig(tmp_path, hardware, runner, check_interval_s=5)
    live1_in_flight(r)
    clock.advance(60)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.STEADY)
    r.signals.append(PauseRequest())
    stepped_until(r, lambda: r.test_row is None)
    held = dict(runner.seen[-1].vector)[runner.logged]
    r = away(tmp_path, r, "close")
    r.resume(1)
    once = r.state.core(runner.logged)
    assert (once.value > held, once.fb) == (True, held)
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert (r.state.core(runner.logged).value, r.state.core(runner.logged).fb) == (once.value, once.fb)


@dataclass
class CutRunner(FakeRunner):
    cut: Group | None = None

    def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
        if spec.stage is not Stage.STEADY or self.cut is None:
            return super().run(spec, stop, sink)
        assert stop.wait(10), "the pause never stopped the steady test"
        group, self.cut = self.cut, None
        if group is Group.SILICON:
            core = min(spec.vector, key=lambda pair: pair[1])[0]
            return Report(group, 3.0, named=(core,), detail=f"computation: core {core}")
        return Report(group, 3.0, detail="cut_short: stopped before the end of its stress")


def paused_in_steady(tmp_path, group: Group) -> tuple[LiveRig, InFlight]:
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, CutRunner(hardware, cut=group))
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.STEADY)
    cut = r.state.slot
    r.signals.append(PauseRequest())
    r.run_until_rest(0.01)
    return r, cut


def next_test_after_a_resume(tmp_path, r: LiveRig) -> Spec:
    again = rig(tmp_path, r.hardware, r.runner, db=r.store.db)
    again.resume(1)
    stepped_until(again, lambda: again.worker is not None)
    return again.state.slot.spec


@pytest.mark.req("U22.named")
def test_a_pause_reads_the_cut_all_core_test_s_report_before_it_rests_and_judges_a_core_it_names(tmp_path):
    r, cut = paused_in_steady(tmp_path, Group.SILICON)
    core = min(cut.spec.vector, key=lambda pair: pair[1])[0]
    held = dict(cut.spec.vector)[core]
    [row] = rows(r, "SELECT verdict, named_json FROM tuner_tests WHERE token=?", (cut.token,))
    assert (row["verdict"], json.loads(row["named_json"])) == ("silicon", [core])
    rec = r.state.core(core)
    assert (rec.value > held, rec.fb, r.state.lifecycle) == (True, held, Lifecycle.PAUSED)
    said = rows(r, "SELECT message FROM tuner_events WHERE severity='cut_judged'")
    assert len(said) == 1 and f"core {core}" in said[0]["message"]
    assert dict(next_test_after_a_resume(tmp_path, r).vector)[core] == rec.value


@dataclass
class SampleThenStop(FakeRunner):
    def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
        if spec.stage is not Stage.STEADY:
            return super().run(spec, stop, sink)
        sink(taken(min(spec.vector, key=lambda pair: pair[1])[0], 0))
        assert stop.wait(10)
        return Report(Group.APPARATUS, 3.0, detail="cut_short: stopped")


@pytest.mark.req("U22.named")
def test_a_database_pause_writes_nothing_more_of_the_test_it_stopped_into_the_failing_database(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, SampleThenStop(hardware))
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.STEADY)
    written = []
    returned = r.store.test_returned

    def spy(*args):
        written.append(r.state.lifecycle)
        return returned(*args)

    def full(*args):
        raise sqlite3.OperationalError("database or disk is full")

    r.store.test_returned = spy
    r.store.sample = full
    r.run_until_rest(0.01)
    assert (r.state.lifecycle, r.state.pause.reason) == (Lifecycle.PAUSED, PauseReason.DATABASE)
    assert written == []


@pytest.mark.req("U22", "U22.named")
@pytest.mark.parametrize(
    ("group", "asleep", "verdict"), [(Group.APPARATUS, 0.0, "apparatus"), (Group.SILICON, 600.0, "slept")]
)
def test_a_pause_records_a_cut_test_s_report_and_runs_a_cut_short_or_slept_one_again_unjudged(
    tmp_path, group, asleep, verdict
):
    slept = [0.0]
    hardware = FakeHardware()
    hardware.asleep_seconds = lambda: slept[0]
    r = rig(tmp_path, hardware, CutRunner(hardware, cut=group))
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.STEADY)
    cut = r.state.slot
    held = tuple((c.core, c.value, c.fb) for c in r.state.cores)
    slept[0] = asleep
    r.signals.append(PauseRequest())
    r.run_until_rest(0.01)
    [row] = rows(r, "SELECT verdict FROM tuner_tests WHERE token=?", (cut.token,))
    after = tuple((c.core, c.value, c.fb) for c in r.state.cores)
    assert (row["verdict"], after, r.state.lifecycle) == (verdict, held, Lifecycle.PAUSED)
    rerun = next_test_after_a_resume(tmp_path, r)
    assert (rerun.stage, rerun.vector) == (cut.spec.stage, cut.spec.vector)


@pytest.mark.req("L18.d.3.gap")
def test_a_record_logged_while_the_resume_reads_is_judged_once_before_a_first_test_without_a_read_point(
    tmp_path, clock
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, FakeRunner(hardware))
    live1_in_flight(r)
    before = (r.state.core(0).value, r.state.core(0).fb)
    hardware.arriving = [(0, True)]
    r = away(tmp_path, r, "close")
    clock.advance(400)
    r.resume(1)
    assert stock_checks(r) == 0
    one_test(r)
    assert (stock_checks(r), (r.state.core(0).value, r.state.core(0).fb)) == (1, before)
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert (stock_checks(r), (r.state.core(0).value, r.state.core(0).fb)) == (1, before)
    resumed, before_the_test, _ = hardware.harvests
    boot, since, next_boot, through = before_the_test
    assert (boot, since, next_boot, through > since) == ("boot-a", resumed[3], False, True)


@pytest.mark.req("L18.d.3.unread")
def test_a_failed_read_before_the_first_test_after_a_resume_pauses_before_any_test_and_loses_nothing(tmp_path, clock):
    hardware = KernelLog()
    runner = ArmedRunner(hardware)
    r = rig(tmp_path, hardware, runner)
    live1_in_flight(r)
    hardware.arriving = [(0, True)]
    r = away(tmp_path, r, "close")
    clock.advance(400)
    r.resume(1)
    hardware.problem = "journalctl -b exited 1"
    seen = len(runner.seen)
    rested(r)
    assert (r.state.pause.reason, r.state.pause.detail, len(runner.seen)) == (
        PauseReason.KERNEL_LOG_UNREAD,
        "the kernel log since the resume could not be read: journalctl -b exited 1",
        seen,
    )
    hardware.problem = None
    r.resume(1)
    assert stock_checks(r) == 1
    runner.armed = True
    one_test(r)
    core = runner.logged
    after_test = (stock_checks(r), r.state.core(core).value, r.state.core(core).fb)
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert (stock_checks(r), r.state.core(core).value, r.state.core(core).fb) == after_test


@pytest.mark.req("L18.d.3.once")
def test_a_resume_that_cannot_read_its_session_back_saves_no_read_point_an_earlier_unsaved_resume_left(
    tmp_path, monkeypatch
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    live1_in_flight(r)
    before = r.state.core(0).value
    hardware.machine_check(0, True)
    r = away(tmp_path, r, "close")
    save, load, loads = r.store.save, r.store.load, []

    def save_fails_once(*args, **kwargs):
        monkeypatch.setattr(r.store, "save", save)
        raise sqlite3.OperationalError("disk I/O error")

    def second_load_fails(session_id):
        loads.append(session_id)
        if len(loads) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return load(session_id)

    monkeypatch.setattr(r.store, "save", save_fails_once)
    r.resume(1)
    assert r.state.pause.reason is PauseReason.DATABASE
    monkeypatch.setattr(r.store, "load", second_load_fails)
    r.resume(1)
    assert (len(loads), r.state.pause.reason) == (2, PauseReason.DATABASE)
    r.post(Abort())
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    assert again.state.core(0).value > before


@pytest.mark.req("L18.d.3.session")
@pytest.mark.parametrize("carry", ["restart", "continue"])
def test_a_session_carried_on_from_a_resume_in_its_boot_reads_nothing_from_its_parent_s_point(tmp_path, carry):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, ArmedRunner(hardware))
    live1_in_flight(r)
    r.signals.append(Close())
    r.step(0.01)
    hardware.setup_now = (("bios", "B"),)
    r = rig(tmp_path, hardware, r.runner, db=r.store.db)
    r.resume(1)
    assert r.state.pause.reason is PauseReason.SETUP_CHANGED
    reads = len(hardware.harvests)
    if carry == "restart":
        r.restart_on_setup(True)
    else:
        r.continue_on_setup()
    one_test(r)
    assert (r.state.session_id, hardware.harvests[reads:]) == (2, [])


def search_cut(tmp_path, hardware: KernelLog) -> tuple[LiveRig, int, int]:
    r = rig(tmp_path, hardware, ArmedRunner(hardware))
    tested = in_flight(r)
    value = r.state.slot.spec.value
    return r, tested.core, value


@pytest.mark.req("L18.d.3.charged")
def test_a_record_a_cut_search_test_logged_is_charged_to_the_value_it_ran_at(tmp_path):
    hardware = KernelLog()
    r, core, value = search_cut(tmp_path, hardware)
    hardware.machine_check(core, True)
    r = away(tmp_path, r, "close")
    r.resume(1)
    record = r.state.core(core)
    assert (r.state.lifecycle, record.fb, record.value > value) == (Lifecycle.RUNNING, value, True)


def started_long_ago(tmp_path, hardware: KernelLog, clock: TickingClock, check_interval_s: int = 5) -> LiveRig:
    r = rig(tmp_path, hardware, check_interval_s=check_interval_s)
    r.start(config(), 1, 7, True)
    clock.advance(10)
    return r


@pytest.mark.req("L18.d.3.charged")
@pytest.mark.parametrize("landing", ["before the resume reads", "while the resume reads"])
def test_a_cut_search_test_s_record_logged_2_s_after_the_stock_write_backs_its_core_off(tmp_path, clock, landing):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    r = away(tmp_path, r, "close")
    clock.advance(2)
    if landing == "before the resume reads":
        hardware.machine_check(core, True)
    else:
        hardware.arriving = [(core, True)]
    r.resume(1)
    if landing == "while the resume reads":
        one_test(r)
    record = r.state.core(core)
    assert (r.state.lifecycle, record.fb, record.value > value) == (Lifecycle.RUNNING, value, True)


@pytest.mark.req("L18.d.3.charged")
def test_a_record_3_s_after_the_first_write_whose_window_reaches_past_the_start_read_is_charged_and_runs_on(
    tmp_path, clock
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    r = away(tmp_path, r, "close")
    clock.advance(3)
    hardware.machine_check(core, True)
    r.resume(1)
    record = r.state.core(core)
    assert (r.state.lifecycle, record.fb, record.value > value) == (Lifecycle.RUNNING, value, True)


@pytest.mark.req("L18.d.3.charged")
def test_a_session_s_first_co_read_in_a_boot_stands_for_its_core_from_that_boot_s_start(tmp_path, clock):
    hardware = KernelLog(co={0: -3, 1: 0, 8: 0})
    r = rig(tmp_path, hardware)
    clock.advance(100)
    before_the_read = clock.ns - 50 * 1_000_000_000
    r.start(config(), 1, 7, True)
    r = away(tmp_path, r, "close")
    r.resume(1)
    observed = r.store.observed("boot-a")
    assert [read.value for read in observed if isinstance(read, CoRead) and read.core_id == 0][:2] == [-3, 0]
    assert charged_value(observed, 0, before_the_read, 5) == -3
    assert [charged_value(observed, 0, seconds * 1_000_000_000, 5) for seconds in (5, 3)] == [-3, None]


def unplaced_notes(r: LiveRig) -> list[str]:
    return [row["message"] for row in rows(r, "SELECT message FROM tuner_events WHERE severity='record_unplaced'")]


@pytest.mark.req("L18.d.3.unplaced")
def test_a_record_whose_window_cannot_be_placed_is_charged_to_its_core_s_current_value_and_named(tmp_path, clock):
    clock.ns = 1_000_000_000
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    r = away(tmp_path, r, "close")
    clock.advance(1)
    hardware.machine_check(core, True)
    r.resume(1)
    record = r.state.core(core)
    assert (r.state.lifecycle, record.fb, record.value > value) == (Lifecycle.RUNNING, value, True)
    assert unplaced_notes(r) == [
        f"core {core}'s machine check is charged to its current value {value}: the CO journal cannot place its moment"
    ]


@pytest.mark.req("L18.d.3.unplaced")
def test_a_placed_record_read_with_an_unplaced_one_is_applied_and_the_session_runs_on():
    fake = fresh(2, [-3, -2], 1, max_offset=-8, k_1t=1)
    start(fake)
    s = fake.state
    s = s.with_core(replace(s.core(0), phase=Phase.PROVE, value=-2))
    s = s.with_core(replace(s.core(1), phase=Phase.PROVE, value=-4))
    judged = transition(s, KernelLogRead(s.slot.token, (Evidence(1, True, -4), Evidence(0, True))), fake.world())
    after = judged.state
    assert (after.lifecycle, after.core(0).value, after.core(0).fb, after.core(1).value, after.core(1).fb) == (
        Lifecycle.RUNNING,
        -1,
        -2,
        -3,
        -4,
    )
    assert [effect.kind for effect in judged.effects if isinstance(effect, Announce)] == ["record_unplaced"]


@pytest.mark.req("L18.d.3.repeat")
def test_a_record_charged_at_its_core_s_fail_bound_leaves_the_core_where_it_sits():
    fake = fresh(2, [-3, -2], 1, max_offset=-8, k_1t=1)
    start(fake)
    s = fake.state
    s = s.with_core(replace(s.core(0), phase=Phase.PROVEN, value=-3, fb=-5, count=1))
    repeat = transition(s, KernelLogRead(s.slot.token, (Evidence(0, True, -5),)), fake.world())
    assert (repeat.state.core(0), notes(repeat, "record_unjudged")) == (s.core(0), [repeat_note(0, -5, -5)])


@pytest.mark.req("L18.d.3.charged")
@pytest.mark.parametrize("carry", ["restart", "continue"])
def test_a_carried_on_session_s_record_is_placed_by_the_co_its_parent_read_and_wrote_in_the_boot(
    tmp_path, clock, carry
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, ArmedRunner(hardware), check_interval_s=5)
    r.start(config(), 1, 7, True)
    clock.advance(60)
    stepped_until(r, lambda: r.worker is not None)
    r.signals.append(Close())
    r.step(0.01)
    hardware.setup_now = (("bios", "B"),)
    r = rig(tmp_path, hardware, r.runner, r.store.db, 5)
    r.resume(1)
    clock.advance(60)
    if carry == "restart":
        r.restart_on_setup(True)
    else:
        r.continue_on_setup()
    child = r.state.session_id
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(core, True)
    r.resume(child)
    record = r.state.core(core)
    assert (r.state.lifecycle, record.fb, record.value > value, unplaced_notes(r)) == (
        Lifecycle.RUNNING,
        value,
        True,
        [],
    )


@pytest.mark.req("L18.d.3.charged")
def test_a_record_logged_within_the_check_interval_and_the_timer_s_rounding_is_charged_to_the_cut_test(tmp_path, clock):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    r = away(tmp_path, r, "close")
    clock.ns += 5_500_000_000
    hardware.machine_check(core, True)
    r.resume(1)
    record = r.state.core(core)
    assert (r.state.lifecycle, record.fb, record.value > value) == (Lifecycle.RUNNING, value, True)


@pytest.mark.req("L18.d.3.outside")
def test_a_record_on_a_core_another_program_wrote_while_paused_is_charged_to_its_current_value_then_pauses(
    tmp_path, clock
):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(core, True)
    hardware.co[core] = -30
    r.resume(1)
    record = r.state.core(core)
    assert (r.state.pause.reason, record.fb, record.value > value) == (PauseReason.OUTSIDE_WRITE, value, True)
    assert unplaced_notes(r) == [
        f"core {core}'s machine check is charged to its current value {value}: another program wrote its CO"
    ]


@pytest.mark.req("L18.d.3.outside")
@pytest.mark.parametrize(
    ("moved", "reason", "why"),
    [
        ("written", PauseReason.OUTSIDE_WRITE, "another program wrote its CO"),
        ("unreadable", PauseReason.SMU_READ, "its CO could not be read"),
    ],
)
def test_a_record_on_a_stock_core_that_moved_or_cannot_be_read_is_noted_unjudged_before_that_pause(
    tmp_path, clock, moved, reason, why
):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None)
    tested = r.state.slot.spec.core
    stock = next(c.core for c in r.state.cores if c.core != tested and c.value == 0)
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(stock, True)
    if moved == "written":
        hardware.co[stock] = -30
    else:
        del hardware.co[stock]
    r.resume(1)
    unjudged = [row["message"] for row in rows(r, "SELECT message FROM tuner_events WHERE severity='record_unjudged'")]
    assert (r.state.pause.reason, stock_checks(r), unplaced_notes(r)) == (reason, 0, [])
    assert unjudged == [
        f"core {stock}'s machine check is not judged: {why} while the session held it at stock, "
        "so the value it ran at is unknown"
    ]


@pytest.mark.req("L18.d.3.outside")
def test_a_record_beside_a_core_another_program_wrote_is_judged_as_normal_before_the_outside_write_pause(
    tmp_path, clock
):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(core, True)
    hardware.co[8 if core != 8 else 0] = -30
    r.resume(1)
    record = r.state.core(core)
    assert (r.state.pause.reason, record.fb, record.value > value, unplaced_notes(r)) == (
        PauseReason.OUTSIDE_WRITE,
        value,
        True,
        [],
    )


@pytest.mark.req("L18.d.3.outside")
def test_a_record_on_a_core_whose_co_cannot_be_read_is_charged_to_its_current_value_named_as_unread(tmp_path, clock):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.LIVE1)
    sits = r.state.core(8)
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(8, True)
    del hardware.co[8]
    r.resume(1)
    record = r.state.core(8)
    assert (r.state.pause.reason, record.value, record.fb) == (PauseReason.SMU_READ, sits.value + 1, sits.value)
    assert unplaced_notes(r) == [
        f"core 8's machine check is charged to its current value {sits.value}: its CO could not be read"
    ]


@pytest.mark.req("L18.d.3.batch")
def test_a_record_that_pauses_lets_every_other_record_of_its_read_be_applied_first(tmp_path, clock):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None)
    core, value = r.state.slot.spec.core, r.state.slot.spec.value
    other = next(c.core for c in r.state.cores if c.core != core)
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(other, True)
    hardware.machine_check(core, True)
    r.resume(1)
    record = r.state.core(core)
    assert (r.state.pause.reason, r.state.pause.cores, record.fb, record.value > value) == (
        PauseReason.STOCK_UNSTABLE,
        (other,),
        value,
        True,
    )


@pytest.mark.req("L18.d.3.charged")
def test_a_cut_tighten_trial_s_record_charged_at_resume_ends_as_its_own_test_would():
    fake = fresh(2, [-3, -2], 1, max_offset=-8, k_1t=1)
    start(fake)
    s = fake.state
    s = s.with_core(replace(s.core(0), phase=Phase.PROVEN, value=-3, fb=None))
    s = replace(s, flow=replace(s.flow, stage=Stage.TIGHTEN, allcore=AllCore(floors=((0, -8), (1, -8)))))
    after = transition(s, KernelLogRead(s.slot.token, (Evidence(0, True, -4),)), fake.world()).state
    assert (after.core(0).value, after.core(0).fb) == (-3, -4)


@pytest.mark.req("L18.d.3.charged")
def test_a_charge_in_front_of_the_fail_bound_but_deeper_than_its_core_sits_never_moves_the_core_deeper():
    fake = fresh(2, [-3, -2], 1, max_offset=-9, k_1t=1)
    start(fake)
    s = fake.state
    s = s.with_core(replace(s.core(0), phase=Phase.PROVEN, value=-3, fb=-8))
    after = transition(s, KernelLogRead(s.slot.token, (Evidence(0, True, -6),)), fake.world()).state
    assert (after.core(0).value, after.core(0).fb) == (-3, -6)


def another_session_wrote(r: LiveRig, value) -> None:
    db = r.store.db
    db.save_machine_session(
        2,
        status="aborted",
        config_json="{}",
        machine_json="{}",
        boot_id="boot-a",
        bios_version="",
        cpu_model="",
        context_id=None,
    )
    row = db.journal_co_intent(2, 0, -9, False, boot_id="boot-a", mono_ns=time.monotonic_ns())
    db._execute_raw("UPDATE tuner_co_journal SET value=? WHERE id=?", (value, row))


@pytest.mark.req("L18.d.3.charged")
@pytest.mark.parametrize("whose", ["another session's", "its own"])
def test_a_resume_that_meets_a_corrupted_co_journal_row_in_its_boot_pauses_as_database_naming_the_session(
    tmp_path, whose
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None)
    r = away(tmp_path, r, "close")
    if whose == "its own":
        r.store.db._execute_raw("UPDATE tuner_co_journal SET value='x' WHERE id=(SELECT min(id) FROM tuner_co_journal)")
    else:
        another_session_wrote(r, "x")
    hardware.machine_check(0, True)
    r.resume(1)
    named = 1 if whose == "its own" else 2
    assert r.state.pause.reason is PauseReason.DATABASE
    assert f"session {named} is unreadable: core 0 has CO journal value 'x'" in r.state.pause.detail


@pytest.mark.req("L18.d.3.charged")
def test_a_resume_that_meets_a_corrupted_co_read_in_its_boot_pauses_as_database_naming_the_session(tmp_path):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None)
    r = away(tmp_path, r, "close")
    r.store.db._execute_raw("UPDATE tuner_co_reads SET value='x' WHERE id=(SELECT min(id) FROM tuner_co_reads)")
    hardware.machine_check(0, True)
    r.resume(1)
    assert r.state.pause.reason is PauseReason.DATABASE
    assert "session 1 is unreadable: core 0 has CO read 'x'" in r.state.pause.detail


@pytest.mark.req("L18.d.3.split")
def test_a_resume_read_that_leaves_an_unfinished_record_starts_the_next_read_just_before_its_opener(tmp_path):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    live1_in_flight(r)
    r = away(tmp_path, r, "close")
    opener = time.monotonic_ns() // 1000 * 1000
    hardware.unfinished_from_ns = opener
    r.resume(1)
    hardware.unfinished_from_ns = None
    r = away(tmp_path, r, "close")
    r.resume(1)
    _, since, _, _ = hardware.harvests[-1]
    assert since == opener - 1


@pytest.mark.req("L18.d.3.charged", "L18.d.3.gap")
def test_a_record_charged_to_stock_before_the_first_test_after_a_resume_pauses_before_any_test(tmp_path, clock):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None)
    core = r.state.slot.spec.core
    r = away(tmp_path, r, "close")
    clock.advance(6)
    hardware.arriving = [(core, True)]
    r.resume(1)
    seen = len(r.runner.seen)
    rest = rested(r)
    assert (rest.pause.reason, rest.pause.cores, len(r.runner.seen)) == (PauseReason.STOCK_UNSTABLE, (core,), seen)


@pytest.mark.req("L18.d.3.charged")
def test_a_record_logged_after_its_core_held_stock_longer_than_the_check_interval_is_charged_to_stock(tmp_path, clock):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.LIVE1)
    before = (r.state.core(0).value, r.state.core(0).fb)
    r = away(tmp_path, r, "close")
    clock.advance(6)
    hardware.machine_check(0, True)
    r.resume(1)
    assert (stock_checks(r), (r.state.core(0).value, r.state.core(0).fb)) == (1, before)


@pytest.mark.req("L18.d.3.charged")
def test_a_record_whose_window_holds_a_deeper_earlier_value_is_charged_to_where_its_core_last_sat_off_stock(
    tmp_path, clock, monkeypatch
):
    hardware = KernelLog()
    r = started_long_ago(tmp_path, hardware, clock)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.LIVE1)
    sits = r.state.core(1)
    deepest = min(row.value for row in r.store.db.journal_rows(1, "boot-a") if row.core_id == 1)
    r.signals.append(Close())
    r.step(0.01)
    clock.advance(1)
    hardware.machine_check(1, True)
    read, again, _ = resume_read(tmp_path, hardware, r.store.db, monkeypatch, check_interval_s=5)
    start = hardware.logged[-1][1] - 6_000_000_000
    stamps = sorted((row.mono_ns, row.value) for row in r.store.db.journal_rows(1, "boot-a") if row.core_id == 1)
    held = [value for at, value in stamps if at <= start][-1:] + [value for at, value in stamps if at > start]
    assert (read.evidence, deepest in held, deepest < sits.value) == ((Evidence(1, True, sits.value),), True, True)
    assert (again.state.core(1).value, again.state.core(1).fb) == (sits.value + 1, sits.value)


@pytest.mark.req("L18.d.3.gap")
def test_a_record_judged_before_a_first_test_that_ends_thermal_stays_judged_once(tmp_path, clock):
    hardware = KernelLog()
    runner = ArmedRunner(hardware)
    r = rig(tmp_path, hardware, runner)
    live1_in_flight(r)
    before = (r.state.core(0).value, r.state.core(0).fb)
    hardware.arriving = [(0, True)]
    r = away(tmp_path, r, "close")
    clock.advance(400)
    r.resume(1)
    runner.group = Group.THERMAL
    one_test(r)
    runner.group = None
    r = away(tmp_path, r, "close")
    r.resume(1)
    assert (stock_checks(r), (r.state.core(0).value, r.state.core(0).fb)) == (1, before)


@pytest.mark.req("L18.d.3.charged")
def test_a_co_journal_that_cannot_be_read_before_the_first_test_after_a_resume_pauses_naming_it(tmp_path, monkeypatch):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    live1_in_flight(r)
    r = away(tmp_path, r, "close")
    hardware.arriving = [(0, True)]
    r.resume(1)

    def unreadable(boot):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(r.store, "observed", unreadable)
    rest = rested(r)
    assert (rest.pause.reason, rest.pause.detail) == (
        PauseReason.DATABASE,
        "the CO journal could not be read back: disk I/O error",
    )


@pytest.mark.req("PROGRAM_FAULT.outside")
def test_a_program_fault_logged_after_a_resume_read_is_told_once_before_the_first_test(tmp_path):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, ArmedRunner(hardware))
    live1_in_flight(r)
    r = away(tmp_path, r, "close")
    r.resume(1)
    hardware.program_fault(4321)
    one_test(r)
    r = away(tmp_path, r, "close")
    r.resume(1)
    told = program_fault_events(r, 1)
    assert len(told) == 1
    assert told[0].startswith("loop (pid 4321), no CPU named, ")


@pytest.mark.req("L18.d.3.gap")
def test_the_read_before_the_first_test_is_judged_only_while_that_test_waits():
    fake = fresh(2, [-3, -2], 1, max_offset=-5, k_1t=1)
    start(fake)
    waiting = fake.state
    token = waiting.slot.token
    fake.post(KernelLogRead(token + 1, (Evidence(0, True, -2),)))
    assert fake.state == waiting
    fake.post(KernelLogRead(token, (Evidence(1, False, 0),)))
    paused = fake.state
    assert (paused.pause.reason, paused.pause.cores) == (PauseReason.STOCK_UNSTABLE, (1,))
    fake.post(KernelLogRead(token, (Evidence(0, True, -2),)))
    assert fake.state == paused


@pytest.mark.req("L18.d.4.3a")
def test_a_record_from_a_dead_boot_whose_database_fell_behind_counts_at_stock_and_moves_no_core():
    fake = fresh(3, [-3, -1, -2], 1, max_offset=-5, k_1t=2, k_2t=1)
    start(fake)
    while fake.worker is None or fake.state.flow.stage is not Stage.LIVE1:
        assert fake.progress()
    tested = fake.worker.spec.core
    other = next(r for r in fake.state.cores if r.core != tested and r.value != 0)
    fake.marker, fake.behind = (fake.boot, 1), {(1, fake.boot)}
    fake.crash((Evidence(other.core, True), Evidence(tested, True)))
    fake.post(Resume(1))
    record = fake.state.core(other.core)
    assert (record.value, record.fb, fake.state.incidents) == (other.value, other.fb, (Incident.CRASH,))
    assert unjudged_lines(fake) == []


@pytest.mark.req("L18.d.3.charged")
def test_a_record_is_charged_to_the_latest_value_off_stock_its_core_held_over_the_check_interval_before_it():
    s = 1_000_000_000
    rows = [
        JournalRow(1, 1, 0, -5, "boot-a", 1 * s, True, False, False, ""),
        JournalRow(2, 1, 1, -9, "boot-a", 2 * s, True, False, False, ""),
        JournalRow(3, 1, 0, -2, "boot-a", 3 * s, True, False, False, ""),
        JournalRow(4, 1, 0, 0, "boot-a", 4 * s, True, False, False, ""),
        JournalRow(5, 1, 2, -3, "boot-a", 1 * s, True, False, False, ""),
        JournalRow(6, 1, 2, -4, "boot-a", 2 * s, False, False, False, ""),
        JournalRow(7, 1, 2, -6, "boot-a", None, True, False, False, ""),
    ]
    assert [charged_value(rows, 0, 5 * s, window) for window in (2, 1, 4, 5)] == [-2, 0, -2, None]
    assert [charged_value(rows, 0, logged, 0) for logged in (4 * s, 4 * s - 1)] == [0, -2]
    assert [charged_value(rows, 2, 3 * s, 1), charged_value(rows, 2, 1 * s, 0)] == [None, -3]
    reads_last = [
        JournalRow(1, 1, 5, -5, "boot-a", 3 * s, True, False, False, ""),
        CoRead(5, -2, 1 * s),
        CoRead(5, -9, s // 2),
    ]
    assert charged_value(reads_last, 5, 4 * s, 3) == -5


def fine_at(value: int, fb: int | None, stage: Stage = Stage.SEARCH) -> tuple[SessionState, object]:
    fake = fresh(2, [-3, -2], 1, max_offset=-8, k_1t=1)
    start(fake)
    s = fake.state
    s = s.with_core(replace(s.core(0), phase=Phase.FINE, value=value, fb=fb))
    if stage is Stage.TIGHTEN:
        s = replace(s, flow=replace(s.flow, stage=Stage.TIGHTEN, allcore=AllCore(floors=((0, -8), (1, -8)))))
    return s, fake.world()


def read_before_the_first_test(s: SessionState, world, *records: Evidence):
    return transition(s, KernelLogRead(s.slot.token, records), world)


def notes(result, kind: str) -> list[str]:
    return [effect.text for effect in result.effects if isinstance(effect, Announce) and effect.kind == kind]


def unjudged(core: int, record: str, why: str) -> str:
    return f"core {core}'s {record} is not judged: {why}"


def set_aside(core: int, record: str) -> str:
    return unjudged(core, record, f"a stronger record of core {core} in the same read decided its verdict")


def counted_aside(core: int, record: str) -> str:
    return unjudged(core, record, f"it is counted in its core's stock incident, and {set_aside_why(core)}")


def set_aside_why(core: int) -> str:
    return f"a stronger record of core {core} in the same read decided its verdict"


@pytest.mark.req("L18.d.3.core")
@pytest.mark.parametrize(
    ("stage", "value", "fb", "weaker", "stronger", "named"),
    [
        (Stage.SEARCH, -3, -6, (True, -3), (True, -2), "corrected machine check charged to -3"),
        (Stage.SEARCH, -4, None, (True, -6), (False, -4), "corrected machine check charged to -6"),
        (Stage.SEARCH, -4, None, (False, -6), (True, -4), "uncorrected machine check charged to -6"),
        (Stage.SEARCH, -3, -6, (True, -3), (True, 0), "corrected machine check charged to -3"),
    ],
)
def test_the_records_of_one_read_judge_their_core_once_by_the_record_whose_verdict_is_strongest(
    stage, value, fb, weaker, stronger, named
):
    s, world = fine_at(value, fb, stage)
    both = read_before_the_first_test(s, world, Evidence(0, *weaker), Evidence(0, *stronger))
    alone = read_before_the_first_test(s, world, Evidence(0, *stronger))
    assert both.state == alone.state != read_before_the_first_test(s, world, Evidence(0, *weaker)).state
    assert notes(both, "record_unjudged") == [set_aside(0, named)]


@pytest.mark.req("L18.d.3.core")
def test_a_record_that_sets_a_fail_bound_outranks_a_counted_stock_record_that_changes_nothing():
    s, _ = fine_at(-3, None, Stage.TIGHTEN)
    stock, bound = Evidence(0, True, 0), Evidence(0, True, -5)
    for records in ((stock, bound), (bound, stock)):
        judged, aside = _evidence_applied(s, records)
        assert (judged.core(0).value, judged.core(0).fb, judged.incidents) == (-3, -5, (Incident.STOCK_MACHINE_CHECK,))
        assert [note.text for note in aside] == [counted_aside(0, "corrected machine check charged to 0")]


@pytest.mark.req("L18.d.3.core")
def test_a_stock_record_after_a_cut_test_s_record_pauses_and_names_the_record_it_set_aside(tmp_path, clock):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    r.start(config(), 1, 7, True)
    clock.advance(60)
    stepped_until(r, lambda: r.worker is not None)
    spec = r.state.slot.spec
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(spec.core, True)
    clock.advance(10)
    hardware.machine_check(spec.core, True)
    r.resume(1)
    assert (r.state.pause.reason, r.state.pause.cores) == (PauseReason.STOCK_UNSTABLE, (spec.core,))
    assert unjudged_notes(r) == [set_aside(spec.core, f"corrected machine check charged to {spec.value}")]


@pytest.mark.req("L18.d.3.core")
def unjudged_lines(fake) -> list[str]:
    return [line.removeprefix("record_unjudged: ") for line in fake.log if line.startswith("record_unjudged: ")]


@pytest.mark.req("L18.d.3.core")
def test_the_records_of_one_dead_boot_judge_their_core_once():
    def resumed(*records: bool) -> tuple[int, int, SessionState, list[str]]:
        fake = fresh(3, [-3, -1, -2], 1, max_offset=-5, k_1t=2, k_2t=1)
        start(fake)
        while fake.worker is None or fake.state.flow.stage is not Stage.LIVE1:
            assert fake.progress()
        other = next(r for r in fake.state.cores if r.core != fake.worker.spec.core and r.value != 0)
        fake.crash(tuple(Evidence(other.core, corrected) for corrected in records))
        fake.post(Resume(1))
        return other.core, other.value, fake.state, unjudged_lines(fake)

    core, value, one, none = resumed(True)
    backed = (one.core(core).value, one.core(core).fb)
    assert (one.lifecycle, one.incidents, backed, none) == (Lifecycle.RUNNING, (), (value + 1, value), [])
    charged = f"corrected machine check charged to {value}"
    assert resumed(True, True) == (core, value, one, [set_aside(core, charged)])
    convicted = resumed(False)
    assert resumed(False, False)[:3] == resumed(True, False)[:3] == convicted[:3]
    decided = f"the crash conviction of core {core} decided its verdict"
    assert resumed(True, False)[3] == [
        unjudged(core, "corrected machine check with no charge", decided),
        unjudged(core, "uncorrected machine check with no charge", decided),
    ]


@pytest.mark.req("L18.d.3.core")
def test_the_core_a_dead_boot_s_crash_convicts_is_judged_by_that_conviction_alone():
    def resumed(*records: bool) -> tuple[int, int, SessionState, list[str]]:
        fake = fresh(2, [-3, -2], 1, max_offset=-8, k_1t=1)
        start(fake)
        while fake.worker is None:
            assert fake.progress()
        core, value = fake.worker.spec.core, fake.worker.spec.value
        fake.crash(tuple(Evidence(core, corrected) for corrected in records))
        fake.post(Resume(1))
        return core, value, fake.state, unjudged_lines(fake)

    core, value, convicted, none = resumed()
    assert (convicted.lifecycle, convicted.core(core).fb, none) == (Lifecycle.RUNNING, value, [])
    assert resumed(True)[:3] == resumed(False)[:3] == resumed(True, False)[:3] == (core, value, convicted)
    assert resumed(True)[3] == [
        unjudged(
            core, "corrected machine check with no charge", f"the crash conviction of core {core} decided its verdict"
        )
    ]


def cut_at(tmp_path, clock, value: int, logged: int, arriving: int) -> tuple[Spec, LiveRig]:
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    r.start(config(), 1, 7, True)
    clock.advance(60)
    r.state = r.state.with_core(replace(r.state.core(0), phase=Phase.FINE, value=value, fb=-6))
    stepped_until(r, lambda: r.worker is not None)
    spec = r.state.slot.spec
    r = away(tmp_path, r, "close")
    clock.advance(2)
    for _ in range(logged):
        hardware.machine_check(spec.core, True)
    hardware.arriving = [(spec.core, True)] * arriving
    r.resume(1)
    stepped_until(r, lambda: r.worker is not None or r.at_rest())
    return spec, r


def unjudged_notes(r: LiveRig) -> list[str]:
    return [row["message"] for row in rows(r, "SELECT message FROM tuner_events WHERE severity='record_unjudged'")]


def repeat_note(core: int, charged: int, fb: int) -> str:
    return (
        f"core {core}'s machine check is not judged: it is charged to {charged}, "
        f"at or behind its fail bound {fb}, so it repeats a failure already judged"
    )


@pytest.mark.req("L18.d.3.core", "L18.d.3.repeat")
@pytest.mark.parametrize("value", [-1, -3])
def test_two_records_of_a_cut_test_judge_its_core_once_in_one_read_or_across_the_resume_and_the_first_test_s_read(
    tmp_path, clock, value
):
    ends = {}
    for how, logged, arriving in (("one record", 1, 0), ("one read", 2, 0), ("across", 1, 1)):
        spec, r = cut_at(tmp_path / how.replace(" ", "-"), clock, value, logged, arriving)
        record = r.state.core(spec.core)
        ends[how] = (r.state.lifecycle, record.value, record.fb)
        assert (spec.core, spec.value) == (0, value)
    assert (ends["one record"][0], ends["one record"][2]) == (Lifecycle.RUNNING, value)
    assert ends["one read"] == ends["across"] == ends["one record"]
    assert unjudged_notes(r) == [repeat_note(0, value, value)]


@pytest.mark.req("L18.d.3.repeat")
def test_a_late_repeat_read_by_a_second_resume_after_one_that_paused_moves_its_core_no_further(tmp_path, clock):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    r.start(config(), 1, 7, True)
    clock.advance(60)
    r.state = r.state.with_core(replace(r.state.core(0), phase=Phase.FINE, value=-3, fb=-5))
    stepped_until(r, lambda: r.worker is not None)
    spec = r.state.slot.spec
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(spec.core, True)
    hardware.arriving = [(spec.core, True)]
    hardware.co[8] = -30
    r.resume(1)
    once = (r.state.pause.reason, r.state.core(spec.core).value, r.state.core(spec.core).fb)
    hardware.co[8] = 0
    r = away(tmp_path, r, "close")
    r.resume(1)
    record = r.state.core(spec.core)
    assert ((spec.core, spec.value), once) == ((0, -3), (PauseReason.OUTSIDE_WRITE, -2, -3))
    assert (r.state.lifecycle, record.value, record.fb) == (Lifecycle.RUNNING, -2, -3)
    assert unjudged_notes(r) == [repeat_note(0, -3, -3)]


@pytest.mark.req("L18.d.3.core")
def test_the_core_a_same_boot_resume_fails_for_the_test_its_database_lost_is_judged_once():
    fake = fresh(2, [-3, -2], 1, max_offset=-8, k_1t=1)
    start(fake)
    while fake.worker is None:
        assert fake.progress()
    core, value = fake.worker.spec.core, fake.worker.spec.value
    cut = replace(fake.state, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(PauseReason.RESUMING, "reading"))
    stock = tuple((c, 0) for c, _ in fake.layout.cpus_of)

    def resumed(*records: Evidence):
        read = CrashEvidenceRead(
            dirty_end=False,
            was_running=True,
            same_boot=True,
            stale_db=True,
            marked_now=True,
            evidence=records,
            co_now=stock,
        )
        return transition(cut, read, fake.world())

    alone = resumed()
    judged = resumed(Evidence(core, True, value))
    assert (alone.state.lifecycle, alone.state.core(core).fb) == (Lifecycle.RUNNING, value)
    assert judged.state == alone.state
    assert (notes(alone, "record_unjudged"), notes(judged, "record_unjudged")) == (
        [],
        [
            unjudged(
                core,
                f"corrected machine check charged to {value}",
                f"the fail verdict on core {core}'s test, whose result the database lost, decided its verdict",
            )
        ],
    )
    again = read_before_the_first_test(judged.state, fake.world(), Evidence(core, True, value))
    assert (again.state.lifecycle, again.state.cores) == (Lifecycle.RUNNING, judged.state.cores)
    assert notes(again, "record_unjudged") == [repeat_note(core, value, value)]


@pytest.mark.req("L18.d.3.repeat")
@pytest.mark.parametrize(("stage", "value"), [(Stage.SEARCH, 0), (Stage.TIGHTEN, 0), (Stage.SEARCH, -1)])
def test_a_record_charged_at_or_behind_its_core_s_fail_bound_repeats_a_judged_failure_and_is_not_judged(stage, value):
    s, world = fine_at(value, -2, stage)
    repeat = read_before_the_first_test(s, world, Evidence(0, True, -2))
    stock = read_before_the_first_test(s, world, Evidence(0, True, 0))
    assert (repeat.state.lifecycle, repeat.state.cores, repeat.state.incidents) == (
        Lifecycle.RUNNING,
        s.cores,
        s.incidents,
    )
    assert notes(repeat, "record_unjudged") == [repeat_note(0, -2, -2)]
    if stage is Stage.SEARCH:
        assert stock.state.pause.reason is PauseReason.STOCK_UNSTABLE
    else:
        assert stock.state.incidents == (*s.incidents, Incident.STOCK_MACHINE_CHECK)


@pytest.mark.req("L18.d.3.unplaced")
def test_a_record_the_journal_cannot_place_on_a_core_the_session_holds_at_stock_is_noted_not_judged():
    s, world = fine_at(0, None)
    unplaced = read_before_the_first_test(s, world, Evidence(0, True))
    assert (unplaced.state.lifecycle, unplaced.state.cores, notes(unplaced, "record_unplaced")) == (
        Lifecycle.RUNNING,
        s.cores,
        [],
    )
    assert notes(unplaced, "record_unjudged") == [
        "core 0's machine check is not judged: the CO journal cannot place its moment while the session held it "
        "at stock, so the value it ran at is unknown"
    ]


@pytest.mark.req("L18.d.3.charged")
def test_a_record_from_a_steady_test_cut_in_its_first_seconds_is_charged_to_the_steady_value(tmp_path, clock):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    live1_in_flight(r)
    clock.advance(60)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.STEADY)
    passed = dict(r.state.flow.allcore.floors)[0]
    steady = dict(r.state.slot.spec.vector)[0]
    r = away(tmp_path, r, "close")
    clock.advance(2)
    hardware.machine_check(0, True)
    r.resume(1)
    record = r.state.core(0)
    assert (passed, steady) == (-6, -3)
    assert (r.state.lifecycle, record.value, record.fb) == (Lifecycle.RUNNING, -2, -3)


@pytest.mark.req("L18.d.3.charged")
@pytest.mark.parametrize("read", ["resume", "before the first test"])
def test_a_record_after_a_database_failure_sent_the_cores_to_stock_is_charged_to_stock(
    tmp_path, clock, monkeypatch, read
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    live1_in_flight(r)
    save = r.store.save
    monkeypatch.setattr(r.store, "save", broken)
    stepped_until(r, lambda: r.state.pause is not None and r.state.pause.reason is PauseReason.DATABASE)
    monkeypatch.setattr(r.store, "save", save)
    lost = {c.core for c in r.store.load(1)[0][0].cores if c.in_test}
    other = next(c for c in r.state.cores if c.core not in lost and c.value != 0)
    clock.advance(20)
    if read == "resume":
        hardware.machine_check(other.core, True)
    else:
        hardware.arriving = [(other.core, True)]
    r.resume(1)
    stepped_until(r, lambda: r.worker is not None or r.at_rest())
    record = r.state.core(other.core)
    assert (stock_checks(r), record.value, record.fb) == (1, other.value, other.fb)


@pytest.mark.req("L18.d.3.once")
def test_a_cut_all_core_test_s_late_report_never_moves_the_stored_read_point_back(tmp_path, clock):
    hardware = KernelLog()
    runner = ArmedRunner(hardware)
    r = rig(tmp_path, hardware, runner, check_interval_s=5)
    live1_in_flight(r)
    clock.advance(60)
    stepped_until(r, lambda: r.worker is not None and r.state.flow.stage is Stage.STEADY)
    r.signals.append(PauseRequest())
    r.step(0.01)
    clock.advance(2)
    hardware.machine_check(0, True)
    r.resume(1)
    once = (r.state.core(0).value, r.state.core(0).fb)
    committed = r.store.db.load_machine_session(1)[2]
    r.step(0.01)
    hardware.problem = "journalctl -b exited 1"
    stepped_until(r, r.at_rest)
    hardware.problem = None
    kept = r.store.db.load_machine_session(1)[2]
    r.resume(1)
    assert (r.state.pause, kept, hardware.harvests[-1][1]) == (None, committed, committed)
    assert (r.state.core(0).value, r.state.core(0).fb) == once


@pytest.mark.req("L18.d.3.batch")
def test_one_pause_names_every_core_whose_record_pauses_and_each_cause():
    s, world = fine_at(-3, -5)
    stock = read_before_the_first_test(s, world, Evidence(0, True, 0), Evidence(1, True, 0))
    held = read_before_the_first_test(s, world, Evidence(7, True, -2), Evidence(0, True, 0))
    assert (stock.state.pause.reason, stock.state.pause.cores, stock.state.pause.detail) == (
        PauseReason.STOCK_UNSTABLE,
        (0, 1),
        "core 0 logged a machine check at its stock value; core 1 logged a machine check at its stock value",
    )
    assert (held.state.pause.reason, held.state.pause.cores, held.state.pause.detail) == (
        PauseReason.HELD_KNOB,
        (7, 0),
        "core 7 logged a machine check at a value this session does not own; "
        "core 0 logged a machine check at its stock value",
    )


@pytest.mark.req("L18.d.3.batch")
def test_two_records_that_each_reach_the_incident_limit_count_both_in_one_incidents_pause():
    s, world = fine_at(-3, -5)
    s = replace(
        s,
        flow=replace(s.flow, stage=Stage.TIGHTEN, allcore=AllCore(floors=((0, -8), (1, -8)))),
        incidents=(Incident.CRASH,),
    )
    paused = read_before_the_first_test(s, world, Evidence(0, True, 0), Evidence(1, True, 0)).state
    assert (paused.pause.reason, paused.incidents, paused.pause.detail) == (
        PauseReason.INCIDENTS,
        (Incident.CRASH, Incident.STOCK_MACHINE_CHECK, Incident.STOCK_MACHINE_CHECK),
        "1 crash nobody could be blamed for, 2 machine checks at stock values",
    )


def two_of_three_in(stage: Stage):
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    fake.config = replace(fake.config, selected=(0, 1))
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not stage or fake.worker.spec.core != 0:
        assert fake.progress()
    return fake


def verdict_with(fake, s: SessionState, *records: Evidence):
    report = Report(Group.PASS, fake.worker.spec.seconds, evidence=records)
    return transition(s, live.WorkerReported(fake.worker.token, report), fake.world())


@pytest.mark.req("G3.once")
@pytest.mark.parametrize(
    ("stage", "core", "sits", "weaker", "stronger"),
    [
        (Stage.STEADY, 1, None, True, False),
        (Stage.STEADY, 1, -1, True, True),
        (Stage.TIGHTEN, 0, None, True, False),
        (Stage.LIVE1, 1, None, True, False),
    ],
)
def test_a_test_verdict_judges_each_core_once_by_its_strongest_record_and_names_the_others(
    stage, core, sits, weaker, stronger
):
    fake = two_of_three_in(stage)
    ran = dict(fake.worker.spec.vector)[core]
    s = fake.state if sits is None else fake.state.with_core(replace(fake.state.core(core), value=sits))
    both = verdict_with(fake, s, Evidence(core, weaker), Evidence(core, stronger))
    alone = verdict_with(fake, s, Evidence(core, stronger))
    assert both.state == alone.state
    assert (alone.state.core(core).fb, alone.state.core(core).value > ran) == (ran, True)
    assert notes(both, "record_unjudged") == [set_aside(core, f"corrected machine check charged to {ran}")]


@pytest.mark.req("G3.batch")
@pytest.mark.parametrize("stage", [Stage.STEADY, Stage.LIVE1])
def test_a_test_verdict_judges_the_records_after_one_that_pauses(stage):
    fake = two_of_three_in(stage)
    paused = verdict_with(fake, fake.state, Evidence(2, True), Evidence(1, True)).state
    alone = verdict_with(fake, fake.state, Evidence(1, True)).state
    assert (paused.pause.reason, paused.pause.cores, paused.core(1)) == (PauseReason.HELD_KNOB, (2,), alone.core(1))
    assert alone.core(1).value > fake.state.core(1).value


@pytest.mark.req("G3.batch")
def test_one_pause_of_a_test_verdict_names_every_core_whose_record_pauses_and_each_cause():
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    start(fake)
    while fake.worker is None:
        assert fake.progress()
    first, second = (core for core in (0, 1, 2) if core != fake.worker.spec.core)
    pause = verdict_with(fake, fake.state, Evidence(first, True), Evidence(second, True)).state.pause
    stock = "logged a machine check at its stock value"
    assert (fake.worker.spec.stage, pause.reason, pause.cores, pause.detail) == (
        Stage.SEARCH,
        PauseReason.STOCK_UNSTABLE,
        (first, second),
        f"core {first} {stock}; core {second} {stock}",
    )


def resuming_from(stage: Stage, values: dict[int, int]) -> tuple[SessionState, object]:
    fake = two_of_three_in(stage)
    s = fake.state
    for core, value in values.items():
        s = s.with_core(replace(s.core(core), value=value, fb=None))
    return replace(s, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(PauseReason.RESUMING, "reading")), fake.world()


def read_at_resume(s: SessionState, world, *records: Evidence, same_boot: bool = False, **facts):
    read = CrashEvidenceRead(not same_boot, same_boot, True, evidence=records, co_now=((0, 0), (1, 0), (2, 0)), **facts)
    return transition(s, read, world)


HELD_CRASH = "core 2 crashed the machine at a value this session does not own"


@pytest.mark.req("L18.d.7.records", "L18.d.4.3a", "L18.d.4.5")
@pytest.mark.parametrize(
    ("values", "facts", "records", "pause", "judged"),
    [
        (
            {0: 0, 1: 0},
            {"same_boot": True, "stale_db": True, "marked_now": True},
            (Evidence(1, True, -2),),
            (PauseReason.STOCK_UNSTABLE, (0,), "core 0 fails at stock with every core at 0"),
            (0, -2, 0, 0, ()),
        ),
        (
            {0: 0, 1: -6},
            {"suspects": (0,)},
            (Evidence(1, True),),
            (PauseReason.STOCK_UNSTABLE, (0,), "core 0 crashed at 0"),
            (-5, -6, 0, 1, ()),
        ),
        (
            {0: 0, 1: 0},
            {},
            (Evidence(1, True),),
            (PauseReason.STOCK_UNSTABLE, (), "the machine crashed with every owned core at 0"),
            (0, None, 0, 0, (Incident.CRASH,)),
        ),
        (
            {0: 0, 1: 0},
            {},
            (Evidence(0, True), Evidence(1, True)),
            (PauseReason.STOCK_UNSTABLE, (), "the machine crashed with every owned core at 0"),
            (0, None, 0, 0, (Incident.CRASH,)),
        ),
        (
            {0: 0, 1: 0},
            {"stale_db": True},
            (Evidence(1, True),),
            (PauseReason.STOCK_UNSTABLE, (0,), "core 0 fails at stock with every core at 0"),
            (0, None, 0, 0, (Incident.CRASH,)),
        ),
        (
            {1: -6},
            {},
            (Evidence(2, False), Evidence(1, True)),
            (PauseReason.HELD_KNOB, (2,), HELD_CRASH),
            (-5, -6, 0, 0, ()),
        ),
        (
            {1: -6},
            {},
            (Evidence(1, False), Evidence(2, False)),
            (PauseReason.HELD_KNOB, (2,), HELD_CRASH),
            (-3, -6, 1, 1, ()),
        ),
    ],
)
def test_a_resume_that_pauses_early_judges_the_other_records_of_its_read_first(values, facts, records, pause, judged):
    s, world = resuming_from(Stage.LIVE1, values)
    paused = read_at_resume(s, world, *records, **facts).state
    core = paused.core(1)
    assert (paused.pause.reason, paused.pause.cores, paused.pause.detail) == pause
    assert (core.value, core.fb, core.crash_count, paused.resume_streak, paused.incidents) == judged


@pytest.mark.req("L18.d.7.records", "L18.d.7")
@pytest.mark.parametrize(
    ("stored", "facts", "detail"),
    [
        (
            (Incident.CRASH,),
            {"stale_db": True},
            "core 0 fails at stock with every core at 0; 2 crashes nobody could be blamed for",
        ),
        ((Incident.CRASH, Incident.CRASH), {"suspects": (0,)}, "core 0 crashed at 0"),
    ],
)
def test_a_resume_that_pauses_early_names_the_incident_limit_only_when_its_own_crash_reaches_it(stored, facts, detail):
    s, world = resuming_from(Stage.LIVE1, {0: 0, 1: 0})
    paused = read_at_resume(replace(s, incidents=stored), world, **facts).state
    assert (paused.incidents, paused.pause.reason, paused.pause.cores, paused.pause.detail) == (
        (Incident.CRASH, Incident.CRASH),
        PauseReason.STOCK_UNSTABLE,
        (0,),
        detail,
    )


@pytest.mark.req("L18.d.7.records")
def test_a_resume_that_pauses_early_names_its_own_cause_first_then_each_record_that_pauses():
    s, world = resuming_from(Stage.SEARCH, {})
    other = read_at_resume(s, world, Evidence(2, False), Evidence(1, True)).state.pause
    assert (other.reason, other.cores, other.detail) == (
        PauseReason.HELD_KNOB,
        (2, 1),
        f"{HELD_CRASH}; core 1 logged a machine check at its stock value",
    )


@pytest.mark.req("L18.d.4.3b.held", "U50")
@pytest.mark.parametrize(
    ("stage", "core"), [(Stage.SEARCH, 0), (Stage.STEADY, 0), (Stage.TIGHTEN, 1), (Stage.LIVE1, 1)]
)
def test_a_dead_boot_s_uncorrected_record_on_a_tuned_core_convicts_it_beside_a_held_core_s_crash(stage, core):
    s, world = resuming_from(stage, {})
    alone = read_at_resume(s, world, Evidence(core, False)).state
    held = read_at_resume(s, world, Evidence(2, False), Evidence(core, False)).state
    assert (alone.lifecycle, alone.core(core).crash_count, alone.resume_streak) == (Lifecycle.RUNNING, 1, 1)
    convicted = (held.core(core), held.flow.allcore, held.resume_streak, held.incidents)
    assert convicted == (alone.core(core), alone.flow.allcore, 1, ())
    assert (held.pause.reason, held.pause.cores, held.pause.detail) == (PauseReason.HELD_KNOB, (2,), HELD_CRASH)


@pytest.mark.req("L18.d.4.3b.held", "L18.d.6", "L18.d.4.5")
@pytest.mark.parametrize(
    ("stage", "values", "records", "facts"),
    [
        (Stage.SEARCH, {}, (Evidence(2, False), Evidence(0, False)), {}),
        (Stage.STEADY, {}, (Evidence(2, False), Evidence(0, False)), {}),
        (Stage.SEARCH, {}, (Evidence(2, False), Evidence(0, False), Evidence(1, True)), {}),
        (Stage.LIVE1, {0: 0}, (Evidence(1, True),), {"suspects": (0,)}),
    ],
)
def test_a_crash_that_brings_the_resume_streak_to_its_limit_quarantines_before_any_pause(stage, values, records, facts):
    s, world = resuming_from(stage, values)
    quarantined = read_at_resume(replace(s, resume_streak=2), world, *records, **facts).state
    assert (quarantined.lifecycle, quarantined.resume_streak, quarantined.pause) == (Lifecycle.QUARANTINED, 3, None)


@pytest.mark.req("L18.d.3.core", "L18.d.4.once")
@pytest.mark.parametrize("stage", [Stage.LIVE1, Stage.STEADY])
def test_a_dead_boot_judges_its_records_alike_whatever_order_its_kernel_log_holds_them_in(stage):
    s, world = resuming_from(stage, {})
    held = s.core(1).value
    stock, tuned, other = Evidence(1, True, 0), Evidence(1, True, held), Evidence(0, True, 0)
    alone = read_at_resume(s, world, stock, tuned)
    orders = [(other, stock, tuned), (stock, tuned, other), (tuned, other, stock)]
    results = [read_at_resume(s, world, *records) for records in orders]
    first = results[0].state
    assert [result.state for result in results] == [first, first, first]
    assert (alone.state.incidents, alone.state.core(1).value, alone.state.core(1).fb) == (
        (Incident.STOCK_MACHINE_CHECK,),
        held + 1,
        held,
    )
    assert (first.incidents, first.core(1).value, first.core(1).fb) == (
        (Incident.STOCK_MACHINE_CHECK,),
        held + 1,
        held,
    )
    counted = unjudged(
        1,
        "corrected machine check charged to 0",
        "it is counted in the dead boot's one incident, and a stronger record of core 1 in the same read "
        "decided its verdict",
    )
    assert [notes(result, "record_unjudged") for result in (alone, *results)] == [[counted]] * 4


@pytest.mark.req("L18.d.3.core", "L18.d.4.3b")
def test_every_uncorrected_record_of_a_convicted_core_is_judged_at_stock_or_named():
    s, world = resuming_from(Stage.SEARCH, {})
    held = s.core(0).value
    stock = read_at_resume(s, world, Evidence(0, False, held), Evidence(0, False, 0))
    tuned = read_at_resume(s, world, Evidence(0, False, held), Evidence(0, False, held))
    decided = unjudged(
        0, f"uncorrected machine check charged to {held}", "the crash conviction of core 0 decided its verdict"
    )
    assert (stock.state.pause.reason, stock.state.pause.detail, notes(stock, "record_unjudged")) == (
        PauseReason.STOCK_UNSTABLE,
        "core 0 logged a machine check at its stock value",
        [decided],
    )
    assert (tuned.state.lifecycle, tuned.state.core(0).crash_count, notes(tuned, "record_unjudged")) == (
        Lifecycle.RUNNING,
        1,
        [decided, decided],
    )


@pytest.mark.req("L18.d.4.charged", "L18.d.4.3b")
def test_a_record_of_a_core_convicted_at_0_is_charged_before_it_is_set_aside_so_both_twins_are_stock_verdicts():
    s, world = resuming_from(Stage.LIVE1, {0: 0})
    uncharged = read_at_resume(s, world, Evidence(0, True), suspects=(0,)).state
    charged = read_at_resume(s, world, Evidence(0, True, 0), suspects=(0,)).state
    assert (uncharged, uncharged.incidents) == (charged, (Incident.STOCK_MACHINE_CHECK,))


@pytest.mark.req("L18.d.3.core", "L18.d.3.unplaced")
def test_a_record_set_aside_is_named_once_and_never_as_unplaced():
    s, world = resuming_from(Stage.SEARCH, {})
    convicted = read_at_resume(s, world, Evidence(0, False), Evidence(0, True))
    s, world = resuming_from(Stage.LIVE1, {})
    lost = read_at_resume(s, world, Evidence(0, True), same_boot=True, stale_db=True)
    named = [(notes(result, "record_unplaced"), len(notes(result, "record_unjudged"))) for result in (convicted, lost)]
    assert named == [([], 2), ([], 1)]


READ_ENDS = {"dead boot": (True, False), "same-boot resume": (False, True), "clean end": (False, False)}


@pytest.mark.req("L18.d.3.core", "L18.d.4.2.charged", "L18.d.3.batch")
@pytest.mark.parametrize("kind", ["dead boot", "same-boot resume", "clean end", "live read"])
def test_a_core_with_a_stock_record_and_a_tuned_one_counts_the_stock_incident_and_backs_off_in_every_read(kind):
    fake = two_of_three_in(Stage.LIVE1)
    held = fake.state.core(1).value
    records = (Evidence(1, True, 0), Evidence(1, True, held))
    if kind == "live read":
        fake.deliver()
        result = transition(fake.state, KernelLogRead(fake.state.slot.token, records), fake.world())
    else:
        s, world = resuming_from(Stage.LIVE1, {})
        dirty, same = READ_ENDS[kind]
        read = CrashEvidenceRead(dirty, same, True, evidence=records, co_now=((0, 0), (1, 0), (2, 0)))
        result = transition(s, read, world)
    after = result.state
    assert (after.incidents, after.core(1).value, after.core(1).fb) == (
        (Incident.STOCK_MACHINE_CHECK,),
        held + 1,
        held,
    )
    counted = "the dead boot's one incident" if kind == "dead boot" else "its core's stock incident"
    [aside] = notes(result, "record_unjudged")
    assert f"charged to 0 is not judged: it is counted in {counted}, and " in aside


@pytest.mark.req("L18.d.3.batch", "G3.zero_elsewhere")
def test_a_live_read_whose_stock_records_bring_the_incidents_to_the_limit_pauses_as_incidents():
    fake = two_of_three_in(Stage.LIVE1)
    fake.deliver()
    s = fake.state
    result = transition(s, KernelLogRead(s.slot.token, (Evidence(0, True, 0), Evidence(1, True, 0))), fake.world())
    stock = Incident.STOCK_MACHINE_CHECK
    assert (s.incidents, result.state.incidents, result.state.pause.reason) == (
        (),
        (stock, stock),
        PauseReason.INCIDENTS,
    )


@pytest.mark.req("G3.charged", "G3.once")
def test_a_bisect_test_s_records_are_judged_at_the_values_the_test_ran_whatever_their_order():
    fake = fresh(4, [-12, -6, -3, -9], 1, max_offset=-20, k_1t=2)
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1:
        assert fake.progress()
    fake.crash(())
    fake.post(Resume(1))
    while (
        fake.worker is None
        or fake.worker.spec.stage is not Stage.BISECT
        or dict(fake.worker.spec.vector)[1] == 0
        or dict(fake.worker.spec.vector)[2] != 0
    ):
        assert fake.progress()
    s, ran = fake.state, dict(fake.worker.spec.vector)
    orders = ((Evidence(2, True), Evidence(1, True)), (Evidence(1, True), Evidence(2, True)))
    first, second = (verdict_with(fake, s, *records).state for records in orders)
    assert first == second
    assert (first.incidents, first.core(2), first.core(1).fb) == ((Incident.STOCK_MACHINE_CHECK,), s.core(2), ran[1])


@pytest.mark.req("L07.d", "L07.G3")
def test_a_live1_back_off_of_a_core_the_sequence_has_not_reached_tests_that_core_once_at_its_new_value():
    fake = two_of_three_in(Stage.LIVE1)
    held = fake.state.core(1).value
    fake.deliver(Report(Group.PASS, fake.worker.spec.seconds, evidence=(Evidence(1, True),)))
    tested = []
    while fake.state.flow.stage is Stage.LIVE1 and fake.progress():
        if fake.worker is not None and fake.worker.spec.stage is Stage.LIVE1:
            tested.append((fake.worker.spec.core, fake.worker.spec.value))
    assert tested == [(1, held + 1)]


QUARANTINED = "3 crashes in a row with no test surviving"


@pytest.mark.req("L18.d.6")
def test_a_quarantine_names_every_cause_it_takes_over_from_the_pause():
    s, world = resuming_from(Stage.SEARCH, {})
    held = read_at_resume(replace(s, resume_streak=2), world, Evidence(2, False), Evidence(0, False))
    reverts = [effect for effect in held.effects if isinstance(effect, RevertAll)]
    s, world = resuming_from(Stage.LIVE1, {0: 0})
    zero = read_at_resume(replace(s, resume_streak=2), world, suspects=(0,))
    assert notes(held, "quarantined") == [f"{QUARANTINED}; {HELD_CRASH}"]
    assert notes(zero, "quarantined") == [f"{QUARANTINED}; core 0 crashed at 0"]
    assert reverts == [RevertAll(s.baseline, True)]


@pytest.mark.req("L18.d.4.5", "L18.d.6")
def test_a_crash_with_every_owned_core_at_0_counts_an_incident_and_leaves_the_streak():
    s, world = resuming_from(Stage.LIVE1, {0: 0, 1: 0})
    paused = read_at_resume(replace(s, resume_streak=2), world).state
    assert (paused.lifecycle, paused.resume_streak, paused.incidents, paused.pause.reason) == (
        Lifecycle.PAUSED,
        2,
        (Incident.CRASH,),
        PauseReason.STOCK_UNSTABLE,
    )


@pytest.mark.req("L18.d.4.3b", "L18.d.3.core")
def test_a_convicted_core_s_own_record_charged_to_stock_is_judged_as_the_stock_verdict_it_is():
    s, world = resuming_from(Stage.SEARCH, {})
    alone = read_at_resume(s, world).state.core(0)
    result = read_at_resume(s, world, Evidence(0, False, 0))
    after, convicted = result.state, result.state.core(0)
    assert (after.pause.reason, after.pause.cores, after.pause.detail, after.resume_streak) == (
        PauseReason.STOCK_UNSTABLE,
        (0,),
        "core 0 logged a machine check at its stock value",
        1,
    )
    assert (convicted.value, convicted.fb, convicted.crash_count) == (alone.value, alone.fb, 1)
    assert notes(result, "record_unjudged") == []


@pytest.mark.req("L18.d.3.repeat", "L18.d.4.3b")
def test_a_dead_boot_s_uncorrected_record_charged_at_its_fail_bound_repeats_and_convicts_nothing():
    s, world = resuming_from(Stage.LIVE1, {})
    rec = s.core(1)
    repeat = read_at_resume(s, world, Evidence(1, False, rec.fb))
    twin = read_at_resume(s, world, Evidence(1, True, rec.fb))
    ahead = read_at_resume(s, world, Evidence(1, False, rec.value)).state
    unbounded, world = resuming_from(Stage.LIVE1, {1: rec.value})
    no_bound = read_at_resume(unbounded, world, Evidence(1, False, rec.value)).state
    assert (repeat.state, notes(repeat, "record_unjudged")) == (twin.state, [repeat_note(1, rec.fb, rec.fb)])
    assert (repeat.state.core(1).crash_count, ahead.core(1).crash_count, no_bound.core(1).crash_count) == (0, 1, 1)


@pytest.mark.req("L18.d.4.charged")
def test_a_record_that_ends_the_crash_s_bisection_back_in_live1_requeues_its_core_so_its_new_value_is_proven():
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    fake.config = replace(fake.config, selected=(0, 1))
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1 or fake.worker.spec.core != 1:
        assert fake.progress()
    held, tested = fake.state.core(0).value, fake.worker.spec.value
    fake.crash((Evidence(0, True),))
    fake.post(Resume(1))
    s = fake.state
    proven = []
    while fake.progress() and fake.state.flow.stage is Stage.LIVE1:
        if fake.worker is not None:
            proven.append((fake.worker.spec.core, fake.worker.spec.value))
    assert (s.flow.stage, s.flow.live1.requeue, s.core(0).value, s.core(0).fb) == (Stage.LIVE1, (0,), held + 1, held)
    assert proven == [(1, tested), (0, held + 1)]


@pytest.mark.req("L07.G3.convict")
def test_a_crash_conviction_in_live1_requeues_a_core_it_already_passed_so_its_new_value_is_proven():
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    fake.config = replace(fake.config, selected=(0, 1))
    start(fake)
    passed = []
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1 or fake.worker.spec.core != 1:
        if fake.worker is not None and fake.worker.spec.stage is Stage.LIVE1:
            passed.append(fake.worker.spec.core)
        assert fake.progress()
    held, tested = fake.state.core(0).value, fake.worker.spec.value
    fake.crash((Evidence(0, False),))
    fake.post(Resume(1))
    convicted = fake.state.core(0)
    proven = []
    while fake.progress() and fake.state.flow.stage is Stage.LIVE1:
        if fake.worker is not None:
            proven.append((fake.worker.spec.core, fake.worker.spec.value))
    assert (passed, convicted.fb, convicted.crash_count, convicted.value > held) == ([0], held, 1, True)
    assert proven == [(1, tested), (0, convicted.value)]


@pytest.mark.req("L07.G3.convict")
def test_a_crash_conviction_of_a_core_live1_has_queued_already_tests_it_once_at_its_new_value():
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    fake.config = replace(fake.config, selected=(0, 1))
    start(fake)
    while fake.worker is None or fake.worker.spec.stage is not Stage.LIVE1 or fake.worker.spec.core != 1:
        assert fake.progress()
    fake.deliver(Report(Group.PASS, 1.0, evidence=(Evidence(0, True),)))
    while fake.worker is None:
        assert fake.progress()
    queued = fake.worker.spec
    fake.crash((Evidence(0, False),))
    fake.post(Resume(1))
    after = fake.state.core(0)
    proven = []
    while fake.progress() and fake.state.flow.stage is Stage.LIVE1:
        if fake.worker is not None:
            proven.append((fake.worker.spec.core, fake.worker.spec.value))
    assert (queued.stage, queued.core, after.crash_count, after.value > queued.value) == (Stage.LIVE1, 0, 1, True)
    assert proven == [(0, after.value)]


@pytest.mark.req("L07.G3.convict")
def test_a_crash_conviction_outside_live1_queues_no_live1_test():
    s, world = resuming_from(Stage.STEADY, {})
    held = s.core(0).value
    after = read_at_resume(s, world, Evidence(0, False)).state
    convicted = after.core(0)
    assert (after.flow.stage, after.flow.live1, convicted.fb, convicted.crash_count) == (Stage.STEADY, None, held, 1)


@pytest.mark.req("L07.G3.convict")
def test_a_crash_conviction_that_ends_a_live1_begun_bisection_requeues_a_core_live1_already_passed():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    passed = []
    for _ in range(2):
        until_test(fake, Stage.LIVE1)
        passed.append(fake.worker.spec.core)
        fake.deliver()
    until_test(fake, Stage.LIVE1)
    fake.crash()
    fake.post(Resume(1))
    while fake.worker is None or not any(dict(fake.worker.spec.vector)[core] for core in passed):
        assert fake.progress()
    spec, entry = fake.worker.spec, fake.state.flow.bisect.entry
    target = next(core for core in passed if dict(spec.vector)[core])
    held = dict(spec.vector)[target]
    fake.crash((Evidence(target, False),))
    fake.post(Resume(1))
    convicted = fake.state.core(target)
    proven = []
    while fake.progress() and fake.state.flow.stage is Stage.LIVE1:
        if fake.worker is not None:
            proven.append((fake.worker.spec.core, fake.worker.spec.value))
    assert (spec.stage, entry, convicted.fb, convicted.crash_count) == (Stage.BISECT, Stage.LIVE1, held, 1)
    assert (target, convicted.value) in proven


@pytest.mark.req("L18.d.7.records", "L18.d.7")
@pytest.mark.parametrize(
    ("records", "cores", "cause"),
    [
        ((Evidence(1, True),), (1,), "core 1 logged a machine check at its stock value"),
        ((Evidence(2, True),), (2,), "core 2 logged a machine check at a value this session does not own"),
    ],
)
def test_a_crash_that_reaches_the_incident_limit_is_named_in_the_one_pause_a_record_raises(records, cores, cause):
    s, world = resuming_from(Stage.SEARCH, {})
    idle = replace(s, cores=tuple(replace(r, in_test=False) for r in s.cores), incidents=(Incident.CRASH,))
    pause = read_at_resume(idle, world, *records).state.pause
    assert (pause.reason, pause.cores, pause.detail) == (
        PauseReason.INCIDENTS,
        cores,
        f"2 crashes nobody could be blamed for; {cause}",
    )


@pytest.mark.req("L18.d.4.charged", "L18.d.4.3b")
def test_a_dead_boot_s_uncorrected_record_is_judged_by_its_charge_and_convicts_only_a_core_off_stock_at_death():
    s, world = resuming_from(Stage.STEADY, {})
    placed_at_stock = read_at_resume(s, world, Evidence(0, False, 0)).state
    corrected = read_at_resume(s, world, Evidence(0, True, 0)).state
    assert (placed_at_stock.core(0).crash_count, placed_at_stock.incidents) == (0, (Incident.STOCK_MACHINE_CHECK,))
    assert placed_at_stock == corrected
    s, world = resuming_from(Stage.SEARCH, {1: -4})
    earlier = read_at_resume(s, world, Evidence(1, False, -4)).state
    record = earlier.core(1)
    assert (earlier.lifecycle, earlier.core(0).crash_count, record.value, record.fb, record.crash_count) == (
        Lifecycle.RUNNING,
        1,
        -1,
        -4,
        0,
    )


@pytest.mark.req("L18.d.3.repeat")
def test_a_dead_boot_s_record_charged_at_its_core_s_fail_bound_leaves_the_core_where_it_sits():
    s, world = resuming_from(Stage.LIVE1, {})
    fb = s.core(1).fb
    repeat = read_at_resume(s, world, Evidence(1, True, fb))
    assert (repeat.state.core(1), notes(repeat, "record_unjudged")) == (s.core(1), [repeat_note(1, fb, fb)])


def unplaced_at_death(core: int, value: int) -> str:
    return (
        f"core {core}'s machine check is charged to {value}, its value when the last boot ended: "
        "its moment in that boot is not known"
    )


@pytest.mark.req("L18.d.4.charged")
def test_a_dead_boot_s_record_the_journal_cannot_place_is_charged_to_its_core_s_value_at_death_and_named():
    s, world = resuming_from(Stage.LIVE1, {})
    held = s.core(1).value
    result = read_at_resume(s, world, Evidence(1, True))
    assert (result.state.core(1).value, result.state.core(1).fb) == (held + 1, held)
    assert notes(result, "record_unplaced") == [unplaced_at_death(1, held)]


@pytest.mark.req("L18.d.4.2.charged")
def test_a_clean_end_s_record_is_charged_as_a_dead_boot_s_and_backs_its_tuned_core_off():
    s, world = resuming_from(Stage.SEARCH, {})
    held = s.core(0).value
    clean = CrashEvidenceRead(False, False, True, evidence=(Evidence(0, True),), co_now=((0, 0), (1, 0), (2, 0)))
    result = transition(s, clean, world)
    after = result.state
    assert (after.lifecycle, after.core(0).value, after.core(0).fb, after.resume_streak, after.incidents) == (
        Lifecycle.RUNNING,
        held + 1,
        held,
        0,
        (),
    )
    assert notes(result, "record_unplaced") == [unplaced_at_death(0, held)]


@pytest.mark.req("L18.d.4.3b.held")
def test_every_other_record_of_the_held_core_whose_crash_pauses_a_dead_boot_is_named_and_not_judged():
    s, world = resuming_from(Stage.LIVE1, {})
    result = read_at_resume(s, world, Evidence(2, False), Evidence(2, False), Evidence(2, True))
    pause = result.state.pause
    decided = "core 2's crash at a value this session does not own decided its verdict"
    assert (pause.reason, pause.cores, pause.detail) == (PauseReason.HELD_KNOB, (2,), HELD_CRASH)
    assert notes(result, "record_unjudged") == [
        unjudged(2, "uncorrected machine check with no charge", decided),
        unjudged(2, "corrected machine check with no charge", decided),
    ]


@pytest.mark.req("L18.d.7.convict")
def test_a_dead_boot_convicts_every_core_its_crash_convicts_before_it_pauses_once_for_those_at_stock():
    s, world = resuming_from(Stage.LIVE1, {0: 0, 1: -6})
    paused = read_at_resume(s, world, Evidence(1, True), suspects=(0, 1)).state
    s, world = resuming_from(Stage.LIVE1, {0: 0, 1: 0})
    both = read_at_resume(s, world, suspects=(0, 1)).state.pause
    convicted = paused.core(1)
    assert (paused.pause.reason, paused.pause.cores, paused.pause.detail) == (
        PauseReason.STOCK_UNSTABLE,
        (0,),
        "core 0 crashed at 0",
    )
    assert (convicted.value, convicted.fb, convicted.crash_count) == (-3, -6, 1)
    assert (both.reason, both.cores, both.detail) == (
        PauseReason.STOCK_UNSTABLE,
        (0, 1),
        "core 0 crashed at 0; core 1 crashed at 0",
    )


@pytest.mark.req("L18.d.4.once")
def test_a_resume_in_the_same_boot_counts_an_incident_for_each_core_with_a_record_at_stock():
    s, world = resuming_from(Stage.LIVE1, {0: 0, 1: 0})
    paused = read_at_resume(s, world, Evidence(0, True, 0), Evidence(1, True, 0), same_boot=True).state
    stock = Incident.STOCK_MACHINE_CHECK
    assert (paused.incidents, paused.pause.reason) == ((stock, stock), PauseReason.INCIDENTS)


@pytest.mark.req("L18.d.4.once")
@pytest.mark.parametrize(
    ("edges", "in_test", "records", "incidents"),
    [
        ([-6, 0, 0], True, ((1, True), (2, True)), (Incident.STOCK_MACHINE_CHECK,)),
        ([-6, -3, 0], True, ((0, False), (1, True), (2, True)), (Incident.STOCK_MACHINE_CHECK,)),
        ([-6, 0, 0], False, ((1, True), (2, True)), (Incident.CRASH,)),
    ],
)
def test_one_dead_boot_raises_the_incidents_once_however_many_of_its_records_are_at_stock(
    edges, in_test, records, incidents
):
    fake = fresh(3, edges, 1, max_offset=-20, k_1t=2)
    start(fake)
    while fake.state.flow.stage is not Stage.LIVE1 or (fake.worker is not None) is not in_test:
        assert fake.progress()
    fake.crash(tuple(Evidence(core, corrected) for core, corrected in records))
    fake.post(Resume(1))
    assert fake.state.incidents == incidents


@pytest.mark.req("L18.d.4.charged")
@pytest.mark.parametrize("stage", [Stage.STEADY, Stage.LIVE1])
@pytest.mark.parametrize(("corrected", "steps", "convicted"), [(True, 1, 0), (False, 3, 1)])
def test_a_dead_boot_s_records_back_each_core_off_from_the_value_it_held_when_the_boot_died(
    stage, corrected, steps, convicted
):
    fake = two_of_three_in(stage)
    (tested, value), (other, held) = [(r.core, r.value) for r in fake.state.cores]
    fake.crash((Evidence(tested, corrected), Evidence(other, True)))
    fake.post(Resume(1))
    s = fake.state
    assert (s.lifecycle, s.flow.stage, s.incidents, s.resume_streak) == (Lifecycle.RUNNING, stage, (), 1)
    assert [(r.core, r.value, r.fb, r.crash_count) for r in s.cores] == [
        (tested, value + steps, value, convicted),
        (other, held + 1, held, 0),
    ]


@pytest.mark.req("L18.d.4.charged")
def test_a_dead_boot_s_record_logged_in_the_wait_before_an_all_core_test_is_charged_by_that_boot_s_journal(
    tmp_path, clock
):
    hardware = KernelLog()
    r = rig(tmp_path, hardware, check_interval_s=5)
    r.start(config(), 1, 7, True)
    clock.advance(10)
    stepped_until(r, lambda: r.worker is None and r.state.flow.stage is Stage.TRANSIENT)
    clock.advance(20)
    hardware.machine_check(0, True)
    clock.advance(20)
    stepped_until(r, lambda: r.worker is not None)
    dying = r.state.core(0)
    hardware.boot, hardware.co = "boot-b", {0: 0, 1: 0, 8: 0}
    again = rig(tmp_path, hardware, db=r.store.db, check_interval_s=5)
    again.resume(1)
    assert (all(rec.in_test for rec in r.state.cores), dying.value < 0) == (True, True)
    assert (again.state.lifecycle, again.state.incidents, again.state.core(0).fb) == (
        Lifecycle.RUNNING,
        (Incident.STOCK_MACHINE_CHECK,),
        dying.fb,
    )


@pytest.mark.req("L18.d.4.charged")
def test_a_dead_boot_between_all_core_tests_charges_its_records_to_the_stock_its_cores_waited_at():
    fake = fresh(3, [-12, -6, -3], 1, max_offset=-20, k_1t=2)
    fake.config = replace(fake.config, selected=(0, 1))
    start(fake)
    while fake.state.flow.stage is not Stage.TRANSIENT or fake.worker is not None:
        assert fake.progress()
    waited = [(r.core, r.value, r.fb) for r in fake.state.cores]
    fake.crash((Evidence(0, True), Evidence(1, True)))
    fake.post(Resume(1))
    s = fake.state
    assert all(value != 0 for _, value, _ in waited)
    assert (s.lifecycle, s.incidents, [(r.core, r.value, r.fb) for r in s.cores]) == (
        Lifecycle.RUNNING,
        (Incident.CRASH,),
        waited,
    )


@pytest.mark.req("L18.d.4.charged")
@pytest.mark.parametrize("corrected", [True, False])
def test_a_core_the_crash_rule_moved_moves_once_for_its_record_and_a_record_at_stock_stays_a_stock_verdict(
    corrected,
):
    s, world = resuming_from(Stage.STEADY, {1: 0})
    held = s.core(0).value
    after = read_at_resume(s, world, Evidence(0, True), Evidence(1, corrected)).state
    moved = [(r.core, r.value, r.fb) for r in after.cores]
    assert (after.lifecycle, after.incidents) == (Lifecycle.RUNNING, (Incident.STOCK_MACHINE_CHECK,))
    assert moved == [(0, held + 1, held), (1, 0, None)]


@pytest.mark.req("L18.d.3.outside")
@pytest.mark.parametrize(("moved", "reason"), [(-30, PauseReason.OUTSIDE_WRITE), (None, PauseReason.SMU_READ)])
def test_a_record_the_journal_places_off_stock_on_a_moved_core_the_session_held_at_stock_is_judged_there(moved, reason):
    fake = fresh(2, [-3, -2], 1, max_offset=-8, k_1t=1)
    start(fake)
    s = fake.state
    s = s.with_core(replace(s.core(0), phase=Phase.PROVEN, value=0, fb=None))
    s = replace(
        s,
        lifecycle=Lifecycle.PAUSED,
        slot=None,
        pause=Pause(PauseReason.RESUMING, "reading"),
        flow=replace(s.flow, stage=Stage.TIGHTEN, allcore=AllCore(floors=((0, -3), (1, -8)))),
    )
    read = CrashEvidenceRead(
        dirty_end=False,
        was_running=True,
        same_boot=True,
        evidence=(Evidence(0, True, -1),),
        co_now=((0, moved), (1, 0)),
    )
    resumed = transition(s, read, fake.world())
    assert (resumed.state.pause.reason, resumed.state.core(0).value, resumed.state.core(0).fb) == (reason, 0, -1)
    assert notes(resumed, "record_unjudged") == []


@pytest.mark.req("L18.d.3.charged")
def test_a_corrupted_co_journal_row_in_the_boot_stops_no_read_that_has_no_record_to_charge(tmp_path):
    hardware = KernelLog()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    stepped_until(r, lambda: r.worker is not None)
    r = away(tmp_path, r, "close")
    another_session_wrote(r, -300)
    r.resume(1)
    assert r.state.lifecycle is Lifecycle.RUNNING
    one_test(r)
    assert (r.state.lifecycle, len(hardware.harvests)) == (Lifecycle.RUNNING, 2)


@pytest.mark.req("L01.baselines", "TERMS.kvw", "TM.I_M2")
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


@pytest.mark.req("TERMS.failing_effect", "TERMS.kvw_intent")
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


@pytest.mark.req("L29.d.log")
def test_a_worker_that_outlives_its_bound_is_said_and_kept(tmp_path):
    from corecycler.tuner.machine.types import AwaitWorker

    r = rig(tmp_path)
    said = []
    r.say = said.append
    release = threading.Event()
    stuck = threading.Thread(target=release.wait, daemon=True)
    stuck.start()
    r.worker = live._Worker(5, threading.Event(), stuck)
    r._apply(r.state, AwaitWorker(5, 0.05))
    assert said == ["test 5 did not stop within 0.05 s; its worker is kept until it ends"]
    assert r.worker.thread is stuck
    release.set()
    r._apply(r.state, AwaitWorker(5, 5.0))
    assert (r.worker, len(said)) == (None, 1)


@pytest.mark.req("L29.e")
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


@pytest.mark.req("L01.announce")
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


@pytest.mark.req("T22.F16")
def test_a_runner_that_raises_pauses_as_internal_with_its_run_time_unread(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, FakeRunner(hardware, raise_once=True))
    r.start(config(), 1, 7, True)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.INTERNAL
    tests = rows(r, "SELECT verdict, detail, peaks_json, seconds_run FROM tuner_tests")
    assert tests == [
        {
            "verdict": "internal_error",
            "detail": "the test runner raised RuntimeError('runner broke')",
            "peaks_json": None,
            "seconds_run": None,
        }
    ]


@pytest.mark.req("L19.d.inflight")
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


@pytest.mark.req("T22.F9")
def test_the_log_says_a_test_of_several_cores_with_each_core_s_value_never_one_core_as_its_subject(tmp_path):
    said = []
    r = rig(tmp_path)
    r.say = said.append
    r.start(config(), 1, 7, True)
    r.run_until_rest(0.01)
    tests = [line.split(": ", 1)[1] for line in said if line.startswith("test ")]
    one_core = [line for line in tests if line.startswith("core ")]
    several = [line for line in tests if not line.startswith("core ")]
    assert {line.split()[2] for line in one_core} == {Stage.SEARCH.value, Stage.LIVE1.value}
    assert several
    first = config().selected[0]
    assert all(f", cores {first} at " in line for line in several)


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
    scalar: float | None = 10.0
    boost: int | None = None
    commands: SimpleNamespace = field(
        default_factory=lambda: SimpleNamespace(co_range=(-50, 10), get_curve_shaper_cmd=None, ccd_fuse_addr=None)
    )
    shaper: tuple | None = None

    def get_curve_shaper(self):
        return self.shaper

    def get_pbo_scalar(self):
        return self.scalar

    def get_boost_limit(self):
        return self.boost

    def get_co_offset(self, core):
        return self.echo if self.echo is not None else self.co.get(core, 0)

    def set_co_offset(self, core, value):
        if self.accept:
            self.co[core] = value
        return self.accept


def sysfs(tmp_path: Path, scaling: int, top: int, boost: str | None, pstate: bool = True) -> Path:
    freq = tmp_path / "cpu" / "cpu0" / "cpufreq"
    freq.mkdir(parents=True)
    (freq / "scaling_cur_freq").write_text("624000")
    (freq / "scaling_max_freq").write_text(str(scaling))
    (freq / ("amd_pstate_max_freq" if pstate else "cpuinfo_max_freq")).write_text(str(top))
    if boost is not None:
        (tmp_path / "cpu" / "cpufreq").mkdir()
        (tmp_path / "cpu" / "cpufreq" / "boost").write_text(boost)
    return tmp_path / "cpu"


@pytest.mark.req("TERMS.kvw_refused", "TM.I_M3", "VERDICT.30")
def test_the_smu_adapter_writes_only_when_asked_and_checks_the_read_back():
    said = []
    dry = live_hw.SmuHardware(FakeSmu(), False, said.append, warm, vcache="none")
    assert dry.write_co(3, -5) is None and dry.smu.co == {} and said == ["dry: core 3 would be set to -5"]
    real = live_hw.SmuHardware(FakeSmu(), True, said.append, warm, vcache="none")
    assert real.write_co(3, -5) is None and real.read_co(3) == -5
    assert said[-1] == "core 3 set to -5, read back -5"
    assert "refused" in live_hw.SmuHardware(FakeSmu(accept=False), True, said.append, warm, vcache="none").write_co(
        3, -5
    )
    assert "-4" in live_hw.SmuHardware(FakeSmu(echo=-4), True, said.append, warm, vcache="none").write_co(3, -5)


@pytest.mark.req("TERMS.kvw_raised")
@pytest.mark.parametrize("error", [ValueError("CO value -60 out of range [-50, 10]"), OSError(5, "Input/output error")])
def test_a_write_the_smu_driver_raises_on_is_a_refusal_naming_the_error(error):
    smu = FakeSmu()

    def raising(core, value):
        raise error

    smu.set_co_offset = raising
    assert live_hw.SmuHardware(smu, True, print, warm, vcache="none").write_co(3, -5) == f"the SMU write raised {error}"


def pci_device(root, address, vendor, device, driver=None, vram=None):
    node = root / address
    node.mkdir(parents=True)
    (node / "vendor").write_text(vendor + "\n")
    (node / "device").write_text(device + "\n")
    if driver is not None:
        bound = root.parent / "drivers" / driver
        bound.mkdir(parents=True, exist_ok=True)
        (node / "driver").symlink_to(bound)
    if vram is not None:
        (node / "mem_info_vram_total").write_text(vram + "\n")


def test_the_igpu_is_found_by_its_pci_id_with_its_driver_and_carve_out(tmp_path):
    root = tmp_path / "devices"
    pci_device(root, "0000:01:00.0", "0x1002", "0x744c", driver="amdgpu", vram="25753026560")
    pci_device(root, "0000:7c:00.0", "0x1002", "0x13c0", driver="amdgpu", vram="536870912")
    assert live_hw.igpu(root) == {
        "present": True,
        "address": "0000:7c:00.0",
        "device": "0x13c0",
        "driver": "amdgpu",
        "vram_total": "536870912",
    }


def test_an_igpu_without_a_driver_is_present_and_unbound(tmp_path):
    root = tmp_path / "devices"
    pci_device(root, "0000:7c:00.0", "0x1002", "0x164e")
    assert live_hw.igpu(root) == {
        "present": True,
        "address": "0000:7c:00.0",
        "device": "0x164e",
        "driver": None,
        "vram_total": "unreadable: No such file or directory",
    }


def test_no_igpu_id_is_absent_and_two_or_an_unreadable_tree_is_unknown(tmp_path):
    root = tmp_path / "devices"
    pci_device(root, "0000:01:00.0", "0x1002", "0x744c", driver="amdgpu")
    assert live_hw.igpu(root) == {"present": False}
    pci_device(root, "0000:7c:00.0", "0x1002", "0x13c0")
    pci_device(root, "0000:7d:00.0", "0x1002", "0x164e")
    two = live_hw.igpu(root)
    assert two["present"] is None and "0000:7c:00.0, 0000:7d:00.0" in two["problem"]
    gone = live_hw.igpu(tmp_path / "none")
    assert gone["present"] is None and "not readable" in gone["problem"]


@pytest.mark.parametrize("calibrated", [True, False])
def test_the_adapter_gathers_the_boot_s_facts(monkeypatch, calibrated):
    pm = SimpleNamespace(is_calibrated=calibrated, fclk_mhz=2033.0, uclk_mhz=3000.0, mclk_mhz=3000.0)
    monkeypatch.setattr(live_hw, "PMTableReader", lambda: SimpleNamespace(read=lambda: pm))
    monkeypatch.setattr(live_hw, "read_bios_version", lambda: "2401")
    asked = []
    monkeypatch.setattr(live_hw.kernel, "reset_reasons", lambda boot: asked.append(boot) or (("a line",), None))
    monkeypatch.setattr(live_hw, "igpu", lambda: {"present": False})
    facts = live_hw.SmuHardware(FakeSmu(), False, print, warm, vcache="none").boot_facts("b1")
    clocks = (2033.0, 3000.0, 3000.0) if calibrated else (None, None, None)
    assert facts == BootFacts("2401", *clocks, ("a line",), None, {"present": False})
    assert asked == ["b1"]


@pytest.mark.req("T22.F17")
def test_a_boot_clock_that_reads_0_or_lies_past_the_table_is_recorded_unread(monkeypatch, tmp_path):
    from corecycler.smu import pmtable

    version, uclk = 0x00620205, 3000.0
    offsets = pmtable.PM_TABLE_OFFSETS[version]
    raw = bytearray(offsets.mclk)
    struct.pack_into("<f", raw, offsets.uclk, uclk)
    smu = tmp_path / "ryzen_smu_drv"
    smu.mkdir()
    (smu / "pm_table").write_bytes(bytes(raw))
    (smu / "pm_table_version").write_bytes(struct.pack("<I", version))
    monkeypatch.setattr(live_hw, "PMTableReader", lambda: pmtable.PMTableReader(smu))
    monkeypatch.setattr(live_hw, "read_bios_version", lambda: "2401")
    monkeypatch.setattr(live_hw.kernel, "reset_reasons", lambda boot: ((), None))
    monkeypatch.setattr(live_hw, "igpu", lambda: {"present": False})
    facts = live_hw.SmuHardware(FakeSmu(), False, print, warm, vcache="none").boot_facts("b1")
    assert (facts.fclk_mhz, facts.uclk_mhz, facts.mclk_mhz) == (None, uclk, None)


def note(kind: int, name: bytes, desc: bytes) -> bytes:
    def pad(raw: bytes) -> bytes:
        return raw + b"\0" * (-len(raw) % 4)

    return struct.pack("<III", len(name), len(desc), kind) + pad(name) + pad(desc)


def test_the_kernel_build_id_is_the_gnu_note_among_the_kernel_s_notes(tmp_path):
    notes = (
        note(1, b"Xen\0", b"\x01\x02\x03")
        + note(3, b"Linux\0", b"\x05\x06\x07")
        + note(3, b"GNU\0", bytes.fromhex("deadbeef00"))
    )
    assert live_hw.build_id(notes) == "deadbeef00"
    assert live_hw.build_id(note(1, b"Linux\0", b"\x00")) is None
    assert live_hw.build_id(b"") is None
    (tmp_path / "notes").write_bytes(notes)
    assert live_hw.kernel_build(tmp_path / "notes").endswith(" build deadbeef00")
    assert live_hw.kernel_build(tmp_path / "gone").endswith(" build unreadable: No such file or directory")


def test_microcode_is_every_distinct_revision_the_cpus_report(tmp_path):
    info = tmp_path / "cpuinfo"
    info.write_text("processor\t: 0\nmicrocode\t: 0xb404023\n\nprocessor\t: 1\nmicrocode\t: 0xb404023\n")
    assert live_hw.microcode(info) == "0xb404023"
    info.write_text("microcode\t: 0xb404023\nmicrocode\t: 0xb404022\n")
    assert live_hw.microcode(info) == "0xb404022,0xb404023"
    info.write_text("processor\t: 0\n")
    assert live_hw.microcode(info) == "absent"
    assert live_hw.microcode(tmp_path / "gone") == "unreadable: No such file or directory"


def test_the_app_version_is_the_installed_package_s_or_says_it_is_not_installed(monkeypatch):
    monkeypatch.setattr(live_hw.metadata, "version", lambda name: "1.2.3")
    assert live_hw.app_version() == "1.2.3"

    def absent(name):
        raise live_hw.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(live_hw.metadata, "version", absent)
    assert live_hw.app_version() == "unreadable: not installed"


def test_the_qt_version_is_the_installed_pyside6_s_or_says_it_is_not_installed(monkeypatch):
    asked = []

    def installed(name):
        asked.append(name)
        return "6.10.1"

    monkeypatch.setattr(live_hw.metadata, "version", installed)
    assert (live_hw.qt_version(), asked) == ("6.10.1", ["PySide6"])

    def absent(name):
        raise live_hw.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(live_hw.metadata, "version", absent)
    assert live_hw.qt_version() == "not installed"


@pytest.mark.req("D49", "L24.c.keys", "L24.l24a.keys")
def test_the_setup_holds_one_line_per_key_with_the_reason_for_what_could_not_be_read(tmp_path, monkeypatch):
    dmi = tmp_path / "dmi"
    dmi.mkdir()
    for name, value in (("bios_vendor", "ASUS"), ("bios_version", "2401"), ("bios_date", "05/01/2026")):
        (dmi / name).write_text(value + "\n")
    cpu = tmp_path / "cpu"
    cpu.mkdir()
    (cpu / "online").write_text("0-3\n")
    monkeypatch.setattr(live_hw, "DMI", dmi)
    monkeypatch.setattr(live_hw, "MODULES", tmp_path / "modules")
    monkeypatch.setattr(live_hw, "SMU_DRIVER", str(tmp_path / "drv"))
    monkeypatch.setattr(live_hw, "read_power_limits", lambda: (200.0, None, 230.0))
    monkeypatch.setattr(live_hw, "app_version", lambda: "1.2.3")
    monkeypatch.setattr(live_hw, "kernel_build", lambda: "7.2.7 build abc")
    monkeypatch.setattr(live_hw, "microcode", lambda: "0xb404023")
    monkeypatch.setattr(live_hw, "pm_table_version", lambda: "0x00620205")
    monkeypatch.setattr(live_hw, "qt_version", lambda: "6.10.1")
    hw = live_hw.SmuHardware(FakeSmu(), False, print, warm, cpu, vcache="ccd 0")
    setup = dict(hw.setup(((0, (0, 2)), (1, (1, 3))), ((0, 0), (1, 1))))
    assert setup == {
        "app": "1.2.3",
        "bios": "ASUS 2401 05/01/2026 unreadable: No such file or directory",
        "curve_shaper": "none on this generation",
        "kernel": "7.2.7 build abc",
        "microcode": "0xb404023",
        "pbo": "ppt 200.0 tdc unreadable edc 230.0 scalar 10.0 boost unreadable",
        "pm_table": "0x00620205",
        "python": live_hw.platform.python_version(),
        "qt": "6.10.1",
        "ryzen_smu": "unreadable: No such file or directory unreadable: No such file or directory",
        "smu_firmware": "unreadable: No such file or directory",
        "topology": "online 0-3; 0: 0,2 l3 0; 1: 1,3 l3 1",
        "vcache": "ccd 0",
        "zenpower": "not loaded",
    }
    (tmp_path / "modules" / "zenpower").mkdir(parents=True)
    (tmp_path / "modules" / "zenpower" / "version").write_text("0.4.0\n")
    assert dict(hw.setup((), ()))["zenpower"] == "0.4.0 unreadable: No such file or directory"


@pytest.mark.req("TM.A2.x3d")
@pytest.mark.parametrize(
    ("is_x3d", "ccd", "fact"),
    [(True, 1, "ccd 1"), (True, None, "unknown"), (False, None, "none")],
)
def test_the_setup_names_the_v_cache_ccd_and_says_when_it_cannot_tell(is_x3d, ccd, fact):
    assert live_hw.vcache_fact(SimpleNamespace(is_x3d=is_x3d, vcache_ccd=ccd)) == fact


@pytest.mark.req("D87", "KC.6")
def test_a_resume_in_the_same_boot_after_another_tool_wrote_a_core_pauses_as_an_outside_write(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    r.post(PauseRequest())
    r.run_until_rest(0.01)
    left = dict(hardware.co)
    hardware.co[1] = -19
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    final = again.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.OUTSIDE_WRITE
    assert f"core 1 reads -19, the session left it at {left[1]}" in final.pause.detail
    assert hardware.co[1] == 0


@pytest.mark.req("D17", "L17.G11", "L23.h")
def test_a_resume_after_a_database_pause_in_the_same_boot_takes_the_stock_reads_as_the_tool_s_own(
    tmp_path, monkeypatch
):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while not any(hardware.co.values()):
        r.step(0.01)
    monkeypatch.setattr(r.store, "save", broken)
    assert r.run_until_rest(0.01).pause.reason is PauseReason.DATABASE
    monkeypatch.undo()
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    while again.worker is None and not again.at_rest():
        again.step(0.01)
    assert again.state.pause is None


@pytest.mark.req("L24.d.pause", "L24.l24a.compare")
def test_a_session_resumed_on_a_changed_setup_pauses_before_any_test_and_names_the_change(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    r.post(PauseRequest())
    r.run_until_rest(0.01)
    hardware.setup_now = (("bios", "B"),)
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    final = again.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.SETUP_CHANGED
    assert "bios was A, is now B" in final.pause.detail
    assert again.worker is None


@pytest.mark.req("L24.d.nochange")
def test_a_resume_where_only_recorded_only_facts_changed_notes_them_and_goes_on(tmp_path):
    hardware = FakeHardware()
    hardware.setup_now = (("bios", "A"), ("python", "3.14.6"))
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    r.post(PauseRequest())
    r.run_until_rest(0.01)
    hardware.setup_now = (("bios", "A"), ("python", "3.14.7"), ("qt", "6.10.1"))
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    while again.worker is None and again.state.pause is None:
        again.step(0.01)
    assert again.state.pause is None
    noted = [e["message"] for e in again.store.db.get_tuner_events(1) if e["severity"] == "setup_noted"]
    assert noted == ["python was 3.14.6, is now 3.14.7", "qt was absent, is now 6.10.1"]
    assert dict(again.state.setup) == {"bios": "A", "python": "3.14.7", "qt": "6.10.1"}


def test_the_adapter_reads_the_kernel_facts_it_needs(monkeypatch):
    monkeypatch.setattr(live_hw.kernel, "read_boot_id", lambda: "b1")
    monkeypatch.setattr(live_hw.kernel, "boot_end", lambda boot: kernel.BootEnd(clean=boot == "b0", problem=None))
    hw = live_hw.SmuHardware(FakeSmu(), False, print, warm, vcache="none")
    assert (hw.boot_id(), hw.boot_end("b0").clean, hw.boot_end("b9").clean, hw.temperature()) == (
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
        live_hw.kernel,
        "harvest_after",
        lambda boot, after, next_boot, through_mono_ns: (
            asked.append((boot, after, next_boot, through_mono_ns)) or found
        ),
    )
    assert hw.harvest("b0", 5_000, True, None) is found
    assert hw.harvest("b1", None, False, 9_000) is found
    assert asked == [
        ("b0", kernel.HarvestFrom(mono_ns=5_000, realtime_us=None), True, None),
        ("b1", None, False, 9_000),
    ]


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


@pytest.mark.req("L27.e.uncorrected")
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


@pytest.mark.req("D51", "K8.tests", "K9.tests", "EC.G1")
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
            seen.append(kwargs["settle_s"])

        def run(self, lanes, cfg, duration):
            seen.append((lanes[0].cpus, cfg.tests, cfg.seconds_per_test, cfg.binary, duration))
            return {
                lanes[0].core_id: StressResult(
                    core_id=lanes[0].core_id, outcome=Outcome.PASS, detail="ok", duration_seconds=duration
                )
            }

    monkeypatch.setattr(live_hw.kernel, "ErrorDetector", FakeDetector)
    monkeypatch.setattr(live_hw.kernel, "mce_settle_s", lambda: 6.0)
    runner = live_hw.StressRunner(lambda: "backend", tmp_path, {0: 0, 16: 0}, warm, dict, supervisor=FakeSupervisor)
    one = runner.run(
        Spec(Klass.T1, 0, -5, (16,), 60.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0), threading.Event()
    )
    two = runner.run(
        Spec(Klass.T2, 0, -5, (0, 16), 90.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0), threading.Event()
    )
    assert one.group is Group.PASS and two.group is Group.PASS
    assert one.kernel_read_ns == two.kernel_read_ns == 4_242
    assert ((16,), ("SFTv4", "FFTv4", "N63"), 20, None, 60.0) in seen
    assert ((0, 16), ("SFTv4", "N63", "VT3"), 30, "19-ZN2 ~ Kagari", 90.0) in seen
    assert seen.count("reset") == 2 and seen.count("close") == 2
    assert seen.count(6.0) == 2


@pytest.mark.req("HANG.heartbeat", "HANG.path", "HANG.between")
@pytest.mark.parametrize("sudo_uid", [None, 1234], ids=["as the user", "as root under sudo"])
def test_the_driver_hands_its_invoking_user_s_heartbeat_to_the_supervisor_of_each_test(tmp_path, monkeypatch, sudo_uid):
    from corecycler.config.paths import heartbeat_path

    handed = []

    class FakeDetector:
        read_started_ns = 0

        def reset(self):
            pass

        def close(self):
            pass

    class FakeSupervisor:
        def __init__(self, **kwargs):
            handed.append(kwargs.get("heartbeat"))

        def run(self, lanes, cfg, duration):
            core = lanes[0].core_id
            return {core: StressResult(core_id=core, outcome=Outcome.PASS, detail="", duration_seconds=duration)}

    monkeypatch.setattr(live_hw.kernel, "ErrorDetector", FakeDetector)
    monkeypatch.setattr(live_hw.kernel, "mce_settle_s", lambda: 0.0)
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--cores", "0", "--max-offset", "-8"])
    kagari = SimpleNamespace(binary_problem=lambda name: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "moved-home"))
    db = database(Path("history") / "hb.db")
    with monkeypatch.context() as sudo:
        if sudo_uid is not None:
            sudo.setattr(os, "geteuid", lambda: 0)
            sudo.setenv("SUDO_UID", str(sudo_uid))
        built = live_hw.build_rig(args, topology(), FakeSmu(), lambda: kagari, lambda line: None, db, IDENTITY, 300)
    built.runner.supervisor = FakeSupervisor
    spec = Spec(Klass.T1, 0, -5, (16,), 60.0, ((0, -5),), Stage.SEARCH, thermal_limit_c=95.0)
    assert built.runner.run(spec, threading.Event()).group is Group.PASS
    [beats] = handed
    assert beats is built.heartbeat
    assert beats.path == heartbeat_path(os.getuid() if sudo_uid is None else sudo_uid)
    assert beats.probe_dir == tmp_path.resolve() / "history"


def beat_of(heartbeat: Path) -> dict:
    return json.loads(heartbeat.read_text()) if heartbeat.exists() else {}


def beating_rig(tmp_path, hardware: FakeHardware, runner) -> tuple[LiveRig, Path, list[float]]:
    r = rig(tmp_path, hardware, runner)
    heartbeat = tmp_path / "run" / "heartbeat.json"
    r.heartbeat = Heartbeat(heartbeat, tmp_path / "history")
    now = [0.0]
    r.clock = lambda: now[0]
    return r, heartbeat, now


@pytest.mark.req("HANG.between")
@pytest.mark.parametrize("rest", [PauseRequest, Close], ids=["pause", "close"])
def test_the_driver_beats_between_with_its_probe_dir_through_a_long_cool_down_and_says_idle_once_it_rests(
    tmp_path, rest
):
    hardware = FakeHardware()
    heat = [99.0]
    hardware.temperature = lambda: heat[0]
    r, heartbeat, now = beating_rig(tmp_path, hardware, FakeRunner(hardware, group=Group.THERMAL))
    r.start(config(), 1, 7, True)

    def stepped() -> dict:
        now[0] += 1.0
        r.step(0.001)
        return beat_of(heartbeat)

    for _ in range(100):
        if getattr(r.state.slot, "cool_below_c", None) is not None:
            break
        stepped()
    r.runner.group = None
    began = now[0]
    seen = [(now[0], stepped()) for _ in range(150)]
    assert now[0] - began == 150.0 and getattr(r.state.slot, "cool_below_c", None) is not None
    assert {(beat.get("state"), beat.get("probe_dir")) for _, beat in seen} == {("between", str(tmp_path / "history"))}
    assert {beat["pid"] for _, beat in seen} == {os.getpid()}
    written = [at for (at, beat), (_, before) in zip(seen[1:], seen, strict=False) if beat["at"] != before["at"]]
    assert len(written) >= 28 and {later - earlier for earlier, later in itertools.pairwise(written)} == {5.0}
    r.signals.append(rest())
    r.run_until_rest(0.001)
    assert beat_of(heartbeat)["state"] == "idle"


@pytest.mark.req("HANG.between", "HANG.watch")
def test_a_test_that_ends_and_then_blocks_leaves_a_between_beat_that_goes_stale_and_resets(tmp_path):
    from corecycler import cli
    from corecycler.smu import writer_lock

    hardware = FakeHardware()
    ended, released = threading.Event(), threading.Event()

    class EndsThenBlocks(FakeRunner):
        def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
            if not ended.is_set():
                supervisor, _, _ = make_supervisor(FakeBackend(), heartbeat=r.heartbeat)
                run_one(supervisor, lane(tmp_path), 0.1)
                ended.set()
                released.wait(30)
            return super().run(spec, stop, sink)

    r, heartbeat, _ = beating_rig(tmp_path, hardware, EndsThenBlocks(hardware))
    jumps = itertools.count()
    r.clock = lambda: time.monotonic() + 10.0 * next(jumps)
    found: dict = {}

    def watched() -> None:
        try:
            if ended.wait(10):
                found["last"] = beat_of(heartbeat)
                time.sleep(0.2)
                found["later"] = beat_of(heartbeat)
                stale = found["last"]["at"] + cli.HANG_STALE_S + 1.0
                found["watch"] = cli.hang_reason(heartbeat, cli.HANG_STALE_S, stale, writer_lock.process_state)
        finally:
            released.set()
            r.signals.append(PauseRequest())

    watching = threading.Thread(target=watched, daemon=True)
    r.start(config(), 1, 7, True)
    watching.start()
    r.run_until_rest(0.01)
    watching.join(10)
    assert "last" in found, (r.state.lifecycle, r.state.pause)
    last = found["last"]
    assert (last.get("state"), last.get("probe_dir")) == ("between", str(tmp_path / "history")), last
    assert found["later"] == last
    assert found["watch"][1:] == (last["at"], tmp_path / "history"), found["watch"]
    assert beat_of(heartbeat)["state"] == "idle"


@pytest.mark.req("HANG.between")
def test_the_verdict_s_commit_the_co_writes_and_the_read_before_a_resumed_test_are_watched_as_between(tmp_path):
    hardware = FakeHardware()
    waiting, go = threading.Event(), threading.Event()

    class EndsIdle(FakeRunner):
        def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
            if not go.is_set():
                waiting.set()
                go.wait(30)
                r.heartbeat.ended(r.clock())
            return super().run(spec, stop, sink)

    class AfterTheTopCheck(queue.Queue):
        def get(self, block=True, timeout=None):
            if waiting.is_set() and not go.is_set():
                go.set()
                return super().get(timeout=10)
            return super().get(block, timeout)

    r, heartbeat, now = beating_rig(tmp_path, hardware, EndsIdle(hardware))
    r.inbox = AfterTheTopCheck()
    seen: list[tuple[str, str | None]] = []
    stepping = [False]

    def watched(name: str, call):
        def recorded(*args, **kwargs):
            if stepping[0]:
                seen.append((name, beat_of(heartbeat).get("state")))
            return call(*args, **kwargs)

        return recorded

    r.store.test_returned = watched("verdict", r.store.test_returned)
    r.store.save = watched("commit", r.store.save)
    hardware.write_co = watched("co write", hardware.write_co)
    hardware.harvest = watched("kernel read", hardware.harvest)

    def stepped(steps: int) -> None:
        stepping[0] = True
        for _ in range(steps):
            now[0] += 1.0
            r.step(0.001)
        stepping[0] = False

    r.start(config(), 1, 7, True)
    stepped(8)
    assert go.is_set()
    r.signals.append(PauseRequest())
    r.run_until_rest(0.001)
    r.resume(1)
    stepped(4)
    assert {name for name, _ in seen} == {"verdict", "commit", "co write", "kernel read"}
    assert {state for _, state in seen} == {"between"}, seen


@pytest.mark.req("HANG.between")
def test_a_step_that_raises_out_of_the_driver_s_loop_leaves_the_beat_idle(tmp_path):
    hardware = FakeHardware()
    r, heartbeat, now = beating_rig(tmp_path, hardware, FakeRunner(hardware))
    r.start(config(), 1, 7, True)
    states: list[str | None] = []

    def broken(*args, **kwargs):
        states.append(beat_of(heartbeat).get("state"))
        raise RuntimeError("the store broke")

    r.store.save = broken
    with pytest.raises(RuntimeError, match="the store broke"):
        r.run_until_rest(0.001)
    assert states == ["between"]
    assert beat_of(heartbeat)["state"] == "idle"


@pytest.mark.req("HANG.between")
def test_a_test_still_running_keeps_its_own_beat_through_the_driver_s_steps_and_its_rest_and_its_end_says_idle(
    tmp_path,
):
    hardware = FakeHardware()
    released = threading.Event()

    class Beating(FakeRunner):
        def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
            r.heartbeat.running("a test that does not stop", 0.0)
            released.wait(30)
            r.heartbeat.ended(0.0)
            return Report(Group.PASS, 1.0)

    r, heartbeat, now = beating_rig(tmp_path, hardware, Beating(hardware))
    r.start(config(stop_bound_s=1.0), 1, 7, True)
    r.step(0.001)
    try:
        assert within(5.0, lambda: beat_of(heartbeat).get("state") == "running")
        for _ in range(12):
            now[0] += 1.0
            r.step(0.001)
        assert beat_of(heartbeat)["state"] == "running"
        r.inbox.put(InternalError("the runner broke"))
        assert r.run_until_rest(0.001).lifecycle is Lifecycle.PAUSED and r.worker is not None
        assert beat_of(heartbeat)["state"] == "running"
    finally:
        released.set()
    assert within(5.0, lambda: beat_of(heartbeat)["state"] == "idle")


@pytest.mark.req("HANG.between")
def test_the_commit_after_a_stopped_test_is_watched_as_between_whatever_the_test_last_said(tmp_path):
    hardware = FakeHardware()
    seen: list[str | None] = []

    class Stopping(FakeRunner):
        def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
            r.heartbeat.running("a test", 0.0)
            stop.wait(30)
            r.heartbeat.ended(0.0)
            return Report(Group.PASS, 1.0)

    r, heartbeat, now = beating_rig(tmp_path, hardware, Stopping(hardware))
    save = r.store.save

    def recorded(*args, **kwargs):
        seen.append(beat_of(heartbeat).get("state"))
        return save(*args, **kwargs)

    r.start(config(), 1, 7, True)
    r.step(0.001)
    assert within(5.0, lambda: beat_of(heartbeat).get("state") == "running")
    r.store.save = recorded
    r.signals.append(Close())
    for _ in range(50):
        if r.at_rest():
            break
        r.step(0.001)
    assert r.at_rest() and r.worker is None
    assert seen == ["between"]


def topology():
    cores = {core: SimpleNamespace(logical_cpus=cpus, ccd=dict(DOMAINS)[core]) for core, cpus in CPUS}
    return SimpleNamespace(cores=cores, model_name="cpu-1", is_x3d=False, vcache_ccd=None)


@pytest.mark.req("VERDICT.27b")
def test_an_unexpected_error_in_the_driver_closes_the_session_with_every_core_at_baseline(tmp_path):
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
            "--write",
            "--acknowledge",
        ]
    )
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, db=database(tmp_path / "broken.db"))
    r.say = lines.append

    def broken(wait=1.0):
        while r.worker is None:
            r.step(0.01)
        assert any(value != 0 for value in hardware.co.values())
        raise RuntimeError("the run loop broke")

    r.run_until_rest = broken
    with pytest.raises(RuntimeError, match="the run loop broke"):
        live_hw.run(args, r, lines.append)
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert any(line.startswith("the driver stopped on an unexpected error") for line in lines)
    assert r.heartbeat.said.get("state") == "idle"
    reopened = database(tmp_path / "broken.db")
    assert [session.status for session in reopened.list_machine_sessions(limit=1)] == ["paused"]


@pytest.mark.req("HANG.between")
def test_the_close_after_an_error_between_tests_runs_watched_and_ends_idle(tmp_path):
    hardware = FakeHardware()
    r, heartbeat, _now = beating_rig(tmp_path, hardware, FakeRunner(hardware))
    args = live_hw.parse(
        ["--state-dir", str(tmp_path / "s"), "--start", "--cores", "0,1,8", "--max-offset", "-8", "--write"]
    )
    closing = []
    during_close = []
    write_co = hardware.write_co

    def recording(core, value):
        if closing:
            during_close.append(beat_of(heartbeat).get("state"))
        return write_co(core, value)

    hardware.write_co = recording

    def broken(wait=1.0):
        with r.heartbeat.driven():
            seen = False
            while not (seen and r.worker is None):
                seen = seen or r.worker is not None
                r.step(0.01)
        closing.append(True)
        raise RuntimeError("the run loop broke between tests")

    r.run_until_rest = broken
    with pytest.raises(RuntimeError, match="between tests"):
        live_hw.run(args, r, lambda _line: None)
    assert during_close and set(during_close) == {"between"}
    assert beat_of(heartbeat)["state"] == "idle"


@pytest.mark.req("O19.stretch")
@pytest.mark.parametrize(
    ("cores", "read"), [(CPUS, False), (tuple((core, (core, core + 16)) for core in range(16)), True)]
)
def test_the_rig_reads_the_pm_core_arrays_only_for_the_measured_core_layout(tmp_path, monkeypatch, cores, read):
    from corecycler.smu.pmtable import PMTableData

    table = PMTableData(pm_table_version=0x00620205, raw_floats=[1.0] * 397)
    monkeypatch.setattr(live_hw, "PMTableReader", lambda: SimpleNamespace(read=lambda: table))
    monkeypatch.setattr(live_hw, "Sensors", lambda: SimpleNamespace(read=lambda: SensorRead(tctl=50.0)))
    layout = {core: SimpleNamespace(logical_cpus=cpus, ccd=0) for core, cpus in cores}
    topo = SimpleNamespace(cores=layout, model_name="cpu-1", is_x3d=False, vcache_ccd=None)
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--cores", "0,1", "--max-offset", "-8"])
    kagari = SimpleNamespace(binary_problem=lambda name: None)
    built = live_hw.build_rig(
        args, topo, FakeSmu(), lambda: kagari, lambda line: None, database(tmp_path / "pm.db"), IDENTITY, 300
    )
    assert bool(built.hardware.sensors().cores) is read


@pytest.mark.req("K9.capability")
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
    kagari = SimpleNamespace(binary_problem=lambda name: f"the y-cruncher binary {name!r} is not in /nowhere")
    built = live_hw.build_rig(args, topology(), FakeSmu(), lambda: kagari, lines.append, db, IDENTITY, 7)
    assert built.hardware.write is True and built.cpus_of == CPUS and built.domain_of == DOMAINS
    assert built.check_interval_s == 7
    assert built.hardware.vcache == "none"
    assert built.two_t_problem == "the y-cruncher binary '19-ZN2 ~ Kagari' is not in /nowhere"
    assert built.co_range == (-50, 10)
    assert built.store.db is db
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.say = lines.append
    r.runner = FakeRunner(hardware)
    args.state_dir = tmp_path / "state"
    assert live_hw.run(args, r, lines.append) == live_hw.EXIT_COMPLETED
    assert any(line.startswith("started: session 1,") for line in lines)
    assert "rest: completed" in lines
    resumed = live_hw.parse(["--state-dir", str(tmp_path / "state"), "--session", "1", "--resume"])
    again = rig(tmp_path, hardware, db=r.store.db)
    again.say = lines.append
    assert live_hw.run(resumed, again, lines.append) == live_hw.EXIT_REFUSED
    assert lines[-1] == "refused: the session is completed: use Validate Profile"


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


@pytest.mark.parametrize("action", ["--resume", "--validate", "--continue", "--restart"])
def test_a_resume_or_a_validation_names_its_session(capsys, action):
    with pytest.raises(SystemExit):
        live_hw.parse(["--state-dir", "s", action])
    assert f"{action} needs --session" in capsys.readouterr().err


def paused_then_moved_to_a_new_setup(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    r.post(PauseRequest())
    r.run_until_rest(0.01)
    hardware.setup_now = (("bios", "B"),)
    return hardware, rig(tmp_path, hardware, db=r.store.db)


@pytest.mark.req("L24.l24b.cli")
@pytest.mark.parametrize(("flag", "origin"), [("--continue", "continue"), ("--restart", "restart")])
def test_the_command_line_carries_a_session_onto_a_new_setup(tmp_path, flag, origin):
    hardware, again = paused_then_moved_to_a_new_setup(tmp_path)
    said = []
    again.say = said.append
    args = live_hw.parse(["--state-dir", str(tmp_path / "state"), "--session", "1", flag, "--acknowledge"])
    assert live_hw.run(args, again, said.append) == 0
    sessions = {r["id"]: r for r in rows(again, "SELECT id, status, parent_session_id, origin FROM tuner_sessions")}
    assert sessions[1]["status"] == "superseded"
    assert (sessions[2]["parent_session_id"], sessions[2]["origin"]) == (1, origin)
    assert rows(again, "SELECT message FROM tuner_events WHERE session_id=1 AND severity='superseded'") == [
        {"message": f"superseded by session 2 ({origin})"}
    ]
    assert again.state.session_id == 2
    assert rows(again, "SELECT DISTINCT session_id FROM tuner_tests WHERE started_at IS NOT NULL ORDER BY 1")[-1] == {
        "session_id": 2
    }


def test_a_resume_that_pauses_exits_paused(tmp_path):
    hardware, again = paused_then_moved_to_a_new_setup(tmp_path)
    said = []
    again.say = said.append
    args = live_hw.parse(["--state-dir", str(tmp_path / "state"), "--session", "1", "--resume"])
    assert live_hw.run(args, again, said.append) == live_hw.EXIT_PAUSED
    assert any(line.startswith("rest: paused (setup_changed") for line in said)


@pytest.mark.parametrize(
    ("control", "code", "rest"),
    [(Close(), live_hw.EXIT_SIGNAL, "idle"), (live.Abort(), live_hw.EXIT_ABORTED, "aborted")],
)
@pytest.mark.req("E6.exit")
def test_a_signal_s_outcome_is_the_driver_s_exit_code(tmp_path, control, code, rest):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.runner = FakeRunner(hardware)
    said = []
    r.say = said.append
    r.signals.append(control)
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--cores", "0,1,8", "--acknowledge"])
    assert live_hw.run(args, r, said.append) == code
    assert f"rest: {rest}" in said


def test_a_restart_refused_at_start_s_gates_is_a_refusal(tmp_path):
    hardware, again = paused_then_moved_to_a_new_setup(tmp_path)
    said = []
    again.say = said.append
    resume = again.resume

    def resume_then_lose_the_kernel_log(session_id, reengage, carry):
        resume(session_id, reengage, carry)
        hardware.refusal = "the kernel log /dev/kmsg cannot be read"

    again.resume = resume_then_lose_the_kernel_log
    args = live_hw.parse(["--state-dir", str(tmp_path / "state"), "--session", "1", "--restart"])
    assert live_hw.run(args, again, said.append) == live_hw.EXIT_REFUSED
    assert said[-1] == "refused: the kernel log /dev/kmsg cannot be read"
    assert rows(again, "SELECT id, status FROM tuner_sessions") == [{"id": 1, "status": "paused"}]


@pytest.mark.req("L24.l24b.supersede")
def test_a_continued_session_inherits_its_parent_s_proven_values_and_a_restart_does_not(tmp_path):
    for choice in ("continue", "restart"):
        folder = tmp_path / choice
        hardware, again = paused_then_moved_to_a_new_setup(folder)
        proven = again.store.db.journal_proven_values(1, -1)
        again.resume(1)
        again.run_until_rest(0.01)
        if choice == "continue":
            again.continue_on_setup()
        else:
            again.restart_on_setup(True)
        inherited = rows(
            again,
            "SELECT core_id, value, boot_id, mono_ns, confirmed, survived FROM tuner_co_journal"
            " WHERE session_id=2 AND boot_id='' ORDER BY core_id",
        )
        if choice == "continue":
            assert proven
            assert inherited == [
                {"core_id": core, "value": value, "boot_id": "", "mono_ns": None, "confirmed": 1, "survived": 1}
                for core, value in sorted(proven.items())
            ]
        else:
            assert inherited == []


@pytest.mark.req("L24.G11")
def test_a_supersede_that_cannot_be_written_pauses_as_database_and_leaves_the_parent_paused(tmp_path, monkeypatch):
    hardware, again = paused_then_moved_to_a_new_setup(tmp_path)
    again.resume(1)
    again.run_until_rest(0.01)
    monkeypatch.setattr(again.store, "supersede", broken)
    again.continue_on_setup()
    final = again.run_until_rest(0.01)
    assert final.pause.reason is PauseReason.DATABASE
    assert "session 1 could not be superseded" in final.pause.detail
    assert rows(again, "SELECT status FROM tuner_sessions WHERE id=1") == [{"status": "paused"}]
    assert rows(again, "SELECT id FROM tuner_sessions WHERE id=2") == []


KERNEL_WAS = (("bios", "A"), ("kernel", "7.2.8 build a1"))
KERNEL_NOW = (("bios", "A"), ("kernel", "7.2.11 build b2"))


def paused_by_the_owner_on_the_old_kernel(tmp_path) -> tuple[FakeHardware, LiveRig]:
    hardware = FakeHardware(setup_now=KERNEL_WAS)
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    r.post(PauseRequest())
    r.run_until_rest(0.01)
    return hardware, r


def driven(tmp_path, flag: str, again: LiveRig, *extra: str) -> int:
    args = live_hw.parse(
        ["--state-dir", str(tmp_path / "state"), "--session", "1", flag, "--write", "--acknowledge", *extra]
    )
    return live_hw.run(args, again, lambda line: None)


def lineage(r: LiveRig) -> list:
    return rows(r, "SELECT id, status, parent_session_id, origin FROM tuner_sessions ORDER BY id")


@pytest.mark.req("KERNEL.continue")
@pytest.mark.parametrize("flag", ["--resume", "--continue"])
def test_a_resume_that_finds_only_the_kernel_changed_carries_the_session_into_a_new_one_by_itself(tmp_path, flag):
    hardware, r = paused_by_the_owner_on_the_old_kernel(tmp_path)
    hardware.setup_now = KERNEL_NOW
    again = rig(tmp_path, hardware, db=r.store.db)
    assert driven(tmp_path, flag, again) == live_hw.EXIT_COMPLETED
    assert lineage(again) == [
        {"id": 1, "status": "superseded", "parent_session_id": None, "origin": None},
        {"id": 2, "status": "completed", "parent_session_id": 1, "origin": "continue"},
    ]
    said = [e["message"] for e in again.store.db.get_tuner_events(2) if e["severity"] == "continued"]
    assert len(said) == 1
    assert said[0].startswith(
        "the kernel changed (7.2.8 build a1 to 7.2.11 build b2): session 2 continues session 1 under it by itself; "
    )
    assert "every core is tested alone on the new setup before the session completes" in said[0]
    assert dict(live.read_stored(again.store.db, 2)[0].setup) == dict(KERNEL_NOW)


@pytest.mark.req("KERNEL.boot")
def test_the_boot_resume_attributes_the_crash_first_then_carries_the_session_onto_the_new_kernel(tmp_path, monkeypatch):
    from corecycler import cli

    hardware = FakeHardware(setup_now=KERNEL_WAS)
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    in_test = next(c for c in r.state.cores if c.in_test)
    hardware.boot, hardware.co, hardware.end_problem = "boot-b", {0: 0, 1: 0, 8: 0}, "journalctl -b exited 1"
    hardware.setup_now = KERNEL_NOW
    again = rig(tmp_path, hardware, db=r.store.db)
    asked = []

    def boot_unit_drive(argv):
        asked.append(argv)
        return live_hw.run(live_hw.parse(["--state-dir", str(tmp_path / "state"), *argv]), again, lambda line: None)

    monkeypatch.setattr(cli, "drive", boot_unit_drive)
    assert cli.cmd_resume_interrupted([], r.store.db) == live_hw.EXIT_COMPLETED
    assert asked == [["--resume", "--session", "1", "--write"]]
    parent = live.read_stored(again.store.db, 1)[0]
    assert parent.status == "superseded"
    assert next(c for c in parent.cores if c.core == in_test.core).crash_count == 1
    kinds = [e["severity"] for e in again.store.db.get_tuner_events(2)]
    assert kinds.index("boot_end_unread") < kinds.index("continued")
    assert lineage(again)[1] == {"id": 2, "status": "completed", "parent_session_id": 1, "origin": "continue"}


@pytest.mark.req("KERNEL.choice")
@pytest.mark.parametrize(
    ("flag", "code", "sessions"),
    [
        (
            "--restart",
            live_hw.EXIT_COMPLETED,
            [
                {"id": 1, "status": "superseded", "parent_session_id": None, "origin": None},
                {"id": 2, "status": "completed", "parent_session_id": 1, "origin": "restart"},
            ],
        ),
        ("--abort", live_hw.EXIT_ABORTED, [{"id": 1, "status": "aborted", "parent_session_id": None, "origin": None}]),
    ],
)
def test_a_restart_or_an_abort_after_only_the_kernel_changed_does_what_it_names_and_never_continues(
    tmp_path, flag, code, sessions
):
    hardware, r = paused_by_the_owner_on_the_old_kernel(tmp_path)
    hardware.setup_now = KERNEL_NOW
    again = rig(tmp_path, hardware, db=r.store.db)
    assert driven(tmp_path, flag, again) == code
    assert lineage(again) == sessions
    continued = rows(again, "SELECT id FROM tuner_events WHERE severity='continued'")
    assert continued == []


@pytest.mark.req("KERNEL.choice")
def test_a_rig_resume_that_asks_for_no_carry_pauses_on_a_kernel_only_change(tmp_path):
    hardware, r = paused_by_the_owner_on_the_old_kernel(tmp_path)
    hardware.setup_now = KERNEL_NOW
    again = rig(tmp_path, hardware, db=r.store.db)
    again.resume(1)
    final = again.run_until_rest(0.01)
    assert (final.session_id, final.pause.reason) == (1, PauseReason.SETUP_CHANGED)
    assert "kernel was 7.2.8 build a1, is now 7.2.11 build b2" in final.pause.detail
    assert lineage(again) == [{"id": 1, "status": "paused", "parent_session_id": None, "origin": None}]


def stored_by_f453db4(r: LiveRig, session_id: int) -> None:
    r.store.db._execute_raw(
        "UPDATE tuner_sessions SET machine_json = json_remove(machine_json, '$.flow.carried') WHERE id = ?",
        (session_id,),
    )
    assert "carried" not in json.loads(r.store.db.load_machine_session(session_id)[0])["flow"]


def begun_by(tmp_path, began: str) -> tuple[FakeHardware, LiveRig, int]:
    hardware, again = paused_then_moved_to_a_new_setup(tmp_path)
    if began == "start":
        return hardware, again, 1
    again.resume(1)
    again.run_until_rest(0.01)
    if began == "continue":
        again.continue_on_setup()
    else:
        again.restart_on_setup(True)
    again.post(Close())
    return hardware, again, 2


@pytest.mark.req("KERNEL.old")
@pytest.mark.parametrize(("began", "carried"), [("start", False), ("restart", False), ("continue", True)])
def test_a_session_stored_without_the_mark_is_carried_exactly_when_a_continue_created_it(tmp_path, began, carried):
    _, again, session = begun_by(tmp_path, began)
    stored_by_f453db4(again, session)
    assert live.read_stored(again.store.db, session)[0].flow.carried is carried


@pytest.mark.req("KERNEL.old")
def test_a_session_a_continue_created_and_f453db4_stored_runs_the_final_pass_before_it_completes(tmp_path):
    hardware, again, session = begun_by(tmp_path, "continue")
    stored_by_f453db4(again, session)
    later = rig(tmp_path, hardware, db=again.store.db)
    later.resume(session)
    assert later.run_until_rest(0.01).lifecycle is Lifecycle.COMPLETED
    ran = rows(later, "SELECT stage, core FROM tuner_tests WHERE session_id=? ORDER BY id DESC", (session,))
    final_pass = []
    for row in ran:
        if row["stage"] != "live1":
            break
        final_pass.append(row["core"])
    assert sorted(set(final_pass)) == [0, 1, 8]


@pytest.mark.req("KERNEL.owner")
def test_a_session_the_owner_paused_waits_through_a_boot_and_his_resume_carries_it_onto_the_new_kernel(
    tmp_path, monkeypatch, capsys
):
    from corecycler import cli

    hardware, r = paused_by_the_owner_on_the_old_kernel(tmp_path)
    hardware.boot, hardware.clean, hardware.setup_now = "boot-b", True, KERNEL_NOW
    asked = []
    monkeypatch.setattr(cli, "drive", asked.append)
    assert cli.cmd_resume_interrupted([], r.store.db) == cli.EXIT_COMPLETED
    assert asked == []
    assert capsys.readouterr().out == "corecycler resume: no interrupted session\n"
    assert lineage(r) == [{"id": 1, "status": "paused", "parent_session_id": None, "origin": None}]
    again = rig(tmp_path, hardware, db=r.store.db)
    assert driven(tmp_path, "--resume", again) == live_hw.EXIT_COMPLETED
    assert lineage(again)[1] == {"id": 2, "status": "completed", "parent_session_id": 1, "origin": "continue"}


UNREAD_BASE_CO = "session 1 could not be read: KeyError('base_co')"


NO_BASE_CO = "json_remove(machine_json, '$.base_co')"


def store_as(r: LiveRig, machine_json: str) -> None:
    r.store.db._execute_raw(f"UPDATE tuner_sessions SET machine_json = {machine_json} WHERE id = 1")


def drop_base_co(r: LiveRig) -> None:
    store_as(r, NO_BASE_CO)


def as_written(r: LiveRig) -> tuple:
    return rows(r, "SELECT * FROM tuner_sessions"), rows(r, "SELECT total_changes() AS rows"), dict(r.hardware.co)


@pytest.mark.req("UNREAD.command")
@pytest.mark.parametrize(
    ("machine_json", "error"),
    [
        (NO_BASE_CO, "KeyError('base_co')"),
        ("'{'", "JSONDecodeError("),
        ("json_set(machine_json, '$.flow.allcore', json('[1]'))", "AttributeError("),
    ],
    ids=["no-base-co", "malformed-json", "allcore-a-list"],
)
@pytest.mark.parametrize("flag", ["--resume", "--continue", "--restart", "--validate"])
def test_a_command_on_a_session_whose_stored_state_cannot_be_read_is_refused_by_name_and_writes_nothing(
    tmp_path, flag, machine_json, error
):
    hardware, r = paused_by_the_owner_on_the_old_kernel(tmp_path)
    again = rig(tmp_path, hardware, db=r.store.db)
    said = []
    again.say = said.append
    store_as(again, machine_json)
    before = as_written(again)
    assert driven(tmp_path, flag, again) == live_hw.EXIT_REFUSED
    assert len(said) == 1 and said[0].startswith(f"refused: session 1 could not be read: {error}")
    assert as_written(again) == before


@pytest.mark.req("UNREAD.boot")
def test_the_boot_resume_of_a_session_stored_without_its_base_co_is_refused_by_name_and_writes_nothing(
    tmp_path, monkeypatch
):
    from corecycler import cli

    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    hardware.boot, hardware.co = "boot-b", {0: 0, 1: 0, 8: 0}
    again = rig(tmp_path, hardware, db=r.store.db)
    said = []
    again.say = said.append
    drop_base_co(again)
    before = as_written(again)
    asked = []

    def boot_unit_drive(argv):
        asked.append(argv)
        return live_hw.run(live_hw.parse(["--state-dir", str(tmp_path / "state"), *argv]), again, lambda line: None)

    monkeypatch.setattr(cli, "drive", boot_unit_drive)
    assert cli.cmd_resume_interrupted([], again.store.db) == cli.EXIT_REFUSED
    assert asked == [["--resume", "--session", "1", "--write"]]
    assert said == [f"refused: {UNREAD_BASE_CO}"]
    assert as_written(again) == before


@pytest.mark.req("UNREAD.readback")
def test_a_session_that_turns_unreadable_before_the_resume_reads_it_back_pauses_as_database_naming_it(
    tmp_path, monkeypatch
):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    open_test = r.test_row[1]
    for n in range(telemetry.TAIL_GROUP):
        r.store.sample(open_test, taken(0, n), durable=True)
    hardware.boot, hardware.co = "boot-b", {0: -4, 1: -4, 8: -4}
    again = rig(tmp_path, hardware, db=r.store.db)
    load = again.store.load
    reads = []

    def unreadable_at_the_second_read(session_id):
        reads.append(session_id)
        if len(reads) > 1:
            drop_base_co(again)
        return load(session_id)

    monkeypatch.setattr(again.store, "load", unreadable_at_the_second_read)
    again.resume(1)
    assert reads == [1, 1]
    assert (again.state.pause.reason, again.state.pause.detail) == (PauseReason.DATABASE, UNREAD_BASE_CO)
    tail = (
        "SELECT (SELECT count(*) FROM tuner_samples WHERE test_id = ?) AS tail,"
        " (SELECT count(*) FROM tuner_sample_blocks WHERE test_id = ?) AS blocks"
    )
    assert rows(again, tail, (open_test, open_test)) == [{"tail": telemetry.TAIL_GROUP, "blocks": 0}]
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert again.store.marker_boot(1) == ("boot-b", True)
    assert rows(again, "SELECT severity, message FROM tuner_events WHERE boot_id = 'boot-b'") == [
        {"severity": "database_failed", "message": f"{UNREAD_BASE_CO}; every owned core is sent back to stock"}
    ]


def test_the_command_line_validates_a_completed_session(tmp_path):
    r, said = completed_then_rebooted(tmp_path, clean=True)
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--session", "1", "--validate"])
    assert live_hw.run(args, r, said.append) == 0
    assert any(line.startswith("validating: session 1") for line in said)
    assert "rest: completed" in said


@pytest.mark.req("T22.F12")
def test_an_aborted_validation_exits_and_notifies_as_aborted_never_as_a_confirmed_profile(tmp_path, monkeypatch):
    from corecycler.cli import EXIT_MEANINGS

    r, said = completed_then_rebooted(tmp_path, clean=True)
    r.signals.append(live.Abort())
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--session", "1", "--validate"])
    code = live_hw.run(args, r, said.append)
    assert "rest: completed" in said
    assert code != live_hw.EXIT_COMPLETED
    assert EXIT_MEANINGS[code] == "validation aborted; the session stays completed"
    monkeypatch.setattr(live_hw, "load_settings", lambda: SimpleNamespace(notify_on_completion=True))
    sent = []
    monkeypatch.setattr("corecycler.notify.desktop_notify", lambda title, body, urgency: sent.append((title, body)))
    live_hw.notify_outcome(code)
    [(title, body)] = sent
    assert title == "Validation aborted"
    assert body.startswith("The validation stopped before it ended; the session stays completed")


@pytest.mark.req("ORDER.seed")
@pytest.mark.parametrize(
    ("given", "seed", "says"),
    [((), 424242, "order seed 424242, drawn at Start"), (("--seed", "7"), 7, "order seed 7, given")],
)
def test_a_start_draws_its_order_seed_unless_one_is_given(tmp_path, monkeypatch, given, seed, says):
    monkeypatch.setattr(live_hw.secrets, "randbelow", lambda bound: 424242)
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--cores", "0,1,8", "--write", *given])
    started, said = [], []
    r = rig(tmp_path)
    r.start = lambda config, session, chosen, acknowledged: started.append(chosen)
    r.run_until_rest = lambda wait=1.0: r.state
    live_hw.run(args, r, said.append)
    assert started == [seed]
    assert says in said


@pytest.mark.req("T22.F21")
def test_a_refused_start_names_no_session_and_no_seed(tmp_path):
    db = database(tmp_path / "held.db")
    first = rig(tmp_path, FakeHardware(), db=db)
    first.start(config(), 1, 7, True)
    first.post(PauseRequest())
    said = []
    second = rig(tmp_path, FakeHardware(), db=db)
    second.say = said.append
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--cores", "0,1,8", "--write", "--seed", "7"])
    assert live_hw.run(args, second, said.append) == live_hw.EXIT_REFUSED
    assert said == ["refused: session 1 is paused: resume it or abort it before starting another"]


@pytest.mark.req("VERDICT.27")
@pytest.mark.req("VERDICT.27b")
def test_signals_queue_the_owner_s_controls(tmp_path, monkeypatch):
    installed = {}
    monkeypatch.setattr(live_hw.signal, "signal", lambda number, handler: installed.__setitem__(number, handler))
    r = rig(tmp_path)
    live_hw.install_signals(r)
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2):
        installed[number](number, None)
    assert [type(event).__name__ for event in r.signals] == ["Close", "Close", "Close", "PauseRequest", "Abort"]


@pytest.fixture
def driver_home(tmp_path, monkeypatch):
    import corecycler.config.paths as paths
    import corecycler.engine.backends
    import corecycler.engine.topology

    monkeypatch.setattr(paths, "user_home", lambda: tmp_path / "home")
    monkeypatch.setattr(corecycler.engine.topology, "detect_topology", topology)
    monkeypatch.setattr(live_hw, "build_smu", lambda topo: FakeSmu())
    monkeypatch.setattr(live_hw, "read_identity", lambda db, smu, topo: IDENTITY)
    monkeypatch.setattr(corecycler.engine.backends, "load_all", lambda: None)
    monkeypatch.setattr(live_hw, "install_signals", lambda r: None)
    monkeypatch.setattr(live_hw.SmuHardware, "power", lambda self: UNCAPPED)
    monkeypatch.setattr(live_hw.SmuHardware, "preflight", lambda self: (None, ()))
    notified = []
    monkeypatch.setattr(live_hw, "notify_outcome", notified.append)
    return tmp_path / "home" / ".local" / "share" / "corecycler", notified


@pytest.mark.req("L01.3", "TM.A1.co")
def test_main_refuses_without_an_smu_and_runs_with_one(tmp_path, driver_home, monkeypatch, capsys):
    _, notified = driver_home
    monkeypatch.setattr(live_hw, "build_smu", lambda topo: None)
    argv = ["--state-dir", str(tmp_path / "s"), "--db", str(tmp_path / "h.db"), "--session", "1", "--resume"]
    assert live_hw.main(argv) == live_hw.EXIT_REFUSED
    assert "unavailable" in capsys.readouterr().err
    monkeypatch.setattr(live_hw, "build_smu", lambda topo: FakeSmu())
    assert live_hw.main(argv) == live_hw.EXIT_REFUSED
    assert "refused: session 1 is not stored" in capsys.readouterr().out
    assert notified == [live_hw.EXIT_REFUSED, live_hw.EXIT_REFUSED]


@pytest.mark.req("E6.lock", "TM.I_M9", "WRITER.owned")
def test_one_co_writer_at_a_time_on_a_machine(tmp_path, driver_home, writer_dir, capsys):
    from corecycler.smu.writer_lock import co_writer, running_driver

    _, notified = driver_home
    argv = ["--state-dir", str(tmp_path / "s"), "--db", str(tmp_path / "other.db"), "--session", "1", "--resume"]
    with co_writer():
        assert live_hw.main(argv) == live_hw.EXIT_LOCKED
    assert f"live: another CO writer holds {writer_dir / 'tuner.lock'}" in capsys.readouterr().err
    assert notified == []
    seen = []
    drive = live_hw._drive
    live_hw._drive = lambda args: seen.append(running_driver()) or drive(args)
    try:
        assert live_hw.main(argv) == live_hw.EXIT_REFUSED
    finally:
        live_hw._drive = drive
    assert seen == [os.getpid()]
    assert running_driver() is None


DRIVEN = {
    "start": ["--start", "--cores", "0,1,8"],
    "resume": ["--session", "1", "--resume"],
    "validate": ["--session", "1", "--validate"],
    "continue": ["--session", "1", "--continue"],
    "restart": ["--session", "1", "--restart"],
}


def driver_argv(tmp_path: Path, command: list[str]) -> list[str]:
    return ["--state-dir", str(tmp_path / "s"), "--db", str(tmp_path / "h.db"), "--write", *command]


@pytest.mark.req("HEAVY.driver")
@pytest.mark.parametrize("command", DRIVEN.values(), ids=DRIVEN.keys())
def test_the_driver_refuses_to_drive_while_another_tool_holds_the_shared_lock_and_writes_nothing(
    tmp_path, driver_home, heavy_lock, monkeypatch, capsys, command
):
    _, notified = driver_home
    built, ran = [], []
    monkeypatch.setattr(live_hw, "build_smu", lambda topo: built.append(topo) or FakeSmu())
    monkeypatch.setattr(live_hw, "run", lambda args, rig, say: ran.append(args) or live_hw.EXIT_COMPLETED)
    with held_by_another_tool(heavy_lock, f"{GPU_LINE}\n".encode()):
        assert live_hw.main(driver_argv(tmp_path, command)) == live_hw.EXIT_REFUSED
    assert capsys.readouterr().err == f"live: another heavy test holds the shared lock: {GPU_LINE}\n"
    assert (built, ran, notified) == ([], [], [])
    assert not (tmp_path / "h.db").exists() and not (tmp_path / "s").exists()


@pytest.mark.req("HEAVY.driver")
def test_the_boot_resume_is_refused_while_another_tool_holds_the_shared_lock_with_the_code_its_unit_retries(
    tmp_path, driver_home, heavy_lock, monkeypatch, capsys
):
    from corecycler import cli

    listed = database(tmp_path / "listed.db")
    save_machine_session(listed, tmp_path, "running")
    ran = []
    monkeypatch.setattr(live_hw, "run", lambda args, rig, say: ran.append(args) or live_hw.EXIT_COMPLETED)
    monkeypatch.setattr(cli, "drive", lambda argv: live_hw.main(driver_argv(tmp_path, argv)))
    with held_by_another_tool(heavy_lock, f"{GPU_LINE}\n".encode()):
        assert cli.cmd_resume_interrupted([], listed) == cli.EXIT_REFUSED
    assert capsys.readouterr().err == f"live: another heavy test holds the shared lock: {GPU_LINE}\n"
    assert ran == [] and not (tmp_path / "h.db").exists()


@pytest.mark.req("HEAVY.driver")
@pytest.mark.parametrize("rest", ["paused", "closed", "an error"])
def test_the_driver_holds_the_shared_lock_while_it_drives_and_lets_go_when_it_comes_to_rest(
    tmp_path, driver_home, heavy_lock, monkeypatch, rest
):
    ends = {"paused": live_hw.EXIT_PAUSED, "closed": live_hw.EXIT_SIGNAL}
    seen = []

    def session(args, rig, say):
        seen.append(holder_seen_by_another_tool(heavy_lock))
        if rest == "an error":
            raise RuntimeError("the driver broke")
        return ends[rest]

    monkeypatch.setattr(live_hw, "run", session)
    argv = driver_argv(tmp_path, DRIVEN["resume"])
    if rest == "an error":
        with pytest.raises(RuntimeError, match="the driver broke"):
            live_hw.main(argv)
    else:
        assert live_hw.main(argv) == ends[rest]
    [line] = seen
    assert line is not None and said_by(line)[:2] == ("corecycler-tuner", os.getpid())
    assert holder_seen_by_another_tool(heavy_lock) is None


@pytest.mark.req("HEAVY.driver")
def test_an_abort_runs_no_test_and_goes_ahead_while_another_tool_holds_the_shared_lock(
    tmp_path, driver_home, heavy_lock, monkeypatch
):
    ran = []
    monkeypatch.setattr(live_hw, "run", lambda args, rig, say: ran.append(args.abort) or live_hw.EXIT_ABORTED)
    with held_by_another_tool(heavy_lock, f"{GPU_LINE}\n".encode()):
        assert live_hw.main(driver_argv(tmp_path, ["--session", "1", "--abort"])) == live_hw.EXIT_ABORTED
    assert ran == [True]


def test_the_driver_runs_its_session_inside_a_raised_machine_check_poll(tmp_path, driver_home, monkeypatch):
    events = []

    @contextlib.contextmanager
    def poll(say):
        events.append("raised")
        yield 120
        events.append("restored")

    monkeypatch.setattr(live_hw.kernel, "fast_mce_poll", poll)
    monkeypatch.setattr(
        live_hw, "run", lambda args, rig, say: events.append(("ran", rig.check_interval_s)) or live_hw.EXIT_REFUSED
    )
    argv = ["--state-dir", str(tmp_path / "s"), "--db", str(tmp_path / "h.db"), "--session", "1", "--resume"]
    assert live_hw.main(argv) == live_hw.EXIT_REFUSED
    assert events == ["raised", ("ran", 120), "restored"]


def test_a_usage_error_is_a_refusal_and_help_is_not(driver_home, capsys):
    assert live_hw.main(["--resume"]) == live_hw.EXIT_REFUSED
    assert "--resume needs --session" in capsys.readouterr().err
    assert live_hw.main(["--help"]) == live_hw.EXIT_COMPLETED


def test_the_state_directory_defaults_under_the_user_s_data(driver_home):
    data, _ = driver_home
    assert live_hw.parse(["--session", "1", "--resume"]).state_dir == data / "tuner"


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--start"], "--start needs --config FILE or --cores"),
        (["--start", "--config", "c.json", "--cores", "0"], "--config holds the whole configuration"),
        (["--start", "--config", "c.json", "--k1", "3"], "--config holds the whole configuration"),
        (["--resume", "--session", "1", "--config", "c.json"], "go with --start only"),
        (["--validate", "--session", "1", "--max-offset", "-9"], "go with --start only"),
    ],
)
@pytest.mark.req("E6.config")
def test_a_start_has_exactly_one_configuration_source(argv, problem, capsys):
    with pytest.raises(SystemExit):
        live_hw.parse(["--state-dir", "s", *argv])
    assert problem in capsys.readouterr().err


def test_the_flags_build_the_spec_s_config_with_only_what_was_given():
    args = live_hw.parse(["--state-dir", "s", "--start", "--cores", "0,8", "--k1", "3"])
    built = live_hw.start_config(args, DOMAINS)
    assert built == MachineConfig(Mode.CO, built.method, (0, 8), k_1t=3)
    assert built.max_offset == MachineConfig(Mode.CO, built.method, (0, 8)).max_offset


def test_a_config_file_starts_the_session_it_describes(tmp_path):
    wanted = config(max_offset=-9, k_1t=1, k_2t=1, wait_between_cores_s=0)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(live.config_to_json(wanted)))
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.runner = FakeRunner(hardware)
    said = []
    r.say = said.append
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--config", str(path), "--acknowledge"])
    live_hw.run(args, r, said.append)
    assert any(line.startswith("started: session 1,") for line in said)
    (stored,) = rows(r, "SELECT config_json FROM tuner_sessions WHERE id=1")
    assert live.config_from_json(json.loads(stored["config_json"])) == wanted


WRONG_TYPES = [
    ("k_1t", "10"),
    ("soak", 1),
    ("coarse_step", True),
    ("thermal_limit_c", "95"),
    ("selected", ["0"]),
    ("stage_seconds", [["burst", "480"], ["warm", 480.0], ["idle", 360.0]]),
    ("starts", [["0", -3]]),
    ("starts", [[0, True]]),
]


@pytest.mark.parametrize(("name", "value"), WRONG_TYPES)
def test_a_configuration_with_a_wrong_type_is_refused_at_the_boundary(name, value):
    raw = live.config_to_json(config())
    assert live.config_from_json(raw) == config()
    with pytest.raises(ValueError, match=f"wrong types for {name}"):
        live.config_from_json({**raw, name: value})


@pytest.mark.req("T25.start")
def test_starts_survive_the_stored_configuration_and_a_configuration_stored_before_them_has_none():
    wanted = config(starts=((0, -3), (1, -5)))
    raw = live.config_to_json(wanted)
    assert live.config_from_json(json.loads(json.dumps(raw))) == wanted
    del raw["starts"]
    assert live.config_from_json(raw).starts == ()


@pytest.mark.parametrize("content", [None, "{", '{"mode": "co"}', "[]"])
def test_a_config_that_cannot_be_read_refuses_the_start(tmp_path, content):
    path = tmp_path / "c.json"
    if content is not None:
        path.write_text(content)
    said = []
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--config", str(path)])
    r = rig(tmp_path)
    assert live_hw.run(args, r, said.append) == live_hw.EXIT_REFUSED
    assert said[-1].startswith("refused: the start configuration: ")
    assert r.store.db.list_machine_sessions() == []


class TestNotifyOutcome:
    def test_unknown_code_is_silent(self, capsys):
        live_hw.notify_outcome(999)
        assert capsys.readouterr().err == ""

    def test_disabled_by_setting(self, monkeypatch, capsys):
        monkeypatch.setattr(live_hw, "load_settings", lambda: SimpleNamespace(notify_on_completion=False))
        sent = []
        monkeypatch.setattr("corecycler.notify.desktop_notify", lambda *a, **k: sent.append(a))
        live_hw.notify_outcome(live_hw.EXIT_COMPLETED)
        assert sent == []

    def test_each_outcome_sends_its_note(self, monkeypatch):
        monkeypatch.setattr(live_hw, "load_settings", lambda: SimpleNamespace(notify_on_completion=True))
        sent = []
        monkeypatch.setattr("corecycler.notify.desktop_notify", lambda title, body, urgency: sent.append(title))
        for code in (live_hw.EXIT_COMPLETED, live_hw.EXIT_PAUSED, live_hw.EXIT_QUARANTINED, live_hw.EXIT_ABORTED):
            live_hw.notify_outcome(code)
        assert sent == ["Tuning complete", "Tuning paused", "Tuning quarantined", "Tuning aborted"]

    @pytest.mark.req("E6.exit", "L18.d.6")
    def test_the_quarantine_note_says_the_machine_crashed_in_a_row_and_points_to_the_causes(self, monkeypatch):
        monkeypatch.setattr(live_hw, "load_settings", lambda: SimpleNamespace(notify_on_completion=True))
        sent = []
        monkeypatch.setattr("corecycler.notify.desktop_notify", lambda title, body, urgency: sent.append(body))
        live_hw.notify_outcome(live_hw.EXIT_QUARANTINED)
        (body,) = sent
        assert ("crashed" in body, "causes" in body, "unsafe" in body) == (True, True, False)

    def test_failure_surfaces_on_stderr(self, monkeypatch, capsys):
        def boom():
            raise RuntimeError("no settings")

        monkeypatch.setattr(live_hw, "load_settings", boom)
        live_hw.notify_outcome(live_hw.EXIT_COMPLETED)
        assert "notification failed: no settings" in capsys.readouterr().err


class TestBuildSmu:
    def test_none_on_an_unsupported_cpu(self):
        from corecycler.engine.topology import CPUTopology

        assert live_hw.build_smu(CPUTopology(model_name="Intel", family=6, model=1)) is None

    def test_none_when_the_driver_is_absent(self, monkeypatch):
        from corecycler.engine.topology import CPUTopology

        monkeypatch.setattr(live_hw.RyzenSMU, "is_available", staticmethod(lambda *a, **k: False))
        assert live_hw.build_smu(CPUTopology(family=26, model=0x44, model_name="Test 9950X")) is None

    @pytest.mark.req("TM.A3.co")
    def test_the_driver_when_available(self, monkeypatch):
        from corecycler.engine.topology import CPUTopology, PhysicalCore

        topo = CPUTopology(model_name="AMD Ryzen 9 9950X3D 16-Core Processor", family=26, model=0x44)
        topo.cores = {cid: PhysicalCore(core_id=cid, ccd=cid // 8, ccx=None, logical_cpus=(cid,)) for cid in range(16)}
        monkeypatch.setattr(live_hw.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True))
        assert live_hw.build_smu(topo) is not None


def test_the_live_module_exports_the_controls():
    assert {"Abort", "Close", "PauseRequest", "LiveRig", "Store"} <= set(live.__all__)
    assert run_to_rest


@pytest.mark.req("L23.G10", "TRUTH.REVERT_RESULT")
def test_a_revert_that_fails_is_said_and_the_core_left_unknown(tmp_path):
    said = []
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.say = said.append
    r.start(config(), 1, 7, True)
    hardware.refuse = 4
    r.run_until_rest(0.01)
    assert any(line.startswith("revert_failed: core") for line in said)
    assert None in r.resident.values()
    stored = rows(r, "SELECT message FROM tuner_events WHERE session_id=1 AND severity='revert_failed'")
    assert stored and "could not be written back" in stored[0]["message"]
    assert "refused for the test" in stored[0]["message"]


@pytest.mark.req("TRUTH.REVERT_RESULT")
def test_an_abort_never_claims_the_cores_are_at_baseline(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    hardware.refuse = 4
    r.post(live.Abort())
    aborted = rows(r, "SELECT message FROM tuner_events WHERE session_id=1 AND severity='aborted'")
    assert aborted == [{"message": "aborted on request; every owned core is sent back to its baseline"}]
    failed = rows(r, "SELECT message FROM tuner_events WHERE session_id=1 AND severity='revert_failed'")
    assert len(failed) == 1


@pytest.mark.req("TRUTH.REVERT_RESULT")
def test_a_failed_revert_that_cannot_be_recorded_is_said_and_reported_as_a_database_failure(tmp_path):
    said = []
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.say = said.append
    r.start(config(), 1, 7, True)
    record = r.store.event

    def event(session, kind, text, boot):
        if kind == "revert_failed":
            raise sqlite3.Error("disk full")
        return record(session, kind, text, boot)

    r.store.event = event
    hardware.refuse = 4
    r.post(live.Abort())
    assert any(line.startswith("revert_failed: core") for line in said)
    assert "database_failed: the failed revert could not be recorded: disk full" in said


@pytest.mark.req("K23")
def test_a_sleep_between_tests_makes_the_rig_rewrite_every_core(tmp_path):
    hardware = FakeHardware()
    slept = [0.0]
    hardware.asleep_seconds = lambda: slept[0]
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    for _ in range(10_000):
        if isinstance(r.state.slot, InFlight):
            break
        r.step(0.01)
    before = r.state.slot.token
    hardware.co = {0: -3, 1: -3, 8: -3}
    slept[0] = 600.0
    for _ in range(10_000):
        if isinstance(r.state.slot, InFlight) and r.state.slot.token != before:
            break
        r.step(0.01)
    assert isinstance(r.state.slot, InFlight)
    assert hardware.co == dict(r.state.slot.spec.vector)
    r.run_until_rest(0.01)
    assert r.state.lifecycle is Lifecycle.COMPLETED
    assert hardware.co == EDGES


def test_a_start_the_machine_s_preflight_refuses_writes_nothing_and_says_why(tmp_path):
    said = []
    r = rig(tmp_path, FakeHardware(refusal="the kernel log /dev/kmsg cannot be read"))
    r.say = said.append
    r.start(config(), 1, 7, True)
    assert said == ["refused: the kernel log /dev/kmsg cannot be read"]
    assert r.store.load(1) == (None, None) and r.hardware.co == {0: 0, 1: 0, 8: 0}


def test_preflight_warnings_become_session_events_when_it_starts(tmp_path):
    r = rig(tmp_path, FakeHardware(warnings=("ananicy-cpp is running",)))
    r.start(config(), 1, 7, True)
    events = rows(r, "SELECT message FROM tuner_events WHERE session_id=1 AND severity='warning'")
    assert events == [{"message": "ananicy-cpp is running"}]


def proc_tree(
    root: Path, processes: dict[int, tuple[str, tuple[str, ...]]], argv: dict[int, tuple[str, ...]] | None = None
) -> Path:
    for pid, (comm, targets) in processes.items():
        fd = root / str(pid) / "fd"
        fd.mkdir(parents=True)
        (root / str(pid) / "comm").write_text(comm + "\n")
        (root / str(pid) / "cmdline").write_bytes(
            b"\0".join(arg.encode() for arg in (argv or {}).get(pid, (comm,))) + b"\0"
        )
        for n, target in enumerate(targets):
            (fd / str(n)).symlink_to(target)
    (root / "self").mkdir(parents=True)
    return root


class Tool:
    def __init__(self, name, problem=None):
        self.name, self.problem = name, problem

    def is_available(self):
        return self.problem is None

    def resolution(self):
        return SimpleNamespace(problem=self.problem)


def preflight(
    tmp_path,
    monkeypatch,
    *,
    smu=None,
    temp=60.0,
    tools=(),
    usable=60_000,
    procs=None,
    scheduling=None,
    unread=(),
    sched_ext=None,
    lane=None,
    mce=None,
    load=None,
    power=UNCAPPED,
    budget=None,
):
    monkeypatch.setattr(live_hw.kernel, "kernel_log_problem", lambda: None)
    monkeypatch.setattr(live_hw.kernel, "mce_poll_problem", mce or (lambda: None))
    monkeypatch.setattr(live_hw, "lane_problem", lane or (lambda: None))
    monkeypatch.setattr(live_hw, "scheduling_problem", scheduling or (lambda: None))
    proc = proc_tree(tmp_path / "proc", procs or {})
    unreadable(monkeypatch, proc, *unread)
    return live_hw.preflight(
        smu or FakeSmu(),
        lambda: SensorRead(hottest=temp),
        tools,
        budget or (lambda n, floor: SimpleNamespace(usable_mb=usable)),
        power,
        proc,
        sched_ext or tmp_path / "no-sched-ext",
        load or (lambda: {0: 5.0, 1: 0.4}),
    )


def test_a_ready_machine_passes_preflight_without_warnings(tmp_path, monkeypatch):
    assert preflight(tmp_path, monkeypatch, tools=(Tool("y-cruncher"),)) == (None, ())


@pytest.mark.req("T22.F14")
@pytest.mark.parametrize("earlier", [None, "the tool runs under an idle or batch scheduling policy"])
def test_an_unavailable_memory_budget_refuses_preflight_after_any_earlier_refusal(tmp_path, monkeypatch, earlier):
    reason = "/sys/fs/cgroup/user.slice/memory.current is missing, so the memory in use under its limit is unknown"

    def unavailable(instances, floor):
        raise MemoryBudgetUnavailable(reason)

    refusal, warnings = preflight(tmp_path, monkeypatch, budget=unavailable, scheduling=lambda: earlier)
    assert refusal == (f"the memory budget is unavailable: {reason}" if earlier is None else earlier)
    assert warnings == ()


@pytest.mark.req("K50")
def test_a_cpu_busy_above_5_percent_over_the_sample_warns_naming_each_such_cpu(tmp_path, monkeypatch):
    refusal, warnings = preflight(tmp_path, monkeypatch, load=lambda: {8: 7.2, 0: 12.6, 1: 5.0, 2: 0.0})
    assert refusal is None
    assert warnings == ("background load above 5 % over 5 s: CPU 0 13 %, CPU 8 7 %",)


@pytest.mark.req("L01.9.epp")
@pytest.mark.parametrize(
    ("power", "warned"),
    [
        (PowerState(epp="balance_power"), "EPP is balance_power, which holds the clocks under performance"),
        (PowerState(epp="power"), "EPP is power, which holds the clocks under performance"),
        (
            PowerState(split=(("EPP", "performance on cpus 0-7; balance_power on cpus 8-15"),)),
            "EPP is performance on cpus 0-7; balance_power on cpus 8-15, which holds the clocks under performance",
        ),
        (PowerState(epp="balance_performance"), None),
        (PowerState(split=(("EPP", "performance on cpus 0-7; balance_performance on cpus 8-15"),)), None),
        (PowerState(), None),
    ],
)
def test_an_epp_that_saves_power_on_any_cpu_warns_at_start_naming_it(tmp_path, monkeypatch, power, warned):
    refusal, warnings = preflight(tmp_path, monkeypatch, power=power)
    assert refusal is None
    assert warnings == (() if warned is None else (warned,))


@pytest.mark.req("K50")
def test_the_background_load_is_the_busy_share_between_two_reads_5_s_apart(monkeypatch):
    events = []
    reads = iter([{}, {0: 30.0}])
    monkeypatch.setattr(live_hw.CPUUsageReader, "read", lambda self: events.append("read") or next(reads))
    assert live_hw.background_load(events.append) == {0: 30.0}
    assert events == ["read", 5.0, "read"]


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"temp": None}, "no CPU temperature"),
        ({"procs": {4242: ("monitor", ("/sys/kernel/ryzen_smu_drv/smu_args",))}}, "monitor (pid 4242) holds"),
        ({"tools": (Tool("stressapptest", "not on PATH"),)}, "stressapptest is not installed: not on PATH"),
    ],
)
@pytest.mark.req("INV.L01", "L01.14", "L01.6")
def test_each_preflight_gate_refuses_naming_its_reason(tmp_path, monkeypatch, changes, says):
    refusal, _ = preflight(tmp_path, monkeypatch, **changes)
    assert says in refusal


@pytest.mark.req("L01.12", "C1.base_shaper", "L24.c.shaper", "L18.d.8.shaper")
@pytest.mark.parametrize(
    ("shaper", "fact", "refused"),
    [
        (
            ((0, 0, 0), (-30, -27, -12), (-25, -18, -13), (-21, -17, -10), (-6, -3, -2)),
            "Min 0/0/0, Low -30/-27/-12, Medium -25/-18/-13, High -21/-17/-10, Max -6/-3/-2",
            False,
        ),
        (None, "unreadable: the SMU refused the read or its band check failed", True),
    ],
)
def test_the_bios_shaper_joins_the_setup_and_an_unreadable_one_refuses_start(
    tmp_path, monkeypatch, shaper, fact, refused
):
    smu = FakeSmu(shaper=shaper, commands=SimpleNamespace(co_range=(-50, 10), get_curve_shaper_cmd=0x84))
    assert live_hw.shaper_fact(smu) == fact
    refusal, _ = preflight(tmp_path, monkeypatch, smu=smu)
    assert (refusal is not None and "Curve Shaper could not be read" in refusal) is refused


def test_a_shaper_read_of_the_wrong_length_is_never_shown_as_a_partial_table():
    smu = FakeSmu(shaper=((0, 0, 0),) * 4, commands=SimpleNamespace(co_range=(-50, 10), get_curve_shaper_cmd=0x84))
    with pytest.raises(ValueError):
        live_hw.shaper_fact(smu)


@pytest.mark.req("L01.7")
def test_a_host_that_cannot_contain_or_count_a_lane_is_refused_at_start_naming_why(tmp_path, monkeypatch):
    problem = "a test scope limited to CPU 0 ran on CPUs 0-9: AllowedCPUs is not enforced"
    refusal, _ = preflight(tmp_path, monkeypatch, lane=lambda: problem)
    assert refusal == problem


def test_the_first_failing_gate_wins_and_the_later_ones_never_run(tmp_path, monkeypatch):
    def never():
        raise AssertionError("a later gate ran")

    refusal, _ = preflight(tmp_path, monkeypatch, temp=None, scheduling=never)
    assert "no CPU temperature" in refusal


@pytest.mark.req("L01.10")
def test_a_scheduling_policy_that_could_starve_the_watch_refuses(monkeypatch):
    monkeypatch.setattr(live_hw.os, "sched_getscheduler", lambda pid: live_hw.os.SCHED_IDLE)
    assert "idle or batch" in live_hw.scheduling_problem()
    monkeypatch.setattr(live_hw.os, "sched_getscheduler", lambda pid: live_hw.os.SCHED_OTHER)
    assert live_hw.scheduling_problem() is None


def paused_with_its_cores_moved(tmp_path) -> tuple[FakeHardware, LiveRig]:
    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    r.post(PauseRequest())
    r.run_until_rest(0.01)
    hardware.co = {0: -7, 1: -7, 8: -7}
    return hardware, r


@pytest.mark.req("L01.a.abort")
@pytest.mark.parametrize(
    ("policy", "tools", "says"),
    [
        (os.SCHED_OTHER, (Tool("y-cruncher", "not found on PATH"),), "y-cruncher is not installed: not found on PATH"),
        (os.SCHED_IDLE, (), "the tool runs under an idle or batch scheduling policy"),
    ],
    ids=["the stress tool missing", "an idle policy"],
)
def test_an_abort_goes_ahead_where_only_a_test_s_precondition_refuses_and_every_command_that_tests_is_refused(
    tmp_path, monkeypatch, policy, tools, says
):
    monkeypatch.setattr(live_hw.os, "sched_getscheduler", lambda pid: policy)
    refusal, _ = preflight(tmp_path, monkeypatch, tools=tools, scheduling=live_hw.scheduling_problem)
    assert says in refusal
    hardware, r = paused_with_its_cores_moved(tmp_path)
    hardware.refusal = refusal
    for command in ("--resume", "--continue", "--restart"):
        again = rig(tmp_path, hardware, db=r.store.db)
        assert driven(tmp_path, command, again) == live_hw.EXIT_REFUSED
        assert again.refused == refusal
    assert hardware.co == {0: -7, 1: -7, 8: -7}
    runner = FakeRunner(hardware)
    aborted = rig(tmp_path, hardware, runner=runner, db=r.store.db)
    assert driven(tmp_path, "--abort", aborted) == live_hw.EXIT_ABORTED
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert runner.seen == []
    assert lineage(aborted) == [{"id": 1, "status": "aborted", "parent_session_id": None, "origin": None}]


BROKEN_STORED_CONFIGURATION = pytest.mark.parametrize(
    ("domain_of", "k_1t_floor"),
    [(((0, 0), (1, 0), (8, None)), KNOB_FLOOR["k_1t"]), (DOMAINS, 3)],
    ids=["a core whose CCD reads none under the alternate order", "a k_1t floor raised since it started"],
)


@pytest.mark.req("L01.a.abort")
@BROKEN_STORED_CONFIGURATION
def test_an_abort_goes_ahead_where_the_stored_configuration_s_own_checks_tell_a_resume_to_abort(
    tmp_path, monkeypatch, domain_of, k_1t_floor
):
    hardware, r = paused_with_its_cores_moved(tmp_path)
    monkeypatch.setitem(KNOB_FLOOR, "k_1t", k_1t_floor)
    resumed = rig(tmp_path, hardware, db=r.store.db)
    resumed.domain_of = domain_of
    assert driven(tmp_path, "--resume", resumed) == live_hw.EXIT_REFUSED
    assert resumed.refused.endswith("; abort session 1 and start a new one")
    assert hardware.co == {0: -7, 1: -7, 8: -7}
    runner = FakeRunner(hardware)
    aborted = rig(tmp_path, hardware, runner=runner, db=r.store.db)
    aborted.domain_of = domain_of
    assert driven(tmp_path, "--abort", aborted) == live_hw.EXIT_ABORTED
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert runner.seen == []
    assert lineage(aborted) == [{"id": 1, "status": "aborted", "parent_session_id": None, "origin": None}]


@pytest.mark.req("L01.a.abort")
@BROKEN_STORED_CONFIGURATION
def test_a_quarantined_session_s_broken_configuration_refuses_a_reengaged_resume_and_never_its_reengaged_abort(
    tmp_path, monkeypatch, domain_of, k_1t_floor
):
    hardware, r = quarantined_by_three_crashes(tmp_path)
    monkeypatch.setitem(KNOB_FLOOR, "k_1t", k_1t_floor)
    resumed = rig(tmp_path, hardware, db=r.store.db)
    resumed.domain_of = domain_of
    assert driven(tmp_path, "--resume", resumed, "--reengage") == live_hw.EXIT_REFUSED
    assert resumed.refused.endswith("; abort session 1 and start a new one")
    hardware.co = {0: -7, 1: -7, 8: -7}
    runner = FakeRunner(hardware)
    aborted = rig(tmp_path, hardware, runner=runner, db=r.store.db)
    aborted.domain_of = domain_of
    assert driven(tmp_path, "--abort", aborted, "--reengage") == live_hw.EXIT_ABORTED
    assert hardware.co == {0: 0, 1: 0, 8: 0}
    assert runner.seen == []
    assert lineage(aborted) == [{"id": 1, "status": "aborted", "parent_session_id": None, "origin": None}]


@pytest.mark.req("L01.a.abort")
def test_an_abort_is_refused_on_what_reverting_the_co_needs_and_writes_nothing(tmp_path):
    hardware, r = paused_with_its_cores_moved(tmp_path)
    held = "ryzenadj (pid 5) is a known SMU tool, so its writes and this session's could collide"
    hardware.revert_refusal = held
    again = rig(tmp_path, hardware, db=r.store.db)
    assert driven(tmp_path, "--abort", again) == live_hw.EXIT_REFUSED
    assert again.refused == held
    assert hardware.co == {0: -7, 1: -7, 8: -7}
    assert lineage(again) == [{"id": 1, "status": "paused", "parent_session_id": None, "origin": None}]


@pytest.mark.req("L01.a.abort")
def test_reverting_the_co_needs_the_core_map_and_no_other_smu_writer_whatever_the_scheduling_policy(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(live_hw.os, "sched_getscheduler", lambda pid: live_hw.os.SCHED_IDLE)
    quiet = proc_tree(tmp_path / "quiet", {})
    held = proc_tree(tmp_path / "held", {5: ("ryzenadj", ())})
    blocked = SimpleNamespace(core_map_error="CCD 1's core fuse cannot be read")
    assert live_hw.revert_problem(FakeSmu(), quiet) is None
    assert live_hw.revert_problem(blocked, held) == "CCD 1's core fuse cannot be read"
    assert live_hw.revert_problem(FakeSmu(), held) == (
        "ryzenadj (pid 5) is a known SMU tool, so its writes and this session's could collide"
    )


def unreadable(monkeypatch, proc, *pids):
    real = Path.iterdir

    def denied(path):
        if path in {proc / str(pid) / "fd" for pid in pids}:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(Path, "iterdir", denied)


@pytest.mark.req("D84")
def test_a_process_whose_files_cannot_be_read_is_checked_by_name(tmp_path, monkeypatch):
    proc = proc_tree(
        tmp_path / "proc",
        {77: ("systemd", ()), 78: ("python3", ()), 79: ("bash", ())},
        argv={78: ("python3", "/opt/zen/zenstates.py", "-l")},
    )
    unreadable(monkeypatch, proc, 77, 79)
    refusal, by_name_only = live_hw.smu_holder(proc)
    assert refusal == "python3 (pid 78) is a known SMU tool, so its writes and this session's could collide"
    quiet = proc_tree(tmp_path / "quiet", {77: ("systemd", ()), 79: ("bash", ())})
    unreadable(monkeypatch, quiet, 77, 79)
    assert live_hw.smu_holder(quiet) == (None, 2)


@pytest.mark.req("L01.8")
def test_a_known_smu_tool_refuses_even_without_a_driver_handle(tmp_path):
    proc = proc_tree(tmp_path / "proc", {5: ("ryzenadj", ())})
    assert live_hw.smu_holder(proc) == (
        "ryzenadj (pid 5) is a known SMU tool, so its writes and this session's could collide",
        0,
    )


def test_processes_checked_by_name_only_are_a_warning(tmp_path, monkeypatch):
    refusal, warnings = preflight(tmp_path, monkeypatch, procs={77: ("systemd", ())}, unread=(77,))
    assert refusal is None
    assert warnings == ("SMU tool check by name only for 1 processes: their open files need root",)


def sched_ext(root: Path, state: str | None, ops: str | None = None) -> Path:
    root.mkdir()
    if state is not None:
        (root / "state").write_text(f"{state}\n")
    if ops is not None:
        (root / "root").mkdir()
        (root / "root" / "ops").write_text(f"{ops}\n")
    return root


@pytest.mark.req("K49", "L01.13", "T22.F20")
def test_a_running_ananicy_alone_is_not_a_warning_and_a_short_hot_budget_is(tmp_path, monkeypatch):
    usable = 8000
    refusal, warnings = preflight(tmp_path, monkeypatch, usable=usable, procs={9: ("ananicy-cpp", ())})
    assert refusal is None
    assert warnings == (
        f"HOT would run y-cruncher with {usable // 2} MB now, under its {stages.HOT_MEMORY_CAP_MB} MB cap",
    )


@pytest.mark.req("K49", "L01.13")
def test_a_hot_budget_at_its_cap_is_not_below_it_and_warns_nothing(tmp_path, monkeypatch):
    assert preflight(tmp_path, monkeypatch, usable=2 * stages.HOT_MEMORY_CAP_MB) == (None, ())


@pytest.mark.req("INT.3")
def test_an_enabled_sched_ext_scheduler_is_a_warning_naming_it(tmp_path, monkeypatch):
    root = sched_ext(tmp_path / "scx", "enabled", "lavd_1.0.13")
    refusal, warnings = preflight(tmp_path, monkeypatch, sched_ext=root)
    assert refusal is None
    assert warnings == ("sched_ext scheduler lavd_1.0.13 is running; tests run under it",)


def test_sched_ext_absent_or_disabled_is_silent_and_unreadable_is_said(tmp_path, monkeypatch):
    assert live_hw.sched_ext_note(tmp_path / "absent") is None
    assert live_hw.sched_ext_note(sched_ext(tmp_path / "off", "disabled")) is None
    nameless = live_hw.sched_ext_note(sched_ext(tmp_path / "nameless", "enabled"))
    assert nameless.startswith("sched_ext scheduler (name unreadable: ")
    blocked = sched_ext(tmp_path / "blocked", None)
    (blocked / "state").mkdir()
    assert live_hw.sched_ext_note(blocked).startswith("sched_ext state unreadable: ")


def test_a_process_that_vanishes_mid_scan_is_skipped(tmp_path):
    proc = proc_tree(tmp_path / "proc", {5: ("ryzenadj", ("/sys/kernel/ryzen_smu_drv/smu_args",))})
    (proc / "6").mkdir()
    assert "ryzenadj (pid 5) is a known SMU tool" in live_hw.smu_holder(proc)[0]
    holder = proc_tree(tmp_path / "holder", {8: ("monitor", ("/sys/kernel/ryzen_smu_drv/smu_args",))})
    assert live_hw.smu_holder(holder) == (
        "monitor (pid 8) holds the SMU driver open, so its writes and this session's could collide",
        0,
    )
    quiet = proc_tree(tmp_path / "quiet", {7: ("bash", ())})
    (quiet / "6").mkdir()
    (quiet / "9").mkdir()
    (quiet / "9" / "comm").write_text("bash\n")
    (quiet / "9" / "cmdline").write_bytes(b"bash\0")
    assert live_hw.smu_holder(quiet) == (None, 0)


def test_the_hardware_adapter_checks_its_own_smu_sensors_tools_budget_and_power_state(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(live_hw, "preflight", lambda *parts: seen.append(parts) or ("refused", ()))
    tools = (Tool("y-cruncher"),)
    cpu = sysfs(tmp_path, 4300000, 5756452, "1")
    hw = live_hw.SmuHardware(FakeSmu(), False, print, warm, cpu, tools=tools, budget=len, vcache="none")
    assert hw.preflight() == ("refused", ())
    assert seen == [(hw.smu, warm, tools, len, live_hw.read_power_state(cpu))]
    monkeypatch.setattr(live_hw, "revert_problem", lambda *parts: seen.append(parts) or "held")
    assert hw.revert_problem() == "held"
    assert seen[1:] == [(hw.smu,)]


def test_the_lane_budget_reads_the_budget_under_the_lane_cgroup(monkeypatch):
    monkeypatch.setattr(live_hw, "lane_cgroup_parent", lambda: "/lanes")
    monkeypatch.setattr(live_hw, "memory_budget", lambda n, floor, lane_cgroup: (n, floor, lane_cgroup))
    assert live_hw.lane_budget(2, 256) == (2, 256, "/lanes")


def test_a_session_that_is_not_stored_reads_as_not_stored_by_name(tmp_path):
    assert live.read_stored(database(tmp_path / "h.db"), 9) == (None, "session 9 is not stored")


def test_the_hardware_adapter_reads_the_power_state_under_its_cpu_root(tmp_path):
    hw = live_hw.SmuHardware(FakeSmu(), False, print, warm, sysfs(tmp_path, 4300000, 5756452, "1"), vcache="none")
    state = hw.power()
    assert (state.max_mhz, state.top_mhz, state.boost) == (4300.0, 5756.452, "1")


@pytest.mark.req("L18.d.4.poll")
def test_a_slow_machine_check_poll_refuses_the_start(tmp_path, monkeypatch):
    late = "machine-check poll every 300 s, above 5 s, so corrected errors log late: echo 5 | sudo tee x"
    assert preflight(tmp_path, monkeypatch, mce=lambda: late, tools=(Tool("y-cruncher"),)) == (late, ())


@pytest.mark.req("A2.coverage")
def test_coverage_counts_each_core_s_seconds_per_clock_and_ccd_temperature_cell_while_the_load_runs():
    second = 1_000_000_000
    samples = [
        {"running": True, "mono_ns": 0, "clocks": [[0, 5040.0], [1, 4410.0]], "tccd": [[0, 61.0], [1, 54.9]]},
        {"running": True, "mono_ns": second // 2, "clocks": [[0, 5099.0], [1, None]], "tccd": [[0, 64.9], [1, 55.0]]},
        {"running": False, "mono_ns": second, "clocks": [[0, 3000.0], [1, 3000.0]], "tccd": [[0, 40.0], [1, 40.0]]},
        {"running": True, "mono_ns": 5 * second, "clocks": [[0, 5100.0]], "tccd": [[0, 66.0]]},
        {"running": True, "mono_ns": 20 * second, "clocks": [], "tccd": []},
    ]
    assert coverage(samples, {0: 0, 1: 1}) == {
        0: {"cells": [[5000, 60, 1.0], [5100, 65, 1.0]], "unreadable_s": 0.0},
        1: {"cells": [[4400, 50, 0.5]], "unreadable_s": 1.5},
    }


def test_coverage_keeps_hundredths_of_a_second():
    third = 1_000_000_000 // 3
    samples = [
        {"running": True, "mono_ns": 0, "clocks": [[0, 5000.0]], "tccd": [[0, 60.0]]},
        {"running": True, "mono_ns": third, "clocks": [], "tccd": []},
        {"running": True, "mono_ns": 2 * third, "clocks": [], "tccd": []},
    ]
    assert coverage(samples, {0: 0}) == {0: {"cells": [[5000, 60, 0.33]], "unreadable_s": 0.33}}


def test_coverage_of_no_samples_is_empty_for_every_core():
    assert coverage([], {0: 0}) == {0: {"cells": [], "unreadable_s": 0.0}}


@pytest.mark.req("A2.coverage")
def test_a_returned_test_stores_each_core_s_coverage_from_its_own_samples(tmp_path):
    store = Store(database(":memory:"), tmp_path / "s", IDENTITY)
    store.save(stored_with_fb(None), "boot-a")
    row = store.db.insert_tuner_test(1, "boot-a", 1, "search", None, "1t", 0, -3, "[]", "[0]", 60.0, "{}")
    for n, temp in enumerate((61.0, 62.0, 63.0)):
        sample = telemetry.Sample(True, ((0, 5040.0),), 60.0, mono_ns=n * 100_000_000, tccd=((0, temp),))
        store.sample(row, sample, durable=n == 2)
    store.test_returned("boot-a", row, Report(Group.PASS, 60.0), {0: 0})
    (saved,) = rows(SimpleNamespace(store=store), "SELECT coverage_json FROM tuner_tests WHERE id=?", (row,))
    assert json.loads(saved["coverage_json"]) == {"0": {"cells": [[5000, 60, 0.2]], "unreadable_s": 0.0}}


@dataclass
class OnlyTccd2Runner(FakeRunner):
    def run(self, spec: Spec, stop: threading.Event, sink=None) -> Report:
        for n in range(3):
            sink(telemetry.Sample(True, ((spec.core, 5040.0),), 70.0, mono_ns=n * 500_000_000, tccd=((1, 61.0),)))
        return super().run(spec, stop, sink)


@pytest.mark.req("A2.coverage")
def test_a_tested_core_s_coverage_reads_the_tccd_of_its_physical_ccd_not_of_its_l3_rank(tmp_path):
    hardware = FakeHardware()
    r = rig(tmp_path, hardware, OnlyTccd2Runner(hardware), physical_ccd_of=((0, 1), (1, 1), (8, 1)))
    r.start(config(), 1, 7, True)
    while not rows(r, "SELECT id FROM tuner_tests WHERE ended_at IS NOT NULL"):
        r.step(0.01)
    first = rows(r, "SELECT core, coverage_json FROM tuner_tests WHERE ended_at IS NOT NULL ORDER BY id LIMIT 1")[0]
    record = json.loads(first["coverage_json"])[str(first["core"])]
    assert record == {"cells": [[5000, 60, 1.0]], "unreadable_s": 0.0}


@pytest.mark.req("A2.coverage")
def test_the_rig_takes_each_core_s_physical_ccd_from_the_driver_s_core_map(tmp_path):
    vermeer = SimpleNamespace(
        cores={core: SimpleNamespace(logical_cpus=(core, core + 6), ccd=0) for core in range(6)},
        model_name="AMD Ryzen 5 5600X 6-Core Processor",
        is_x3d=False,
        vcache_ccd=None,
    )
    args = live_hw.parse(["--state-dir", str(tmp_path / "s"), "--start", "--cores", "0", "--max-offset", "-8"])
    kagari = SimpleNamespace(binary_problem=lambda name: None)
    db = database(tmp_path / "vermeer.db")
    built = live_hw.build_rig(args, vermeer, pr23_5600x(), lambda: kagari, lambda line: None, db, IDENTITY, 300)
    assert built.domain_of == tuple((core, 0) for core in range(6))
    assert built.physical_ccd_of == tuple((core, 1) for core in range(6))


def single_core_row(stage, core, value, verdict, cells, ended=True, recorded_core=None, records=(), unblamed=False):
    record = {str(core if recorded_core is None else recorded_core): {"cells": cells, "unreadable_s": 0.0}}
    evidence = {"records": [asdict(found) for found in records], "unblamed_corrected": unblamed}
    return {
        "stage": stage,
        "core": core,
        "value": value,
        "verdict": verdict,
        "ended_at": "2026-10-15T15:00:00+00:00" if ended else None,
        "coverage_json": None if cells is None else json.dumps(record),
        "evidence_json": json.dumps(evidence) if ended else None,
    }


@pytest.mark.req("A2.margins")
def test_the_margin_map_keeps_per_clock_the_deepest_pass_above_the_shallowest_failure_of_single_core_tests():
    found = margins(
        [
            single_core_row("search", 9, -35, "pass", [[5400, 55, 20.0]]),
            single_core_row("search", 9, -30, "pass", [[5400, 60, 20.0], [5800, 50, 1.0]]),
            single_core_row("search", 9, -28, "silicon", [[5400, 60, 12.03], [5500, 65, 6.0]]),
            single_core_row("search", 9, -28, "pass", [[5400, 55, 0.07]]),
            single_core_row("live1", 9, -27, "pass", [[5400, 55, 20.04]]),
            single_core_row("search", 9, -29, "thermal", [[5400, 80, 20.0]]),
            single_core_row("steady", 9, -40, "silicon", [[4400, 80, 20.0]]),
            single_core_row("search", 9, -44, None, None, ended=False),
            single_core_row("search", 9, -26, "pass", None),
            single_core_row("search", 9, -25, "pass", [[5400, 55, 20.0]], recorded_core=3),
            single_core_row("search", 4, -39, "pass", [[4800, 55, 20.0]]),
        ]
    )
    assert list(found) == [4, 9]
    assert found[9] == {
        "bins": {
            5400: {
                "passed": -27,
                "passed_s": 20.0,
                "failed": -28,
                "failed_s": 12.0,
                "deeper": [-35, -30, -28],
                "temp": [55, 65],
                "seconds": 72.1,
            },
            5500: {
                "passed": None,
                "passed_s": 0.0,
                "failed": -28,
                "failed_s": 6.0,
                "deeper": [],
                "temp": [65, 70],
                "seconds": 6.0,
            },
            5800: {
                "passed": -30,
                "passed_s": 1.0,
                "failed": None,
                "failed_s": 0.0,
                "deeper": [],
                "temp": [50, 55],
                "seconds": 1.0,
            },
        },
        "open": [-44],
        "unrecorded": 2,
    }
    assert found[4]["bins"][4800]["passed"] == -39


@pytest.mark.req("A2.margins")
def test_a_pass_with_a_record_on_its_own_core_is_the_failure_the_machine_graded_it_at_that_value():
    fake = fresh(2, [-10, -10], 1, max_offset=-12, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    returned, tests, injected = fake.returned, [], []

    def stored(worker, report=None, cut=False):
        spec = worker.spec
        if not injected and spec.stage is Stage.SEARCH and (spec.core, spec.value) == (0, -5):
            report = Report(Group.PASS, spec.seconds, evidence=(Evidence(0, corrected=True),))
            injected.append(spec.value)
        out = returned(worker, report, cut)
        verdict, cells = out.report.group.value, [[5500, 55, 20.0]]
        tests.append(
            single_core_row(spec.stage.value, spec.core, spec.value, verdict, cells, records=out.report.evidence)
        )
        return out

    fake.returned = stored
    start(fake)
    run_to_rest(fake, 20000)
    assert (fake.state.lifecycle, injected) == (Lifecycle.COMPLETED, [-5])
    assert [(record.core, record.value, record.fb) for record in fake.db.cores][0] == (0, -4, -5)
    cell = margins(tests)[0]["bins"][5500]
    assert (cell["passed"], cell["failed"]) == (-4, -5)


@pytest.mark.req("A2.margins")
def test_an_environment_verdict_with_an_unblamed_corrected_record_is_a_failure_and_the_flag_fails_nothing_else():
    cells = [[5400, 55, 20.0]]
    found = margins(
        [
            single_core_row("search", 9, -27, "pass", cells, unblamed=True),
            single_core_row("search", 9, -28, "environment", cells),
            single_core_row("search", 9, -30, "pass", cells),
            single_core_row("live1", 9, -31, "environment", cells, unblamed=True),
        ]
    )
    assert found[9]["bins"][5400] == {
        "passed": -30,
        "passed_s": 20.0,
        "failed": -31,
        "failed_s": 20.0,
        "deeper": [],
        "temp": [55, 60],
        "seconds": 60.0,
    }


@pytest.mark.req("A2.margins")
def test_a_record_on_another_core_leaves_the_tested_core_s_pass_a_pass_and_one_on_its_own_fails_any_verdict():
    cells = [[5400, 55, 20.0]]
    found = margins(
        [
            single_core_row("search", 9, -30, "pass", cells, records=(Evidence(3, corrected=True),)),
            single_core_row("search", 9, -31, "thermal", cells, records=(Evidence(9, corrected=False),)),
        ]
    )
    cell = found[9]["bins"][5400]
    assert (cell["passed"], cell["failed"], cell["seconds"]) == (-30, -31, 40.0)


@pytest.mark.req("A2.margins")
def test_a_record_on_the_tested_core_fails_its_pass_wherever_it_stands_among_the_records():
    cells = [[5400, 55, 20.0]]
    found = margins(
        [
            single_core_row("search", 9, -30, "pass", cells),
            single_core_row(
                "search", 9, -31, "pass", cells, records=(Evidence(3, corrected=True), Evidence(9, corrected=True))
            ),
        ]
    )
    cell = found[9]["bins"][5400]
    assert (cell["passed"], cell["failed"]) == (-30, -31)


@pytest.mark.req("VERDICT.18", "PRELUDE.1")
@pytest.mark.parametrize(
    ("asleep_after", "group", "detail"),
    [
        (5.0, Group.SLEPT, "the machine slept 5 s during the test; y-cruncher passed"),
        (0.5, Group.PASS, "y-cruncher passed"),
        (1.0, Group.PASS, "y-cruncher passed"),
    ],
)
def test_a_test_the_machine_slept_through_is_reported_slept_first(tmp_path, asleep_after, group, detail):
    from corecycler.tuner.machine.types import WorkerReported

    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    r.asleep_at_worker_start = 0.0
    hardware.asleep_seconds = lambda: asleep_after
    reported = r.slept_through(WorkerReported(1, Report(Group.PASS, 60.0, detail="y-cruncher passed")))
    assert (reported.report.group, reported.report.detail) == (group, detail)


def test_a_report_already_slept_is_left_as_it_came(tmp_path):
    from corecycler.tuner.machine.types import WorkerReported

    hardware = FakeHardware()
    r = rig(tmp_path, hardware)
    hardware.asleep_seconds = lambda: 9.0
    came = WorkerReported(1, Report(Group.SLEPT, 60.0, detail="slept"))
    assert r.slept_through(came) is came


@pytest.mark.req("K23")
def test_the_gap_counts_what_grew_past_one_second_since_the_last_test_started_and_holds_until_the_next(tmp_path):
    hardware = FakeHardware()
    asleep = iter([5.0, 6.0, 6.5, 6.5])
    hardware.asleep_seconds = lambda: next(asleep)
    r = rig(tmp_path, hardware)
    r.asleep_at_worker_start = 5.0
    assert [r.world().gap_grew for _ in range(4)] == [False, False, True, True]


@pytest.mark.req("K23")
def test_a_sleep_while_a_thermal_cool_down_polls_still_makes_the_next_test_rewrite_every_core(tmp_path):
    hardware = FakeHardware()
    slept, heat = [0.0], [99.0]
    hardware.asleep_seconds = lambda: slept[0]
    hardware.temperature = lambda: heat[0]
    runner = FakeRunner(hardware, group=Group.THERMAL)
    r = rig(tmp_path, hardware, runner=runner)
    r.start(config(), 1, 7, True)
    for _ in range(10_000):
        if getattr(r.state.slot, "cool_below_c", None) is not None:
            break
        r.step(0.01)
    runner.group = None
    hardware.co = {0: -3, 1: -3, 8: -3}
    slept[0] = 600.0
    r.step(0.01)
    assert getattr(r.state.slot, "cool_below_c", None) is not None
    heat[0] = 50.0
    for _ in range(10_000):
        if isinstance(r.state.slot, InFlight):
            break
        r.step(0.01)
    assert isinstance(r.state.slot, InFlight)
    assert hardware.co == dict(r.state.slot.spec.vector)


def test_a_stored_all_core_cursor_from_before_the_rounds_loads_without_its_retired_fields():
    from corecycler.tuner.live import _allcore_from_json

    old = {
        "t_idx": 2,
        "t_clean": 30.0,
        "t_types": [["light", 1]],
        "t_busy": [],
        "co_delta": [[0, 1]],
        "rung": 1,
        "floors": [[0, -6], [1, -3]],
        "item": 0,
        "frozen": [1],
        "tightened": False,
        "pass_no": 1,
    }
    cursor = _allcore_from_json(old)
    assert (cursor.t_idx, cursor.floors, cursor.frozen, cursor.rounds) == (2, ((0, -6), (1, -3)), (1,), 0)
    with pytest.raises(TypeError):
        _allcore_from_json({**old, "unknown": 1})


@pytest.mark.req("L25.d.power", "O19.record")
def test_every_sensor_reading_carries_the_core_rail_and_the_package_power_beside_the_board_voltages():
    from corecycler.monitor.hwmon import SensorRead
    from corecycler.smu.pmtable import PMTableData

    board = SensorRead(tctl=60.0, volts=(("nct6799:in0", 1.25),))
    floats = [0.0] * 613
    floats[0x574 // 4 + 12], floats[0x5B4 // 4 + 12], floats[0x5F4 // 4 + 12], floats[0x4F4 // 4 + 12] = (
        5.438,
        5.459,
        99.87,
        1.149,
    )
    table = PMTableData(
        vdd_vid_v=1.3, vdd_v=1.29, package_power_w=142.5, pm_table_version=0x00620205, raw_floats=floats
    )
    read = live_hw.with_pm_table(lambda: board, lambda: table, range(16))()
    assert read.cores[12] == (
        12,
        pytest.approx(5438.0),
        pytest.approx(5459.0),
        pytest.approx(99.87),
        pytest.approx(1.149),
    )
    assert read.volts == (
        ("nct6799:in0", 1.25),
        ("pm_table:vdd", pytest.approx(1.29)),
        ("pm_table:vdd_vid", pytest.approx(1.3)),
    )
    assert read.watts == (("pm_table:package", pytest.approx(142.5)),)
    assert live_hw.with_pm_table(lambda: board, lambda: table, range(12))().cores == ()
    assert live_hw.with_pm_table(lambda: board, lambda: None, range(16))() == board


@pytest.mark.req("L00.d", "INTERRUPT.fifo")
def test_events_are_transitioned_one_at_a_time_in_arrival_order_and_a_follow_up_after_its_cause(tmp_path, monkeypatch):
    calls, depth = [], [0]
    transitioned = live.transition

    def counted(state, event, world):
        depth[0] += 1
        calls.append((type(event).__name__, depth[0]))
        try:
            return transitioned(state, event, world)
        finally:
            depth[0] -= 1

    monkeypatch.setattr(live, "transition", counted)
    r = rig(tmp_path)
    r.start(config(), 1, 7, True)
    while r.worker is None:
        r.step(0.01)
    r.signals.extend([PauseRequest(), Abort()])
    r.step(0.01)
    r.step(0.01)
    names = [name for name, _ in calls]
    assert names.index("PauseRequest") < names.index("Abort")
    assert r.state.lifecycle is Lifecycle.ABORTED
    r2 = rig(tmp_path / "again")
    r2.start(config(), 2, 7, True)
    while r2.worker is None:
        r2.step(0.01)
    r2.signals.append(Close())
    r2.step(0.01)
    calls.clear()
    again = rig(tmp_path / "again", r2.hardware, db=r2.store.db)
    again.resume(2)
    names = [name for name, _ in calls]
    assert names[:2] == ["Resume", "CrashEvidenceRead"]
    assert all(level == 1 for _, level in calls)
