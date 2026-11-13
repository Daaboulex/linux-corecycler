from __future__ import annotations

from collections import Counter
from dataclasses import replace

from corecycler.tuner.machine import allcore, bisect, search
from corecycler.tuner.machine.resume import Attribution, attribute, resident
from corecycler.tuner.machine.types import (
    ALLCORE,
    ENDURANCE_CYCLE,
    KNOB_FLOOR,
    ROUND_CYCLE,
    STAGE_LOADS,
    TRANSIENT_CYCLE,
    Abort,
    AllCore,
    Announce,
    AwaitWorker,
    CancelSchedule,
    Close,
    CloseDb,
    Commit,
    ContinuationDue,
    ContinueOnSetup,
    CoreRecord,
    CrashEvidenceRead,
    DatabaseFailed,
    Event,
    Evidence,
    Flow,
    ForgetResident,
    Group,
    Incident,
    InFlight,
    InternalError,
    KernelLogRead,
    Klass,
    Lifecycle,
    Live1,
    Load,
    MachineConfig,
    MarkSurvived,
    Mode,
    Pause,
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
    Streaks,
    Supersede,
    TestSpec,
    Transition,
    ValidateProfile,
    Waiting,
    WorkerReported,
    World,
    WriteCO,
    WriteStockMarker,
)
from corecycler.tuner.orders import next_core, order_problem, round_sequence

ALLCORE_LOADS = frozenset({Load.HOT, Load.BURST, Load.WARM, Load.IDLE, Load.MEMORY, Load.SOAK})
QUARANTINE_STREAK = 3
INCIDENT_LIMIT = 2
COOL_POLL_S = 1.0
UNCORRECTED_STEPS = 3
HELD = (Lifecycle.RUNNING, Lifecycle.PAUSING, Lifecycle.PAUSED)
RESUMABLE = frozenset({"running", "paused", "aborted"})
SETUP_REMEDY = "put it back and resume, or continue or restart under it"


RECORDED_ONLY = frozenset({"pm_table", "python", "qt", "zenpower"})
KERNEL = "kernel"
KERNEL_UNREAD = ("unreadable", "absent")

INCIDENT_NAMES = {
    Incident.CRASH: ("crash nobody could be blamed for", "crashes nobody could be blamed for"),
    Incident.STOCK_MACHINE_CHECK: ("machine check at a stock value", "machine checks at stock values"),
    Incident.SOAK_FAILURE: ("soak failure", "soak failures"),
    Incident.UNRECORDED: ("stored without its kind", "stored without their kind"),
}


def _incidents_text(incidents: tuple[Incident, ...]) -> str:
    counts = [(kind, incidents.count(kind)) for kind in Incident if kind in incidents]
    return ", ".join(f"{count} {INCIDENT_NAMES[kind][count > 1]}" for kind, count in counts)


def transition(s: SessionState, e: Event, w: World) -> Transition:
    match e:
        case Start():
            return _start(s, e, w, hinted=True)
        case Resume():
            return _resume(s, e, w)
        case ValidateProfile():
            return _validate(s, e, w)
        case ContinueOnSetup():
            return _continue_on_setup(s, e, w)
        case RestartOnSetup():
            return _restart_on_setup(s, e, w)
        case PauseRequest():
            return _pause_request(s)
        case Abort():
            return _abort(s)
        case Close():
            return _close(s)
        case ContinuationDue():
            return _continuation(s, e, w)
        case WorkerReported():
            return _reported(s, e, w)
        case CrashEvidenceRead():
            return _crash_evidence(s, e, w)
        case KernelLogRead():
            return _kernel_log_read(s, e)
        case SmuRefused():
            return _smu_refused(s, e)
        case DatabaseFailed():
            return _database_failed(s, e)
        case InternalError():
            return _internal(s, e)
    raise TypeError(f"no transition for {type(e).__name__}")


def _status(s: SessionState) -> str:
    if s.lifecycle in (Lifecycle.RUNNING, Lifecycle.PAUSING):
        return "running"
    if s.lifecycle is Lifecycle.PAUSED:
        return "paused"
    return s.lifecycle.value


def stored(s: SessionState) -> Stored:
    if s.session_id is None or s.config is None:
        raise ValueError("only a session can be stored")
    return Stored(
        session_id=s.session_id,
        status=_status(s),
        config=s.config,
        seed=s.seed,
        baseline=s.baseline,
        cores=s.cores,
        flow=s.flow,
        resume_streak=s.resume_streak,
        incidents=s.incidents,
        power=s.power,
        setup=s.setup,
        base_co=s.base_co,
        failed_vectors=s.failed_vectors,
        cleared_vectors=s.cleared_vectors,
        clearable_vector=s.clearable_vector,
    )


def reconstruct(st: Stored) -> SessionState:
    flow = None if st.flow is None else replace(st.flow, applied=False)
    return SessionState(
        lifecycle=Lifecycle.PAUSED,
        session_id=st.session_id,
        config=st.config,
        seed=st.seed,
        baseline=st.baseline,
        cores=st.cores,
        flow=flow if flow is not None else Flow(Stage.SEARCH),
        pause=Pause(PauseReason.RESUMING, "reading what the last boot left"),
        resume_streak=st.resume_streak,
        incidents=st.incidents,
        power=st.power,
        setup=st.setup,
        base_co=st.base_co,
        failed_vectors=st.failed_vectors,
        cleared_vectors=st.cleared_vectors,
        clearable_vector=st.clearable_vector,
    )


def _commit(s: SessionState) -> Commit:
    return Commit(stored(s))


def _clear_in_test(s: SessionState) -> SessionState:
    return replace(s, cores=tuple(replace(r, in_test=False) for r in s.cores))


def _worker_token(s: SessionState) -> int | None:
    return s.slot.token if isinstance(s.slot, InFlight) else None


def _stop_effects(s: SessionState, revert_values: tuple[tuple[int, int], ...], journal: bool) -> list:
    token = _worker_token(s)
    effects: list = [CancelSchedule()]
    if token is not None:
        effects.append(StopWorker(token))
    effects.append(RevertAll(revert_values, journal))
    if token is not None and s.config is not None:
        effects.append(AwaitWorker(token, s.config.stop_bound_s))
    return effects


def _keep_pick(s: SessionState) -> SessionState:
    flow = s.flow
    picked = s.slot.picked if isinstance(s.slot, Waiting) else None
    if isinstance(s.slot, InFlight):
        picked = s.slot.spec.core
    if picked is not None and flow is not None and flow.stage is Stage.SEARCH and flow.owed is None:
        return replace(s, flow=replace(flow, owed=picked))
    return s


def _owe_interrupted(s: SessionState, in_test: tuple[int, ...]) -> SessionState:
    flow = s.flow
    if flow.stage is Stage.SEARCH and flow.owed is None and len(in_test) == 1:
        return replace(s, flow=replace(flow, owed=in_test[0]))
    return s


def _database_paused(s: SessionState) -> bool:
    return s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is PauseReason.DATABASE


def _enter_pause(s: SessionState, reason: PauseReason, detail: str, cores: tuple[int, ...] = ()) -> Transition:
    s = _keep_pick(s)
    effects = _stop_effects(s, s.baseline, journal=True)
    paused = _clear_in_test(
        replace(s, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(reason, detail, cores), streaks=Streaks())
    )
    paused = replace(paused, flow=replace(paused.flow, applied=False))
    effects += [_commit(paused), Announce("paused", f"{reason.value}: {detail}")]
    return Transition(paused, tuple(effects))


def _two_cpus_everywhere(cores: tuple[int, ...], w: World) -> bool:
    cpus = dict(w.cpus_of)
    return all(len(cpus.get(core, ())) >= 2 for core in cores)


def _start(s: SessionState, e: Start, w: World, *, hinted: bool) -> Transition:
    if s.lifecycle in HELD:
        return Transition(s, refused="a session is held: pause or abort it first")
    cfg = e.config
    problem = _start_problem(cfg, e, w)
    if problem is not None:
        return Transition(s, refused=problem)
    token = s.next_token
    baseline = tuple((core, 0) for core in sorted(cfg.selected))
    starts, said = _starts(cfg, w, hinted)
    new = SessionState(
        lifecycle=Lifecycle.RUNNING,
        session_id=e.session_id,
        config=cfg,
        seed=e.seed,
        baseline=baseline,
        cores=tuple(CoreRecord(core, start=dict(starts).get(core, 0)) for core in sorted(cfg.selected)),
        flow=Flow(Stage.SEARCH, Klass.T1),
        slot=Waiting(token, 0.0),
        next_token=token + 1,
        power=w.power,
        setup=w.setup,
        base_co=w.bios_co,
    )
    order = "1 core, not interleaved" if len(baseline) == 1 else f"{len(baseline)} cores, {cfg.method.value} order"
    effects: list = [Commit(stored(new), first=True)]
    effects += [WriteCO(core, value) for core, value in baseline if w.resident_of(core) != value]
    acknowledged = _to_acknowledge(cfg, w)
    effects += [
        Announce("started", f"session {e.session_id}, {order}"),
        *([Announce("starts", said)] if said else []),
        *([Announce("acknowledged", acknowledged)] if acknowledged else []),
        *(Announce("warning", warning) for warning in w.preflight_warnings),
        Schedule(token, 0.0),
    ]
    return Transition(new, tuple(effects))


def _starts(cfg: MachineConfig, w: World, hinted: bool) -> tuple[tuple[tuple[int, int], ...], str | None]:
    if cfg.starts:
        return cfg.starts, _starts_from("the configuration", cfg.starts)
    if not hinted:
        return (), None
    source = next((st for st in w.completed if st.power == w.power and not _setup_changes(st.setup, w.setup)), None)
    if source is None:
        why = w.completed_unread or "no completed session has this setup, power state and Curve Shaper"
        return (), f"every core from 0; {why}"
    hints = tuple((r.core, r.value) for r in source.cores if r.core in cfg.selected and cfg.max_offset <= r.value <= -1)
    if not hints:
        return (), f"every core from 0; session {source.session_id}'s values are 0 or outside {cfg.max_offset}..-1"
    return hints, _starts_from(f"session {source.session_id}'s values", hints)


def _starts_from(source: str, starts: tuple[tuple[int, int], ...]) -> str:
    return f"from {source}, {', '.join(f'core {core} at {value}' for core, value in starts)}; any other core from 0"


def _start_problem(cfg, e: Start, w: World) -> str | None:
    if w.preflight_refusal is not None:
        return w.preflight_refusal
    if w.held_session is not None:
        held, status = w.held_session
        return f"session {held} is {status}: resume it or abort it before starting another"
    if cfg.mode is not Mode.CO:
        return f"the {cfg.mode.value} mode is not built yet; only per-core CO runs"
    problem = config_problem(cfg, w.co_range)
    if problem is not None:
        return problem
    domain_of = dict(w.domain_of)
    problem = order_problem(cfg.method, cfg.selected, domain_of)
    if problem is not None:
        return problem
    cpus = dict(w.cpus_of)
    missing = sorted(core for core in cfg.selected if not cpus.get(core))
    if missing:
        return f"cores {missing} are not in the core map"
    if _two_cpus_everywhere(cfg.selected, w) and w.two_t_problem is not None:
        return f"the 2T class cannot run: {w.two_t_problem}"
    bios = dict(w.bios_co)
    unreadable = sorted(core for core in cfg.selected if bios.get(core) is None)
    if unreadable:
        return f"the CO of cores {unreadable} could not be read at Start"
    named = _to_acknowledge(cfg, w)
    if named and not e.acknowledged:
        return f"{named}: acknowledge to start"
    return None


def _to_acknowledge(cfg: MachineConfig, w: World) -> str:
    bios = dict(w.bios_co)
    already = sorted(core for core in cfg.selected if bios[core] != 0)
    changed = [] if w.newest_base is None else co_changed_since(w.newest_base[1], bios, cfg.selected)
    named = []
    if already:
        named.append(f"cores {already} read a CO other than 0 at Start, set by the BIOS or written since boot")
    if changed:
        named.append(f"the CO differs from when session {w.newest_base[0]} started: {', '.join(changed)}")
    if w.newest_unread is not None:
        named.append(f"{w.newest_unread}, so whether the CO moved since it started cannot be judged")
    return "; ".join(named)


