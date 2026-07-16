"""Evidence-based crash attribution, the isolated crash hunt, and cross-core
MCE evidence. The replay scenario: a validation freeze leaves every core
in_test and the whole CO journal survived while kernel MCEs name the culprits.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.detector import MCEEvent
from history.db import HistoryDB
from tuner import persistence as tp
from tuner.config import TunerConfig
from tuner.engine import TunerEngine, _pick_report
from tuner.state import CoreState, TunerPhase

# Real kernel MCE line shape (kernel: prefix stripped).
CPU5_CORRECTED = (
    "[Hardware Error]: CPU:{cpu} (1a:44:0) "
    "MC0_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: 0xdc204000000d0175"
)


@pytest.fixture
def db(tmp_path):
    d = HistoryDB(tmp_path / "test.db")
    yield d
    d.close()


class FakeSMU:
    def __init__(self):
        self.written: dict[int, int] = {}
        self.commands = SimpleNamespace(co_range=(-60, 30))

    def set_co_offset(self, core_id: int, value: int) -> bool:
        self.written[core_id] = value
        return True

    def get_co_offset(self, core_id: int) -> int:
        return self.written.get(core_id, 0)


def _event(cpu: int, corrected: bool = True) -> MCEEvent:
    return MCEEvent(
        timestamp=0.0,
        cpu=cpu,
        bank=0,
        message=CPU5_CORRECTED.format(cpu=cpu),
        corrected=corrected,
        raw_ts=float(cpu),
    )


def _make_engine(db, topo, mock_backend, **cfg_kwargs):
    defaults = dict(
        coarse_step=5, fine_step=1, crash_penalty_steps=3,
        cores_to_test=sorted(topo.cores),
    )
    defaults.update(cfg_kwargs)
    cfg = TunerConfig(**defaults)
    eng = TunerEngine(
        db=db, topology=topo, smu=FakeSMU(), backend=mock_backend, config=cfg,
    )
    eng._session_id = tp.create_session(db, cfg, "", "")
    return eng


def _seed_hardened_validating(eng, db, best: dict[int, int], baselines: dict[int, int]):
    """Seed a validation freeze's persisted shape: every core HARDENED and
    in_test (a validation stage was stressing the whole set when the box
    froze), every journal row survived (validation re-applies proven values).
    """
    sid = eng._session_id
    eng._core_states = {}
    for core_id, offset in best.items():
        cs = CoreState(
            core_id=core_id, phase=TunerPhase.HARDENED,
            current_offset=offset, best_offset=offset,
            baseline_offset=baselines[core_id], in_test=True,
        )
        eng._core_states[core_id] = cs
        tp.save_core_state(db, sid, cs)
        db.journal_co_intent(sid, core_id, offset, survived=True)
    db.update_tuner_session_status(sid, "validating")
    return tp.get_session(db, sid)


BEST = {0: -41, 1: -37, 2: -36, 3: -43, 4: -42, 5: -30, 6: -41, 7: -50}
BASELINES = {c: (-15 if c < 4 else -6) for c in BEST}


class TestForensicAttribution:
    """Kernel-journal evidence names the culprits; policy guesses never run."""

    def test_replay_field_incident_penalizes_kernel_named_cores(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_hardened_validating(eng, db, BEST, BASELINES)
        # The kernel named cores 5 and 6 (corrected LS MCEs) before the freeze.
        eng._forensics = lambda since, timeout=15.0: (
            [_event(5), _event(5), _event(6)], True,
        )

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == [5, 6]
        assert pending_hunt is False
        # The named cores took the penalty, anchored at their resident values.
        assert eng._core_states[5].backoff_fail_bound == -30
        assert eng._core_states[6].backoff_fail_bound == -41
        assert eng._core_states[5].phase == TunerPhase.BACKOFF_PRECONFIRM
        # The deepest-undervolt core is untouched.
        assert eng._core_states[7].crash_count == 0
        assert eng._core_states[7].best_offset == -50
        assert eng._core_states[7].phase == TunerPhase.HARDENED
        assert all(not cs.in_test for cs in eng._core_states.values())

    def test_sibling_cpu_maps_to_its_physical_core(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        """An MCE on the second SMT sibling is evidence about the same core."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_hardened_validating(eng, db, BEST, BASELINES)
        sibling = topo_dual_ccd_x3d.cores[3].logical_cpus[1]
        eng._forensics = lambda since, timeout=15.0: ([_event(sibling)], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == [3]
        assert pending_hunt is False

    def test_unattributable_events_do_not_penalize(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        """A kernel panic line with no CPU proves a crash, names no core."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_hardened_validating(eng, db, BEST, BASELINES)
        eng._forensics = lambda since, timeout=15.0: ([_event(-1)], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True  # fall through to the hunt

    def test_no_forensics_multi_core_requests_hunt_not_guess(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_hardened_validating(eng, db, BEST, BASELINES)
        eng._forensics = lambda since, timeout=15.0: ([], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True
        assert all(cs.crash_count == 0 for cs in eng._core_states.values())
        assert all(not cs.in_test for cs in eng._core_states.values())

    def test_forensics_unavailable_fails_closed_to_hunt(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        """journalctl missing is NOT a clean bill — the hunt still runs."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        session = _seed_hardened_validating(eng, db, BEST, BASELINES)
        eng._forensics = lambda since, timeout=15.0: ([], False)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == []
        assert pending_hunt is True

    def test_persisted_hunt_slot_is_proof_by_isolation(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        """A crash while one core was hunted alone (others at stock) convicts it."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        db.set_hunting_core(eng._session_id, 5)
        db.set_unattributed_crashes(eng._session_id, 1)
        session = tp.get_session(db, eng._session_id)
        eng._forensics = lambda since, timeout=15.0: ([], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == [5]
        assert pending_hunt is False
        assert eng._core_states[5].crash_count == 1
        assert tp.get_unattributed_crashes(db, eng._session_id) == 0
        assert tp.get_session(db, eng._session_id).hunting_core is None

    def test_single_in_test_search_flow_keeps_direct_blame(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        """Isolation mode: one core away from baseline — attribution is sound."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        cs = CoreState(
            core_id=2, phase=TunerPhase.COARSE_SEARCH,
            current_offset=-20, in_test=True,
        )
        eng._core_states = {2: cs}
        tp.save_core_state(db, eng._session_id, cs)
        session = tp.get_session(db, eng._session_id)  # status: running
        eng._forensics = lambda since, timeout=15.0: ([], True)

        crashed, pending_hunt = eng._attribute_crash_after_reboot(session)

        assert crashed == [2]
        assert pending_hunt is False
        assert cs.crash_count == 1


class TestCrashHunt:
    def test_hunt_orders_by_suspicion_and_isolates_first_suspect(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
        # Core 5 has prior kernel-error evidence -> hunted first.
        tp.log_test_result(
            db, eng._session_id, 5, -30, "mce_evidence", passed=False,
            error_msg="prior", error_type="mce", duration=None,
        )

        eng._start_worker = lambda *a, **k: None  # no real worker threads
        eng._start_hunt()

        assert eng._hunt_queue[0:0] == []  # first suspect already popped
        assert eng.status == "hunting"
        # First slot: core 5 at its tuned value, every other core at stock.
        assert eng._smu.written[5] == -30
        assert all(eng._smu.written[c] == 0 for c in BEST if c != 5)
        assert eng._core_states[5].in_test is True
        assert tp.get_session(db, eng._session_id).hunting_core == 5

    def test_fruitless_hunt_counts_and_pauses_at_threshold(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(
            db, topo_dual_ccd_x3d, mock_backend, max_unattributed_crash_hunts=2,
        )
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
        db.update_tuner_session_status(eng._session_id, "validating")
        eng._start_worker = lambda *a, **k: None  # no real worker threads

        eng._hunting = True
        eng._hunt_queue = []
        eng._end_hunt_fruitless()
        assert tp.get_unattributed_crashes(db, eng._session_id) == 1
        assert eng.status != "paused"  # first fruitless hunt resumes validation

        eng._hunting = True
        eng._hunt_queue = []
        eng._end_hunt_fruitless()
        assert tp.get_unattributed_crashes(db, eng._session_id) == 2
        assert eng.status == "paused"  # threshold reached — user's call now

    def test_hunt_slot_failure_convicts_core_and_resets_counter(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
        db.set_unattributed_crashes(eng._session_id, 1)
        eng._hunting = True
        eng._co_applied[5] = -30

        eng._on_hunt_slot_finished(5, passed=False, error_type="mce", foreign={})

        cs = eng._core_states[5]
        assert cs.backoff_fail_bound == -30
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.crash_count == 0  # evidence-grade: nothing crashed
        assert tp.get_unattributed_crashes(db, eng._session_id) == 0
        assert eng._hunting is False


class TestForeignMceEvidence:
    def test_parse_groups_by_core_and_severity(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        payload = json.dumps([
            {"cpu": 5, "bank": 0, "corrected": True, "message": "a", "raw_ts": 1.0},
            {"cpu": 5, "bank": 0, "corrected": False, "message": "b", "raw_ts": 2.0},
            {"cpu": 6, "bank": 0, "corrected": True, "message": "c", "raw_ts": 3.0},
            {"cpu": 2, "bank": 0, "corrected": True, "message": "own", "raw_ts": 4.0},
            {"cpu": -1, "bank": -1, "corrected": False, "message": "panic", "raw_ts": 5.0},
        ])

        foreign = eng._foreign_mce_by_core(tested_core=2, mce_json=payload)

        assert sorted(foreign) == [5, 6]
        assert foreign[5]["corrected"] is False  # any uncorrected wins
        assert foreign[6]["corrected"] is True

    def test_malformed_payload_is_no_evidence(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        assert eng._foreign_mce_by_core(0, "not json") == {}
        assert eng._foreign_mce_by_core(0, "") == {}
        assert eng._foreign_mce_by_core(0, json.dumps({"cpu": 1})) == {}

    def test_corrected_evidence_backs_off_one_step_and_reearns(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, fine_step=1)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        eng._co_applied[5] = -30

        eng._apply_foreign_evidence({5: {"corrected": True, "messages": ["m"]}})

        cs = eng._core_states[5]
        assert cs.backoff_fail_bound == -30
        assert cs.current_offset == -29  # exactly one fine step
        assert cs.best_offset == -29
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM  # must re-earn
        assert cs.crash_count == 0  # a warning, not a crash
        rows = tp.get_test_log(db, eng._session_id, core_id=5)
        assert any(r["phase"] == "mce_evidence" and r["error_type"] == "mce" for r in rows)

    def test_uncorrected_evidence_gets_crash_grade_penalty(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        eng = _make_engine(
            db, topo_dual_ccd_x3d, mock_backend, fine_step=1, crash_penalty_steps=3,
        )
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        eng._co_applied[6] = -41

        eng._apply_foreign_evidence({6: {"corrected": False, "messages": ["m"]}})

        cs = eng._core_states[6]
        assert cs.backoff_fail_bound == -41
        assert cs.current_offset == -38  # three steps
        assert cs.crash_count == 1

    def test_error_at_stock_changes_no_state(
        self, db, topo_dual_ccd_x3d, mock_backend
    ):
        """An MCE at CO=0 is not a Curve Optimizer problem — never walk zero."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        eng._co_applied[4] = 0
        before = eng._core_states[4].best_offset

        eng._apply_foreign_evidence({4: {"corrected": True, "messages": ["m"]}})

        cs = eng._core_states[4]
        assert cs.best_offset == before
        assert cs.phase == TunerPhase.HARDENED
        rows = tp.get_test_log(db, eng._session_id, core_id=4)
        assert any(r["phase"] == "mce_evidence" for r in rows)  # recorded, loudly

    def test_survival_marking_excludes_named_cores(self, db):
        sid = db.create_tuner_session("{}", "1.0", "TestCPU")
        db.journal_co_intent(sid, 0, -20, survived=False)
        db.journal_co_intent(sid, 5, -30, survived=False)

        db.journal_mark_survived(sid, exclude_cores=[5])

        survived = db.journal_survived_values(sid)
        assert 0 in survived
        assert 5 not in survived
        assert (5, -30) in db.journal_suspects(sid)


class TestPickReport:
    def _result(self, core_id: int, passed: bool):
        return SimpleNamespace(core_id=core_id, passed=passed)

    def test_primary_pass_with_no_other_failures(self):
        results = {0: [self._result(0, True)], 1: [self._result(1, True)]}
        core, report = _pick_report(results, primary=0)
        assert core == 0
        assert report.passed is True

    def test_failure_on_any_core_outranks_primary_pass(self):
        """A failure on any core in the batch must outrank the primary's pass."""
        results = {
            0: [self._result(0, True)],
            5: [self._result(5, False)],
            7: [self._result(7, True)],
        }
        core, report = _pick_report(results, primary=0)
        assert core == 5
        assert report.passed is False

    def test_primary_failure_is_reported_directly(self):
        results = {0: [self._result(0, False)], 5: [self._result(5, False)]}
        core, report = _pick_report(results, primary=0)
        assert core == 0

    def test_no_results_returns_none(self):
        core, report = _pick_report({}, primary=3)
        assert core == 3
        assert report is None


class TestDriftAgainstJournal:
    def test_no_false_drift_when_smu_holds_tuner_written_values(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        """Reopening mid-validation: the SMU holding exactly what the tuner
        wrote (the confirmed offsets) is not drift."""
        import tuner.engine as engine_mod

        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
            tp.save_core_state(db, eng._session_id, cs)
        for core_id, offset in BEST.items():
            eng._smu.written[core_id] = offset  # SMU == tuner's last write
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *a, **k: False)

        eng._start_worker = lambda *a, **k: None  # no real worker threads
        drift_reports: list[str] = []
        eng.co_drift_detected.connect(drift_reports.append)
        eng.resume(eng._session_id)

        assert drift_reports == []

    def test_third_party_change_is_reported_against_last_write(
        self, db, topo_dual_ccd_x3d, mock_backend, monkeypatch
    ):
        import tuner.engine as engine_mod

        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
            tp.save_core_state(db, eng._session_id, cs)
        for core_id, offset in BEST.items():
            eng._smu.written[core_id] = offset
        eng._smu.written[3] = -10  # someone changed core 3 behind our back
        monkeypatch.setattr(engine_mod, "_rebooted_since", lambda *a, **k: False)

        eng._start_worker = lambda *a, **k: None  # no real worker threads
        drift_reports: list[str] = []
        eng.co_drift_detected.connect(drift_reports.append)
        eng.resume(eng._session_id)

        assert len(drift_reports) == 1
        drift = json.loads(drift_reports[0])
        assert drift == {"3": {"expected": BEST[3], "actual": -10}}
