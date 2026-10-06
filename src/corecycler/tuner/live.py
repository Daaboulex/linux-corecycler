from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from corecycler.engine import detector as kernel
from corecycler.tuner import telemetry
from corecycler.tuner.machine.session import transition
from corecycler.tuner.machine.types import (
    Abort,
    AllCore,
    Announce,
    AwaitWorker,
    BisectRun,
    CancelSchedule,
    ClearStockMarker,
    Close,
    CloseDb,
    Commit,
    ContinuationDue,
    CoreRecord,
    CrashEvidenceRead,
    DatabaseFailed,
    Evidence,
    FailContext,
    Flow,
    ForgetResident,
    InternalError,
    Klass,
    Lifecycle,
    Live1,
    Load,
    MachineConfig,
    Mode,
    PauseReason,
    PauseRequest,
    Phase,
    ReadCrashEvidence,
    Report,
    Resume,
    RevertAll,
    Schedule,
    SessionState,
    SmuRefused,
    Stage,
    Start,
    StartWorker,
    StopWorker,
    Stored,
    TestSpec,
    WorkerReported,
    World,
    WriteCO,
    WriteStockMarker,
)
from corecycler.tuner.orders import Method, RoundCursor
from corecycler.tuner.persistence import crash_suspects, resident_at_death

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from corecycler.history.db import HistoryDB, JournalRow


class Hardware(Protocol):
    def read_co(self, core: int) -> int | None: ...
    def write_co(self, core: int, value: int) -> str | None: ...
    def temperature(self) -> float | None: ...
    def frequency_capped(self) -> bool: ...
    def boot_id(self) -> str: ...
    def boot_ended_clean(self, boot_id: str) -> bool: ...
    def asleep_seconds(self) -> float: ...
    def harvest(self, boot_id: str, since_ns: int | None, next_boot: bool) -> kernel.JournalHarvest: ...


class Runner(Protocol):
    def run(self, spec: TestSpec, stop: threading.Event, sink: Callable[[telemetry.Sample], None]) -> Report: ...


def config_to_json(cfg: MachineConfig) -> dict:
    return {**asdict(cfg), "selected": list(cfg.selected)}


def config_from_json(raw: dict) -> MachineConfig:
    return MachineConfig(
        **{
            **raw,
            "mode": Mode(raw["mode"]),
            "method": Method(raw["method"]),
            "selected": tuple(raw["selected"]),
            "stage_seconds": _pairs(raw["stage_seconds"]),
        }
    )


def _pairs(raw) -> tuple:
    return tuple(tuple(item) for item in raw)


def _groups(raw) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(group) for group in raw)


def _bisect_from_json(raw: dict | None) -> BisectRun | None:
    if raw is None:
        return None
    return BisectRun(
        entry=Stage(raw["entry"]),
        lane=raw["lane"],
        klass=Klass(raw["klass"]),
        universe=tuple(raw["universe"]),
        values=_pairs(raw["values"]),
        queue=_groups(raw["queue"]),
        results=tuple((tuple(group), passed) for group, passed in raw["results"]),
        parents=tuple((tuple(child), tuple(parent)) for child, parent in raw["parents"]),
        rerun=None if raw["rerun"] is None else tuple(raw["rerun"]),
    )


def _allcore_from_json(raw: dict | None) -> AllCore | None:
    if raw is None:
        return None
    pairs = ("t_types", "t_busy", "co_delta", "floors")
    return AllCore(**{**raw, **{name: _pairs(raw[name]) for name in pairs}, "frozen": tuple(raw["frozen"])})


def _live1_from_json(raw: dict) -> Live1:
    back = raw["back"]
    return Live1(raw["index"], tuple(raw["requeue"]), raw["solo"], None if back is None else Stage(back))


def _flow_from_json(raw: dict | None) -> Flow | None:
    if raw is None:
        return None
    live = raw["live1"]
    return Flow(
        stage=Stage(raw["stage"]),
        klass=Klass(raw["klass"]),
        cursor=RoundCursor(**raw["cursor"]),
        owed=raw["owed"],
        last_core=raw["last_core"],
        applied=raw["applied"],
        dirty=raw["dirty"],
        live1=None if live is None else _live1_from_json(live),
        bisect=_bisect_from_json(raw["bisect"]),
        q5=tuple(((key[0], key[1]), count) for key, count in raw["q5"]),
        q5_others=tuple(raw["q5_others"]),
        allcore=_allcore_from_json(raw["allcore"]),
    )


