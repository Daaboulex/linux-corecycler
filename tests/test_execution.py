"""Behavior spec for the one supervised execution engine."""

from __future__ import annotations

import contextlib
import ctypes.util
import itertools
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from corecycler.config.tools import Resolution
from corecycler.engine import containment, execution, topology
from corecycler.engine.backends.base import LaneSignal, Outcome, StressBackend, StressConfig, StressMode
from corecycler.engine.backends.mprime import MprimeBackend
from corecycler.engine.backends.stress_ng import StressNgBackend
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.engine.backends.ycruncher import YCruncherBackend
from corecycler.engine.detector import (
    CoreMachineCheck,
    ErrorDetector,
    KernelRead,
    KernelTrouble,
    UnblamedMachineCheck,
)
from corecycler.engine.execution import (
    Lane,
    SuperviseHooks,
    Supervisor,
    ThermalWatch,
    busy_fraction,
    cpu_times,
    exit_signal,
    watch_idle,
)
from tests.test_detector import LONE_DECODER_RECORD, _append, _kmsg

if TYPE_CHECKING:
    from collections.abc import Callable


BUSY = "while True: pass"

DEAF_TO_SIGTERM = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open("ready", "w").close()
while True:
    time.sleep(0.01)
"""

MARKS_OUR_SIGTERM_THEN_HOLDS = """
import signal, time
hit = []
def on_term(*_):
    open("termed", "w").close()
    hit.append(time.monotonic())
signal.signal(signal.SIGTERM, on_term)
open("ready", "w").close()
while not hit or time.monotonic() - hit[0] < 0.6:
    time.sleep(0.01)
"""

SLOW_TO_STOP = """
import signal, time
hit = []
signal.signal(signal.SIGTERM, lambda *_: hit.append(time.monotonic()))
open("ready", "w").close()
while not hit or time.monotonic() - hit[0] < {hold}:
    time.sleep(0.01)
"""

KILLS_ITS_LEADER_ON_SIGTERM = """
import os, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
leader = os.getpid()
if os.fork() == 0:
    hit = []
    signal.signal(signal.SIGTERM, lambda *_: hit.append(1))
    open("ready", "w").close()
    while not hit:
        time.sleep(0.01)
    time.sleep(0.3)
    os.kill(leader, signal.SIGKILL)
    os._exit(0)
while True:
    time.sleep(0.01)
"""

WRITES_ITS_PID_THEN_RUNS = """
import os, sys, time
with open(sys.argv[1] + ".tmp", "w") as out:
    out.write(str(os.getpid()))
