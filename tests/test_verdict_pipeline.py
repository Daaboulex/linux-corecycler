"""Cross-seam spec: a real payload's outcome travels unchanged into the engine."""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from corecycler.engine import containment, execution
from corecycler.engine.backends.base import Outcome, StressConfig
from corecycler.engine.backends.mprime import MprimeBackend
from corecycler.engine.backends.stress_ng import StressNgBackend
from corecycler.engine.parallel import ParallelStress
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig
from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.history.db import HistoryDB
from corecycler.tuner import engine as eng
from corecycler.tuner import persistence as tp
from corecycler.tuner.state import TunerPhase
from tests.test_execution import scripted, within
from tests.test_tuner_engine_phases import _backend, _config, _smu, _topo

if TYPE_CHECKING:
    from pathlib import Path

FATAL_AT_ONCE = """
import sys, time
line = "FATAL ERROR: Rounding was 0.5, expected less than 0.4"
open("results.txt", "w").write(line + "\\n")
print(line, flush=True)
time.sleep(0.05)
sys.exit(0)
"""

BUSY = "while True: pass"

MPRIME_TORTURE_THREADS = """
import re, time
text = open("prime.txt").read()
def key(name):
    return int(re.search(name + r"=(\\d+)", text).group(1))
threads = max(1, min(key("TortureCores"), key("NumCPUs"))) * (2 if key("TortureHyperthreading") else 1)
open("../threads", "w").write(str(threads))
while True:
    time.sleep(0.05)
"""

STRESSOR_SEGV = """
import sys
print("stress-ng: info:  [101] stressor terminated with unexpected signal 11 'SIGSEGV'", flush=True)
print("stress-ng: info:  [100] failed: 1: cpu (1)", flush=True)
print("stress-ng: info:  [100] unsuccessful run completed in 0.05 secs", flush=True)
sys.exit(2)
"""


@pytest.fixture
def engine(tmp_path, monkeypatch):
    db = HistoryDB(":memory:")
    instance = eng.TunerEngine(
        db=db, topology=_topo(), smu=_smu(), backend=_backend(), config=_config(), work_dir=tmp_path
    )
    monkeypatch.setattr(instance, "_start_worker", MagicMock())
    monkeypatch.setattr(eng.QTimer, "singleShot", lambda _ms, fn: None)
    monkeypatch.setattr(execution, "read_cpu_temperature", lambda: 40.0)
    instance.start()
    yield instance
    db.close()


def _worker(backend, tmp_path, seconds: float) -> eng._TunerWorker:
    scheduler = CoreScheduler(
        topology=_topo(),
        backend=backend,
        stress_config=StressConfig(),
        scheduler_config=SchedulerConfig(
            seconds_per_core=seconds, cores_to_test=[0], stop_on_error=True, poll_interval=0.02
        ),
        work_dir=tmp_path / "run",
    )
    return eng._TunerWorker(0, 0, scheduler)


def _deliver(engine, worker) -> None:
    worker.finished.connect(engine._on_test_finished)
    try:
        with patch.object(containment, "contain", return_value=None):
            worker.run()
    finally:
        worker.finished.disconnect(engine._on_test_finished)


def _last_row(engine) -> dict:
    return tp.get_test_log(engine._db, engine._session_id, core_id=0)[-1]


def test_an_mprime_fatal_error_in_the_first_second_reaches_the_engine_as_a_computation_fail(engine, tmp_path):
    tested = engine._core_states[0].current_offset
    _deliver(engine, _worker(scripted(MprimeBackend, FATAL_AT_ONCE), tmp_path, 5))
    row = _last_row(engine)
    assert not row["passed"]
    assert row["error_type"] == "computation"
    assert "FATAL ERROR" in row["error_message"]
    assert engine.status != "paused"
    assert engine._core_states[0].coarse_fail_offset == tested


def test_a_loaded_mprime_run_reaches_the_engine_as_a_pass(engine, tmp_path):
    _deliver(engine, _worker(scripted(MprimeBackend, BUSY), tmp_path, 2))
    row = _last_row(engine)
    assert row["passed"]
    assert row["error_type"] is None
    assert engine.status != "paused"


def test_the_report_crosses_the_worker_thread_as_one_object(engine, tmp_path):
    qtcore = pytest.importorskip("PySide6.QtCore")
    if not hasattr(qtcore, "QCoreApplication"):
        pytest.skip("the stand-in Qt has no event loop to cross")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    class Receiver(qtcore.QObject):
        def __init__(self) -> None:
            super().__init__()
            self.taken: list[tuple[object, bool]] = []

        @qtcore.Slot(object)
        def take(self, report) -> None:
            self.taken.append((report, qtcore.QThread.currentThread() == app.thread()))

    receiver = Receiver()
    worker = _worker(scripted(MprimeBackend, FATAL_AT_ONCE), tmp_path, 5)
    worker.finished.connect(receiver.take)
    worker.finished.connect(engine._on_test_finished)
    with patch.object(containment, "contain", return_value=None):
        worker.start()
        deadline = time.monotonic() + 30
        while not receiver.taken and time.monotonic() < deadline:
            app.sendPostedEvents(receiver, 0)
            time.sleep(0.01)
        assert worker.wait(30000)
    app.sendPostedEvents(engine, 0)
    report, on_the_receivers_thread = receiver.taken[0]
    assert isinstance(report, eng.WorkerReport)
    assert report.result.outcome is Outcome.COMPUTATION
    assert on_the_receivers_thread
    assert _last_row(engine)["error_type"] == "computation"


