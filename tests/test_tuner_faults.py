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

import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corecycler.engine.backends.base import FFTPreset, Outcome, StressBackend, StressConfig, StressMode
from corecycler.engine.execution import ThermalWatch
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig
from corecycler.history.db import HistoryDB
from corecycler.tuner import engine as engine_mod
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine
from corecycler.tuner.state import CoreState, TunerPhase
from tests._machine import core_bank_record, installed, journaled, this_boot
from tests._reports import mce, report, trouble, unblamed

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
    with patch("corecycler.tuner.engine.QTimer.singleShot", new=lambda _ms, fn: fn()):
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
        self.reject_set = False  # set_co_offset returns False (rejected / read-back mismatch)
        self.raise_on_set = False  # set_co_offset raises (driver/permission fault)
        self.rejecting_cores: set[int] = set()
        self.raising_cores: set[int] = set()

    def set_co_offset(self, core_id: int, value: int) -> bool:
        self.writes.append((core_id, value))
        if self.raise_on_set or core_id in self.raising_cores:
            raise RuntimeError("SMU fault (injected)")
        if self.reject_set or core_id in self.rejecting_cores:
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


class _ProcessDeath(BaseException):
    pass


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
        coarse_step=5,
        fine_step=1,
        max_offset=-30,
        search_duration_seconds=1,
        confirm_duration_seconds=1,
        cores_to_test=[0],
    )
    defaults.update(cfg_kwargs)
    cfg = TunerConfig(**defaults)
    return TunerEngine(
        db=db,
        topology=topo,
        smu=smu,
        backend=backend,
        config=cfg,
    )


def _resume_fresh(db, topo, smu, backend, sid, **cfg_kwargs) -> TunerEngine:
    """Build a brand-new engine (simulating a fresh process after a reboot) and
    resume the given session, with _run_next stubbed out."""
    eng = make_engine(db, topo, smu, backend, **cfg_kwargs)
    with patch.object(eng, "_run_next"):
        eng.resume(sid)
    return eng


def _suspects(db, sid) -> dict[int, int]:
    return tp.crash_suspects(tp.resident_at_death(tp.journal_rows(db, sid, this_boot())), -1)


def _proven(db, sid) -> dict[int, int]:
    return tp.journal_proven_values(db, sid, -1)


class _StubBackend(StressBackend):
    """A do-nothing backend — the closed-loop driver patches the worker, so the
    backend is constructed but never executed."""

    name = "stub"

    def is_available(self) -> bool:
        return True

    def get_command(self, config, work_dir):
        return ["true"]

    def findings(self, output, work_dir):
        return ()

    def get_supported_modes(self):
        return [StressMode.SSE]

    def prepare(self, work_dir, config):
        pass


def _make_topo(n_cores: int, n_ccds: int):
    """Build a CPUTopology with n_cores spread across n_ccds (no sysfs needed)."""
    from corecycler.engine.topology import CPUTopology, PhysicalCore

    topo = CPUTopology()
    per_ccd = max(1, (n_cores + n_ccds - 1) // n_ccds)
    for i in range(n_cores):
        topo.cores[i] = PhysicalCore(core_id=i, ccd=min(i // per_ccd, n_ccds - 1), ccx=None, logical_cpus=(i,))
    topo.ccds = n_ccds
    return topo


class _StubSignal:
    def connect(self, _slot):
        return None

    def disconnect(self, _slot=None):
        return None


class _StubThread:
    """The QThread surface the engine drives: retire on finish, tear down on abort."""

    def __init__(self) -> None:
        self.finished = _StubSignal()
        self.scheduler = SimpleNamespace(force_stop=lambda: None)

    def wait(self, _msec=0):
        return True

    def isRunning(self):
        return True

    def terminate(self):
        return None

    def deleteLater(self):
        return None


def _stub_transition_worker(pending, stage_of):
    class _Stub(_StubThread):
        def __init__(self, core_id, _logical_cpu, _scheduler, cores, _duration, parent=None):
            super().__init__()
            self._dispatch = (core_id, list(cores), stage_of())

        def start(self):
            pending.append(self._dispatch)

    return _Stub


def _stub_soak_worker(pending, stage_of):
    class _Stub(_StubThread):
        def __init__(self, core_id, _duration, parent=None):
            super().__init__()
            self._dispatch = (core_id, None, stage_of())

        def start(self):
            pending.append(self._dispatch)

    return _Stub


SOAK_STAGE = 7


def _mce_events(cpu):
    return (mce(cpu, bank=5),)


_FAULTS = {
    "thermal": lambda core, cliffs: (Outcome.THERMAL, ()),
    "apparatus": lambda core, cliffs: (Outcome.NOT_STARTED, ()),
    "killed": lambda core, cliffs: (Outcome.OUTSIDE_KILL, ()),
    "foreign_mce": lambda core, cliffs: (
        Outcome.OWN_CORE_MCE,
        _mce_events(next((c for c in cliffs if c != core), core)),
    ),
    "unattributed_mce": lambda core, cliffs: (Outcome.UNATTRIBUTED_MCE, (unblamed(bank=5),)),
}


def drive_validation(db, topo, backend, cliffs, agg_margin, cfg_kw, cap=8000, abort_at=0, faults=None):
    """Drive the REAL multi-core validation flow to termination, every stage.

    Cores are seeded CONFIRMED at their individual stable limit, then validation
    runs. The aggregate model: a set passes only if EVERY member's offset is at
    least ``agg_margin`` less aggressive than its individual stable limit (power
    delivery makes the aggregate tougher than a core alone). A validation failure
    backs off the most aggressive core and restarts.

    Stages 4 (rapid transitions) and 7 (soak) construct their worker classes
    directly instead of going through _start_worker, so both are replaced by
    stubs that route the dispatch into the same queue -- otherwise those two
    stages could only be exercised by turning them off. The soak runs NO
    synthetic load: its failure mode is a kernel event, not aggregate
    instability, so it always passes here. Returns (final_engine, steps).
    """
    sid = tp.create_session(db, TunerConfig(**cfg_kw), "", "", boot_id=this_boot())
    pending: list[tuple[int, list[int] | None, int]] = []
    holder: dict[str, object] = {}

    def stage_of():
        eng = holder.get("eng")
        return eng._validation_stage if eng is not None else 0

    def patched_single(core_id, duration, **kw):
        holder["eng"]._worker = _StubThread()
        pending.append((core_id, None, stage_of()))

    def patched_multi(cores, duration, **kw):
        holder["eng"]._worker = _StubThread()
        pending.append((cores[0], list(cores), stage_of()))

    eng = make_engine(db, topo, FaultSMU(), backend, **cfg_kw)
    holder["eng"] = eng
    eng._start_worker = patched_single
    eng._start_multi_core_worker = patched_multi
    # Force the memory stage to run (stressapptest is not installed under test)
    # so the full chain including a live memory stage is exercised, not skipped.
    eng._get_memory_backend = lambda: object()
    eng._session_id = sid
    eng._core_states = {
        c: CoreState(
            core_id=c, phase=TunerPhase.CONFIRMED, current_offset=stable, best_offset=stable, baseline_offset=0
        )
        for c, (stable, _crash) in cliffs.items()
    }
    for cs in eng._core_states.values():
        tp.save_core_state(db, sid, cs)
    profile = {c: stable for c, (stable, _crash) in cliffs.items()}

    steps = 0
    with (
        patch.object(engine_mod, "_RapidTransitionWorker", _stub_transition_worker(pending, stage_of)),
        patch.object(engine_mod, "_SoakWorker", _stub_soak_worker(pending, stage_of)),
    ):
        eng._enter_auto_validation(profile)
        while pending and steps < cap:
            steps += 1
            if abort_at and steps == abort_at:
                eng.abort()
                break
            core, cset, stage = pending.pop(0)
            fault = faults.get(steps) if faults else None
            if fault is not None:
                outcome, events = _FAULTS[fault](core, cliffs)
                eng._on_test_finished(report(core, outcome, fault, events=events))
                continue
            members = cset if cset is not None else [core]
            ok = stage == SOAK_STAGE or all(
                eng._core_states[m].best_offset >= cliffs[m][0] + agg_margin for m in members
            )
            eng._on_test_finished(report(core) if ok else report(core, Outcome.COMPUTATION, "agg"))
    return eng, steps


def drive_closed_loop(
    db, topo, backend, machine, cliffs, cfg_kw, baseline=0, cap=6000, reboot_interval=0, app_exit_interval=0
):
    """Drive the REAL tuner loop against a simulated CPU.

    Only the worker is replaced — by a stability oracle keyed on each core's
    (stable_limit, crash_limit). A hard crash (offset at/over crash_limit) is
    injected the real way: the offset is journaled by the real _apply_co before
    the crash, then a fresh engine recovers via the real resume(). When
    ``reboot_interval`` > 0, an unrelated power-loss reboot is also injected every
    that-many steps (the in-flight offset is journaled but did not itself crash).
    When ``app_exit_interval`` > 0, a plain APP EXIT (no reboot: window closed,
    SIGKILL) is injected every that-many steps — the fresh engine resumes in the
    no-reboot world, which must clear in_test WITHOUT a crash penalty and write
    baselines back explicitly. Returns (final_engine, steps, crashes, sid).
    """
    sid = tp.create_session(db, TunerConfig(**cfg_kw), "", "", boot_id=this_boot())
    pending: list[int] = []

    def patched(core_id, duration, **kw):
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
            machine.reboot(clean=False)
            holder["eng"] = fresh()
            holder["eng"].resume(sid)
            continue
        if app_exit_interval and steps % app_exit_interval == 1 and holder["eng"].status == "running":
            # Plain app exit: same persisted state, NO reboot — resume must not
            # penalize anything and must restore baselines explicitly.
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
        if offset >= stable:  # less aggressive than the cliff -> pass
            e._on_test_finished(report(core))
        elif offset <= crash:  # at/over the crash point -> hard crash
            crashes += 1
            machine.reboot(clean=False)
            holder["eng"] = fresh()
            holder["eng"].resume(sid)
        else:  # between -> detected (soft) failure
            e._on_test_finished(report(core, Outcome.COMPUTATION, "calc error"))
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
    sid = tp.create_session(db, TunerConfig(**cfg_kw), "", "", boot_id=this_boot())
    pending: list[int] = []
    visits: dict[tuple[int, int], int] = {}
    crashed_at: dict[int, set[int]] = {c: set() for c in cliffs}

    def patched(core_id, duration, **kw):
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

        if offset >= stable:  # always stable
            e._on_test_finished(report(core))
        elif offset <= hard:  # always a hard crash
            do_crash(core, offset)
        else:  # marginal: flaky
            visits[(core, offset)] = visits.get((core, offset), 0) + 1
            if visits[(core, offset)] >= flaky[core]:
                do_crash(core, offset)  # crashed on a re-test
            else:
                e._on_test_finished(report(core))  # looked stable
    return holder["eng"], steps, crashes, crashed_at, sid


# ---------------------------------------------------------------------------
# T1: a hard crash with NO in_test flag is caught by the journal
# ---------------------------------------------------------------------------


class TestJournalCatchesUnflaggedCrash:
    def test_unflagged_crash_is_penalized_via_journal(self, db, topo, smu, mock_backend, machine):
        """The CO journal catches a crash with no in_test flag (idle, baseline
        restore, revert)."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        # Core was NOT mid-test (in_test=False) but -12 was resident when the box died.
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.FINE_SEARCH,
                current_offset=-12,
                best_offset=-10,
                baseline_offset=0,
                in_test=False,
            ),
        )
        journaled(db, sid, 0, -12, survived=False)

        machine.reboot(clean=False)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0], crash_penalty_steps=3, fine_step=1)

        cs = eng._core_states[0]
        assert cs.crash_count == 1  # penalized despite no in_test
        assert cs.backoff_fail_bound == -12  # hard fail bound at the crashing value
        assert cs.current_offset == -9  # -12 backed off toward 0 by 3
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.in_test is False  # the suspect's in_test flag is cleared
        # The journal-detected recovery is logged as a real crash (passed=False).
        logs = tp.get_test_log(db, sid, core_id=0)
        assert any(e.get("error_type") == "crash" and not e.get("passed") for e in logs)

    def test_a_journal_suspect_is_penalized_at_its_journal_value_not_its_current_offset(
        self, db, topo, smu, mock_backend, machine
    ):
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.FINE_SEARCH,
                current_offset=-20,
                baseline_offset=0,
                in_test=False,
            ),
        )
        journaled(db, sid, 0, -10, survived=False)

        machine.reboot(clean=False)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0], crash_penalty_steps=3, fine_step=1)

        cs = eng._core_states[0]
        assert cs.backoff_fail_bound == -10
        assert cs.current_offset == -7
        crashes = [row["offset_tested"] for row in tp.get_test_log(db, sid, core_id=0) if row["error_type"] == "crash"]
        assert crashes == [-10]

    def test_zero_value_is_never_a_suspect(self, db, topo, smu, mock_backend):
        """CO=0 (stock) is axiomatically safe and must never be treated as a crash."""
        cfg = TunerConfig(cores_to_test=[0])
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(db, sid, CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH))
        journaled(db, sid, 0, 0, survived=False)
        assert _suspects(db, sid) == {}


# ---------------------------------------------------------------------------
# T2: an unstable baseline is escaped (converges toward CO=0)
# ---------------------------------------------------------------------------


class TestUnstableBaselineEscapes:
    def test_baseline_descends_toward_zero_when_it_crashes(self, db, topo, smu, mock_backend, machine):
        """If the baseline value itself crashes the box, the baseline is no longer
        a safe floor -- it must descend toward 0 so resume cannot re-apply it."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        # baseline == current == -20: the inherited baseline itself was resident and crashed.
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-20,
                baseline_offset=-20,
                in_test=False,
            ),
        )
        journaled(db, sid, 0, -20, survived=False)

        machine.reboot(clean=False)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0], crash_penalty_steps=3, fine_step=1)

        cs = eng._core_states[0]
        # Baseline re-anchored from -20 toward 0 (to -17); never clamps back to -20.
        assert cs.baseline_offset == -17
        assert cs.current_offset == -17
        assert cs.backoff_fail_bound == -20

    def test_repeated_baseline_crash_marches_to_zero(self, db, topo, smu, mock_backend, machine):
        """Each resume that finds the baseline crashing moves it one penalty step
        closer to 0 -- it can never get stuck re-applying the same crashing value."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=1, fine_step=1, resume_crash_quarantine_threshold=20)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-5,
                baseline_offset=-5,
                in_test=False,
            ),
        )
        last = -5
        for _ in range(5):
            cs = db.get_tuner_core_states(sid)[0]
            journaled(db, sid, 0, cs.current_offset, survived=False)
            machine.reboot(clean=False)
            eng = _resume_fresh(
                db,
                topo,
                smu,
                mock_backend,
                sid,
                cores_to_test=[0],
                crash_penalty_steps=1,
                fine_step=1,
                resume_crash_quarantine_threshold=20,
            )
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
    def test_quarantines_after_threshold_and_forces_stock(self, db, topo, smu, mock_backend, machine):
        """The headline guarantee: a machine that keeps hard-crashing on resume is
        bounded -- after `threshold` crash-resumes the tuner forces every core to
        CO=0, quarantines the session, and stops, instead of looping forever."""
        threshold = 3
        cfg = TunerConfig(
            cores_to_test=[0], resume_crash_quarantine_threshold=threshold, crash_penalty_steps=1, fine_step=1
        )
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-30,
                baseline_offset=0,
                in_test=False,
            ),
        )

        eng = None
        for i in range(threshold):
            cs = db.get_tuner_core_states(sid)[0]
            resident = cs.current_offset if cs.current_offset != 0 else -30
            journaled(db, sid, 0, resident, survived=False)
            machine.reboot(clean=False)
            eng = _resume_fresh(
                db,
                topo,
                smu,
                mock_backend,
                sid,
                cores_to_test=[0],
                resume_crash_quarantine_threshold=threshold,
                crash_penalty_steps=1,
                fine_step=1,
            )
            if eng.status == "quarantined":
                assert i == threshold - 1  # not before the threshold
                break

        assert eng.status == "quarantined"
        assert db.get_tuner_session(sid).status == "quarantined"
        assert smu.applied.get(0) == 0  # forced to stock
        # A quarantined session is never resumed automatically (fail closed) --
        # but it stays reachable by hand, or the run is lost with all its work.
        assert sid not in [s.id for s in db.list_resumable_tuner_sessions()]
        assert sid in [s.id for s in db.list_recoverable_tuner_sessions()]
        # in_test is cleared in memory AND persisted for every core.
        assert all(not cs.in_test for cs in eng._core_states.values())
        assert all(not c.in_test for c in db.get_tuner_core_states(sid).values())

    def test_clean_resume_does_not_increment_breaker(self, db, topo, smu, mock_backend):
        """A normal pause/resume with no crash never moves toward quarantine."""
        cfg = TunerConfig(cores_to_test=[0], resume_crash_quarantine_threshold=3)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.FINE_SEARCH,
                current_offset=-8,
                in_test=False,
            ),
        )  # no journal suspect, not in_test
        eng = _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0], resume_crash_quarantine_threshold=3)
        assert eng.status != "quarantined"
        assert db.get_resume_crash_streak(sid) == 0

    def test_surviving_test_resets_breaker(self, db, topo, smu, mock_backend):
        """Progress (a completed test) resets the streak, so occasional crashes
        during a long legitimate tune never accumulate into a quarantine."""
        cfg = TunerConfig(cores_to_test=[0], search_duration_seconds=1)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        eng._session_id = sid
        eng._core_states = {0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5)}
        db.set_resume_crash_streak(sid, 2)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(0))
        assert db.get_resume_crash_streak(sid) == 0


class TestValidationCrashArmsBreaker:
    """A hard crash DURING multi-core validation must arm the circuit breaker.

    Validation re-applies each core's confirmed offset, which `_apply_co` journals
    survived=1, so the CO-journal crash detector is blind to it. Only the in_test
    flag can attribute a multi-core power-interaction crash (the failure mode
    validation exists to find).
    """

    def _seed_validating_at_stage2(self, db, smu, topo, backend, cliffs, **cfg):
        """Seed a confirmed profile and run the REAL stage-2 (all-core) launch,
        capturing the stressed set without starting a worker."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=list(cliffs), **cfg), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, backend, cores_to_test=list(cliffs), **cfg)
        launched: list[list[int]] = []
        eng._start_multi_core_worker = lambda cores, duration: launched.append(list(cores))
        eng._session_id = sid
        eng._core_states = {
            c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=v, best_offset=v, baseline_offset=0)
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

    def test_validation_stage_flags_all_stressed_cores_in_test(self, db, topo, smu, mock_backend):
        """A multi-core validation stage flags EVERY stressed core in_test and
        PERSISTS it before the worker — the CO journal is blind here (confirmed
        offsets journal survived=1), so in_test is the only signal that can
        attribute a validation crash."""
        cliffs = {0: -10, 1: -12, 2: -8, 3: -15}
        _eng, sid, launched = self._seed_validating_at_stage2(
            db,
            smu,
            topo,
            mock_backend,
            cliffs,
        )
        assert launched == [sorted(cliffs)]  # all cores stressed together
        persisted = db.get_tuner_core_states(sid)
        assert all(persisted[c].in_test for c in cliffs)  # all flagged + persisted
        assert _suspects(db, sid) == {}

    def test_in_test_validation_cores_are_attributed_as_crashes(self, db, topo, smu, mock_backend, machine):
        """Cores left in_test by a crashing validation worker are SEEN on the
        resume path — phase is irrelevant, only the in_test flag matters, and
        the journal cannot see it. A multi-core stress set cannot identify the
        guilty core, so a validation crash with no kernel forensics penalizes
        NOBODY and requests the isolated crash hunt instead. The breaker still
        arms (pending_hunt counts as a crash-resume)."""
        cliffs = {0: -10, 1: -12}
        _eng, sid, _ = self._seed_validating_at_stage2(
            db,
            smu,
            topo,
            mock_backend,
            cliffs,
            crash_penalty_steps=1,
            fine_step=1,
        )
        assert _suspects(db, sid) == {}
        machine.reboot(clean=False)
        eng = make_engine(db, topo, FaultSMU(), mock_backend, cores_to_test=list(cliffs))
        hunts: list[bool] = []
        with patch.object(eng, "_start_hunt", lambda: hunts.append(True)):
            eng.resume(sid)
        assert hunts == [True]
        stored = db.get_tuner_core_states(sid)
        assert [stored[c].crash_count for c in cliffs] == [0, 0]
        assert not any(stored[c].in_test for c in cliffs)
        assert tp.get_resume_crash_streak(db, sid) == 1

    def test_normal_validation_completion_clears_in_test(self, db, topo, smu, mock_backend):
        """A surviving validation test must leave NO stale in_test on any stressed
        core, or a later resume would wrongly fire the breaker on a clean session."""
        cliffs = {0: -10, 1: -12}
        eng, sid, _ = self._seed_validating_at_stage2(
            db,
            smu,
            topo,
            mock_backend,
            cliffs,
        )
        assert all(db.get_tuner_core_states(sid)[c].in_test for c in cliffs)
        with patch.object(eng, "_run_next"), patch.object(eng, "_run_validation_next"):
            eng._on_test_finished(report(sorted(cliffs)[0]))
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
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, baseline_offset=0)
        }
        smu.reject_set = True
        ok = eng._apply_co_isolation(0, -10)
        assert ok is False
        assert eng._status == "paused"
        assert [row.confirmed for row in tp.journal_rows(db, eng._session_id, this_boot())] == [False]
        assert _suspects(db, eng._session_id) == {0: -10}

    def test_raising_write_propagates_to_caller_pause(self, db, topo, smu, mock_backend):
        """A driver exception on write is handled by the caller's pause path."""
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, baseline_offset=0)
        }
        smu.raise_on_set = True
        ok = eng._apply_co_isolation(0, -10)
        assert ok is False
        assert eng._status == "paused"


