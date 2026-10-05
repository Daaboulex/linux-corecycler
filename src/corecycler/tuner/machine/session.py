from __future__ import annotations

from dataclasses import replace

from corecycler.tuner.machine import search
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
    Mode,
    Pause,
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
    Streaks,
    TestSpec,
    Transition,
    Waiting,
    WorkerReported,
    World,
    WriteCO,
    WriteStockMarker,
)
from corecycler.tuner.orders import next_core, order_problem

QUARANTINE_STREAK = 3
INCIDENT_LIMIT = 2
COOL_POLL_S = 1.0
HELD = (Lifecycle.RUNNING, Lifecycle.PAUSING, Lifecycle.PAUSED)
RESUMABLE = frozenset({"running", "paused", "aborted"})


def transition(s: SessionState, e: Event, w: World) -> Transition:
    match e:
        case Start():
            return _start(s, e, w)
        case Resume():
            return _resume(s, e, w)
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
    )


def _commit(s: SessionState) -> Commit:
    return Commit(stored(s))


def _baselines(s: SessionState) -> tuple[tuple[int, int], ...]:
    return s.baseline


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
    if isinstance(s.slot, Waiting) and s.slot.picked is not None and s.flow is not None and s.flow.owed is None:
        return replace(s, flow=replace(s.flow, owed=s.slot.picked))
    return s


def _evidence_pause(s: SessionState, evidence, exclude: int | None) -> Transition | None:
    owned = {r.core for r in s.cores}
    for ev in evidence:
        if ev.core == exclude:
            continue
        if ev.core in owned:
            return _enter_pause(
                s, PauseReason.STOCK_UNSTABLE, f"core {ev.core} logged a machine check at its stock value", (ev.core,)
            )
        return _enter_pause(
            s,
            PauseReason.HELD_KNOB,
            f"core {ev.core} logged a machine check at the value this session does not own",
            (ev.core,),
        )
    return None


def _enter_pause(s: SessionState, reason: PauseReason, detail: str, cores: tuple[int, ...] = ()) -> Transition:
    s = _keep_pick(s)
    effects = _stop_effects(s, _baselines(s), journal=True)
    paused = _clear_in_test(
        replace(s, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(reason, detail, cores), streaks=Streaks())
    )
    flow = paused.flow if paused.flow is not None else Flow(Stage.SEARCH)
    paused = replace(paused, flow=replace(flow, applied=False))
    effects += [_commit(paused), Announce("paused", f"{reason.value}: {detail}")]
    return Transition(paused, tuple(effects))


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
    )
    effects: list = [_commit(new)]
    effects += [WriteCO(core, value) for core, value in baseline if w.resident_of(core) != value]
    effects += [
        Announce("started", f"session {e.session_id}, {len(baseline)} cores, {cfg.method.value} order"),
        Schedule(token, 0.0),
    ]
    return Transition(new, tuple(effects))


def _start_problem(cfg, e: Start, w: World) -> str | None:
    if w.preflight_refusal is not None:
        return w.preflight_refusal
    if cfg.mode is not Mode.CO:
        return f"the {cfg.mode.value} mode is not built yet; only per-core CO runs"
    domain_of = dict(w.domain_of)
    problem = order_problem(cfg.method, cfg.selected, domain_of)
    if problem is not None:
        return problem
    cpus = dict(w.cpus_of)
    missing = sorted(core for core in cfg.selected if not cpus.get(core))
    if missing:
        return f"cores {missing} are not in the core map"
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


def _database_paused(s: SessionState) -> bool:
    return s.lifecycle is Lifecycle.PAUSED and s.pause is not None and s.pause.reason is PauseReason.DATABASE


def _resume_stored(s: SessionState, e: Resume, w: World) -> Transition:
    st = w.stored
    if st is None or st.session_id != e.session_id:
        return Transition(s, refused=f"session {e.session_id} is not stored")
    if st.status == "completed":
        return Transition(s, refused="the session is completed: use Validate Profile")
    if st.status not in RESUMABLE:
        return Transition(s, refused=f"a session stored as {st.status} cannot be resumed")
    if w.preflight_refusal is not None:
        return Transition(s, refused=w.preflight_refusal)
    problem = order_problem(st.config.method, st.config.selected, dict(w.domain_of))
    if problem is not None:
        return Transition(s, refused=f"{problem}; abort session {st.session_id} and start a new one")
    resumed = replace(reconstruct(st), next_token=s.next_token)
    return Transition(resumed, (ReadCrashEvidence(e.session_id),))