def co_changed_since(base_co, reads: dict[int, int | None], cores) -> list[str]:
    base = dict(base_co)
    return [
        f"C{core} {'unread' if base.get(core) is None else base[core]} to {reads[core]}"
        for core in sorted(cores)
        if reads[core] is not None and base.get(core) != reads[core]
    ]


def _resume(s: SessionState, e: Resume, w: World) -> Transition:
    if s.lifecycle in (Lifecycle.RUNNING, Lifecycle.PAUSING):
        return Transition(s, refused="the session is running")
    if s.lifecycle is Lifecycle.PAUSED:
        if s.session_id != e.session_id:
            return Transition(s, refused=f"session {s.session_id} is held: abort it to resume another")
        if _database_paused(s):
            return _resume_stored(s, e, w)
        if s.pause is not None and s.pause.reason is PauseReason.RESUMING:
            return Transition(s, refused="the resume is already reading the last boot")
        resuming = replace(s, pause=Pause(PauseReason.RESUMING, "reading what the last boot left"))
        return Transition(resuming, (ReadCrashEvidence(e.session_id, e.child_id),))
    return _resume_stored(s, e, w)


def config_problem(cfg: MachineConfig, co_range: tuple[int, int] | None) -> str | None:
    low = [
        f"{name} {getattr(cfg, name)} is below {floor}"
        for name, floor in KNOB_FLOOR.items()
        if getattr(cfg, name) < floor
    ]
    if low:
        return "; ".join(low)
    if cfg.fine_step > cfg.coarse_step:
        return f"fine_step {cfg.fine_step} is above coarse_step {cfg.coarse_step}"
    if co_range is None:
        return "this CPU's CO range is unknown"
    if not co_range[0] <= cfg.max_offset <= -cfg.fine_step:
        return f"max_offset {cfg.max_offset} is outside {co_range[0]}..{-cfg.fine_step}, the CPU's range to one step"
    starts = [core for core, _ in cfg.starts]
    if len(set(starts)) != len(starts) or not set(starts) <= set(cfg.selected):
        return f"starts must name each selected core at most once: {list(cfg.starts)}"
    outside = [f"core {core} at {value}" for core, value in cfg.starts if not cfg.max_offset <= value <= -1]
    if outside:
        return f"a start must be within {cfg.max_offset}..-1: {', '.join(outside)}"
    if cfg.cool_min_s > cfg.cool_cap_s:
        return f"cool_min_s {cfg.cool_min_s} is above cool_cap_s {cfg.cool_cap_s}"
    names = [name for name, _ in cfg.stage_seconds]
    if sorted(names) != sorted(STAGE_LOADS) or any(seconds <= 0 for _, seconds in cfg.stage_seconds):
        return f"stage_seconds must give {', '.join(sorted(STAGE_LOADS))} once each, each above 0"
    return None


def _stored_gates(st: Stored, w: World, abort: bool) -> str | None:
    """The world's preflight refusal, then the stored configuration's own checks, which only a command that runs a
    test needs: an abort reverts the CO and runs none, so a configuration it could not run never holds it back."""
    if abort or w.preflight_refusal is not None:
        return w.preflight_refusal
    problem = config_problem(st.config, w.co_range) or order_problem(
        st.config.method, st.config.selected, dict(w.domain_of)
    )
    if problem is None:
        return None
    remedy = "start a new session" if st.status == "completed" else f"abort session {st.session_id} and start a new one"
    return f"{problem}; {remedy}"


def _resume_stored(s: SessionState, e: Resume, w: World) -> Transition:
    st = w.stored
    if st is None or st.session_id != e.session_id:
        return Transition(s, refused=w.stored_unread or f"session {e.session_id} is not stored")
    if st.status == "completed":
        return Transition(s, refused="the session is completed: use Validate Profile")
    if st.status == "quarantined":
        return _reengage(s, e, w)
    if st.status not in RESUMABLE:
        return Transition(s, refused=f"a session stored as {st.status} cannot be resumed")
    refusal = _stored_gates(st, w, abort=e.abort)
    if refusal is not None:
        return Transition(s, refused=refusal)
    resumed = replace(reconstruct(st), next_token=s.next_token)
    return Transition(resumed, (ReadCrashEvidence(e.session_id, e.child_id),))


def _reengage(s: SessionState, e: Resume, w: World) -> Transition:
    st = w.stored
    if not e.reengage:
        return Transition(
            s,
            refused=f"session {st.session_id} is quarantined: resume it with --reengage to go on from proven ground",
        )
    refusal = _stored_gates(st, w, abort=e.abort)
    if refusal is not None:
        return Transition(s, refused=refusal)
    resumed = replace(reconstruct(st), next_token=s.next_token, resume_streak=0)
    proven = dict(w.proven)
    cores, said = [], []
    for rec in resumed.cores:
        ground = next((v for v in proven.get(rec.core, ()) if v != 0 and rec.inside(v, st.config.max_offset)), None)
        idle = replace(rec, count=0, in_test=False, skipped=None, reclaim_floor=None)
        if ground is None:
            cores.append(replace(idle, phase=Phase.NOT_STARTED, value=0, start=0))
            said.append(f"core {rec.core} from stock")
        else:
            cores.append(replace(idle, phase=Phase.PROVEN, value=ground))
            said.append(f"core {rec.core} at {ground} (proven)")
    reengaged = replace(resumed, cores=tuple(cores), clearable_vector=None)
    notice = Announce("reengaged", f"session {st.session_id} re-engaged on proven ground: " + ", ".join(said))
    return Transition(reengaged, (notice, ReadCrashEvidence(e.session_id, e.child_id)))


def _setup_choice_problem(s: SessionState, w: World) -> str | None:
    if s.lifecycle is not Lifecycle.PAUSED or s.pause is None or s.pause.reason is not PauseReason.SETUP_CHANGED:
        return "only a session paused as setup_changed can continue or restart under a new setup"
    reads = dict(w.bios_co)
    unread = sorted(core for core, _ in w.cpus_of if reads.get(core) is None)
    if unread:
        return f"the CO of core {', '.join(map(str, unread))} could not be read"
    if _setup_changes(s.setup, w.setup) or _held_base_changes(s, w.bios_co):
        return None
    moved = _changes(s.power, w.power)
    if moved:
        return f"only the power state changed since session {s.session_id} started: {moved}; restore it and resume"
    return (
        f"the setup a verdict depends on, the power state and the CO of the cores session {s.session_id} "
        "does not own read as at its start; resume it"
    )


def _superseded(s: SessionState) -> Stored:
    return replace(stored(s), status="superseded")


REENTRY = "it re-enters the all-core stages from steady with the margin"
FINAL_PASS = "every core is tested alone on the new setup before the session completes"


def _continue_on_setup(s: SessionState, e: ContinueOnSetup, w: World) -> Transition:
    problem = _setup_choice_problem(s, w)
    if problem is not None:
        return Transition(s, refused=problem)
    changed = "; ".join(filter(None, (_setup_changes(s.setup, w.setup), _held_base_changes(s, w.bios_co))))
    said = f"session {e.child_id} continues session {s.session_id} under a new setup: {changed}"
    return _carry_on(s, e.child_id, w.setup, w.bios_co, said)


def _carry_on(
    s: SessionState,
    child_id: int,
    setup: tuple[tuple[str, str], ...],
    base_co: tuple[tuple[int, int | None], ...],
    said: str,
    noted: tuple[Announce, ...] = (),
) -> Transition:
    token = s.next_token
    entered = _reentered(
        replace(
            s,
            lifecycle=Lifecycle.RUNNING,
            session_id=child_id,
            setup=setup,
            base_co=base_co,
            pause=None,
            slot=Waiting(token, 0.0),
            streaks=Streaks(),
            next_token=token + 1,  # equivalent-mutant: tokens only need to rise, nothing reads their step
            clearable_vector=None,
        )
    )
    flow = entered.flow
    if flow.live1 is not None and flow.live1.back is Stage.FINALIZE:
        flow = replace(flow, live1=Live1(back=Stage.FINALIZE))
    child = replace(entered, flow=replace(flow, carried=True))
    retest = REENTRY if entered.flow is not s.flow else f"it goes on in {flow.stage.value}"
    effects: list = [Supersede(_superseded(s), stored(child), "continue")]
    effects += [WriteCO(core, value) for core, value in s.baseline]
    effects += [Announce("continued", f"{said}; {retest}, and {FINAL_PASS}"), *noted, Schedule(token, 0.0)]
    return Transition(child, tuple(effects))


def _restart_on_setup(s: SessionState, e: RestartOnSetup, w: World) -> Transition:
    problem = _setup_choice_problem(s, w)
    if problem is not None:
        return Transition(s, refused=problem)
    parent_held = w.held_session is not None and w.held_session[0] == s.session_id
    world = replace(w, held_session=None) if parent_held else w
    started = _start(
        SessionState(next_token=s.next_token), Start(s.config, e.child_id, s.seed, e.acknowledged), world, hinted=False
    )
    if started.refused is not None:
        return Transition(s, refused=started.refused)
    rest = tuple(effect for effect in started.effects if not isinstance(effect, Commit))
    restarted = Announce(
        "restarted",
        f"session {e.child_id} restarts session {s.session_id} under a new setup: "
        + "; ".join(filter(None, (_setup_changes(s.setup, w.setup), _held_base_changes(s, w.bios_co)))),
    )
    return Transition(started.state, (Supersede(_superseded(s), stored(started.state), "restart"), *rest, restarted))


def _validate(s: SessionState, e: ValidateProfile, w: World) -> Transition:
    if s.lifecycle in HELD:
        return Transition(s, refused="a session is held: pause or abort it first")
    st = w.stored
    if st is None or st.session_id != e.session_id:
        return Transition(s, refused=w.stored_unread or f"session {e.session_id} is not stored")
    if st.status != "completed":
        return Transition(
            s, refused=f"session {st.session_id} is {st.status}: only a completed session can be validated"
        )
    refusal = _stored_gates(st, w, abort=False)
    if refusal is not None:
        return Transition(s, refused=refusal)
    if w.stored_crash_suspects:
        cores = ", ".join(map(str, w.stored_crash_suspects))
        return Transition(
            s,
            refused=f"the boot session {st.session_id} completed in {_ending(w)}, while cores {cores} held unproven "
            "values, and a completed session has no step that attributes it: start a new session",
        )
    reads = dict(w.bios_co)
    unread = sorted(core for core, _ in w.cpus_of if reads.get(core) is None)
    if unread:
        return Transition(s, refused=f"the CO of core {', '.join(map(str, unread))} could not be read")
    if st.power != w.power:
        return Transition(
            s,
            refused=f"the power state changed since session {st.session_id} started: "
            f"{_changes(st.power, w.power)}; restore it and validate",
        )
    token = s.next_token
    profile = tuple((r.core, r.value) for r in st.cores)
    running = SessionState(
        lifecycle=Lifecycle.RUNNING,
        session_id=st.session_id,
        config=st.config,
        seed=st.seed,
        baseline=st.baseline,
        cores=tuple(replace(r, in_test=False) for r in st.cores),
        flow=Flow(Stage.LIVE1, live1=Live1(), allcore=AllCore(floors=profile), validating=profile),
        slot=Waiting(token, 0.0),
        resume_streak=st.resume_streak,
        next_token=token + 1,
        power=st.power,
        setup=st.setup,
        base_co=st.base_co,
        failed_vectors=st.failed_vectors,
        cleared_vectors=st.cleared_vectors,
    )
    changed = "; ".join(filter(None, (_setup_changes(st.setup, w.setup), _held_base_changes(st, w.bios_co))))
    if changed:
        detail = f"the setup changed since session {st.session_id} started: {changed}; {SETUP_REMEDY}"
        return _enter_pause(running, PauseReason.SETUP_CHANGED, detail, _held_moved(st, w.bios_co))
    effects: list = [_commit(running)]
    effects += [WriteCO(core, value) for core, value in st.baseline if w.resident_of(core) != value]
    effects += [
        Announce(
            "validating",
            f"session {st.session_id}: proving its completed values again"
            + ("" if w.stored_crash_suspects is None else f", after the boot it completed in {_ending(w)}"),
        ),
        Schedule(token, 0.0),
    ]
    return Transition(running, tuple(effects))