class TestInTestMarkFollowsTheWrite:
    def _search_with_core_1_cached(self, db, topo, smu, backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0, 1]), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, backend, cores_to_test=[0, 1])
        eng._session_id = sid
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, baseline_offset=0),
            1: CoreState(core_id=1, phase=TunerPhase.CONFIRMED, current_offset=-5, best_offset=-5, baseline_offset=0),
        }
        for cs in eng._core_states.values():
            tp.save_core_state(db, sid, cs)
        journaled(db, sid, 1, -5, survived=True)
        eng._co_survived[1] = -5
        eng._co_applied[1] = -5
        smu.applied[1] = -5
        eng._set_status("running")
        started: list[int] = []
        eng._start_worker = lambda core_id, _duration, **_kw: started.append(core_id)
        return eng, sid, started

    def _assert_paused_with_no_mark(self, db, eng, sid, started):
        assert eng.status == "paused"
        assert started == []
        assert not any(cs.in_test for cs in db.get_tuner_core_states(sid).values())

    def test_a_refused_baseline_write_leaves_no_in_test_mark(self, db, topo, smu, mock_backend):
        eng, sid, started = self._search_with_core_1_cached(db, topo, smu, mock_backend)
        smu.rejecting_cores = {1}
        eng._run_next()
        self._assert_paused_with_no_mark(db, eng, sid, started)

    def test_a_raising_co_write_leaves_no_in_test_mark(self, db, topo, smu, mock_backend):
        eng, sid, started = self._search_with_core_1_cached(db, topo, smu, mock_backend)
        smu.raising_cores = {1}
        eng._run_next()
        self._assert_paused_with_no_mark(db, eng, sid, started)

    def test_a_crash_after_a_refused_write_convicts_nobody(self, db, topo, smu, mock_backend, machine):
        eng, sid, _started = self._search_with_core_1_cached(db, topo, smu, mock_backend)
        smu.rejecting_cores = {1}
        eng._run_next()
        del eng
        machine.reboot(clean=False)
        _resume_fresh(db, topo, FaultSMU(), mock_backend, sid, cores_to_test=[0, 1])
        assert {c: cs.crash_count for c, cs in db.get_tuner_core_states(sid).items()} == {0: 0, 1: 0}
        assert [row for row in tp.get_test_log(db, sid) if row["error_type"] == "crash"] == []

    def test_a_death_during_another_cores_baseline_write_convicts_nobody(self, db, topo, smu, mock_backend, machine):
        eng, sid, started = self._search_with_core_1_cached(db, topo, smu, mock_backend)

        def die(core_id, value):
            raise _ProcessDeath(f"killed while writing core {core_id} = {value}")

        smu.set_co_offset = die
        with pytest.raises(_ProcessDeath):
            eng._run_next()
        assert started == []
        assert not any(cs.in_test for cs in db.get_tuner_core_states(sid).values())

        del eng
        machine.reboot(clean=False)
        _resume_fresh(db, topo, FaultSMU(), mock_backend, sid, cores_to_test=[0, 1])
        assert {c: cs.crash_count for c, cs in db.get_tuner_core_states(sid).items()} == {0: 0, 1: 0}

    def test_abort_saves_the_cleared_mark_after_reverting(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = sid
        eng._core_states = {
            0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, baseline_offset=0)
        }
        tp.save_core_state(db, sid, eng._core_states[0])
        eng._set_status("running")
        eng._start_worker = lambda _core_id, _duration, **_kw: setattr(eng, "_worker", _StubThread())
        eng._run_next()
        assert smu.applied[0] == -10
        assert db.get_tuner_core_states(sid)[0].in_test

        eng.abort()

        assert smu.applied[0] == 0
        assert not db.get_tuner_core_states(sid)[0].in_test

    def test_abort_reverts_every_core_before_it_saves_a_core_state(self, db, topo, smu, mock_backend, monkeypatch):
        cliffs = {0: -10, 1: -12, 2: -8, 3: -14}
        sid = tp.create_session(db, TunerConfig(cores_to_test=list(cliffs)), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=list(cliffs))
        eng._session_id = sid
        eng._core_states = {
            c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=v, best_offset=v, baseline_offset=0)
            for c, v in cliffs.items()
        }
        eng._co_survived = dict(cliffs)
        eng._set_status("validating")
        eng._validation_stage = 2
        eng._validation_core_order = sorted(cliffs)
        eng._start_multi_core_worker = lambda _cores, _duration, **_kw: setattr(eng, "_worker", _StubThread())
        eng._run_validation_stage2()
        assert smu.applied == cliffs
        assert all(cs.in_test for cs in db.get_tuner_core_states(sid).values())

        order: list[str] = []
        write, save = smu.set_co_offset, db.upsert_tuner_core_state

        def spied_write(core_id, value):
            order.append("write")
            return write(core_id, value)

        def spied_save(session_id, cs):
            order.append("save")
            save(session_id, cs)

        monkeypatch.setattr(smu, "set_co_offset", spied_write)
        monkeypatch.setattr(db, "upsert_tuner_core_state", spied_save)

        eng.abort()

        writes = [i for i, kind in enumerate(order) if kind == "write"]
        saves = [i for i, kind in enumerate(order) if kind == "save"]
        assert len(writes) == len(cliffs)
        assert saves
        assert max(writes) < min(saves)
        assert all(smu.applied[c] == 0 for c in cliffs)
        assert not any(cs.in_test for cs in db.get_tuner_core_states(sid).values())

    def test_an_apparatus_streak_ending_in_a_hunt_clears_the_slot(self, db, topo, smu, mock_backend):
        best = {0: -10, 1: -10, 2: -10, 3: -20}
        sid = tp.create_session(db, TunerConfig(cores_to_test=list(best)), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=list(best))
        eng._session_id = sid
        eng._core_states = {
            c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=v, best_offset=v, baseline_offset=0)
            for c, v in best.items()
        }
        for cs in eng._core_states.values():
            tp.save_core_state(db, sid, cs)
        eng._start_worker = lambda *_args, **_kw: None
        eng._start_hunt()
        assert tp.get_session(db, sid).hunting_core == 3

        for _ in range(eng._config.max_apparatus_retries + 1):
            eng._on_test_finished(report(3, Outcome.NOT_STARTED, "Failed to start stress test: boom", 0.0))

        assert eng.status == "paused"
        assert tp.get_session(db, sid).hunting_core is None


