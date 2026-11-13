from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from corecycler.config.paths import atomic_write, hand_back
from corecycler.engine import detector as kernel
from corecycler.engine.execution import Heartbeat
from corecycler.history.db import CoRead, CorruptSessionError
from corecycler.monitor.frequency import PowerState, cpu_ranges
from corecycler.tuner import telemetry
from corecycler.tuner.machine.bisect import resolve
from corecycler.tuner.machine.session import transition
from corecycler.tuner.machine.types import (
    ALLCORE,
    HELD_STATUSES,
    Abort,
    AllCore,
    Announce,
    AwaitWorker,
    BisectRun,
    CancelSchedule,
    Close,
    CloseDb,
    Commit,
    ContinuationDue,
    ContinueOnSetup,
    CoreRecord,
    CrashEvidenceRead,
    DatabaseFailed,
    Evidence,
    Flow,
    ForgetResident,
    Group,
    Incident,
    InternalError,
    KernelLogRead,
    Klass,
    Lifecycle,
    Live1,
    MachineConfig,
    MarkSurvived,
    Mode,
    PauseReason,
    PauseRequest,
    Phase,
    ReadCrashEvidence,
    Report,
    RestartOnSetup,
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
    Supersede,
    TestSpec,
    ValidateProfile,
    WorkerReported,
    World,
    WriteCO,
    WriteStockMarker,
)
from corecycler.tuner.orders import Method, RoundCursor

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from corecycler.history.db import HistoryDB, JournalRow


@dataclass(frozen=True, slots=True)
class BootFacts:
    bios_version: str
    fclk_mhz: float | None
    uclk_mhz: float | None
    mclk_mhz: float | None
    reset_reasons: tuple[str, ...]
    reset_problem: str | None
    igpu: dict


class Hardware(Protocol):
    def read_co(self, core: int) -> int | None: ...
    def write_co(self, core: int, value: int) -> str | None: ...
    def temperature(self) -> float | None: ...
    def power(self) -> PowerState: ...
    def preflight(self) -> tuple[str | None, tuple[str, ...]]: ...
    def revert_problem(self) -> str | None: ...
    def boot_id(self) -> str: ...
    def boot_end(self, boot_id: str) -> kernel.BootEnd: ...
    def boot_facts(self, boot_id: str) -> BootFacts: ...
    def setup(
        self, cpus_of: tuple[tuple[int, tuple[int, ...]], ...], domain_of: tuple[tuple[int, int | None], ...]
    ) -> tuple[tuple[str, str], ...]: ...
    def asleep_seconds(self) -> float: ...
    def harvest(
        self, boot_id: str, since_ns: int | None, next_boot: bool, through_ns: int | None
    ) -> kernel.JournalHarvest: ...


class Runner(Protocol):
    def run(self, spec: TestSpec, stop: threading.Event, sink: Callable[[telemetry.Sample], None]) -> Report: ...


def config_to_json(cfg: MachineConfig) -> dict:
    return {**asdict(cfg), "selected": list(cfg.selected)}


KNOB_TYPES = {"int": (int,), "float": (int, float), "bool": (bool,)}


def config_from_json(raw: dict) -> MachineConfig:
    cfg = MachineConfig(
        **{
            **raw,
            "mode": Mode(raw["mode"]),
            "method": Method(raw["method"]),
            "selected": tuple(raw["selected"]),
            "stage_seconds": _pairs(raw["stage_seconds"]),
            "starts": _pairs(raw.get("starts", ())),
        }
    )
    wrong = [
        f.name
        for f in fields(MachineConfig)
        if f.type in KNOB_TYPES
        and (
            not isinstance(getattr(cfg, f.name), KNOB_TYPES[f.type])
            or (f.type != "bool" and isinstance(getattr(cfg, f.name), bool))
        )
    ]
    if not all(isinstance(core, int) and not isinstance(core, bool) for core in cfg.selected):
        wrong.append("selected")
    if not all(
        len(pair) == 2 and isinstance(pair[0], str) and isinstance(pair[1], int | float) for pair in cfg.stage_seconds
    ):
        wrong.append("stage_seconds")
    if not all(
        len(pair) == 2 and all(isinstance(n, int) and not isinstance(n, bool) for n in pair) for pair in cfg.starts
    ):
        wrong.append("starts")
    if wrong:
        raise ValueError(f"wrong types for {', '.join(wrong)}")
    return cfg


MHZ_BIN = 100
TEMP_BIN = 5
STEP_CAP_S = 1.0


