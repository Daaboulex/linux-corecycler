from __future__ import annotations

from dataclasses import replace

from corecycler.tuner.machine import allcore, bisect, search
from corecycler.tuner.machine.resume import attribute, resident
from corecycler.tuner.machine.types import (
    ALLCORE,
    ENDURANCE_CYCLE,
    KNOB_FLOOR,
    STAGE_LOADS,
    TRANSIENT_CYCLE,
    Abort,
    AllCore,
    Announce,
    AwaitWorker,
    CancelSchedule,
    ClearStockMarker,
    Close,
    CloseDb,
    Commit,
    ContinuationDue,
    ContinueOnSetup,
    CoreRecord,
    CrashEvidenceRead,
    DatabaseFailed,
    Event,
    Flow,
    ForgetResident,
    Group,
    InFlight,
    InternalError,
    Klass,
    Lifecycle,
    Live1,
    Load,
    MachineConfig,
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


def transition(s: SessionState, e: Event, w: World) -> Transition:
    match e:
        case Start():
            return _start(s, e, w)
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


def _start(s: SessionState, e: Start, w: World) -> Transition:
    if s.lifecycle in HELD:
        return Transition(s, refused="a session is held: pause or abort it first")
    cfg = e.config
    problem = _start_problem(cfg, e, w)
    if problem is not None:
        return Transition(s, refused=problem)
    token = s.next_token
    baseline = tuple((core, 0) for core in sorted(cfg.selected))
    new = SessionState(
        lifecycle=Lifecycle.RUNNING,
        session_id=e.session_id,
        config=cfg,
        seed=e.seed,
        baseline=baseline,
        cores=tuple(CoreRecord(core) for core in sorted(cfg.selected)),
        flow=Flow(Stage.SEARCH, Klass.T1),
        slot=Waiting(token, 0.0),
        next_token=token + 1,
        power=w.power,
        setup=w.setup,
        base_co=w.bios_co,
    )
    effects: list = [_commit(new)]
    effects += [WriteCO(core, value) for core, value in baseline if w.resident_of(core) != value]
    effects += [
        Announce("started", f"session {e.session_id}, {len(baseline)} cores, {cfg.method.value} order"),
        *(Announce("warning", warning) for warning in w.preflight_warnings),
        Schedule(token, 0.0),
    ]
    return Transition(new, tuple(effects))


def _start_problem(cfg, e: Start, w: World) -> str | None:
    if w.preflight_refusal is not None:
        return w.preflight_refusal
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
        return f"the BIOS CO of cores {unreadable} could not be read"
    if any(bios[core] != 0 for core in cfg.selected) and not e.acknowledged:
        return "the BIOS sets a CO on some cores; acknowledge the listed values to start"
    return None


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
        return Transition(resuming, (ReadCrashEvidence(e.session_id),))
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
    if cfg.cool_min_s > cfg.cool_cap_s:
        return f"cool_min_s {cfg.cool_min_s} is above cool_cap_s {cfg.cool_cap_s}"
    names = [name for name, _ in cfg.stage_seconds]
    if sorted(names) != sorted(STAGE_LOADS) or any(seconds <= 0 for _, seconds in cfg.stage_seconds):
        return f"stage_seconds must give {', '.join(sorted(STAGE_LOADS))} once each, each above 0"
    return None


def _stored_gates(st: Stored, w: World) -> str | None:
    if w.preflight_refusal is not None:
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
        return Transition(s, refused=f"session {e.session_id} is not stored")
    if st.status == "completed":
        return Transition(s, refused="the session is completed: use Validate Profile")
    if st.status not in RESUMABLE:
        return Transition(s, refused=f"a session stored as {st.status} cannot be resumed")
    refusal = _stored_gates(st, w)
    if refusal is not None:
        return Transition(s, refused=refusal)
    resumed = replace(reconstruct(st), next_token=s.next_token)
    return Transition(resumed, (ReadCrashEvidence(e.session_id),))


def _setup_choice_problem(s: SessionState, w: World) -> str | None:
    if s.lifecycle is not Lifecycle.PAUSED or s.pause is None or s.pause.reason is not PauseReason.SETUP_CHANGED:
        return "only a session paused as setup_changed can continue or restart under a new setup"
    if not _changes(s.setup, w.setup) and not _held_base_changes(s, w.bios_co):
        return f"nothing that affects a verdict changed since session {s.session_id} started; resume it"
    return None


def _superseded(s: SessionState) -> Stored:
    return replace(stored(s), status="superseded")


def _continue_on_setup(s: SessionState, e: ContinueOnSetup, w: World) -> Transition:
    problem = _setup_choice_problem(s, w)
    if problem is not None:
        return Transition(s, refused=problem)
    changed = "; ".join(filter(None, (_changes(s.setup, w.setup), _held_base_changes(s, w.bios_co))))
    token = s.next_token
    child = replace(
        s,
        lifecycle=Lifecycle.RUNNING,
        session_id=e.child_id,
        setup=w.setup,
        base_co=w.bios_co,
        pause=None,
        slot=Waiting(token, 0.0),
        streaks=Streaks(),
        next_token=token + 1,
    )
    effects: list = [Supersede(_superseded(s), stored(child), "continue"), ClearStockMarker()]
    effects += [WriteCO(core, value) for core, value in s.baseline]
    effects += [
        Announce("continued", f"session {e.child_id} continues session {s.session_id} under a new setup: {changed}"),
        Schedule(token, 0.0),
    ]
    return Transition(child, tuple(effects))


def _restart_on_setup(s: SessionState, e: RestartOnSetup, w: World) -> Transition:
    problem = _setup_choice_problem(s, w)
    if problem is not None:
        return Transition(s, refused=problem)
    started = _start(SessionState(next_token=s.next_token), Start(s.config, e.child_id, s.seed, e.acknowledged), w)
    if started.refused is not None:
        return Transition(s, refused=started.refused)
    rest = tuple(effect for effect in started.effects if not isinstance(effect, Commit))
    restarted = Announce(
        "restarted",
        f"session {e.child_id} restarts session {s.session_id} under a new setup: "
        + "; ".join(filter(None, (_changes(s.setup, w.setup), _held_base_changes(s, w.bios_co)))),
    )
    return Transition(started.state, (Supersede(_superseded(s), stored(started.state), "restart"), *rest, restarted))


def _validate(s: SessionState, e: ValidateProfile, w: World) -> Transition:
    if s.lifecycle in HELD:
        return Transition(s, refused="a session is held: pause or abort it first")
    st = w.stored
    if st is None or st.session_id != e.session_id:
        return Transition(s, refused=f"session {e.session_id} is not stored")
    if st.status != "completed":
        return Transition(
            s, refused=f"session {st.session_id} is {st.status}: only a completed session can be validated"
        )
    refusal = _stored_gates(st, w)
    if refusal is not None:
        return Transition(s, refused=refusal)
    if w.stored_crash_suspects:
        cores = ", ".join(map(str, w.stored_crash_suspects))
        return Transition(
            s,
            refused=f"the boot session {st.session_id} completed in crashed while cores {cores} held unproven "
            "values, and a completed session has no step that attributes it: start a new session",
        )
    changed = _changes(st.setup, w.setup)
    if changed:
        return Transition(
            s,
            refused=f"the setup changed since session {st.session_id} started: {changed}; "
            "start a new session on this setup",
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
    )
    effects: list = [_commit(running)]
    effects += [WriteCO(core, value) for core, value in st.baseline if w.resident_of(core) != value]
    effects += [
        Announce(
            "validating",
            f"session {st.session_id}: proving its completed values again"
            + ("" if w.stored_crash_suspects is None else ", after the boot it completed in crashed"),
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
    return replace(out, flow=replace(flow, dirty=True)), False


def _crash_evidence(s: SessionState, e: CrashEvidenceRead, w: World) -> Transition:
    if s.lifecycle is not Lifecycle.PAUSED or s.pause is None or s.pause.reason is not PauseReason.RESUMING:
        return Transition(s)
    result = _attributed(s, e, w)
    if e.kernel_problem is None:
        return result
    note = Announce(
        "kernel_log_unread",
        f"the crashed boot's kernel log could not be read, so the crash was attributed without it: {e.kernel_problem}",
    )
    return replace(result, effects=(*result.effects, note))


def _attributed(s: SessionState, e: CrashEvidenceRead, w: World) -> Transition:
    verdict = attribute(s, e)
    after = s
    for core in verdict.stale_fail:
        after, stock = _stale_fail(after, core, w)
        if stock is not None:
            return _enter_pause(_clear_in_test(after), PauseReason.STOCK_UNSTABLE, stock, (core,))
    for core in verdict.convict:
        after, at_stock = _convict(after, core)
        if at_stock:
            return _enter_pause(_clear_in_test(after), PauseReason.STOCK_UNSTABLE, f"core {core} crashed at 0", (core,))
    flow_crash = verdict.flow_crash and s.flow.stage is not Stage.SOAK
    if verdict.flow_crash:
        after, stock = _flow_crash(after, w, e.context)
        if stock is not None:
            return _enter_pause(_clear_in_test(after), PauseReason.STOCK_UNSTABLE, stock)
    after = _owe_interrupted(_clear_in_test(after), tuple(r.core for r in s.cores if r.in_test))
    streak = s.resume_streak + 1 if verdict.convict or flow_crash else s.resume_streak
    incidents = s.incidents + 1 if verdict.incident else s.incidents
    after = replace(after, resume_streak=streak, incidents=incidents)
    if verdict.held is not None:
        return _enter_pause(
            after,
            PauseReason.HELD_KNOB,
            f"core {verdict.held} crashed the machine at a value this session does not own",
            (verdict.held,),
        )
    for ev in verdict.evidence:
        applied = _apply_evidence(after, ev, at_stock=verdict.at_stock)
        if isinstance(applied, Transition):
            return applied
        after = applied
    if streak >= QUARANTINE_STREAK:
        quarantined = replace(after, lifecycle=Lifecycle.QUARANTINED, flow=None, slot=None, pause=None)
        return Transition(
            quarantined,
            (
                RevertAll(s.baseline, True),
                _commit(quarantined),
                Announce("quarantined", f"{streak} crashes in a row with no test surviving"),
            ),
        )
    if after.incidents > s.incidents and after.incidents >= INCIDENT_LIMIT:
        return _enter_pause(after, PauseReason.INCIDENTS, f"{after.incidents} crashes nobody could be blamed for")
    changed = _changes(after.setup, w.setup)
    if changed:
        return _enter_pause(
            after,
            PauseReason.SETUP_CHANGED,
            f"the setup changed since the session started: {changed}; put it back and resume, or start a new session",
        )
    moved = _power_moved(after, w)
    if moved is not None:
        return _enter_pause(after, PauseReason.SETUP_CHANGED, moved)
    knobs = _knob_problem(after, e, w)
    if knobs is not None:
        return _enter_pause(after, *knobs)
    token = after.next_token
    running = replace(
        after,
        lifecycle=Lifecycle.RUNNING,
        pause=None,
        slot=Waiting(token, 0.0),
        streaks=Streaks(),
        next_token=token + 1,
    )
    effects: list = [_commit(running), ClearStockMarker()]
    effects += [WriteCO(core, value) for core, value in s.baseline]
    effects += [Announce("resumed", f"session {s.session_id}"), Schedule(token, 0.0)]
    return Transition(running, tuple(effects))


def _held_base_changes(s: SessionState, reads: tuple[tuple[int, int | None], ...]) -> str:
    base, owned = dict(s.base_co), {core for core, _ in s.baseline}
    return "; ".join(
        f"core {core} is {value}, the BIOS gave {base.get(core)} when the session started"
        for core, value in reads
        if core not in owned and value != base.get(core)
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
            moved = tuple(core for core in cores if core not in baseline and reads[core] != base.get(core))
            return (
                PauseReason.SETUP_CHANGED,
                f"the BIOS CO of a core the session does not own changed: {changed}",
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
    found = "; ".join(f"core {c} reads {reads[c]}, the session left it at {expected[c]}" for c in moved)
    return PauseReason.OUTSIDE_WRITE, f"something else wrote the CO while the session was away: {found}", moved


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


def _flow_crash(s: SessionState, w: World, context=None) -> tuple[SessionState, str | None]:
    flow = s.flow
    if flow.stage is Stage.MEMORY:
        return _memory_flow_rule(s, w)
    if flow.stage in ALLCORE:
        return _allcore_fail(s, context, w)
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
        group = bisect.current_group(run)
        if not group:
            return s, "the held values with every owned core at 0 crashed the machine"
        return replace(s, flow=replace(flow, bisect=bisect.record(run, False, dict(w.domain_of)))), None
    raise ValueError(f"a crash with a core in test has no flow rule in {flow.stage}")


def _apply_evidence(s: SessionState, ev, at_stock: bool = False) -> SessionState | Transition:
    owned = {r.core for r in s.cores}
    if ev.core not in owned:
        return _enter_pause(
            s,
            PauseReason.HELD_KNOB,
            f"core {ev.core} logged a machine check at a value this session does not own",
            (ev.core,),
        )
    value = 0 if at_stock else resident(s, ev.core)
    stage = s.flow.stage
    if value == 0:
        if stage is Stage.SEARCH:
            return _enter_pause(
                s, PauseReason.STOCK_UNSTABLE, f"core {ev.core} logged a machine check at its stock value", (ev.core,)
            )
        incidents = s.incidents + 1
        out = replace(s, incidents=incidents)
        if incidents >= INCIDENT_LIMIT:
            return _enter_pause(out, PauseReason.INCIDENTS, f"{incidents} machine checks at stock values")
        return out
    steps = 1 if ev.corrected else UNCORRECTED_STEPS
    rec = s.core(ev.core)
    backed = search.back_off_in_place(replace(rec, value=value), s.config, steps)
    out = s.with_core(backed)
    flow = replace(out.flow, dirty=True)
    if stage is Stage.LIVE1 and flow.live1 is not None and ev.core not in flow.live1.requeue:
        flow = replace(flow, live1=replace(flow.live1, requeue=(*flow.live1.requeue, ev.core)))
    if stage is Stage.BISECT:
        flow = _bisect_exit(flow)
    if stage in ALLCORE and flow.allcore is not None:
        flow = replace(flow, allcore=replace(flow.allcore, t_clean=0.0))
    return replace(out, flow=flow)


def _pause_request(s: SessionState) -> Transition:
    if s.lifecycle is Lifecycle.RUNNING and isinstance(s.slot, InFlight):
        pausing = replace(s, lifecycle=Lifecycle.PAUSING, pause=Pause(PauseReason.USER, "paused by the user"))
        return Transition(pausing, (_commit(pausing), Announce("pausing", "finishing the current test")))
    if s.lifecycle is Lifecycle.RUNNING:
        return _enter_pause(s, PauseReason.USER, "paused by the user")
    if s.lifecycle in (Lifecycle.PAUSING, Lifecycle.PAUSED):
        return Transition(s, refused="already paused")
    return Transition(s, refused="nothing is running")


def _abort(s: SessionState) -> Transition:
    if s.lifecycle not in HELD:
        return Transition(s, refused="nothing to abort")
    s = _clear_in_test(_keep_pick(s))
    profile = s.flow.validating if s.flow is not None else None
    if profile is not None and tuple((r.core, r.value) for r in s.cores) == profile:
        effects = _stop_effects(s, profile, journal=True)
        done = replace(s, lifecycle=Lifecycle.COMPLETED, flow=None, slot=None, pause=None, streaks=Streaks())
        effects += [_commit(done), Announce("completed", "validation aborted: the completed values stand")]
        return Transition(done, tuple(effects))
    effects = _stop_effects(s, s.baseline, journal=True)
    effects += [
        Commit(replace(stored(s), status="aborted")),
        Announce("aborted", "every owned core is at its baseline"),
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
        return _enter_pause(s, PauseReason.THERMAL, "the CPU did not cool below the limit within the cap")
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
            flow=Flow(Stage.SEARCH, Klass.T2, last_core=flow.last_core),
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
        Stage.LIVE1, last_core=s.flow.last_core, live1=Live1(), allcore=s.flow.allcore, validating=s.flow.validating
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
    if rec.phase is not Phase.PROVEN and not rec.inside(rec.value, cfg.max_offset):
        return _enter_pause(
            replace(s, flow=flow),
            PauseReason.INTERNAL,
            f"core {core} would be tested at {rec.value}, at or past its fail bound",
        )
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
    elif live.requeue:
        core = live.requeue[0]
        vector = tuple((r.core, r.value) for r in s.cores)
    elif live.back is not None:
        return _next(replace(s, flow=replace(flow, stage=live.back, live1=None)), w, None)
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
    if cursor is None:
        cursor = AllCore(floors=tuple((r.core, r.value) for r in s.cores))
    else:
        cursor = replace(cursor, t_idx=0, t_clean=0.0, pass_no=cursor.pass_no + 1)
    if s.flow.validating is not None:
        return _next(replace(s, flow=_past_tighten(replace(s.flow, live1=None, allcore=cursor), s.config)), w, None)
    flow = replace(s.flow, stage=Stage.STEADY, live1=None, allcore=cursor)
    return _allcore_next(replace(s, flow=flow), w)


def _load_seconds(cfg, load: Load) -> float:
    if load is Load.HOT:
        return cfg.hot_seconds
    if load is Load.SOAK:
        return cfg.soak_seconds
    return dict(cfg.stage_seconds)[load.value]


def _all_cpus(s: SessionState, w: World) -> tuple[int, ...]:
    cpus = dict(w.cpus_of)
    return tuple(sorted(cpu for r in s.cores for cpu in cpus[r.core]))


def _past_tighten(flow: Flow, cfg) -> Flow:
    cursor = replace(flow.allcore, tightened=True, item=None, t_idx=0, t_clean=0.0)
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
    flow = _past_tighten(s.flow, s.config)
    return _next(replace(s, flow=flow), w, None)


def _allcore_next(s: SessionState, w: World) -> Transition:
    cfg = s.config
    flow = s.flow
    cursor = flow.allcore
    values = {r.core: r.value for r in s.cores}
    if flow.stage is Stage.TIGHTEN:
        if cursor.item is None:
            waiting = [core for core in allcore.tighten_items(cursor) if core not in cursor.frozen]
            if not waiting:
                return _after_tighten(s, w)
            core = waiting[0]
            rec = s.core(core)
            floor = dict(cursor.floors).get(core, rec.value)
            target = rec.value - cfg.fine_step
            if rec.value <= floor or not rec.inside(target, cfg.max_offset):
                frozen = replace(cursor, frozen=(*cursor.frozen, core))
                return _allcore_next(replace(s, flow=replace(flow, allcore=frozen)), w)
            cursor = replace(cursor, item=core, t_idx=0, t_clean=0.0)
            flow = replace(flow, allcore=cursor)
        item = s.core(cursor.item)
        if not item.inside(item.value - cfg.fine_step, cfg.max_offset):
            frozen = replace(cursor, frozen=(*cursor.frozen, item.core), item=None, t_idx=0, t_clean=0.0)
            return _allcore_next(replace(s, flow=replace(flow, allcore=frozen)), w)
        values[cursor.item] -= cfg.fine_step
        load = TRANSIENT_CYCLE[cursor.t_idx % len(TRANSIENT_CYCLE)]
    elif flow.stage is Stage.STEADY:
        load = Load.HOT
    elif flow.stage is Stage.TRANSIENT:
        load = TRANSIENT_CYCLE[cursor.t_idx % len(TRANSIENT_CYCLE)]
    elif flow.stage is Stage.ENDURANCE:
        load = ENDURANCE_CYCLE[cursor.t_idx % len(ENDURANCE_CYCLE)]
    else:
        load = Load.SOAK
    first = s.cores[0].core
    vector = tuple((core, values[core]) for core, _ in s.baseline)
    cpus = () if load is Load.SOAK else _all_cpus(s, w)
    spec = TestSpec(Klass.T1, first, values[first], cpus, _load_seconds(cfg, load), vector, flow.stage, load)
    return _start_test(s, flow, spec, w)


def _allcore_fail(s: SessionState, ctx, w: World) -> tuple[SessionState, str | None]:
    flow = s.flow
    cursor = flow.allcore
    stage = flow.stage
    if stage is Stage.SOAK:
        return replace(s, incidents=s.incidents + 1, flow=replace(flow, dirty=True)), None
    if stage is Stage.TIGHTEN:
        frozen = replace(cursor, frozen=(*cursor.frozen, cursor.item), item=None, t_idx=0, t_clean=0.0)
        return replace(s, flow=replace(flow, allcore=frozen)), None
    if stage in (Stage.STEADY, Stage.ENDURANCE) and allcore.steady_class(ctx):
        values = {r.core: r.value for r in s.cores}
        if not any(values.values()):
            return s, "the all-core load fails with every owned core at 0"
        run = bisect.begin(stage, s.cores[0].core, Klass.T1, values, dict(w.domain_of))
        cursor = replace(cursor, t_clean=0.0)
        return replace(s, flow=replace(flow, stage=Stage.BISECT, bisect=run, allcore=cursor)), None
    fix = allcore.transient_fix(cursor, allcore.context_or_unknown(ctx), s.cores, len(s.cores), w.owner_layout)
    if fix.core is None:
        return s, "a transient failure found nothing left to loosen"
    loosened = s.with_core(search.loosen(s.core(fix.core), s.config))
    cursor = replace(fix.cursor, t_clean=0.0)
    return replace(loosened, flow=replace(flow, allcore=cursor, dirty=True)), None


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
    s = replace(s, streaks=Streaks(), resume_streak=0)
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
    if flow.stage is Stage.TIGHTEN and flow.allcore.item in named:
        return None
    movable = [core for core in named if s.core(core).value < 0]
    if not movable:
        return None
    for core in movable:
        s = s.with_core(search.back_off_in_place(s.core(core), s.config, 1))
    return replace(s, flow=replace(flow, allcore=replace(flow.allcore, t_clean=0.0), dirty=True))


def _allcore_verdict(s: SessionState, spec: TestSpec, rep: Report, group: Group, pausing: bool, w: World) -> Transition:
    cfg = s.config
    flow = s.flow
    cursor = flow.allcore
    s = replace(s, streaks=Streaks(), resume_streak=0)
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
        if cursor.t_clean < window:
            flow = replace(flow, allcore=cursor)
        elif flow.stage is Stage.ENDURANCE:
            flow = _past_endurance(replace(flow, allcore=cursor), cfg)
        elif flow.stage is Stage.TIGHTEN:
            rec = s.core(cursor.item)
            s = s.with_core(replace(rec, value=rec.value - cfg.fine_step, count=0))
            delta = dict(cursor.co_delta)
            delta[cursor.item] -= 1
            cursor = replace(cursor, co_delta=tuple(sorted(delta.items())), item=None, t_idx=0, t_clean=0.0)
            flow = replace(flow, allcore=cursor, dirty=True)
        elif not cursor.tightened:
            flow = replace(flow, stage=Stage.TIGHTEN, allcore=replace(cursor, item=None, t_idx=0, t_clean=0.0))
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
    if run.entry in (Stage.STEADY, Stage.ENDURANCE):
        cpus, seconds, load = _all_cpus(s, w), s.config.bisect_seconds, Load.HOT
    elif run.entry is Stage.MEMORY:
        cpus, seconds, load = _all_cpus(s, w), s.config.memory_seconds, Load.MEMORY
    else:
        cpus, seconds, load = _cpus_for(lane, run.klass, w), s.config.test_seconds_1t, Load.ONE_T
    spec = TestSpec(run.klass, lane, dict(vector)[lane], cpus, seconds, vector, Stage.BISECT, load)
    return _start_test(s, flow, spec, w)


def _changes(recorded: tuple[tuple[str, str], ...], now: tuple[tuple[str, str], ...]) -> str:
    was, found = dict(recorded), dict(now)
    return "; ".join(
        f"{name} was {was.get(name, 'absent')}, is now {found.get(name, 'absent')}"
        for name in sorted(was.keys() | found.keys())
        if was.get(name) != found.get(name)
    )


def _power_moved(s: SessionState, w: World) -> str | None:
    if s.power == w.power:
        return None
    return f"the power state changed since the session started: {_changes(s.power, w.power)}; restore it and resume"


def _start_test(s: SessionState, flow: Flow, spec: TestSpec, w: World) -> Transition:
    moved = _power_moved(s, w)
    if moved is not None:
        return _enter_pause(replace(s, flow=flow), PauseReason.SETUP_CHANGED, moved)
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
    for core, value in spec.vector:
        if w.gap_grew or w.resident_of(core) != value:
            effects.append(WriteCO(core, value))
    effects += [_commit(started), StartWorker(token, spec)]
    return Transition(started, tuple(effects))


def _finalize(s: SessionState, w: World) -> Transition:
    flow = s.flow
    if flow.dirty:
        return _enter_live1(replace(s, flow=replace(flow, dirty=False)), w)
    if not flow.applied:
        token = s.next_token
        applied = replace(s, flow=replace(flow, applied=True), slot=Waiting(token, 0.0), next_token=token + 1)
        effects: list = [WriteCO(r.core, r.value) for r in s.cores if w.resident_of(r.core) != r.value]
        effects += [Schedule(token, 0.0)]
        return Transition(applied, tuple(effects))
    done = replace(s, lifecycle=Lifecycle.COMPLETED, flow=None, slot=None, incidents=0)
    values = ", ".join(f"{r.core}: {r.value}" for r in s.cores)
    return Transition(done, (_commit(done), Announce("completed", f"final values {values}")))


def _reported(s: SessionState, e: WorkerReported, w: World) -> Transition:
    if s.lifecycle not in (Lifecycle.RUNNING, Lifecycle.PAUSING) or not isinstance(s.slot, InFlight):
        return Transition(s)
    if s.slot.token != e.token:
        return Transition(s)
    spec = s.slot.spec
    rep = e.report
    pausing = s.lifecycle is Lifecycle.PAUSING
    if spec.load in ALLCORE_LOADS:
        after = s
        for ev in rep.evidence:
            applied = _apply_evidence(after, ev)
            if isinstance(applied, Transition):
                return applied
            after = applied
        after = _clear_in_test(after)
        if rep.evidence:
            return _next_wait(replace(after, streaks=Streaks(), resume_streak=0), spec, pausing)
    else:
        after = _after_test(s, spec.core)
    for ev in () if spec.load in ALLCORE_LOADS else rep.evidence:
        if ev.core == spec.core:
            continue
        applied = _apply_evidence(after, ev)
        if isinstance(applied, Transition):
            return applied
        after = applied
    if s.flow.stage is Stage.BISECT and after.flow.stage is not Stage.BISECT:
        return _next_wait(after, spec, pausing)
    group = Group.APPARATUS if rep.skipped and spec.stage is not Stage.MEMORY else rep.group
    if any(ev.core == spec.core for ev in rep.evidence) and group is Group.PASS:
        group = Group.SILICON
    if group is Group.ENVIRONMENT and rep.unblamed_corrected and spec.stage in (Stage.SEARCH, Stage.LIVE1):
        recurred, after = _q5(after, spec)
        if recurred:
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
        return _bisect_verdict(s, spec, group, pausing, w)
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
        resume_streak=0,
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
    s = replace(s, streaks=Streaks(), resume_streak=0)
    s, stock = _live1_outcome(s, spec.core, spec.value, group is Group.PASS, w)
    if stock is not None:
        return _enter_pause(s, PauseReason.STOCK_UNSTABLE, stock, (spec.core,))
    return _next_wait(s, spec, pausing)


def _bisect_exit(flow: Flow) -> Flow:
    return replace(flow, stage=flow.bisect.entry, bisect=None, dirty=True)


def _q6(s: SessionState, group: tuple[int, ...]) -> tuple[SessionState, str | None]:
    loosen = [r for r in s.cores if r.core in group and r.value != 0]
    if not loosen:
        return s, f"the cores {list(group)} fail together and none can loosen"
    cores = tuple(search.back_off_in_place(r, s.config, 1) if r in loosen else r for r in s.cores)
    flow = s.flow
    return replace(s, cores=cores, flow=replace(flow, bisect=replace(flow.bisect, rerun=group))), None


def _bisect_outcome(s: SessionState, passed: bool, w: World) -> tuple[SessionState, str | None]:
    flow = s.flow
    run = flow.bisect
    current = bisect.current_group(run)
    if run.rerun is not None:
        if passed:
            return replace(s, flow=_bisect_exit(flow)), None
        return _q6(s, run.rerun)
    if not passed and not current:
        return s, "the held values with every owned core at 0 fail the slot"
    if not passed and len(current) == 1:
        core = current[0]
        backed = search.back_off_in_place(replace(s.core(core), value=dict(run.values)[core]), s.config, 1)
        return replace(s.with_core(backed), flow=_bisect_exit(flow)), None
    run = bisect.record(run, passed, dict(w.domain_of))
    s = replace(s, flow=replace(flow, bisect=run))
    if run.queue:
        return s, None
    return _q6(s, bisect.resolve(run))


def _bisect_verdict(s: SessionState, spec: TestSpec, group: Group, pausing: bool, w: World) -> Transition:
    s = replace(s, streaks=Streaks(), resume_streak=0)
    s, stock = _bisect_outcome(s, group is Group.PASS, w)
    if stock is not None:
        return _enter_pause(s, PauseReason.STOCK_UNSTABLE, stock)
    return _next_wait(s, spec, pausing)


def _next_wait(after: SessionState, spec: TestSpec, pausing: bool) -> Transition:
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    waiting = replace(after, slot=Waiting(token, 0.0), next_token=token + 1)
    effects: list = [_commit(waiting)]
    if spec.stage is Stage.SEARCH:
        effects.append(WriteCO(spec.core, after.baseline_of(spec.core)))
    if spec.load in ALLCORE_LOADS:
        effects += [WriteCO(core, base) for core, base in after.baseline]
    effects.append(Schedule(token, 0.0))
    return Transition(waiting, tuple(effects))


def _retry_flow(s: SessionState, spec: TestSpec) -> Flow:
    return replace(s.flow, owed=spec.core) if spec.stage is Stage.SEARCH else s.flow


def _thermal(s: SessionState, spec: TestSpec, rep: Report, pausing: bool, w: World) -> Transition:
    cfg = s.config
    rec = s.core(spec.core)
    if spec.stage is Stage.SEARCH and rec.phase in (Phase.COARSE, Phase.FINE):
        s = s.with_core(replace(rec, walk_seconds=rec.walk_seconds + rep.seconds))
    streak = s.streaks.thermal + 1
    after = replace(s, streaks=replace(s.streaks, thermal=streak), resume_streak=0, flow=_retry_flow(s, spec))
    if streak > cfg.thermal_retries:
        return _enter_pause(after, PauseReason.THERMAL, f"{streak} thermal stops in a row")
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    limit = spec.thermal_limit_c - cfg.cool_margin_c
    waiting = replace(
        after,
        slot=Waiting(token, cfg.cool_min_s, cool_below_c=limit, cool_deadline=w.now + cfg.cool_cap_s),
        next_token=token + 1,
    )
    effects: list = [_commit(waiting)]
    if spec.stage is Stage.SEARCH:
        effects.append(WriteCO(spec.core, after.baseline_of(spec.core)))
    effects.append(Schedule(token, cfg.cool_min_s))
    return Transition(waiting, tuple(effects))


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
    effects.append(_commit(waiting))
    if spec.stage is Stage.SEARCH:
        effects.append(WriteCO(spec.core, after.baseline_of(spec.core)))
    effects.append(Schedule(token, 0.0))
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
    if _database_paused(s):
        return Transition(s)
    effects = _stop_effects(s, tuple((core, 0) for core, _ in s.baseline), journal=False)
    effects.insert(effects.index(next(x for x in effects if isinstance(x, RevertAll))), WriteStockMarker())
    failed = _clear_in_test(
        replace(s, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(PauseReason.DATABASE, e.error), streaks=Streaks())
    )
    effects.append(Announce("database_failed", f"every owned core is at stock; {e.error}"))
    return Transition(failed, tuple(effects))


def _internal(s: SessionState, e: InternalError) -> Transition:
    if s.lifecycle not in HELD or _database_paused(s):
        return Transition(s, (Announce("internal_error", e.error),))
    return _enter_pause(s, PauseReason.INTERNAL, e.error)