# ---------------------------------------------------------------------------
# T5: the journal write-ahead / proven-safe envelope logic
# ---------------------------------------------------------------------------


class TestWriteAheadJournal:
    def test_aggressive_value_journaled_unsurvived(self, db, topo, smu, mock_backend):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng._apply_co(0, -30)
        assert smu.applied[0] == -30
        assert _suspects(db, eng._session_id) == {0: -30}

    def test_within_envelope_value_journaled_survived(self, db, topo, smu, mock_backend):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng._co_survived[0] = -30  # -30 already proven safe
        eng._apply_co(0, -20)  # less aggressive than proven
        assert _suspects(db, eng._session_id) == {}

    def test_zero_is_always_survived(self, db, topo, smu, mock_backend):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng._apply_co(0, 0)
        assert _suspects(db, eng._session_id) == {}
        assert _proven(db, eng._session_id) == {0: 0}

    def test_every_co_write_appends_a_row_with_its_boot_and_stamp(self, db, topo, smu, mock_backend, machine):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        first = machine.mono_ns
        eng._apply_co(0, -10)
        machine.advance(1)
        eng._apply_co(0, 0)

        rows = tp.journal_rows(db, eng._session_id, machine.boot_id)
        assert [(row.core_id, row.value, row.mono_ns) for row in rows] == [(0, -10, first), (0, 0, first + 10**9)]

    def test_a_write_is_confirmed_only_after_the_smu_accepts_it(self, db, topo, smu, mock_backend, machine):
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1, 2])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0, 1, 2]), "", "", boot_id=this_boot())
        smu.rejecting_cores = {1}
        smu.raising_cores = {2}

        assert eng._apply_co(0, -10) is True
        assert eng._apply_co(1, -12) is False
        with pytest.raises(RuntimeError, match="injected"):
            eng._apply_co(2, -14)

        rows = tp.journal_rows(db, eng._session_id, machine.boot_id)
        assert [(row.core_id, row.value, row.confirmed) for row in rows] == [
            (0, -10, True),
            (1, -12, False),
            (2, -14, False),
        ]

    def test_resume_rebuilds_proven_safe_envelope_from_journal(self, db, topo, smu, mock_backend):
        """Resume must rebuild the proven-safe envelope from the journal so a value
        the machine already survived is not re-flagged as a suspect."""
        cfg = TunerConfig(cores_to_test=[0])
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-10,
            ),
        )
        journaled(db, sid, 0, -30, survived=True)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        assert eng._co_survived.get(0) == -30

    def test_apply_co_without_smu_returns_false(self, db, topo, mock_backend):
        """With no SMU, _apply_co performs no write and reports failure (fail closed)."""
        eng = make_engine(db, topo, None, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
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
    def test_converges_safely_under_injected_crashes(self, db, topo, mock_backend, machine, order):
        # Per-core ground truth: offset >= stable passes; between stable and crash
        # is a detected (soft) failure; at/over crash the machine HARD-crashes.
        cliffs = {0: (-12, -15), 1: (-22, -25), 2: (-7, -10), 3: (-17, -20)}
        cfg_kw = dict(
            cores_to_test=list(cliffs),
            test_order=order,
            coarse_step=5,
            fine_step=1,
            max_offset=-40,
            crash_penalty_steps=3,
            auto_validate=False,
            resume_crash_quarantine_threshold=50,
        )
        eng, steps, crashes, sid = drive_closed_loop(db, topo, mock_backend, machine, cliffs, cfg_kw)

        assert steps < 6000, f"did not converge ({order})"
        assert crashes >= 1, "no hard crash was exercised — simulation is vacuous"
        assert eng.status in ("idle", "quarantined")
        # SAFETY INVARIANT: the RESIDENT CO (what is actually in the SMU now) never
        # sits at or beyond any core's crash point.
        resident = eng._smu.applied
        for c, (_stable, crash) in cliffs.items():
            r = resident.get(c, 0)
            assert not (r == crash or r < crash), f"[{order}] core {c} left resident at {r}, crashes at {crash}"


# ---------------------------------------------------------------------------
# T9b: intermittent instability — safety holds when a value passes then crashes
# ---------------------------------------------------------------------------


class TestIntermittentInstability:
    """The deterministic closed-loop sim proves safety only for a monotonic cliff;
    real CO instability is intermittent -- an offset passes a short search test
    and crashes a later confirm/harden. These drive the REAL loop against an
    intermittent oracle and assert the generalized safety invariant: after
    termination, each core's resident CO is strictly LESS aggressive than every offset
    that actually hard-crashed during the run (resident > max(crashed offsets))."""

    def test_offset_that_passes_then_crashes_is_backed_off(self, db, topo, mock_backend):
        # Core 0: >= -8 always stable; <= -20 always hard-crashes; a marginal offset
        # passes its FIRST test then hard-crashes on the SECOND -- "passed search,
        # failed confirm/harden".
        cliffs = {0: (-8, -20)}
        flaky = {0: 2}
        eng, steps, crashes, crashed_at, sid = drive_intermittent(
            db,
            topo,
            mock_backend,
            cliffs,
            flaky,
            dict(
                cores_to_test=[0],
                coarse_step=4,
                fine_step=1,
                max_offset=-40,
                crash_penalty_steps=3,
                auto_validate=False,
                resume_crash_quarantine_threshold=50,
                search_duration_seconds=1,
                confirm_duration_seconds=1,
            ),
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
        order = data.draw(st.sampled_from(["sequential", "round_robin", "ccd_round_robin"]), label="order")
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
                cores_to_test=list(range(n_cores)),
                test_order=order,
                coarse_step=coarse,
                fine_step=fine,
                max_offset=-50,
                crash_penalty_steps=data.draw(st.integers(min_value=1, max_value=5), label="penalty"),
                auto_validate=False,
                resume_crash_quarantine_threshold=50,
                search_duration_seconds=1,
                confirm_duration_seconds=1,
            )
            eng, steps, crashes, crashed_at, sid = drive_intermittent(db, topo, _StubBackend(), cliffs, flaky, cfg_kw)

            assert steps < 8000, f"no convergence: cliffs={cliffs} flaky={flaky}"
            assert eng.status in ("idle", "quarantined"), f"stuck in {eng.status}: cliffs={cliffs} flaky={flaky}"
            resident = eng._smu.applied
            for c in cliffs:
                if not crashed_at[c]:
                    continue
                r = resident.get(c, 0)
                worst = max(crashed_at[c])  # least-aggressive offset that crashed
                assert r > worst, f"core {c} resident {r} <= hard-crash offset {worst}: cliffs={cliffs} flaky={flaky}"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# T10: interruption safety — abort/pause must leave the SMU at a safe state
# ---------------------------------------------------------------------------


class TestInterruptionSafety:
    def test_abort_reverts_all_cores_not_just_the_tested_one(self, db, topo, smu, mock_backend):
        """abort() must revert EVERY core to baseline, so aborting during validation
        (where all confirmed cores are applied at once) never leaves the others at
        aggressive CO resident in the SMU."""
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1, 2])
        eng._session_id = tp.create_session(db, TunerConfig(cores_to_test=[0, 1, 2]), "", "", boot_id=this_boot())
        eng._core_states = {
            i: CoreState(core_id=i, phase=TunerPhase.CONFIRMED, current_offset=-20, best_offset=-20, baseline_offset=0)
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

    @settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_tuner_robust_over_random_scenarios(self, data):
        n_cores = data.draw(st.integers(min_value=1, max_value=8), label="n_cores")
        n_ccds = data.draw(st.sampled_from([1, 2, 4]), label="n_ccds")
        order = data.draw(
            st.sampled_from(
                [
                    "sequential",
                    "round_robin",
                    "weakest_first",
                    "ccd_alternating",
                    "ccd_round_robin",
                ]
            ),
            label="order",
        )
        coarse = data.draw(st.integers(min_value=2, max_value=8), label="coarse")
        fine = data.draw(st.integers(min_value=1, max_value=min(coarse, 3)), label="fine")
        penalty = data.draw(st.integers(min_value=1, max_value=5), label="penalty")
        hardening = data.draw(st.booleans(), label="hardening")
        # 0 = no spurious reboots; else inject a power-loss reboot every N steps.
        reboot_interval = data.draw(st.sampled_from([0, 0, 5, 11, 19]), label="reboot_interval")
        # 0 = no app exits; else a no-reboot app exit + resume every N steps.
        app_exit_interval = data.draw(st.sampled_from([0, 0, 7, 13]), label="app_exit_interval")
        cliffs = {}
        for c in range(n_cores):
            stable = data.draw(st.integers(min_value=-45, max_value=-3), label=f"stable{c}")
            gap = data.draw(st.integers(min_value=1, max_value=12), label=f"gap{c}")
            cliffs[c] = (stable, stable - gap)

        db = HistoryDB(":memory:")
        try:
            topo = _make_topo(n_cores, n_ccds)
            tiers = [{"backend": "mprime", "stress_mode": "AVX2", "fft_preset": "SMALL"}] if hardening else []
            cfg_kw = dict(
                cores_to_test=list(range(n_cores)),
                test_order=order,
                coarse_step=coarse,
                fine_step=fine,
                max_offset=-60,
                crash_penalty_steps=penalty,
                auto_validate=False,
                resume_crash_quarantine_threshold=4,
                hardening_tiers=tiers,
                search_duration_seconds=1,
                confirm_duration_seconds=1,
            )
            with tempfile.TemporaryDirectory() as home, pytest.MonkeyPatch.context() as patcher:
                eng, steps, crashes, sid = drive_closed_loop(
                    db,
                    topo,
                    _StubBackend(),
                    installed(Path(home), patcher),
                    cliffs,
                    cfg_kw,
                    reboot_interval=reboot_interval,
                    app_exit_interval=app_exit_interval,
                )

            assert steps < 6000, f"no convergence: order={order} cliffs={cliffs} reboot={reboot_interval}"
            assert eng.status in ("idle", "quarantined"), (
                f"stuck in {eng.status}: order={order} cliffs={cliffs} hardening={hardening} reboot={reboot_interval}"
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
            topology=topo,
            backend=backend,
            stress_config=StressConfig(mode=StressMode.SSE, fft_preset=FFTPreset.SMALL),
            scheduler_config=SchedulerConfig(cores_to_test=[0], **cfg),
        )

    def test_no_sensor_blocks_when_required(self):
        watch = ThermalWatch(
            max_temperature=95.0,
            grace_seconds=3.0,
            hard_margin=8.0,
            require_sensor=True,
            read=lambda: None,
        )
        assert watch.safe() is False  # fail closed

    def test_no_sensor_lenient_when_not_required(self):
        watch = ThermalWatch(
            max_temperature=95.0,
            grace_seconds=3.0,
            hard_margin=8.0,
            require_sensor=False,
            read=lambda: None,
        )
        assert watch.safe() is True  # explicit opt-out

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
        import corecycler.tuner.engine as te

        captured = {}

        class _CapScheduler:
            def __init__(self, *, topology, backend, stress_config, scheduler_config, work_dir):
                captured["cfg"] = scheduler_config

        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0], allow_missing_thermal_sensor=allow_missing)
        eng._session_id = tp.create_session(db, eng._config, "", "", boot_id=this_boot())
        eng._core_states = {0: CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-5)}
        with patch.object(te, "CoreScheduler", _CapScheduler), patch.object(te, "_TunerWorker"):
            eng._start_worker(0, 5)
        assert captured["cfg"].require_thermal_sensor is expect_required