def _convict(s: SessionState, core: int) -> tuple[SessionState, bool]:
    cfg = s.config
    rec = s.core(core)
    value = resident(s, core)
    if value == 0:
        return s, True
    if s.flow.stage is Stage.SEARCH:
        return s.with_core(search.on_crash(replace(rec, value=value), cfg).record), False
    backed = search.back_off_in_place(replace(rec, value=value), cfg, search.CRASH_STEPS)
    out = s.with_core(replace(backed, crash_count=rec.crash_count + 1))
    flow = out.flow
    if flow.stage is Stage.BISECT and core in bisect.current_group(flow.bisect):
        flow = _bisect_exit(flow)
    flow = _requeued(flow, core)
    if flow.stage is Stage.TIGHTEN:
        flow = replace(flow, allcore=_frozen(flow.allcore, (core,)))
    elif flow.stage in ALLCORE:
        flow = replace(flow, allcore=replace(flow.allcore, frozen=(*flow.allcore.frozen, core), t_clean=0.0))
    return replace(out, flow=replace(flow, dirty=True)), False


def _crash_evidence(s: SessionState, e: CrashEvidenceRead, w: World) -> Transition:
    if s.lifecycle is not Lifecycle.PAUSED or s.pause is None or s.pause.reason is not PauseReason.RESUMING:
        return Transition(s)
    verdict = attribute(s, e)
    decided = {core: f"the crash conviction of core {core} decided its verdict" for core in verdict.convict}
    if verdict.held is not None:
        decided[verdict.held] = f"core {verdict.held}'s crash at a value this session does not own decided its verdict"
    if e.same_boot and verdict.lost is not None:
        decided[verdict.lost] = (
            f"the fail verdict on core {verdict.lost}'s test, whose result the database lost, decided its verdict"
        )
    aside = tuple(
        ev
        for ev in verdict.evidence
        if ev.core in decided
        and (ev.core not in verdict.convict or (resident(s, ev.core) if ev.charged is None else ev.charged) != 0)
    )
    kept = tuple(ev for ev in verdict.evidence if ev not in aside)
    if e.same_boot:
        evidence, substituted, placed = _placed(s, kept, _knob_problem(s, e, w))
    elif verdict.at_stock:
        evidence, substituted, placed = kept, kept, ()
    else:
        evidence, placed = _charged(s, kept)
        substituted = ()
    notes = (*(_unjudged(ev, decided[ev.core]) for ev in aside), *placed)
    verdict = replace(verdict, evidence=evidence)
    result = _attributed(s, e, w, verdict, substituted)
    if e.end_unread is not None:
        unread = Announce(
            "boot_end_unread", f"the last boot's end could not be read, so it is treated as a crash: {e.end_unread}"
        )
        stored_first = 1 if isinstance(result.effects[0], Supersede) else 0
        result = replace(result, effects=(*result.effects[:stored_first], unread, *result.effects[stored_first:]))
    faults = tuple(Announce("program_fault", line) for line in e.program_faults)
    result = replace(result, effects=(*result.effects, *notes, *faults))
    if e.kernel_problem is None:
        return result
    unread = (
        "this boot's kernel log could not be read, so this resume judged none of its records"
        if e.same_boot
        else "the crashed boot's kernel log could not be read, so the crash was attributed without it"
    )
    return replace(result, effects=(*result.effects, Announce("kernel_log_unread", f"{unread}: {e.kernel_problem}")))


def _ending(w: World) -> str:
    if w.stored_end_unread is None:
        return "crashed"
    return f"an end that could not be read ({w.stored_end_unread}), treated as a crash"


def _attributed(
    s: SessionState, e: CrashEvidenceRead, w: World, verdict: Attribution, substituted: tuple[Evidence, ...]
) -> Transition:
    after = s
    if e.dirty_end and s.flow.stage is Stage.BISECT and any(r.in_test for r in s.cores):
        after = replace(s, flow=replace(s.flow, bisect=replace(s.flow.bisect, quick=False)))
    slot_or_whole = s.flow.bisect is not None or _replay_stage(s.flow) in ALLCORE | {Stage.MEMORY}
    if (verdict.lost is not None or verdict.flow_crash) and slot_or_whole:
        after = _remember(after, tuple((core, resident(s, core)) for core, _ in s.baseline))
    early: list[Pause] = []
    if verdict.lost is not None:
        after, stock = _stale_fail(after, verdict.lost, w)
        if stock is not None:
            early.append(Pause(PauseReason.STOCK_UNSTABLE, stock, (verdict.lost,)))
    at_zero: list[int] = []
    for core in verdict.convict:
        after, at_stock = _convict(after, core)
        if at_stock:
            at_zero.append(core)
    if at_zero:
        crashed = "; ".join(f"core {core} crashed at 0" for core in at_zero)
        early.append(Pause(PauseReason.STOCK_UNSTABLE, crashed, tuple(at_zero)))
    flow_crash = at_stock_crash = False
    if verdict.flow_crash:
        after, stock = _flow_crash(after, w)
        at_stock_crash = stock is not None
        flow_crash = not at_stock_crash
        if at_stock_crash:
            early.append(Pause(PauseReason.STOCK_UNSTABLE, stock))
    cleared = _clear_in_test(after)
    after = cleared if early else _owe_interrupted(cleared, tuple(r.core for r in s.cores if r.in_test))
    streak = s.resume_streak + 1 if verdict.convict or flow_crash else s.resume_streak
    counted = verdict.incident or at_stock_crash
    incidents = (*s.incidents, Incident.CRASH) if counted else s.incidents
    after = replace(after, resume_streak=streak, incidents=incidents)
    if counted or at_zero or verdict.held is not None:
        after = _owe_replay(after, _held(after))
    after = _set_failed(after, verdict.evidence, () if any(r.in_test for r in s.cores) else substituted)
    judged, recorded, aside = _records_judged(after, verdict.evidence, verdict.at_stock, counted, e.dirty_end)
    reached = len(judged.incidents) > len(s.incidents) and len(judged.incidents) >= INCIDENT_LIMIT
    limit = [Pause(PauseReason.INCIDENTS, _incidents_text(judged.incidents))] if reached else []
    held: list[Pause] = []
    if verdict.held is not None:
        crashed = f"core {verdict.held} crashed the machine at a value this session does not own"
        held.append(Pause(PauseReason.HELD_KNOB, crashed, (verdict.held,)))
    result = _resumed(s, judged, e, w, streak, (*early, *limit, *held, *recorded))
    return replace(result, effects=(*result.effects, *aside))


def _charged(s: SessionState, evidence: tuple[Evidence, ...]) -> tuple[tuple[Evidence, ...], tuple[Announce, ...]]:
    records = {r.core: r for r in s.cores}
    charged: list[Evidence] = []
    notes: list[Announce] = []
    for ev in evidence:
        rec = records.get(ev.core)
        if rec is not None and ev.charged is None:
            value = resident(s, ev.core)
            charged.append(replace(ev, charged=value))
            notes.append(
                Announce(
                    "record_unplaced",
                    f"core {ev.core}'s machine check is charged to {value}, its value when the last boot ended: "
                    "its moment in that boot is not known",
                )
            )
        elif rec is not None and rec.fb is not None and ev.charged <= rec.fb:
            notes.append(_repeated(ev, rec.fb))
        else:
            charged.append(ev)
    return tuple(charged), tuple(notes)


def _resumed(
    s: SessionState, after: SessionState, e: CrashEvidenceRead, w: World, streak: int, pauses: tuple[Pause, ...]
) -> Transition:
    if streak >= QUARANTINE_STREAK:
        quarantined = replace(after, lifecycle=Lifecycle.QUARANTINED, flow=None, slot=None, pause=None)
        causes = _causes(after, pauses)
        said = f"{streak} crashes in a row with no test surviving" + (f"; {causes}" if causes else "")
        return Transition(
            quarantined,
            (
                RevertAll(s.baseline, True),
                _commit(quarantined),
                Announce("quarantined", said),
            ),
        )
    if pauses:
        return _one_pause(after, pauses)
    changed = _setup_changes(after.setup, w.setup)
    was, now = dict(after.setup).get(KERNEL, "absent"), dict(w.setup).get(KERNEL, "absent")
    kernel_only = (
        e.child_id is not None
        and _differing(_verdict_part(after.setup), _verdict_part(w.setup)) == [KERNEL]
        and not any(word in kernel for kernel in (was, now) for word in KERNEL_UNREAD)
    )
    if changed and not kernel_only:
        return _enter_pause(
            after,
            PauseReason.SETUP_CHANGED,
            f"the setup changed since the session started: {changed}; {SETUP_REMEDY}",
        )
    moved = _power_moved(after, w)
    if moved is not None:
        return _enter_pause(after, PauseReason.SETUP_CHANGED, moved)
    knobs = _knob_problem(after, e, w)
    if knobs is not None:
        return _enter_pause(after, *knobs)
    if kernel_only:
        said = (
            f"the kernel changed ({was} to {now}): "
            f"session {e.child_id} continues session {s.session_id} under it by itself"
        )
        return _carry_on(after, e.child_id, w.setup, e.co_now, said, _noted(after.setup, w.setup))
    token = after.next_token
    noted = tuple(item for item in w.setup if item[0] in RECORDED_ONLY)
    running = replace(
        after,
        lifecycle=Lifecycle.RUNNING,
        pause=None,
        slot=Waiting(token, 0.0),
        streaks=Streaks(),
        next_token=token + 1,
        setup=tuple(sorted((*_verdict_part(after.setup), *noted))),
    )
    effects: list = [_commit(running)]
    effects += [WriteCO(core, value) for core, value in s.baseline]
    effects += [Announce("resumed", f"session {s.session_id}"), *_noted(after.setup, w.setup), Schedule(token, 0.0)]
    return Transition(running, tuple(effects))


def _held_moved(s: SessionState | Stored, reads: tuple[tuple[int, int | None], ...]) -> tuple[int, ...]:
    base, owned = dict(s.base_co), {core for core, _ in s.baseline}
    return tuple(core for core, value in reads if core not in owned and value != base.get(core))


def _held_base_changes(s: SessionState | Stored, reads: tuple[tuple[int, int | None], ...]) -> str:
    base, values = dict(s.base_co), dict(reads)
    return "; ".join(
        f"core {core} reads {values[core]}, "
        + ("with no CO read at Start" if base.get(core) is None else f"the CO read at Start was {base[core]}")
        for core in _held_moved(s, reads)
    )


def _knob_problem(s: SessionState, e: CrashEvidenceRead, w: World) -> tuple[PauseReason, str, tuple[int, ...]] | None:
    reads = dict(e.co_now)
    cores = [core for core, _ in w.cpus_of]
    unread = tuple(core for core in cores if reads.get(core) is None)
    if unread:
        return PauseReason.SMU_READ, f"the CO of core {', '.join(map(str, unread))} could not be read", unread
    base, baseline, written = dict(s.base_co), dict(s.baseline), dict(e.last_written)
    if not e.same_boot:
        changed = _held_base_changes(s, e.co_now)
        if changed:
            moved = _held_moved(s, e.co_now)
            return (
                PauseReason.SETUP_CHANGED,
                f"the CO of a core the session does not own differs from its read at Start: {changed}",
                moved,
            )
        if not e.marked_now:
            return None
    expected = {
        core: written.get(core, base.get(core) if e.same_boot else baseline[core])
        if core in baseline
        else base.get(core)
        for core in cores
    }
    moved = tuple(
        core
        for core in cores
        if reads[core] != expected[core] and not (e.marked_now and core in baseline and reads[core] == 0)
    )
    if not moved:
        return None
    return (
        PauseReason.OUTSIDE_WRITE,
        f"something else wrote the CO while the session was away: {_left(moved, reads, expected)}",
        moved,
    )


