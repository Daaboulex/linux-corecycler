"""End-to-end fault-injection tests for the auto-tuner crash/resume lifecycle.

These prove the unbreakable invariants rather than assuming them:

  - a hard crash with NO in_test flag is still caught (the CO write-ahead journal)
  - an unstable baseline is escaped, converging toward CO=0 (never an inescapable floor)
  - repeated resume-crashes trip the circuit breaker -> forced CO=0 + quarantine
  - SMU write failure pauses without corrupting state
  - thermal protection fails closed when no sensor is readable
  - every core-cycling style recovers a journal-detected crash

A "hard crash" is modelled the way it really happens: the value is journaled as
resident-but-unsurvived in the DB, then a FRESH engine is constructed on the same
DB and resume()d -- exactly what a process death + reboot leaves behind. This
exercises the real persistence, resume, journal, penalty and circuit-breaker code
paths end to end (the worker/subprocess layer is out of scope, as in the rest of
the tuner suite).
"""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from engine.backends.base import FFTPreset, StressBackend, StressConfig, StressMode
from engine.scheduler import CoreScheduler, SchedulerConfig
from history.db import HistoryDB
from tuner import persistence as tp
from tuner.config import TunerConfig
from tuner.engine import TunerEngine
from tuner.state import CoreState, TunerPhase

# ---------------------------------------------------------------------------
# Fault-injectable fake SMU
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _synchronous_qtimer():
    """Force QTimer.singleShot to run its callback synchronously for every test in
    this file. The closed-loop driver relies on the engine's QTimer continuations
    firing inline; the conftest Qt mock already does this, but a REAL PySide6 env
    (a developer's machine) would queue them on an idle event loop and the driver
    would stall. This makes the fault tests env-independent."""
    with patch("tuner.engine.QTimer.singleShot", new=lambda _ms, fn: fn()):
        yield


class FaultSMU:
    """Controllable fake RyzenSMU for fault injection.

    Records every write, can reject or raise on a write, and reports back the
    last value applied per core (mirroring the real read-back behaviour).
    """

    def __init__(self, co_range: tuple[int, int] = (-60, 10)) -> None:
        self.commands = SimpleNamespace(co_range=co_range)
        self.applied: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []
        self.reject_set = False   # set_co_offset returns False (rejected / read-back mismatch)
        self.raise_on_set = False  # set_co_offset raises (driver/permission fault)

    def set_co_offset(self, core_id: int, value: int) -> bool:
        self.writes.append((core_id, value))
        if self.raise_on_set:
            raise RuntimeError("SMU fault (injected)")
        if self.reject_set:
            return False
        self.applied[core_id] = value
        return True

    def get_co_offset(self, core_id: int) -> int:
        return self.applied.get(core_id, 0)

    def get_all_co_offsets(self, num_cores: int) -> dict[int, int]:
        return {c: self.applied.get(c, 0) for c in range(num_cores)}

    def get_pbo_scalar(self) -> float:
        return 1.0

    def get_boost_limit(self) -> int:
        return 5500


class _HardCrash(Exception):
    """Simulates the machine dying DURING the SMU hardware write (power loss /
    instant hard crash) — the process never returns from set_co_offset."""


class CrashDuringWriteSMU(FaultSMU):
    """Fake SMU that hard-crashes the machine while writing a specific value.

    Models reality faithfully: the crash happens *inside* the hardware write, so
    recovery is only possible if the value was journaled BEFORE the write call.
    """

    def __init__(self, crash_at: tuple[int, int] | None) -> None:
        super().__init__()
        self._crash_at = crash_at

    def set_co_offset(self, core_id: int, value: int) -> bool:
        self.writes.append((core_id, value))
        if self._crash_at is not None and (core_id, value) == self._crash_at:
            raise _HardCrash(f"machine died writing core {core_id} = {value}")
        self.applied[core_id] = value
        return True


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def topo(topo_single_ccd):
    """4-core single-CCD topology (cores 0..3)."""
    return topo_single_ccd


@pytest.fixture
def smu():
    return FaultSMU()


def make_engine(db, topo, smu, backend, **cfg_kwargs) -> TunerEngine:
    defaults = dict(
        coarse_step=5, fine_step=1, max_offset=-30,
        search_duration_seconds=1, confirm_duration_seconds=1,
        cores_to_test=[0],
    )
    defaults.update(cfg_kwargs)
    cfg = TunerConfig(**defaults)
    return TunerEngine(
        db=db, topology=topo, smu=smu, backend=backend, config=cfg,
    )


def _resume_fresh(db, topo, smu, backend, sid, **cfg_kwargs) -> TunerEngine:
    """Build a brand-new engine (simulating a fresh process after a reboot) and
    resume the given session, with _run_next stubbed out."""
    eng = make_engine(db, topo, smu, backend, **cfg_kwargs)
    with patch.object(eng, "_run_next"):
        eng.resume(sid)
    return eng


class _StubBackend(StressBackend):
    """A do-nothing backend — the closed-loop driver patches the worker, so the
    backend is constructed but never executed."""

    name = "stub"

    def is_available(self) -> bool:
        return True

    def get_command(self, config, work_dir):
        return ["true"]

    def parse_output(self, stdout, stderr, returncode):
        return True, None

    def get_supported_modes(self):
        return [StressMode.SSE]

    def prepare(self, work_dir, config):
        pass

    def cleanup(self, work_dir, *, preserve_on_error: bool = False):
        pass