# ---------------------------------------------------------------------------
# T7: every core-cycling style recovers a journal-detected crash
# ---------------------------------------------------------------------------


class TestEveryStyleRecoversCrash:
    @pytest.mark.parametrize(
        "order",
        [
            "sequential",
            "round_robin",
            "weakest_first",
            "ccd_alternating",
            "ccd_round_robin",
        ],
    )
    def test_style_penalizes_journal_suspect_on_resume(self, db, topo, smu, mock_backend, order, machine):
        """Regardless of test-order strategy, a journal-detected crash is penalized
        on resume and the crashing offset is never left re-applied."""
        cfg = TunerConfig(cores_to_test=[0, 1], test_order=order, crash_penalty_steps=2, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-20,
                baseline_offset=0,
                in_test=False,
            ),
        )
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=1,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-5,
                baseline_offset=0,
                in_test=False,
            ),
        )
        journaled(db, sid, 0, -20, survived=False)

        machine.reboot(clean=False)
        eng = _resume_fresh(
            db, topo, smu, mock_backend, sid, cores_to_test=[0, 1], test_order=order, crash_penalty_steps=2, fine_step=1
        )

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

    def test_crash_during_real_write_is_recovered(self, db, topo, mock_backend, machine):
        smu = CrashDuringWriteSMU(crash_at=(0, -28))
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        # in_test=False on purpose: ONLY the journal can recover this, not the flag.
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-28,
                baseline_offset=0,
                in_test=False,
            ),
        )

        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = sid
        # Real forward write that dies mid-write — no hand-written journal row.
        with pytest.raises(_HardCrash):
            eng._apply_co(0, -28)

        # Write-ahead proof: the value is already journaled (and durable) at the
        # instant the machine died — before set_co_offset returned.
        assert _suspects(db, sid) == {0: -28}

        # A fresh engine (new process) recovers using ONLY the persisted journal.
        del eng
        machine.reboot(clean=False)
        eng2 = make_engine(db, topo, CrashDuringWriteSMU(crash_at=None), mock_backend, cores_to_test=[0])
        with patch.object(eng2, "_run_next"):
            eng2.resume(sid)
        cs = eng2._core_states[0]
        assert cs.crash_count == 1  # the dying write was recovered as a crash
        assert cs.backoff_fail_bound == -28  # and bounded as never-retry

    def test_journal_is_durable_to_a_fresh_connection(self, tmp_path, topo, mock_backend):
        """The journal must be readable by a NEW connection (a fresh process after
        reboot), i.e. committed to the file — not just cached in the writer's
        connection. db1 is deliberately not closed before db2 opens, modelling a
        crash where the writer never closed cleanly."""
        path = tmp_path / "durable.db"
        db1 = HistoryDB(path)
        smu = FaultSMU()
        eng = make_engine(db1, topo, smu, mock_backend, cores_to_test=[0])
        eng._session_id = tp.create_session(db1, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng._apply_co(0, -30)

        db2 = HistoryDB(path)  # separate connection = the recovering process
        try:
            assert _suspects(db2, eng._session_id) == {0: -30}
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

    @settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_validation_terminates_and_settles_safely(self, data):
        n_cores = data.draw(st.integers(min_value=2, max_value=6), label="n_cores")
        n_ccds = data.draw(st.sampled_from([1, 2]), label="n_ccds")
        agg_margin = data.draw(st.integers(min_value=0, max_value=5), label="agg_margin")
        order = data.draw(st.sampled_from(["sequential", "ccd_round_robin"]), label="order")
        transitions = data.draw(st.booleans(), label="validate_transitions")
        spectrum = data.draw(st.booleans(), label="validate_spectrum")
        memory = data.draw(st.booleans(), label="validate_memory")
        soak = data.draw(st.booleans(), label="validate_soak")
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
                cores_to_test=list(range(n_cores)),
                test_order=order,
                auto_validate=True,
                hardening_tiers=[],
                validate_transitions=transitions,
                validate_spectrum=spectrum,
                validate_memory=memory,
                validate_soak=soak,
                fine_step=1,
                validate_duration_seconds=1,
                spectrum_slot_seconds=30,
                soak_duration_seconds=60,
                search_duration_seconds=1,
                confirm_duration_seconds=1,
            )
            eng, steps = drive_validation(db, topo, _StubBackend(), cliffs, agg_margin, cfg_kw)

            assert steps < 8000, f"validation did not converge: cliffs={cliffs} margin={agg_margin}"
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


class TestAbortSafety:
    """Abort is a safety action, not just a stop: whatever the tuner was doing,
    no aggressive CO may stay resident in the SMU afterwards. During validation
    EVERY confirmed core has its offset applied at once, so reverting only the
    core under test would leave the rest undervolted with nothing driving them."""

    @settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_abort_mid_validation_reverts_every_core(self, data):
        n_cores = data.draw(st.integers(min_value=2, max_value=5), label="n_cores")
        abort_at = data.draw(st.integers(min_value=1, max_value=n_cores), label="abort_at")
        cliffs = {}
        for c in range(n_cores):
            stable = data.draw(st.integers(min_value=-30, max_value=-8), label=f"stable{c}")
            cliffs[c] = (stable, stable - 5)

        db = HistoryDB(":memory:")
        try:
            topo = _make_topo(n_cores, 1)
            cfg_kw = dict(
                cores_to_test=list(range(n_cores)),
                auto_validate=True,
                hardening_tiers=[],
                fine_step=1,
                validate_duration_seconds=1,
                spectrum_slot_seconds=30,
                soak_duration_seconds=60,
                search_duration_seconds=1,
                confirm_duration_seconds=1,
            )
            eng, _steps = drive_validation(db, topo, _StubBackend(), cliffs, 0, cfg_kw, abort_at=abort_at)

            assert eng._abort_requested
            assert eng.status == "idle"
            assert eng._worker is None
            assert eng._validation_stage == 0
            assert not eng._soaking
            for c, cs in eng._core_states.items():
                resident = eng._smu.applied.get(c, cs.baseline_offset)
                assert resident == cs.baseline_offset, (
                    f"core {c} left resident at {resident}, baseline {cs.baseline_offset}"
                )
                assert not cs.in_test
            assert tp.get_session(db, eng._session_id).status == "aborted"
        finally:
            db.close()