def _crash_evidence(s: SessionState, e: CrashEvidenceRead, w: World) -> Transition:
    if s.lifecycle is not Lifecycle.PAUSED or s.pause is None or s.pause.reason is not PauseReason.RESUMING:
        return Transition(s)
    cfg = s.config
    in_test = [r for r in s.cores if r.in_test]
    cleared = _clear_in_test(s)
    convicted = False
    incident = False
    if e.same_boot and e.stale_db:
        for rec in in_test:
            if rec.phase is not Phase.PROVEN or rec.value != 0:
                step = search.on_fail(cleared.core(rec.core), cfg, s.flow.klass, 0.0)
                cleared = cleared.with_core(step.record)
    if e.dirty_end and not e.same_boot:
        if len(in_test) == 1:
            step = search.on_crash(cleared.core(in_test[0].core), cfg)
            if step.stock_unstable:
                return _enter_pause(
                    cleared, PauseReason.STOCK_UNSTABLE, f"core {in_test[0].core} crashed at 0", (in_test[0].core,)
                )
            cleared = cleared.with_core(step.record)
            convicted = True
        else:
            incident = bool(in_test) or e.was_running
    streak = s.resume_streak + 1 if convicted else s.resume_streak
    incidents = s.incidents + 1 if incident else s.incidents
    cleared = replace(cleared, resume_streak=streak, incidents=incidents)
    named = _evidence_pause(cleared, e.evidence, exclude=None)
    if named is not None:
        return named
    if streak >= QUARANTINE_STREAK:
        quarantined = replace(cleared, lifecycle=Lifecycle.QUARANTINED, flow=None, slot=None, pause=None)
        return Transition(
            quarantined,
            (
                RevertAll(_baselines(s), True),
                _commit(quarantined),
                Announce("quarantined", f"{streak} crashes in a row with no test surviving"),
            ),
        )
    if incident and incidents >= INCIDENT_LIMIT:
        return _enter_pause(cleared, PauseReason.INCIDENTS, f"{incidents} crashes nobody could be blamed for")
    token = cleared.next_token
    running = replace(
        cleared,
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
    effects = _stop_effects(s, _baselines(s), journal=True)
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
    effects = _stop_effects(s, _baselines(s), journal=not database)
    if not database:
        paused = _clear_in_test(
            replace(
                s, lifecycle=Lifecycle.PAUSED, slot=None, pause=Pause(PauseReason.USER, "closed"), streaks=Streaks()
            )
        )
        effects.append(_commit(paused))
    effects.append(CloseDb())
    return Transition(SessionState(next_token=s.next_token), tuple(effects))


def _cpus_for(core: int, w: World) -> tuple[int, ...]:
    cpus = dict(w.cpus_of)[core]
    if core == 0 and len(cpus) > 1:
        return (cpus[1],)
    return (cpus[0],)


def _continuation(s: SessionState, e: ContinuationDue, w: World) -> Transition:
    if s.lifecycle is not Lifecycle.RUNNING or not isinstance(s.slot, Waiting) or s.slot.token != e.token:
        return Transition(s)
    if s.flow.stage is Stage.FINALIZE:
        return _finalize(s, w)
    slot = s.slot
    if slot.cool_below_c is not None:
        return _cooling(s, slot, w)
    if search.class_done(s.cores):
        return _end_class(s, w)
    return _pick_and_start(s, w, slot.picked)


def _end_class(s: SessionState, w: World) -> Transition:
    finalize = replace(s, flow=Flow(Stage.FINALIZE))
    return _finalize(finalize, w)


def _cooling(s: SessionState, slot: Waiting, w: World) -> Transition:
    if w.temperature_c is not None and slot.cool_below_c is not None and w.temperature_c < slot.cool_below_c:
        return _pick_and_start(replace(s, slot=Waiting(slot.token, 0.0)), w, None)
    if slot.cool_deadline is not None and w.now >= slot.cool_deadline:
        return _enter_pause(s, PauseReason.THERMAL, "the CPU did not cool below the limit within the cap")
    token = s.next_token
    waiting = replace(slot, token=token, seconds=COOL_POLL_S)
    return Transition(replace(s, slot=waiting, next_token=token + 1), (Schedule(token, COOL_POLL_S),))


def _pick_and_start(s: SessionState, w: World, picked: int | None) -> Transition:
    cfg = s.config
    flow = s.flow
    cores = tuple(r.core for r in s.cores)
    if picked is None:
        core, cursor = next_core(cfg.method, cores, dict(w.domain_of), s.seed, flow.klass.value, flow.cursor, flow.owed)
        flow = replace(flow, cursor=cursor)
        if flow.owed is None and flow.last_core is not None and core != flow.last_core and cfg.wait_between_cores_s > 0:
            token = s.next_token
            waiting = replace(
                s, flow=flow, slot=Waiting(token, cfg.wait_between_cores_s, picked=core), next_token=token + 1
            )
            return Transition(waiting, (Schedule(token, cfg.wait_between_cores_s),))
    else:
        core = picked
    if w.frequency_capped:
        return _enter_pause(
            replace(s, flow=flow),
            PauseReason.APPARATUS,
            "a frequency cap or disabled boost would make every verdict meaningless",
        )
    rec = search.resolve_start(s.core(core), cfg)
    if rec.phase is not Phase.PROVEN and not rec.inside(rec.value, cfg.max_offset):
        return _enter_pause(
            replace(s, flow=flow),
            PauseReason.INTERNAL,
            f"core {core} would be tested at {rec.value}, at or past its fail bound",
        )
    token = s.next_token
    spec = TestSpec(flow.klass, core, rec.value, _cpus_for(core, w), cfg.test_seconds_1t)
    flow = replace(flow, owed=None, last_core=core)
    started = replace(
        s.with_core(replace(rec, in_test=True)), flow=flow, slot=InFlight(token, spec), next_token=token + 1
    )
    effects: list = []
    if w.gap_grew:
        effects.append(ForgetResident())
    for other, value in s.baseline:
        if other != core and (w.gap_grew or w.resident_of(other) != value):
            effects.append(WriteCO(other, value))
    if w.gap_grew or w.resident_of(core) != rec.value:
        effects.append(WriteCO(core, rec.value))
    effects += [_commit(started), StartWorker(token, spec)]
    return Transition(started, tuple(effects))


def _finalize(s: SessionState, w: World) -> Transition:
    flow = s.flow
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
    named = _evidence_pause(_after_test(s, spec.core), rep.evidence, exclude=spec.core)
    if named is not None:
        return named
    group = rep.group
    if any(ev.core == spec.core for ev in rep.evidence) and group is Group.PASS:
        group = Group.SILICON
    if group in (Group.PASS, Group.SILICON):
        return _verdict(s, spec, rep, group, pausing)
    if group is Group.THERMAL:
        return _thermal(s, spec, rep, pausing, w)
    return _apparatus(s, spec, rep, pausing)


def _after_test(s: SessionState, core: int) -> SessionState:
    return s.with_core(replace(s.core(core), in_test=False))


def _verdict(s: SessionState, spec: TestSpec, rep: Report, group: Group, pausing: bool) -> Transition:
    cfg = s.config
    rec = s.core(spec.core)
    if group is Group.PASS:
        step = search.on_pass(rec, cfg, spec.klass, rep.seconds)
    else:
        step = search.on_fail(rec, cfg, spec.klass, rep.seconds)
    if step.stock_unstable:
        return _enter_pause(
            _after_test(s, spec.core),
            PauseReason.STOCK_UNSTABLE,
            f"core {spec.core} failed at its stock value",
            (spec.core,),
        )
    starts = s.streaks.start_failures
    if step.first_value:
        starts = 0 if group is Group.PASS else starts + 1
    after = replace(
        s.with_core(replace(step.record, in_test=False)),
        streaks=Streaks(start_failures=starts),
        resume_streak=0,
        flow=replace(s.flow, owed=spec.core if group is Group.SILICON else None),
    )
    if cfg.start_failure_threshold > 0 and starts >= cfg.start_failure_threshold:
        return _enter_pause(after, PauseReason.START_FAILURES, f"{starts} cores failed their first value in a row")
    return _next_wait(after, spec, pausing)


def _next_wait(after: SessionState, spec: TestSpec, pausing: bool) -> Transition:
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    waiting = replace(after, slot=Waiting(token, 0.0), next_token=token + 1)
    base = after.baseline_of(spec.core)
    return Transition(waiting, (_commit(waiting), WriteCO(spec.core, base), Schedule(token, 0.0)))


def _thermal(s: SessionState, spec: TestSpec, rep: Report, pausing: bool, w: World) -> Transition:
    cfg = s.config
    rec = s.core(spec.core)
    if rec.phase in (Phase.COARSE, Phase.FINE):
        rec = replace(rec, walk_seconds=rec.walk_seconds + rep.seconds)
    streak = s.streaks.thermal + 1
    after = replace(
        s.with_core(replace(rec, in_test=False)),
        streaks=replace(s.streaks, thermal=streak),
        resume_streak=0,
        flow=replace(s.flow, owed=spec.core),
    )
    if streak > cfg.thermal_retries:
        return _enter_pause(after, PauseReason.THERMAL, f"{streak} thermal stops in a row")
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    limit = cfg.thermal_limit_c - cfg.cool_margin_c
    waiting = replace(
        after,
        slot=Waiting(token, cfg.cool_min_s, cool_below_c=limit, cool_deadline=w.now + cfg.cool_cap_s),
        next_token=token + 1,
    )
    return Transition(
        waiting, (_commit(waiting), WriteCO(spec.core, after.baseline_of(spec.core)), Schedule(token, cfg.cool_min_s))
    )


def _apparatus(s: SessionState, spec: TestSpec, rep: Report, pausing: bool) -> Transition:
    cfg = s.config
    streak = s.streaks.apparatus + 1
    after = replace(
        _after_test(s, spec.core), streaks=replace(s.streaks, apparatus=streak), flow=replace(s.flow, owed=spec.core)
    )
    if streak > cfg.apparatus_retries:
        return _enter_pause(after, PauseReason.APPARATUS, f"no verdict {streak} times in a row: {rep.detail}")
    if pausing:
        return _enter_pause(after, PauseReason.USER, "paused by the user")
    token = after.next_token
    waiting = replace(after, slot=Waiting(token, 0.0), next_token=token + 1)
    effects: list = []
    if rep.group is Group.SLEPT:
        effects.append(ForgetResident())
    effects += [_commit(waiting), WriteCO(spec.core, after.baseline_of(spec.core)), Schedule(token, 0.0)]
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
    if s.pause is not None and s.pause.reason is PauseReason.DATABASE:
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
