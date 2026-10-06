from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace

from corecycler.tuner.machine.session import transition
from corecycler.tuner.machine.types import (
    Abort,
    Announce,
    AwaitWorker,
    CancelSchedule,
    ClearStockMarker,
    Close,
    CloseDb,
    Commit,
    ContinuationDue,
    CrashEvidenceRead,
    DatabaseFailed,
    Evidence,
    ForgetResident,
    Group,
    InFlight,
    Lifecycle,
    MachineConfig,
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
    Start,
    StartWorker,
    StopWorker,
    Stored,
    Transition,
    WorkerReported,
    World,
    WriteCO,
    WriteStockMarker,
)


class Broken(AssertionError):
    pass


@dataclass(frozen=True, slots=True)
class Layout:
    cpus_of: tuple[tuple[int, tuple[int, ...]], ...]
    domain_of: tuple[tuple[int, int | None], ...]

    @staticmethod
    def make(cores: int, domains: int = 1, smt: bool = True) -> Layout:
        per = max(1, -(-cores // domains))
        cpus = tuple((c, (2 * c, 2 * c + 1) if smt else (c,)) for c in range(cores))
        return Layout(cpus, tuple((c, c // per) for c in range(cores)))


@dataclass
class Faults:
    refuse_writes: int = 0
    fail_commits: int = 0


@dataclass
class Fake:
    layout: Layout
    edges: dict[int, int]
    bios: dict[int, int]
    state: SessionState = field(default_factory=SessionState)
    co: dict[int, int] = field(default_factory=dict)
    resident: dict[int, int | None] = field(default_factory=dict)
    db: Stored | None = None
    worker: InFlight | None = None
    timer: tuple[int, float] | None = None
    now: float = 0.0
    temperature: float | None = 60.0
    capped: bool = False
    gap_grew: bool = False
    boot: int = 0
    stored_boot: int = 0
    faults: Faults = field(default_factory=Faults)
    config: MachineConfig | None = None
    marker_boot: int | None = None
    stock_fail_owed: set[int] = field(default_factory=set)
    handling: str = ""
    failed_at: set[tuple[int, int]] = field(default_factory=set)
    log: list[str] = field(default_factory=list)
    steps: int = 0
    edges_2t: dict[int, int] | None = None
    stacks: tuple[tuple[tuple[int, ...], tuple[int, ...], int], ...] = ()
    two_t_problem: str | None = None
    crash_note: tuple[tuple[Evidence, ...], bool] = ((), False)

    def __post_init__(self) -> None:
        if not self.co:
            self.co = dict(self.bios)

    def world(self) -> World:
        return World(
            bios_co=tuple(sorted(self.bios.items())),
            resident=tuple(sorted(self.resident.items())),
            gap_grew=self.gap_grew,
            frequency_capped=self.capped,
            now=self.now,
            temperature_c=self.temperature,
            stored=self.db,
            cpus_of=self.layout.cpus_of,
            domain_of=self.layout.domain_of,
            two_t_problem=self.two_t_problem,
        )

    def post(self, event) -> None:
        queue = deque([event])
        while queue:
            ev = queue.popleft()
            before = self.state
            self.steps += 1
            try:
                result = transition(before, ev, self.world())
            except Exception as exc:
                raise Broken(f"transition raised on {type(ev).__name__} from {before.lifecycle}: {exc!r}") from exc
            self.gap_grew = False
            self.handling = type(ev).__name__
            stored_before = self.db
            follow = self.run(before, result)
            self.state = result.state
            self.check(before, ev, result)
            if isinstance(ev, CrashEvidenceRead) and ev.dirty_end and stored_before is not None:
                for name in ("incidents", "resume_streak"):
                    if getattr(result.state, name) > getattr(stored_before, name) + 1:
                        raise Broken(f"one dead boot raised {name} by more than one: it was attributed twice")
            if follow is not None:
                queue.append(follow)

    def run(self, before: SessionState, result: Transition):
        acting = result.state if result.state.session_id is not None else before
        for effect in result.effects:
            follow = self.apply(acting, effect)
            if follow is not None:
                return follow
        return None

    def owned(self, state: SessionState) -> dict[int, int]:
        return dict(state.baseline) if state.baseline else {}

    def apply(self, state: SessionState, effect):
        cfg = state.config
        match effect:
            case WriteCO(core=core, value=value):
                if core not in self.owned(state):
                    raise Broken(f"wrote core {core}, which the session does not own")
                if cfg is not None and not cfg.max_offset <= value <= 0:
                    raise Broken(f"wrote {value} to core {core}, outside [{cfg.max_offset}, 0]")
                if self.faults.refuse_writes > 0:
                    self.faults.refuse_writes -= 1
                    self.resident[core] = None
                    return SmuRefused(core, value, "injected refusal")
                self.co[core] = value
                self.resident[core] = value
            case RevertAll(values=values, journal=journal):
                if journal and self.handling == "DatabaseFailed":
                    raise Broken("the database-failure handler journaled its revert into the failed database")
                for core, value in values:
                    if core not in self.owned(state):
                        raise Broken(f"reverted core {core}, which the session does not own")
                    self.co[core] = value
                    self.resident[core] = value
            case StopWorker(token=token):
                if self.worker is not None and self.worker.token == token:
                    self.worker = None
            case AwaitWorker():
                pass
            case Commit(stored=st):
                if self.faults.fail_commits > 0:
                    self.faults.fail_commits -= 1
                    return DatabaseFailed("injected database failure")
                if self.db is not None and st.session_id == self.db.session_id:
                    self.check_bounds_only_tighten(self.db, st)
                self.db = st
                self.stored_boot = self.boot
            case StartWorker(token=token, spec=spec):
                self.check_start(state, spec)
                self.worker = InFlight(token, spec)
            case Schedule(token=token, seconds=seconds):
                self.timer = (token, self.now + seconds)
            case CancelSchedule():
                self.timer = None
            case ForgetResident():
                self.resident = {}
            case ReadCrashEvidence():
                dirty = self.stored_boot != self.boot
                evidence, shared = self.crash_note if dirty else ((), False)
                self.crash_note = ((), False)
                return CrashEvidenceRead(
                    dirty_end=dirty,
                    same_boot=not dirty,
                    was_running=self.db is not None and self.db.status == "running",
                    stale_db=self.marker_boot == (self.stored_boot if dirty else self.boot),
                    shared_uncorrected=shared,
                    evidence=evidence,
                )
            case WriteStockMarker():
                self.marker_boot = self.boot
            case ClearStockMarker():
                self.marker_boot = None
            case Announce(kind=kind, text=text):
                self.log.append(f"{kind}: {text}")
            case CloseDb():
                pass
            case _:
                raise Broken(f"unknown effect {effect!r}")
        return None

    def check_bounds_only_tighten(self, old: Stored, new: Stored) -> None:
        olds = {r.core: r.fb for r in old.cores}
        for r in new.cores:
            before = olds.get(r.core)
            if before is not None and (r.fb is None or r.fb < before):
                raise Broken(f"core {r.core}'s fail bound loosened from {before} to {r.fb}")

    def check_start(self, state: SessionState, spec) -> None:
        rec = state.core(spec.core)
        if rec.fb is not None and spec.value <= rec.fb:
            raise Broken(f"core {spec.core} tested at {spec.value}, at or past its fail bound {rec.fb}")
        if spec.value == 0:
            if spec.core in self.stock_fail_owed:
                raise Broken(f"core {spec.core} retested at stock after a stock FAIL with no STOCK_UNSTABLE pause")
        elif (spec.core, spec.value) in self.failed_at or any(
            core == spec.core and value != 0 and spec.value < value for core, value in self.failed_at
        ):
            raise Broken(f"core {spec.core} tested at {spec.value} after a FAIL at or above it")
        vector = dict(spec.vector)
        if vector.get(spec.core) != spec.value:
            raise Broken(f"core {spec.core}'s test value {spec.value} is not its value in the test vector")
        if set(vector) != {core for core, _ in state.baseline}:
            raise Broken(f"the test vector {sorted(vector)} does not cover exactly the owned cores")
        for core, value in vector.items():
            if self.co.get(core) != value:
                raise Broken(f"core {core} sits at {self.co.get(core)} during a {spec.stage} test that needs {value}")

    def check(self, before: SessionState, event, result: Transition) -> None:
        s = result.state
        if s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is PauseReason.STOCK_UNSTABLE:
            self.stock_fail_owed.clear()
        if s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is not PauseReason.RESUMING:
            for core, base in s.baseline:
                if self.co.get(core) != base:
                    raise Broken(f"paused ({s.pause.reason}) with core {core} at {self.co.get(core)}, not {base}")
        if s.lifecycle is Lifecycle.COMPLETED:
            finals = {r.core: r.value for r in s.cores}
            if self.stacked(finals):
                raise Broken(f"completed with values {finals} that fail together")
            two_cpus = all(len(cpus) >= 2 for _, cpus in self.layout.cpus_of)
            for r in s.cores:
                edge = self.edges[r.core]
                if two_cpus and self.edges_2t is not None:
                    edge = max(edge, self.edges_2t[r.core])
                if r.value < edge:
                    raise Broken(f"completed with core {r.core} at {r.value}, past its true edge {edge}")
                if r.phase is not Phase.PROVEN:
                    raise Broken(f"completed with core {r.core} still {r.phase}")
                if self.co.get(r.core) != r.value:
                    raise Broken(f"completed but core {r.core} sits at {self.co.get(r.core)}, not {r.value}")

    def edge(self, core: int, klass) -> int:
        if klass.value == "2t" and self.edges_2t is not None:
            return self.edges_2t[core]
        return self.edges[core]

    def stacked(self, vector: dict[int, int], lane: int | None = None) -> bool:
        return any(
            (lane is None or lane in lanes) and sum(abs(vector.get(core, 0)) for core in cores) > limit
            for lanes, cores, limit in self.stacks
        )

    def verdict_for(self, spec) -> Report:
        vector = dict(spec.vector)
        failed = spec.value < self.edge(spec.core, spec.klass) or self.stacked(vector, spec.core)
        return Report(Group.SILICON if failed else Group.PASS, spec.seconds)

    def deliver(self, report: Report | None = None) -> None:
        if self.worker is None:
            raise Broken("no worker to report")
        token, spec = self.worker.token, self.worker.spec
        report = report if report is not None else self.verdict_for(spec)
        isolated = all(value == 0 for core, value in spec.vector if core != spec.core)
        if report.group is Group.SILICON and spec.stage.value in ("search", "live1"):
            if spec.value != 0:
                self.failed_at.add((spec.core, spec.value))
            elif isolated:
                self.stock_fail_owed.add(spec.core)
        self.worker = None
        self.now += report.seconds
        self.post(WorkerReported(token, report))

    def fire(self) -> None:
        if self.timer is None:
            raise Broken("no timer to fire")
        token, due = self.timer
        self.timer = None
        self.now = max(self.now, due)
        self.post(ContinuationDue(token))

    def crash(self, evidence: tuple[Evidence, ...] = (), shared: bool = False) -> None:
        self.crash_note = (evidence, shared)
        self.state = SessionState(next_token=self.state.next_token)
        self.worker = None
        self.timer = None
        self.boot += 1
        self.co = dict(self.bios)
        self.resident = {}

    def progress(self) -> bool:
        if self.worker is not None:
            self.deliver()
            return True
        if self.timer is not None:
            self.fire()
            return True
        return False


def run_to_rest(fake: Fake, limit: int) -> int:
    moves = 0
    while fake.progress():
        moves += 1
        if moves > limit:
            raise Broken(f"no rest after {limit} moves: {fake.state.lifecycle} {fake.state.flow}")
    return moves


def fresh(cores: int, edges: list[int], domains: int = 1, smt: bool = True, bios: int = 0, **cfg) -> Fake:
    from corecycler.tuner.machine.types import Mode
    from corecycler.tuner.orders import default_method

    layout = Layout.make(cores, domains, smt)
    selected = tuple(range(cores))
    method = cfg.pop("method", default_method(selected, dict(layout.domain_of)))
    config = MachineConfig(Mode.CO, method, selected, **cfg)
    return Fake(layout, dict(enumerate(edges)), {c: bios for c in range(cores)}, config=config)


def start(fake: Fake, session_id: int = 1, seed: int = 7) -> None:
    fake.post(Start(fake.config, session_id, seed, acknowledged=True))


__all__ = [
    "Abort",
    "Broken",
    "Close",
    "Evidence",
    "Fake",
    "Faults",
    "Group",
    "PauseRequest",
    "Report",
    "Resume",
    "fresh",
    "replace",
    "run_to_rest",
    "start",
]