def stored_to_json(st: Stored) -> dict:
    out = asdict(st)
    out["config"] = config_to_json(st.config)
    return out


def stored_from_json(raw: dict) -> Stored:
    return Stored(
        session_id=raw["session_id"],
        status=raw["status"],
        config=config_from_json(raw["config"]),
        seed=raw["seed"],
        baseline=_pairs(raw["baseline"]),
        cores=tuple(CoreRecord(**{**core, "phase": Phase(core["phase"])}) for core in raw["cores"]),
        flow=_flow_from_json(raw["flow"]),
        resume_streak=raw["resume_streak"],
        incidents=raw["incidents"],
    )


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as handle:
        json.dump(payload, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@dataclass(frozen=True, slots=True)
class Identity:
    bios_version: str
    cpu_model: str
    context_id: int | None


class Store:
    def __init__(self, db: HistoryDB, root: Path, identity: Identity) -> None:
        self.db = db
        self.root = root
        self.identity = identity
        self.retracted: dict[tuple[int, int], int] = {}
        root.mkdir(parents=True, exist_ok=True)

    def new_session_id(self) -> int:
        return self.db.next_tuner_session_id()

    def save(self, st: Stored, boot: str) -> None:
        tightened = {
            (st.session_id, r.core): r.fb
            for r in st.cores
            if r.fb is not None and self.retracted.get((st.session_id, r.core)) != r.fb
        }
        with self.db.transaction():
            self.db.save_machine_session(
                st.session_id,
                status=st.status,
                config_json=json.dumps(config_to_json(st.config)),
                machine_json=json.dumps(stored_to_json(st)),
                boot_id=boot,
                bios_version=self.identity.bios_version,
                cpu_model=self.identity.cpu_model,
                context_id=self.identity.context_id,
            )
            for (session, core), fb in tightened.items():
                self.db.journal_retract(session, core, fb, -1)
        self.retracted.update(tightened)

    def load(self, session_id: int) -> tuple[Stored, str, int | None] | None:
        row = self.db.load_machine_session(session_id)
        if row is None:
            return None
        raw, boot, read_ns = row
        return replace(stored_from_json(json.loads(raw)), session_id=session_id), boot, read_ns

    def test_started(self, session_id: int, token: int, spec: TestSpec, boot: str) -> int:
        return self.db.insert_tuner_test(
            session_id,
            boot,
            token,
            spec.stage.value,
            None if spec.load is None else spec.load.value,
            spec.klass.value,
            spec.core,
            spec.value,
            json.dumps(spec.vector),
            json.dumps(spec.cpus),
            spec.seconds,
        )

    def test_returned(self, session_id: int, boot: str, row: int, report: Report) -> None:
        evidence = {"records": [asdict(e) for e in report.evidence], "unblamed_corrected": report.unblamed_corrected}
        self.db.end_tuner_test(
            row,
            report.group.value,
            report.seconds,
            json.dumps(report.named),
            None if report.context is None else json.dumps(asdict(report.context)),
            json.dumps(evidence),
            report.detail,
        )
        corrected = tuple(sorted({e.core for e in report.evidence if e.corrected}))
        self.db.journal_mark_survived(session_id, boot_id=boot, exclude_cores=corrected)
        if report.kernel_read_ns is not None:
            self.db.set_kernel_read(session_id, report.kernel_read_ns)

    def journal_rows(self, session_id: int, boot: str) -> list[JournalRow]:
        return self.db.journal_rows(session_id, boot)

    def taken_over(self, session_id: int, boot: str) -> bool:
        return self.db.journal_taken_over(session_id, boot) is not None

    def sample(self, row: int, sample: telemetry.Sample) -> None:
        self.db.insert_tuner_sample(
            row,
            sample.mono_ns,
            sample.running,
            sample.tctl,
            json.dumps(sample.mhz),
            json.dumps(sample.tccd),
            json.dumps(sample.volts),
        )

    def crash_context(self, session_id: int, boot: str, core_of_cpu: dict[int, int]) -> FailContext | None:
        test = self.db.open_tuner_test(session_id, boot)
        if test is None:
            return None
        tail = self.db.tuner_sample_tail(test["id"], telemetry.RING)
        if not tail:
            return None
        ring = [
            telemetry.Sample(
                bool(s["running"]),
                tuple((core, mhz) for core, mhz in json.loads(s["clocks_json"])),
                s["tctl"],
            )
            for s in tail
        ]
        assigned = tuple(sorted({core_of_cpu[cpu] for cpu in json.loads(test["cpus_json"]) if cpu in core_of_cpu}))
        return telemetry.context(ring, test["load"] == Load.HOT.value, assigned, ())

    def test_broken(self, row: int, detail: str) -> None:
        evidence = {"records": [], "unblamed_corrected": False}
        self.db.end_tuner_test(row, "internal_error", 0.0, "[]", None, json.dumps(evidence), detail)

    def event(self, session_id: int, kind: str, text: str, boot: str) -> None:
        self.db.insert_tuner_event(session_id, text, boot, kind)

    def journal_intent(self, session_id: int, core: int, value: int, boot: str, mono_ns: int) -> int:
        envelope = self.db.journal_envelope(session_id, core)
        survived = value == 0 or (envelope is not None and value >= envelope)
        return self.db.journal_co_intent(session_id, core, value, survived, boot_id=boot, mono_ns=mono_ns)

    def journal_confirm(self, row: int) -> None:
        self.db.journal_confirm(row)

    def write_marker(self, boot: str) -> None:
        _write_atomic(self.root / "stock-marker.json", {"boot": boot})

    def marker_boot(self) -> str | None:
        path = self.root / "stock-marker.json"
        return json.loads(path.read_text())["boot"] if path.exists() else None

    def clear_marker(self) -> None:
        (self.root / "stock-marker.json").unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class Sampled:
    token: int
    sample: telemetry.Sample


@dataclass
class _Worker:
    token: int
    stop: threading.Event
    thread: threading.Thread


@dataclass
class LiveRig:
    hardware: Hardware
    runner: Runner
    store: Store
    cpus_of: tuple[tuple[int, tuple[int, ...]], ...]
    domain_of: tuple[tuple[int, int | None], ...]
    two_t_problem: str | None = None
    say: Callable[[str], None] = print
    clock: Callable[[], float] = time.monotonic
    state: SessionState = field(default_factory=SessionState)
    resident: dict[int, int | None] = field(default_factory=dict)
    timer: tuple[int, float] | None = None
    worker: _Worker | None = None
    inbox: queue.Queue = field(default_factory=queue.Queue)
    signals: list = field(default_factory=list)
    asleep_at_last_check: float = 0.0
    stored_boot: str | None = None
    stored_read_ns: int | None = None
    test_row: tuple[int, int] | None = None

    def world(self) -> World:
        asleep = self.hardware.asleep_seconds()
        gap = asleep - self.asleep_at_last_check > 1.0
        self.asleep_at_last_check = asleep
        return World(
            resident=tuple(sorted(self.resident.items())),
            gap_grew=gap,
            frequency_capped=self.hardware.frequency_capped(),
            now=self.clock(),
            temperature_c=self.hardware.temperature(),
            cpus_of=self.cpus_of,
            domain_of=self.domain_of,
            two_t_problem=self.two_t_problem,
        )

    def world_for_start(self) -> World:
        return replace(self.world(), bios_co=tuple((core, self.hardware.read_co(core)) for core, _ in self.cpus_of))

    def world_for_resume(self, session_id: int) -> World:
        loaded = self.store.load(session_id)
        world = self.world()
        if loaded is None:
            return world
        stored, self.stored_boot, self.stored_read_ns = loaded
        return replace(world, stored=stored)

    def post(self, event, world: World | None = None) -> None:
        pending = [event]
        while pending:
            ev = pending.pop(0)
            before = self.state
            result = transition(before, ev, world if world is not None and ev is event else self.world())
            if result.refused is not None:
                self.say(f"refused: {result.refused}")
            self.state = result.state
            acting = result.state if result.state.session_id is not None else before
            follow = self._run(acting, result.effects)
            if follow is not None:
                pending.append(follow)

    def _run(self, acting: SessionState, effects) -> object | None:
        for effect in effects:
            follow = self._apply(acting, effect)
            if follow is not None:
                return follow
        return None

    def _apply(self, acting: SessionState, effect) -> object | None:
        match effect:
            case WriteCO(core=core, value=value):
                try:
                    row = self._journal(acting, core, value)
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the CO journal could not be written: {exc}")
                problem = self.hardware.write_co(core, value)
                if problem is not None:
                    self.resident[core] = None
                    return SmuRefused(core, value, problem)
                self.resident[core] = value
                try:
                    self.store.journal_confirm(row)
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the CO journal could not be confirmed: {exc}")
            case RevertAll(values=values, journal=journal):
                failed: sqlite3.Error | None = None
                for core, value in values:
                    row = None
                    if journal and failed is None:
                        try:
                            row = self._journal(acting, core, value)
                        except sqlite3.Error as exc:
                            failed = exc
                    problem = self.hardware.write_co(core, value)
                    self.resident[core] = None if problem else value
                    if problem is not None:
                        self.say(f"revert of core {core} to {value} failed: {problem}")
                    elif row is not None:
                        try:
                            self.store.journal_confirm(row)
                        except sqlite3.Error as exc:
                            failed = exc
                if failed is not None:
                    return DatabaseFailed(f"the CO journal failed during a revert: {failed}")
            case StopWorker(token=token):
                if self.worker is not None and self.worker.token == token:
                    self.worker.stop.set()
            case AwaitWorker(token=token, bound_s=bound):
                if self.worker is not None and self.worker.token == token:
                    self.worker.thread.join(bound)
                    if not self.worker.thread.is_alive():
                        self.worker = None
            case Commit(stored=st):
                try:
                    self.store.save(st, self.hardware.boot_id())
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the session could not be written: {exc}")
                self.stored_boot = self.hardware.boot_id()
            case StartWorker(token=token, spec=spec):
                try:
                    row = self.store.test_started(acting.session_id, token, spec, self.hardware.boot_id())
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the test row could not be written: {exc}")
                self.test_row = (token, row)
                self._start(token, spec)
            case Schedule(token=token, seconds=seconds):
                self.timer = (token, self.clock() + seconds)
            case CancelSchedule():
                self.timer = None
            case ForgetResident():
                self.resident = {}
            case ReadCrashEvidence():
                return self._crash_evidence(acting)
            case WriteStockMarker():
                self.store.write_marker(self.hardware.boot_id())
            case ClearStockMarker():
                self.store.clear_marker()
            case Announce(kind=kind, text=text):
                self.say(f"{kind}: {text}")
                try:
                    self.store.event(acting.session_id, kind, text, self.hardware.boot_id())
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the event could not be written: {exc}")
            case CloseDb():
                pass
        return None

    def _journal(self, acting: SessionState, core: int, value: int) -> int:
        return self.store.journal_intent(acting.session_id, core, value, self.hardware.boot_id(), time.monotonic_ns())

    def _end_test(self, token: int, write: Callable[[int], None]) -> DatabaseFailed | None:
        if self.test_row is None or self.test_row[0] != token:
            return None
        row = self.test_row[1]
        self.test_row = None
        try:
            write(row)
        except sqlite3.Error as exc:
            return DatabaseFailed(f"the test result could not be written: {exc}")
        return None

    def _crash_evidence(self, acting: SessionState) -> CrashEvidenceRead | DatabaseFailed:
        now = self.hardware.boot_id()
        last = self.stored_boot or now
        same = last == now
        dirty = not same and not self.hardware.boot_ended_clean(last)
        marker = self.store.marker_boot()
        session = acting.session_id
        try:
            loaded = self.store.load(session)
            rows = self.store.journal_rows(session, last)
            other = not same and self.store.taken_over(session, last)
            context = None if same else self.store.crash_context(session, last, self._core_of_cpu())
        except sqlite3.Error as exc:
            return DatabaseFailed(f"the session could not be read back: {exc}")
        stamps = [row.mono_ns for row in rows if row.mono_ns is not None]
        since = self.stored_read_ns if self.stored_read_ns is not None else min(stamps, default=None)
        harvest = self.hardware.harvest(last, since, not same)
        evidence, shared = self._kernel_evidence((*harvest.dead_boot, *harvest.next_boot))
        return CrashEvidenceRead(
            dirty_end=dirty,
            same_boot=same,
            was_running=loaded is not None and loaded[0].status == "running",
            stale_db=marker == (now if same else last),
            other_session_wrote=other,
            shared_uncorrected=shared,
            suspects=tuple(sorted(crash_suspects(resident_at_death(rows), -1))),
            context=context,
            evidence=evidence,
            kernel_problem=harvest.problem,
        )

    def _core_of_cpu(self) -> dict[int, int]:
        return {cpu: core for core, cpus in self.cpus_of for cpu in cpus}

    def _kernel_evidence(self, events) -> tuple[tuple[Evidence, ...], bool]:
        core_of_cpu = self._core_of_cpu()
        evidence = tuple(
            Evidence(core_of_cpu[e.cpu], e.corrected)
            for e in events
            if isinstance(e, kernel.CoreMachineCheck) and e.cpu in core_of_cpu
        )
        shared = any(
            not e.corrected
            and (
                isinstance(e, kernel.UnblamedMachineCheck)
                or (isinstance(e, kernel.CoreMachineCheck) and e.cpu not in core_of_cpu)
            )
            for e in events
        )
        return evidence, shared

    def _start(self, token: int, spec: TestSpec) -> None:
        load = "" if spec.load is None else f" {spec.load.value}"
        cpus = ",".join(str(cpu) for cpu in spec.cpus)
        self.say(
            f"test {token}: core {spec.core} {spec.stage.value} {spec.klass.value}{load} "
            f"at {spec.value} for {spec.seconds:g} s on cpus {cpus}"
        )
        stop = threading.Event()

        def work() -> None:
            try:
                report = self.runner.run(spec, stop, lambda sample: self.inbox.put(Sampled(token, sample)))
            except Exception as exc:
                self.inbox.put(InternalError(f"the test runner raised {exc!r}"))
                return
            self.inbox.put(WorkerReported(token, report))

        thread = threading.Thread(target=work, name=f"tuner-test-{token}", daemon=True)
        self.worker = _Worker(token, stop, thread)
        thread.start()

    def step(self, wait: float) -> bool:
        if self.signals:
            self.post(self.signals.pop(0))
            return True
        deadline = self.timer[1] if self.timer is not None else None
        timeout = wait if deadline is None else max(0.0, min(wait, deadline - self.clock()))
        try:
            event = self.inbox.get(timeout=timeout)
        except queue.Empty:
            if self.timer is not None and self.clock() >= self.timer[1]:
                token = self.timer[0]
                self.timer = None
                self.post(ContinuationDue(token))
                return True
            return False
        if isinstance(event, Sampled):
            if self.test_row is not None and self.test_row[0] == event.token:
                try:
                    self.store.sample(self.test_row[1], event.sample)
                except sqlite3.Error as exc:
                    self.post(DatabaseFailed(f"a telemetry sample could not be written: {exc}"))
            return True
        failed = None
        if isinstance(event, WorkerReported):
            if self.worker is not None and self.worker.token == event.token:
                self.worker = None
                report = event.report
                detail = f" {report.detail}" if report.detail else ""
                self.say(f"verdict {event.token}: {report.group.value} after {report.seconds:.0f} s{detail}")
            session, boot = self.state.session_id, self.hardware.boot_id()
            failed = self._end_test(event.token, lambda row: self.store.test_returned(session, boot, row, event.report))
        elif isinstance(event, InternalError) and self.test_row is not None:
            failed = self._end_test(self.test_row[0], lambda row: self.store.test_broken(row, event.error))
        self.post(event if failed is None else failed)
        return True

    def at_rest(self) -> bool:
        s = self.state
        if s.lifecycle in (Lifecycle.COMPLETED, Lifecycle.ABORTED, Lifecycle.QUARANTINED, Lifecycle.IDLE):
            return self.worker is None and self.timer is None
        return s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is not PauseReason.RESUMING

    def start(self, config: MachineConfig, session_id: int, seed: int, acknowledged: bool) -> None:
        self.post(Start(config, session_id, seed, acknowledged), self.world_for_start())

    def resume(self, session_id: int) -> None:
        self.post(Resume(session_id), self.world_for_resume(session_id))

    def run_until_rest(self, wait: float = 1.0) -> SessionState:
        while not self.at_rest():
            self.step(wait)
        return self.state


__all__ = ["Abort", "Close", "Hardware", "Identity", "LiveRig", "PauseRequest", "Runner", "Store"]