class Lane1FatalsMprime(MprimeBackend):
    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        return [sys.executable, "-c", FATAL_AT_ONCE if work_dir.parent.name == "core_1" else BUSY]


def test_a_two_lane_mprime_batch_reaches_the_engine_with_every_lane(engine, tmp_path, monkeypatch):
    for core in (0, 1):
        engine._core_states[core].phase = TunerPhase.CONFIRMED
    engine._validation_stage = 2
    engine._set_status("validating")
    logged: list[tuple[int, Outcome]] = []
    real_log_parallel_rows = engine._log_parallel_rows

    def spy(report, phase):
        logged.extend((lane.core_id, lane.outcome) for lane in report.lanes)
        real_log_parallel_rows(report, phase)

    monkeypatch.setattr(engine, "_log_parallel_rows", spy)
    runner = ParallelStress(
        topology=_topo(),
        backend=Lane1FatalsMprime(),
        stress_config=StressConfig(),
        scheduler_config=SchedulerConfig(seconds_per_core=5, cores_to_test=[0, 1], poll_interval=0.02),
        work_dir=tmp_path / "batch",
    )
    _deliver(engine, eng._ParallelWorker(0, 0, runner))
    assert logged == [(0, Outcome.CUT_SHORT), (1, Outcome.COMPUTATION)]
    reported = tp.get_test_log(engine._db, engine._session_id, core_id=1)[-1]
    assert reported["error_type"] == "computation"
    assert tp.get_test_log(engine._db, engine._session_id, core_id=0) == []


class RecordsItsTortureConfig(MprimeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.prime_txt: list[str] = []

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        super().prepare(work_dir, config)
        self.prime_txt.append((work_dir / "prime.txt").read_text())

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        return [sys.executable, "-c", MPRIME_TORTURE_THREADS]


def test_each_stage4_lane_runs_one_mprime_torture_worker_that_meets_its_load_floor(tmp_path, monkeypatch):
    monkeypatch.setattr(execution, "read_cpu_temperature", lambda: 40.0)
    cores = [0, 1, 2, 3]
    topology = CPUTopology()
    for core in cores:
        topology.cores[core] = PhysicalCore(core_id=core, ccd=0, ccx=None, logical_cpus=(core, core + len(cores)))
    backend = RecordsItsTortureConfig()
    scheduler = CoreScheduler(
        topology=topology,
        backend=backend,
        stress_config=StressConfig(threads=2),
        scheduler_config=SchedulerConfig(cores_to_test=cores, poll_interval=0.02),
        work_dir=tmp_path / "rapid",
    )
    written = [tmp_path / "rapid" / "rapid_transition" / f"core_{core}" / "threads" for core in cores]

    def holds_the_deadline_until_every_lane_started(_core: int, _status) -> None:
        within(10.0, lambda: all(path.exists() for path in written))

    scheduler.on_status_update.append(holds_the_deadline_until_every_lane_started)
    with patch.object(containment, "contain", return_value=None):
        results = scheduler.run_rapid_transitions(cores, total_duration=4.0, load_seconds=4.0)
    assert len(backend.prime_txt) == len(cores)
    for prime_txt in backend.prime_txt:
        assert {"NumCPUs=1", "TortureCores=1", "TortureHyperthreading=0"} <= set(prime_txt.splitlines())
    started = {
        int(path.parent.name.removeprefix("core_")): int(path.read_text())
        for path in (tmp_path / "rapid").glob("rapid_transition/core_*/threads")
    }
    assert started == dict.fromkeys(cores, 1)
    assert {core: result.outcome for core, result in results.items()} == dict.fromkeys(cores, Outcome.PASS)


def test_a_stress_ng_stressor_crash_in_a_hunt_gets_the_crash_penalty(engine, tmp_path):
    engine._hunting = True
    engine._set_status("hunting")
    cs = engine._core_states[0]
    crashes, resident = cs.crash_count, engine._co_applied.get(0, cs.current_offset)
    _deliver(engine, _worker(scripted(StressNgBackend, STRESSOR_SEGV), tmp_path, 5))
    assert cs.crash_count == crashes + 1
    assert _last_row(engine)["error_type"] == "crash"
    config = engine._config
    assert cs.current_offset == resident - config.direction * config.crash_penalty_steps * config.fine_step