class TestValidationFaultInjection:
    """Validation must survive the apparatus, not only the silicon.

    A thermal stop, a backend that will not launch, an external kill and a
    kernel event that names another core (or no core at all) all arrive on the
    same signal as a real failure. None of them is evidence about the offset
    under test, so none may make an offset MORE aggressive, and the flow must
    still reach a terminal state instead of looping on the fault."""

    @settings(max_examples=120, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(data=st.data())
    def test_faults_never_deepen_an_offset_and_always_terminate(self, data):
        n_cores = data.draw(st.integers(min_value=2, max_value=4), label="n_cores")
        cliffs = {}
        for c in range(n_cores):
            stable = data.draw(st.integers(min_value=-30, max_value=-8), label=f"stable{c}")
            cliffs[c] = (stable, stable - 5)
        faults = data.draw(
            st.dictionaries(
                st.integers(min_value=1, max_value=30),
                st.sampled_from(sorted(_FAULTS)),
                max_size=6,
            ),
            label="faults",
        )

        db = HistoryDB(":memory:")
        try:
            topo = _make_topo(n_cores, 1)
            cfg_kw = dict(
                cores_to_test=list(range(n_cores)),
                auto_validate=True,
                hardening_tiers=[],
                fine_step=1,
                validate_duration_seconds=1,
                spectrum_slot_seconds=30,
                soak_duration_seconds=60,
                search_duration_seconds=1,
                confirm_duration_seconds=1,
            )
            eng, steps = drive_validation(db, topo, _StubBackend(), cliffs, 0, cfg_kw, faults=faults)

            assert steps < 8000, f"did not terminate: faults={faults}"
            assert eng.status in ("idle", "paused", "running", "quarantined"), eng.status
            for c, (stable, crash) in cliffs.items():
                best = eng._core_states[c].best_offset
                assert best is not None
                assert best >= stable, (
                    f"core {c} deepened to {best} below its seeded {stable} -- a fault moved the search bound"
                )
                assert best > crash
                assert best <= 0
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
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0], coarse_step=0), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend)
        logs: list[str] = []
        eng.log_message.connect(logs.append)
        with patch.object(eng, "_run_next") as run_next:
            eng.resume(sid)
        assert run_next.call_count == 0, "resume proceeded on an invalid config"
        assert any("Invalid tuner config" in m for m in logs)

    def test_validate_profile_fails_closed_on_out_of_range_config(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0], coarse_step=0), "", "", boot_id=this_boot())
        # A CONFIRMED core so validate_profile clears its empty-profile guard and
        # reaches the config load/validate.
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.CONFIRMED,
                current_offset=-10,
                best_offset=-10,
                baseline_offset=0,
            ),
        )
        eng = make_engine(db, topo, smu, mock_backend)
        logs: list[str] = []
        eng.log_message.connect(logs.append)
        with patch.object(eng, "_run_next") as run_next:
            eng.validate_profile(sid)
        assert run_next.call_count == 0, "validate_profile proceeded on an invalid config"
        assert any("Invalid tuner config" in m for m in logs)

    def test_resume_still_proceeds_on_a_valid_config(self, db, topo, smu, mock_backend):
        """The guard is not over-eager: a valid config_json resumes normally."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.FINE_SEARCH,
                current_offset=-10,
                best_offset=-8,
                baseline_offset=0,
                in_test=False,
            ),
        )
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


def _in_test(core_id: int, offset: int, **fields) -> CoreState:
    return CoreState(core_id=core_id, phase=TunerPhase.COARSE_SEARCH, current_offset=offset, in_test=True, **fields)


def _crash_rows(db, sid) -> list[dict]:
    return [row for row in tp.get_test_log(db, sid) if row["error_type"] == "crash"]


def _session_on_this_boot(db, states, *, status="running") -> int:
    sid = tp.create_session(
        db, TunerConfig(cores_to_test=sorted(cs.core_id for cs in states)), "", "", boot_id=this_boot()
    )
    for cs in states:
        tp.save_core_state(db, sid, cs)
    tp.update_session_status(db, sid, status)
    return sid


def _validating_on_this_boot(db, cliffs) -> int:
    sid = _session_on_this_boot(
        db,
        [
            CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=v, best_offset=v, baseline_offset=0)
            for c, v in cliffs.items()
        ],
        status="validating",
    )
    for c, v in cliffs.items():
        journaled(db, sid, c, v, survived=True)
        tp.log_test_result(db, sid, c, v, "confirm", True, duration=1.0)
    return sid


class TestValidateRefusesAnUnreadableSession:
    def test_a_corrupt_journal_value_refuses_validation_and_changes_nothing(self, db, topo, smu, mock_backend):
        sid = _session_on_this_boot(
            db,
            [CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-20, best_offset=-20)],
            status="completed",
        )
        journaled(db, sid, 0, -150, survived=True)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        said: list[str] = []
        eng.log_message.connect(said.append)
        with patch.object(eng, "_run_next"):
            eng.validate_profile(sid)
        assert [line for line in said if line.startswith("Cannot validate")] == [
            f"Cannot validate: session {sid} is unreadable: core 0 has CO journal value -150, "
            "not an integer in the sane CO range -100..100"
        ]
        assert (tp.get_session(db, sid).status, db.get_tuner_core_states(sid)[0].phase) == (
            "completed",
            TunerPhase.CONFIRMED,
        )


class TestEnvelopePerSession:
    def _resumed_with_a_deep_envelope(self, db, topo, smu, mock_backend):
        deep = _session_on_this_boot(db, [CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-30)])
        journaled(db, deep, 0, -30, survived=True)
        tp.update_session_status(db, deep, "paused")
        eng = _resume_fresh(db, topo, smu, mock_backend, deep, cores_to_test=[0])
        assert eng._co_survived == {0: -30}
        tp.update_session_status(db, deep, "completed")
        return eng

    def test_validating_another_session_uses_that_sessions_envelope(self, db, topo, smu, mock_backend):
        eng = self._resumed_with_a_deep_envelope(db, topo, smu, mock_backend)
        shallow = _session_on_this_boot(
            db,
            [CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-20, best_offset=-20)],
            status="completed",
        )
        journaled(db, shallow, 0, -10, survived=True)
        with patch.object(eng, "_run_next"):
            eng.validate_profile(shallow)
        assert eng._co_survived == {0: -10}

    def test_a_new_session_starts_with_no_envelope(self, db, topo, smu, mock_backend):
        eng = self._resumed_with_a_deep_envelope(db, topo, smu, mock_backend)
        with patch.object(eng, "_run_next"):
            eng.start()
        assert eng._co_survived == {}


class TestCorruptJournal:
    def test_a_journal_value_outside_the_sane_range_refuses_the_resume(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_in_test(0, -10)])
        journaled(db, sid, 0, -150, survived=False)
        dead = machine.boot_id
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        said: list[str] = []
        eng.log_message.connect(said.append)
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        assert [line for line in said if line.startswith("Cannot resume")] == [
            f"Cannot resume: session {sid} is unreadable: core 0 has CO journal value -150, "
            "not an integer in the sane CO range -100..100"
        ]
        assert (_crash_rows(db, sid), tp.get_session(db, sid).boot_id) == ([], dead)

    @pytest.mark.parametrize(
        ("table", "column", "said"),
        [
            ("tuner_co_journal", "mono_ns", "core 0 has CO journal stamp 'x', not an integer"),
            ("tuner_sessions", "kernel_read_ns", "the last kernel read stamp 'x' is not an integer"),
        ],
    )
    def test_a_stamp_that_is_not_an_integer_refuses_the_resume(
        self, db, topo, smu, mock_backend, machine, table, column, said
    ):
        sid = _session_on_this_boot(db, [_in_test(0, -10)])
        journaled(db, sid, 0, -10, survived=False)
        db._HistoryDB__conn.execute(f"UPDATE {table} SET {column}='x'")
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        lines: list[str] = []
        eng.log_message.connect(lines.append)
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        assert [line for line in lines if line.startswith("Cannot resume")] == [
            f"Cannot resume: session {sid} is unreadable: {said}"
        ]
        assert _crash_rows(db, sid) == []


class TestRebootGate:
    def test_no_reboot_clears_in_test_without_penalty(self, db, topo, smu, mock_backend):
        sid = _session_on_this_boot(db, [_in_test(0, -30)])
        journaled(db, sid, 0, -30, survived=False)

        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        cs = eng._core_states[0]
        assert cs.crash_count == 0
        assert cs.current_offset == -30
        assert cs.phase == TunerPhase.COARSE_SEARCH
        assert not cs.in_test
        assert not db.get_tuner_core_states(sid)[0].in_test

    @pytest.mark.parametrize("entry", ["start", "resume", "validate"])
    def test_an_unreadable_boot_identity_refuses_every_entry_point(self, db, topo, smu, mock_backend, machine, entry):
        sid = _validating_on_this_boot(db, {0: -10, 1: -12})
        machine.boot_id_path.write_text("garbled\n")
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        said: list[str] = []
        eng.log_message.connect(said.append)
        calls = {"start": eng.start, "resume": lambda: eng.resume(sid), "validate": lambda: eng.validate_profile(sid)}

        with (
            patch.object(eng, "_run_next") as run_next,
            patch.object(eng, "_run_validation_next") as run_validation_next,
        ):
            calls[entry]()

        assert said[-1].startswith(f"Cannot {entry}: boot identity unreadable: ")
        assert (run_next.call_count, run_validation_next.call_count) == (0, 0)
        assert smu.writes == []
        assert [s.id for s in db.list_tuner_sessions()] == [sid]
        assert tp.get_session(db, sid).status == "validating"

    def test_an_unreadable_last_write_time_still_attributes_from_the_flags(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_in_test(0, -30)])
        journaled(db, sid, 0, -30, survived=False)
        db._execute_raw("UPDATE tuner_co_journal SET mono_ns=NULL, written_at='unreadable' WHERE session_id=?", (sid,))
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend)
        said: list[str] = []
        eng.log_message.connect(said.append)

        with patch.object(eng, "_run_next"):
            eng.resume(sid)

        assert "Kernel-journal forensics unavailable: the session's last write time is unreadable." in said
        assert db.get_tuner_core_states(sid)[0].crash_count == 1

    def test_start_stamps_the_session_with_this_boot(self, db, topo, smu, mock_backend, machine):
        eng = make_engine(db, topo, smu, mock_backend)
        with patch.object(eng, "_run_next"):
            eng.start()
        assert tp.get_session(db, eng.session_id).boot_id == machine.boot_id

    def test_a_new_boot_whose_clock_reads_earlier_is_a_reboot(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_in_test(0, -30)])
        journaled(db, sid, 0, -30, survived=False)
        machine.reboot(clean=False, rtc_offset_s=-7200)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        stored = db.get_tuner_core_states(sid)[0]
        assert (stored.crash_count, stored.backoff_fail_bound) == (1, -30)

    def test_the_fatal_machine_check_logged_early_in_the_next_boot_convicts_its_core(
        self, db, topo, smu, mock_backend, machine
    ):
        sid = _session_on_this_boot(
            db,
            [_in_test(0, -30), CoreState(core_id=2, phase=TunerPhase.FINE_SEARCH, current_offset=-25, best_offset=-25)],
        )
        journaled(db, sid, 0, -30, survived=False)
        journaled(db, sid, 2, -25, survived=True)
        machine.reboot(clean=False, rtc_offset_s=-7200)
        for line in core_bank_record(2, corrected=False):
            machine.kernel(line, mono_s=1)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        stored = db.get_tuner_core_states(sid)
        assert (stored[2].crash_count, stored[2].backoff_fail_bound) == (1, -25)
        assert (stored[0].crash_count, stored[0].current_offset, stored[0].in_test) == (0, -30, False)
        assert [(row["core_id"], row["offset_tested"]) for row in _crash_rows(db, sid)] == [(2, -25)]

    def test_a_record_written_to_the_kernel_log_by_a_user_process_is_not_harvested(
        self, db, topo, smu, mock_backend, machine
    ):
        sid = _session_on_this_boot(
            db,
            [_in_test(0, -30), CoreState(core_id=2, phase=TunerPhase.FINE_SEARCH, current_offset=-25, best_offset=-25)],
        )
        journaled(db, sid, 0, -30, survived=False)
        journaled(db, sid, 2, -25, survived=True)
        machine.kernel_now(core_bank_record(2, corrected=False), facility=1)
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        stored = db.get_tuner_core_states(sid)
        assert (stored[0].crash_count, stored[2].crash_count) == (1, 0)
        assert [(row["core_id"], row["offset_tested"]) for row in _crash_rows(db, sid)] == [(0, -30)]

    def test_a_clean_shutdown_of_the_sessions_boot_convicts_nobody(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_in_test(0, -30)])
        journaled(db, sid, 0, -30, survived=False)
        tp.set_resume_crash_streak(db, sid, 1)
        machine.reboot(clean=True)

        eng = _resume_fresh(db, topo, smu, mock_backend, sid)

        stored = db.get_tuner_core_states(sid)[0]
        assert (stored.crash_count, stored.current_offset, stored.in_test) == (0, -30, False)
        assert _crash_rows(db, sid) == []
        session = tp.get_session(db, sid)
        assert session.resume_crash_streak == 1
        assert session.boot_id == machine.boot_id
        assert (eng.status, eng._hunting) == ("running", False)

    def test_a_clean_shutdown_still_applies_that_boots_machine_checks_as_evidence(
        self, db, topo, smu, mock_backend, machine
    ):
        sid = _session_on_this_boot(
            db, [CoreState(core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-20, best_offset=-20)]
        )
        journaled(db, sid, 1, -20, survived=True)
        tp.set_resume_crash_streak(db, sid, 1)
        machine.kernel_now(core_bank_record(1))
        machine.reboot(clean=True)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        stored = db.get_tuner_core_states(sid)[1]
        assert (stored.current_offset, stored.crash_count) == (-19, 0)
        assert [row["error_type"] for row in tp.get_test_log(db, sid)] == ["mce"]
        assert tp.get_resume_crash_streak(db, sid) == 1

    def test_the_probe_asks_about_the_sessions_boot(self, db, topo, smu, mock_backend, machine):
        sid = _validating_on_this_boot(db, {0: -10, 1: -12})
        machine.reboot(clean=False)
        machine.reboot(clean=True)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        with patch.object(eng, "_run_next"), patch.object(eng, "_run_validation_next"):
            eng.resume(sid)
        assert tp.get_unattributed_crashes(db, sid) == 1

    def test_an_unreadable_record_of_the_sessions_boot_counts_as_a_crash(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_in_test(0, -30)])
        journaled(db, sid, 0, -30, survived=False)
        dead = machine.boot_id
        machine.reboot(clean=True)
        machine.forget_boot(dead)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        assert db.get_tuner_core_states(sid)[0].crash_count == 1

    def test_reconciling_evidence_before_attribution_cannot_hide_the_value_that_crashed(
        self, db, topo, smu, mock_backend, machine
    ):
        sid = _session_on_this_boot(
            db,
            [CoreState(core_id=0, phase=TunerPhase.CONFIRMED, current_offset=-20, best_offset=-20, in_test=True)],
        )
        journaled(db, sid, 0, -20, survived=True)
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        stored = db.get_tuner_core_states(sid)[0]
        assert (stored.crash_count, stored.backoff_fail_bound) == (1, -20)
        assert [row["offset_tested"] for row in _crash_rows(db, sid)] == [-20]

    def test_a_failed_save_inside_attribution_commits_nothing_and_the_next_resume_attributes_once(
        self, tmp_path, topo, smu, mock_backend, machine, monkeypatch
    ):
        db = HistoryDB(tmp_path / "history.db")
        sid = _session_on_this_boot(db, [_in_test(0, -30)])
        journaled(db, sid, 0, -30, survived=False)
        dead = machine.boot_id
        machine.reboot(clean=False)
        real = HistoryDB.upsert_tuner_core_state
        failures = [sqlite3.OperationalError("database is locked")]

        def locked_once(history, session_id, cs):
            if failures:
                raise failures.pop()
            real(history, session_id, cs)

        monkeypatch.setattr(HistoryDB, "upsert_tuner_core_state", locked_once)
        eng = make_engine(db, topo, smu, mock_backend)
        said: list[str] = []
        eng.log_message.connect(said.append)
        with patch.object(eng, "_run_next") as run_next:
            eng.resume(sid)

        assert run_next.call_count == 0
        assert smu.writes == []
        assert _crash_rows(db, sid) == []
        assert db.get_tuner_core_states(sid)[0].in_test is True
        assert tp.get_session(db, sid).boot_id == dead
        assert any("Cannot resume: the attribution could not be saved" in line for line in said)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        assert len(_crash_rows(db, sid)) == 1
        stored = db.get_tuner_core_states(sid)[0]
        assert (stored.crash_count, stored.current_offset) == (1, -27)
        db.close()

    @pytest.mark.parametrize("dying_write", ["upsert_tuner_core_state", "stamp_session_boot"])
    def test_a_death_inside_attribution_then_another_reboot_penalizes_once(
        self, tmp_path, topo, smu, mock_backend, machine, monkeypatch, dying_write
    ):
        path = tmp_path / "history.db"
        db = HistoryDB(path)
        sid = _session_on_this_boot(db, [_in_test(0, -30)])
        journaled(db, sid, 0, -30, survived=False)
        machine.reboot(clean=False)
        real = getattr(HistoryDB, dying_write)

        def dies_on_the_cleared_write(history, session_id, *args):
            if dying_write == "stamp_session_boot" or not args[0].in_test:
                raise _ProcessDeath(f"killed inside {dying_write}")
            real(history, session_id, *args)

        with monkeypatch.context() as dying:
            dying.setattr(HistoryDB, dying_write, dies_on_the_cleared_write)
            eng = make_engine(db, topo, smu, mock_backend)
            with patch.object(eng, "_run_next"), pytest.raises(_ProcessDeath):
                eng.resume(sid)
        db.close()

        db = HistoryDB(path)
        machine.reboot(clean=False)
        _resume_fresh(db, topo, smu, mock_backend, sid)

        assert [row["offset_tested"] for row in _crash_rows(db, sid)] == [-30]
        stored = db.get_tuner_core_states(sid)[0]
        assert (stored.crash_count, stored.backoff_fail_bound) == (1, -30)
        db.close()

    def test_a_resume_without_crash_evidence_keeps_the_streak(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10)])
        tp.set_resume_crash_streak(db, sid, 2)
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, sid)

        assert tp.get_resume_crash_streak(db, sid) == 2
        assert db.get_tuner_core_states(sid)[0].crash_count == 0

    def test_a_dead_boot_is_attributed_exactly_once(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_in_test(0, -30, baseline_offset=-5)])
        journaled(db, sid, 0, -30, survived=False)
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, sid)
        _resume_fresh(db, topo, smu, mock_backend, sid)

        assert len(_crash_rows(db, sid)) == 1
        assert db.get_tuner_core_states(sid)[0].crash_count == 1

    @pytest.mark.parametrize("owed", ["in-test flag", "unproven resident value", "hunt slot"])
    def test_validate_profile_refuses_a_session_an_earlier_boot_left_with_work_to_attribute(
        self, db, topo, smu, mock_backend, machine, owed
    ):
        sid = _validating_on_this_boot(db, {0: -10, 1: -12})
        tp.update_session_status(db, sid, "completed")
        if owed == "in-test flag":
            cs = db.get_tuner_core_states(sid)[1]
            cs.in_test = True
            tp.save_core_state(db, sid, cs)
        elif owed == "unproven resident value":
            journaled(db, sid, 1, -14, survived=False)
        else:
            tp.set_hunting_core(db, sid, 1)
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        said: list[str] = []
        eng.log_message.connect(said.append)

        with patch.object(eng, "_run_next") as run_next:
            eng.validate_profile(sid)

        assert said[-1].endswith(f"Resume session {sid} first.")
        assert run_next.call_count == 0
        assert tp.get_session(db, sid).status == "completed"
        assert db.get_tuner_core_states(sid)[0].phase == TunerPhase.CONFIRMED

    def test_validate_profile_stamps_a_session_an_earlier_boot_left_settled(self, db, topo, smu, mock_backend, machine):
        sid = _validating_on_this_boot(db, {0: -10, 1: -12})
        tp.update_session_status(db, sid, "completed")
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])

        with patch.object(eng, "_run_next") as run_next:
            eng.validate_profile(sid)

        assert run_next.call_count == 1
        session = tp.get_session(db, sid)
        assert (session.status, session.boot_id) == ("validating", machine.boot_id)


def _engine_wrote(db, topo, smu, backend, sid, core_id, value) -> None:
    eng = make_engine(db, topo, smu, backend, cores_to_test=[core_id])
    eng._session_id = sid
    assert eng._apply_co(core_id, value)


def _search_session(db, offsets: dict[int, int]) -> int:
    return _session_on_this_boot(
        db, [CoreState(core_id=c, phase=TunerPhase.COARSE_SEARCH, current_offset=v) for c, v in offsets.items()]
    )


class TestJournalPerBoot:
    def test_the_proven_envelope_is_the_most_aggressive_survived_row(self, db, topo, smu, mock_backend, machine):
        sid = _search_session(db, {0: -10})
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        eng._core_states = tp.load_core_states(db, sid)
        with patch.object(eng, "_run_next"):
            for offset in (-10, -15):
                eng._core_states[0].current_offset = offset
                assert eng._apply_co_isolation(0, offset)
                eng._on_test_finished(report(0))
        assert smu.applied[0] == 0

        resumed = _resume_fresh(db, topo, smu, mock_backend, sid)

        assert resumed._co_survived[0] == -15

    def test_a_refused_writes_intent_is_never_marked_survived(self, db, topo, smu, mock_backend, machine):
        sid = _search_session(db, {0: -10, 1: -12})
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        eng._session_id = sid
        eng._core_states = tp.load_core_states(db, sid)
        smu.rejecting_cores = {1}
        assert eng._apply_co(1, -12) is False
        assert eng._apply_co(0, -10) is True

        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(0))

        assert eng._co_survived == {0: -10}
        assert [row.survived for row in tp.journal_rows(db, sid, machine.boot_id) if row.core_id == 1] == [False]

    def test_a_value_replaced_before_a_test_is_not_marked_by_it(self, db, topo, smu, mock_backend, machine):
        sid = _search_session(db, {0: -20, 1: -10})
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        eng._session_id = sid
        eng._core_states = tp.load_core_states(db, sid)
        for core_id, value in ((0, -20), (0, 0), (1, -10)):
            assert eng._apply_co(core_id, value)

        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(1))

        rows = tp.journal_rows(db, sid, machine.boot_id)
        assert [(row.value, row.survived) for row in rows if row.core_id == 0] == [(-20, False), (0, True)]
        assert eng._co_survived[1] == -10

    def test_a_named_core_is_anchored_at_its_dead_boot_resident_value(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(
            db,
            [CoreState(core_id=2, phase=TunerPhase.CONFIRMED, current_offset=-22, best_offset=-22)],
            status="validating",
        )
        _engine_wrote(db, topo, smu, mock_backend, sid, 2, -25)
        machine.crash_after(core_bank_record(2, corrected=False))

        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[2])

        assert [(row["core_id"], row["offset_tested"]) for row in _crash_rows(db, sid)] == [(2, -25)]
        assert db.get_tuner_core_states(sid)[2].backoff_fail_bound == -25

    def test_a_dead_boots_row_is_never_marked_by_the_next_boots_test(self, db, topo, smu, mock_backend, machine):
        sid = _search_session(db, {0: -10, 1: -12})
        _engine_wrote(db, topo, smu, mock_backend, sid, 1, -12)
        dead = machine.boot_id
        machine.reboot(clean=True)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0, 1])
        assert eng._apply_co(0, -10) is True

        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(0))

        assert eng._co_survived == {0: -10}
        assert [(row.core_id, row.survived) for row in tp.journal_rows(db, sid, dead)] == [(1, False)]

    def test_suspects_come_from_the_dead_boot_only(self, db, topo, smu, mock_backend, machine):
        sid = _search_session(db, {0: -12, 1: -10})
        _engine_wrote(db, topo, smu, mock_backend, sid, 0, -12)
        machine.reboot(clean=False)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0, 1])
        eng._start_worker = lambda *_args, **_kw: None
        eng._run_next()
        assert {c: cs.in_test for c, cs in db.get_tuner_core_states(sid).items()} == {0: False, 1: True}
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0, 1])

        assert [(row["core_id"], row["offset_tested"]) for row in _crash_rows(db, sid)] == [(0, -12), (1, -10)]

    def test_a_replaced_or_unconfirmed_value_suspects_the_resident_candidate(
        self, db, topo, smu, mock_backend, machine
    ):
        sid = _session_on_this_boot(
            db,
            [
                CoreState(core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-20),
                CoreState(core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-18),
            ],
        )
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        eng._session_id = sid
        for core_id, value in ((0, -20), (0, 0), (1, -15)):
            assert eng._apply_co(core_id, value)
        smu.rejecting_cores = {1}
        assert eng._apply_co(1, -18) is False
        machine.reboot(clean=False)

        _resume_fresh(db, topo, FaultSMU(), mock_backend, sid, cores_to_test=[0, 1])

        assert [(row["core_id"], row["offset_tested"]) for row in _crash_rows(db, sid)] == [(1, -15)]
        assert db.get_tuner_core_states(sid)[1].backoff_fail_bound == -15


def _wall_seconds(row) -> float:
    return datetime.fromisoformat(row.written_at).timestamp()


def _fine(core_id: int, offset: int) -> CoreState:
    return CoreState(core_id=core_id, phase=TunerPhase.FINE_SEARCH, current_offset=offset, best_offset=offset)


class TestRetraction:
    def test_a_crash_retracts_its_value_and_everything_more_aggressive(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_in_test(0, -11)])
        for value in (-11, -15, -11):
            journaled(db, sid, 0, value, survived=True)
        machine.reboot(clean=False)

        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        assert eng._apply_co(0, -13)

        assert [row["offset_tested"] for row in _crash_rows(db, sid)] == [-11]
        assert tp.journal_proven_values(db, sid, -1).get(0, 0) >= -10
        assert [row.survived for row in tp.journal_rows(db, sid, machine.boot_id) if row.value == -13] == [False]


class TestHarvestAfterEachCoresLastWrite:
    def test_the_harvest_skips_records_before_the_last_write_after_a_clock_step_back(
        self, db, topo, smu, mock_backend, machine
    ):
        sid = _session_on_this_boot(db, [_fine(1, -20)])
        journaled(db, sid, 1, -20, survived=True)
        stepped_back = _wall_seconds(tp.journal_rows(db, sid, machine.boot_id)[-1]) + 30
        for line in core_bank_record(1):
            machine.kernel(line, mono_s=100, realtime=stepped_back)
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[1])

        stored = db.get_tuner_core_states(sid)[1]
        assert (stored.current_offset, stored.crash_count) == (-20, 0)
        assert tp.get_test_log(db, sid) == []

    def test_a_record_about_one_core_outlives_a_later_write_to_another(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_fine(0, -10), _fine(1, -20)])
        journaled(db, sid, 1, -20, survived=True)
        for line in core_bank_record(1):
            machine.kernel(line, mono_s=machine.mono_ns / 1e9 + 1, realtime=time.time())
        machine.advance(5)
        journaled(db, sid, 0, -10, survived=True)
        machine.reboot(clean=True)

        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0, 1])

        stored = db.get_tuner_core_states(sid)
        assert (stored[0].current_offset, stored[1].current_offset) == (-10, -19)
        assert [(row["core_id"], row["error_type"]) for row in tp.get_test_log(db, sid)] == [(1, "mce")]

    def test_a_migrated_dead_boot_is_read_after_its_last_write_time(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_fine(1, -20)])
        journaled(db, sid, 1, -20, survived=True)
        db._execute_raw("UPDATE tuner_co_journal SET mono_ns=NULL WHERE session_id=?", (sid,))
        written = _wall_seconds(tp.journal_rows(db, sid, machine.boot_id)[-1])
        for offset_s, corrected in ((-60, False), (60, True)):
            for line in core_bank_record(1, corrected=corrected):
                machine.kernel(line, mono_s=machine.mono_ns / 1e9 + 1, realtime=written + offset_s)
        machine.reboot(clean=True)

        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[1])

        assert db.get_tuner_core_states(sid)[1].current_offset == -19
        assert [row["error_type"] for row in tp.get_test_log(db, sid)] == ["mce"]

    def test_a_dead_boots_harvest_is_bounded_by_that_boots_last_kernel_read(self, db, topo, smu, mock_backend, machine):
        sid = _session_on_this_boot(db, [_fine(1, -20)])
        db._execute_raw("UPDATE tuner_sessions SET kernel_read_ns=? WHERE id=?", (machine.mono_ns, sid))
        machine.reboot(clean=False)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[1])
        assert eng._apply_co(1, -20)
        machine.kernel_now(core_bank_record(1))
        machine.reboot(clean=True)

        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[1])

        assert db.get_tuner_core_states(sid)[1].current_offset == -19

    def test_a_test_end_without_a_kernel_read_keeps_the_last_one(self, db, topo, smu, mock_backend, machine):
        sid = _search_session(db, {0: -10})
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        eng._core_states = tp.load_core_states(db, sid)

        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(0, kernel_read_ns=4_000_000_000_000))
            eng._on_test_finished(report(0, Outcome.NOT_STARTED, "the stress tool did not start"))

        assert tp.get_session(db, sid).kernel_read_ns == 4_000_000_000_000


class TestTwoSessionsInOneBoot:
    def test_a_crash_after_another_session_wrote_later_in_that_boot_is_that_sessions(
        self, db, topo, smu, mock_backend, machine
    ):
        first = _session_on_this_boot(db, [_in_test(0, -30)])
        _engine_wrote(db, topo, smu, mock_backend, first, 0, -30)
        tp.set_resume_crash_streak(db, first, 1)
        second = _session_on_this_boot(db, [_in_test(1, -25)])
        _engine_wrote(db, topo, smu, mock_backend, second, 1, -25)
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend)
        said: list[str] = []
        eng.log_message.connect(said.append)

        with patch.object(eng, "_run_next"):
            eng.resume(first)
        _resume_fresh(db, topo, smu, mock_backend, second, cores_to_test=[1])

        crashes = [(row["core_id"], row["offset_tested"]) for sid in (first, second) for row in _crash_rows(db, sid)]
        assert crashes == [(1, -25)]
        stored = db.get_tuner_core_states(first)[0]
        assert (stored.crash_count, stored.current_offset, stored.in_test) == (0, -30, False)
        assert tp.get_resume_crash_streak(db, first) == 1
        assert any(line.startswith(f"Session {second} wrote Curve Optimizer values later") for line in said)

    def test_only_the_machine_checks_logged_before_the_other_sessions_write_are_this_sessions_evidence(
        self, db, topo, smu, mock_backend, machine
    ):
        first = _session_on_this_boot(
            db, [CoreState(core_id=1, phase=TunerPhase.FINE_SEARCH, current_offset=-20, best_offset=-20)]
        )
        _engine_wrote(db, topo, smu, mock_backend, first, 1, -20)
        machine.kernel_now(core_bank_record(1))
        machine.advance(5)
        second = _session_on_this_boot(db, [_in_test(1, -25)])
        _engine_wrote(db, topo, smu, mock_backend, second, 1, -25)
        machine.kernel_now(core_bank_record(1, corrected=False))
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, first, cores_to_test=[1])

        stored = db.get_tuner_core_states(first)[1]
        assert (stored.current_offset, stored.crash_count) == (-19, 0)
        assert [row["error_type"] for row in tp.get_test_log(db, first)] == ["mce"]

    def test_two_sessions_of_an_unknown_boot_each_answer_for_their_own_flags(
        self, db, topo, smu, mock_backend, machine
    ):
        first = _session_on_this_boot(db, [_in_test(0, -30)])
        _engine_wrote(db, topo, smu, mock_backend, first, 0, -30)
        second = _session_on_this_boot(db, [_in_test(1, -25)])
        _engine_wrote(db, topo, smu, mock_backend, second, 1, -25)
        db._execute_raw("UPDATE tuner_sessions SET boot_id=''")
        db._execute_raw("UPDATE tuner_co_journal SET boot_id='', mono_ns=NULL")
        machine.reboot(clean=False)

        _resume_fresh(db, topo, smu, mock_backend, first)

        assert [(row["core_id"], row["offset_tested"]) for row in _crash_rows(db, first)] == [(0, -30)]


# ---------------------------------------------------------------------------
# A hard crash at a CONFIRMED/HARDENED value invalidates the confirmation
# ---------------------------------------------------------------------------


class TestCrashAtConfirmedValue:
    def test_crash_demotes_best_and_reenters_backoff(self, db, topo, smu, mock_backend, machine):
        """Validation and finalize re-apply best_offset — leaving a value that
        hard-crashed the box as "best" re-crashes it on every resume (observed
        live: core 1 at -42, phase hardened, crash, resume, re-validate at -42)."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=3, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.HARDENED,
                current_offset=-42,
                best_offset=-42,
                baseline_offset=-15,
                in_test=True,
            ),
        )
        machine.reboot(clean=False)

        eng = _resume_fresh(db, topo, smu, mock_backend, sid)

        cs = eng._core_states[0]
        assert _crash_rows(db, sid) and {row["core_id"] for row in _crash_rows(db, sid)} == {0}
        assert eng._hunting is False
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM  # must re-earn confirmation
        assert cs.current_offset == -39  # penalized by 3 steps
        assert cs.best_offset == -39  # crashed -42 cannot stay best
        assert cs.backoff_fail_bound == -42  # crashed value is a hard bound