def _left(moved: tuple[int, ...], reads: dict[int, int | None], expected: dict[int, int | None]) -> str:
    return "; ".join(f"core {c} reads {reads[c]}, the session left it at {expected[c]}" for c in moved)


def _co_reads_problem(s: SessionState, w: World) -> tuple[PauseReason, str, tuple[int, ...]] | None:
    cores = tuple(core for core, _ in w.cpus_of)
    reads = dict(w.co_now or ())
    unread = tuple(core for core in cores if reads.get(core) is None)
    if unread:
        return PauseReason.SMU_READ, f"the CO of core {', '.join(map(str, unread))} could not be read", unread
    owned, base = {core for core, _ in s.baseline}, dict(s.base_co)
    cached = {} if w.gap_grew else dict(w.resident)
    expected = {core: cached.get(core) if core in owned else base.get(core) for core in cores}
    moved = tuple(core for core in cores if expected[core] is not None and reads[core] != expected[core])
    if not moved:
        return None
    return (
        PauseReason.OUTSIDE_WRITE,
        f"something else wrote the CO during the session: {_left(moved, reads, expected)}",
        moved,
    )


def _stale_fail(s: SessionState, core: int, w: World) -> tuple[SessionState, str | None]:
    stage = s.flow.stage
    value = resident(s, core)
    if stage is Stage.SEARCH:
        rec = s.core(core)
        if rec.value == 0:
            return s, None
        return s.with_core(search.on_fail(rec, s.config, s.flow.klass, 0.0).record), None
    if stage is Stage.LIVE1:
        return _live1_outcome(s, core, value, False, w)
    if stage is Stage.BISECT:
        return _bisect_outcome(s, False, w)
    if stage is Stage.MEMORY:
        return _memory_flow_rule(s, w)
    if stage in ALLCORE:
        return _allcore_fail(s, None, w)
    raise ValueError(f"a lost verdict has no stage rule in {stage}")


def _flow_crash(s: SessionState, w: World) -> tuple[SessionState, str | None]:
    flow = s.flow
    if flow.stage is Stage.MEMORY:
        return _memory_flow_rule(s, w)
    if flow.stage in ALLCORE:
        return _allcore_fail(s, None, w)
    lane = next(r.core for r in s.cores if r.in_test)
    if flow.stage is Stage.LIVE1:
        values = {r.core: r.value for r in s.cores}
        if not any(values.values()):
            return s, "the machine crashed with every owned core at 0"
        run = bisect.begin(Stage.LIVE1, lane, Klass.T1, values, dict(w.domain_of))
        return replace(s, flow=replace(flow, stage=Stage.BISECT, bisect=run)), None
    if flow.stage is Stage.BISECT:
        run = flow.bisect
        if run.rerun is not None:
            return _q6(s, run.rerun)
        if bisect.retesting(run):
            return _q6(s, run.universe)
        group = bisect.current_group(run)
        if not group:
            return s, "the held values with every owned core at 0 crashed the machine"
        return replace(s, flow=replace(flow, bisect=bisect.record(run, False, dict(w.domain_of)))), None
    raise ValueError(f"a crash with a core in test has no flow rule in {flow.stage}")


def _judge_record(
    s: SessionState, ev: Evidence, at_stock: bool = False, counted: bool = False
) -> tuple[SessionState, Pause | None]:
    owned = {r.core for r in s.cores}
    if ev.core not in owned:
        return s, Pause(
            PauseReason.HELD_KNOB,
            f"core {ev.core} logged a machine check at a value this session does not own",
            (ev.core,),
        )
    rec = s.core(ev.core)
    value = 0 if at_stock else resident(s, ev.core) if ev.charged is None else ev.charged
    stage = s.flow.stage
    if value == 0:
        if stage is Stage.SEARCH:
            return s, Pause(
                PauseReason.STOCK_UNSTABLE, f"core {ev.core} logged a machine check at its stock value", (ev.core,)
            )
        if counted:
            return s, None
        incidents = (*s.incidents, Incident.STOCK_MACHINE_CHECK)
        out = replace(s, incidents=incidents)
        if len(incidents) >= INCIDENT_LIMIT:
            return out, Pause(PauseReason.INCIDENTS, _incidents_text(incidents))
        return out, None
    steps = 1 if ev.corrected else UNCORRECTED_STEPS
    backed = search.back_off_in_place(replace(rec, value=value), s.config, steps)
    if ev.charged is not None:
        backed = replace(backed, value=max(backed.value, rec.value))
    out = s.with_core(backed)
    flow = replace(out.flow, dirty=True)
    if stage is Stage.BISECT and flow.bisect.rerun != flow.bisect.universe:
        flow = _bisect_exit(flow)
    flow = _requeued(flow, ev.core)  # equivalent-mutant: old lines differ only in LIVE1 with no cursor, never reached
    if stage in ALLCORE and flow.allcore is not None:
        flow = replace(flow, allcore=replace(flow.allcore, t_clean=0.0))
    return replace(out, flow=flow), None


def _strength(s: SessionState, ev: Evidence, at_stock: bool, counted: bool) -> tuple[bool | int | tuple[int, ...], ...]:
    out, pause = _judge_record(s, ev, at_stock, counted)
    if pause is not None:
        return (True,)
    after = out.core(ev.core)
    return False, after.value, () if after.fb is None else (after.fb,)


def _strongest(
    s: SessionState, evidence: tuple[Evidence, ...], at_stock: bool, counted: bool
) -> tuple[tuple[Evidence, ...], tuple[Evidence, ...]]:
    strength = [_strength(s, ev, at_stock, counted) for ev in evidence]
    cores = dict.fromkeys(ev.core for ev in evidence)
    kept = [max((i for i, ev in enumerate(evidence) if ev.core == core), key=strength.__getitem__) for core in cores]
    return tuple(evidence[i] for i in kept), tuple(ev for i, ev in enumerate(evidence) if i not in kept)


def _unjudged(ev: Evidence, why: str) -> Announce:
    record = "corrected" if ev.corrected else "uncorrected"
    charge = "with no charge" if ev.charged is None else f"charged to {ev.charged}"
    return Announce("record_unjudged", f"core {ev.core}'s {record} machine check {charge} is not judged: {why}")


def _placed(
    s: SessionState, evidence: tuple[Evidence, ...], knobs: tuple[PauseReason, str, tuple[int, ...]] | None
) -> tuple[tuple[Evidence, ...], tuple[Evidence, ...], tuple[Announce, ...]]:
    records = {r.core: r for r in s.cores}
    reason, _, moved = knobs if knobs is not None else (None, "", ())
    unread = reason is PauseReason.SMU_READ
    placed: list[Evidence] = []
    substituted: list[Evidence] = []
    notes: list[Announce] = []
    for ev in evidence:
        rec = records.get(ev.core)
        if rec is None:
            placed.append(ev)
            continue
        if ev.core in moved:
            why = "its CO could not be read" if unread else "another program wrote its CO"
        elif ev.charged is None:
            why = "the CO journal cannot place its moment"
        else:
            why = None
        if why is not None and rec.value != 0:
            placed.append(replace(ev, charged=rec.value))
            substituted.append(placed[-1])
            notes.append(
                Announce(
                    "record_unplaced",
                    f"core {ev.core}'s machine check is charged to its current value {rec.value}: {why}",
                )
            )
        elif why is not None and not ev.charged:
            notes.append(
                Announce(
                    "record_unjudged",
                    f"core {ev.core}'s machine check is not judged: {why} while the session held it at stock, "
                    "so the value it ran at is unknown",
                )
            )
        elif rec.fb is not None and ev.charged <= rec.fb:
            notes.append(_repeated(ev, rec.fb))
        else:
            placed.append(ev)
    return tuple(placed), tuple(substituted), tuple(notes)


def _repeated(ev: Evidence, fb: int) -> Announce:
    return Announce(
        "record_unjudged",
        f"core {ev.core}'s machine check is not judged: it is charged to {ev.charged}, "
        f"at or behind its fail bound {fb}, so it repeats a failure already judged",
    )


def _kernel_log_read(s: SessionState, e: KernelLogRead) -> Transition:
    if s.lifecycle is not Lifecycle.RUNNING or not isinstance(s.slot, Waiting) or s.slot.token != e.token:
        return Transition(s)
    if e.problem is not None:
        return _enter_pause(
            s, PauseReason.KERNEL_LOG_UNREAD, f"the kernel log since the resume could not be read: {e.problem}"
        )
    faults = tuple(Announce("program_fault", line) for line in e.program_faults)
    evidence, substituted, notes = _placed(s, e.evidence, None)
    judged, aside = _evidence_applied(_set_failed(s, evidence, substituted), evidence)
    if isinstance(judged, Transition):
        return replace(judged, effects=(*judged.effects, *notes, *aside, *faults))
    return Transition(judged, (_commit(judged), *notes, *aside, *faults, Schedule(e.token, 0.0)))


def _evidence_applied(
    s: SessionState, evidence: tuple[Evidence, ...]
) -> tuple[SessionState | Transition, tuple[Announce, ...]]:
    judged, pauses, notes = _records_judged(s, evidence, False, False)
    return (_one_pause(judged, pauses) if pauses else judged), notes


def _records_judged(
    s: SessionState, evidence: tuple[Evidence, ...], at_stock: bool, counted: bool, once: bool = False
) -> tuple[SessionState, tuple[Pause, ...], tuple[Announce, ...]]:
    stock = tuple(ev for ev in evidence if not counted and _judge_record(s, ev, at_stock)[0].incidents != s.incidents)
    pauses: list[Pause] = []
    if stock:
        counted = True
        cores = len(dict.fromkeys(ev.core for ev in stock))
        s = replace(s, incidents=(*s.incidents, *(Incident.STOCK_MACHINE_CHECK,) * (1 if once else cores)))
        s = _owe_replay(s, _held(s))
        if len(s.incidents) >= INCIDENT_LIMIT:
            pauses.append(Pause(PauseReason.INCIDENTS, _incidents_text(s.incidents)))
    kept, aside = _strongest(s, evidence, at_stock, counted)
    counted_in = "the dead boot's one incident" if once else "its core's stock incident"
    notes: list[Announce] = []
    for ev in aside:
        stronger = f"a stronger record of core {ev.core} in the same read decided its verdict"
        notes.append(_unjudged(ev, f"it is counted in {counted_in}, and {stronger}" if ev in stock else stronger))
    for ev in kept:
        s, pause = _judge_record(s, ev, at_stock, counted)
        if pause is not None:
            pauses.append(pause)
    return s, tuple(pauses), tuple(notes)


def _one_pause(s: SessionState, pauses: tuple[Pause, ...]) -> Transition:
    cores = tuple(core for pause in pauses for core in pause.cores)
    return _enter_pause(s, pauses[0].reason, _causes(s, pauses), cores)


def _causes(s: SessionState, pauses: tuple[Pause, ...]) -> str:
    return "; ".join(
        dict.fromkeys(
            _incidents_text(s.incidents) if pause.reason is PauseReason.INCIDENTS else pause.detail for pause in pauses
        )
    )


def _pause_request(s: SessionState) -> Transition:
    if s.lifecycle is Lifecycle.RUNNING and isinstance(s.slot, InFlight) and s.slot.spec.load in ALLCORE_LOADS:
        stage = s.slot.spec.stage
        paused = _enter_pause(s, PauseReason.USER, f"paused by the user; the cut {stage} test runs again on resume")
        return replace(paused, state=replace(paused.state, pause=replace(paused.state.pause, cut=s.slot)))
    if s.lifecycle is Lifecycle.RUNNING and isinstance(s.slot, InFlight):
        pausing = replace(s, lifecycle=Lifecycle.PAUSING, pause=Pause(PauseReason.USER, "paused by the user"))
        return Transition(pausing, (_commit(pausing), Announce("pausing", "finishing the current test")))
    if s.lifecycle is Lifecycle.RUNNING:
        return _enter_pause(s, PauseReason.USER, "paused by the user")
    if s.lifecycle in (Lifecycle.PAUSING, Lifecycle.PAUSED):
        return Transition(s, refused="already paused")
    return Transition(s, refused="nothing is running")