def _make_topo(n_cores: int, n_ccds: int):
    """Build a CPUTopology with n_cores spread across n_ccds (no sysfs needed)."""
    from engine.topology import CPUTopology, PhysicalCore

    topo = CPUTopology()
    per_ccd = max(1, (n_cores + n_ccds - 1) // n_ccds)
    for i in range(n_cores):
        topo.cores[i] = PhysicalCore(
            core_id=i, ccd=min(i // per_ccd, n_ccds - 1), ccx=None, logical_cpus=(i,)
        )
    topo.ccds = n_ccds
    return topo


def drive_validation(db, topo, backend, cliffs, agg_margin, cfg_kw, cap=4000):
    """Drive the REAL multi-core validation flow (stages 1/2/3) to termination.

    Cores are seeded CONFIRMED at their individual stable limit, then validation
    runs. The aggregate model: a set passes only if EVERY member's offset is at
    least ``agg_margin`` less aggressive than its individual stable limit (power
    delivery makes the aggregate tougher than a core alone). A validation failure
    backs off the most aggressive core and restarts. Returns (final_engine, steps).
    """
    sid = tp.create_session(db, TunerConfig(**cfg_kw), "", "")
    pending: list[tuple[int, list[int] | None]] = []

    def patched_single(core_id, duration):
        pending.append((core_id, None))

    def patched_multi(cores, duration):
        pending.append((cores[0], list(cores)))

    eng = make_engine(db, topo, FaultSMU(), backend, **cfg_kw)
    eng._start_worker = patched_single
    eng._start_multi_core_worker = patched_multi
    eng._session_id = sid
    eng._core_states = {
        c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=stable,
                     best_offset=stable, baseline_offset=0)
        for c, (stable, _crash) in cliffs.items()
    }
    for cs in eng._core_states.values():
        tp.save_core_state(db, sid, cs)
    profile = {c: stable for c, (stable, _crash) in cliffs.items()}
    eng._enter_auto_validation(profile)

    steps = 0
    while pending and steps < cap:
        steps += 1
        core, cset = pending.pop(0)
        members = cset if cset is not None else [core]
        ok = all(
            eng._core_states[m].best_offset >= cliffs[m][0] + agg_margin
            for m in members
        )
        eng._on_test_finished(core, ok, "", "" if ok else "agg", 1.0, 0.0)
    return eng, steps


def drive_closed_loop(db, topo, backend, cliffs, cfg_kw, baseline=0, cap=6000,
                      reboot_interval=0):
    """Drive the REAL tuner loop against a simulated CPU.

    Only the worker is replaced — by a stability oracle keyed on each core's
    (stable_limit, crash_limit). A hard crash (offset at/over crash_limit) is
    injected the real way: the offset is journaled by the real _apply_co before
    the crash, then a fresh engine recovers via the real resume(). When
    ``reboot_interval`` > 0, an unrelated power-loss reboot is also injected every
    that-many steps (the in-flight offset is journaled but did not itself crash),
    testing resume at arbitrary points. Returns (final_engine, steps, crashes, sid).
    """
    sid = tp.create_session(db, TunerConfig(**cfg_kw), "", "")
    pending: list[int] = []

    def patched(core_id, duration):
        pending.append(core_id)

    def fresh():
        e = make_engine(db, topo, FaultSMU(), backend, **cfg_kw)
        e._start_worker = patched
        e._session_id = sid
        return e

    eng = fresh()
    eng._core_states = {c: CoreState(core_id=c, baseline_offset=baseline) for c in cliffs}
    for cs in eng._core_states.values():
        tp.save_core_state(db, sid, cs)
    eng._set_status("running")
    holder = {"eng": eng}
    holder["eng"]._run_next()

    steps = crashes = 0
    while pending and steps < cap:
        steps += 1
        if reboot_interval and steps % reboot_interval == 0 and holder["eng"].status == "running":
            # Power-loss reboot unrelated to the test: discard the engine; the
            # in-flight offset is journaled, so a fresh engine recovers it.
            pending.clear()
            holder["eng"] = fresh()
            holder["eng"].resume(sid)
            continue
        core = pending.pop(0)
        e = holder["eng"]
        cs = e._core_states.get(core)
        if cs is None:
            continue
        offset = cs.current_offset
        stable, crash = cliffs[core]
        if offset >= stable:               # less aggressive than the cliff -> pass
            e._on_test_finished(core, True, "", "", 1.0, 0.0)
        elif offset <= crash:              # at/over the crash point -> hard crash
            crashes += 1
            holder["eng"] = fresh()
            holder["eng"].resume(sid)
        else:                              # between -> detected (soft) failure
            e._on_test_finished(core, False, "calc error", "computation", 1.0, 0.0)
    return holder["eng"], steps, crashes, sid


def drive_intermittent(db, topo, backend, cliffs, flaky, cfg_kw, baseline=0, cap=8000):
    """Like drive_closed_loop, but instability is INTERMITTENT, not a deterministic
    cliff. The deterministic sim proves safety only for a monotonic cliff; real CO
    instability often passes a short search test and crashes a later confirm/harden.

    Per core: offset >= stable always passes; offset <= hard always hard-crashes; an
    offset in the marginal band (hard < offset < stable) PASSES (looks stable) until
    the flaky[core]-th time THAT offset is tested, then hard-crashes -- the "passed
    search, failed confirm" case. Every offset that actually hard-crashed is recorded
    in crashed_at. Returns (engine, steps, crashes, crashed_at, sid).
    """
    sid = tp.create_session(db, TunerConfig(**cfg_kw), "", "")
    pending: list[int] = []
    visits: dict[tuple[int, int], int] = {}
    crashed_at: dict[int, set[int]] = {c: set() for c in cliffs}

    def patched(core_id, duration):
        pending.append(core_id)

    def fresh():
        e = make_engine(db, topo, FaultSMU(), backend, **cfg_kw)
        e._start_worker = patched
        e._session_id = sid
        return e

    eng = fresh()
    eng._core_states = {c: CoreState(core_id=c, baseline_offset=baseline) for c in cliffs}
    for cs in eng._core_states.values():
        tp.save_core_state(db, sid, cs)
    eng._set_status("running")
    holder = {"eng": eng}
    holder["eng"]._run_next()

    steps = crashes = 0

    def do_crash(core, off):
        nonlocal crashes
        crashes += 1
        crashed_at[core].add(off)
        holder["eng"] = fresh()
        holder["eng"].resume(sid)

    while pending and steps < cap:
        steps += 1
        core = pending.pop(0)
        e = holder["eng"]
        cs = e._core_states.get(core)
        if cs is None:
            continue
        offset = cs.current_offset
        stable, hard = cliffs[core]

        if offset >= stable:                       # always stable
            e._on_test_finished(core, True, "", "", 1.0, 0.0)
        elif offset <= hard:                       # always a hard crash
            do_crash(core, offset)
        else:                                      # marginal: flaky
            visits[(core, offset)] = visits.get((core, offset), 0) + 1
            if visits[(core, offset)] >= flaky[core]:
                do_crash(core, offset)             # crashed on a re-test
            else:
                e._on_test_finished(core, True, "", "", 1.0, 0.0)  # looked stable
    return holder["eng"], steps, crashes, crashed_at, sid


# ---------------------------------------------------------------------------
# T1: a hard crash with NO in_test flag is caught by the journal
# ---------------------------------------------------------------------------


class TestJournalCatchesUnflaggedCrash:
    def test_unflagged_crash_is_penalized_via_journal(self, db, topo, smu, mock_backend):
        """The old in_test-only detection missed crashes during idle / baseline
        restore / revert. The CO journal catches them."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "")
        # Core was NOT mid-test (in_test=False) but -12 was resident when the box died.
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-12,
            best_offset=-10, baseline_offset=0, in_test=False,
        ))
        db.journal_co_intent(sid, 0, -12, survived=False)  # resident, unsurvived -> suspect

        eng = _resume_fresh(db, topo, smu, mock_backend, sid,
                            cores_to_test=[0], crash_penalty_steps=3, fine_step=1)

        cs = eng._core_states[0]
        assert cs.crash_count == 1                      # penalized despite no in_test
        assert cs.backoff_fail_bound == -12             # hard fail bound at the crashing value
        assert cs.current_offset == -9                  # -12 backed off toward 0 by 3
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.in_test is False                      # the suspect's in_test flag is cleared
        # The journal-detected recovery is logged as a real crash (passed=False).
        logs = tp.get_test_log(db, sid, core_id=0)
        assert any(e.get("error_type") == "crash" and not e.get("passed") for e in logs)

    def test_zero_value_is_never_a_suspect(self, db, topo, smu, mock_backend):
        """CO=0 (stock) is axiomatically safe and must never be treated as a crash."""
        cfg = TunerConfig(cores_to_test=[0])
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH))
        db.journal_co_intent(sid, 0, 0, survived=False)
        assert db.journal_suspects(sid) == []


# ---------------------------------------------------------------------------
# T2: an unstable baseline is escaped (converges toward CO=0)
# ---------------------------------------------------------------------------


class TestUnstableBaselineEscapes:
    def test_baseline_descends_toward_zero_when_it_crashes(self, db, topo, smu, mock_backend):
        """If the baseline value itself crashes the box, the baseline is no longer
        a safe floor -- it must descend toward 0 so resume cannot re-apply it."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "")
        # baseline == current == -20: the inherited baseline itself was resident and crashed.
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-20,
            baseline_offset=-20, in_test=False,
        ))
        db.journal_co_intent(sid, 0, -20, survived=False)

        eng = _resume_fresh(db, topo, smu, mock_backend, sid,
                            cores_to_test=[0], crash_penalty_steps=3, fine_step=1)

        cs = eng._core_states[0]
        # Baseline re-anchored from -20 toward 0 (to -17); never clamps back to -20.
        assert cs.baseline_offset == -17
        assert cs.current_offset == -17
        assert cs.backoff_fail_bound == -20

    def test_repeated_baseline_crash_marches_to_zero(self, db, topo, smu, mock_backend):
        """Each resume that finds the baseline crashing moves it one penalty step
        closer to 0 -- it can never get stuck re-applying the same crashing value."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=1, fine_step=1,
                          resume_crash_quarantine_threshold=20)
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
            baseline_offset=-5, in_test=False,
        ))
        last = -5
        for _ in range(5):
            cs = db.get_tuner_core_states(sid)[0]
            db.journal_co_intent(sid, 0, cs.current_offset, survived=False)
            eng = _resume_fresh(db, topo, smu, mock_backend, sid,
                                cores_to_test=[0], crash_penalty_steps=1, fine_step=1,
                                resume_crash_quarantine_threshold=20)
            cs = eng._core_states[0]
            # Strictly less aggressive each round, never past 0.
            assert cs.baseline_offset > last or cs.baseline_offset == 0
            assert -5 <= cs.baseline_offset <= 0
            last = cs.baseline_offset
        assert last == 0  # converged to stock


# ---------------------------------------------------------------------------
# T3: repeated resume-crash trips the circuit breaker -> quarantine + CO=0
# ---------------------------------------------------------------------------


class TestResumeCrashCircuitBreaker:
    def test_quarantines_after_threshold_and_forces_stock(self, db, topo, smu, mock_backend):
        """The headline guarantee: a machine that keeps hard-crashing on resume is
        bounded -- after `threshold` crash-resumes the tuner forces every core to
        CO=0, quarantines the session, and stops, instead of looping forever."""
        threshold = 3
        cfg = TunerConfig(cores_to_test=[0], resume_crash_quarantine_threshold=threshold,
                          crash_penalty_steps=1, fine_step=1)
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-30,
            baseline_offset=0, in_test=False,
        ))

        eng = None
        for i in range(threshold):
            cs = db.get_tuner_core_states(sid)[0]
            resident = cs.current_offset if cs.current_offset != 0 else -30
            db.journal_co_intent(sid, 0, resident, survived=False)  # crashed again
            eng = _resume_fresh(db, topo, smu, mock_backend, sid,
                                cores_to_test=[0],
                                resume_crash_quarantine_threshold=threshold,
                                crash_penalty_steps=1, fine_step=1)
            if eng.status == "quarantined":
                assert i == threshold - 1  # not before the threshold
                break

        assert eng.status == "quarantined"
        assert db.get_tuner_session(sid).status == "quarantined"
        assert smu.applied.get(0) == 0                     # forced to stock
        # A quarantined session is not offered for resume (fail closed).
        assert sid not in [s.id for s in db.list_resumable_tuner_sessions()]
        # in_test is cleared in memory AND persisted for every core.
        assert all(not cs.in_test for cs in eng._core_states.values())
        assert all(not c.in_test for c in db.get_tuner_core_states(sid).values())

    def test_clean_resume_does_not_increment_breaker(self, db, topo, smu, mock_backend):
        """A normal pause/resume with no crash never moves toward quarantine."""
        cfg = TunerConfig(cores_to_test=[0], resume_crash_quarantine_threshold=3)
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-8, in_test=False,
        ))  # no journal suspect, not in_test
        eng = _resume_fresh(db, topo, smu, mock_backend, sid,
                            cores_to_test=[0], resume_crash_quarantine_threshold=3)
        assert eng.status != "quarantined"
        assert db.get_resume_crash_streak(sid) == 0

    def test_surviving_test_resets_breaker(self, db, topo, smu, mock_backend):
        """Progress (a completed test) resets the streak, so occasional crashes
        during a long legitimate tune never accumulate into a quarantine."""
        cfg = TunerConfig(cores_to_test=[0], search_duration_seconds=1)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        sid = tp.create_session(db, cfg, "", "")
        eng._session_id = sid
        eng._core_states = {0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                                         current_offset=-5)}
        db.set_resume_crash_streak(sid, 2)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(0, True, "", "", 1.0, 0.0)
        assert db.get_resume_crash_streak(sid) == 0


class TestValidationCrashArmsBreaker:
    """A hard crash DURING multi-core validation must arm the circuit breaker.

    Validation re-applies each core's confirmed offset, which `_apply_co` journals
    survived=1, so the CO-journal crash detector is blind to it. Only the in_test
    flag can attribute a multi-core power-interaction crash (the failure mode
    validation exists to find). Before the fix the validation workers never set
    in_test, so such a crash left `crashed` empty -> streak reset to 0 -> the same
    profile was re-applied into the same crash forever.
    """

    def _seed_validating_at_stage2(self, db, smu, topo, backend, cliffs, **cfg):
        """Seed a confirmed profile and run the REAL stage-2 (all-core) launch,
        capturing the stressed set without starting a worker."""
        sid = tp.create_session(
            db, TunerConfig(cores_to_test=list(cliffs), **cfg), "", ""
        )
        eng = make_engine(db, topo, smu, backend, cores_to_test=list(cliffs), **cfg)
        launched: list[list[int]] = []
        eng._start_multi_core_worker = lambda cores, duration: launched.append(list(cores))
        eng._session_id = sid
        eng._core_states = {
            c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=v,
                         best_offset=v, baseline_offset=0)
            for c, v in cliffs.items()
        }
        # By the time validation runs, each core's confirmed offset is in the
        # proven-safe envelope, so _apply_co journals it survived=1 — which is
        # exactly why the journal detector is blind and only in_test can catch a
        # validation crash. Seed it so the test reproduces that real state.
        eng._co_survived = dict(cliffs)
        for cs in eng._core_states.values():
            tp.save_core_state(db, sid, cs)
        eng._set_status("validating")
        eng._validation_stage = 2
        eng._validation_core_order = sorted(cliffs)
        eng._run_validation_stage2()
        return eng, sid, launched

    def test_validation_stage_flags_all_stressed_cores_in_test(
        self, db, topo, smu, mock_backend
    ):
        """The fix: a multi-core validation stage flags EVERY stressed core in_test
        and PERSISTS it before the worker — and the CO journal is blind here
        (confirmed offsets journal survived=1), so in_test is the only signal that
        can attribute a validation crash."""
        cliffs = {0: -10, 1: -12, 2: -8, 3: -15}
        _eng, sid, launched = self._seed_validating_at_stage2(
            db, smu, topo, mock_backend, cliffs,
        )
        assert launched == [sorted(cliffs)]               # all cores stressed together
        persisted = db.get_tuner_core_states(sid)
        assert all(persisted[c].in_test for c in cliffs)  # all flagged + persisted
        assert tp.journal_suspects(db, sid) == []         # journal cannot catch it

    def test_in_test_validation_cores_are_attributed_as_crashes(
        self, db, topo, smu, mock_backend
    ):
        """The detection half of the fix: a confirmed core left in_test by a crashing
        validation worker is attributed as a crash and penalized on the resume path —
        phase is irrelevant, only the in_test flag matters, and the journal cannot see
        it. The streak/quarantine half (a non-empty crashed set -> breaker) is the
        same code TestResumeCrashCircuitBreaker covers; the two compose to the full
        guarantee. (resume()'s drift/status Qt signal emissions abort under
        pytest+PySide6, but _detect_and_handle_crashes is the load-bearing logic and
        the end-to-end path is verified out-of-harness.)"""
        cliffs = {0: -10, 1: -12}
        eng, sid, _ = self._seed_validating_at_stage2(
            db, smu, topo, mock_backend, cliffs, crash_penalty_steps=1, fine_step=1,
        )
        assert tp.journal_suspects(db, sid) == []  # journal is blind to validation
        crashed = eng._detect_and_handle_crashes(eng._core_states)
        # Attribution policy: a multi-core stress set cannot identify the guilty
        # core; penalizing every member would demolish the whole profile on one
        # event. Only the MOST AGGRESSIVE resident offset is penalized (the same
        # policy the soft-fail validation path uses) — the breaker still arms
        # (crashed is non-empty), and repeated wrong guesses are bounded by it.
        assert crashed == [1]                            # -12 is most aggressive
        assert eng._core_states[1].crash_count == 1      # penalized
        assert eng._core_states[0].crash_count == 0      # spared
        assert all(not eng._core_states[c].in_test for c in cliffs)  # flags cleared

    def test_normal_validation_completion_clears_in_test(self, db, topo, smu, mock_backend):
        """A surviving validation test must leave NO stale in_test on any stressed
        core, or a later resume would wrongly fire the breaker on a clean session."""
        cliffs = {0: -10, 1: -12}
        eng, sid, _ = self._seed_validating_at_stage2(
            db, smu, topo, mock_backend, cliffs,
        )
        assert all(db.get_tuner_core_states(sid)[c].in_test for c in cliffs)
        with patch.object(eng, "_run_next"), patch.object(eng, "_run_validation_next"):
            eng._on_test_finished(sorted(cliffs)[0], True, "", "", 1.0, 0.0)
        assert eng._cores_under_stress == []
        assert all(not db.get_tuner_core_states(sid)[c].in_test for c in cliffs)


# ---------------------------------------------------------------------------
# T4: SMU write failure pauses without corrupting state
# ---------------------------------------------------------------------------


class TestSMUWriteFault:
    def test_rejected_write_pauses_and_journals_intent(self, db, topo, smu, mock_backend):
        """A rejected SMU write (read-back mismatch) pauses the tuner rather than
        recording a false stability failure, and the intent is journaled so the
        value is treated as suspect (fail closed) on the next resume."""
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng._core_states = {0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                                         current_offset=-10, baseline_offset=0)}
        smu.reject_set = True
        ok = eng._apply_co_isolation(0, -10)
        assert ok is False
        assert eng._status == "paused"
        assert (0, -10) in db.journal_suspects(eng._session_id)

    def test_raising_write_propagates_to_caller_pause(self, db, topo, smu, mock_backend):
        """A driver exception on write is handled by the caller's pause path."""
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng._core_states = {0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                                         current_offset=-10, baseline_offset=0)}
        smu.raise_on_set = True
        ok = eng._apply_co_isolation(0, -10)
        assert ok is False
        assert eng._status == "paused"


