from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace

from corecycler.history.db import JournalRow
from corecycler.tuner.live import crash_suspects, resident_at_death
from corecycler.tuner.machine.session import ALLCORE_LOADS, transition
from corecycler.tuner.machine.types import (
    HELD_STATUSES,
    Abort,
    Announce,
    AwaitWorker,
    CancelSchedule,
    Close,
    CloseDb,
    Commit,
    ContinuationDue,
    CrashEvidenceRead,
    DatabaseFailed,
    Evidence,
    FailContext,
    ForgetResident,
    Group,
    InFlight,
    KernelLogRead,
    Lifecycle,
    Load,
    MachineConfig,
    MarkSurvived,
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
    Supersede,
    Transition,
    ValidateProfile,
    Waiting,
    WorkerReported,
    World,
    WriteCO,
    WriteStockMarker,
)


class Broken(AssertionError):
    pass


def replay_of(spec, epoch: int) -> tuple:
    return (spec.vector, spec.load, spec.seconds, spec.plan_seed, epoch)


def remembered(spec) -> bool:
    """An all-core load or a Bisect slot, whose failure the session keeps; a LIVE1-begun slot runs one core's load."""
    return spec.load in ALLCORE_LOADS or spec.stage is Stage.BISECT


def held(state: SessionState | Stored) -> tuple[tuple[int, int], ...]:
    return tuple((r.core, r.value) for r in state.cores)


def at_baseline_between_slots(st: Stored | None, co: dict[int, int]) -> bool:
    flow = None if st is None else st.flow
    return (
        flow is not None
        and flow.stage is Stage.BISECT
        and not any(r.in_test for r in st.cores)
        and all(co.get(core) == base for core, base in st.baseline)
    )


