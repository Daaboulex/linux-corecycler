from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from corecycler.tuner.orders import Method, RoundCursor


class Mode(StrEnum):
    CO = "co"
    SHAPER = "shaper"
    BOTH = "both"


class Lifecycle(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ABORTED = "aborted"
    QUARANTINED = "quarantined"


TERMINAL = frozenset({Lifecycle.COMPLETED, Lifecycle.ABORTED, Lifecycle.QUARANTINED})


class PauseReason(StrEnum):
    USER = "user"
    RESUMING = "resuming"
    APPARATUS = "apparatus"
    SMU_WRITE = "smu_write"
    SMU_READ = "smu_read"
    THERMAL = "thermal"
    DATABASE = "database"
    STOCK_UNSTABLE = "stock_unstable"
    INCIDENTS = "incidents"
    PROFILE_APPLY = "profile_apply"
    START_FAILURES = "start_failures"
    INTERNAL = "internal"
    OUTSIDE_WRITE = "outside_write"
    HELD_KNOB = "held_knob"


class Klass(StrEnum):
    T1 = "1t"
    T2 = "2t"


class Phase(StrEnum):
    NOT_STARTED = "not_started"
    COARSE = "coarse"
    FINE = "fine"
    PROVE = "prove"
    PROVEN = "proven"


class Group(StrEnum):
    PASS = "pass"
    SILICON = "silicon"
    THERMAL = "thermal"
    ENVIRONMENT = "environment"
    APPARATUS = "apparatus"
    SLEPT = "slept"


class Stage(StrEnum):
    SEARCH = "search"
    LIVE1 = "live1"
    BISECT = "bisect"
    STEADY = "steady"
    TRANSIENT = "transient"
    TIGHTEN = "tighten"
    ENDURANCE = "endurance"
    MEMORY = "memory"
    SOAK = "soak"
    FINALIZE = "finalize"


ALLCORE = frozenset({Stage.STEADY, Stage.TRANSIENT, Stage.TIGHTEN, Stage.ENDURANCE, Stage.SOAK})


class Load(StrEnum):
    ONE_T = "1t"
    TWO_T = "2t"
    HOT = "hot"
    HOT_T = "hot_t"
    BURST = "burst"
    WARM = "warm"
    IDLE = "idle"
    MEMORY = "memory"
    SOAK = "soak"


TRANSIENT_CYCLE = (Load.BURST, Load.WARM, Load.BURST, Load.IDLE)
ENDURANCE_CYCLE = (Load.HOT, Load.BURST, Load.WARM, Load.BURST, Load.IDLE)


@dataclass(frozen=True, slots=True)
class MachineConfig:
    mode: Mode
    method: Method
    selected: tuple[int, ...]
    max_offset: int = -50
    coarse_step: int = 5
    fine_step: int = 1
    k_1t: int = 10
    k_2t: int = 6
    walk_budget_s: float = 7200.0
    test_seconds_1t: float = 60.0
    test_seconds_2t: float = 90.0
    hot_seconds: float = 510.0
    bisect_seconds: float = 160.0
    stage_seconds: tuple[tuple[str, float], ...] = (("burst", 480.0), ("warm", 480.0), ("idle", 360.0))
    transient_window_s: float = 3600.0
    soak_seconds: float = 1800.0
    soak: bool = True
    endurance: bool = False
    endurance_window_s: float = 10800.0
    memory: bool = True
    memory_seconds: float = 300.0
    wait_between_cores_s: float = 15.0
    apparatus_retries: int = 3
    thermal_retries: int = 3
    thermal_limit_c: float = 95.0
    cool_margin_c: float = 5.0
    cool_min_s: float = 5.0
    cool_cap_s: float = 300.0
    start_failure_threshold: int = 0
    stop_bound_s: float = 5.0

    def k(self, klass: Klass) -> int:
        return self.k_1t if klass is Klass.T1 else self.k_2t


@dataclass(frozen=True, slots=True)
class CoreRecord:
    core: int
    phase: Phase = Phase.NOT_STARTED
    value: int = 0
    fb: int | None = None
    best: int | None = None
    start: int = 0
    count: int = 0
    walk_seconds: float = 0.0
    walk_tests: int = 0
    crash_count: int = 0
    in_test: bool = False

    def inside(self, value: int, max_offset: int) -> bool:
        return max_offset <= value <= 0 and (self.fb is None or value > self.fb)


@dataclass(frozen=True, slots=True)
class TestSpec:
    klass: Klass
    core: int
    value: int
    cpus: tuple[int, ...]
    seconds: float
    vector: tuple[tuple[int, int], ...] = ()
    stage: Stage = Stage.SEARCH
    load: Load | None = None


@dataclass(frozen=True, slots=True)
class InFlight:
    token: int
    spec: TestSpec


@dataclass(frozen=True, slots=True)
class Waiting:
    token: int
    seconds: float
    picked: int | None = None
    cool_below_c: float | None = None
    cool_deadline: float | None = None


Slot = InFlight | Waiting


@dataclass(frozen=True, slots=True)
class Pause:
    reason: PauseReason
    detail: str
    cores: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Streaks:
    apparatus: int = 0
    thermal: int = 0
    start_failures: int = 0


@dataclass(frozen=True, slots=True)
class Live1:
    index: int = 0
    requeue: tuple[int, ...] = ()
    solo: int | None = None
    back: Stage | None = None


@dataclass(frozen=True, slots=True)
class BisectRun:
    entry: Stage
    lane: int
    klass: Klass
    universe: tuple[int, ...]
    values: tuple[tuple[int, int], ...]
    queue: tuple[tuple[int, ...], ...]
    results: tuple[tuple[tuple[int, ...], bool], ...] = ()
    parents: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = ()
    rerun: tuple[int, ...] | None = None


@dataclass(frozen=True, slots=True)
class FailContext:
    steady: bool
    working: tuple[int, ...]
    paused: bool
    mhz: float | None = None
    tctl: float | None = None


@dataclass(frozen=True, slots=True)
class AllCore:
    t_idx: int = 0
    t_clean: float = 0.0
    t_types: tuple = ()
    t_busy: tuple = ()
    co_delta: tuple = ()
    rung: int = 0
    floors: tuple[tuple[int, int], ...] = ()
    item: int | None = None
    frozen: tuple[int, ...] = ()
    tightened: bool = False
    pass_no: int = 1


@dataclass(frozen=True, slots=True)
class Flow:
    stage: Stage
    klass: Klass = Klass.T1
    cursor: RoundCursor = RoundCursor()
    owed: int | None = None
    last_core: int | None = None
    applied: bool = False
    dirty: bool = False
    live1: Live1 | None = None
    bisect: BisectRun | None = None
    q5: tuple[tuple[tuple[int, int], int], ...] = ()
    q5_others: tuple[int, ...] = ()
    allcore: AllCore | None = None


@dataclass(frozen=True, slots=True)
class SessionState:
    lifecycle: Lifecycle = Lifecycle.IDLE
    session_id: int | None = None
    config: MachineConfig | None = None
    seed: int = 0
    baseline: tuple[tuple[int, int], ...] = ()
    cores: tuple[CoreRecord, ...] = ()
    flow: Flow | None = None
    slot: Slot | None = None
    pause: Pause | None = None
    streaks: Streaks = Streaks()
    resume_streak: int = 0
    incidents: int = 0
    next_token: int = 1

    def __post_init__(self) -> None:
        problems = list(_state_problems(self))
        if problems:
            raise ValueError("illegal session state: " + "; ".join(problems))

    def core(self, core: int) -> CoreRecord:
        for record in self.cores:
            if record.core == core:
                return record
        raise KeyError(f"core {core} is not owned by this session")

    def with_core(self, record: CoreRecord) -> SessionState:
        return replace(self, cores=tuple(record if r.core == record.core else r for r in self.cores))

    def baseline_of(self, core: int) -> int:
        return dict(self.baseline)[core]


def _state_problems(s: SessionState):
    held = s.lifecycle not in (Lifecycle.IDLE, *TERMINAL)
    if s.lifecycle is Lifecycle.IDLE and (s.session_id, s.flow, s.slot, s.pause) != (None, None, None, None):
        yield "IDLE holds a session, a flow, a slot or a pause"
    if s.lifecycle is Lifecycle.RUNNING and (s.flow is None or s.slot is None or s.pause is not None):
        yield "RUNNING needs a flow and exactly one slot and no pause"
    if s.lifecycle is Lifecycle.PAUSING and (not isinstance(s.slot, InFlight) or s.pause is None):
        yield "PAUSING needs a test in flight and a pause"
    if s.lifecycle is Lifecycle.PAUSED and (s.slot is not None or s.pause is None or s.flow is None):
        yield "PAUSED needs a pause and a flow and no slot"
    if s.lifecycle in TERMINAL and (s.slot is not None or s.flow is not None or s.session_id is None):
        yield "a terminal state holds a slot or a flow, or has no session"
    if held and (s.session_id is None or s.config is None):
        yield "a held state has no session or no config"
    if held and s.config is not None and tuple(r.core for r in s.cores) != tuple(sorted(s.config.selected)):
        yield "the core records are not the selected cores in order"


@dataclass(frozen=True, slots=True)
class Evidence:
    core: int
    corrected: bool


@dataclass(frozen=True, slots=True)
class Report:
    group: Group
    seconds: float
    evidence: tuple[Evidence, ...] = ()
    detail: str = ""
    unblamed_corrected: bool = False
    context: FailContext | None = None
    named: tuple[int, ...] = ()
    skipped: str = ""


@dataclass(frozen=True, slots=True)
class Start:
    config: MachineConfig
    session_id: int
    seed: int
    acknowledged: bool = False


@dataclass(frozen=True, slots=True)
class PauseRequest:
    pass


@dataclass(frozen=True, slots=True)
class Abort:
    pass


@dataclass(frozen=True, slots=True)
class Close:
    pass


@dataclass(frozen=True, slots=True)
class Resume:
    session_id: int


@dataclass(frozen=True, slots=True)
class ContinuationDue:
    token: int


@dataclass(frozen=True, slots=True)
class WorkerReported:
    token: int
    report: Report


@dataclass(frozen=True, slots=True)
class CrashEvidenceRead:
    dirty_end: bool
    same_boot: bool
    was_running: bool
    stale_db: bool = False
    other_session_wrote: bool = False
    shared_uncorrected: bool = False
    suspects: tuple[int, ...] = ()
    context: FailContext | None = None
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True, slots=True)
class SmuRefused:
    core: int
    value: int
    cause: str