def _failure_unresolved(s: SessionState) -> bool:
    flow = s.flow
    solo = flow.live1 is not None and flow.live1.solo is not None
    return bool(flow.unreplayed) or flow.bisect is not None or solo or _barring(s) is not None


def _abort(s: SessionState) -> Transition:
    if s.lifecycle not in HELD:
        return Transition(s, refused="nothing to abort")
    s = _clear_in_test(_keep_pick(s))
    profile = s.flow.validating if s.flow is not None else None
    unmoved = profile is not None and not s.flow.carried and tuple((r.core, r.value) for r in s.cores) == profile
    if unmoved and not _failure_unresolved(s):
        effects = _stop_effects(s, profile, journal=True)
        done = replace(s, lifecycle=Lifecycle.COMPLETED, flow=None, slot=None, pause=None, streaks=Streaks())
        effects += [
            _commit(done),
            Announce("completed", "validation aborted; the session stays completed and its values are sent back"),
        ]
        return Transition(done, tuple(effects))
    said = (
        "validation aborted with a failure it had not resolved, so its values are not restored"
        if unmoved
        else "aborted on request"
    )
    effects = _stop_effects(s, s.baseline, journal=True)
    effects += [
        Commit(replace(stored(s), status="aborted")),
        Announce("aborted", f"{said}; every owned core is sent back to its baseline"),
    ]
    aborted = replace(s, lifecycle=Lifecycle.ABORTED, flow=None, slot=None, pause=None, streaks=Streaks())
    return Transition(aborted, tuple(effects))


def _close(s: SessionState) -> Transition:
    if s.lifecycle not in HELD:
        return Transition(SessionState(next_token=s.next_token), (CloseDb(),))
    s = _keep_pick(s)
    database = _database_paused(s)
    effects = _stop_effects(s, s.baseline, journal=not database)
    if not database:
        paused = _clear_in_test(
            replace(
                s, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(PauseReason.USER, "closed"), streaks=Streaks()
            )
        )
        effects.append(_commit(paused))
    effects.append(CloseDb())
    return Transition(SessionState(next_token=s.next_token), tuple(effects))


def _cpus_for(core: int, klass: Klass, w: World) -> tuple[int, ...]:
    cpus = dict(w.cpus_of)[core]
    if klass is Klass.T2:
        return tuple(cpus[:2])
    if core == 0 and len(cpus) > 1:
        return (cpus[1],)
    return (cpus[0],)


def _continuation(s: SessionState, e: ContinuationDue, w: World) -> Transition:
    if s.lifecycle is not Lifecycle.RUNNING or not isinstance(s.slot, Waiting) or s.slot.token != e.token:
        return Transition(s)
    slot = s.slot
    if slot.cool_below_c is not None:
        return _cooling(s, slot, w)
    return _next(s, w, slot.picked)


def _next(s: SessionState, w: World, picked: int | None) -> Transition:
    if s.flow.stage is Stage.SOAK:
        s = replace(s, flow=_past_memory(s.flow, s.config))
    stage = s.flow.stage
    if stage is Stage.FINALIZE:
        return _finalize(s, w)
    if stage is Stage.MEMORY:
        return _memory_next(s, w)
    if stage in ALLCORE:
        return _allcore_next(s, w)
    if stage is Stage.LIVE1:
        return _live1_next(s, w, picked)
    if stage is Stage.BISECT:
        return _bisect_next(s, w)
    if search.class_done(s.cores):
        return _end_class(s, w)
    return _search_next(s, w, picked)


def _cooling(s: SessionState, slot: Waiting, w: World) -> Transition:
    if w.temperature_c is not None and slot.cool_below_c is not None and w.temperature_c < slot.cool_below_c:
        return _next(replace(s, slot=Waiting(slot.token, 0.0)), w, None)
    if slot.cool_deadline is not None and w.now >= slot.cool_deadline:
        if w.temperature_c is None:
            return _enter_pause(
                s, PauseReason.APPARATUS, "the CPU temperature could not be read, so cooling was never confirmed"
            )
        return _enter_pause(
            s,
            PauseReason.THERMAL,
            f"the CPU was at {w.temperature_c:.1f} C, not below {slot.cool_below_c:.1f} C, "
            "when the cool-down cap ran out",
        )
    token = s.next_token
    waiting = replace(slot, token=token, seconds=COOL_POLL_S)
    return Transition(replace(s, slot=waiting, next_token=token + 1), (Schedule(token, COOL_POLL_S),))


def _end_class(s: SessionState, w: World) -> Transition:
    flow = s.flow
    cores = tuple(r.core for r in s.cores)
    if flow.klass is Klass.T1 and _two_cpus_everywhere(cores, w):
        two = replace(
            s,
            cores=tuple(search.begin_2t(r) for r in s.cores),
            flow=Flow(Stage.SEARCH, Klass.T2, last_core=flow.last_core, carried=flow.carried),
        )
        return _search_next(two, w, None)
    entered = _enter_live1(s, w)
    if flow.klass is Klass.T1:
        single = sorted(core for core in cores if len(dict(w.cpus_of).get(core, ())) < 2)
        skipped = Announce("skipped", f"the 2T class is skipped: cores {single} have one online CPU")
        return replace(entered, effects=(skipped, *entered.effects))
    return entered


def _enter_live1(s: SessionState, w: World) -> Transition:
    flow = Flow(
        Stage.LIVE1,
        last_core=s.flow.last_core,
        live1=Live1(),
        allcore=s.flow.allcore,
        validating=s.flow.validating,
        carried=s.flow.carried,
        unreplayed=s.flow.unreplayed,
    )
    return _live1_next(replace(s, flow=flow), w, None)


def _wait_first(s: SessionState, flow: Flow, core: int) -> Transition | None:
    cfg = s.config
    if flow.last_core is not None and core != flow.last_core and cfg.wait_between_cores_s > 0:
        token = s.next_token
        waiting = replace(
            s, flow=flow, slot=Waiting(token, cfg.wait_between_cores_s, picked=core), next_token=token + 1
        )
        return Transition(waiting, (Schedule(token, cfg.wait_between_cores_s),))
    return None


def _search_next(s: SessionState, w: World, picked: int | None) -> Transition:
    cfg = s.config
    flow = s.flow
    cores = tuple(r.core for r in s.cores)
    if picked is None:
        core, cursor = next_core(cfg.method, cores, dict(w.domain_of), s.seed, flow.klass.value, flow.cursor, flow.owed)
        repeat = flow.owed is not None
        flow = replace(flow, cursor=cursor)
        if not repeat:
            waiting = _wait_first(s, flow, core)
            if waiting is not None:
                return waiting
    else:
        core = picked
    rec = search.resolve_start(s.core(core), cfg)
    vector = tuple((c, rec.value if c == core else base) for c, base in s.baseline)
    seconds = cfg.test_seconds_1t if flow.klass is Klass.T1 else cfg.test_seconds_2t
    spec = TestSpec(flow.klass, core, rec.value, _cpus_for(core, flow.klass, w), seconds, vector, Stage.SEARCH)
    return _start_test(s.with_core(rec), replace(flow, owed=None), spec, w)


def _live1_sequence(s: SessionState, w: World) -> tuple[int, ...]:
    cores = tuple(r.core for r in s.cores)
    return round_sequence(s.config.method, cores, dict(w.domain_of), s.seed, "live1", 0)


def _live1_next(s: SessionState, w: World, picked: int | None) -> Transition:
    flow = s.flow
    live = flow.live1
    sequence = _live1_sequence(s, w)
    if live.solo is not None:
        core = live.solo
        vector = tuple((c, base) for c, base in s.baseline)
    elif live.index < len(sequence):
        core = sequence[live.index]
        vector = tuple((r.core, r.value) for r in s.cores)
        flow = replace(flow, live1=replace(live, requeue=tuple(c for c in live.requeue if c != core)))
    elif live.requeue:
        core = live.requeue[0]
        vector = tuple((r.core, r.value) for r in s.cores)
    elif live.back is not None:
        carried = flow.carried and live.back is not Stage.FINALIZE
        return _next(replace(s, flow=replace(flow, stage=live.back, live1=None, carried=carried)), w, None)
    else:
        return _end_live1(s, w)
    if picked is None:
        waiting = _wait_first(s, flow, core)
        if waiting is not None:
            return waiting
    spec = TestSpec(
        Klass.T1, core, dict(vector)[core], _cpus_for(core, Klass.T1, w), s.config.test_seconds_1t, vector, Stage.LIVE1
    )
    return _start_test(s, flow, spec, w)


def _end_live1(s: SessionState, w: World) -> Transition:
    cursor = s.flow.allcore
    fresh = cursor is None
    if fresh:
        cursor = AllCore(floors=tuple((r.core, r.value) for r in s.cores))
    else:
        cursor = replace(cursor, t_idx=0, t_clean=0.0, pass_no=cursor.pass_no + 1)
    if s.flow.validating is not None:
        return _next(replace(s, flow=_past_tighten(replace(s.flow, live1=None, allcore=cursor), s.config)), w, None)
    if fresh:
        s = _entry_margin(s, cursor)
    flow = replace(s.flow, stage=Stage.STEADY, live1=None, allcore=cursor)
    return _allcore_next(replace(s, flow=flow), w)


def _entry_margin(s: SessionState, cursor: AllCore) -> SessionState:
    floors, margin = dict(cursor.floors), allcore.ENTRY_MARGIN_STEPS * s.config.fine_step
    entered = (replace(r, value=max(r.value, min(0, floors.get(r.core, r.value) + margin))) for r in s.cores)
    return replace(s, cores=tuple(entered))


def _reentered(s: SessionState) -> SessionState:
    flow = s.flow
    if flow is None or flow.validating is not None:
        return s
    entry = flow.bisect.entry if flow.stage is Stage.BISECT else flow.stage
    if entry not in ALLCORE:
        return s
    cursor = replace(flow.allcore, t_idx=0, t_clean=0.0, rounds=0, tightened=False)
    entered = replace(flow, stage=Stage.STEADY, bisect=None, allcore=cursor, one_off=None)
    return _entry_margin(replace(s, flow=entered), cursor)


def _frozen(cursor: AllCore, cores: tuple[int, ...]) -> AllCore:
    return replace(cursor, frozen=(*cursor.frozen, *cores), t_idx=0, t_clean=0.0)


def _round_passed(s: SessionState, cursor: AllCore) -> tuple[SessionState, AllCore]:
    for core in allcore.round_members(s.cores, s.config, cursor):
        rec = s.core(core)
        s = s.with_core(replace(rec, value=rec.value - s.config.fine_step))
    return s, replace(cursor, rounds=cursor.rounds + 1, t_idx=0, t_clean=0.0)


def _reclaim_ended(s: SessionState) -> SessionState:
    floors = dict(s.flow.allcore.floors)
    off_floor = any(r.value != floors.get(r.core, r.value) for r in s.cores)
    flow = replace(s.flow, dirty=s.flow.dirty or off_floor)
    return replace(s, flow=_past_tighten(flow, s.config))


def _load_seconds(cfg, load: Load) -> float:
    if load is Load.HOT:
        return cfg.hot_seconds
    if load is Load.SOAK:
        return cfg.soak_seconds
    return dict(cfg.stage_seconds)[load.value]


STAGE_CYCLES = {
    Stage.STEADY: (Load.HOT,),
    Stage.TRANSIENT: TRANSIENT_CYCLE,
    Stage.TIGHTEN: ROUND_CYCLE,
    Stage.ENDURANCE: ENDURANCE_CYCLE,
    Stage.SOAK: (Load.SOAK,),
}