@dataclass(frozen=True, slots=True)
class Layout:
    cpus_of: tuple[tuple[int, tuple[int, ...]], ...]
    domain_of: tuple[tuple[int, int | None], ...]

    @staticmethod
    def make(cores: int, domains: int = 1, smt: bool = True) -> Layout:
        per = max(1, -(-cores // domains))
        cpus = tuple((c, (2 * c, 2 * c + 1) if smt else (c,)) for c in range(cores))
        return Layout(cpus, tuple((c, c // per) for c in range(cores)))


def kept_rows(rows: tuple[JournalRow, ...]) -> tuple[JournalRow, ...]:
    last: dict[int, JournalRow] = {}
    landed: dict[int, JournalRow] = {}
    for row in rows:
        last[row.core_id] = row
        if row.confirmed:
            landed[row.core_id] = row
    keep = {row.id for row in (*last.values(), *landed.values())}
    return tuple(row for row in rows if row.id in keep)


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
    stopped: InFlight | None = None
    last_report: Report | None = None
    timer: tuple[int, float] | None = None
    now: float = 0.0
    temperature: float | None = 60.0
    power: tuple[tuple[str, str], ...] = (("platform profile", "balanced"),)
    setup: tuple[tuple[str, str], ...] = (("bios", "A"),)
    tjmax_c: float | None = None
    co_range: tuple[int, int] | None = (-50, 10)
    gap_grew: bool = False
    boot: int = 0
    stored_boot: int = 0
    faults: Faults = field(default_factory=Faults)
    config: MachineConfig | None = None
    marker: tuple[int, int] | None = None
    behind: set[tuple[int, int]] = field(default_factory=set)
    stock_fail_owed: set[int] = field(default_factory=set)
    handling: str = ""
    failed_at: set[tuple[int, int]] = field(default_factory=set)
    failed_allcore: set[tuple] = field(default_factory=set)
    failed_vectors: set[tuple[tuple[int, int], ...]] = field(default_factory=set)
    failed_twice: set[tuple[tuple[int, int], ...]] = field(default_factory=set)
    failed_slots: set[tuple[tuple[int, int], ...]] = field(default_factory=set)
    log: list[str] = field(default_factory=list)
    steps: int = 0
    edges_2t: dict[int, int] | None = None
    stacks: tuple[tuple[tuple[int, ...], tuple[int, ...], int], ...] = ()
    two_t_problem: str | None = None
    edges_hot: dict[int, int] | None = None
    edges_transient: dict[int, int] | None = None
    names_cores: bool = False
    edges_memory: dict[int, int] | None = None
    memory_skip: str = ""
    crash_note: tuple[tuple[Evidence, ...], bool] = ((), False)
    journal: tuple[JournalRow, ...] = ()
    journal_seq: int = 0
    dead_suspects: tuple[int, ...] = ()
    proven: dict[int, frozenset[int]] = field(default_factory=dict)
    superseded: set[int] = field(default_factory=set)
    outside: set[int] = field(default_factory=set)
    completed: tuple[Stored, ...] = ()
    crashed_bisect: bool = False
    owed: set[Stage | None] = field(default_factory=set)
    died_between_slots: bool = False
    epoch: int = 0

    def __post_init__(self) -> None:
        if not self.co:
            self.co = dict(self.bios)

    def world(self) -> World:
        return World(
            bios_co=tuple(sorted(self.bios.items())),
            resident=tuple(sorted(self.resident.items())),
            gap_grew=self.gap_grew,
            power=self.power,
            setup=self.setup,
            tjmax_c=self.tjmax_c,
            co_range=self.co_range,
            co_now=tuple(sorted(self.co.items())),
            now=self.now,
            temperature_c=self.temperature,
            stored=self.db,
            cpus_of=self.layout.cpus_of,
            domain_of=self.layout.domain_of,
            two_t_problem=self.two_t_problem,
            held_session=(self.db.session_id, self.db.status)
            if self.db is not None and self.db.status in HELD_STATUSES
            else None,
            proven=self.proven_newest_first() if self.db is not None and self.db.status == "quarantined" else (),
            completed=self.completed,
        )

    def proven_newest_first(self) -> tuple[tuple[int, tuple[int, ...]], ...]:
        kept = [row for row in reversed(self.journal) if row.survived and not row.retracted]
        cores = sorted({row.core_id for row in kept})
        return tuple((core, tuple(row.value for row in kept if row.core_id == core)) for core in cores)

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
            if isinstance(ev, CrashEvidenceRead) and result.state.lifecycle is Lifecycle.RUNNING and self.outside:
                raise Broken(f"resumed over an outside write on cores {sorted(self.outside)}")
            stored_before = self.db
            follow = self.run(before, result)
            self.state = result.state
            if result.state.flow is not None and result.state.flow.bisect is None:
                self.crashed_bisect = False
            self.check(before, ev, result)
            self.check_validation(before, ev, result.state)
            if follow is None:
                self.check_settled(before, ev, result)
            if isinstance(ev, CrashEvidenceRead) and ev.dirty_end and stored_before is not None:
                after = result.state
                if self.died_between_slots:
                    self.died_between_slots = False
                    self.check_unmoved(stored_before, after)
                counts = (
                    ("incidents", len(after.incidents), len(stored_before.incidents)),
                    ("resume_streak", after.resume_streak, stored_before.resume_streak),
                )
                for name, now, before_boot in counts:
                    if now > before_boot + 1:
                        raise Broken(f"one dead boot raised {name} by more than one: it was attributed twice")
                if (
                    ev.was_running
                    and after.lifecycle is Lifecycle.RUNNING
                    and len(after.incidents) <= len(stored_before.incidents)
                    and after.resume_streak <= stored_before.resume_streak
                ):
                    raise Broken("a crash raised neither the incidents nor the resume streak, so it can repeat forever")
            if follow is not None:
                queue.append(follow)
            elif self.stopped is not None:
                stopped, self.stopped = self.stopped, None
                queue.append(self.returned(stopped, cut=True))

    def run(self, before: SessionState, result: Transition):
        acting = result.state if result.state.session_id is not None else before
        tightened = None
        for effect in result.effects:
            if isinstance(effect, WriteCO) and self.resident.get(effect.core) is not None:
                if effect.value < self.resident[effect.core]:
                    tightened = effect.core
                elif effect.value > self.resident[effect.core] and tightened is not None:
                    raise Broken(f"core {effect.core} loosened after core {tightened} tightened in one vector change")
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
                    self.journaled(core, value, confirmed=False)
                    return SmuRefused(core, value, "injected refusal")
                self.co[core] = value
                self.resident[core] = value
                self.outside.discard(core)
                self.journaled(core, value, confirmed=True)
            case RevertAll(values=values, journal=journal):
                if journal and self.handling == "DatabaseFailed":
                    raise Broken("the database-failure handler journaled its revert into the failed database")
                for core, value in values:
                    if core not in self.owned(state):
                        raise Broken(f"reverted core {core}, which the session does not own")
                    self.co[core] = value
                    self.resident[core] = value
                    self.outside.discard(core)
                    if journal:
                        self.journaled(core, value, confirmed=True)
            case StopWorker(token=token):
                if self.worker is not None and self.worker.token == token:
                    cut = None if state.pause is None else state.pause.cut
                    self.stopped = self.worker if cut == self.worker else None
                    self.worker = None
            case AwaitWorker():
                pass
            case Commit(stored=st, first=first):
                if self.faults.fail_commits > 0:
                    self.faults.fail_commits -= 1
                    if not first:
                        self.behind.add((st.session_id, self.boot))
                    return DatabaseFailed("injected database failure", first)
                if self.db is not None and st.session_id == self.db.session_id:
                    self.check_bounds_only_tighten(self.db, st)
                self.landed(st.session_id)
                if first:
                    self.journal = ()
                    self.forget_session()
                self.db = st
                self.retract(st)
                self.stored_boot = self.boot
            case Supersede(parent=parent, child=child, origin=origin):
                if self.faults.fail_commits > 0:
                    self.faults.fail_commits -= 1
                    return DatabaseFailed("injected database failure")
                if self.db is None or parent.session_id != self.db.session_id or parent.status != "superseded":
                    raise Broken(f"superseded {parent.session_id} ({parent.status}), not the stored session")
                self.superseded.add(parent.session_id)
                self.landed(parent.session_id, child.session_id)
                self.db = child
                self.journal = ()
                if origin == "restart":
                    self.forget_session()
                else:
                    self.epoch += 1
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
            case MarkSurvived(exclude=exclude):
                rep = self.last_report
                proves = rep is not None and rep.group in (Group.PASS, Group.SILICON, Group.THERMAL)
                if not proves or rep.skipped or rep.unblamed_corrected:
                    raise Broken(f"values marked survived after a report that proves nothing: {rep}")
                self.mark_survived(set(exclude))
            case ReadCrashEvidence(session_id=session, child_id=child_id):
                dirty = self.stored_boot != self.boot
                read = self.stored_boot if dirty else self.boot
                if self.marker == (read, session) and (session, read) not in self.behind:
                    raise Broken(
                        f"session {session}'s resume read a lost verdict in boot {read}, where its stored state did "
                        "not fall behind or has been stored since"
                    )
                evidence, shared = self.crash_note if dirty else ((), False)
                self.crash_note = ((), False)
                suspects = self.dead_suspects if dirty else ()
                self.dead_suspects = ()
                return CrashEvidenceRead(
                    dirty_end=dirty,
                    same_boot=not dirty,
                    was_running=self.db is not None and self.db.status == "running",
                    stale_db=self.marker == (read, session),
                    shared_uncorrected=shared,
                    suspects=suspects,
                    evidence=evidence,
                    co_now=tuple((core, self.co.get(core)) for core, _ in self.layout.cpus_of),
                    last_written=()
                    if dirty
                    else tuple(sorted({row.core_id: row.value for row in self.journal if row.confirmed}.items())),
                    marked_now=self.marker == (self.boot, session),
                    child_id=child_id,
                )
            case WriteStockMarker():
                self.marker = (self.boot, state.session_id)
            case Announce(kind=kind, text=text):
                self.log.append(f"{kind}: {text}")
                if kind == "reengaged":
                    self.epoch += 1
            case CloseDb():
                pass
            case _:
                raise Broken(f"unknown effect {effect!r}")
        return None

    def landed(self, *sessions: int) -> None:
        self.behind = {entry for entry in self.behind if entry[0] not in sessions}
        if self.marker is not None and self.marker[1] in sessions:
            self.marker = None

    def forget_session(self) -> None:
        self.proven = {}
        self.failed_at = set()
        self.failed_allcore = set()
        self.failed_vectors = set()
        self.failed_twice = set()
        self.failed_slots = set()
        self.stock_fail_owed = set()

    def check_bounds_only_tighten(self, old: Stored, new: Stored) -> None:
        olds = {r.core: r.fb for r in old.cores}
        for r in new.cores:
            before = olds.get(r.core)
            if before is not None and (r.fb is None or r.fb < before):
                raise Broken(f"core {r.core}'s fail bound loosened from {before} to {r.fb}")

    def check_start(self, state: SessionState, spec) -> None:
        if state.session_id in self.superseded:
            raise Broken(f"superseded session {state.session_id} ran a test")
        if spec.quick and self.crashed_bisect:
            raise Broken("a quick group ran after a dead boot inside a group of its bisection")
        if spec.stage is Stage.SOAK and state.flow.dirty:
            raise Broken("a SOAK test started on a dirty profile, which must be proven again first")
        if state.setup != self.setup:
            raise Broken(f"a test started on the setup {self.setup}, not the session's {state.setup}")
        for core, value in spec.vector:
            fb = state.core(core).fb
            if value != 0 and fb is not None and value <= fb:
                raise Broken(f"core {core} runs at {value} in a {spec.stage} test, at or past its fail bound {fb}")
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
        if spec.stage is Stage.TIGHTEN:
            for failed in self.failed_vectors:
                moved = {core: value for core, value in failed if value != 0}
                if moved and all(vector[core] <= value for core, value in moved.items()):
                    raise Broken(f"a TIGHTEN test at {vector} is at or past {moved}, where an all-core test failed")
        if vector.get(spec.core) != spec.value:
            raise Broken(f"core {spec.core}'s test value {spec.value} is not its value in the test vector")
        if set(vector) != {core for core, _ in state.baseline}:
            raise Broken(f"the test vector {sorted(vector)} does not cover exactly the owned cores")
        for core, value in vector.items():
            if self.co.get(core) != value:
                raise Broken(f"core {core} sits at {self.co.get(core)} during a {spec.stage} test that needs {value}")

    def check_settled(self, before: SessionState, event, result: Transition) -> None:
        s = result.state
        if (
            isinstance(event, WorkerReported)
            and isinstance(before.slot, InFlight)
            and before.slot.token == event.token
            and (before.slot.spec.load in ALLCORE_LOADS or before.slot.spec.stage in (Stage.SEARCH, Stage.BISECT))
            and s.lifecycle is Lifecycle.RUNNING
            and isinstance(s.slot, Waiting)
        ):
            for core, base in s.baseline:
                if self.co.get(core) != base and core not in self.outside:
                    raise Broken(
                        f"core {core} waits at {self.co.get(core)} after a {before.slot.spec.stage} test "
                        f"reported {event.report.group}, not at its baseline {base}"
                    )

    def check_validation(self, before: SessionState, event, after: SessionState) -> None:
        """A validation owes a clean test at its values after a failure there: a stage's failure owes that stage's
        test, and a dead boot or a stock core's machine check owes the next test at those values, whatever stage it
        belongs to. While its cores hold those values it completes owing nothing, and an Abort ends aborted exactly
        while it owes something (D95, D101) or its values sit at or past a failure that still bars them (D118)."""
        if isinstance(event, ValidateProfile) and after.flow is not None and after.flow.validating is not None:
            self.owed = set()
            return
        flow = before.flow
        if flow is None or flow.validating is None or flow.carried or held(before) != flow.validating:
            return
        read = (
            isinstance(event, KernelLogRead) and isinstance(before.slot, Waiting) and before.slot.token == event.token
        )
        if read and any(ev.charged == 0 and ev.core in dict(flow.validating) for ev in event.evidence):
            self.owed.add(None)
        owing = sorted("the next test" if stage is None else stage.value for stage in self.owed)
        if after.lifecycle is Lifecycle.COMPLETED and owing:
            raise Broken(f"a validation completed at its values {dict(flow.validating)} owing a clean pass: {owing}")
        barred = self.barring(dict(flow.validating))
        if isinstance(event, Abort) and after.lifecycle is Lifecycle.ABORTED and not owing and barred is None:
            raise Broken(
                f"a validation aborted at its values {dict(flow.validating)} though a clean test at them followed "
                "every failure"
            )

    def check_unmoved(self, stored: Stored, after: SessionState) -> None:
        """A dead boot between two slots of a bisection, every owned core at its baseline when the boot died, is
        evidence about the baseline: it convicts no core and moves no value or fail bound (L18.d.4.3b.between)."""
        was = {r.core: (r.value, r.fb, r.crash_count) for r in stored.cores}
        for r in after.cores:
            if (r.value, r.fb, r.crash_count) != was[r.core]:
                raise Broken(
                    f"a dead boot between two bisection slots, every owned core at its baseline, moved core "
                    f"{r.core} from (value, fail bound, crashes) {was[r.core]} to {(r.value, r.fb, r.crash_count)}"
                )

    def judge_validation(self, spec, report: Report, cut: bool) -> None:
        flow = self.state.flow
        if flow is None or flow.validating is None:
            return
        stage = flow.bisect.entry if spec.stage is Stage.BISECT else spec.stage
        at_profile = spec.vector == flow.validating
        if cut:
            if at_profile and report.group is Group.SILICON and report.named and not report.evidence:
                self.owed.add(stage)
            return
        vector = dict(spec.vector)
        if held(self.state) == flow.validating and any(vector.get(ev.core) == 0 for ev in report.evidence):
            self.owed.add(None)
        if not at_profile:
            return
        lost = self.faults.fail_commits > 0
        if lost or report.group is Group.SILICON or (report.group is Group.ENVIRONMENT and spec.load is Load.SOAK):
            self.owed.add(stage)
        elif report.group is Group.PASS and not report.evidence and not report.skipped:
            self.owed -= {stage, None}

    def check(self, before: SessionState, event, result: Transition) -> None:
        s = result.state
        if s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is PauseReason.STOCK_UNSTABLE:
            self.stock_fail_owed.clear()
        if s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is not PauseReason.RESUMING:
            for core, base in s.baseline:
                if self.co.get(core) != base and core not in self.outside:
                    raise Broken(f"paused ({s.pause.reason}) with core {core} at {self.co.get(core)}, not {base}")
        if s.lifecycle is Lifecycle.COMPLETED:
            finals = {r.core: r.value for r in s.cores}
            if self.stacked(finals):
                raise Broken(f"completed with values {finals} that fail together")
            for edges in (self.edges_hot, self.edges_transient, self.edges_memory):
                if edges is not None and any(finals[core] < edges[core] for core in finals):
                    raise Broken(f"completed with values {finals} that fail an all-core load")
            for vector, *_ in self.failed_allcore:
                failed = {core: value for core, value in vector if value != 0}
                if failed and all(finals[core] <= value for core, value in failed.items()):
                    raise Broken(f"completed with values {finals} at or past {dict(vector)}, a failed all-core vector")
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
        load = spec.load.value if spec.load is not None else None
        if load == "soak":
            return Report(Group.PASS, spec.seconds)
        if load == "memory":
            if self.memory_skip:
                return Report(Group.PASS, 0.0, skipped=self.memory_skip)
            edges = self.edges_memory or self.edges
            failing = tuple(core for core, value in vector.items() if value < edges[core])
            if not failing and not self.stacked(vector):
                return Report(Group.PASS, spec.seconds)
            return Report(Group.SILICON, spec.seconds, named=failing)
        if load in ("hot", "burst", "warm", "idle"):
            edges = (self.edges_hot if load == "hot" else self.edges_transient) or self.edges
            failing = tuple(core for core, value in vector.items() if value < edges[core])
            if not failing and not self.stacked(vector):
                return Report(Group.PASS, spec.seconds)
            working = tuple(vector) if load == "hot" else failing or tuple(vector)
            context = FailContext(steady=load == "hot", working=working, paused=False, mhz=5000.0, tctl=70.0)
            named = failing if self.names_cores else ()
            return Report(Group.SILICON, spec.seconds, context=context, named=named)
        failed = spec.value < self.edge(spec.core, spec.klass) or self.stacked(vector, spec.core)
        return Report(Group.SILICON if failed else Group.PASS, spec.seconds)

    def deliver(self, report: Report | None = None) -> None:
        if self.worker is None:
            raise Broken("no worker to report")
        worker, self.worker = self.worker, None
        self.post(self.returned(worker, report))

    def returned(self, worker: InFlight, report: Report | None = None, cut: bool = False) -> WorkerReported:
        token, spec = worker.token, worker.spec
        report = report if report is not None else self.verdict_for(spec)
        self.judge_validation(spec, report, cut)
        isolated = all(value == 0 for core, value in spec.vector if core != spec.core)
        if report.group is Group.SILICON and spec.stage.value in ("search", "live1"):
            if spec.value != 0:
                self.failed_at.add((spec.core, spec.value))
            elif isolated:
                self.stock_fail_owed.add(spec.core)
        if remembered(spec):
            replay = replay_of(spec, self.epoch)
            failed = report.group is Group.SILICON or (report.group is Group.ENVIRONMENT and spec.load is Load.SOAK)
            if (failed or self.faults.fail_commits > 0) and not cut:
                self.failed_once_more(spec)
            if failed or self.faults.fail_commits > 0:
                self.failed_allcore.add(replay)
            elif report.group is Group.PASS and not report.evidence and not report.skipped:
                self.failed_allcore.discard(replay)
        self.now += report.seconds
        self.last_report = report
        if report.program_faults is not None:
            bare = replace(report, program_faults=None)
            seen = transition(self.state, WorkerReported(token, report), self.world())
            if seen != transition(self.state, WorkerReported(token, bare), self.world()):
                raise Broken(f"the machine's transition read the report's program faults: {report.program_faults}")
        return WorkerReported(token, report)

    def failed_once_more(self, spec) -> None:
        if spec.vector in self.failed_vectors:
            self.failed_twice.add(spec.vector)
        if spec.stage is Stage.BISECT:
            self.failed_slots.add(spec.vector)
        self.failed_vectors.add(spec.vector)

    def barring(self, values: dict[int, int]) -> tuple[tuple[int, int], ...] | None:
        """A failure bars values at or past it on every core it moved until an exact replay of it passes in its
        epoch, unless it failed twice (D118.again) or in a Bisect slot (D116), which nothing clears."""
        uncleared = {record[0] for record in self.failed_allcore}
        return next(
            (
                vector
                for vector in sorted(uncleared | self.failed_twice | self.failed_slots)
                if any(value for _, value in vector) and all(values[core] <= value for core, value in vector)
            ),
            None,
        )

    def journaled(self, core: int, value: int, confirmed: bool) -> None:
        envelope = self.proven.get(core, frozenset())
        survived = value == 0 or (bool(envelope) and value >= min(envelope))
        self.journal_seq += 1
        row = JournalRow(self.journal_seq, 1, core, value, str(self.boot), None, confirmed, survived, False, "")
        self.journal = kept_rows((*self.journal, row))

    def mark_survived(self, exclude: set[int]) -> None:
        latest = {row.core_id: row.id for row in self.journal}
        marked = [
            row
            for row in self.journal
            if latest[row.core_id] == row.id and row.confirmed and row.core_id not in exclude
        ]
        self.journal = tuple(replace(row, survived=True) if row in marked else row for row in self.journal)
        proven = dict(self.proven)
        for row in marked:
            proven[row.core_id] = proven.get(row.core_id, frozenset()) | {row.value}
        self.proven = proven

    def retract(self, st: Stored) -> None:
        bounds = {r.core: r.fb for r in st.cores}
        self.proven = {
            core: frozenset(v for v in values if bounds.get(core) is None or v > bounds[core])
            for core, values in self.proven.items()
        }

    def fire(self) -> None:
        if self.timer is None:
            raise Broken("no timer to fire")
        token, due = self.timer
        self.timer = None
        self.now = max(self.now, due)
        self.post(ContinuationDue(token))

    def crash(self, evidence: tuple[Evidence, ...] = (), shared: bool = False) -> None:
        st = self.db
        if st is not None and st.status == "running" and st.flow is not None and st.flow.validating == held(st):
            self.owed.add(None)
        if self.worker is not None and remembered(self.worker.spec) and not shared:
            self.failed_allcore.add(replay_of(self.worker.spec, self.epoch))
            self.failed_once_more(self.worker.spec)
        if self.worker is not None and self.worker.spec.stage is Stage.BISECT:
            self.crashed_bisect = True
        self.died_between_slots = at_baseline_between_slots(st, self.co)
        self.crash_note = (evidence, shared)
        self.dead_suspects = tuple(sorted(crash_suspects(resident_at_death(self.journal), -1)))
        self.journal = ()
        self.state = SessionState(next_token=self.state.next_token)
        self.worker = None
        self.timer = None
        self.boot += 1
        self.co = dict(self.bios)
        self.resident = {}
        self.outside = set()

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
    selected = cfg.pop("selected", tuple(range(cores)))
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


def save_machine_session(db, root, status: str, session_id: int | None = None) -> Stored:
    from corecycler.tuner.live import Identity, Store
    from corecycler.tuner.machine.session import stored
    from tests._machine import this_boot

    fake = fresh(2, [-3, -2], 1, max_offset=-5, k_1t=1)
    start(fake)
    saved = replace(stored(fake.state), status=status)
    if session_id is not None:
        saved = replace(saved, session_id=session_id)
    Store(db, root / "state", Identity("bios-1", "cpu-1", None)).save(saved, this_boot())
    return saved


UNREADABLE_ROWS = (
    ("json_remove(machine_json, '$.base_co')", "KeyError('base_co')"),
    (
        "json_set(machine_json, '$.flow.allcore', json('[1]'))",
        "AttributeError(\"'list' object has no attribute 'items'\")",
    ),
)