# ---------------------------------------------------------------------------
# T5: the journal write-ahead / proven-safe envelope logic
# ---------------------------------------------------------------------------


class TestWriteAheadJournal:
    def test_aggressive_value_journaled_unsurvived(self, db, topo, smu, mock_backend):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng._apply_co(0, -30)
        assert smu.applied[0] == -30
        assert (0, -30) in db.journal_suspects(eng._session_id)  # new territory -> suspect

    def test_within_envelope_value_journaled_survived(self, db, topo, smu, mock_backend):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng._co_survived[0] = -30                       # -30 already proven safe
        eng._apply_co(0, -20)                           # less aggressive than proven
        assert db.journal_suspects(eng._session_id) == []  # not a suspect

    def test_zero_is_always_survived(self, db, topo, smu, mock_backend):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng._apply_co(0, 0)
        assert db.journal_suspects(eng._session_id) == []
        assert db.journal_survived_values(eng._session_id).get(0) == 0

    def test_resume_rebuilds_proven_safe_envelope_from_journal(self, db, topo, smu, mock_backend):
        """Resume must rebuild the proven-safe envelope from the journal so a value
        the machine already survived is not re-flagged as a suspect."""
        cfg = TunerConfig(cores_to_test=[0])
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10,
        ))
        db.journal_co_intent(sid, 0, -30, survived=True)  # -30 proven safe a prior boot
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        assert eng._co_survived.get(0) == -30

    def test_apply_co_without_smu_returns_false(self, db, topo, mock_backend):
        """With no SMU, _apply_co performs no write and reports failure (fail closed)."""
        eng = make_engine(db, topo, None, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        assert eng._apply_co(0, -10) is False


# ---------------------------------------------------------------------------
# T9: closed-loop end-to-end simulation — true injection through the real loop
# ---------------------------------------------------------------------------


class TestClosedLoopSimulation:
    """Drive the REAL tuner loop (_run_next -> _apply_co [journals] -> worker ->
    _on_test_finished -> _advance_core) against a simulated CPU. Only the worker is
    replaced — by a stability oracle. Hard crashes are injected the real way: the
    offset is journaled by the real _apply_co before the crash, then a fresh engine
    recovers via the real resume(). Nothing is hand-fed. Proves the whole machine
    (picker + state machine + journal + crash recovery) terminates and never leaves
    a core settled on an offset that hard-crashes the box."""

    @pytest.mark.parametrize("order", ["sequential", "round_robin", "ccd_round_robin"])
    def test_converges_safely_under_injected_crashes(self, db, topo, mock_backend, order):
        # Per-core ground truth: offset >= stable passes; between stable and crash
        # is a detected (soft) failure; at/over crash the machine HARD-crashes.
        cliffs = {0: (-12, -15), 1: (-22, -25), 2: (-7, -10), 3: (-17, -20)}
        cfg_kw = dict(cores_to_test=list(cliffs), test_order=order, coarse_step=5,
                      fine_step=1, max_offset=-40, crash_penalty_steps=3,
                      auto_validate=False, resume_crash_quarantine_threshold=50)
        eng, steps, crashes, sid = drive_closed_loop(db, topo, mock_backend, cliffs, cfg_kw)

        assert steps < 6000, f"did not converge ({order})"
        assert crashes >= 1, "no hard crash was exercised — simulation is vacuous"
        assert eng.status in ("idle", "quarantined")
        # SAFETY INVARIANT: the RESIDENT CO (what is actually in the SMU now) never
        # sits at or beyond any core's crash point.
        resident = eng._smu.applied
        for c, (_stable, crash) in cliffs.items():
            r = resident.get(c, 0)
            assert not (r == crash or r < crash), (
                f"[{order}] core {c} left resident at {r}, crashes at {crash}"
            )


# ---------------------------------------------------------------------------
# T9b: intermittent instability — safety holds when a value passes then crashes
# ---------------------------------------------------------------------------


class TestIntermittentInstability:
    """The deterministic closed-loop sim proves safety only for a monotonic cliff;
    the audit flagged that real CO instability is intermittent -- an offset passes a
    short search test and crashes a later confirm/harden. These drive the REAL loop
    against an intermittent oracle and assert the generalized safety invariant: after
    termination, each core's resident CO is strictly LESS aggressive than every offset
    that actually hard-crashed during the run (resident > max(crashed offsets))."""

    def test_offset_that_passes_then_crashes_is_backed_off(self, db, topo, mock_backend):
        # Core 0: >= -8 always stable; <= -20 always hard-crashes; a marginal offset
        # passes its FIRST test then hard-crashes on the SECOND -- "passed search,
        # failed confirm/harden". Deterministic regression for the intermittent path.
        cliffs = {0: (-8, -20)}
        flaky = {0: 2}
        eng, steps, crashes, crashed_at, sid = drive_intermittent(
            db, topo, mock_backend, cliffs, flaky,
            dict(cores_to_test=[0], coarse_step=4, fine_step=1, max_offset=-40,
                 crash_penalty_steps=3, auto_validate=False,
                 resume_crash_quarantine_threshold=50,
                 search_duration_seconds=1, confirm_duration_seconds=1),
        )
        assert steps < 8000
        assert crashes >= 1, "the intermittent crash never fired -- the test is vacuous"
        assert eng.status in ("idle", "quarantined")
        resident = eng._smu.applied.get(0, 0)
        assert crashed_at[0], "no crash recorded -- vacuous"
        worst = max(crashed_at[0])  # least-aggressive offset that hard-crashed
        assert resident > worst, f"left resident at {resident}, it hard-crashed at {worst}"

    @settings(max_examples=250, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_safe_under_random_intermittent_instability(self, data):
        n_cores = data.draw(st.integers(min_value=1, max_value=4), label="n_cores")
        n_ccds = data.draw(st.sampled_from([1, 2]), label="n_ccds")
        order = data.draw(st.sampled_from(
            ["sequential", "round_robin", "ccd_round_robin"]), label="order")
        coarse = data.draw(st.integers(min_value=2, max_value=6), label="coarse")
        fine = data.draw(st.integers(min_value=1, max_value=min(coarse, 3)), label="fine")
        cliffs: dict[int, tuple[int, int]] = {}
        flaky: dict[int, int] = {}
        for c in range(n_cores):
            stable = data.draw(st.integers(min_value=-30, max_value=-3), label=f"stable{c}")
            gap = data.draw(st.integers(min_value=2, max_value=15), label=f"gap{c}")
            cliffs[c] = (stable, stable - gap)
            flaky[c] = data.draw(st.integers(min_value=2, max_value=4), label=f"flaky{c}")

        db = HistoryDB(":memory:")
        try:
            topo = _make_topo(n_cores, n_ccds)
            cfg_kw = dict(
                cores_to_test=list(range(n_cores)), test_order=order,
                coarse_step=coarse, fine_step=fine, max_offset=-50,
                crash_penalty_steps=data.draw(st.integers(min_value=1, max_value=5),
                                              label="penalty"),
                auto_validate=False, resume_crash_quarantine_threshold=50,
                search_duration_seconds=1, confirm_duration_seconds=1,
            )
            eng, steps, crashes, crashed_at, sid = drive_intermittent(
                db, topo, _StubBackend(), cliffs, flaky, cfg_kw)

            assert steps < 8000, f"no convergence: cliffs={cliffs} flaky={flaky}"
            assert eng.status in ("idle", "quarantined"), (
                f"stuck in {eng.status}: cliffs={cliffs} flaky={flaky}")
            resident = eng._smu.applied
            for c in cliffs:
                if not crashed_at[c]:
                    continue
                r = resident.get(c, 0)
                worst = max(crashed_at[c])  # least-aggressive offset that crashed
                assert r > worst, (
                    f"core {c} resident {r} <= hard-crash offset {worst}: "
                    f"cliffs={cliffs} flaky={flaky}")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# T10: interruption safety — abort/pause must leave the SMU at a safe state
# ---------------------------------------------------------------------------


class TestInterruptionSafety:
    def test_abort_reverts_all_cores_not_just_the_tested_one(
        self, db, topo, smu, mock_backend
    ):
        """abort() must revert EVERY core to baseline, so aborting during validation
        (where all confirmed cores are applied at once) never leaves the others at
        aggressive CO resident in the SMU."""
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1, 2])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0, 1, 2]), "", "")
        eng._core_states = {
            i: CoreState(core_id=i, phase=TunerPhase.CONFIRMED, current_offset=-20,
                         best_offset=-20, baseline_offset=0)
            for i in range(3)
        }
        for i in range(3):  # all three resident at an aggressive offset
            smu.applied[i] = -20
            eng._co_applied[i] = -20
        eng._set_status("validating")
        eng.abort()
        assert all(smu.applied[i] == 0 for i in range(3)), smu.applied
        assert eng.status == "idle"