def _stage_load(stage: Stage, cursor: AllCore) -> Load:
    cycle = STAGE_CYCLES[stage]
    return cycle[cursor.t_idx % len(cycle)]


def _plan_seed(s: SessionState, stage: Stage, load: Load) -> str:
    cursor = s.flow.allcore
    return f"{s.seed}:{stage.value}:{load.value}:{cursor.pass_no}:{cursor.rounds}:{cursor.t_idx}"


def _all_cpus(s: SessionState, w: World) -> tuple[int, ...]:
    cpus = dict(w.cpus_of)
    return tuple(sorted(cpu for r in s.cores for cpu in cpus[r.core]))


def _loaded_cpus(s: SessionState, w: World, load: Load) -> tuple[int, ...]:
    return () if load is Load.SOAK else _all_cpus(s, w)


def _past_tighten(flow: Flow, cfg) -> Flow:
    cursor = replace(flow.allcore, tightened=True, t_idx=0, t_clean=0.0)
    if cfg.endurance:
        return replace(flow, stage=Stage.ENDURANCE, allcore=cursor)
    return _past_endurance(replace(flow, allcore=cursor), cfg)


def _past_endurance(flow: Flow, cfg) -> Flow:
    if cfg.memory:
        return replace(flow, stage=Stage.MEMORY)
    return _past_memory(flow, cfg)


def _past_memory(flow: Flow, cfg) -> Flow:
    if cfg.soak and not flow.dirty:
        return replace(flow, stage=Stage.SOAK)
    return replace(flow, stage=Stage.FINALIZE, applied=False)


def _after_tighten(s: SessionState, w: World) -> Transition:
    return _next(_reclaim_ended(s), w, None)


def _allcore_next(s: SessionState, w: World) -> Transition:
    cfg = s.config
    flow = s.flow
    cursor = flow.allcore
    values = {r.core: r.value for r in s.cores}
    if flow.stage is Stage.TIGHTEN:
        members = allcore.round_members(s.cores, s.config, cursor)
        if not members or cursor.rounds >= allcore.ENTRY_MARGIN_STEPS:
            return _after_tighten(s, w)
        for core in members:
            values[core] -= cfg.fine_step
        if any(_at_or_past(values, failed) for failed in s.failed_vectors):
            return _after_tighten(s, w)
    load = _stage_load(flow.stage, cursor)
    first = s.cores[0].core
    vector = tuple((core, values[core]) for core, _ in s.baseline)
    cpus = _loaded_cpus(s, w, load)
    seconds = _load_seconds(cfg, load)
    plan_seed = _plan_seed(s, flow.stage, load)
    spec = TestSpec(Klass.T1, first, values[first], cpus, seconds, vector, flow.stage, load, plan_seed=plan_seed)
    return _start_test(s, flow, spec, w)


def _allcore_fail(s: SessionState, ctx, w: World) -> tuple[SessionState, str | None]:
    flow = s.flow
    cursor = flow.allcore
    stage = flow.stage
    if stage is Stage.TIGHTEN:
        return _reclaim_ended(s), None
    values = {r.core: r.value for r in s.cores}
    if not any(values.values()):
        return s, "the all-core load fails with every owned core at 0"
    run = bisect.begin(stage, s.cores[0].core, Klass.T1, values, dict(w.domain_of))
    run = replace(run, quick=stage in (Stage.STEADY, Stage.ENDURANCE) and allcore.steady_class(ctx))
    cursor = replace(cursor, t_clean=0.0)
    return replace(s, flow=replace(flow, stage=Stage.BISECT, bisect=run, allcore=cursor)), None


def _memory_next(s: SessionState, w: World) -> Transition:
    values = {r.core: r.value for r in s.cores}
    first = s.cores[0].core
    vector = tuple((core, values[core]) for core, _ in s.baseline)
    cfg = s.config
    spec = TestSpec(
        Klass.T1, first, values[first], _all_cpus(s, w), cfg.memory_seconds, vector, Stage.MEMORY, Load.MEMORY
    )
    return _start_test(s, s.flow, spec, w)


def _memory_flow_rule(s: SessionState, w: World) -> tuple[SessionState, str | None]:
    values = {r.core: r.value for r in s.cores}
    if not any(values.values()):
        return s, "the memory load fails with every owned core at 0"
    run = bisect.begin(Stage.MEMORY, s.cores[0].core, Klass.T1, values, dict(w.domain_of))
    return replace(s, flow=replace(s.flow, stage=Stage.BISECT, bisect=run)), None


def _memory_lanes(s: SessionState, named: tuple[int, ...], w: World) -> SessionState | Transition:
    owned = {r.core for r in s.cores}
    held = tuple(core for core in named if core not in owned)
    if held:
        return _enter_pause(
            s,
            PauseReason.HELD_KNOB,
            f"the memory lane of core {held[0]} failed at a value this session does not own",
            held,
        )
    if not named:
        s, stock = _memory_flow_rule(s, w)
        return s if stock is None else _enter_pause(s, PauseReason.STOCK_UNSTABLE, stock)
    requeue: list[int] = []
    solo = None
    for core in named:
        rec = s.core(core)
        if rec.value < 0:
            s = s.with_core(search.back_off_in_place(rec, s.config, 1))
            requeue.append(core)
        elif solo is None and any(r.value != 0 for r in s.cores if r.core != core):
            solo = core
        elif solo is None:
            return _enter_pause(
                s, PauseReason.STOCK_UNSTABLE, f"core {core} fails the memory load with every core at 0", (core,)
            )
    live = Live1(index=len(_live1_sequence(s, w)), requeue=tuple(requeue), solo=solo, back=Stage.MEMORY)
    return replace(s, flow=replace(s.flow, stage=Stage.LIVE1, live1=live, dirty=True))


def _memory_verdict(s: SessionState, spec: TestSpec, rep: Report, group: Group, pausing: bool, w: World) -> Transition:
    s = replace(s, streaks=Streaks())
    if rep.skipped and Stage.MEMORY in s.flow.unreplayed:
        return _enter_pause(
            s,
            PauseReason.APPARATUS,
            f"MEMORY was skipped, and this validation owes a clean MEMORY pass: {rep.skipped}; "
            "fix the memory test and resume, or Abort, which ends the session aborted",
        )
    if rep.skipped:
        skipped = _next_wait(replace(s, flow=_past_memory(s.flow, s.config)), spec, pausing)
        notice = Announce("memory_skipped", f"MEMORY skipped, not a stability verdict: {rep.skipped}")
        return replace(skipped, effects=(notice, *skipped.effects))
    if group is Group.PASS:
        return _next_wait(replace(s, flow=_past_memory(s.flow, s.config)), spec, pausing)
    fixed = _memory_lanes(s, rep.named, w)
    if isinstance(fixed, Transition):
        return fixed
    return _next_wait(fixed, spec, pausing)


def _allcore_named(s: SessionState, named: tuple[int, ...]) -> SessionState | Transition | None:
    owned = {r.core for r in s.cores}
    held = tuple(core for core in named if core not in owned)
    if held:
        return _enter_pause(
            s, PauseReason.HELD_KNOB, f"y-cruncher named core {held[0]}, whose value this session does not own", held
        )
    flow = s.flow
    if flow.stage is Stage.TIGHTEN:
        hit = tuple(core for core in allcore.round_members(s.cores, s.config, flow.allcore) if core in named)
        if hit:
            return replace(s, flow=replace(flow, allcore=_frozen(flow.allcore, hit)))
    movable = [core for core in named if s.core(core).value < 0]
    if not movable:
        return None
    for core in movable:
        s = s.with_core(search.back_off_in_place(s.core(core), s.config, 1))
    restart = 0 if flow.stage is Stage.TIGHTEN else flow.allcore.t_idx
    return replace(s, flow=replace(flow, allcore=replace(flow.allcore, t_idx=restart, t_clean=0.0), dirty=True))


def _allcore_verdict(s: SessionState, spec: TestSpec, rep: Report, group: Group, pausing: bool, w: World) -> Transition:
    cfg = s.config
    flow = s.flow
    cursor = flow.allcore
    s = replace(s, streaks=Streaks())
    if group is Group.SILICON:
        fixed = _allcore_named(s, rep.named)
        if isinstance(fixed, Transition):
            return fixed
        if fixed is not None:
            return _next_wait(fixed, spec, pausing)
        s, stock = _allcore_fail(s, rep.context, w)
        if stock is not None:
            return _enter_pause(s, PauseReason.STOCK_UNSTABLE, stock)
        return _next_wait(s, spec, pausing)
    if flow.stage is Stage.STEADY:
        flow = replace(flow, stage=Stage.TRANSIENT, allcore=replace(cursor, t_idx=0, t_clean=0.0))
    elif flow.stage is Stage.SOAK:
        flow = replace(flow, stage=Stage.FINALIZE, applied=False)
    else:
        cursor = replace(cursor, t_idx=cursor.t_idx + 1, t_clean=cursor.t_clean + rep.seconds)
        window = cfg.endurance_window_s if flow.stage is Stage.ENDURANCE else cfg.transient_window_s
        if flow.stage is Stage.TIGHTEN:
            if cursor.t_idx >= len(ROUND_CYCLE):
                s, cursor = _round_passed(s, cursor)
            flow = replace(flow, allcore=cursor)
        elif cursor.t_clean < window:
            flow = replace(flow, allcore=cursor)
        elif flow.stage is Stage.ENDURANCE:
            flow = _past_endurance(replace(flow, allcore=cursor), cfg)
        elif not cursor.tightened:
            flow = replace(flow, stage=Stage.TIGHTEN, allcore=replace(cursor, t_idx=0, t_clean=0.0))
        else:
            flow = _past_tighten(replace(flow, allcore=cursor), cfg)
    return _next_wait(replace(s, flow=flow), spec, pausing)


def _bisect_next(s: SessionState, w: World) -> Transition:
    flow = s.flow
    run = flow.bisect
    group = bisect.current_group(run)
    values = {r.core: r.value for r in s.cores} if run.rerun is not None else bisect.slot_values(run, group)
    vector = tuple((c, values[c] if c in group else base) for c, base in s.baseline)
    lane = run.lane
    quick = run.quick and group != run.universe
    if quick:
        cpus, seconds, load = _all_cpus(s, w), s.config.bisect_seconds, Load.HOT
    elif run.entry in ALLCORE:
        load = _stage_load(run.entry, flow.allcore)
        cpus, seconds = _loaded_cpus(s, w, load), _load_seconds(s.config, load)
    elif run.entry is Stage.MEMORY:
        cpus, seconds, load = _all_cpus(s, w), s.config.memory_seconds, Load.MEMORY
    else:
        cpus, seconds, load = _cpus_for(lane, run.klass, w), s.config.test_seconds_1t, Load.ONE_T
    plan_seed = _plan_seed(s, run.entry, load) if run.entry in ALLCORE else None
    spec = TestSpec(
        run.klass, lane, dict(vector)[lane], cpus, seconds, vector, Stage.BISECT, load, quick=quick, plan_seed=plan_seed
    )
    return _start_test(s, flow, spec, w)