@dataclass(frozen=True, slots=True)
class DatabaseFailed:
    error: str


@dataclass(frozen=True, slots=True)
class InternalError:
    error: str


Event = (
    Start
    | PauseRequest
    | Abort
    | Close
    | Resume
    | ContinuationDue
    | WorkerReported
    | CrashEvidenceRead
    | SmuRefused
    | DatabaseFailed
    | InternalError
)


@dataclass(frozen=True, slots=True)
class Stored:
    session_id: int
    status: str
    config: MachineConfig
    seed: int
    baseline: tuple[tuple[int, int], ...]
    cores: tuple[CoreRecord, ...]
    flow: Flow | None
    resume_streak: int = 0
    incidents: int = 0


@dataclass(frozen=True, slots=True)
class World:
    bios_co: tuple[tuple[int, int | None], ...] = ()
    preflight_refusal: str | None = None
    resident: tuple[tuple[int, int | None], ...] = ()
    gap_grew: bool = False
    frequency_capped: bool = False
    now: float = 0.0
    temperature_c: float | None = None
    stored: Stored | None = None
    cpus_of: tuple[tuple[int, tuple[int, ...]], ...] = ()
    domain_of: tuple[tuple[int, int | None], ...] = ()
    two_t_problem: str | None = None
    owner_layout: bool = False

    def resident_of(self, core: int) -> int | None:
        return dict(self.resident).get(core)