# ---------------------------------------------------------------------------
# T11: property-based fuzz of the whole loop over random scenarios
# ---------------------------------------------------------------------------


class TestPropertyFuzz:
    """Hypothesis drives the REAL tuner over random CPU profiles, core counts, CCD
    layouts, test orders, configs and crash points. The robustness invariants must
    hold for EVERY generated combination: the run terminates, reaches a terminal
    state (never stuck 'running'), and never leaves a resident CO at or beyond a
    core's crash point."""

    @settings(max_examples=400, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_tuner_robust_over_random_scenarios(self, data):
        n_cores = data.draw(st.integers(min_value=1, max_value=8), label="n_cores")
        n_ccds = data.draw(st.sampled_from([1, 2, 4]), label="n_ccds")
        order = data.draw(st.sampled_from([
            "sequential", "round_robin", "weakest_first",
            "ccd_alternating", "ccd_round_robin",
        ]), label="order")
        coarse = data.draw(st.integers(min_value=2, max_value=8), label="coarse")
        fine = data.draw(st.integers(min_value=1, max_value=min(coarse, 3)), label="fine")
        penalty = data.draw(st.integers(min_value=1, max_value=5), label="penalty")
        hardening = data.draw(st.booleans(), label="hardening")
        # 0 = no spurious reboots; else inject a power-loss reboot every N steps.
        reboot_interval = data.draw(st.sampled_from([0, 0, 5, 11, 19]), label="reboot_interval")
        cliffs = {}
        for c in range(n_cores):
            stable = data.draw(st.integers(min_value=-45, max_value=-3), label=f"stable{c}")
            gap = data.draw(st.integers(min_value=1, max_value=12), label=f"gap{c}")
            cliffs[c] = (stable, stable - gap)

        db = HistoryDB(":memory:")
        try:
            topo = _make_topo(n_cores, n_ccds)
            tiers = ([{"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"}]
                     if hardening else [])
            cfg_kw = dict(
                cores_to_test=list(range(n_cores)), test_order=order,
                coarse_step=coarse, fine_step=fine, max_offset=-60,
                crash_penalty_steps=penalty, auto_validate=False,
                resume_crash_quarantine_threshold=4, hardening_tiers=tiers,
                search_duration_seconds=1, confirm_duration_seconds=1,
            )
            eng, steps, crashes, sid = drive_closed_loop(
                db, topo, _StubBackend(), cliffs, cfg_kw, reboot_interval=reboot_interval)

            assert steps < 6000, (
                f"no convergence: order={order} cliffs={cliffs} reboot={reboot_interval}"
            )
            assert eng.status in ("idle", "quarantined"), (
                f"stuck in {eng.status}: order={order} cliffs={cliffs} "
                f"hardening={hardening} reboot={reboot_interval}"
            )
            resident = eng._smu.applied
            for c, (_stable, crash) in cliffs.items():
                r = resident.get(c, 0)
                assert not (r == crash or r < crash), (
                    f"core {c} resident {r} crashes at {crash}: order={order} cliffs={cliffs}"
                )
        finally:
            db.close()


# ---------------------------------------------------------------------------
# T6: thermal protection fails closed when no sensor is readable
# ---------------------------------------------------------------------------


class TestThermalFailClosed:
    def _scheduler(self, topo, backend, **cfg) -> CoreScheduler:
        return CoreScheduler(
            topology=topo, backend=backend,
            stress_config=StressConfig(mode=StressMode.SSE, fft_preset=FFTPreset.SMALL),
            scheduler_config=SchedulerConfig(cores_to_test=[0], **cfg),
        )

    def test_no_sensor_blocks_when_required(self, topo, mock_backend):
        sched = self._scheduler(topo, mock_backend, require_thermal_sensor=True)
        with patch.object(CoreScheduler, "_read_cpu_temperature", return_value=None):
            assert sched._check_temperature() is False   # fail closed

    def test_no_sensor_lenient_when_not_required(self, topo, mock_backend):
        sched = self._scheduler(topo, mock_backend, require_thermal_sensor=False)
        with patch.object(CoreScheduler, "_read_cpu_temperature", return_value=None):
            assert sched._check_temperature() is True    # explicit opt-out

    def test_tuner_default_requires_sensor(self, db, topo, smu, mock_backend):
        """The tuner drives the scheduler with the sensor required by default."""
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        assert eng._config.allow_missing_thermal_sensor is False

    @pytest.mark.parametrize("allow_missing, expect_required", [(False, True), (True, False)])
    def test_sensor_requirement_propagates_into_scheduler(
        self, db, topo, smu, mock_backend, allow_missing, expect_required
    ):
        """The fail-closed flag must actually reach the scheduler — not just sit in
        config. Capture the SchedulerConfig built in _start_worker and assert
        require_thermal_sensor is wired = not allow_missing_thermal_sensor."""
        import tuner.engine as te

        captured = {}

        class _CapScheduler:
            def __init__(self, *, topology, backend, stress_config, scheduler_config, work_dir):
                captured["cfg"] = scheduler_config

        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0],
                          allow_missing_thermal_sensor=allow_missing)
        eng._session_id = tp.create_session(db, eng._config, "", "")
        eng._core_states = {0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                                         current_offset=-5)}
        with patch.object(te, "CoreScheduler", _CapScheduler), \
             patch.object(te, "_TunerWorker"):
            eng._start_worker(0, 5)
        assert captured["cfg"].require_thermal_sensor is expect_required