def _verdict_part(setup: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    return tuple(item for item in setup if item[0] not in RECORDED_ONLY)


def _setup_changes(recorded: tuple[tuple[str, str], ...], now: tuple[tuple[str, str], ...]) -> str:
    return _changes(_verdict_part(recorded), _verdict_part(now))


def _noted(recorded: tuple[tuple[str, str], ...], now: tuple[tuple[str, str], ...]) -> tuple[Announce, ...]:
    was, found = dict(recorded), dict(now)
    return tuple(
        Announce("setup_noted", f"{name} was {was.get(name, 'absent')}, is now {found.get(name, 'absent')}")
        for name in sorted(RECORDED_ONLY)
        if was.get(name) != found.get(name)
    )


def _differing(recorded: tuple[tuple[str, str], ...], now: tuple[tuple[str, str], ...]) -> list[str]:
    was, found = dict(recorded), dict(now)
    return [name for name in sorted(was.keys() | found.keys()) if was.get(name) != found.get(name)]


def _changes(recorded: tuple[tuple[str, str], ...], now: tuple[tuple[str, str], ...]) -> str:
    was, found = dict(recorded), dict(now)
    return "; ".join(
        f"{name} was {was.get(name, 'absent')}, is now {found.get(name, 'absent')}"
        for name in _differing(recorded, now)
    )


def _power_moved(s: SessionState, w: World) -> str | None:
    if s.power == w.power:
        return None
    return f"the power state changed since the session started: {_changes(s.power, w.power)}; restore it and resume"


def _start_test(s: SessionState, flow: Flow, spec: TestSpec, w: World) -> Transition:
    moved = _power_moved(s, w)
    if moved is not None:
        return _enter_pause(replace(s, flow=flow), PauseReason.SETUP_CHANGED, moved)
    outside = [
        (core, value)
        for core, value in spec.vector
        if value != 0 and not s.core(core).inside(value, s.config.max_offset)
    ]
    if outside:
        core, value = outside[0]
        detail = (
            f"core {core} would run at {value}, outside {s.config.max_offset}..0 "
            f"or at or past its fail bound {s.core(core).fb}"
        )
        return _enter_pause(replace(s, flow=flow), PauseReason.INTERNAL, detail)
    problem = _co_reads_problem(s, w)
    if problem is not None:
        return _enter_pause(replace(s, flow=flow), *problem)
    limit = s.config.thermal_limit_c if w.tjmax_c is None else min(s.config.thermal_limit_c, w.tjmax_c)
    spec = replace(spec, thermal_limit_c=limit)
    token = s.next_token
    flow = replace(flow, last_core=spec.core)
    marked = s.with_core(replace(s.core(spec.core), in_test=True))
    if spec.load in ALLCORE_LOADS:
        marked = replace(s, cores=tuple(replace(r, in_test=True) for r in s.cores))
    started = replace(marked, flow=flow, slot=InFlight(token, spec), next_token=token + 1)
    effects: list = []
    if w.gap_grew:
        effects.append(ForgetResident())
    writes = [(core, value) for core, value in spec.vector if w.gap_grew or w.resident_of(core) != value]
    effects += [WriteCO(core, value) for core, value in sorted(writes, key=lambda write: _write_rank(w, *write))]
    effects += [_commit(started), StartWorker(token, spec)]
    return Transition(started, tuple(effects))


def _write_rank(w: World, core: int, value: int) -> int:
    held = None if w.gap_grew else w.resident_of(core)
    if held is None:
        return 1
    return 0 if value > held else 2  # equivalent-mutant: only the ranks' order counts, held never equals value


def _at_or_past(values: dict[int, int], vector: tuple[tuple[int, int], ...]) -> bool:
    return all(values[core] <= value for core, value in vector)


def _barring(s: SessionState) -> tuple[tuple[int, int], ...] | None:
    held = dict(_held(s))
    return next((v for v in s.failed_vectors if v not in s.cleared_vectors and _at_or_past(held, v)), None)


def _unbarred(s: SessionState) -> tuple[SessionState, tuple[Announce, ...]]:
    """Each core a barring vector moved loosens one fine step with no fail bound, since the cores failed together
    (D102), until no remembered failure its own replay never cleared bars the held values (D118)."""
    barred = []
    while (vector := _barring(s)) is not None:
        barred.append(vector)
        moved = {core for core, value in vector if value}
        s = replace(s, cores=tuple(search.loosen(r, s.config) if r.core in moved else r for r in s.cores))
    if not barred:
        return s, ()
    vectors = "; ".join(", ".join(f"{core}: {value}" for core, value in vector) for vector in barred)
    values = ", ".join(f"{r.core}: {r.value}" for r in s.cores)
    said = f"the held values were at or past failed vectors no replay cleared ({vectors}); loosened to {values}"
    return s, (Announce("loosened", f"{said}, and LIVE1 proves them again"),)


def _finalize(s: SessionState, w: World) -> Transition:
    s, loosened = _unbarred(s)
    flow = s.flow
    if loosened or flow.dirty or (flow.unreplayed and _held(s) == flow.validating):
        entered = _enter_live1(s, w)
        return replace(entered, effects=(*loosened, *entered.effects))
    if flow.carried:
        final = replace(flow, stage=Stage.LIVE1, live1=Live1(back=Stage.FINALIZE))
        return _live1_next(replace(s, flow=final), w, None)
    if not flow.applied:
        token = s.next_token
        applied = replace(s, flow=replace(flow, applied=True), slot=Waiting(token, 0.0), next_token=token + 1)
        effects: list = [WriteCO(r.core, r.value) for r in s.cores if w.resident_of(r.core) != r.value]
        effects += [Schedule(token, 0.0)]
        return Transition(applied, tuple(effects))
    problem = _co_reads_problem(s, w)
    if problem is not None:
        return _enter_pause(s, *problem)
    done = replace(s, lifecycle=Lifecycle.COMPLETED, flow=None, slot=None, incidents=())
    values = ", ".join(f"{r.core}: {r.value}" for r in s.cores)
    return Transition(done, (_commit(done), Announce("completed", f"final values {values}")))


SURVIVING = frozenset({Group.PASS, Group.SILICON, Group.THERMAL})


def _survival_mark(rep: Report) -> MarkSurvived | None:
    if rep.group not in SURVIVING or rep.unblamed_corrected or rep.skipped:
        return None
    return MarkSurvived(tuple(sorted({ev.core for ev in rep.evidence if ev.corrected})))


def _reported(s: SessionState, e: WorkerReported, w: World) -> Transition:
    cut = None if s.pause is None else s.pause.cut
    if cut is not None and cut.token == e.token:
        return _cut_reported(s, cut, e.report, w)
    if s.lifecycle not in (Lifecycle.RUNNING, Lifecycle.PAUSING) or not isinstance(s.slot, InFlight):
        return Transition(s)
    if s.slot.token != e.token:
        return Transition(s)
    mark = _survival_mark(e.report)
    result = _judged(s if mark is None else replace(s, resume_streak=0), e.report, w)
    return result if mark is None else replace(result, effects=(mark, *result.effects))


def _cut_reported(s: SessionState, cut: InFlight, rep: Report, w: World) -> Transition:
    if rep.group is not Group.SILICON or not rep.named or rep.evidence:
        return Transition(s)
    judged = _judged(replace(s, lifecycle=Lifecycle.PAUSING, slot=cut), rep, w)
    named = ", ".join(str(core) for core in rep.named)
    said = Announce("cut_judged", f"the cut {cut.spec.stage} test named core {named}: {rep.detail}")
    return replace(judged, effects=(*judged.effects, said))


def _replay_stage(flow: Flow) -> Stage:
    """FINALIZE runs no test of its own: what fails there is owed by LIVE1, the first stage of the pass FINALIZE
    sends a validation that still owes a failure back through."""
    if flow.bisect is not None:
        return flow.bisect.entry
    return Stage.LIVE1 if flow.stage is Stage.FINALIZE else flow.stage


def _owe_replay(s: SessionState, vector: tuple[tuple[int, int], ...]) -> SessionState:
    flow = s.flow
    stage = _replay_stage(flow)
    if vector != flow.validating or stage in flow.unreplayed:
        return s
    return replace(s, flow=replace(flow, unreplayed=(*flow.unreplayed, stage)))


def _remember(s: SessionState, vector: tuple[tuple[int, int], ...]) -> SessionState:
    """Only a vector's first failure outside a Bisect can be cleared, by the next clean all-core pass at it; a
    Bisect slot's failure never is, and a later failure ends that for good, a cleared mark included, since the
    owner's one-off is a single failure (D95, D116, D118)."""
    if not any(value for _, value in vector):
        return s
    if vector in s.failed_vectors:
        cleared = tuple(v for v in s.cleared_vectors if v != vector)
        return replace(s, cleared_vectors=cleared, clearable_vector=None)
    clearable = vector if s.flow.bisect is None else None
    return replace(s, failed_vectors=(*s.failed_vectors, vector), clearable_vector=clearable)


def _replayed(s: SessionState, spec: TestSpec, rep: Report) -> SessionState:
    if rep.group is Group.SILICON or (rep.group is Group.ENVIRONMENT and spec.load is Load.SOAK):
        failed = _remember(s, spec.vector) if spec.load in ALLCORE_LOADS or s.flow.bisect is not None else s
        return _owe_replay(failed, spec.vector)
    if rep.group is not Group.PASS or rep.evidence or rep.skipped:
        return s
    if spec.load in ALLCORE_LOADS and spec.vector == s.clearable_vector:
        s = replace(s, cleared_vectors=(*s.cleared_vectors, spec.vector), clearable_vector=None)
    flow = s.flow
    if spec.vector != flow.validating:
        return s
    cleared = (_replay_stage(flow), Stage.LIVE1)
    return replace(s, flow=replace(flow, unreplayed=tuple(owed for owed in flow.unreplayed if owed not in cleared)))


def _judged(s: SessionState, rep: Report, w: World) -> Transition:
    spec = s.slot.spec
    s = _replayed(s, spec, rep)
    whole = spec.load in ALLCORE_LOADS
    ran = dict(spec.vector)
    records = tuple(
        ev if ev.charged is not None or ev.core not in ran else replace(ev, charged=ran[ev.core])
        for ev in rep.evidence
        if whole or ev.core != spec.core
    )
    after, aside = _evidence_applied(_set_failed(s if whole else _after_test(s, spec.core), records, ()), records)
    result = after if isinstance(after, Transition) else _graded(s, after, rep, w)
    return replace(result, effects=(*result.effects, *aside))


def _graded(s: SessionState, after: SessionState, rep: Report, w: World) -> Transition:
    spec = s.slot.spec
    pausing = s.lifecycle is Lifecycle.PAUSING
    if spec.load in ALLCORE_LOADS:
        after = _clear_in_test(after)
        if rep.evidence:
            return _next_wait(replace(after, streaks=Streaks()), spec, pausing)
    if s.flow.stage is Stage.BISECT and after.flow.stage is not Stage.BISECT:
        return _next_wait(after, spec, pausing)
    if spec.stage is Stage.BISECT and any(ev.core != spec.core for ev in rep.evidence):
        return _next_wait(replace(after, streaks=Streaks()), spec, pausing)
    group = Group.APPARATUS if rep.skipped and spec.stage is not Stage.MEMORY else rep.group
    if any(ev.core == spec.core for ev in rep.evidence) and group is Group.PASS:
        group = Group.SILICON
    if group is Group.ENVIRONMENT and rep.unblamed_corrected and spec.stage in (Stage.SEARCH, Stage.LIVE1):
        recurred, after = _q5(after, spec)
        if recurred:
            group = Group.SILICON
    if group is Group.ENVIRONMENT and spec.load is Load.SOAK:
        group = Group.SILICON
    if group in (Group.PASS, Group.SILICON):
        return _verdict(after, spec, rep, group, pausing, w)
    if group is Group.THERMAL:
        return _thermal(after, spec, rep, pausing, w)
    return _apparatus(after, spec, rep, pausing)


def _q5(s: SessionState, spec: TestSpec) -> tuple[bool, SessionState]:
    flow = s.flow
    counts = dict(flow.q5)
    key = (spec.core, spec.value)
    counts[key] = counts.get(key, 0) + 1
    others = tuple(sorted({*flow.q5_others, spec.core}))
    out = replace(s, flow=replace(flow, q5=tuple(sorted(counts.items())), q5_others=others))
    return counts[key] >= 2 and others == (spec.core,), out


def _after_test(s: SessionState, core: int) -> SessionState:
    return s.with_core(replace(s.core(core), in_test=False))


def _verdict(s: SessionState, spec: TestSpec, rep: Report, group: Group, pausing: bool, w: World) -> Transition:
    if spec.stage is Stage.MEMORY:
        return _memory_verdict(s, spec, rep, group, pausing, w)
    if spec.stage in ALLCORE:
        return _allcore_verdict(s, spec, rep, group, pausing, w)
    if spec.stage is Stage.LIVE1:
        return _live1_verdict(s, spec, group, pausing, w)
    if spec.stage is Stage.BISECT:
        return _bisect_verdict(s, spec, rep, group, pausing, w)
    cfg = s.config
    rec = s.core(spec.core)
    if group is Group.PASS:
        step = search.on_pass(rec, cfg, spec.klass, rep.seconds)
    else:
        step = search.on_fail(rec, cfg, spec.klass, rep.seconds)
    if step.stock_unstable:
        return _enter_pause(s, PauseReason.STOCK_UNSTABLE, f"core {spec.core} failed at its stock value", (spec.core,))
    starts = s.streaks.start_failures
    if step.first_value:
        starts = 0 if group is Group.PASS else starts + 1
    after = replace(
        s.with_core(step.record),
        streaks=Streaks(start_failures=starts),
        flow=replace(s.flow, owed=spec.core if group is Group.SILICON else None),
    )
    if cfg.start_failure_threshold > 0 and starts >= cfg.start_failure_threshold:
        return _enter_pause(after, PauseReason.START_FAILURES, f"{starts} cores failed their first value in a row")
    return _next_wait(after, spec, pausing)


def _live1_outcome(s: SessionState, core: int, value: int, passed: bool, w: World) -> tuple[SessionState, str | None]:
    cfg = s.config
    flow = s.flow
    live = flow.live1
    if passed:
        if live.solo is not None:
            backed = tuple(search.back_off_in_place(r, cfg, 1) if r.core != core else r for r in s.cores)
            return replace(s, cores=backed, flow=replace(flow, live1=replace(live, solo=None), dirty=True)), None
        if live.index < len(_live1_sequence(s, w)):
            return replace(s, flow=replace(flow, live1=replace(live, index=live.index + 1))), None
        return replace(s, flow=replace(flow, live1=replace(live, requeue=live.requeue[1:]))), None
    if live.solo is not None:
        return s, f"core {core} fails at stock with every other core at 0"
    if value != 0:
        backed = search.back_off_in_place(replace(s.core(core), value=value), cfg, 1)
        return replace(s.with_core(backed), flow=replace(flow, dirty=True)), None
    if any(r.value != 0 for r in s.cores if r.core != core):
        return replace(s, flow=replace(flow, live1=replace(live, solo=core))), None
    return s, f"core {core} fails at stock with every core at 0"


def _live1_verdict(s: SessionState, spec: TestSpec, group: Group, pausing: bool, w: World) -> Transition:
    s = replace(s, streaks=Streaks())
    s, stock = _live1_outcome(s, spec.core, spec.value, group is Group.PASS, w)
    if stock is not None:
        return _enter_pause(s, PauseReason.STOCK_UNSTABLE, stock, (spec.core,))
    return _next_wait(s, spec, pausing)


def _bisect_exit(flow: Flow) -> Flow:
    return replace(flow, stage=flow.bisect.entry, bisect=None, dirty=True)


def _requeued(flow: Flow, core: int) -> Flow:
    if flow.stage is Stage.LIVE1 and core not in flow.live1.requeue:
        return replace(flow, live1=replace(flow.live1, requeue=(*flow.live1.requeue, core)))
    return flow


def _q6(s: SessionState, group: tuple[int, ...]) -> tuple[SessionState, str | None]:
    loosen = [r for r in s.cores if r.core in group and r.value != 0]
    if not loosen:
        return s, f"the cores {list(group)} fail together and none can loosen"
    cores = tuple(search.loosen(r, s.config) if r in loosen else r for r in s.cores)
    flow = s.flow
    cursor = flow.allcore
    if cursor is not None:
        cursor = replace(cursor, frozen=(*cursor.frozen, *(r.core for r in loosen)))
    return replace(s, cores=cores, flow=replace(flow, bisect=replace(flow.bisect, rerun=group), allcore=cursor)), None


def _held(s: SessionState) -> tuple[tuple[int, int], ...]:
    return tuple((r.core, r.value) for r in s.cores)


def _found(s: SessionState, core: int) -> SessionState:
    value = dict(s.flow.bisect.values)[core]
    return s.with_core(search.back_off_in_place(replace(s.core(core), value=value), s.config, 1))


def _leaf_rules(s: SessionState, leaves: tuple[tuple[int, ...], ...]) -> tuple[SessionState, str | None]:
    """A core found alone has backed off already, when its own slot failed; the first group of several among the
    leaves takes Q6, each later group takes its own once the replay before it passes, and the bisection exits only
    after the last."""
    group = next((leaf for leaf in leaves if len(leaf) > 1), None)
    if group is None:
        return replace(s, flow=_bisect_exit(s.flow)), None
    return _q6(s, group)


def _full_set(s: SessionState) -> tuple[SessionState, str | None]:
    universe = s.flow.bisect.universe
    if len(universe) == 1:
        return _leaf_rules(_found(s, universe[0]), ())
    return _q6(s, universe)


def _bisect_outcome(s: SessionState, passed: bool, w: World) -> tuple[SessionState, str | None]:
    flow = s.flow
    run = flow.bisect
    current = bisect.current_group(run)
    if run.rerun is not None:
        if passed:
            return _leaf_rules(s, bisect.after(run))
        return _q6(s, run.rerun)
    if passed and bisect.retesting(run):
        return replace(s, flow=replace(flow, stage=run.entry, bisect=None, one_off=(run.entry, _held(s)))), None
    if not passed and not current:
        return s, "the held values with every owned core at 0 fail the slot"
    if bisect.retesting(run):
        return _full_set(s)
    if not passed and len(current) == 1:
        s = _found(s, current[0])
    run = bisect.record(run, passed, dict(w.domain_of))
    s = replace(s, flow=replace(s.flow, bisect=run))
    if not bisect.accounted(run):
        return s, None
    leaves = bisect.resolve(run)
    if leaves != (run.universe,):
        return _leaf_rules(s, leaves)
    if flow.one_off == (run.entry, _held(s)):
        return _full_set(s)
    return replace(s, flow=replace(s.flow, bisect=bisect.retest(run))), None


def _at_full_set(flow: Flow) -> bool:
    return flow.stage is Stage.BISECT and bisect.current_group(flow.bisect) == flow.bisect.universe


def _set_failed(s: SessionState, records: tuple[Evidence, ...], substituted: tuple[Evidence, ...]) -> SessionState:
    """A charge the tuner substituted for the CO journal's fails no set that has not run; a caller passes none as
    substituted when the set's slot was running."""
    if not _at_full_set(s.flow):
        return s
    run = s.flow.bisect
    ran = {r.core: r.value for r in s.cores} if run.rerun is not None else dict(run.values)
    kept = Counter(records) - Counter(substituted)
    if any(ev.charged and ev.charged == ran.get(ev.core) for ev in kept):
        return _full_set(s)[0]
    return s


def _bisect_verdict(s: SessionState, spec: TestSpec, rep: Report, group: Group, pausing: bool, w: World) -> Transition:
    s = replace(s, streaks=Streaks())
    owned = {r.core for r in s.cores}
    held = tuple(core for core in rep.named if core not in owned)
    if held and _at_full_set(s.flow):
        detail = f"the full set's replay named core {held[0]}, whose value this session does not own"
        return _enter_pause(s, PauseReason.HELD_KNOB, detail, held)
    s, stock = _bisect_outcome(s, group is Group.PASS, w)
    if stock is not None:
        return _enter_pause(s, PauseReason.STOCK_UNSTABLE, stock)
    return _next_wait(s, spec, pausing)


def _next_wait(after: SessionState, spec: TestSpec, pausing: bool) -> Transition:
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    waiting = replace(after, slot=Waiting(token, 0.0), next_token=token + 1)
    return Transition(waiting, (_commit(waiting), *_back_to_baseline(after, spec), Schedule(token, 0.0)))


def _back_to_baseline(s: SessionState, spec: TestSpec) -> tuple[WriteCO, ...]:
    if spec.load in ALLCORE_LOADS:
        return tuple(WriteCO(core, base) for core, base in s.baseline)
    if spec.stage is Stage.SEARCH:
        return (WriteCO(spec.core, s.baseline_of(spec.core)),)
    if spec.stage is Stage.BISECT:
        ran = dict(spec.vector)
        return tuple(WriteCO(core, base) for core, base in s.baseline if ran[core] != base)
    return ()


def _retry_flow(s: SessionState, spec: TestSpec) -> Flow:
    return replace(s.flow, owed=spec.core) if spec.stage is Stage.SEARCH else s.flow


def _thermal(s: SessionState, spec: TestSpec, rep: Report, pausing: bool, w: World) -> Transition:
    cfg = s.config
    rec = s.core(spec.core)
    if spec.stage is Stage.SEARCH and rec.phase in (Phase.COARSE, Phase.FINE):
        s = s.with_core(replace(rec, walk_seconds=rec.walk_seconds + rep.seconds))
    streak = s.streaks.thermal + 1
    after = replace(s, streaks=replace(s.streaks, thermal=streak), flow=_retry_flow(s, spec))
    if streak > cfg.thermal_retries:
        last = f"; the last: {rep.detail}" if rep.detail else ""
        return _enter_pause(
            after, PauseReason.THERMAL, f"{streak} thermal stops in a row at the {spec.thermal_limit_c:g} C limit{last}"
        )
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    limit = spec.thermal_limit_c - cfg.cool_margin_c
    waiting = replace(
        after,
        slot=Waiting(token, cfg.cool_min_s, cool_below_c=limit, cool_deadline=w.now + cfg.cool_cap_s),
        next_token=token + 1,
    )
    return Transition(waiting, (_commit(waiting), *_back_to_baseline(after, spec), Schedule(token, cfg.cool_min_s)))


def _apparatus(s: SessionState, spec: TestSpec, rep: Report, pausing: bool) -> Transition:
    cfg = s.config
    streak = s.streaks.apparatus + 1
    after = replace(s, streaks=replace(s.streaks, apparatus=streak), flow=_retry_flow(s, spec))
    if streak > cfg.apparatus_retries:
        return _enter_pause(after, PauseReason.APPARATUS, f"no verdict {streak} times in a row: {rep.detail}")
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    waiting = replace(after, slot=Waiting(token, 0.0), next_token=token + 1)
    effects: list = []
    if rep.group is Group.SLEPT:
        effects.append(ForgetResident())
    effects += [_commit(waiting), *_back_to_baseline(after, spec), Schedule(token, 0.0)]
    return Transition(waiting, tuple(effects))


def _smu_refused(s: SessionState, e: SmuRefused) -> Transition:
    if s.lifecycle not in HELD:
        return Transition(s)
    if s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is not PauseReason.RESUMING:
        return Transition(s)
    finalizing = s.flow is not None and s.flow.stage is Stage.FINALIZE
    reason = PauseReason.PROFILE_APPLY if finalizing else PauseReason.SMU_WRITE
    return _enter_pause(s, reason, f"core {e.core} refused {e.value}: {e.cause}", (e.core,))


def _database_failed(s: SessionState, e: DatabaseFailed) -> Transition:
    if s.lifecycle not in HELD:
        return Transition(s, (Announce("database_failed", e.error),))
    if e.first:
        idle = SessionState(next_token=s.next_token)
        return Transition(idle, (Announce("database_failed", f"{e.error}; session {s.session_id} was not started"),))
    if _database_paused(s):
        return Transition(s)
    effects = _stop_effects(s, tuple((core, 0) for core, _ in s.baseline), journal=False)
    effects.insert(effects.index(next(x for x in effects if isinstance(x, RevertAll))), WriteStockMarker())
    failed = _clear_in_test(
        replace(s, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(PauseReason.DATABASE, e.error), streaks=Streaks())
    )
    effects.append(Announce("database_failed", f"{e.error}; every owned core is sent back to stock"))
    return Transition(failed, tuple(effects))


def _internal(s: SessionState, e: InternalError) -> Transition:
    if s.lifecycle not in HELD or _database_paused(s):
        return Transition(s, (Announce("internal_error", e.error),))
    return _enter_pause(s, PauseReason.INTERNAL, e.error)
