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

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from engine.backends.base import FFTPreset, StressConfig, StressMode
from engine.scheduler import CoreScheduler, SchedulerConfig
from history.db import HistoryDB
from tuner import persistence as tp
from tuner.config import TunerConfig
from tuner.engine import TunerEngine
from tuner.state import CoreState, TunerPhase


# ---------------------------------------------------------------------------
# Fault-injectable fake SMU
# ---------------------------------------------------------------------------


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