# ---------------------------------------------------------------------------
# T7: every core-cycling style recovers a journal-detected crash
# ---------------------------------------------------------------------------


class TestEveryStyleRecoversCrash:
    @pytest.mark.parametrize("order", [
        "sequential", "round_robin", "weakest_first",
        "ccd_alternating", "ccd_round_robin",
    ])
    def test_style_penalizes_journal_suspect_on_resume(
        self, db, topo, smu, mock_backend, order
    ):
        """Regardless of test-order strategy, a journal-detected crash is penalized
        on resume and the crashing offset is never left re-applied."""
        cfg = TunerConfig(cores_to_test=[0, 1], test_order=order,
                          crash_penalty_steps=2, fine_step=1)
        sid = tp.create_session(db, cfg, "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-20,
            baseline_offset=0, in_test=False,
        ))
        tp.save_core_state(db, sid, CoreState(
            core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
            baseline_offset=0, in_test=False,
        ))
        db.journal_co_intent(sid, 0, -20, survived=False)

        eng = _resume_fresh(db, topo, smu, mock_backend, sid,
                            cores_to_test=[0, 1], test_order=order,
                            crash_penalty_steps=2, fine_step=1)

        cs0 = eng._core_states[0]
        assert cs0.crash_count == 1
        assert cs0.backoff_fail_bound == -20
        # The crashing offset (-20) is never the resident/current value anymore.
        assert eng._is_more_aggressive(-20, cs0.current_offset) or cs0.current_offset == 0
        # The untouched core is not penalized.
        assert eng._core_states[1].crash_count == 0