def coverage(samples: list[dict], ccd_of: dict[int, int | None]) -> dict[int, dict]:
    cells: dict[int, dict[tuple[int, int], float]] = {core: {} for core in ccd_of}
    unreadable = dict.fromkeys(ccd_of, 0.0)
    for sample, following in zip(samples, samples[1:], strict=False):
        if not sample["running"]:
            continue
        seconds = max(0.0, min((following["mono_ns"] - sample["mono_ns"]) / 1e9, STEP_CAP_S))
        clocks = dict(sample["clocks"])
        temps = dict(sample["tccd"])
        for core, ccd in ccd_of.items():
            mhz, temp = clocks.get(core), temps.get(ccd)
            if mhz is None or temp is None:
                unreadable[core] += seconds
                continue
            cell = (int(mhz // MHZ_BIN) * MHZ_BIN, int(temp // TEMP_BIN) * TEMP_BIN)
            cells[core][cell] = cells[core].get(cell, 0.0) + seconds
    return {
        core: {
            "cells": [[mhz, temp, round(seconds, 2)] for (mhz, temp), seconds in sorted(cells[core].items())],
            "unreadable_s": round(unreadable[core], 2),
        }
        for core in ccd_of
    }


ONE_CORE_STAGES = frozenset({Stage.SEARCH, Stage.LIVE1})
MARGIN_SIDES = {Group.PASS: "passes", Group.SILICON: "fails"}


class UnreadableEvidenceError(ValueError):
    pass


def _graded(test: dict) -> str:
    """A one-core test's group read stricter than the machine grades it: a record on the tested core fails it whatever
    the load said, where the machine fails only a pass; and an environment verdict with a corrected machine check no
    core is blamed for fails at its first occurrence, where the machine fails it only once it recurs at that core and
    value with no other core showing one. An evidence_json in no shape a build of this tool writes is refused naming
    the test, never read as no evidence."""
    raw = test["evidence_json"]
    try:
        evidence = json.loads(raw)
    except TypeError, ValueError:
        evidence = None
    if not (
        type(evidence) is dict
        and type(evidence.get("records")) is list
        and all(type(record) is dict and type(record.get("core")) is int for record in evidence["records"])
        and type(evidence.get("unblamed_corrected")) is bool
    ):
        raise UnreadableEvidenceError(
            f"test {test['id']} has evidence_json {raw!r}, in no shape a build of this tool writes"
        )
    own = any(record["core"] == test["core"] for record in evidence["records"])
    unblamed = test["verdict"] == Group.ENVIRONMENT and evidence["unblamed_corrected"]
    return Group.SILICON if own or unblamed else test["verdict"]


def margins(tests: list[dict]) -> dict[int, dict]:
    found: dict[int, dict] = {}
    for test in tests:
        if test["stage"] not in ONE_CORE_STAGES:
            continue
        core = found.setdefault(test["core"], {"bins": {}, "open": [], "unrecorded": 0})
        if test["ended_at"] is None:
            core["open"].append(test["value"])
            continue
        side = MARGIN_SIDES.get(_graded(test))
        if side is None:
            continue
        record = None if test["coverage_json"] is None else json.loads(test["coverage_json"]).get(str(test["core"]))
        if record is None:
            core["unrecorded"] += 1
            continue
        for mhz, temp, seconds in record["cells"]:
            seen = core["bins"].setdefault(mhz, {"passes": {}, "fails": {}, "temps": set()})
            seen[side][test["value"]] = seen[side].get(test["value"], 0.0) + seconds
            seen["temps"].add(temp)
    return {
        number: {**core, "bins": {mhz: _margin(seen) for mhz, seen in sorted(core["bins"].items())}}
        for number, core in sorted(found.items())
    }


def _margin(seen: dict) -> dict:
    failed = max(seen["fails"], default=None)
    passed = min((value for value in seen["passes"] if failed is None or value > failed), default=None)
    return {
        "passed": passed,
        "passed_s": round(seen["passes"].get(passed, 0.0), 1),
        "failed": failed,
        "failed_s": round(seen["fails"].get(failed, 0.0), 1),
        "deeper": sorted(value for value in seen["passes"] if failed is not None and value <= failed),
        "seconds": round(sum(seen["passes"].values()) + sum(seen["fails"].values()), 1),
        "temp": [min(seen["temps"]), max(seen["temps"]) + TEMP_BIN],
    }


def peaks(samples: list[dict]) -> dict[int, float]:
    highest: dict[int, float] = {}
    for sample in samples:
        for core, mhz in sample["clocks"]:
            if mhz is not None and mhz > highest.get(core, 0.0):
                highest[core] = mhz
    return dict(sorted(highest.items()))


def own_peaks(tests: list[dict]) -> list[dict]:
    highest: dict[tuple[int, float | None, str | None], float] = {}
    for test in tests:
        if test["stage"] not in ONE_CORE_STAGES or test["peaks_json"] is None:
            continue
        mhz = json.loads(test["peaks_json"]).get(str(test["core"]))
        if mhz is None:
            continue
        power = {} if test["power_json"] is None else json.loads(test["power_json"])
        key = (test["core"], _own_ceiling(power, json.loads(test["cpus_json"])), power.get("boost"))
        highest[key] = max(mhz, highest.get(key, mhz))
    return [
        {"core": core, "cap_mhz": cap, "boost": boost, "mhz": mhz}
        for (core, cap, boost), mhz in sorted(highest.items(), key=lambda item: item[0][0])
    ]


def _own_ceiling(power: dict, cpus: list[int]) -> float | None:
    if "ceilings" not in power:
        return power["max_mhz"] if "split" in power else None
    read = dict(power["ceilings"])
    own = [read.get(cpu) for cpu in cpus]
    return max(own) if own and None not in own else None


def _stored_from_row(row: tuple[str, str, int | None, str | None], session_id: int) -> Stored:
    return replace(stored_from_json(json.loads(row[0]), continued=row[3] == "continue"), session_id=session_id)


UNREADABLE_STORED = (AttributeError, LookupError, TypeError, ValueError)


def load_stored(db: HistoryDB, session_id: int) -> tuple[tuple[Stored, str, int | None] | None, str | None]:
    row = db.load_machine_session(session_id)
    if row is None:
        return None, None
    try:
        return (_stored_from_row(row, session_id), row[1], row[2]), None
    except UNREADABLE_STORED as exc:
        return None, f"session {session_id} could not be read: {exc!r}"


def read_stored(db: HistoryDB, session_id: int) -> tuple[Stored, None] | tuple[None, str]:
    loaded, unread = load_stored(db, session_id)
    if loaded is None:
        return None, unread or f"session {session_id} is not stored"
    return loaded[0], None


def _pairs(raw) -> tuple:
    return tuple(tuple(item) for item in raw)


def _groups(raw) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(group) for group in raw)


BISECT_ENTRIES = frozenset({Stage.LIVE1, Stage.MEMORY, Stage.STEADY, Stage.TRANSIENT, Stage.ENDURANCE, Stage.SOAK})
BISECT_GROUPS = "groups of the universe's cores, each in ascending order"


def _ascending(group: tuple, cores: tuple[int, ...]) -> bool:
    """Each member an int among cores and above the one before it; True equals 1 but names no core."""
    return all(type(core) is int and core in cores for core in group) and group == tuple(sorted(set(group)))


def _bisect_from_json(raw: dict | None, owned: tuple[int, ...]) -> BisectRun | None:
    """A stored bisection reads back exactly, or is refused naming the first field holding what begin and record never
    write: a value of the wrong type, a core outside the owned cores or the universe, a group recorded twice, a child
    not inside its parent or neither recorded nor queued, nothing left to test, or a replay of a group that is no failed
    leaf; so no relation among its own fields makes the state machine raise later or skip a group it owes. The leaves
    are read only once every field holds what it should."""
    if raw is None:
        return None
    entry = Stage(raw["entry"])
    run = BisectRun(
        entry=entry,
        lane=raw["lane"],
        klass=Klass(raw["klass"]),
        universe=tuple(raw["universe"]),
        values=_pairs(raw["values"]),
        queue=_groups(raw["queue"]),
        results=tuple((tuple(group), passed) for group, passed in raw["results"]),
        parents=tuple((tuple(child), tuple(parent)) for child, parent in raw["parents"]),
        rerun=None if raw["rerun"] is None else tuple(raw["rerun"]),
        quick=_quick_from_json(raw, entry),
    )
    universe = run.universe
    cores = tuple(core for core, _ in run.values)
    recorded = tuple(group for group, _ in run.results)
    rules = (
        ("entry", entry in BISECT_ENTRIES, "a stage a bisection begins in"),
        ("lane", type(run.lane) is int and run.lane in owned, f"one of the owned cores {list(owned)}"),
        (
            "universe",
            bool(universe) and _ascending(universe, owned),
            f"one or more of the owned cores {list(owned)} in ascending order",
        ),
        (
            "values",
            _ascending(cores, universe)
            and len(cores) == len(universe)
            and all(type(value) is int and value < 0 for _, value in run.values),
            "a value below 0 for each core of the universe in order",
        ),
        ("queue", all(_ascending(group, universe) for group in run.queue), BISECT_GROUPS),
        (
            "results",
            all(_ascending(group, universe) and type(passed) is bool for group, passed in run.results)
            and len(set(recorded)) == len(recorded),
            f"{BISECT_GROUPS}, each recorded once and passed true or false",
        ),
        (
            "parents",
            all(
                _ascending(child, universe)
                and _ascending(parent, universe)
                and set(child) < set(parent)
                and (child in recorded or child in run.queue)
                for child, parent in run.parents
            ),
            f"pairs of {BISECT_GROUPS}, each child inside its parent and recorded or queued",
        ),
        (
            "rerun",
            bool(run.queue) if run.rerun is None else _ascending(run.rerun, universe),
            "a group of the universe's cores in ascending order, or null while a group is queued",
        ),
    )
    for name, kept, rule in rules:
        if not kept:
            raise ValueError(f"the stored bisect's {name} is {raw[name]!r}, not {rule}")
    leaves = resolve(run)
    if run.rerun is not None and run.rerun not in leaves:
        listed = [list(leaf) for leaf in leaves]
        raise ValueError(f"the stored bisect's rerun is {raw['rerun']!r}, not one of its failed leaves {listed}")
    return run


def _quick_from_json(raw: dict, entry: Stage) -> bool:
    """A run stored without quick ran its smaller groups quick exactly when its entry was STEADY or ENDURANCE."""
    if "quick" not in raw:
        return entry in (Stage.STEADY, Stage.ENDURANCE)
    quick = raw["quick"]
    if not isinstance(quick, bool):
        raise ValueError(f"the stored bisect's quick is {quick!r}, not true or false")
    return quick


RETIRED_ALLCORE = frozenset({"t_types", "t_busy", "co_delta", "rung", "item"})


def _allcore_from_json(raw: dict | None) -> AllCore | None:
    if raw is None:
        return None
    kept = {name: value for name, value in raw.items() if name not in RETIRED_ALLCORE}
    return AllCore(**{**kept, "floors": _pairs(kept["floors"]), "frozen": tuple(kept["frozen"])})


def _live1_from_json(raw: dict) -> Live1:
    back = raw["back"]
    return Live1(raw["index"], tuple(raw["requeue"]), raw["solo"], None if back is None else Stage(back))


def _one_off_from_json(raw: list | None) -> tuple[Stage, tuple[tuple[int, int], ...]] | None:
    if raw is None:
        return None
    stage, held = raw
    return Stage(stage), _pairs(held)


def _unreplayed_from_json(raw: dict, cfg: MachineConfig) -> tuple[Stage, ...]:
    """A flow stored before the record existed owes no replay. An earlier build stored finalize for a validation that
    died in FINALIZE, which runs no test; it reads as LIVE1, what a dead boot there owes now."""
    stored = raw.get("unreplayed", [])
    malformed = f"the stored flow's unreplayed is {stored!r}, not a list of stages"
    if not isinstance(stored, list | tuple):
        raise ValueError(malformed)
    try:
        stages = tuple(Stage(stage) for stage in stored)
    except ValueError as exc:
        raise ValueError(malformed) from exc
    owed = tuple(Stage.LIVE1 if stage is Stage.FINALIZE else stage for stage in stages)
    if owed and raw["validating"] is None:
        raise ValueError(f"the stored flow's unreplayed is {stored!r}, but the flow validates no values")
    switches = ((Stage.ENDURANCE, cfg.endurance), (Stage.MEMORY, cfg.memory), (Stage.SOAK, cfg.soak))
    replayed = (Stage.LIVE1, *(stage for stage, on in switches if on))
    unrun = next((stage for stage in owed if stage not in replayed), None)
    if unrun is not None:
        names = ", ".join(stage.value for stage in replayed)
        raise ValueError(f"the stored flow's unreplayed is {stored!r}: a validation replays only {names}, not {unrun}")
    return owed


WRITTEN_VECTOR = "giving cores {} an integer value at or below 0, one of them below 0"


def _owned_vector(written, owned: tuple[int, ...]) -> tuple[tuple[int, int], ...]:
    vector = tuple((core, value) for core, value in written)
    if (
        tuple(core for core, _ in vector) != owned
        or any(type(core) is not int or type(value) is not int or value > 0 for core, value in vector)
        or not any(value for _, value in vector)
    ):
        raise ValueError(written)
    return vector


def _vectors_from_json(raw: dict, name: str, owned: tuple[int, ...]) -> tuple[tuple[tuple[int, int], ...], ...]:
    stored = raw.get(name, [])
    try:
        return tuple(_owned_vector(vector, owned) for vector in stored)
    except (TypeError, ValueError) as exc:
        each = WRITTEN_VECTOR.format(list(owned))
        raise ValueError(f"the stored session's {name} is {stored!r}, not a list of vectors each {each}") from exc


def _remembered_from_json(raw: dict, baseline: tuple[tuple[int, int], ...]) -> tuple:
    """A session stored before a record existed remembers no failed vector, has none cleared and none a re-test may
    still clear; a value the session could not have written refuses it naming the record (D117, D118)."""
    owned = tuple(core for core, _ in baseline)
    failed = _vectors_from_json(raw, "failed_vectors", owned)
    if len(set(failed)) < len(failed):
        raise ValueError(f"the stored session's failed_vectors is {raw['failed_vectors']!r}: each vector is named once")
    cleared = _vectors_from_json(raw, "cleared_vectors", owned)
    if len(set(cleared)) < len(cleared) or not set(cleared) <= set(failed):
        raise ValueError(
            f"the stored session's cleared_vectors is {raw['cleared_vectors']!r}: each must be a remembered failed "
            "vector, named once"
        )
    written = raw.get("clearable_vector")
    if written is None:
        return failed, cleared, None
    try:
        clearable = _owned_vector(written, owned)
    except (TypeError, ValueError) as exc:
        each = WRITTEN_VECTOR.format(list(owned))
        raise ValueError(f"the stored session's clearable_vector is {written!r}, not null or a vector {each}") from exc
    if clearable not in failed or clearable in cleared:
        raise ValueError(
            f"the stored session's clearable_vector is {written!r}, not a remembered failed vector still uncleared"
        )
    return failed, cleared, clearable


def _flow_from_json(raw: dict | None, continued: bool, cfg: MachineConfig) -> Flow | None:
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
        bisect=_bisect_from_json(raw["bisect"], cfg.selected),
        q5=tuple(((key[0], key[1]), count) for key, count in raw["q5"]),
        q5_others=tuple(raw["q5_others"]),
        allcore=_allcore_from_json(raw["allcore"]),
        validating=None if raw["validating"] is None else _pairs(raw["validating"]),
        one_off=_one_off_from_json(raw.get("one_off")),
        carried=raw.get("carried", continued),
        unreplayed=_unreplayed_from_json(raw, cfg),
    )