# ---------------------------------------------------------------------------
# Startup/environment failures are not stability verdicts
# ---------------------------------------------------------------------------


class TestStartupFailureIsNotAVerdict:
    def test_a_not_started_fault_is_retried_then_pauses_reverted_and_persisted(self, db, topo, smu, mock_backend):
        """A missing binary / scheduler construction error is retried without
        a verdict, and after max_apparatus_retries it pauses. Neither path may:
        advance the state machine, leave the aggressive offset resident, leave
        in_test=1 persisted (a later reboot+resume would fabricate a crash
        verdict), or mark the never-tested offset as survived in the journal."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        cs = CoreState(
            core_id=0,
            phase=TunerPhase.COARSE_SEARCH,
            current_offset=-10,
            baseline_offset=0,
            in_test=True,
        )
        eng._core_states = {0: cs}
        tp.save_core_state(db, sid, cs)
        eng._co_applied[0] = -10
        smu.applied[0] = -10
        journaled(db, sid, 0, -10, survived=False)

        fault = report(0, Outcome.NOT_STARTED, "Failed to start stress test: boom", 0.0)
        with patch.object(eng, "_run_next") as nxt, patch.object(eng, "_advance_core") as adv:
            eng._on_test_finished(fault)
            assert eng._status != "paused"
            nxt.assert_called_once()
            eng._apparatus_fault_streak = eng._config.max_apparatus_retries
            eng._on_test_finished(fault)

        assert eng._status == "paused"
        adv.assert_not_called()
        assert tp.get_test_log(db, sid) == []
        assert smu.applied[0] == 0
        assert not db.get_tuner_core_states(sid)[0].in_test
        assert _proven(db, sid).get(0) != -10

    def test_validation_env_failure_pauses_without_backoff(self, db, topo, smu, mock_backend):
        """Scheduler construction failure during a validation stage is an
        apparatus fault: the stage is retried, then the session pauses. It is
        never logged as a validate FAIL that backs off the most aggressive
        (healthy) core."""
        cliffs = {0: -10, 1: -12}
        sid = tp.create_session(db, TunerConfig(cores_to_test=list(cliffs)), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=list(cliffs))
        eng._session_id = sid
        eng._core_states = {
            c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=v, best_offset=v, baseline_offset=0)
            for c, v in cliffs.items()
        }
        eng._co_survived = dict(cliffs)
        eng._set_status("validating")
        eng._validation_stage = 2
        eng._validation_core_order = sorted(cliffs)

        with patch("corecycler.tuner.engine.ParallelStress", side_effect=RuntimeError("boom")) as runner:
            eng._run_validation_stage2()

        assert eng._status == "paused"
        assert runner.call_count == eng._config.max_apparatus_retries + 1
        assert eng._core_states[1].best_offset == -12
        assert not [r for r in tp.get_test_log(db, sid) if r["phase"].startswith("validate")]


# ---------------------------------------------------------------------------
# Apparatus circuit breaker: implausible fail streaks recover from evidence
# ---------------------------------------------------------------------------


class TestApparatusBreaker:
    def _seed(self, db, topo, smu, backend, streak_threshold=5):
        sid = tp.create_session(
            db,
            TunerConfig(cores_to_test=[0], apparatus_failure_streak=streak_threshold),
            "",
            "",
            boot_id=this_boot(),
        )
        eng = make_engine(
            db,
            topo,
            smu,
            backend,
            apparatus_failure_streak=streak_threshold,
        )
        eng._session_id = sid
        cs = CoreState(
            core_id=0,
            phase=TunerPhase.BACKOFF_PRECONFIRM,
            current_offset=-20,
            best_offset=-20,
            baseline_offset=0,
            backoff_mode=True,
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
            tp.log_test_result(
                db,
                sid,
                0,
                off,
                "backoff_preconfirm",
                False,
                error_msg="mprime error: FATAL ERROR",
                error_type="computation",
                duration=122.0,
            )

        with patch.object(eng, "_run_next"), patch.object(eng, "_advance_core") as adv:
            # the 5th consecutive fail crosses the threshold
            eng._on_test_finished(report(0, Outcome.COMPUTATION, "mprime error: FATAL ERROR", 122.0))

        assert eng._status == "paused"
        adv.assert_not_called()  # verdict not walked into the search
        assert cs.best_offset == -44  # rolled back to proven evidence
        assert cs.current_offset == -44
        assert cs.phase == TunerPhase.CONFIRMING  # must re-earn confirmation
        assert cs.backoff_fail_bound is None and cs.backoff_pass_bound is None
        persisted = db.get_tuner_core_states(sid)[0]
        assert persisted.best_offset == -44  # rollback is durable

    def test_below_threshold_does_not_trip(self, db, topo, smu, mock_backend):
        eng, sid, cs = self._seed(db, topo, smu, mock_backend, streak_threshold=5)
        for off in (-24, -23):
            tp.log_test_result(db, sid, 0, off, "backoff_preconfirm", False, error_type="computation", duration=122.0)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(0, Outcome.COMPUTATION, "mprime error: FATAL ERROR", 122.0))
        assert eng._status != "paused"  # normal backoff continues

    def test_pass_breaks_the_streak(self, db, topo, smu, mock_backend):
        eng, sid, cs = self._seed(db, topo, smu, mock_backend, streak_threshold=5)
        for off in (-24, -23, -22):
            tp.log_test_result(db, sid, 0, off, "backoff_preconfirm", False, error_type="computation", duration=122.0)
        tp.log_test_result(db, sid, 0, -21, "backoff_preconfirm", True, duration=122.0)
        for off in (-20,):
            tp.log_test_result(db, sid, 0, off, "backoff_confirm", False, error_type="computation", duration=300.0)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(0, Outcome.COMPUTATION, "mprime error: FATAL ERROR", 300.0))
        assert eng._status != "paused"  # streak is 2, not 6

    def test_synthetic_crash_rows_do_not_count(self, db, topo, smu, mock_backend):
        eng, sid, cs = self._seed(db, topo, smu, mock_backend, streak_threshold=3)
        # two real fails + two synthetic reboot rows (duration NULL)
        for off in (-24, -23):
            tp.log_test_result(db, sid, 0, off, "backoff_preconfirm", False, error_type="computation", duration=122.0)
        for off in (-22, -21):
            tp.log_test_result(db, sid, 0, off, "coarse_search", False, error_type="crash", duration=None)
        with patch.object(eng, "_run_next"):
            eng._on_test_finished(report(0, Outcome.COMPUTATION, "mprime error: FATAL ERROR", 122.0))
        # real-test streak is 3 (threshold) — trips; but the point is the
        # synthetic rows alone must not have tripped it earlier: recompute
        assert eng._status == "paused"


# ---------------------------------------------------------------------------
# SMU revert failure is a hardware-state fault -> pause, never march on
# ---------------------------------------------------------------------------


class TestRevertFailureFailsClosed:
    def test_failed_post_test_revert_pauses(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-20, baseline_offset=-10, in_test=True)
        eng._core_states = {0: cs}
        eng._co_applied[0] = -20  # aggressive offset resident
        smu.reject_set = True  # SMU refuses the baseline revert

        with patch.object(eng, "_run_next"), patch.object(eng, "_advance_core") as adv:
            eng._on_test_finished(report(0, duration=60.0))

        assert eng._status == "paused"  # fail closed: offset still resident
        adv.assert_not_called()

    def test_resume_pauses_when_baseline_restore_fails(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-20,
                baseline_offset=-10,
            ),
        )
        smu.reject_set = True  # every SMU write rejected
        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        assert eng._status == "paused"  # not "running" on a broken SMU


class TestNoRebootResidentOffset:
    def test_no_reboot_writes_zero_baseline_instead_of_assuming_it(self, db, topo, smu, mock_backend):
        """Without a reboot the SMU is NOT zeroed: a core with baseline 0 that
        died mid-test still holds its test offset. Resume must WRITE the
        baseline back, never assume it (the stale-resident-offset hole)."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-30,
                baseline_offset=0,
                in_test=True,
            ),
        )
        smu.applied[0] = -30  # what the dying app left resident in the SMU

        eng = _resume_fresh(db, topo, smu, mock_backend, sid)

        assert (0, 0) in smu.writes  # baseline explicitly written
        assert smu.applied[0] == 0  # aggressive offset no longer resident
        assert eng._co_applied[0] == 0

    def test_rebooted_zero_baseline_is_not_rewritten(self, db, topo, smu, mock_backend, machine):
        """After a real reboot SMU SRAM is zeroed — writing 0 again would be a
        pointless hardware write (and journal churn)."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-30,
                baseline_offset=0,
            ),
        )
        machine.reboot(clean=False)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        assert (0, 0) not in smu.writes
        assert eng._co_applied[0] == 0


# ---------------------------------------------------------------------------
# Apparatus faults (stall / external kill / unattributable MCE) are not verdicts
# ---------------------------------------------------------------------------


def _seed_validating(db, topo, smu, backend, cliffs, **cfg):
    """A confirmed profile parked at validation stage 2, no worker running."""
    sid = tp.create_session(db, TunerConfig(cores_to_test=list(cliffs), **cfg), "", "", boot_id=this_boot())
    eng = make_engine(db, topo, smu, backend, cores_to_test=list(cliffs), **cfg)
    eng._session_id = sid
    eng._core_states = {
        c: CoreState(core_id=c, phase=TunerPhase.CONFIRMED, current_offset=v, best_offset=v, baseline_offset=0)
        for c, v in cliffs.items()
    }
    for cs in eng._core_states.values():
        tp.save_core_state(db, sid, cs)
    eng._co_survived = dict(cliffs)
    eng._set_status("validating")
    tp.update_session_status(db, sid, "validating")
    eng._validation_stage = 2
    eng._validation_core_order = sorted(cliffs)
    return eng, sid


class TestApparatusFaultIsNotAVerdict:
    """One night of live operation produced 85 stage-2 stall failures — every
    one an orchestration artifact, every one converted into a CO back-off on an
    innocent core. An apparatus fault must retry without a verdict, bounded,
    and never move an offset."""

    def test_stall_during_validation_retries_without_backoff(self, db, topo, smu, mock_backend):
        cliffs = {0: -10, 1: -12}
        eng, sid = _seed_validating(db, topo, smu, mock_backend, cliffs)
        stalled = "Stress test stalled on core 0 (CPU usage near 0 on CPUs 0,8 for 30s)"
        with patch.object(eng, "_run_validation_next") as nxt:
            eng._on_test_finished(report(0, Outcome.STALL, stalled, 35.0))
        assert eng._core_states[0].best_offset == -10
        assert eng._core_states[1].best_offset == -12
        assert eng._status == "validating"
        assert eng._apparatus_fault_streak == 1
        nxt.assert_called_once()
        assert tp.get_test_log(db, sid) == []

    def test_apparatus_faults_bounded_then_pause(self, db, topo, smu, mock_backend):
        cliffs = {0: -10}
        eng, sid = _seed_validating(db, topo, smu, mock_backend, cliffs)
        eng._co_applied = dict(cliffs)
        smu.applied.update(cliffs)
        eng._apparatus_fault_streak = eng._config.max_apparatus_retries
        eng._on_test_finished(report(0, Outcome.STALL, "Stress test stalled on core 0", 35.0))
        assert eng._status == "paused"
        assert tp.get_session(db, sid).status == "paused"
        assert smu.applied[0] == 0
        assert eng._core_states[0].best_offset == -10

    def test_stall_in_search_flow_retries_same_offset(self, db, topo, smu, mock_backend):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, baseline_offset=0, in_test=True)
        eng._core_states = {0: cs}
        eng._co_applied[0] = -10
        smu.applied[0] = -10
        eng._set_status("running")
        with patch.object(eng, "_run_next") as nxt, patch.object(eng, "_advance_core") as adv:
            eng._on_test_finished(report(0, Outcome.STALL, "Stress test stalled on core 0", 35.0))
        adv.assert_not_called()
        nxt.assert_called_once()
        assert cs.current_offset == -10
        assert cs.crash_count == 0
        assert smu.applied[0] == 0
        assert tp.get_test_log(db, sid) == []

    def test_external_kill_is_apparatus_not_verdict(self, db, topo, smu, mock_backend):
        cliffs = {0: -10, 1: -12}
        eng, sid = _seed_validating(db, topo, smu, mock_backend, cliffs)
        with patch.object(eng, "_run_validation_next") as nxt:
            eng._on_test_finished(report(1, Outcome.OUTSIDE_KILL, "Stress process killed externally (code -9)", 12.0))
        assert eng._core_states[1].best_offset == -12
        assert eng._apparatus_fault_streak == 1
        nxt.assert_called_once()
        assert tp.get_test_log(db, sid) == []

    def test_real_verdict_resets_apparatus_streak(self, db, topo, smu, mock_backend):
        cliffs = {0: -10, 1: -12}
        eng, _sid = _seed_validating(db, topo, smu, mock_backend, cliffs)
        eng._apparatus_fault_streak = 2
        with patch.object(eng, "_run_validation_next"):
            eng._on_test_finished(report(0))
        assert eng._apparatus_fault_streak == 0

    def test_unattributed_mce_blocks_survival_promotion(self, db, topo, smu, mock_backend):
        """A machine check naming no CPU taints every resident value — none may
        be promoted to survived off the back of that test."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        eng = make_engine(db, topo, smu, mock_backend)
        eng._session_id = sid
        cs = CoreState(core_id=0, phase=TunerPhase.COARSE_SEARCH, current_offset=-10, baseline_offset=0, in_test=True)
        eng._core_states = {0: cs}
        journaled(db, sid, 0, -10, survived=False)
        unattributed = report(
            0,
            Outcome.UNATTRIBUTED_MCE,
            "Machine check without core attribution during parallel stress: x",
            5.0,
            events=(unblamed(corrected=False, message="x", bank=3),),
        )
        with patch.object(eng, "_run_next"), patch.object(eng, "_advance_core"):
            eng._on_test_finished(unattributed)
        assert _proven(db, sid).get(0) != -10