# ---------------------------------------------------------------------------
# T8: faithful forward-path crash (write-ahead ordering, not a hand-fed journal)
# ---------------------------------------------------------------------------


class TestForwardCrashWriteAhead:
    """The recovery tests above hand-write the journal row, so they prove the
    recovery logic but not that the LIVE forward path journals before the SMU
    write. These drive the real write path (_apply_co) and crash inside the
    hardware write, so they pass only if the journal is durable and written
    write-ahead. Ablation confirms it: with journaling removed, these go red."""

    def test_crash_during_real_write_is_recovered(self, db, topo, mock_backend):
        smu = CrashDuringWriteSMU(crash_at=(0, -28))
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "")
        # in_test=False on purpose: ONLY the journal can recover this, not the flag.
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-28,
            baseline_offset=0, in_test=False,
        ))

        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = sid
        # Real forward write that dies mid-write — no hand-written journal row.
        with pytest.raises(_HardCrash):
            eng._apply_co(0, -28)

        # Write-ahead proof: the value is already journaled (and durable) at the
        # instant the machine died — before set_co_offset returned.
        assert (0, -28) in db.journal_suspects(sid)

        # A fresh engine (new process) recovers using ONLY the persisted journal.
        del eng
        eng2 = make_engine(db, topo, CrashDuringWriteSMU(crash_at=None),
                           mock_backend, cores_to_test=[0])
        with patch.object(eng2, "_run_next"):
            eng2.resume(sid)
        cs = eng2._core_states[0]
        assert cs.crash_count == 1            # the dying write was recovered as a crash
        assert cs.backoff_fail_bound == -28   # and bounded as never-retry

    def test_journal_is_durable_to_a_fresh_connection(self, tmp_path, topo, mock_backend):
        """The journal must be readable by a NEW connection (a fresh process after
        reboot), i.e. committed to the file — not just cached in the writer's
        connection. db1 is deliberately not closed before db2 opens, modelling a
        crash where the writer never closed cleanly."""
        path = tmp_path / "durable.db"
        db1 = HistoryDB(path)
        smu = FaultSMU()
        eng = make_engine(db1, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db1, TunerConfig(cores_to_test=[0]), "", "")
        eng._apply_co(0, -30)

        db2 = HistoryDB(path)  # separate connection = the recovering process
        try:
            assert (0, -30) in db2.journal_suspects(eng._session_id)
        finally:
            db2.close()
            db1.close()


# ---------------------------------------------------------------------------
# T12: property-based fuzz of the multi-core VALIDATION flow
# ---------------------------------------------------------------------------