os.rename(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(30)
"""

LEAVES_A_CHILD = """
import os, sys, time
if os.fork() == 0:
    open("stray.pid", "w").write(str(os.getpid()))
    time.sleep(30)
    os._exit(0)
time.sleep(0.1)
sys.exit(0)
"""

EXITS_AT_ONCE_LEAVING_A_CHILD_IN_ITS_OWN_GROUP = """
import os, time
if os.fork() == 0:
    os.setsid()
    with open("stray.tmp", "w") as out:
        out.write(str(os.getpid()))
    os.rename("stray.tmp", "stray.pid")
    time.sleep(30)
    os._exit(0)
while not os.path.exists("stray.pid"):
    time.sleep(0.01)
time.sleep(0.4)
"""

CORE_0_FAILS_ONCE_CORE_1_IS_READY = """
import glob, os, sys, time
if os.path.basename(os.path.dirname(os.getcwd())) == "core_0":
    while not glob.glob("../../core_1/run-*/ready"):
        time.sleep(0.01)
    time.sleep(0.1)
    print("fake error: FATAL", flush=True)
    sys.exit(3)
"""

CORE_1_FAILS_AND_RUNS_ON_THEN_CORE_0_FAILS = """
import glob, os, sys, time
if os.path.basename(os.path.dirname(os.getcwd())) == "core_1":
    print("fake error: FATAL", flush=True)
    open("printed", "w").close()
    while True:
        time.sleep(0.01)
while not glob.glob("../../core_1/run-*/printed"):
    time.sleep(0.01)
print("fake error: FATAL", flush=True)
sys.exit(3)
"""

CORE_0_ENDS_AT_ONCE = """
import os, sys, time
if os.path.basename(os.path.dirname(os.getcwd())) == "core_0":
    sys.exit(0)
while True:
    time.sleep(0.01)
"""

ONLY_CORE_1_IS_KILLED = """
import os, signal, time
if os.path.basename(os.path.dirname(os.getcwd())) == "core_1":
    time.sleep(0.2)
    os.kill(os.getpid(), signal.SIGKILL)
while True:
    time.sleep(0.01)
"""

FATAL_AT_ONCE = """
import sys, time
line = "FATAL ERROR: Rounding was 0.5, expected less than 0.4"
open("results.txt", "w").write(line + "\\n")
print(line, flush=True)
time.sleep(0.05)
sys.exit(0)
"""

FATAL_THEN_SIGKILL = """
import os, signal, time
open("results.txt", "w").write("FATAL ERROR: Rounding was 0.5, expected less than 0.4\\n")
time.sleep(0.05)
os.kill(os.getpid(), signal.SIGKILL)
"""

FATAL_ON_STDERR = """
import sys
sys.stderr.write("FATAL ERROR: Rounding was 0.5, expected less than 0.4\\n")
sys.exit(1)
"""

BUSY_THEN_EXIT_SEVEN = """
import sys, time
started = time.monotonic()
while time.monotonic() - started < 0.3:
    pass
sys.exit(7)
"""

FAIL_LINE = "stress-ng: fail:  [1] cpu: verification failed"

PRINTS_A_FAIL_THEN_RUNS = f"""
import time
print({FAIL_LINE!r}, flush=True)
open("printed", "w").close()
time.sleep(30)
"""

FAILS_AT_ONCE = f"""
import sys
print({FAIL_LINE!r}, flush=True)
sys.exit(2)
"""

MISCOMPARE_MID_RUN = """
import time
time.sleep(0.2)
print("Report Error: miscompare : DIMM0 : 1 : 3s", flush=True)
time.sleep(60)
"""

FLOODS_ITS_OUTPUT = """
import sys, time
sys.stdout.write("x" * 300_000)
sys.stdout.flush()
open(sys.argv[1], "w").close()
time.sleep(30)
"""

BAD_BYTE_THEN_FAIL = """
import sys
sys.stdout.buffer.write(b"\\xffstress-ng: fail:  [1] cpu: x\\n")
sys.stdout.flush()
sys.exit(2)
"""

LEADER_FAILS_WHILE_A_CHILD_HOLDS_THE_OUTPUT = f"""
import os, sys, time
if os.fork() == 0:
    time.sleep(30)
    os._exit(0)
print({FAIL_LINE!r}, flush=True)
sys.exit(2)
"""

SEGFAULTS = "import os, signal, time; time.sleep(0.05); os.kill(os.getpid(), signal.SIGSEGV)"

LAUNCHER_AFTER_ITS_CHILD_SEGFAULTED = "import sys; sys.exit(11)"

WRITES_IN_ITS_CWD = 'import time; open("scratch", "w").close(); time.sleep(30)'

REMOVES_ITS_OWN_LOG = 'import os, time; os.remove("stdout.log"); open("removed", "w").close(); time.sleep(30)'


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def scripted(backend: type[StressBackend], code: str) -> StressBackend:
    class Scripted(backend):
        def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
            return _child(code)

    return Scripted()


def gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rpartition(")")[2].split()[0] == "Z"
    except (FileNotFoundError, ProcessLookupError):
        return True


def within(seconds: float, condition: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def stop_once(stop: threading.Event, condition: Callable[[], bool]) -> None:
    def watch() -> None:
        within(10.0, condition)
        stop.set()

    threading.Thread(target=watch, daemon=True).start()


def holds_the_deadline_until(condition: Callable[[], bool]) -> SuperviseHooks:
    return SuperviseHooks(on_status=lambda _core, _elapsed: within(10.0, condition))


class FakeBackend(StressBackend):
    name = "fake"

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        found: tuple[LaneSignal, ...] = (),
        prepare_exc: Exception | None = None,
        assert_exc: Exception | None = None,
    ) -> None:
        super().__init__()
        self.command = command or _child("import time; time.sleep(0.4)")
        self.found = found
        self.prepare_exc = prepare_exc
        self.assert_exc = assert_exc
        self.prepared: list[Path] = []
        self.configs: list[StressConfig] = []

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        self.configs.append(config)
        return list(self.command)

    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        return self.found

    def get_supported_modes(self):
        return [StressMode.SSE]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        if self.prepare_exc is not None:
            raise self.prepare_exc
        self.prepared.append(work_dir)

    def assert_prepared(self, work_dir: Path) -> None:
        if self.assert_exc is not None:
            raise self.assert_exc


class ReadsFatal(FakeBackend):
    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        return tuple(LaneSignal(Outcome.COMPUTATION, line) for line in output.splitlines() if "FATAL" in line)


class FakeMprime(MprimeBackend):
    def __init__(self, code: str) -> None:
        super().__init__()
        self.code = code

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        return _child(self.code)


class FakeDetector:
    def __init__(self, reads: list | None = None, *, final_problem: str | None = None) -> None:
        self.reads = [read if isinstance(read, KernelRead) else KernelRead(tuple(read), None) for read in reads or []]
        self.final_problem = final_problem
        self.finals: list[bool] = []

    def reset(self) -> None:
        pass

    def read_new(self, *, final: bool = False) -> KernelRead:
        self.finals.append(final)
        if self.reads:
            return self.reads.pop(0)
        return KernelRead((), self.final_problem if final else None)


def own(cpu: int, message: str = "boom") -> CoreMachineCheck:
    return CoreMachineCheck(cpu=cpu, bank=0, corrected=False, message=message, raw_ts=0.0)


def unblamed(message: str = "boom") -> UnblamedMachineCheck:
    return UnblamedMachineCheck(logged_by_cpu=0, bank=21, corrected=True, message=message, raw_ts=0.0)


def trouble(message: str = "watchdog: BUG: soft lockup - CPU#0 stuck for 22s! [mprime:1]") -> KernelTrouble:
    return KernelTrouble(message=message, raw_ts=0.0)


def cool_thermal() -> ThermalWatch:
    return ThermalWatch(
        max_temperature=95.0,
        grace_seconds=3.0,
        hard_margin=8.0,
        require_sensor=False,
        read=lambda: 40.0,
    )


def hot_thermal() -> ThermalWatch:
    return ThermalWatch(
        max_temperature=95,
        grace_seconds=0,
        hard_margin=8,
        require_sensor=False,
        read=lambda: 104.0,
    )


def make_supervisor(
    backend: StressBackend,
    *,
    detector: FakeDetector | ErrorDetector | None = None,
    thermal: ThermalWatch | None = None,
    stop_event: threading.Event | None = None,
    observed: list | None = None,
    poll_interval: float = 0.02,
    stall_timeout: float = 30.0,
    phase: str = "stress",
    hooks: SuperviseHooks | None = None,
    containment_for=None,
) -> tuple[Supervisor, threading.Event, list]:
    stop = stop_event or threading.Event()
    seen = observed if observed is not None else []
    supervisor = Supervisor(
        backend=backend,
        detector=detector or FakeDetector(),
        thermal=thermal or cool_thermal(),
        stop_event=stop,
        observed=seen,
        poll_interval=poll_interval,
        stall_timeout=stall_timeout,
        phase=phase,
        hooks=hooks,
        containment_for=containment_for or (lambda cpus: None),
    )
    return supervisor, stop, seen


def one_busy_cpu(reader: str = "process_group_cpu_seconds"):
    launched = time.monotonic()
    return patch.object(containment, reader, side_effect=lambda _source: time.monotonic() - launched)


def lane(tmp_path: Path, core_id: int = 0, cpus: tuple[int, ...] = (0,)) -> Lane:
    return Lane(core_id=core_id, cpus=cpus, work_dir=tmp_path / f"core_{core_id}")


def run_one(supervisor: Supervisor, one: Lane, duration: float):
    return supervisor.run([one], StressConfig(), duration)[one.core_id]


def two_lanes(tmp_path: Path) -> list[Lane]:
    return [lane(tmp_path, 0, (0,)), lane(tmp_path, 1, (1,))]


class TestLaunchRefusals:
    def test_prepare_failure_is_not_started(self, tmp_path):
        backend = FakeBackend(prepare_exc=OSError("read-only work dir"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict.outcome is Outcome.NOT_STARTED
        assert "read-only work dir" in verdict.detail

    def test_unreadable_config_refuses_the_launch(self, tmp_path):
        backend = FakeBackend(assert_exc=OSError("local.txt is missing"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict.outcome is Outcome.NOT_STARTED
        assert "local.txt" in verdict.detail

    def test_missing_containment_refuses_the_launch(self, tmp_path):
        backend = FakeBackend()

        def refuse(cpus):
            raise containment.ContainmentUnavailable("no systemd-run")

        supervisor, _, _ = make_supervisor(backend, containment_for=refuse)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict.outcome is Outcome.NOT_STARTED
        assert "no systemd-run" in verdict.detail

    def test_missing_binary_is_not_started(self, tmp_path):
        backend = FakeBackend(command=["/nonexistent/stress-binary"])
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict.outcome is Outcome.NOT_STARTED
        assert "Failed to start" in verdict.detail

    def test_each_lane_runs_as_many_threads_as_it_has_cpus(self, tmp_path):
        backend = FakeBackend(_child(BUSY))
        supervisor, _, _ = make_supervisor(backend)
        supervisor.run([lane(tmp_path, 0, (0, 16)), lane(tmp_path, 1, (1,))], StressConfig(threads=8), 0.1)
        assert [config.threads for config in backend.configs] == [2, 1]

    def test_each_lane_tells_its_backend_its_own_cpus(self, tmp_path):
        backend = FakeBackend(_child(BUSY))
        supervisor, _, _ = make_supervisor(backend)
        supervisor.run([lane(tmp_path, 0, (0, 16)), lane(tmp_path, 1, (1,))], StressConfig(threads=8), 0.1)
        assert [config.cpus for config in backend.configs] == [(0, 16), (1,)]


class TestOutcomeGrading:
    def test_a_computation_error_before_an_instant_exit_is_a_fail(self, tmp_path):
        supervisor, _, _ = make_supervisor(FakeMprime(FATAL_AT_ONCE))
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "FATAL ERROR" in verdict.detail

    def test_a_cpu_signal_death_at_once_is_silicon(self, tmp_path):
        code = "import os, signal, time; time.sleep(0.05); os.kill(os.getpid(), signal.SIGSEGV)"
        supervisor, _, _ = make_supervisor(FakeBackend(_child(code)))
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.CPU_SIGNAL
        assert "SIGSEGV" in verdict.detail

    def test_a_compute_crash_is_silicon(self, tmp_path, monkeypatch):
        root = tmp_path / "y-cruncher"
        (root / "Binaries").mkdir(parents=True)
        compute = root / "Binaries" / "05-A64 ~ Kasumi"
        compute.write_text("")
        (root / "y-cruncher").write_text("")
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("processor\t: 0\nvendor_id\t: AuthenticAMD\nflags\t\t: fpu lm pni\n")
        monkeypatch.setattr(topology, "CPUINFO", cpuinfo)

        class CrashesInCompute(YCruncherBackend):
            def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
                started = super().get_command(config, work_dir)[0]
                return _child(SEGFAULTS if started == str(compute) else LAUNCHER_AFTER_ITS_CHILD_SEGFAULTED)

        backend = CrashesInCompute()
        backend._binary = str(root / "y-cruncher")
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.CPU_SIGNAL
        assert "SIGSEGV" in verdict.detail

    @pytest.mark.real_cpu_time
    def test_an_instant_clean_exit_is_no_work_done_and_shows_the_tools_last_lines(self, tmp_path):
        supervisor, _, _ = make_supervisor(FakeBackend(_child("print('a\\nb\\nlast-line', flush=True)")))
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.NO_WORK_DONE
        assert "last-line" in verdict.detail

    def test_an_unrecognised_exit_code_is_unexplained(self, tmp_path):
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY_THEN_EXIT_SEVEN)))
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.UNEXPLAINED
        assert "code 7" in verdict.detail

    def test_a_lanes_own_machine_check_outranks_a_thermal_trip(self, tmp_path):
        detector = FakeDetector([[own(0)]])
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), detector=detector, thermal=hot_thermal())
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.OWN_CORE_MCE
        assert "temperature" in verdict.detail

    def test_a_lanes_own_machine_check_outranks_an_unattributed_one_read_first(self, tmp_path):
        detector = FakeDetector([[unblamed(), own(0)]])
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), detector=detector)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.OWN_CORE_MCE

    def test_a_lanes_own_output_error_outranks_an_outside_kill(self, tmp_path):
        supervisor, _, _ = make_supervisor(FakeMprime(FATAL_THEN_SIGKILL))
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "killed externally" in verdict.detail

    def test_every_lane_gets_an_outcome_after_a_launch_failure(self, tmp_path):
        class RefusesLaneZero(FakeBackend):
            def prepare(self, work_dir: Path, config: StressConfig) -> None:
                if work_dir.parent.name == "core_0":
                    raise OSError("read-only work dir")
                super().prepare(work_dir, config)

        supervisor, _, _ = make_supervisor(RefusesLaneZero(_child(BUSY)))
        verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), 1.0)
        assert verdicts[0].outcome is Outcome.NOT_STARTED
        assert verdicts[1].outcome is Outcome.CUT_SHORT

    def test_an_error_printed_on_stderr_is_graded(self, tmp_path):
        supervisor, _, _ = make_supervisor(FakeMprime(FATAL_ON_STDERR))
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "Rounding" in verdict.detail

    @pytest.mark.parametrize(
        ("returncode", "sent", "expected"),
        [
            (0, None, None),
            (-signal.SIGSEGV, None, Outcome.CPU_SIGNAL),
            (-signal.SIGILL, signal.SIGTERM, Outcome.CPU_SIGNAL),
            (-signal.SIGBUS, signal.SIGKILL, Outcome.CPU_SIGNAL),
            (-signal.SIGFPE, None, Outcome.CPU_SIGNAL),
            (-signal.SIGTERM, signal.SIGTERM, None),
            (143, signal.SIGTERM, None),
            (-signal.SIGTERM, signal.SIGKILL, None),
            (-signal.SIGKILL, signal.SIGKILL, None),
            (137, signal.SIGKILL, None),
            (-signal.SIGKILL, None, Outcome.OUTSIDE_KILL),
            (-signal.SIGKILL, signal.SIGTERM, Outcome.OUTSIDE_KILL),
            (137, None, Outcome.OUTSIDE_KILL),
            (137, signal.SIGTERM, Outcome.OUTSIDE_KILL),
            (-signal.SIGTERM, None, Outcome.OUTSIDE_KILL),
            (143, None, Outcome.OUTSIDE_KILL),
            (-signal.SIGABRT, None, Outcome.UNEXPLAINED),
            (-signal.SIGTRAP, signal.SIGTERM, Outcome.UNEXPLAINED),
            (7, None, Outcome.UNEXPLAINED),
            (1, signal.SIGTERM, Outcome.UNEXPLAINED),
            (139, None, Outcome.UNEXPLAINED),
        ],
    )
    def test_exit_signal_maps_each_exit(self, returncode, sent, expected):
        found = exit_signal(returncode, sent)
        assert (found.outcome if found is not None else None) is expected


class TestVerdicts:
    def test_a_process_killed_at_the_deadline_passes(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.PASS
        assert verdict.passed

    def test_an_instant_nonzero_exit_is_unexplained(self, tmp_path):
        backend = FakeBackend(_child("import sys; sys.exit(3)"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.UNEXPLAINED
        assert "code 3" in verdict.detail

    def test_an_instant_exit_that_names_its_environment_fault_keeps_the_name(self, tmp_path):
        class SatPayload(StressapptestBackend):
            def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
                return _child(
                    "import sys; sys.stderr.write('Process Error: freepages < neededpages.\\n'); "
                    "print('Status: FAIL - test encountered procedural errors'); sys.exit(1)"
                )

        supervisor, _, _ = make_supervisor(SatPayload())
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.NOT_STARTED
        assert "freepages < neededpages" in verdict.detail

    def test_a_found_failure_is_attributed(self, tmp_path):
        backend = FakeBackend(
            _child("import time; time.sleep(0.3)"),
            found=(LaneSignal(Outcome.COMPUTATION, "fake error: FATAL"),),
        )
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "FATAL" in verdict.detail

    @pytest.mark.real_cpu_time
    def test_a_clean_exit_that_did_its_work_passes(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(1.5)"))
        supervisor, _, _ = make_supervisor(backend)
        with one_busy_cpu():
            verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.PASS
        assert verdict.duration_seconds < 2.0

    @pytest.mark.real_cpu_time
    def test_a_clean_exit_that_did_no_work_is_not_a_pass(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.3)"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.NO_WORK_DONE
        assert "no work done" in verdict.detail
        assert verdict.duration_seconds < 2.0

    def test_a_results_file_error_beats_a_stop_at_the_deadline(self, tmp_path):
        backend = FakeBackend(
            _child("import time; time.sleep(0.5)"),
            found=(LaneSignal(Outcome.COMPUTATION, "fake error: SUMOUT"),),
        )
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 0.2)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "SUMOUT" in verdict.detail

    def test_an_external_kill_is_an_apparatus_fault_not_a_verdict(self, tmp_path):
        backend = FakeBackend(_child("import os, signal, time; time.sleep(0.1); os.kill(os.getpid(), signal.SIGKILL)"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 4.0)
        assert verdict.outcome is Outcome.OUTSIDE_KILL

    def test_an_interrupted_lane_is_cut_short(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(5)"))
        supervisor, stop, _ = make_supervisor(backend)
        threading.Timer(0.08, stop.set).start()
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.CUT_SHORT
        assert not verdict.passed

    def test_no_stress_process_outlives_the_run(self, tmp_path):
        marker = tmp_path / "pid"
        supervisor, stop, _ = make_supervisor(FakeBackend([*_child(WRITES_ITS_PID_THEN_RUNS), str(marker)]))
        stop_once(stop, marker.exists)
        run_one(supervisor, lane(tmp_path), 10.0)
        pid = int(marker.read_text())
        deadline = time.monotonic() + 3
        alive = True
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        assert not alive

    def test_the_supervisor_never_sets_the_callers_stop(self, tmp_path):
        supervisor, stop, _ = make_supervisor(FakeBackend(_child(BUSY)), thermal=hot_thermal())
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.THERMAL
        assert not stop.is_set()


@pytest.mark.real_cpu_time
class TestLoadProof:
    def test_a_payload_that_never_loads_its_cpus_is_not_a_pass(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.NO_WORK_DONE
        assert "no work done" in verdict.detail

    def test_a_payload_that_loads_its_cpus_passes(self, tmp_path):
        backend = FakeBackend(_child(BUSY))
        supervisor, _, _ = make_supervisor(backend)
        with patch.object(execution, "LOAD_FLOOR_FRACTION", 0.05):
            verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.PASS

    def test_an_unreadable_payload_cpu_time_never_proves_load(self, tmp_path):
        backend = FakeBackend(_child(BUSY))
        supervisor, _, _ = make_supervisor(backend)
        with patch.object(containment, "process_group_cpu_seconds", return_value=None):
            verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict.outcome is Outcome.NO_WORK_DONE
        assert "unreadable" in verdict.detail

    def test_the_cpu_time_the_payload_used_up_to_our_stop_counts(self, tmp_path):
        duration = 2.0
        past_the_deadline = threading.Event()

        def wait_out_the_deadline(_core: int, elapsed: float) -> None:
            time.sleep(max(0.0, duration - elapsed))
            past_the_deadline.set()

        def busy_for_the_window_once_past_the_deadline(pid: int) -> float | None:
            if gone(pid):
                return None
            return duration if past_the_deadline.is_set() else 0.0

        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, hooks=SuperviseHooks(on_status=wait_out_the_deadline))
        with patch.object(
            containment, "process_group_cpu_seconds", side_effect=busy_for_the_window_once_past_the_deadline
        ):
            verdict = run_one(supervisor, lane(tmp_path), duration)
        assert past_the_deadline.is_set()
        assert verdict.outcome is Outcome.PASS


class TestTeardown:
    def test_an_outside_kill_during_our_stop_is_not_ours(self, tmp_path):
        one = lane(tmp_path)
        supervisor, _, _ = make_supervisor(
            FakeBackend(_child(KILLS_ITS_LEADER_ON_SIGTERM)),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/ready"))),
        )
        verdict = run_one(supervisor, one, 2.0)
        assert verdict.outcome is Outcome.OUTSIDE_KILL
        assert "externally" in verdict.detail

    def test_a_payload_that_needs_sigkill_never_passes(self, tmp_path):
        one = lane(tmp_path)
        supervisor, _, _ = make_supervisor(
            FakeBackend(_child(DEAF_TO_SIGTERM)),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/ready"))),
        )
        with patch.object(execution, "STOP_GRACE_SECONDS", 0.2):
            verdict = run_one(supervisor, one, 2.0)
        assert verdict.outcome is Outcome.DID_NOT_STOP
        assert "did not stop" in verdict.detail

    def test_stopping_takes_one_grace_whatever_the_lane_count(self, tmp_path):
        backend = FakeBackend(_child(DEAF_TO_SIGTERM))
        supervisor, stop, _ = make_supervisor(backend)
        lanes = [lane(tmp_path, core, (core,)) for core in range(4)]
        stopped: list[float] = []

        def stop_once_every_lane_ignores_sigterm() -> None:
            within(10.0, lambda: all(any(one.work_dir.glob("run-*/ready")) for one in lanes))
            stopped.append(time.monotonic())
            stop.set()

        with (
            patch.object(execution, "STOP_GRACE_SECONDS", 0.5),
            patch.object(execution, "KILL_WAIT_SECONDS", 0.5),
        ):
            threading.Thread(target=stop_once_every_lane_ignores_sigterm, daemon=True).start()
            verdicts = supervisor.run(lanes, StressConfig(), 30.0)
        returned = time.monotonic()
        assert returned - stopped[0] < 1.5
        assert [verdict.outcome for verdict in verdicts.values()] == [Outcome.DID_NOT_STOP] * 4

    def test_a_payload_child_does_not_outlive_its_lane(self, tmp_path):
        backend = FakeBackend(_child(LEAVES_A_CHILD))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        stray = int((verdict.evidence_dir / "stray.pid").read_text())
        try:
            assert within(1.0, lambda: gone(stray))
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(stray, signal.SIGKILL)

    def test_the_supervisor_never_reaps_a_child_it_did_not_start(self, tmp_path):
        unrelated = subprocess.Popen(_child("import sys; sys.exit(7)"))
        os.waitid(os.P_PID, unrelated.pid, os.WEXITED | os.WNOWAIT)
        supervisor, _, _ = make_supervisor(FakeBackend())
        run_one(supervisor, lane(tmp_path), 0.2)
        assert unrelated.wait(timeout=5) == 7

    def test_a_lane_stopped_before_the_deadline_is_not_graded_after_the_teardown(self, tmp_path):
        duration = 4.0
        backend = ReadsFatal(_child(CORE_0_FAILS_ONCE_CORE_1_IS_READY + SLOW_TO_STOP.format(hold=duration + 0.5)))
        supervisor, _, _ = make_supervisor(backend)
        with patch.object(execution, "STOP_GRACE_SECONDS", duration + 1.5):
            verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), duration)
        assert verdicts[0].outcome is Outcome.COMPUTATION
        assert "FATAL" in verdicts[0].detail
        assert verdicts[1].outcome is Outcome.CUT_SHORT

    def test_a_lane_carries_its_own_time_not_the_teardowns(self, tmp_path):
        hold = 1.0
        one = lane(tmp_path)
        supervisor, _, _ = make_supervisor(
            FakeBackend(_child(SLOW_TO_STOP.format(hold=hold))),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/ready"))),
        )
        began = time.monotonic()
        verdict = run_one(supervisor, one, 2.0)
        returned = time.monotonic()
        assert verdict.outcome is Outcome.PASS
        assert verdict.duration_seconds < returned - began - hold

    def test_a_cut_lane_with_its_own_error_is_still_a_fail(self, tmp_path):
        supervisor, _, _ = make_supervisor(ReadsFatal(_child(CORE_1_FAILS_AND_RUNS_ON_THEN_CORE_0_FAILS)))
        verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), 10.0)
        assert [verdicts[core].outcome for core in (0, 1)] == [Outcome.COMPUTATION] * 2
        assert verdicts[1].duration_seconds < 5.0

    def test_a_stop_after_the_deadline_cuts_nothing(self, tmp_path):
        one = lane(tmp_path)
        supervisor, stop, _ = make_supervisor(
            FakeBackend(_child(MARKS_OUR_SIGTERM_THEN_HOLDS)),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/ready"))),
        )
        stop_once(stop, lambda: any(one.work_dir.glob("run-*/termed")))
        verdict = run_one(supervisor, one, 2.0)
        assert stop.is_set()
        assert verdict.outcome is Outcome.PASS

    def test_an_outside_kill_on_one_lane_is_never_a_batch_pass(self, tmp_path):
        supervisor, _, _ = make_supervisor(FakeBackend(_child(ONLY_CORE_1_IS_KILLED)))
        verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), 10.0)
        assert verdicts[1].outcome is Outcome.OUTSIDE_KILL
        assert verdicts[0].outcome is Outcome.CUT_SHORT


class TestOutputFiles:
    def test_output_printed_before_the_deadline_is_graded(self, tmp_path):
        one = lane(tmp_path)
        supervisor, _, _ = make_supervisor(
            scripted(StressNgBackend, PRINTS_A_FAIL_THEN_RUNS),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/printed"))),
        )
        verdict = run_one(supervisor, one, 2.0)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "verification failed" in verdict.detail

    def test_an_error_printed_mid_run_stops_the_test_within_one_scan(self, tmp_path):
        supervisor, _, _ = make_supervisor(scripted(StressapptestBackend, MISCOMPARE_MID_RUN))
        with patch.object(execution, "OUTPUT_SCAN_INTERVAL", 0.2):
            verdict = run_one(supervisor, lane(tmp_path), 30.0)
        assert verdict.duration_seconds < 3.0
        assert verdict.outcome is Outcome.COMPUTATION
        assert "miscompare" in verdict.detail

    def test_output_beyond_the_pipe_capacity_never_blocks_the_payload(self, tmp_path):
        marker = tmp_path / "wrote-it-all"
        supervisor, stop, _ = make_supervisor(FakeBackend([*_child(FLOODS_ITS_OUTPUT), str(marker)]))
        stop_once(stop, marker.exists)
        run_one(supervisor, lane(tmp_path), 10.0)
        assert marker.exists()

    def test_a_bad_byte_does_not_erase_the_output(self, tmp_path):
        supervisor, _, _ = make_supervisor(scripted(StressNgBackend, BAD_BYTE_THEN_FAIL))
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "fail" in verdict.detail

    def test_a_child_holding_the_output_cannot_hide_the_leaders_text(self, tmp_path):
        supervisor, _, _ = make_supervisor(scripted(StressNgBackend, LEADER_FAILS_WHILE_A_CHILD_HOLDS_THE_OUTPUT))
        verdict = run_one(supervisor, lane(tmp_path), 5.0)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "verification failed" in verdict.detail

    def test_a_failed_tests_logs_stay_and_a_passed_tests_go(self, tmp_path):
        failing = lane(tmp_path, 0)
        supervisor, _, _ = make_supervisor(scripted(StressNgBackend, FAILS_AT_ONCE))
        failed = run_one(supervisor, failing, 10.0)
        assert failed.outcome is Outcome.COMPUTATION
        assert failed.evidence_dir is not None
        assert failed.evidence_dir.parent == failing.work_dir
        assert re.fullmatch(r"run-\d{8}T\d{12}", failed.evidence_dir.name)
        assert FAIL_LINE in (failed.evidence_dir / execution.STDOUT_LOG).read_text()
        assert f"evidence: {failed.evidence_dir}" in failed.detail
        passing = lane(tmp_path, 1, (1,))
        supervisor, _, _ = make_supervisor(
            FakeBackend(_child(WRITES_IN_ITS_CWD)),
            hooks=holds_the_deadline_until(lambda: any(passing.work_dir.glob("run-*/scratch"))),
        )
        passed = run_one(supervisor, passing, 2.0)
        assert passed.outcome is Outcome.PASS
        assert passed.evidence_dir is None
        assert "evidence" not in passed.detail
        assert list(passing.work_dir.rglob("*")) == []

    def test_an_unreadable_output_log_is_never_a_pass(self, tmp_path):
        one = lane(tmp_path)
        supervisor, _, _ = make_supervisor(
            FakeBackend(_child(REMOVES_ITS_OWN_LOG)),
            hooks=holds_the_deadline_until(lambda: any(one.work_dir.glob("run-*/removed"))),
        )
        verdict = run_one(supervisor, one, 2.0)
        assert verdict.outcome is Outcome.UNVERIFIED
        assert "stdout.log" in verdict.detail

    def test_a_passed_run_dir_that_cannot_be_removed_is_logged_as_an_error(self, tmp_path, caplog):
        supervisor, _, _ = make_supervisor(FakeBackend(_child(WRITES_IN_ITS_CWD)))
        with patch.object(execution.shutil, "rmtree", side_effect=PermissionError("run dir: permission denied")):
            verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.PASS
        assert {r.levelname for r in caplog.records if "permission denied" in r.getMessage()} == {"ERROR"}


class TestMceAttribution:
    def test_an_own_cpu_event_fails_the_lane(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(5)"))
        detector = FakeDetector([[own(16)]])
        supervisor, _, seen = make_supervisor(backend, detector=detector)
        verdict = run_one(supervisor, lane(tmp_path, cpus=(0, 16)), 5.0)
        assert verdict.outcome is Outcome.OWN_CORE_MCE
        assert len(seen) == 1

    def test_a_foreign_event_is_evidence_not_a_verdict(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        detector = FakeDetector([[own(9)]])
        supervisor, _, seen = make_supervisor(backend, detector=detector)
        verdict = run_one(supervisor, lane(tmp_path, cpus=(0, 16)), 2.0)
        assert verdict.outcome is Outcome.PASS
        assert [e.cpu for e in seen] == [9]

    def test_a_lone_machine_check_stops_the_lane_through_the_real_reader(self, tmp_path):
        kmsg = tmp_path / "kmsg"
        kmsg.write_text("")
        reader = ErrorDetector(kmsg)
        reader.reset()

        def log_the_record_once(_core: int, _elapsed: float) -> None:
            if not kmsg.read_text():
                _append(kmsg, _kmsg(LONE_DECODER_RECORD, first_seq=100))

        supervisor, _, seen = make_supervisor(
            FakeBackend(_child(BUSY)), detector=reader, hooks=SuperviseHooks(on_status=log_the_record_once)
        )
        verdict = run_one(supervisor, lane(tmp_path, cpus=(9,)), 10.0)
        reader.close()
        assert verdict.outcome is Outcome.OWN_CORE_MCE
        assert verdict.duration_seconds < 5.0
        assert [type(event) for event in seen] == [CoreMachineCheck]

    def test_late_events_read_after_the_teardown_still_grade_the_lane(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(60)"))
        detector = FakeDetector([[own(0)]])
        supervisor, _, _ = make_supervisor(backend, detector=detector, poll_interval=1.0)
        verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 0.0)
        assert verdict.outcome is Outcome.OWN_CORE_MCE

    def test_the_read_after_the_teardown_is_final(self, tmp_path):
        detector = FakeDetector()
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), detector=detector)
        run_one(supervisor, lane(tmp_path), 0.3)
        assert detector.finals[-1] is True
        assert not any(detector.finals[:-1])

    def test_a_failed_final_kernel_read_is_never_a_pass(self, tmp_path):
        detector = FakeDetector(final_problem="kernel log overran; records lost")
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), detector=detector)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.UNVERIFIED
        assert "kernel log overran; records lost" in verdict.detail

    def test_a_failed_kernel_read_during_the_run_is_never_a_pass(self, tmp_path):
        detector = FakeDetector([KernelRead((), "kernel log /dev/kmsg unreadable: [Errno 1] Operation not permitted")])
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), detector=detector)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.UNVERIFIED
        assert "Operation not permitted" in verdict.detail

    def test_a_lanes_own_machine_check_outranks_a_failed_read(self, tmp_path):
        detector = FakeDetector([KernelRead((own(0),), "kernel log overran; records lost")])
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), detector=detector)
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.OWN_CORE_MCE

    def test_a_shared_bank_error_during_a_batch_blames_no_lane(self, tmp_path):
        shared = UnblamedMachineCheck(logged_by_cpu=0, bank=21, corrected=True, message="UMC", raw_ts=1.0)
        detector = FakeDetector([[shared]])
        supervisor, _, seen = make_supervisor(FakeBackend(_child(BUSY)), detector=detector)
        verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), 2.0)
        assert [verdicts[core].outcome for core in (0, 1)] == [Outcome.UNATTRIBUTED_MCE] * 2
        assert seen == [shared]

    def test_kernel_trouble_during_a_batch_is_its_own_type(self, tmp_path):
        detector = FakeDetector([[trouble()]])
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)), detector=detector)
        verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), 2.0)
        assert [verdicts[core].outcome for core in (0, 1)] == [Outcome.KERNEL_TROUBLE] * 2


@pytest.mark.real_cpu_time
class TestStallWatchdog:
    def test_an_idle_lane_is_failed_as_stalled(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, stall_timeout=0.05)
        with patch.object(execution, "STALL_GRACE_SECONDS", 0.0):
            stalls: list[int] = []
            supervisor.hooks = SuperviseHooks(on_stall=stalls.append)
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict.outcome is Outcome.STALL
        assert stalls == [0]

    def test_a_hung_payload_stalls_even_when_its_cpus_are_busy(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, stall_timeout=0.2)
        totals = itertools.count(100, 100)
        with (
            patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
            patch.object(execution, "cpu_times", side_effect=lambda _cpu: (0, next(totals))),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 3.0)
        assert verdict.outcome is Outcome.STALL

    def test_a_busy_lane_is_not_accused(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, stall_timeout=0.05)
        with patch.object(execution, "STALL_GRACE_SECONDS", 0.0), one_busy_cpu():
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 2.0)
        assert verdict.outcome is Outcome.PASS

    def test_an_unreadable_payload_cpu_time_stalls_the_lane(self, tmp_path):
        backend = FakeBackend(_child(BUSY))
        supervisor, _, _ = make_supervisor(backend, stall_timeout=0.05)
        with (
            patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
            patch.object(containment, "process_group_cpu_seconds", return_value=None),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict.outcome is Outcome.STALL


class TestContainmentWatchdog:
    @pytest.fixture(autouse=True)
    def scope_kills(self):
        with patch.object(containment, "kill_scope") as kill_scope:
            yield kill_scope

    def _contained(self, cpus):
        return containment.Containment(prefix=[], unit="cc-test", scope="/user.slice/cc-test.scope")

    def _run_adopted(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value={0}),
        ):
            return run_one(supervisor, lane(tmp_path, cpus=(0,)), 0.3)

    def test_a_known_scope_is_killed_when_the_lane_stops(self, tmp_path, scope_kills):
        self._run_adopted(tmp_path)
        scope_kills.assert_called_once_with("/user.slice/cc-test.scope")

    def test_a_scope_is_killed_even_when_no_watchdog_tick_saw_it(self, tmp_path, scope_kills):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(execution.Supervisor, "_containment_fault", return_value=None),
        ):
            run_one(supervisor, lane(tmp_path, cpus=(0,)), 0.3)
        scope_kills.assert_called_once_with("/user.slice/cc-test.scope")

    def test_a_scope_that_cannot_be_killed_is_logged_as_an_error(self, tmp_path, scope_kills, caplog):
        scope_kills.side_effect = PermissionError("cgroup.kill: permission denied")
        self._run_adopted(tmp_path)
        scope_kills.assert_called_once_with("/user.slice/cc-test.scope")
        assert {r.levelname for r in caplog.records if "permission denied" in r.getMessage()} == {"ERROR"}

    def test_a_widened_kernel_record_is_a_containment_fault(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value={0, 3, 7}),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict.outcome is Outcome.CONTAINMENT_BROKE
        assert "containment fault" in verdict.detail

    @pytest.mark.real_cpu_time
    def test_a_matching_kernel_record_raises_no_alarm(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value={0}) as records,
            one_busy_cpu("scope_cpu_seconds") as scope_reads,
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 2.0)
        assert verdict.outcome is Outcome.PASS
        records.assert_called_with("/user.slice/cc-test.scope")
        scope_reads.assert_called_with("/user.slice/cc-test.scope")

    def test_a_scope_that_never_adopts_the_payload_is_a_fault(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value=None),
            patch.object(execution, "CONTAINMENT_GRACE_SECONDS", 0.05),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict.outcome is Outcome.CONTAINMENT_BROKE
        assert "never adopted" in verdict.detail

    def test_a_vanished_kernel_record_is_a_fault(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value=None),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict.outcome is Outcome.CONTAINMENT_BROKE
        assert "vanished" in verdict.detail


def found_elsewhere(_pid: int, unit: str) -> str:
    return f"/elsewhere.slice/{unit}.scope"


class TestTheScopeIsKnownFromLaunch:
    LANE_PARENT = "/user.slice/app.slice"

    @pytest.fixture
    def contained_by_the_probe(self):
        probe = containment.Probe(containment.MECHANISM_USER, self.LANE_PARENT)

        def found(key):
            return Resolution(key=key, path=Path("/probed") / key, origin="path")

        with (
            patch.object(containment, "_probe", return_value=probe),
            patch.object(containment.tools, "resolve", side_effect=found),
        ):
            yield lambda cpus: replace(containment.contain(cpus), prefix=[])

    def test_a_contained_lane_whose_leader_is_gone_gets_no_group_sweep(self, tmp_path, contained_by_the_probe):
        swept: list[int] = []
        real_signal_group = Supervisor._signal_group

        def spy(supervisor_self, run, sig):
            if sig == signal.SIGKILL:
                swept.append(run.lane.core_id)
            return real_signal_group(supervisor_self, run, sig)

        backend = FakeBackend(_child(EXITS_AT_ONCE_LEAVING_A_CHILD_IN_ITS_OWN_GROUP))
        supervisor, _, _ = make_supervisor(backend, containment_for=contained_by_the_probe)
        try:
            with patch.object(Supervisor, "_signal_group", spy), patch.object(containment, "kill_scope") as killed:
                run_one(supervisor, lane(tmp_path / "lanes", cpus=(0,)), 5.0)
            assert killed.call_count == 1
            assert swept == []
        finally:
            for member in (tmp_path / "lanes").glob("core_0/run-*/stray.pid"):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(member.read_text()), signal.SIGKILL)

    def test_a_leader_gone_before_any_watchdog_saw_its_scope_still_has_the_scope_killed(
        self, tmp_path, contained_by_the_probe
    ):
        cgroup_fs = tmp_path / "cgroup"
        scopes: list[Path] = []
        kill_scope = containment.kill_scope

        def contained(cpus):
            launched = contained_by_the_probe(cpus)
            scopes.append(cgroup_fs / self.LANE_PARENT.lstrip("/") / f"{launched.unit}.scope")
            scopes[-1].mkdir(parents=True)
            (scopes[-1] / "cgroup.kill").write_text("")
            return launched

        def cgroup_kill(cgroup_path):
            kill_scope(cgroup_path, cgroup_base=cgroup_fs)
            if (scopes[0] / "cgroup.kill").read_text() == "1":
                for member in (tmp_path / "lanes").glob("core_0/run-*/stray.pid"):
                    os.kill(int(member.read_text()), signal.SIGKILL)

        backend = FakeBackend(_child(EXITS_AT_ONCE_LEAVING_A_CHILD_IN_ITS_OWN_GROUP))
        supervisor, _, _ = make_supervisor(backend, containment_for=contained)
        try:
            with patch.object(containment, "kill_scope", side_effect=cgroup_kill):
                run_one(supervisor, lane(tmp_path / "lanes", cpus=(0,)), 5.0)
            stray = int(next((tmp_path / "lanes").glob("core_0/run-*/stray.pid")).read_text())
            assert (scopes[0] / "cgroup.kill").read_text() == "1"
            assert within(2.0, lambda: gone(stray))
        finally:
            for member in (tmp_path / "lanes").glob("core_0/run-*/stray.pid"):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(member.read_text()), signal.SIGKILL)

    def test_a_payload_found_in_a_scope_other_than_the_launched_one_is_a_containment_fault(
        self, tmp_path, contained_by_the_probe
    ):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=contained_by_the_probe)
        with (
            patch.object(containment, "payload_cgroup", side_effect=found_elsewhere),
            patch.object(containment, "scope_effective_cpus", return_value={0}),
            patch.object(containment, "kill_scope"),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 1.0)
        assert verdict.outcome is Outcome.CONTAINMENT_BROKE
        assert "/elsewhere.slice/" in verdict.detail
        assert self.LANE_PARENT in verdict.detail

    def test_a_payload_first_found_elsewhere_at_the_stop_is_a_containment_fault(self, tmp_path, contained_by_the_probe):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=contained_by_the_probe)
        with (
            patch.object(containment, "payload_cgroup", side_effect=found_elsewhere),
            patch.object(execution.Supervisor, "_containment_fault", return_value=None),
            patch.object(containment, "kill_scope"),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 0.3)
        assert verdict.outcome is Outcome.CONTAINMENT_BROKE
        assert "/elsewhere.slice/" in verdict.detail


OOM_EVENTS = "low 0\nhigh 0\nmax 0\noom {kills}\noom_kill {kills}\noom_group_kill 0\n"


class TestOomKillsInTheScope:
    SCOPE = "/user.slice/cc-test.scope"

    @pytest.fixture
    def scope_dir(self, tmp_path):
        cgroup_fs = tmp_path / "cgroup"
        scope = cgroup_fs / self.SCOPE.lstrip("/")
        scope.mkdir(parents=True)
        (scope / "cgroup.procs").write_text("")
        kills, exists = containment.scope_oom_kills, containment.scope_exists
        with (
            patch.object(containment, "scope_oom_kills", side_effect=lambda path: kills(path, cgroup_base=cgroup_fs)),
            patch.object(containment, "scope_exists", side_effect=lambda path: exists(path, cgroup_base=cgroup_fs)),
            patch.object(containment, "payload_cgroup", return_value=self.SCOPE),
            patch.object(containment, "scope_effective_cpus", return_value={0}),
            patch.object(containment, "kill_scope"),
        ):
            yield scope

    def _run(self, tmp_path, duration: float = 2.0):
        backend = FakeBackend(_child("import time; time.sleep(60)"))
        supervisor, _, _ = make_supervisor(
            backend,
            containment_for=lambda cpus: containment.Containment(prefix=[], unit="cc-test", scope=self.SCOPE),
        )
        return run_one(supervisor, lane(tmp_path / "lanes", cpus=(0,)), duration)

    def test_an_oom_kill_in_the_lanes_scope_is_an_outside_kill_that_stops_the_lane(self, tmp_path, scope_dir):
        (scope_dir / "memory.events").write_text(OOM_EVENTS.format(kills=1))
        verdict = self._run(tmp_path, duration=30.0)
        assert verdict.outcome is Outcome.OUTSIDE_KILL
        assert "the kernel's OOM killer killed 1 process(es) in this lane's scope" in verdict.detail
        assert verdict.duration_seconds < 5.0

    def test_a_scope_with_no_oom_kill_passes(self, tmp_path, scope_dir):
        (scope_dir / "memory.events").write_text(OOM_EVENTS.format(kills=0))
        assert self._run(tmp_path).outcome is Outcome.PASS

    def test_a_count_that_appears_only_at_the_stop_is_read_before_the_stop_empties_the_scope(self, tmp_path, scope_dir):
        (scope_dir / "memory.events").write_text(OOM_EVENTS.format(kills=0))
        stop_all, signal_group = execution.Supervisor._stop_all, execution.Supervisor._signal_group

        def killed_as_the_stop_begins(supervisor, runs):
            (scope_dir / "memory.events").write_text(OOM_EVENTS.format(kills=2))
            stop_all(supervisor, runs)

        def collected_once_our_stop_empties_it(supervisor, run, sig):
            if sig == signal.SIGTERM and scope_dir.exists():
                shutil.rmtree(scope_dir)
            signal_group(supervisor, run, sig)

        with (
            patch.object(execution.Supervisor, "_stop_all", killed_as_the_stop_begins),
            patch.object(execution.Supervisor, "_signal_group", collected_once_our_stop_empties_it),
        ):
            verdict = self._run(tmp_path)
        assert verdict.outcome is Outcome.OUTSIDE_KILL
        assert "killed 2 process(es)" in verdict.detail

    def test_a_kill_the_watchdog_counted_stays_counted_when_the_counter_restarts(self, tmp_path, scope_dir):
        (scope_dir / "memory.events").write_text(OOM_EVENTS.format(kills=3))
        stop_all = execution.Supervisor._stop_all

        def restarted_as_the_stop_begins(supervisor, runs):
            (scope_dir / "memory.events").write_text(OOM_EVENTS.format(kills=0))
            stop_all(supervisor, runs)

        with patch.object(execution.Supervisor, "_stop_all", restarted_as_the_stop_begins):
            verdict = self._run(tmp_path, duration=30.0)
        assert verdict.outcome is Outcome.OUTSIDE_KILL
        assert "killed 3 process(es)" in verdict.detail

    def test_an_unreadable_count_in_a_live_scope_is_never_a_pass(self, tmp_path, scope_dir):
        verdict = self._run(tmp_path)
        assert verdict.outcome is Outcome.UNVERIFIED
        assert "memory.events" in verdict.detail
        assert "verdict unavailable" in verdict.detail

    def test_a_scope_already_gone_is_no_read_problem(self, tmp_path, scope_dir):
        shutil.rmtree(scope_dir)
        assert self._run(tmp_path).outcome is Outcome.PASS


class TestThermalGuard:
    def test_a_missing_sensor_fails_closed_when_required(self):
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=3,
            hard_margin=8,
            require_sensor=True,
            read=lambda: None,
        )
        assert watch.safe() is False

    def test_a_missing_sensor_is_tolerated_when_optional(self):
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=3,
            hard_margin=8,
            require_sensor=False,
            read=lambda: None,
        )
        assert watch.safe() is True

    def test_a_brief_overshoot_is_granted_grace(self):
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=60,
            hard_margin=8,
            require_sensor=False,
            read=lambda: 96.0,
        )
        assert watch.safe() is True

    def test_a_sustained_overshoot_trips(self):
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=0.0,
            hard_margin=8,
            require_sensor=False,
            read=lambda: 96.0,
        )
        assert watch.safe() is False
        assert watch.tripped

    def test_recovery_before_grace_resets_the_window(self):
        temps = iter([96.0, 40.0, 96.0])
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=60,
            hard_margin=8,
            require_sensor=False,
            read=lambda: next(temps),
        )
        assert watch.safe() is True
        assert watch.safe() is True
        assert watch.safe() is True
        assert watch._over_since is not None

    def test_the_hard_ceiling_trips_immediately(self):
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=60,
            hard_margin=8,
            require_sensor=False,
            read=lambda: 103.5,
        )
        assert watch.safe() is False
        assert watch.tripped

    def test_hysteresis_governs_resume(self):
        temps = iter([104.0, 91.0, 89.0])
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=0,
            hard_margin=8,
            require_sensor=False,
            read=lambda: next(temps),
        )
        assert watch.safe() is False
        assert watch.safe() is False
        assert watch.safe() is True

    def test_a_trip_is_a_batch_outcome_and_fires_the_hook(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(5)"))
        seen_temps: list[float] = []
        supervisor, _, _ = make_supervisor(
            backend, thermal=hot_thermal(), hooks=SuperviseHooks(on_thermal=seen_temps.append)
        )
        verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), 5.0)
        assert [verdicts[core].outcome for core in (0, 1)] == [Outcome.THERMAL] * 2
        assert seen_temps == [104.0]

    def test_a_trip_reaches_a_lane_that_had_already_ended(self, tmp_path):
        alive_by_tick: dict[float, set[int]] = {}

        def hot_once_core_0_has_ended() -> float:
            return 104.0 if alive_by_tick and alive_by_tick[max(alive_by_tick)] == {1} else 40.0

        thermal = ThermalWatch(
            max_temperature=95,
            grace_seconds=0,
            hard_margin=8,
            require_sensor=False,
            read=hot_once_core_0_has_ended,
        )
        supervisor, _, _ = make_supervisor(
            FakeBackend(_child(CORE_0_ENDS_AT_ONCE)),
            thermal=thermal,
            hooks=SuperviseHooks(on_status=lambda core, elapsed: alive_by_tick.setdefault(elapsed, set()).add(core)),
        )
        verdicts = supervisor.run(two_lanes(tmp_path), StressConfig(), 10.0)
        assert [verdicts[core].outcome for core in (0, 1)] == [Outcome.THERMAL] * 2


class TestTemperatureSource:
    def test_an_absent_hwmon_tree_reads_as_no_sensor(self, tmp_path):
        with patch.object(execution, "Path", lambda _p: tmp_path / "nope"):
            assert execution.read_cpu_temperature() is None

    def test_the_hottest_matching_sensor_wins(self, tmp_path):
        hw = tmp_path / "hwmon0"
        hw.mkdir()
        (hw / "name").write_text("k10temp\n")
        (hw / "temp1_input").write_text("65000\n")
        (hw / "temp2_input").write_text("72500\n")
        foreign = tmp_path / "hwmon1"
        foreign.mkdir()
        (foreign / "name").write_text("nvme\n")
        (foreign / "temp1_input").write_text("99000\n")
        with patch.object(execution, "Path", lambda _p: tmp_path):
            assert execution.read_cpu_temperature() == 72.5

    def test_a_garbled_input_is_skipped(self, tmp_path):
        hw = tmp_path / "hwmon0"
        hw.mkdir()
        (hw / "name").write_text("zenpower\n")
        (hw / "temp1_input").write_text("garbage\n")
        with patch.object(execution, "Path", lambda _p: tmp_path):
            assert execution.read_cpu_temperature() is None


class TestWatchIdle:
    def _args(self, **overrides):
        args = dict(
            cpus=(0,),
            duration=0.05,
            thermal=cool_thermal(),
            detector=FakeDetector(),
            stop_event=threading.Event(),
            observed=[],
            phase="idle stability",
            poll_interval=0.01,
        )
        args.update(overrides)
        return args

    def test_a_quiet_idle_reports_nothing(self):
        assert watch_idle(**self._args()) is None

    def test_a_stop_ends_the_idle_immediately(self):
        stop = threading.Event()
        stop.set()
        detector = FakeDetector()
        start = time.monotonic()
        assert watch_idle(**self._args(duration=5.0, stop_event=stop, detector=detector)) is None
        assert time.monotonic() - start < 1.0
        assert detector.finals == [True]

    def test_an_overheat_during_idle_is_thermal_and_leaves_the_stop_to_the_caller(self):
        stop = threading.Event()
        found = watch_idle(**self._args(thermal=hot_thermal(), duration=5.0, stop_event=stop))
        assert found is not None and found.outcome is Outcome.THERMAL
        assert "temperature" in found.text
        assert not stop.is_set()

    def test_an_own_core_record_outranks_a_thermal_trip_during_idle(self):
        detector = FakeDetector([[own(0)]])
        seen: list = []
        found = watch_idle(**self._args(thermal=hot_thermal(), detector=detector, observed=seen, duration=5.0))
        assert found is not None and found.outcome is Outcome.OWN_CORE_MCE
        assert [e.cpu for e in seen] == [0]
        assert detector.finals == [True]

    def test_a_signal_mid_idle_returns_only_after_a_final_read(self):
        detector = FakeDetector([[trouble()], [own(0)]])
        found = watch_idle(**self._args(detector=detector, duration=5.0))
        assert found is not None and found.outcome is Outcome.OWN_CORE_MCE
        assert detector.finals == [False, True]

    def test_an_own_cpu_mce_ends_the_idle(self):
        found = watch_idle(**self._args(detector=FakeDetector([[own(0)]]), duration=5.0))
        assert found is not None and found.outcome is Outcome.OWN_CORE_MCE
        assert "MCE during idle stability" in found.text

    def test_an_unattributed_mce_keeps_its_own_type(self):
        found = watch_idle(**self._args(detector=FakeDetector([[unblamed()]]), duration=5.0))
        assert found is not None and found.outcome is Outcome.UNATTRIBUTED_MCE

    def test_the_strongest_event_of_a_read_is_reported(self):
        found = watch_idle(**self._args(detector=FakeDetector([[unblamed(), own(0)]]), duration=5.0))
        assert found is not None and found.outcome is Outcome.OWN_CORE_MCE

    def test_a_foreign_mce_is_recorded_not_reported(self):
        seen: list = []
        found = watch_idle(**self._args(detector=FakeDetector([[own(9)]]), observed=seen))
        assert found is None
        assert [e.cpu for e in seen] == [9]

    def test_an_idle_watch_reports_kernel_trouble_as_its_own_type(self):
        found = watch_idle(**self._args(detector=FakeDetector([[trouble()]]), duration=5.0))
        assert found is not None and found.outcome is Outcome.KERNEL_TROUBLE
        assert "soft lockup" in found.text

    def test_a_failed_idle_read_is_unverified(self):
        detector = FakeDetector([KernelRead((), "kernel log overran; records lost")])
        found = watch_idle(**self._args(detector=detector, duration=5.0))
        assert found is not None and found.outcome is Outcome.UNVERIFIED
        assert "records lost" in found.text

    def test_the_last_idle_read_is_final(self):
        detector = FakeDetector(final_problem="kernel log overran; records lost")
        found = watch_idle(**self._args(detector=detector))
        assert found is not None and found.outcome is Outcome.UNVERIFIED
        assert detector.finals[-1] is True
        assert not any(detector.finals[:-1])


class TestPreexec:
    def test_the_preexec_resolves_no_library_after_the_fork(self, monkeypatch):
        preexec = execution.make_preexec()

        def resolved_after_the_fork(_name):
            raise AssertionError("find_library ran in the forked child")

        monkeypatch.setattr(ctypes.util, "find_library", resolved_after_the_fork)
        done = subprocess.run(_child("print(1)"), preexec_fn=preexec, capture_output=True, text=True, check=False)
        assert done.stdout == "1\n"

    def test_a_payload_may_not_take_realtime_priority(self):
        done = subprocess.run(
            _child("import resource; print(resource.getrlimit(resource.RLIMIT_RTPRIO))"),
            preexec_fn=execution.make_preexec(),
            capture_output=True,
            text=True,
            check=True,
        )
        assert done.stdout == "(0, 0)\n"

    def test_the_preexec_drops_the_realtime_limit_to_zero(self, monkeypatch):
        limits = []
        monkeypatch.setattr(os, "setsid", lambda: None)
        monkeypatch.setattr(resource, "setrlimit", lambda which, value: limits.append((which, value)))
        execution.make_preexec()()
        assert limits == [(resource.RLIMIT_RTPRIO, (0, 0))]

    def test_a_failed_parent_death_signal_raises_in_the_child(self, monkeypatch):
        class RefusesPrctl:
            def prctl(self, *_args):
                return -1

        monkeypatch.setattr(execution, "_libc", RefusesPrctl)
        preexec = execution.make_preexec()
        monkeypatch.setattr(os, "setsid", lambda: None)
        monkeypatch.setattr(resource, "setrlimit", lambda _which, _value: None)
        with pytest.raises(OSError, match="PR_SET_PDEATHSIG"):
            preexec()

    def test_a_failed_parent_death_signal_refuses_the_launch(self, tmp_path, monkeypatch):
        class RefusesPrctl:
            def prctl(self, *_args):
                return -1

        monkeypatch.setattr(execution, "_libc", RefusesPrctl)
        supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)))
        verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict.outcome is Outcome.NOT_STARTED
        assert "Failed to start stress test" in verdict.detail


class TestHelpers:
    def test_cpu_times_reads_a_real_cpu(self):
        sample = cpu_times(0)
        assert sample is not None and sample[1] >= sample[0]

    def test_cpu_times_tolerates_an_absent_cpu(self):
        assert cpu_times(99999) is None

    def test_busy_fraction_needs_two_samples(self):
        assert busy_fraction(None, (1, 2)) is None
        assert busy_fraction((1, 2), None) is None

    def test_busy_fraction_needs_forward_progress(self):
        assert busy_fraction((10, 100), (10, 100)) is None

    def test_busy_fraction_is_the_non_idle_share(self):
        assert busy_fraction((0, 0), (50, 100)) == 0.5


class TestContainment:
    def test_cpu_list_sorts_and_dedupes(self):
        assert containment.cpu_list({16, 0}) == "0,16"

    def test_contain_refuses_an_empty_cpu_set(self):
        with pytest.raises(containment.ContainmentUnavailable):
            containment.contain(())

    def test_contain_refuses_when_no_mechanism_probes(self):
        with (
            patch.object(containment, "available_mechanism", return_value=None),
            pytest.raises(containment.ContainmentUnavailable, match="refusing"),
        ):
            containment.contain((0,))

    @pytest.fixture
    def user_tools(self, tmp_path):
        found = {"systemd-run": tmp_path / "systemd-run", "setpriv": tmp_path / "setpriv"}
        for tool in found.values():
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)

        def fake_resolve(key):
            return Resolution(key=key, path=found.get(key), origin="path")

        with (
            patch.object(containment.tools, "resolve", side_effect=fake_resolve),
            patch.object(containment, "available_mechanism", return_value=containment.MECHANISM_USER),
        ):
            yield found

    def test_contain_builds_a_user_scope_prefix(self, user_tools):
        with patch.object(containment, "lane_cgroup_parent", return_value="/user.slice/app.slice"):
            prefix = containment.contain((16, 0))
        assert prefix.prefix[0] == str(user_tools["systemd-run"])
        assert "--user" in prefix.prefix
        assert "--unit" in prefix.prefix
        assert prefix.unit.startswith("corecycler-")
        assert "AllowedCPUs=0,16" in prefix.prefix
        assert prefix.prefix[-1] == "--"
        assert str(user_tools["setpriv"]) in prefix.prefix
        assert "--pdeathsig" in prefix.prefix

    @pytest.mark.parametrize(
        ("lane_parent", "scope_parent"), [("/user.slice/app.slice", "/user.slice/app.slice/"), ("/", "/")]
    )
    def test_contain_names_the_scope_under_the_probed_lane_cgroup(self, user_tools, lane_parent, scope_parent):
        with patch.object(containment, "lane_cgroup_parent", return_value=lane_parent):
            contained = containment.contain((0,))
        assert contained.scope == f"{scope_parent}{contained.unit}.scope"

    def test_contain_refuses_when_the_probe_knows_no_lane_cgroup(self, user_tools):
        with (
            patch.object(containment, "lane_cgroup_parent", return_value=None),
            pytest.raises(containment.ContainmentUnavailable, match="no cgroup v2 path"),
        ):
            containment.contain((0,))

    def test_probe_failure_is_cached_but_refreshable(self):
        containment._probe_cache.clear()
        calls = []

        def fake_probe():
            calls.append(1)
            return containment.Probe(None, None)

        with patch.object(containment, "_probe_mechanism", side_effect=fake_probe):
            assert containment.available_mechanism() is None
            assert containment.available_mechanism() is None
            assert containment.available_mechanism(refresh=True) is None
            assert containment.lane_cgroup_parent() is None
        containment._probe_cache.clear()
        assert len(calls) == 2

    def test_the_lane_cgroup_comes_from_the_same_probe(self):
        containment._probe_cache.clear()
        probe = containment.Probe(containment.MECHANISM_SYSTEM, "/system.slice")
        with patch.object(containment, "_probe_mechanism", return_value=probe):
            assert containment.lane_cgroup_parent() == "/system.slice"
            assert containment.available_mechanism() == containment.MECHANISM_SYSTEM
        containment._probe_cache.clear()

    def test_observed_tree_includes_children(self, tmp_path):
        parent = tmp_path / "100"
        (parent / "task" / "100").mkdir(parents=True)
        (parent / "task" / "100" / "status").write_text("Cpus_allowed_list:\t0\n")
        (parent / "stat").write_text("100 (x) S 1 100 100 0 -1 0 0\n")
        child = tmp_path / "101"
        (child / "task" / "101").mkdir(parents=True)
        (child / "task" / "101" / "status").write_text("Cpus_allowed_list:\t4-5\n")
        (child / "stat").write_text("101 (y) S 100 101 101 0 -1 0 0\n")
        assert containment.observed_tree_cpus(100, proc_base=tmp_path) == {0, 4, 5}

    def test_an_unreadable_proc_observes_nothing(self, tmp_path):
        assert containment.observed_tree_cpus(4242, proc_base=tmp_path / "gone") == set()

    @pytest.mark.real_cpu_time
    def test_scope_cpu_seconds_reads_usage_usec(self, tmp_path):
        scope = tmp_path / "user.slice" / "cc-test.scope"
        scope.mkdir(parents=True)
        (scope / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 2000000\nsystem_usec 500000\n")
        assert containment.scope_cpu_seconds("/user.slice/cc-test.scope", cgroup_base=tmp_path) == 2.5

    @pytest.mark.real_cpu_time
    def test_a_cpu_stat_without_usage_proves_nothing(self, tmp_path):
        scope = tmp_path / "cc-test.scope"
        scope.mkdir()
        (scope / "cpu.stat").write_text("user_usec 2000000\nsystem_usec 500000\n")
        assert containment.scope_cpu_seconds("/cc-test.scope", cgroup_base=tmp_path) is None
        (scope / "cpu.stat").write_text("usage_usec many\n")
        assert containment.scope_cpu_seconds("/cc-test.scope", cgroup_base=tmp_path) is None
        assert containment.scope_cpu_seconds("/gone.scope", cgroup_base=tmp_path) is None

    def test_scope_oom_kills_reads_the_oom_kill_counter(self, tmp_path):
        scope = tmp_path / "user.slice" / "cc-test.scope"
        scope.mkdir(parents=True)
        (scope / "memory.events").write_text("low 0\nhigh 0\nmax 4\noom 3\noom_kill 2\noom_group_kill 1\n")
        assert containment.scope_oom_kills("/user.slice/cc-test.scope", cgroup_base=tmp_path) == 2

    def test_a_memory_events_without_a_kill_count_proves_nothing(self, tmp_path):
        scope = tmp_path / "cc-test.scope"
        scope.mkdir()
        (scope / "memory.events").write_text("low 0\nhigh 0\nmax 0\noom 0\noom_group_kill 0\n")
        assert containment.scope_oom_kills("/cc-test.scope", cgroup_base=tmp_path) is None
        (scope / "memory.events").write_text("oom_kill many\n")
        assert containment.scope_oom_kills("/cc-test.scope", cgroup_base=tmp_path) is None
        assert containment.scope_oom_kills("/gone.scope", cgroup_base=tmp_path) is None

    def test_a_scope_exists_while_its_cgroup_procs_reads(self, tmp_path):
        scope = tmp_path / "cc-test.scope"
        scope.mkdir()
        (scope / "cgroup.procs").write_text("")
        assert containment.scope_exists("/cc-test.scope", cgroup_base=tmp_path) is True
        assert containment.scope_exists("/gone.scope", cgroup_base=tmp_path) is False

    @pytest.mark.parametrize(
        ("subtree_control", "refused"),
        [("cpu io memory pids\n", False), ("cpu io pids\n", True), ("", True), (None, True)],
    )
    def test_the_lane_cgroup_must_give_its_scopes_the_memory_controller(self, tmp_path, subtree_control, refused):
        parent = tmp_path / "user.slice" / "app.slice"
        parent.mkdir(parents=True)
        if subtree_control is not None:
            (parent / "cgroup.subtree_control").write_text(subtree_control)
        with patch.object(containment, "lane_cgroup_parent", return_value="/user.slice/app.slice"):
            problem = containment.memory_accounting_problem(cgroup_base=tmp_path)
        if refused:
            assert "/user.slice/app.slice" in problem
            assert "OOM kill" in problem
        else:
            assert problem is None

    def test_an_unknown_lane_cgroup_cannot_count_oom_kills(self, tmp_path):
        with patch.object(containment, "lane_cgroup_parent", return_value=None):
            problem = containment.memory_accounting_problem(cgroup_base=tmp_path)
        assert "no cgroup v2 path" in problem
        assert "OOM kill" in problem

    @pytest.mark.real_cpu_time
    def test_process_group_cpu_seconds_sums_only_the_group(self, tmp_path):
        def proc_stat(pid: int, comm: str, pgrp: int, ticks: tuple[int, int, int, int]) -> None:
            (tmp_path / str(pid)).mkdir()
            used = " ".join(str(t) for t in ticks)
            (tmp_path / str(pid) / "stat").write_text(
                f"{pid} ({comm}) R 1 {pgrp} {pgrp} 0 -1 4194304 0 0 0 0 {used} 20 0 1 0\n"
            )

        proc_stat(100, "mprime", 100, (300, 20, 50, 30))
        proc_stat(101, "a) R 1 200 (b", 100, (100, 0, 0, 0))
        proc_stat(200, "other", 200, (9000, 9000, 0, 0))
        hz = os.sysconf("SC_CLK_TCK")
        assert containment.process_group_cpu_seconds(100, proc_base=tmp_path) == pytest.approx(500 / hz)

    def test_kill_scope_writes_cgroup_kill(self, tmp_path):
        scope = tmp_path / "user.slice" / "cc-test.scope"
        scope.mkdir(parents=True)
        (scope / "cgroup.kill").write_text("")
        containment.kill_scope("/user.slice/cc-test.scope", cgroup_base=tmp_path)
        assert (scope / "cgroup.kill").read_text() == "1"

    def test_kill_scope_of_a_vanished_scope_is_done(self, tmp_path):
        assert containment.kill_scope("/user.slice/gone.scope", cgroup_base=tmp_path) is None

    def test_kill_scope_surfaces_any_other_failure(self, tmp_path):
        (tmp_path / "cc-test.scope" / "cgroup.kill").mkdir(parents=True)
        with pytest.raises(IsADirectoryError):
            containment.kill_scope("/cc-test.scope", cgroup_base=tmp_path)

    @pytest.mark.real_cpu_time
    def test_a_process_group_with_no_readable_member_proves_nothing(self, tmp_path):
        (tmp_path / "300" / "stat").mkdir(parents=True)
        (tmp_path / "301").mkdir()
        (tmp_path / "301" / "stat").write_text("301 (short) R 1 300\n")
        assert containment.process_group_cpu_seconds(300, proc_base=tmp_path) is None


class ReportsSpeeds(FakeBackend):
    def speeds(self, output: str) -> tuple[tuple[str, float, bool], ...]:
        lines = (line.split() for line in output.splitlines() if line.startswith("SPEED"))
        return tuple((name, float(value), True) for _, name, value in lines)


def test_a_passing_lane_keeps_its_speeds_after_its_run_dir_is_gone(tmp_path):
    passing = lane(tmp_path, 1, (1,))
    script = "print('SPEED sft 6.5e9', flush=True)\n" + BUSY
    supervisor, _, _ = make_supervisor(ReportsSpeeds(_child(script)))
    passed = run_one(supervisor, passing, 2.0)
    assert passed.outcome is Outcome.PASS
    assert passed.speeds == (("sft", 6.5e9, True),)
    assert list(passing.work_dir.rglob("*")) == []


def test_a_run_directory_is_handed_back_to_the_invoking_user(tmp_path, monkeypatch):
    handed: list = []
    monkeypatch.setattr(execution, "hand_back", handed.append)
    passing = lane(tmp_path, 1, (1,))
    supervisor, _, _ = make_supervisor(FakeBackend(_child(BUSY)))
    run_one(supervisor, passing, 1.0)
    assert len(handed) == 1 and handed[0].parent == passing.work_dir and handed[0].name.startswith("run-")