def stored_to_json(st: Stored) -> dict:
    out = asdict(st)
    out["config"] = config_to_json(st.config)
    return out


def stored_from_json(raw: dict, *, continued: bool) -> Stored:
    baseline = _pairs(raw["baseline"])
    config = config_from_json(raw["config"])
    failed, cleared, clearable = _remembered_from_json(raw, baseline)
    return Stored(
        session_id=raw["session_id"],
        status=raw["status"],
        config=config,
        seed=raw["seed"],
        baseline=baseline,
        cores=tuple(CoreRecord(**{**core, "phase": Phase(core["phase"])}) for core in raw["cores"]),
        flow=_flow_from_json(raw["flow"], continued, config),
        resume_streak=raw["resume_streak"],
        incidents=_incidents(raw["incidents"]),
        power=tuple((name, value) for name, value in raw["power"]),
        setup=tuple((name, value) for name, value in raw["setup"]),
        base_co=tuple((core, value) for core, value in raw["base_co"]),
        failed_vectors=failed,
        cleared_vectors=cleared,
        clearable_vector=clearable,
    )


def _incidents(raw: list[str] | int) -> tuple[Incident, ...]:
    if not isinstance(raw, int):
        return tuple(Incident(kind) for kind in raw)
    if raw < 0:
        raise ValueError(f"the stored incidents count {raw} is negative")
    return (Incident.UNRECORDED,) * raw