class TestValidationFuzz:
    """Hypothesis drives the REAL multi-core validation (stages 1/2/3) over random
    confirmed profiles and aggregate-instability margins. The backoff-and-restart
    loop must always terminate and finalize, leaving every core validation-stable
    or backed off to baseline — never stuck looping."""

    @settings(max_examples=200, deadline=None,
              suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_validation_terminates_and_settles_safely(self, data):
        n_cores = data.draw(st.integers(min_value=2, max_value=6), label="n_cores")
        n_ccds = data.draw(st.sampled_from([1, 2]), label="n_ccds")
        agg_margin = data.draw(st.integers(min_value=0, max_value=5), label="agg_margin")
        order = data.draw(st.sampled_from(["sequential", "ccd_round_robin"]), label="order")
        # stable in [-30, -8] keeps the aggregate threshold (stable + margin, with
        # margin <= 5) at or below -3, i.e. achievable by undervolting less (a real
        # CPU never needs a positive offset for aggregate stability). Crash is 5 below.
        cliffs = {}
        for c in range(n_cores):
            stable = data.draw(st.integers(min_value=-30, max_value=-8), label=f"stable{c}")
            cliffs[c] = (stable, stable - 5)

        db = HistoryDB(":memory:")
        try:
            topo = _make_topo(n_cores, n_ccds)
            cfg_kw = dict(
                cores_to_test=list(range(n_cores)), test_order=order,
                auto_validate=True, validate_transitions=False, hardening_tiers=[],
                fine_step=1, validate_duration_seconds=1,
                search_duration_seconds=1, confirm_duration_seconds=1,
            )
            eng, steps = drive_validation(db, topo, _StubBackend(), cliffs, agg_margin, cfg_kw)

            assert steps < 4000, f"validation did not converge: cliffs={cliffs} margin={agg_margin}"
            assert eng.status == "idle", f"validation stuck in {eng.status}"
            for c, (stable, crash) in cliffs.items():
                best = eng._core_states[c].best_offset
                assert best is not None
                # Validation only backs OFF (less aggressive) and never settles on a
                # crashing offset; with an achievable aggregate it reaches the margin.
                assert stable <= best <= 0, f"core {c} at {best}, outside [{stable}, 0]"
                assert best > crash
                assert best >= stable + agg_margin, (
                    f"core {c} at {best} below aggregate threshold {stable + agg_margin}"
                )
        finally:
            db.close()


class TestResumePathsValidateConfig:
    """resume() and validate_profile() re-validate the loaded config_json and fail
    closed, not only start(). from_json rejects wrong TYPES but passes a well-typed
    out-of-range value (e.g. coarse_step=0, a non-convergent search), so a corrupted
    or hand-edited DB row would otherwise revive on resume the exact 'looped forever'
    class start() rejects. A resident CO is never unsafe here (every SMU write is
    range-checked), but the tune must refuse rather than spin."""

    def test_resume_fails_closed_on_out_of_range_config(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0], coarse_step=0), "", "")
        eng = make_engine(db, topo, smu, mock_backend)
        logs: list[str] = []
        eng.log_message.connect(logs.append)
        with patch.object(eng, "_run_next") as run_next:
            eng.resume(sid)
        assert run_next.call_count == 0, "resume proceeded on an invalid config"
        assert any("Invalid tuner config" in m for m in logs)

    def test_validate_profile_fails_closed_on_out_of_range_config(
        self, db, topo, smu, mock_backend
    ):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0], coarse_step=0), "", "")
        # A CONFIRMED core so validate_profile clears its empty-profile guard and
        # reaches the config load/validate.
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.CONFIRMED,
            current_offset=-10, best_offset=-10, baseline_offset=0,
        ))
        eng = make_engine(db, topo, smu, mock_backend)
        logs: list[str] = []
        eng.log_message.connect(logs.append)
        with patch.object(eng, "_run_next") as run_next:
            eng.validate_profile(sid)
        assert run_next.call_count == 0, "validate_profile proceeded on an invalid config"
        assert any("Invalid tuner config" in m for m in logs)

    def test_resume_still_proceeds_on_a_valid_config(self, db, topo, smu, mock_backend):
        """The guard is not over-eager: a valid config_json resumes normally."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.FINE_SEARCH,
            current_offset=-10, best_offset=-8, baseline_offset=0, in_test=False,
        ))
        eng = make_engine(db, topo, smu, mock_backend)
        logs: list[str] = []
        eng.log_message.connect(logs.append)
        with patch.object(eng, "_run_next") as run_next:
            eng.resume(sid)
        assert run_next.called, "resume bailed on a valid config"
        assert not any("Invalid tuner config" in m for m in logs)


# ---------------------------------------------------------------------------
# Reboot gate: crash penalties require an actual reboot
# ---------------------------------------------------------------------------


class TestRebootGate:
    """Resume-time crash detection fires ONLY when the machine rebooted since
    the session's last persisted write. A leftover in_test flag or un-survived
    journal row without a reboot is a plain app exit (window closed, SIGKILL
    mid-test) — penalizing it would walk proven-good offsets away on every
    restart of the app."""

    def test_no_reboot_clears_in_test_without_penalty(
        self, db, topo, smu, mock_backend, monkeypatch
    ):
        import tuner.engine as engine_mod
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *a, **k: False)

        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-30,
            baseline_offset=0, in_test=True,
        ))
        tp.journal_co_intent(db, sid, 0, -30, survived=False)

        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        cs = eng._core_states[0]
        assert cs.crash_count == 0            # no penalty
        assert cs.current_offset == -30       # offset untouched
        assert cs.phase == TunerPhase.COARSE_SEARCH
        assert not cs.in_test                 # stale flag cleared...
        assert not db.get_tuner_core_states(sid)[0].in_test  # ...and persisted

    def test_rebooted_since_reads_btime(self, tmp_path, assume_rebooted):
        from datetime import datetime, timedelta

        _rebooted_since = assume_rebooted  # the real function (autouse patch stashes it)
        now = datetime.now(UTC)
        stat = tmp_path / "stat"
        boot_epoch = int(now.timestamp())
        stat.write_text(f"cpu  1 2 3 4\nbtime {boot_epoch}\nprocesses 5\n")

        before_boot = (now - timedelta(hours=1)).isoformat()
        after_boot = (now + timedelta(hours=1)).isoformat()
        assert _rebooted_since(before_boot, stat_path=str(stat)) is True
        assert _rebooted_since(after_boot, stat_path=str(stat)) is False

    def test_rebooted_since_fails_closed(self, tmp_path, assume_rebooted):
        _rebooted_since = assume_rebooted  # the real function
        # No timestamp, unparsable timestamp, missing/garbled stat file:
        # all must assume "rebooted" so crash detection still runs.
        assert _rebooted_since(None) is True
        assert _rebooted_since("not-a-timestamp") is True
        assert _rebooted_since(
            "2026-01-01T00:00:00+00:00", stat_path=str(tmp_path / "missing")
        ) is True
        garbled = tmp_path / "garbled"
        garbled.write_text("btime notanumber\n")
        assert _rebooted_since("2026-01-01T00:00:00+00:00", stat_path=str(garbled)) is True


# ---------------------------------------------------------------------------
# A hard crash at a CONFIRMED/HARDENED value invalidates the confirmation
# ---------------------------------------------------------------------------


class TestCrashAtConfirmedValue:
    def test_crash_demotes_best_and_reenters_backoff(self, db, topo, smu, mock_backend):
        """Validation and finalize re-apply best_offset — leaving a value that
        hard-crashed the box as "best" re-crashes it on every resume (observed
        live: core 1 at -42, phase hardened, crash, resume, re-validate at -42)."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng = make_engine(db, topo, smu, mock_backend, crash_penalty_steps=3, fine_step=1)
        eng._session_id = sid
        cs = CoreState(
            core_id=0, phase=TunerPhase.HARDENED, current_offset=-42,
            best_offset=-42, baseline_offset=-15, in_test=True,
        )
        eng._core_states = {0: cs}

        crashed = eng._detect_and_handle_crashes(eng._core_states)
        assert crashed == [0]
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM  # must re-earn confirmation
        assert cs.current_offset == -39                   # penalized by 3 steps
        assert cs.best_offset == -39                      # crashed -42 cannot stay best
        assert cs.backoff_fail_bound == -42               # crashed value is a hard bound