@dataclass(frozen=True, slots=True)
class WriteCO:
    core: int
    value: int


@dataclass(frozen=True, slots=True)
class RevertAll:
    values: tuple[tuple[int, int], ...]
    journal: bool = True


@dataclass(frozen=True, slots=True)
class StopWorker:
    token: int


@dataclass(frozen=True, slots=True)
class AwaitWorker:
    token: int
    bound_s: float


@dataclass(frozen=True, slots=True)
class Commit:
    stored: Stored


@dataclass(frozen=True, slots=True)
class StartWorker:
    token: int
    spec: TestSpec


@dataclass(frozen=True, slots=True)
class Schedule:
    token: int
    seconds: float


@dataclass(frozen=True, slots=True)
class CancelSchedule:
    pass


@dataclass(frozen=True, slots=True)
class ForgetResident:
    pass


@dataclass(frozen=True, slots=True)
class ReadCrashEvidence:
    session_id: int


@dataclass(frozen=True, slots=True)
class WriteStockMarker:
    pass


@dataclass(frozen=True, slots=True)
class ClearStockMarker:
    pass


@dataclass(frozen=True, slots=True)
class Announce:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class CloseDb:
    pass


Effect = (
    WriteCO
    | RevertAll
    | StopWorker
    | AwaitWorker
    | Commit
    | StartWorker
    | Schedule
    | CancelSchedule
    | ForgetResident
    | ReadCrashEvidence
    | WriteStockMarker
    | ClearStockMarker
    | Announce
    | CloseDb
)


@dataclass(frozen=True, slots=True)
class Transition:
    state: SessionState
    effects: tuple[Effect, ...] = field(default=())
    refused: str | None = None