@dataclass(frozen=True, slots=True)
class Identity:
    bios_version: str
    cpu_model: str
    context_id: int | None


PASSED_SAMPLES_KEPT_DAYS = 30
SLEEP_GAP_S = 1.0
MARKER_SHAPES = (
    frozenset({"boot"}),
    frozenset({"boot", "mono_ns", "cores"}),
    frozenset({"boot", "session", "mono_ns", "cores"}),
)


class StockMarkerError(Exception):
    pass


class Store:
    def __init__(self, db: HistoryDB, root: Path, identity: Identity) -> None:
        self.db = db
        self.root = root
        self.identity = identity
        self.retracted: dict[tuple[int, int], int] = {}
        self.unwritten_row: int | None = None
        self.unwritten: list[dict] = []
        root.mkdir(parents=True, exist_ok=True)
        hand_back(root)
        kept_from = datetime.now(UTC) - timedelta(days=PASSED_SAMPLES_KEPT_DAYS)
        db.prune_tuner_samples(Group.PASS.value, kept_from.isoformat())

    def new_session_id(self) -> int:
        return self.db.next_tuner_session_id()

    def held_session(self) -> tuple[int, str] | None:
        newest = self.db.list_machine_sessions(limit=1)  # equivalent-mutant: only the first row is read
        return (newest[0].id, newest[0].status) if newest and newest[0].status in HELD_STATUSES else None

    def newest_base(self) -> tuple[tuple[int, tuple[tuple[int, int | None], ...]] | None, str | None]:
        newest = self.db.list_machine_sessions(limit=1)  # equivalent-mutant: only the first row is read
        if not newest:
            return None, None
        stored, unread = read_stored(self.db, newest[0].id)
        return (None, unread) if stored is None else ((newest[0].id, stored.base_co), None)

    def completed(self) -> tuple[tuple[Stored, ...], str | None]:
        found: list[Stored] = []
        for session in self.db.list_machine_sessions():
            if session.status != "completed":
                continue
            stored, unread = read_stored(self.db, session.id)
            if stored is None:
                return tuple(found), unread
            found.append(stored)
        return tuple(found), None

    def save(
        self,
        st: Stored,
        boot: str,
        kernel_read_ns: int | None = None,
        co_reads: tuple[tuple[int, tuple[tuple[int, int | None], ...]], ...] = (),
    ) -> None:
        tightened = {
            (st.session_id, r.core): r.fb
            for r in st.cores
            if r.fb is not None and self.retracted.get((st.session_id, r.core)) != r.fb
        }
        with self.db.transaction():
            self._write(st, boot, tightened)
            if kernel_read_ns is not None:
                self.db.set_kernel_read(st.session_id, kernel_read_ns)
            for mono_ns, reads in co_reads:
                self.db.insert_co_reads(st.session_id, boot, mono_ns, reads)
        self.retracted.update(tightened)

    def supersede(
        self,
        parent: Stored,
        child: Stored,
        origin: str,
        boot: str,
        co_reads: tuple[tuple[int, tuple[tuple[int, int | None], ...]], ...],
    ) -> None:
        proven = self.db.journal_proven_values(parent.session_id, -1) if origin == "continue" else {}
        with self.db.transaction():
            self._write(parent, boot, {})
            self._write(child, boot, {})
            for mono_ns, reads in co_reads:
                self.db.insert_co_reads(parent.session_id, boot, mono_ns, reads)
            self.db.link_session(child.session_id, parent.session_id, origin)
            self.db.journal_inherit(child.session_id, proven)
            self.db.insert_tuner_event(
                parent.session_id, f"superseded by session {child.session_id} ({origin})", boot, "superseded"
            )

    def _write(self, st: Stored, boot: str, tightened: dict[tuple[int, int], int]) -> None:
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

    def load(self, session_id: int) -> tuple[tuple[Stored, str, int | None] | None, str | None]:
        return load_stored(self.db, session_id)

    def test_started(self, session_id: int, token: int, spec: TestSpec, boot: str, power: PowerState) -> int:
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
            json.dumps(asdict(power)),
        )

    def mark_survived(self, session_id: int, boot: str, exclude: tuple[int, ...]) -> None:
        self.db.journal_mark_survived(session_id, boot_id=boot, exclude_cores=exclude)

    def test_returned(self, boot: str, row: int, report: Report, ccd_of: dict[int, int | None]) -> None:
        faults = report.program_faults
        evidence = {
            "records": [asdict(e) for e in report.evidence],
            "unblamed_corrected": report.unblamed_corrected,
            "program_faults": None
            if faults is None
            else {
                "started_ns": faults.started_ns,
                "started_clock": "CLOCK_MONOTONIC",
                "faults": [asdict(fault) for fault in faults.kept],
                "dropped": faults.dropped,
            },
        }
        self.write_block()
        samples = self.db.tuner_test_samples(row)
        self.db.end_tuner_test(
            row,
            report.group.value,
            report.seconds,
            json.dumps(report.named),
            None if report.context is None else json.dumps(asdict(report.context)),
            json.dumps(evidence),
            report.detail,
            json.dumps(report.speeds),
            report.output,
            peaks_json=json.dumps(peaks(samples)),
            coverage_json=json.dumps(coverage(samples, ccd_of)),
        )
        self.db.checkpoint()

    def boot_seen(self, boot: str, facts: BootFacts) -> None:
        self.db.insert_tuner_boot(
            boot,
            facts.bios_version,
            facts.fclk_mhz,
            facts.uclk_mhz,
            facts.mclk_mhz,
            json.dumps({"lines": list(facts.reset_reasons), "problem": facts.reset_problem}),
            json.dumps(facts.igpu),
        )

    def journal_rows(self, session_id: int, boot: str) -> list[JournalRow]:
        return self.db.journal_rows(session_id, boot)

    def observed(self, boot: str) -> list[JournalRow | CoRead]:
        reads = sorted(self.db.co_reads(boot), key=lambda read: read.mono_ns)
        since_boot = {read.core_id: replace(read, mono_ns=0) for read in reversed(reads)}
        stock = self.stock_marked(boot)
        marked = () if stock is None else tuple(CoRead(core, value, stock[0]) for core, value in stock[1])
        return [*self.db.boot_co_journal(boot), *reads, *since_boot.values(), *marked]

    def taken_over(self, session_id: int, boot: str) -> bool:
        return self.db.journal_taken_over(session_id, boot) is not None

    def sample(self, row: int, sample: telemetry.Sample, durable: bool) -> None:
        if self.unwritten_row != row:
            self.write_block()
            self.unwritten_row = row
        taken = {
            "mono_ns": sample.mono_ns,
            "running": sample.running,
            "tctl": sample.tctl,
            "clocks": sample.mhz,
            "tccd": sample.tccd,
            "volts": sample.volts,
            "watts": sample.watts,
            "cores": sample.cores,
        }
        self.unwritten.append(taken)
        if len(self.unwritten) >= telemetry.BLOCK:
            self.write_block()
        elif durable and len(self.unwritten) % telemetry.TAIL_GROUP == 0:
            self.db.insert_tuner_samples(row, self.unwritten[-telemetry.TAIL_GROUP :])

    def write_block(self) -> None:
        if self.unwritten:
            self.db.insert_tuner_block(self.unwritten_row, self.unwritten)
            self.unwritten = []

    def pack_dead_tail(self, session_id: int, boot: str) -> None:
        test = self.db.open_tuner_test(session_id, boot)
        if test is not None:
            self.db.pack_tuner_tail(test["id"])

    def test_broken(self, row: int, detail: str) -> None:
        evidence = {"records": [], "unblamed_corrected": False}
        self.write_block()
        self.db.end_tuner_test(row, "internal_error", None, "[]", None, json.dumps(evidence), detail, "[]", "")
        self.db.checkpoint()

    def event(self, session_id: int, kind: str, text: str, boot: str) -> None:
        self.db.insert_tuner_event(session_id, text, boot, kind)

    def told(self, session_id: int, kind: str) -> set[str]:
        return self.db.tuner_event_messages(session_id, kind)

    def journal_intent(self, session_id: int, core: int, value: int, boot: str, mono_ns: int) -> int:
        envelope = self.db.journal_envelope(session_id, core)
        survived = value == 0 or (envelope is not None and value >= envelope)
        return self.db.journal_co_intent(session_id, core, value, survived, boot_id=boot, mono_ns=mono_ns)

    def journal_confirm(self, row: int) -> None:
        self.db.journal_confirm(row)

    def write_marker(self, boot: str, session_id: int, mono_ns: int, cores: tuple[int, ...]) -> None:
        marker = {"boot": boot, "session": session_id, "mono_ns": mono_ns, "cores": list(cores)}
        atomic_write(self.root / "stock-marker.json", json.dumps(marker))

    def _marker(self) -> dict | None:
        """The stock marker, or None when there is none. A file that cannot be read, or in no shape a build of this
        tool writes by its JSON or its fields, is refused naming it, so nothing reads it as a marker."""
        path = self.root / "stock-marker.json"
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StockMarkerError(f"the stock marker {path} could not be read: {exc}") from exc
        try:
            marker = json.loads(raw)
        except ValueError:
            marker = None
        if not (
            isinstance(marker, dict)
            and set(marker) in MARKER_SHAPES
            and type(marker["boot"]) is str
            and all(type(marker[name]) is int for name in ("session", "mono_ns") if name in marker)
            and type(marker.get("cores", [])) is list
            and all(type(core) is int for core in marker.get("cores", []))
        ):
            raise StockMarkerError(f"the stock marker {path} holds {raw!r}, in no shape a build of this tool writes")
        return marker

    def _names(self, marker: dict, session_id: int) -> bool:
        if "session" in marker:
            return marker["session"] == session_id
        held = self.held_session()
        rows = self.db.boot_co_journal(marker["boot"])
        last = rows[-1] if rows else None
        wrote_last = held is not None and held[0] == session_id and last is not None and last.session_id == session_id
        return wrote_last and "mono_ns" in marker and last.mono_ns <= marker["mono_ns"]

    def marker_boot(self, session_id: int) -> tuple[str | None, bool]:
        """The boot the stock marker names, and whether it is this session's. A marker an earlier build wrote names no
        session: it is the session's that is held and wrote the CO last in that boot, at or before the marker."""
        marker = self._marker()
        return (None, False) if marker is None else (marker["boot"], self._names(marker, session_id))

    def stock_marked(self, boot: str) -> tuple[int, tuple[tuple[int, int], ...]] | None:
        marker = self._marker()
        if marker is None or marker["boot"] != boot or "mono_ns" not in marker:
            return None
        return marker["mono_ns"], tuple((core, 0) for core in marker["cores"])

    def clear_marker(self, *sessions: int) -> None:
        """Delete the stock marker when it names one of these sessions, whose stored state has just landed."""
        marker = self._marker()
        if marker is not None and any(self._names(marker, session) for session in sessions):
            path = self.root / "stock-marker.json"
            try:
                path.unlink()
            except OSError as exc:
                raise StockMarkerError(f"the stock marker {path} could not be deleted: {exc}") from exc