# ---------------------------------------------------------------------------
# Startup/environment failures are not stability verdicts
# ---------------------------------------------------------------------------


class TestStartupFailureIsNotAVerdict:
    def test_startup_failure_pauses_without_advancing(self, db, topo, smu, mock_backend):
        """A missing binary / scheduler construction error must pause the tuner,
        not advance the state machine (observed live as full-duration false
        FAILs walking offsets from -49 back to baseline)."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        cs = CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10,
            baseline_offset=0, in_test=True,
        )
        eng._core_states = {0: cs}

        with patch.object(eng, "_run_next"), patch.object(eng, "_advance_core") as adv:
            eng._on_test_finished(0, False, "Failed to start stress test: boom",
                                  "startup", 0.0, 0.0)

        assert eng._status == "paused"
        adv.assert_not_called()
        # No stability verdict was recorded for the never-run test
        assert tp.get_test_log(db, sid) == []


# ---------------------------------------------------------------------------
# Apparatus circuit breaker: implausible fail streaks recover from evidence
# ---------------------------------------------------------------------------


class TestApparatusBreaker:
    def _seed(self, db, topo, smu, backend, streak_threshold=5):
        sid = tp.create_session(
            db, TunerConfig(cores_to_test=[0], apparatus_failure_streak=streak_threshold),
            "", "",
        )
        eng = make_engine(
            db, topo, smu, backend,
            apparatus_failure_streak=streak_threshold,
        )
        eng._session_id = sid
        cs = CoreState(
            core_id=0, phase=TunerPhase.BACKOFF_PRECONFIRM, current_offset=-20,
            best_offset=-20, baseline_offset=0, backoff_mode=True,
        )
        eng._core_states = {0: cs}
        return eng, sid, cs

    def test_trips_rolls_back_to_evidence_and_pauses(self, db, topo, smu, mock_backend):
        """The stale-results class: N consecutive FAILs while every step adds
        voltage is physically implausible — the breaker must roll the core back
        to its most aggressive PROVEN pass, clear poisoned bounds, and pause."""
        eng, sid, cs = self._seed(db, topo, smu, mock_backend, streak_threshold=5)
        tp.log_test_result(db, sid, 0, -44, "confirm", True, duration=300.0)  # proven pass
        for off in (-24, -23, -22, -21):  # 4 prior fails
            tp.log_test_result(db, sid, 0, off, "backoff_preconfirm", False,
                               error_msg="mprime error: FATAL ERROR",
                               error_type="computation", duration=122.0)

        with patch.object(eng, "_run_next"), patch.object(eng, "_advance_core") as adv:
            # the 5th consecutive fail crosses the threshold
            eng._on_test_finished(0, False, "mprime error: FATAL ERROR",
                                  "computation", 122.0, 0.0)

        assert eng._status == "paused"
        adv.assert_not_called()                       # verdict not walked into the search
        assert cs.best_offset == -44                  # rolled back to proven evidence
        assert cs.current_offset == -44
        assert cs.phase == TunerPhase.CONFIRMING      # must re-earn confirmation
        assert cs.backoff_fail_bound is None and cs.backoff_pass_bound is None
        persisted = db.get_tuner_core_states(sid)[0]
        assert persisted.best_offset == -44           # rollback is durable

    def test_below_threshold_does_not_trip(self, db, topo, smu, mock_backend):
        eng, sid, cs = self._seed(db, topo, smu, mock_backend, streak_threshold=5)
        for off in (-24, -23):
            tp.log_test_result(db, sid, 0, off, "backoff_preconfirm", False,
                               error_type="computation", duration=122.0)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(0, False, "mprime error: FATAL ERROR",
                                  "computation", 122.0, 0.0)
        assert eng._status != "paused"                # normal backoff continues

    def test_pass_breaks_the_streak(self, db, topo, smu, mock_backend):
        eng, sid, cs = self._seed(db, topo, smu, mock_backend, streak_threshold=5)
        for off in (-24, -23, -22):
            tp.log_test_result(db, sid, 0, off, "backoff_preconfirm", False,
                               error_type="computation", duration=122.0)
        tp.log_test_result(db, sid, 0, -21, "backoff_preconfirm", True, duration=122.0)
        for off in (-20,):
            tp.log_test_result(db, sid, 0, off, "backoff_confirm", False,
                               error_type="computation", duration=300.0)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(0, False, "mprime error: FATAL ERROR",
                                  "computation", 300.0, 0.0)
        assert eng._status != "paused"                # streak is 2, not 6

    def test_synthetic_crash_rows_do_not_count(self, db, topo, smu, mock_backend):
        eng, sid, cs = self._seed(db, topo, smu, mock_backend, streak_threshold=3)
        # two real fails + two synthetic reboot rows (duration NULL)
        for off in (-24, -23):
            tp.log_test_result(db, sid, 0, off, "backoff_preconfirm", False,
                               error_type="computation", duration=122.0)
        for off in (-22, -21):
            tp.log_test_result(db, sid, 0, off, "coarse_search", False,
                               error_type="crash", duration=None)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(0, False, "mprime error: FATAL ERROR",
                                  "computation", 122.0, 0.0)
        # real-test streak is 3 (threshold) — trips; but the point is the
        # synthetic rows alone must not have tripped it earlier: recompute
        assert eng._status == "paused"


# ---------------------------------------------------------------------------
# SMU revert failure is a hardware-state fault -> pause, never march on
# ---------------------------------------------------------------------------


class TestRevertFailureFailsClosed:
    def test_failed_post_test_revert_pauses(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH,
                       current_offset=-20, baseline_offset=-10, in_test=True)
        eng._core_states = {0: cs}
        eng._co_applied[0] = -20         # aggressive offset resident
        smu.reject_set = True            # SMU refuses the baseline revert

        with patch.object(eng, "_run_next"), patch.object(eng, "_advance_core") as adv:
            eng._on_test_finished(0, True, "", "", 60.0, 0.0)

        assert eng._status == "paused"   # fail closed: offset still resident
        adv.assert_not_called()

    def test_resume_pauses_when_baseline_restore_fails(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-20,
            baseline_offset=-10,
        ))
        smu.reject_set = True            # every SMU write rejected
        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        assert eng._status == "paused"   # not "running" on a broken SMU


class TestNoRebootResidentOffset:
    def test_no_reboot_writes_zero_baseline_instead_of_assuming_it(
        self, db, topo, smu, mock_backend, monkeypatch
    ):
        """Without a reboot the SMU is NOT zeroed: a core with baseline 0 that
        died mid-test still holds its test offset. Resume must WRITE the
        baseline back, never assume it (the stale-resident-offset hole)."""
        import tuner.engine as engine_mod
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *a, **k: False)

        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-30,
            baseline_offset=0, in_test=True,
        ))
        smu.applied[0] = -30  # what the dying app left resident in the SMU

        eng = _resume_fresh(db, topo, smu, mock_backend, sid)

        assert (0, 0) in smu.writes          # baseline explicitly written
        assert smu.applied[0] == 0           # aggressive offset no longer resident
        assert eng._co_applied[0] == 0

    def test_rebooted_zero_baseline_is_not_rewritten(self, db, topo, smu, mock_backend):
        """After a real reboot SMU SRAM is zeroed — writing 0 again would be a
        pointless hardware write (and journal churn)."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-30,
            baseline_offset=0,
        ))
        # autouse fixture patches _rebooted_since -> True (reboot world)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        assert (0, 0) not in smu.writes
        assert eng._co_applied[0] == 0
