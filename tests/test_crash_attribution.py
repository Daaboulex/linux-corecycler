"""Evidence-based crash attribution, the isolated crash hunt, and cross-core
MCE evidence. The replay scenario: a validation freeze leaves every core
in_test and the whole CO journal survived while kernel MCEs name the culprits.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.detector import CoreMachineCheck, KernelTrouble, UnblamedMachineCheck
from corecycler.history.db import HistoryDB
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.engine import TunerEngine, WorkerReport
from corecycler.tuner.state import CoreState, TunerPhase
from tests._machine import core_bank_record, journaled, this_boot
from tests._reports import mce, report

# Real kernel MCE line shape (kernel: prefix stripped).
CPU5_CORRECTED = (
    "[Hardware Error]: CPU:{cpu} (1a:44:0) MC0_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: 0xdc204000000d0175"
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


def _make_engine(db, topo, mock_backend, **cfg_kwargs):
    defaults = dict(
        coarse_step=5,
        fine_step=1,
        crash_penalty_steps=3,
        cores_to_test=sorted(topo.cores),
    )
    defaults.update(cfg_kwargs)
    cfg = TunerConfig(**defaults)
    eng = TunerEngine(
        db=db,
        topology=topo,
        smu=FakeSMU(),
        backend=mock_backend,
        config=cfg,
    )
    eng._session_id = tp.create_session(db, cfg, "", "", boot_id=this_boot())
    return eng


def _resumed(db, topo, mock_backend, sid) -> tuple[TunerEngine, list[str], list[bool]]:
    eng = TunerEngine(db=db, topology=topo, smu=FakeSMU(), backend=mock_backend, config=TunerConfig())
    said: list[str] = []
    hunts: list[bool] = []
    eng.log_message.connect(said.append)
    eng._start_hunt = lambda: hunts.append(True)
    eng._run_next = lambda: None
    eng._run_validation_next = lambda: None
    eng.resume(sid)
    return eng, said, hunts


def _seed_hardened_validating(eng, db, best: dict[int, int], baselines: dict[int, int]):
    """Seed a validation freeze's persisted shape: every core HARDENED and
    in_test (a validation stage was stressing the whole set when the box
    froze), every journal row survived (validation re-applies proven values).
    """
    sid = eng._session_id
    eng._core_states = {}
    for core_id, offset in best.items():
        cs = CoreState(
            core_id=core_id,
            phase=TunerPhase.HARDENED,
            current_offset=offset,
            best_offset=offset,
            baseline_offset=baselines[core_id],
            in_test=True,
        )
        eng._core_states[core_id] = cs
        tp.save_core_state(db, sid, cs)
        journaled(db, sid, core_id, offset, survived=True)
    db.update_tuner_session_status(sid, "validating")
    return tp.get_session(db, sid)


BEST = {0: -41, 1: -37, 2: -36, 3: -43, 4: -42, 5: -30, 6: -41, 7: -50}
BASELINES = {c: (-15 if c < 4 else -6) for c in BEST}


def _seed_proven_validating(eng, db):
    _seed_hardened_validating(eng, db, BEST, BASELINES)
    for core_id, offset in BEST.items():
        tp.log_test_result(db, eng._session_id, core_id, offset, "confirm", True, duration=1.0)
    return eng._session_id


def _crash_rows(db, sid) -> list[tuple[int, int]]:
    return [(row["core_id"], row["offset_tested"]) for row in tp.get_test_log(db, sid) if row["error_type"] == "crash"]


class TestForensicAttribution:
    def test_replay_field_incident_penalizes_kernel_named_cores(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        sid = _seed_proven_validating(_make_engine(db, topo_dual_ccd_x3d, mock_backend), db)
        machine.crash_after(core_bank_record(5), core_bank_record(5), core_bank_record(6))

        eng, _said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert _crash_rows(db, sid) == [(5, -30), (6, -41)]
        assert hunts == []
        assert eng._core_states[5].backoff_fail_bound == -30
        assert eng._core_states[6].backoff_fail_bound == -41
        assert eng._core_states[5].phase == TunerPhase.BACKOFF_PRECONFIRM
        assert eng._core_states[7].crash_count == 0
        assert eng._core_states[7].best_offset == -50
        assert eng._core_states[7].phase == TunerPhase.HARDENED
        assert not any(cs.in_test for cs in db.get_tuner_core_states(sid).values())

    def test_sibling_cpu_maps_to_its_physical_core(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        sid = _seed_proven_validating(_make_engine(db, topo_dual_ccd_x3d, mock_backend), db)
        sibling = topo_dual_ccd_x3d.cores[3].logical_cpus[1]
        machine.crash_after(core_bank_record(sibling, corrected=False))

        _eng, _said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert _crash_rows(db, sid) == [(3, -43)]
        assert hunts == []

    def test_unattributable_events_do_not_penalize(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        sid = _seed_proven_validating(_make_engine(db, topo_dual_ccd_x3d, mock_backend), db)
        machine.crash_after(["Kernel panic - not syncing: Fatal machine check"])

        _eng, _said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert _crash_rows(db, sid) == []
        assert hunts == [True]

    def test_a_shared_bank_in_the_journal_convicts_no_core(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        sid = _seed_proven_validating(_make_engine(db, topo_dual_ccd_x3d, mock_backend), db)
        cpu = topo_dual_ccd_x3d.cores[5].logical_cpus[0]
        machine.crash_after(
            [
                "[Hardware Error]: Corrected error, no action required.",
                f"[Hardware Error]: CPU:{cpu} (1a:44:0) MC21_STATUS[Over|CE|MiscV|AddrV|-|-|SyndV|CECC|-|-|-]: "
                "0xdc2040000400011b",
                "[Hardware Error]: IPID: 0x0000009600050f00, Syndrome: 0x0000000000000001",
                "[Hardware Error]: Unified Memory Controller Ext. Error Code: 0",
            ]
        )

        eng, _said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert _crash_rows(db, sid) == []
        assert hunts == [True]
        assert eng._core_states[5].crash_count == 0

    def test_no_forensics_multi_core_requests_hunt_not_guess(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        sid = _seed_proven_validating(_make_engine(db, topo_dual_ccd_x3d, mock_backend), db)
        machine.crash_after()

        _eng, _said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert hunts == [True]
        stored = db.get_tuner_core_states(sid).values()
        assert all(cs.crash_count == 0 for cs in stored)
        assert not any(cs.in_test for cs in stored)

    def test_forensics_unavailable_fails_closed_to_hunt(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        sid = _seed_proven_validating(_make_engine(db, topo_dual_ccd_x3d, mock_backend), db)
        dead = machine.boot_id
        machine.crash_after()
        machine.forget_boot(dead)

        _eng, said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        unavailable = [line for line in said if line.startswith("Kernel-journal forensics unavailable: ")]
        assert len(unavailable) == 1
        assert f"No journal boot entry found for the specified boot ({dead}+0)." in unavailable[0]
        assert hunts == [True]

    def test_persisted_hunt_slot_is_proof_by_isolation(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        sid = _seed_proven_validating(eng, db)
        db.set_hunting_core(sid, 5)
        db.set_unattributed_crashes(sid, 1)
        machine.crash_after()

        eng, _said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert _crash_rows(db, sid) == [(5, -30)]
        assert hunts == []
        assert eng._core_states[5].crash_count == 1
        assert tp.get_unattributed_crashes(db, sid) == 0
        assert tp.get_session(db, sid).hunting_core is None

    def test_single_in_test_search_flow_keeps_direct_blame(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        sid = _make_engine(db, topo_dual_ccd_x3d, mock_backend)._session_id
        tp.save_core_state(
            db, sid, CoreState(core_id=2, phase=TunerPhase.COARSE_SEARCH, current_offset=-20, in_test=True)
        )
        machine.crash_after()

        eng, _said, hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert _crash_rows(db, sid) == [(2, -20)]
        assert hunts == []
        assert eng._core_states[2].crash_count == 1


class TestCrashHunt:
    def test_hunt_orders_by_suspicion_and_isolates_first_suspect(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
        # Core 5 has prior kernel-error evidence -> hunted first.
        tp.log_test_result(
            db,
            eng._session_id,
            5,
            -30,
            "mce_evidence",
            passed=False,
            error_msg="prior",
            error_type="mce",
            duration=None,
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

    def test_fruitless_hunt_counts_and_pauses_at_threshold(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(
            db,
            topo_dual_ccd_x3d,
            mock_backend,
            max_unattributed_crash_hunts=2,
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

    def test_hunt_slot_failure_convicts_core_and_resets_counter(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
        db.set_unattributed_crashes(eng._session_id, 1)
        eng._hunting = True
        eng._co_applied[5] = -30

        eng._on_hunt_slot_finished(5, passed=False, outcome=Outcome.OWN_CORE_MCE, foreign={})

        cs = eng._core_states[5]
        assert cs.backoff_fail_bound == -30
        assert cs.phase == TunerPhase.BACKOFF_PRECONFIRM
        assert cs.crash_count == 0  # evidence-grade: nothing crashed
        assert tp.get_unattributed_crashes(db, eng._session_id) == 0
        assert eng._hunting is False


class TestForeignMceEvidence:
    def test_events_group_by_core_and_severity(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        events = (
            CoreMachineCheck(cpu=5, bank=0, corrected=True, message="a", raw_ts=1.0),
            CoreMachineCheck(cpu=5, bank=0, corrected=False, message="b", raw_ts=2.0),
            CoreMachineCheck(cpu=6, bank=0, corrected=True, message="c", raw_ts=3.0),
            CoreMachineCheck(cpu=2, bank=0, corrected=True, message="own", raw_ts=4.0),
            UnblamedMachineCheck(logged_by_cpu=6, bank=21, corrected=False, message="umc", raw_ts=5.0),
            KernelTrouble(message="panic", raw_ts=6.0),
        )

        foreign = eng._foreign_mce_by_core(tested_core=2, events=events)

        assert sorted(foreign) == [5, 6]
        assert foreign[5]["corrected"] is False  # any uncorrected wins
        assert foreign[5]["messages"] == ["a", "b"]
        assert foreign[6]["corrected"] is True

    def test_no_events_are_no_evidence(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        assert eng._foreign_mce_by_core(0, ()) == {}

    def test_corrected_evidence_backs_off_one_step_and_reearns(self, db, topo_dual_ccd_x3d, mock_backend):
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

    def test_uncorrected_evidence_gets_crash_grade_penalty(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(
            db,
            topo_dual_ccd_x3d,
            mock_backend,
            fine_step=1,
            crash_penalty_steps=3,
        )
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        eng._co_applied[6] = -41

        eng._apply_foreign_evidence({6: {"corrected": False, "messages": ["m"]}})

        cs = eng._core_states[6]
        assert cs.backoff_fail_bound == -41
        assert cs.current_offset == -38  # three steps
        assert cs.crash_count == 1

    def test_error_at_stock_changes_no_state(self, db, topo_dual_ccd_x3d, mock_backend):
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
        sid = db.create_tuner_session("{}", "1.0", "TestCPU", boot_id=this_boot())
        for core_id, value in ((0, -20), (5, -30)):
            db.journal_confirm(journaled(db, sid, core_id, value))

        db.journal_mark_survived(sid, boot_id=this_boot(), exclude_cores=[5])

        assert db.journal_proven_values(sid, -1) == {0: -20}
        assert tp.crash_suspects(tp.resident_at_death(db.journal_rows(sid, this_boot())), -1) == {5: -30}


class TestDriftAgainstJournal:
    def test_no_false_drift_when_smu_holds_tuner_written_values(self, db, topo_dual_ccd_x3d, mock_backend):
        """Reopening mid-validation: the SMU holding exactly what the tuner
        wrote (the confirmed offsets) is not drift."""
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for cs in eng._core_states.values():
            cs.in_test = False
            tp.save_core_state(db, eng._session_id, cs)
        for core_id, offset in BEST.items():
            eng._smu.written[core_id] = offset  # SMU == tuner's last write

        eng._start_worker = lambda *a, **k: None  # no real worker threads
        drift_reports: list[str] = []
        eng.co_drift_detected.connect(drift_reports.append)
        eng.resume(eng._session_id)

        assert drift_reports == []

    def test_third_party_change_is_reported_against_last_write(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        _seed_hardened_validating(eng, db, BEST, BASELINES)
        for row in tp.journal_rows(db, eng._session_id, this_boot()):
            db.journal_confirm(row.id)
        for cs in eng._core_states.values():
            cs.in_test = False
            tp.save_core_state(db, eng._session_id, cs)
        for core_id, offset in BEST.items():
            eng._smu.written[core_id] = offset
        eng._smu.written[3] = -10  # someone changed core 3 behind our back

        eng._start_worker = lambda *a, **k: None  # no real worker threads
        drift_reports: list[str] = []
        said: list[str] = []
        eng.co_drift_detected.connect(drift_reports.append)
        eng.log_message.connect(said.append)
        eng.resume(eng._session_id)

        assert len(drift_reports) == 1
        drift = json.loads(drift_reports[0])
        assert drift == {"3": {"expected": BEST[3], "actual": -10, "cause": "outside"}}
        assert any("something outside the tuner changed cores [3]" in line for line in said)

    def test_an_unconfirmed_last_write_is_reported_as_incomplete(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, cores_to_test=[0])
        tp.save_core_state(db, eng._session_id, CoreState(core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-12))
        assert eng._apply_co(0, -8)
        eng._smu.set_co_offset = lambda _core_id, _value: False
        assert eng._apply_co(0, -12) is False

        eng._start_worker = lambda *a, **k: None
        drift_reports: list[str] = []
        said: list[str] = []
        eng.co_drift_detected.connect(drift_reports.append)
        eng.log_message.connect(said.append)
        eng.resume(eng._session_id)

        assert [json.loads(sent) for sent in drift_reports] == [
            {"0": {"expected": -12, "actual": -8, "cause": "incomplete"}}
        ]
        drift_lines = [line for line in said if line.startswith("CO drift detected")]
        assert len(drift_lines) == 1
        assert "the tuner's last write did not complete on cores [0]" in drift_lines[0]
        assert "outside" not in drift_lines[0]

    def test_after_a_reboot_an_unfinished_write_never_explains_the_smu(
        self, db, topo_dual_ccd_x3d, mock_backend, machine
    ):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend, cores_to_test=[0])
        sid = eng._session_id
        tp.save_core_state(db, sid, CoreState(core_id=0, phase=TunerPhase.FINE_SEARCH, current_offset=-12))
        assert eng._apply_co(0, -8)
        eng._smu.set_co_offset = lambda _core_id, _value: False
        assert eng._apply_co(0, -12) is False
        machine.reboot(clean=True)
        fresh = TunerEngine(
            db=db, topology=topo_dual_ccd_x3d, smu=FakeSMU(), backend=mock_backend, config=TunerConfig()
        )
        fresh._smu.written[0] = -5
        fresh._run_next = lambda: None
        drift_reports: list[str] = []
        fresh.co_drift_detected.connect(drift_reports.append)

        fresh.resume(sid)

        assert [json.loads(sent) for sent in drift_reports] == [
            {"0": {"expected": -12, "actual": -5, "cause": "outside"}}
        ]


class TestHarvestThreshold:
    def test_machine_checks_read_live_are_not_harvested_again(self, db, topo_dual_ccd_x3d, mock_backend, machine):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        sid = _seed_proven_validating(eng, db)
        eng._validation_stage = 2
        eng._run_next = lambda: None
        cpu = topo_dual_ccd_x3d.cores[5].logical_cpus[0]
        for line in core_bank_record(cpu):
            machine.kernel(line, mono_s=machine.mono_ns / 1e9 + 1, realtime=time.time())
        machine.advance(1)

        eng._on_test_finished(report(0, events=(mce(cpu),), kernel_read_ns=machine.mono_ns))
        assert eng._core_states[5].current_offset == BEST[5] + 1
        eng.pause()
        machine.reboot(clean=True)

        eng, _said, _hunts = _resumed(db, topo_dual_ccd_x3d, mock_backend, sid)

        assert eng._core_states[5].current_offset == BEST[5] + 1
        assert [row["core_id"] for row in tp.get_test_log(db, sid) if row["error_type"] == "mce"] == [5]


class TestNarrativePersistence:
    def test_log_messages_become_durable_events(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        eng.log_message.emit("the story survives the terminal")
        events = tp.get_events(db, eng._session_id)
        assert [e["message"] for e in events] == ["the story survives the terminal"]

    def test_narrative_without_session_is_dropped(self, db, topo_dual_ccd_x3d, mock_backend):
        eng = _make_engine(db, topo_dual_ccd_x3d, mock_backend)
        eng._session_id = None
        eng.log_message.emit("nowhere to persist")  # must not raise


def _mce_events(cpu: int) -> tuple[CoreMachineCheck, ...]:
    return (CoreMachineCheck(cpu=cpu, bank=0, corrected=True, message=CPU5_CORRECTED.format(cpu=cpu), raw_ts=1.0),)


SHARED_BANK = (
    UnblamedMachineCheck(logged_by_cpu=5, bank=21, corrected=True, message=CPU5_CORRECTED.format(cpu=5), raw_ts=1.0),
)


class TestUnattributedMcePayload:
    """A machine check that names NO core taints every resident value.

    Surviving a stress test does not clear an error the hardware just reported,
    and with no CPU named there is no core to exclude -- so the whole resident
    set must stay unproven. Journalling it survived would record a value the
    hardware complained about as proven, and the search would then treat it as
    a safe floor.
    """

    def test_an_event_blaming_no_core_is_detected(self):
        from corecycler.tuner.engine import _has_unattributed_mce

        assert _has_unattributed_mce(SHARED_BANK) is True
        assert _has_unattributed_mce((KernelTrouble(message="Kernel panic - not syncing", raw_ts=1.0),)) is True

    def test_attributed_or_absent_events_are_not(self):
        from corecycler.tuner.engine import _has_unattributed_mce

        assert _has_unattributed_mce(_mce_events(5)) is False
        assert _has_unattributed_mce(()) is False

    def _resident_marks_after_a_pass(self, db, topo_single_ccd, mock_backend, events):
        eng = _make_engine(db, topo_single_ccd, mock_backend)
        sid = eng._session_id
        cs = CoreState(core_id=0, phase=TunerPhase.CONFIRMING, current_offset=-20, best_offset=-20, baseline_offset=0)
        eng._core_states = {0: cs}
        tp.save_core_state(db, sid, cs)
        eng._co_applied[0] = -20
        resident = journaled(db, sid, 0, -20)
        db.journal_confirm(resident)
        result = StressResult(core_id=0, outcome=Outcome.PASS, detail="", duration_seconds=1.0)
        eng._on_test_finished(WorkerReport(lanes=(result,), peak_stretch_pct=0.0, kernel_events=events))
        return [row.survived for row in tp.journal_rows(db, sid, this_boot()) if row.id == resident]

    def test_a_clean_pass_marks_the_resident_set_survived(self, db, topo_single_ccd, mock_backend):
        assert self._resident_marks_after_a_pass(db, topo_single_ccd, mock_backend, ()) == [True]

    def test_a_pass_carrying_an_unattributed_mce_marks_nothing_survived(self, db, topo_single_ccd, mock_backend):
        assert self._resident_marks_after_a_pass(db, topo_single_ccd, mock_backend, SHARED_BANK) == [False]