@dataclass(frozen=True, slots=True)
class Resident:
    last: JournalRow
    confirmed_before: JournalRow | None


def resident_at_death(rows: Iterable[JournalRow]) -> dict[int, Resident]:
    last: dict[int, JournalRow] = {}
    landed: dict[int, JournalRow] = {}
    for row in sorted(rows, key=lambda row: row.id):
        last[row.core_id] = row
        if row.confirmed:
            landed[row.core_id] = row
    return {core_id: Resident(row, None if row.confirmed else landed.get(core_id)) for core_id, row in last.items()}


def crash_suspects(resident: dict[int, Resident], direction: int) -> dict[int, int]:
    suspects: dict[int, int] = {}
    for core_id, held in resident.items():
        unproven = [
            row.value
            for row in (held.last, held.confirmed_before)
            if row is not None and row.value != 0 and (not row.survived or row.retracted)
        ]
        if unproven:
            suspects[core_id] = min(unproven, key=lambda value: direction * value)
    return suspects


def _unproven(rows: list[JournalRow]) -> tuple[int, ...]:
    return tuple(sorted(crash_suspects(resident_at_death(rows), -1)))


def charged_value(rows: list[JournalRow | CoRead], core: int, logged_ns: int, window_s: int) -> int | None:
    start = logged_ns - window_s * 1_000_000_000
    written = sorted(
        (row for row in rows if row.core_id == core and row.mono_ns is not None and row.mono_ns <= logged_ns),
        key=lambda row: row.mono_ns,
    )
    covered = sum(row.mono_ns <= start for row in written)
    held = written[covered - 1 :]
    if not covered or not all(row.confirmed for row in held):
        return None
    return next((row.value for row in reversed(held) if row.value != 0), 0)


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
    physical_ccd_of: tuple[tuple[int, int | None], ...]
    co_range: tuple[int, int] | None
    two_t_problem: str | None = None
    tjmax_c: float | None = None
    say: Callable[[str], None] = print
    clock: Callable[[], float] = time.monotonic
    check_interval_s: int = kernel.MCE_POLL_KERNEL_DEFAULT_S
    heartbeat: Heartbeat = field(default_factory=Heartbeat)
    state: SessionState = field(init=False, default_factory=SessionState)
    resident: dict[int, int | None] = field(init=False, default_factory=dict)
    timer: tuple[int, float] | None = field(init=False, default=None)
    worker: _Worker | None = field(init=False, default=None)
    inbox: queue.Queue = field(init=False, default_factory=queue.Queue)
    signals: list = field(init=False, default_factory=list)
    asleep_at_worker_start: float = field(init=False, default=0.0)
    stored_boot: str | None = field(init=False, default=None)
    stored_read_ns: int | None = field(init=False, default=None)
    read_to: dict[int, int | None] = field(
        init=False,  # equivalent-mutant: init only lets a caller pass the points, and no caller does
        default_factory=dict,
    )
    unread_from: dict[int, int | None] = field(
        init=False,  # equivalent-mutant: init only lets a caller pass the points, and no caller does
        default_factory=dict,
    )
    reads_to: dict[int, tuple[tuple[int, tuple[tuple[int, int | None], ...]], ...]] = field(
        init=False,  # equivalent-mutant: init only lets a caller pass the reads, and no caller does
        default_factory=dict,
    )
    test_row: tuple[int, int, bool] | None = field(init=False, default=None)
    boot_recorded: bool = field(init=False, default=False)
    refused: str | None = field(init=False, default=None)
    abort_taken: bool = field(
        init=False,  # equivalent-mutant: init only lets a caller pass the flag, and no caller does
        default=False,
    )
    boot: str = field(init=False)
    setup: tuple[tuple[str, str], ...] = field(init=False)
    power: PowerState = field(init=False, default_factory=PowerState)

    def __post_init__(self) -> None:
        self.boot = self.hardware.boot_id()
        self.setup = self.hardware.setup(self.cpus_of, self.domain_of)

    def world(self) -> World:
        self.power = self.hardware.power()
        gap = self.hardware.asleep_seconds() - self.asleep_at_worker_start > SLEEP_GAP_S
        return World(
            resident=tuple(sorted(self.resident.items())),
            gap_grew=gap,
            power=self.power.named(),
            setup=self.setup,
            now=self.clock(),
            temperature_c=self.hardware.temperature(),
            cpus_of=self.cpus_of,
            domain_of=self.domain_of,
            two_t_problem=self.two_t_problem,
            tjmax_c=self.tjmax_c,
            co_range=self.co_range,
        )

    def world_checked(self) -> World:
        refusal, warnings = self.hardware.preflight()
        return replace(self.world(), preflight_refusal=refusal, preflight_warnings=warnings)

    def co_reads(self) -> tuple[tuple[int, int | None], ...]:
        return tuple((core, self.hardware.read_co(core)) for core, _ in self.cpus_of)

    def world_for_start(self) -> World:
        completed, unread = self.store.completed()
        newest_base, newest_unread = self.store.newest_base()
        return replace(
            self.world_checked(),
            bios_co=self.co_reads(),
            held_session=self.store.held_session(),
            newest_base=newest_base,
            newest_unread=newest_unread,
            completed=completed,
            completed_unread=unread,
        )

    def world_for_resume(self, session_id: int, world: World) -> World:
        loaded, unread = self.store.load(session_id)
        if loaded is None:
            return replace(world, stored_unread=unread)
        stored, self.stored_boot, self.stored_read_ns = loaded
        proven = self.store.db.journal_proven(session_id) if stored.status == "quarantined" else ()
        return replace(world, stored=stored, proven=proven)

    def post(self, event, world: World | None = None) -> None:
        pending = [event]
        while pending:
            ev = pending.pop(0)
            before = self.state
            result = transition(before, ev, world if world is not None and ev is event else self.world())
            if result.refused is not None:
                self.refused = result.refused
                self.say(f"refused: {result.refused}")
            elif isinstance(ev, Abort):
                self.abort_taken = True
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
                refused: list[str] = []
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
                        refused.append(
                            f"core {core} could not be written back to {value} ({problem}); its value is unknown"
                        )
                    elif row is not None:
                        try:
                            self.store.journal_confirm(row)
                        except sqlite3.Error as exc:
                            failed = exc
                if refused:
                    self.say("revert_failed: " + "; ".join(refused))
                    if journal and failed is None:
                        try:
                            self.store.event(acting.session_id, "revert_failed", "; ".join(refused), self.boot)
                        except sqlite3.Error as exc:
                            return DatabaseFailed(f"the failed revert could not be recorded: {exc}")
                if failed is not None:
                    return DatabaseFailed(f"the CO journal failed during a revert: {failed}")
            case StopWorker(token=token):
                if self.worker is not None and self.worker.token == token:
                    self.worker.stop.set()
            case AwaitWorker(token=token, bound_s=bound):
                if self.worker is not None and self.worker.token == token:
                    self.worker.thread.join(bound)
                    if self.worker.thread.is_alive():
                        self.say(f"test {token} did not stop within {bound:g} s; its worker is kept until it ends")
                    else:
                        self.worker = None
                        self.heartbeat.between(self.clock())
            case Commit(stored=st, first=first):
                try:
                    self.store.save(
                        st, self.boot, self.read_to.get(st.session_id), self.reads_to.get(st.session_id, ())
                    )
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the session could not be written: {exc}", first)
                self.stored_boot = self.boot
                self.read_to.pop(st.session_id, None)
                self.reads_to.pop(st.session_id, None)
                return self._landed(st.session_id)
            case Supersede(parent=parent, child=child, origin=origin):
                try:
                    reads = self.reads_to.get(parent.session_id, ())
                    self.store.supersede(parent, child, origin, self.boot, reads)
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"session {parent.session_id} could not be superseded: {exc}")
                self.stored_boot = self.boot
                return self._landed(parent.session_id, child.session_id)
            case StartWorker(token=token, spec=spec):
                if not self.boot_recorded:
                    try:
                        self.store.boot_seen(self.boot, self.hardware.boot_facts(self.boot))
                    except sqlite3.Error as exc:
                        return DatabaseFailed(f"this boot's facts could not be written: {exc}")
                    self.boot_recorded = True
                try:
                    row = self.store.test_started(acting.session_id, token, spec, self.boot, self.power)
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the test row could not be written: {exc}")
                self.test_row = (token, row, spec.stage in ALLCORE)
                self.asleep_at_worker_start = self.hardware.asleep_seconds()
                self._start(token, spec)
            case Schedule(token=token, seconds=seconds):
                self.timer = (token, self.clock() + seconds)
            case CancelSchedule():
                self.timer = None
            case ForgetResident():
                self.resident = {}
            case MarkSurvived(exclude=exclude):
                try:
                    self.store.mark_survived(acting.session_id, self.boot, exclude)
                except sqlite3.Error as exc:
                    return DatabaseFailed(f"the survival mark could not be written: {exc}")
            case ReadCrashEvidence(child_id=child_id):
                return self._crash_evidence(acting, child_id)
            case WriteStockMarker():
                session, stocked = acting.session_id, time.monotonic_ns()
                cores = tuple(core for core, _ in acting.baseline)
                self.reads_to[session] = (
                    *self.reads_to.get(session, ()),
                    (stocked, tuple((core, 0) for core in cores)),
                )
                try:
                    self.store.write_marker(self.boot, session, stocked, cores)
                except OSError as exc:
                    self.say(
                        "the stock marker could not be written, so the cores go to stock without it and a resume "
                        f"will not know the database fell behind: {exc}"
                    )
            case Announce(kind=kind, text=text):
                self.say(f"{kind}: {text}")
                try:
                    self.store.event(acting.session_id, kind, text, self.boot)
                except sqlite3.Error as exc:
                    if kind == "database_failed":
                        self.say(f"the database failure could not be recorded: {exc}")
                    else:
                        return DatabaseFailed(f"the event could not be written: {exc}")
            case CloseDb():
                pass
        return None

    def _landed(self, *sessions: int) -> DatabaseFailed | None:
        """A session whose state has just been stored is no longer behind it, so a stock marker naming it goes; a store
        in the marker's own boot carried the marker's moment as the session's CO reads in that same transaction."""
        try:
            self.store.clear_marker(*sessions)
        except (sqlite3.Error, CorruptSessionError, StockMarkerError) as exc:
            return DatabaseFailed(f"the stock marker could not be settled once the session was stored: {exc}")

    def _journal(self, acting: SessionState, core: int, value: int) -> int:
        return self.store.journal_intent(acting.session_id, core, value, self.boot, time.monotonic_ns())

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

    def _crash_evidence(self, acting: SessionState, child_id: int | None) -> CrashEvidenceRead | DatabaseFailed:
        self.read_to.clear()
        now = self.boot
        last = self.stored_boot or now
        same = last == now
        end = None if same else self.hardware.boot_end(last)
        dirty = end is not None and not end.clean
        session = acting.session_id
        try:
            named, owned = self.store.marker_boot(session)
            stock = self.store.stock_marked(now)
            loaded, unread = self.store.load(session)
            if unread is not None:
                return DatabaseFailed(unread)
            rows = self.store.journal_rows(session, last)
            other = not same and self.store.taken_over(session, last)
            if not same:
                self.store.pack_dead_tail(session, last)
            told = self.store.told(session, "program_fault")
        except (sqlite3.Error, CorruptSessionError, StockMarkerError) as exc:
            return DatabaseFailed(f"the session could not be read back: {exc}")
        marker = named if owned else None
        if named in (now, last) and not owned:
            self.say(
                f"the stock marker for boot {named} is not taken as session {session}'s, so this resume does not read "
                "that boot as one whose database fell behind"
            )
        stamps = [row.mono_ns for row in rows if row.mono_ns is not None]
        since = self.stored_read_ns if self.stored_read_ns is not None else min(stamps, default=None)
        through = time.monotonic_ns() if same else None
        harvest = self.hardware.harvest(last, since, not same, through)
        harvested = (*harvest.dead_boot, *harvest.next_boot)
        try:
            observed = self.store.observed(last) if self._records(harvest.dead_boot) else None
        except (sqlite3.Error, CorruptSessionError, StockMarkerError) as exc:
            return DatabaseFailed(f"the session could not be read back: {exc}")
        if same:
            self.read_to[session] = self.unread_from[session] = (
                self._read_up_to(harvest, through) if harvest.problem is None else since
            )
        placed, shared = self._kernel_evidence(harvest.dead_boot, observed)
        later, shared_later = self._kernel_evidence(harvest.next_boot, None)
        evidence, shared = (*placed, *later), shared or shared_later
        co_now = self.co_reads()
        self.reads_to[session] = (*(() if stock is None else (stock,)), (time.monotonic_ns(), co_now))
        return CrashEvidenceRead(
            dirty_end=dirty,
            end_unread=end.problem if dirty else None,
            same_boot=same,
            was_running=loaded is not None and loaded[0].status == "running",
            stale_db=marker == (now if same else last),
            other_session_wrote=other,
            shared_uncorrected=shared,
            suspects=_unproven(rows),
            evidence=evidence,
            kernel_problem=harvest.problem,
            co_now=co_now,
            last_written=tuple(sorted({row.core_id: row.value for row in rows if row.confirmed}.items()))
            if same
            else (),
            marked_now=marker == now,
            child_id=child_id,
            program_faults=self._untold_faults(harvested, told),
        )

    @staticmethod
    def _untold_faults(harvested, told: set[str]) -> tuple[str, ...]:
        lines = [
            f"{fault.named}, {fault.raw_ts:.1f} s into its boot: {fault.message}"
            for fault in harvested
            if isinstance(fault, kernel.ProgramFault)
        ]
        last = max((index for index, line in enumerate(lines) if line in told), default=-1)
        kept, dropped = kernel.kept_faults(lines[last + 1 :])
        earlier = (f"{dropped} earlier program faults in that kernel log were not recorded",) if dropped else ()
        return (*earlier, *kept)

    def _core_of_cpu(self) -> dict[int, int]:
        return {cpu: core for core, cpus in self.cpus_of for cpu in cpus}

    def _kernel_evidence(self, events, rows: list[JournalRow | CoRead] | None) -> tuple[tuple[Evidence, ...], bool]:
        core_of_cpu = self._core_of_cpu()
        evidence = tuple(
            Evidence(
                core_of_cpu[e.cpu],
                e.corrected,
                None
                if rows is None
                else charged_value(
                    rows,
                    core_of_cpu[e.cpu],
                    kernel.stamp_ns(e.raw_ts),
                    self.check_interval_s + kernel.MCE_TIMER_ROUNDING_S,
                ),
            )
            for e in self._records(events)
        )
        shared = any(
            (
                isinstance(e, kernel.UnblamedMachineCheck)
                or (isinstance(e, kernel.CoreMachineCheck) and e.cpu not in core_of_cpu)
            )
            and not e.corrected
            for e in events
        )
        return evidence, shared

    def _records(self, events) -> list[kernel.CoreMachineCheck]:
        core_of_cpu = self._core_of_cpu()
        return [e for e in events if isinstance(e, kernel.CoreMachineCheck) and e.cpu in core_of_cpu]

    @staticmethod
    def _read_up_to(read: kernel.JournalHarvest, through: int) -> int:
        return through if read.unfinished_from_ns is None else read.unfinished_from_ns - 1

    def _read_since_resume(self, token: int) -> KernelLogRead | DatabaseFailed:
        session = self.state.session_id
        since = self.unread_from.pop(session)
        through = time.monotonic_ns()
        read = self.hardware.harvest(self.boot, since, False, through)
        if read.problem is not None:
            return KernelLogRead(token, problem=read.problem)
        try:
            rows = self.store.observed(self.boot) if self._records(read.dead_boot) else None
            told = self.store.told(session, "program_fault")
        except (sqlite3.Error, CorruptSessionError, StockMarkerError) as exc:
            return DatabaseFailed(f"the CO journal could not be read back: {exc}")
        self.read_to[session] = self._read_up_to(read, through)
        evidence = self._kernel_evidence(read.dead_boot, rows)[0]
        return KernelLogRead(token, evidence, program_faults=self._untold_faults(read.dead_boot, told))

    def _start(self, token: int, spec: TestSpec) -> None:
        load = "" if spec.load is None else f" {spec.load.value}"
        if spec.stage in ONE_CORE_STAGES:
            scope = f"core {spec.core} {spec.stage.value} {spec.klass.value}{load} at {spec.value}"
        else:
            scope = f"{spec.stage.value}{load}, cores {', '.join(f'{core} at {value}' for core, value in spec.vector)}"
        where = f"on cpus {cpu_ranges(list(spec.cpus))}" if spec.cpus else "with no cpus loaded"
        self.say(f"test {token}: {scope} for {spec.seconds:g} s {where}")
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
        if self.worker is None or not self.worker.thread.is_alive():
            self.heartbeat.between(self.clock())
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
                if self.state.session_id in self.unread_from:
                    self.post(self._read_since_resume(token))
                else:
                    self.post(ContinuationDue(token), replace(self.world(), co_now=self.co_reads()))
                return True
            return False
        if isinstance(event, Sampled):
            if self.test_row is not None and self.test_row[0] == event.token:
                try:
                    self.store.sample(self.test_row[1], event.sample, self.test_row[2])
                except sqlite3.Error as exc:
                    self.post(DatabaseFailed(f"a telemetry sample could not be written: {exc}"))
            return True
        failed = None
        if isinstance(event, WorkerReported):
            if self.test_row is not None and self.test_row[0] == event.token:
                self.worker = None
                self.heartbeat.between(self.clock())
                event = self.slept_through(event)
                report = event.report
                detail = f" {report.detail}" if report.detail else ""
                self.say(f"verdict {event.token}: {report.group.value} after {report.seconds:.0f} s{detail}")
                self._show_program_faults(event.token, report.program_faults)
            session, boot = self.state.session_id, self.boot

            def returned(row: int) -> None:
                self.store.test_returned(boot, row, event.report, dict(self.physical_ccd_of))
                self.read_to[session] = event.report.kernel_read_ns

            failed = self._end_test(event.token, returned)
        elif isinstance(event, InternalError) and self.test_row is not None:
            failed = self._end_test(self.test_row[0], lambda row: self.store.test_broken(row, event.error))
        self.post(event if failed is None else failed)
        if isinstance(event, WorkerReported):
            self.read_to.pop(session, None)
        return True

    def _show_program_faults(self, token: int, faults: kernel.ProgramFaults | None) -> None:
        if faults is None:
            return
        if faults.dropped:
            self.say(f"program_fault: {faults.dropped} earlier program faults in test {token} were not kept")
        for fault in faults.kept:
            into = fault.raw_ts - faults.started_ns / 1e9
            self.say(f"program_fault: {fault.named}, about {into:.1f} s into test {token}: {fault.message}")

    def slept_through(self, event: WorkerReported) -> WorkerReported:
        slept = self.hardware.asleep_seconds() - self.asleep_at_worker_start
        if slept <= SLEEP_GAP_S or event.report.group is Group.SLEPT:
            return event
        said = f"the machine slept {slept:.0f} s during the test"
        detail = f"{said}; {event.report.detail}" if event.report.detail else said
        return replace(event, report=replace(event.report, group=Group.SLEPT, detail=detail))

    def at_rest(self) -> bool:
        s = self.state
        if s.lifecycle in (Lifecycle.COMPLETED, Lifecycle.ABORTED, Lifecycle.QUARANTINED, Lifecycle.IDLE):
            return self.worker is None and self.timer is None
        unread = s.pause is not None and s.pause.cut is not None and self.worker is None and not self.inbox.empty()
        return (
            s.lifecycle is Lifecycle.PAUSED
            and s.pause is not None
            and s.pause.reason is not PauseReason.RESUMING
            and not unread
        )

    def start(self, config: MachineConfig, session_id: int, seed: int, acknowledged: bool) -> None:
        world = self.world_for_start()
        self.reads_to = {session_id: ((time.monotonic_ns(), world.bios_co),)}
        self.post(Start(config, session_id, seed, acknowledged), world)

    def resume(self, session_id: int, reengage: bool = False, carry: bool = False) -> None:
        child_id = self.store.new_session_id() if carry else None
        self.post(Resume(session_id, reengage, child_id), self.world_for_resume(session_id, self.world_checked()))

    def resume_for_abort(self, session_id: int, reengage: bool) -> bool:
        """Resume a stored session for an abort, which runs no test: only what reverting the CO needs is checked. A
        running or paused session the reader refuses is ended aborted unread instead, and True says the abort went
        that way, refused or not."""
        reverting = replace(self.world(), preflight_refusal=self.hardware.revert_problem())
        world = self.world_for_resume(session_id, reverting)
        unread = world.stored_unread
        if unread is None or not any(
            s.id == session_id and s.status in HELD_STATUSES for s in self.store.db.list_machine_sessions()
        ):
            self.post(Resume(session_id, reengage, None, abort=True), world)
            return False
        self._abort_unread(session_id, unread, reverting.preflight_refusal)
        return True

    def _abort_unread(self, session_id: int, unread: str, revert_problem: str | None) -> None:
        """End a held session the reader refuses as aborted without reading it: every core its CO journal shows it
        wrote in this boot goes to 0, then one transaction stores the status and the event that says why and what
        became of the CO, then the stock marker naming the session goes, as a stored abort's does."""
        if revert_problem is not None:
            self._refuse(revert_problem)
            return
        acting = SessionState(lifecycle=Lifecycle.ABORTED, session_id=session_id)
        cores, co = self._unread_co(session_id)
        failed = self._apply(acting, RevertAll(tuple((core, 0) for core in cores), True))
        if failed is not None:
            self._refuse(failed.error)
            return
        refused = ", ".join(str(core) for core in cores if self.resident[core] is None)
        if refused:
            co = (
                f"{co}; the write to cores {refused} was refused, so their CO is unknown: reboot or check the CO"
                " before tuning again"
            )
        text = f"aborted unread: {unread}; {co}"
        try:
            with self.store.db.transaction():
                self.store.db.abort_held_session(session_id)
                self.store.event(session_id, "aborted", text, self.boot)
        except sqlite3.Error as exc:
            self._refuse(f"session {session_id} could not be marked aborted: {exc}")
            return
        self.say(f"aborted: {text}")
        failed = self._landed(session_id)
        if failed is not None:
            self._apply(acting, Announce("database_failed", failed.error))

    def _unread_co(self, session_id: int) -> tuple[tuple[int, ...], str]:
        """The cores an unread abort sends to stock, those its CO journal names in this boot unless another session
        wrote after it, and what its event says of the CO."""
        check = "so no CO was written: reboot or check the CO before tuning again"
        try:
            wrote = self.store.journal_rows(session_id, self.boot)
            taken = self.store.taken_over(session_id, self.boot)
        except (sqlite3.Error, CorruptSessionError) as exc:
            return (), f"its CO journal could not be read ({exc}), {check}"
        cores = tuple(sorted({row.core_id for row in wrote}))
        if not cores:
            return (), "nothing it wrote is resident in this boot"
        if taken:
            return (), f"another session wrote the CO after it in this boot, {check}"
        return cores, f"cores {', '.join(str(core) for core in cores)} sent to stock"

    def _refuse(self, refusal: str) -> None:
        self.refused = refusal
        self.say(f"refused: {refusal}")

    def continue_on_setup(self) -> None:
        self.post(ContinueOnSetup(self.store.new_session_id()), self.world_for_start())

    def restart_on_setup(self, acknowledged: bool) -> None:
        self.post(RestartOnSetup(self.store.new_session_id(), acknowledged), self.world_for_start())

    def validate(self, session_id: int) -> None:
        world = replace(self.world_for_resume(session_id, self.world_checked()), bios_co=self.co_reads())
        last = self.stored_boot
        end = None if world.stored is None or last == self.boot else self.hardware.boot_end(last)
        if end is None or end.clean:
            self.post(ValidateProfile(session_id), world)
            return
        suspects = _unproven(self.store.journal_rows(session_id, last))
        self.post(
            ValidateProfile(session_id),
            replace(world, stored_crash_suspects=suspects, stored_end_unread=end.problem),
        )

    def run_until_rest(self, wait: float = 1.0) -> SessionState:
        with self.heartbeat.driven():
            while not self.at_rest():
                self.step(wait)
        return self.state


__all__ = ["Abort", "Close", "Hardware", "Identity", "LiveRig", "PauseRequest", "Runner", "Store"]