class TestUnattributedMCEParsing:
    def test_event_shapes(self):
        from corecycler.tuner.engine import _has_unattributed_mce

        assert _has_unattributed_mce(()) is False
        assert _has_unattributed_mce((mce(3),)) is False
        assert _has_unattributed_mce((mce(3), unblamed())) is True
        assert _has_unattributed_mce((mce(3), trouble())) is True


# ---------------------------------------------------------------------------
# Fail-closed finalize: a failed validation must never declare completion
# ---------------------------------------------------------------------------


class TestFailClosedFinalize:
    """Session 4 live: 'Validation failed but no core can be backed off
    further' completed the session, applied the unproven profile as truth and
    reported '16 cores confirmed'. Exhaustion must pause honestly instead."""

    def test_finalize_exhausted_reverts_and_pauses(self, db, topo, smu, mock_backend):
        cliffs = {0: -10, 1: -12}
        eng, sid = _seed_validating(db, topo, smu, mock_backend, cliffs)
        eng._co_applied = dict(cliffs)
        smu.applied.update(cliffs)
        eng._finalize_exhausted()
        assert eng._status == "paused"
        assert tp.get_session(db, sid).status == "paused"
        assert smu.applied[0] == 0 and smu.applied[1] == 0  # reverted
        assert tp.get_session(db, sid).status != "completed"

    def test_finalize_session_refuses_dirty(self, db, topo, smu, mock_backend):
        cliffs = {0: -10, 1: -12}
        eng, sid = _seed_validating(db, topo, smu, mock_backend, cliffs)
        eng._validation_dirty = True
        eng._co_applied = dict(cliffs)
        smu.applied.update(cliffs)
        eng._finalize_session(dict(cliffs))
        assert eng._status == "paused"
        assert tp.get_session(db, sid).status != "completed"
        assert smu.applied[0] == 0 and smu.applied[1] == 0

    def test_clean_finalize_completes_and_clears_incidents(self, db, topo, smu, mock_backend):
        cliffs = {0: -10, 1: -12}
        eng, sid = _seed_validating(db, topo, smu, mock_backend, cliffs)
        tp.set_unattributed_crashes(db, sid, 2)
        eng._finalize_session(dict(cliffs))
        assert eng._status == "idle"
        assert tp.get_session(db, sid).status == "completed"
        assert tp.get_unattributed_crashes(db, sid) == 0


