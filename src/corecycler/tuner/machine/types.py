from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING

from corecycler.tuner.orders import Method, RoundCursor

if TYPE_CHECKING:
    from corecycler.engine.detector import ProgramFaults


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


HELD_STATUSES = frozenset({"running", "paused"})


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
    SETUP_CHANGED = "setup_changed"
    KERNEL_LOG_UNREAD = "kernel_log_unread"


class Incident(StrEnum):
    CRASH = "crash"
    STOCK_MACHINE_CHECK = "stock_machine_check"
    SOAK_FAILURE = "soak_failure"
    UNRECORDED = "unrecorded"


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
ROUND_CYCLE = (Load.HOT, *TRANSIENT_CYCLE)
STAGE_LOADS = frozenset(load.value for load in (*TRANSIENT_CYCLE, *ENDURANCE_CYCLE) if load is not Load.HOT)


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
    starts: tuple[tuple[int, int], ...] = ()

    def k(self, klass: Klass) -> int:
        return self.k_1t if klass is Klass.T1 else self.k_2t


KNOB_FLOOR: dict[str, float] = {
    "coarse_step": 1,
    "fine_step": 1,
    "k_1t": 1,
    "k_2t": 1,
    "walk_budget_s": 1.0,
    "test_seconds_1t": 1.0,
    "test_seconds_2t": 1.0,
    "hot_seconds": 1.0,
    "bisect_seconds": 1.0,
    "transient_window_s": 1.0,
    "soak_seconds": 1.0,
    "endurance_window_s": 1.0,
    "memory_seconds": 1.0,
    "wait_between_cores_s": 0.0,
    "apparatus_retries": 0,
    "thermal_retries": 0,
    "thermal_limit_c": 1.0,
    "cool_margin_c": 0.0,
    "cool_min_s": 0.0,
    "cool_cap_s": 0.0,
    "start_failure_threshold": 0,
    "stop_bound_s": 1.0,
}


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
    skipped: int | None = None
    reclaim_floor: int | None = None

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
    thermal_limit_c: float | None = None
    quick: bool = False
    plan_seed: str | None = None


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
    cut: InFlight | None = None


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
    quick: bool = False


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
    floors: tuple[tuple[int, int], ...] = ()
    frozen: tuple[int, ...] = ()
    tightened: bool = False
    pass_no: int = 1
    rounds: int = 0


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
    validating: tuple[tuple[int, int], ...] | None = None
    one_off: tuple[Stage, tuple[tuple[int, int], ...]] | None = None
    carried: bool = False
    unreplayed: tuple[Stage, ...] = ()


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
    incidents: tuple[Incident, ...] = ()
    next_token: int = 1
    power: tuple[tuple[str, str], ...] = ()
    setup: tuple[tuple[str, str], ...] = ()
    base_co: tuple[tuple[int, int | None], ...] = ()
    failed_vectors: tuple[tuple[tuple[int, int], ...], ...] = ()
    cleared_vectors: tuple[tuple[tuple[int, int], ...], ...] = ()
    clearable_vector: tuple[tuple[int, int], ...] | None = None

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
    charged: int | None = None


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
    kernel_read_ns: int | None = None
    speeds: tuple[tuple[str, float, bool], ...] = ()
    output: str = ""
    program_faults: ProgramFaults | None = None


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
    reengage: bool = False
    child_id: int | None = None
    abort: bool = False


@dataclass(frozen=True, slots=True)
class ValidateProfile:
    session_id: int


@dataclass(frozen=True, slots=True)
class ContinueOnSetup:
    child_id: int


@dataclass(frozen=True, slots=True)
class RestartOnSetup:
    child_id: int
    acknowledged: bool = False


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
    evidence: tuple[Evidence, ...] = ()
    kernel_problem: str | None = None
    end_unread: str | None = None
    co_now: tuple[tuple[int, int | None], ...] = ()
    last_written: tuple[tuple[int, int], ...] = ()
    marked_now: bool = False
    child_id: int | None = None
    program_faults: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class KernelLogRead:
    token: int
    evidence: tuple[Evidence, ...] = ()
    problem: str | None = None
    program_faults: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SmuRefused:
    core: int
    value: int
    cause: str


@dataclass(frozen=True, slots=True)
class DatabaseFailed:
    error: str
    first: bool = False


@dataclass(frozen=True, slots=True)
class InternalError:
    error: str


Event = (
    Start
    | PauseRequest
    | Abort
    | Close
    | Resume
    | ValidateProfile
    | ContinueOnSetup
    | RestartOnSetup
    | ContinuationDue
    | WorkerReported
    | CrashEvidenceRead
    | KernelLogRead
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
    incidents: tuple[Incident, ...] = ()
    power: tuple[tuple[str, str], ...] = ()
    setup: tuple[tuple[str, str], ...] = ()
    base_co: tuple[tuple[int, int | None], ...] = ()
    failed_vectors: tuple[tuple[tuple[int, int], ...], ...] = ()
    cleared_vectors: tuple[tuple[tuple[int, int], ...], ...] = ()
    clearable_vector: tuple[tuple[int, int], ...] | None = None


@dataclass(frozen=True, slots=True)
class World:
    bios_co: tuple[tuple[int, int | None], ...] = ()
    preflight_refusal: str | None = None
    preflight_warnings: tuple[str, ...] = ()
    resident: tuple[tuple[int, int | None], ...] = ()
    gap_grew: bool = False
    power: tuple[tuple[str, str], ...] = ()
    setup: tuple[tuple[str, str], ...] = ()
    tjmax_c: float | None = None
    now: float = 0.0
    temperature_c: float | None = None
    stored: Stored | None = None
    stored_unread: str | None = None
    held_session: tuple[int, str] | None = None
    newest_base: tuple[int, tuple[tuple[int, int | None], ...]] | None = None
    newest_unread: str | None = None
    cpus_of: tuple[tuple[int, tuple[int, ...]], ...] = ()
    domain_of: tuple[tuple[int, int | None], ...] = ()
    two_t_problem: str | None = None
    stored_crash_suspects: tuple[int, ...] | None = None
    stored_end_unread: str | None = None
    co_range: tuple[int, int] | None = None
    co_now: tuple[tuple[int, int | None], ...] | None = None
    proven: tuple[tuple[int, tuple[int, ...]], ...] = ()
    completed: tuple[Stored, ...] = ()
    completed_unread: str | None = None

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
    first: bool = False


@dataclass(frozen=True, slots=True)
class Supersede:
    parent: Stored
    child: Stored
    origin: str


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
class MarkSurvived:
    exclude: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadCrashEvidence:
    session_id: int
    child_id: int | None = None


@dataclass(frozen=True, slots=True)
class WriteStockMarker:
    pass


@dataclass(frozen=True, slots=True)
class Announce:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class CloseDb:
    pass


Effect = (
    MarkSurvived
    | WriteCO
    | RevertAll
    | StopWorker
    | AwaitWorker
    | Commit
    | Supersede
    | StartWorker
    | Schedule
    | CancelSchedule
    | ForgetResident
    | ReadCrashEvidence
    | WriteStockMarker
    | Announce
    | CloseDb
)


@dataclass(frozen=True, slots=True)
class Transition:
    state: SessionState
    effects: tuple[Effect, ...] = field(default=())
    refused: str | None = None