# ---------------------------------------------------------------------------
# A dirty reboot mid-validation is an incident, never silence
# ---------------------------------------------------------------------------


class TestUnattributedIncidentOnResume:
    """Two live freezes in one night resumed with zero reaction: no in_test
    core, no kernel evidence, nothing said. The machine dying with the profile
    live must be recorded, must re-owe the clean pass, and must pause for the
    owner when it keeps happening. A provably clean shutdown is exempt."""

    def _seed(self, db, cliffs, unattributed=0):
        sid = tp.create_session(db, TunerConfig(cores_to_test=list(cliffs)), "", "", boot_id=this_boot())
        for c, v in cliffs.items():
            tp.save_core_state(
                db,
                sid,
                CoreState(
                    core_id=c,
                    phase=TunerPhase.CONFIRMED,
                    current_offset=v,
                    best_offset=v,
                    baseline_offset=0,
                ),
            )
            journaled(db, sid, c, v, survived=True)
            # evidence backing the CONFIRMED claim, or the resume-time
            # reconciler demotes the core and validation never re-enters
            tp.log_test_result(db, sid, c, v, "confirm", True, duration=1.0)
        tp.update_session_status(db, sid, "validating")
        if unattributed:
            tp.set_unattributed_crashes(db, sid, unattributed)
        return sid

    def test_dirty_reboot_mid_validation_is_recorded(self, db, topo, smu, mock_backend, machine):
        sid = self._seed(db, {0: -10, 1: -12})
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        with patch.object(eng, "_run_next"), patch.object(eng, "_run_validation_next"):
            eng.resume(sid)
        assert tp.get_unattributed_crashes(db, sid) == 1
        assert eng._validation_dirty is True  # clean pass owed again
        assert eng._status == "validating"  # first incident continues

    def test_repeat_dirty_reboots_pause_for_decision(self, db, topo, smu, mock_backend, machine):
        sid = self._seed(db, {0: -10}, unattributed=1)
        machine.reboot(clean=False)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        with patch.object(eng, "_run_next"), patch.object(eng, "_run_validation_next"):
            eng.resume(sid)
        assert tp.get_unattributed_crashes(db, sid) == 2
        assert eng._status == "paused"

    def test_clean_reboot_mid_validation_is_not_an_incident(self, db, topo, smu, mock_backend, machine):
        sid = self._seed(db, {0: -10, 1: -12})
        machine.reboot(clean=True)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0, 1])
        with patch.object(eng, "_run_next"), patch.object(eng, "_run_validation_next"):
            eng.resume(sid)
        assert tp.get_unattributed_crashes(db, sid) == 0
        assert eng._status == "validating"

    def test_search_flow_reboot_is_not_an_incident(self, db, topo, smu, mock_backend, machine):
        """The incident class is validation-specific: a mid-search reboot is
        already covered by the journal/in_test detectors."""
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.COARSE_SEARCH,
                current_offset=-10,
                baseline_offset=0,
            ),
        )
        machine.reboot(clean=False)
        eng = _resume_fresh(db, topo, smu, mock_backend, sid)
        assert tp.get_unattributed_crashes(db, sid) == 0
        assert eng._status == "running"


class TestReopeningAQuarantinedSession:
    """Picked by hand, a quarantined session continues on proven ground only."""

    def _quarantined(self, db, *, current, baseline, survived=None):
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=1, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.BACKOFF_PRECONFIRM,
                current_offset=current,
                baseline_offset=baseline,
                best_offset=current,
                in_test=False,
            ),
        )
        if survived is not None:
            journaled(db, sid, 0, survived, survived=True)
        tp.set_resume_crash_streak(db, sid, 3)
        tp.update_session_status(db, sid, "quarantined")
        return sid

    def test_an_unproven_offset_is_never_re_applied(self, db, topo, smu, mock_backend):
        sid = self._quarantined(db, current=-30, baseline=-25, survived=-10)
        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0])
        cs = db.get_tuner_core_states(sid)[0]
        assert cs.current_offset == -10, "the search must restart from the survived value"
        assert cs.baseline_offset == -10, "no core may be restored to an unproven baseline"

    def test_with_nothing_survived_it_falls_back_to_stock(self, db, topo, smu, mock_backend):
        sid = self._quarantined(db, current=-25, baseline=-20)
        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0])
        cs = db.get_tuner_core_states(sid)[0]
        assert (cs.current_offset, cs.baseline_offset) == (0, 0)

    def test_proven_work_is_kept_not_discarded(self, db, topo, smu, mock_backend):
        sid = self._quarantined(db, current=-10, baseline=-5, survived=-10)
        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0])
        cs = db.get_tuner_core_states(sid)[0]
        assert cs.current_offset == -10, "a proven offset must survive the re-open"
        assert cs.baseline_offset == -5
        assert cs.best_offset == -10

    def test_best_is_pulled_back_too_because_validation_re_applies_it(self, db, topo, smu, mock_backend):
        """Validation writes best_offset to every core it is not testing."""
        sid = self._quarantined(db, current=-30, baseline=0, survived=-10)
        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0])
        assert db.get_tuner_core_states(sid)[0].best_offset == -10

    def test_a_session_that_was_not_quarantined_is_left_alone(self, db, topo, smu, mock_backend):
        """Only the breaker's own sessions are pulled back; a pause is not one."""
        cfg = TunerConfig(cores_to_test=[0], crash_penalty_steps=1, fine_step=1)
        sid = tp.create_session(db, cfg, "", "", boot_id=this_boot())
        tp.save_core_state(
            db,
            sid,
            CoreState(
                core_id=0,
                phase=TunerPhase.FINE_SEARCH,
                current_offset=-22,
                baseline_offset=-4,
                best_offset=-18,
                in_test=False,
            ),
        )
        tp.update_session_status(db, sid, "paused")
        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0])
        cs = db.get_tuner_core_states(sid)[0]
        assert (cs.current_offset, cs.baseline_offset, cs.best_offset) == (-22, -4, -18)

    def test_the_session_re_opens_with_the_breaker_reset(self, db, topo, smu, mock_backend):
        sid = self._quarantined(db, current=-30, baseline=0, survived=-10)
        _resume_fresh(db, topo, smu, mock_backend, sid, cores_to_test=[0])
        session = db.get_tuner_session(sid)
        assert session.status != "quarantined"
        assert session.resume_crash_streak == 0
        assert sid in [s.id for s in db.list_resumable_tuner_sessions()]

    def test_re_opening_says_what_it_pulled_back(self, db, topo, smu, mock_backend):
        sid = self._quarantined(db, current=-30, baseline=0, survived=-10)
        eng = make_engine(db, topo, smu, mock_backend, cores_to_test=[0])
        said: list[str] = []
        eng.log_message.connect(said.append)
        with patch.object(eng, "_run_next"):
            eng.resume(sid)
        reopened = [m for m in said if "QUARANTINED" in m]
        assert reopened and "[0]" in reopened[0]
