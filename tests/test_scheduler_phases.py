"""CoreScheduler phase loops driven hermetically — no stress process is spawned.

test_scheduler.py covers the happy paths; this file drives the branches a
healthy developer box never reaches: thermal trips, stalls, external kills,
late MCE drains, affinity drift, idle and variable-load segments, and the
rapid-transition cycle. Every process is a stand-in and every signal is
intercepted, so nothing outside the test is ever killed.
"""

from __future__ import annotations

import itertools
import os
import subprocess
from unittest.mock import MagicMock

import pytest

from corecycler.engine import scheduler as sched
from corecycler.engine.backends.base import StressConfig, StressMode
from corecycler.engine.detector import MCEEvent
from corecycler.engine.scheduler import CoreScheduler, SchedulerConfig, TestState
from corecycler.engine.topology import CPUTopology, PhysicalCore


def _topo(cores=2, smt=False):
    topo = CPUTopology(physical_cores=cores, smt_enabled=smt)
    width = 2 if smt else 1
    topo.logical_cpus_count = cores * width
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(
            core_id=cid,
            ccd=0,
            ccx=None,
            logical_cpus=tuple(range(cid * width, (cid + 1) * width)),
        )
    return topo


def _backend(parse=(True, None), poll_error=None):
    backend = MagicMock()
    backend.parse_output.return_value = parse
    backend.poll_errors.return_value = poll_error
    backend.get_command.return_value = ["true"]
    return backend


def _sched(tmp_path, *, backend=None, topo=None, **cfg):
    defaults = {
        "seconds_per_core": 1,
        "poll_interval": 0.001,
        "cores_to_test": [0],
        "stall_timeout": 30.0,
    }
    defaults.update(cfg)
    runner = CoreScheduler(
        topology=topo or _topo(),
        backend=backend or _backend(),
        stress_config=StressConfig(mode=StressMode.SSE),
        scheduler_config=SchedulerConfig(**defaults),
        work_dir=tmp_path,
    )
    runner._check_temperature = lambda: True
    runner._read_core_usage = lambda _cpu: 100.0
    runner._verify_child_affinity = lambda *_a, **_kw: (True, 0)
    return runner


def _proc(*, poll=None, returncode=-15, out="", err="", pid=4242):
    proc = MagicMock()
    if isinstance(poll, list):
        proc.poll.side_effect = itertools.chain(poll, itertools.repeat(poll[-1]))
    else:
        proc.poll.return_value = poll
    proc.returncode = returncode
    proc.communicate.return_value = (out, err)
    proc.pid = pid
    proc.stdout = MagicMock()
    proc.stderr = MagicMock()
    return proc


def _mce(cpu=0, message="Machine check: L1 cache"):
    return MCEEvent(timestamp=1.0, cpu=cpu, bank=5, message=message, corrected=True)


@pytest.fixture(autouse=True)
def never_signal_the_test_runner(monkeypatch):
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda _pgid, _sig: None)


@pytest.fixture
def instant(monkeypatch):
    monkeypatch.setattr(sched.time, "sleep", lambda _s: None)


class TestRunLoop:
    def test_a_stop_mid_cycle_ends_every_remaining_core_and_cycle(self, tmp_path, instant):
        runner = _sched(
            tmp_path, cores_to_test=[0, 1, 2], cycle_count=2, idle_between_cores=0.001
        )
        runner.topology = _topo(3)
        runner._init_core_status()
        cycles = []
        runner.on_cycle_complete = [cycles.append]
        tested = []

        def _fake_test(core_id, cycle):
            tested.append((core_id, cycle))
            if len(tested) == 2:
                runner._stop_event.set()

        runner._test_core = _fake_test
        runner.run()
        assert tested == [(0, 0), (1, 0)]
        assert cycles == [0]
        assert runner.state is TestState.FINISHED


class TestMcePolling:
    def test_no_events_is_no_error(self, tmp_path):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        assert runner._poll_mce({0}, "stress") is None

    def test_an_own_core_event_fails_the_test(self, tmp_path):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=0)]
        assert "MCE during stress" in runner._poll_mce({0}, "stress")
        assert len(runner.observed_mce) == 1

    def test_an_unattributed_event_fails_the_test(self, tmp_path):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=-1)]
        assert runner._poll_mce({0}, "stress") is not None

    def test_a_foreign_event_is_evidence_not_a_verdict(self, tmp_path):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=9)]
        assert runner._poll_mce({0}, "stress") is None
        assert runner.observed_mce[0].cpu == 9


def _hwmon_root(monkeypatch, root):
    real = sched.Path
    monkeypatch.setattr(
        sched, "Path", lambda p: root if str(p) == "/sys/class/hwmon" else real(p)
    )


class TestTemperatureSource:
    def test_an_absent_hwmon_tree_reads_as_no_sensor(self, monkeypatch, tmp_path):
        _hwmon_root(monkeypatch, tmp_path / "missing")
        assert CoreScheduler._read_cpu_temperature() is None

    def test_an_unlistable_hwmon_tree_reads_as_no_sensor(self, monkeypatch, tmp_path):
        blocker = tmp_path / "hwmon"
        blocker.write_text("not a directory")
        _hwmon_root(monkeypatch, blocker)
        assert CoreScheduler._read_cpu_temperature() is None

    def test_an_unreadable_name_file_is_skipped(self, monkeypatch, tmp_path):
        root = tmp_path / "hwmon"
        (root / "hwmon0" / "name").mkdir(parents=True)
        _hwmon_root(monkeypatch, root)
        assert CoreScheduler._read_cpu_temperature() is None

    def test_a_nameless_hwmon_node_is_skipped(self, monkeypatch, tmp_path):
        root = tmp_path / "hwmon"
        (root / "hwmon0").mkdir(parents=True)
        _hwmon_root(monkeypatch, root)
        assert CoreScheduler._read_cpu_temperature() is None

    def test_a_foreign_hwmon_node_is_skipped(self, monkeypatch, tmp_path):
        root = tmp_path / "hwmon"
        node = root / "hwmon0"
        node.mkdir(parents=True)
        (node / "name").write_text("acpitz\n")
        (node / "temp1_input").write_text("45000\n")
        _hwmon_root(monkeypatch, root)
        assert CoreScheduler._read_cpu_temperature() is None

    def test_a_garbled_temperature_input_is_skipped(self, monkeypatch, tmp_path):
        root = tmp_path / "hwmon"
        node = root / "hwmon0"
        node.mkdir(parents=True)
        (node / "name").write_text("k10temp\n")
        (node / "temp1_input").write_text("not a number\n")
        _hwmon_root(monkeypatch, root)
        assert CoreScheduler._read_cpu_temperature() is None

    def test_the_hottest_sensor_wins(self, monkeypatch, tmp_path):
        root = tmp_path / "hwmon"
        node = root / "hwmon0"
        node.mkdir(parents=True)
        (node / "name").write_text("k10temp\n")
        (node / "temp1_input").write_text("65000\n")
        (node / "temp2_input").write_text("81500\n")
        _hwmon_root(monkeypatch, root)
        assert CoreScheduler._read_cpu_temperature() == pytest.approx(81.5)


class TestThermalGuard:
    def test_a_trip_notifies_every_subscriber_once(self, tmp_path):
        runner = _sched(tmp_path, max_temperature=85.0)
        seen = []
        runner.on_thermal_throttle = [seen.append]
        runner._trip_thermal(99.0)
        runner._trip_thermal(99.5)
        assert seen == [99.0]

    def test_a_tripped_scheduler_stays_tripped_inside_hysteresis(self, tmp_path):
        runner = _sched(tmp_path, max_temperature=85.0, over_temp_hard_margin=10.0)
        runner._check_temperature = CoreScheduler._check_temperature.__get__(runner)
        runner._read_cpu_temperature = staticmethod(lambda: 86.0)
        runner._thermal_tripped = True
        assert runner._check_temperature() is False


class TestAffinityVerification:
    def _tree(self, tmp_path, tids):
        base = tmp_path / "proc"
        task = base / "77" / "task"
        task.mkdir(parents=True)
        for name, allowed in tids.items():
            tid_dir = task / name
            tid_dir.mkdir()
            if allowed is not None:
                (tid_dir / "status").write_text(f"Name:\tstress\nCpus_allowed_list:\t{allowed}\n")
        return base

    def test_a_missing_task_dir_is_lenient(self, tmp_path):
        assert CoreScheduler._verify_child_affinity(
            77, {0}, "0", proc_base=tmp_path / "proc"
        ) == (True, 0)

    def test_an_unlistable_task_dir_is_lenient(self, tmp_path):
        base = tmp_path / "proc" / "77"
        base.mkdir(parents=True)
        (base / "task").write_text("not a directory")
        assert CoreScheduler._verify_child_affinity(
            77, {0}, "0", proc_base=tmp_path / "proc"
        ) == (True, 0)

    def test_a_non_numeric_task_entry_is_skipped(self, tmp_path):
        base = self._tree(tmp_path, {"notatid": "0"})
        assert CoreScheduler._verify_child_affinity(77, {0}, "0", proc_base=base) == (True, 0)

    def test_a_thread_without_a_status_file_is_skipped(self, tmp_path):
        base = self._tree(tmp_path, {"77": None})
        assert CoreScheduler._verify_child_affinity(77, {0}, "0", proc_base=base) == (True, 0)

    def test_a_pinned_thread_needs_no_repin(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(os, "sched_setaffinity", lambda *a: calls.append(a))
        base = self._tree(tmp_path, {"77": "0-1"})
        assert CoreScheduler._verify_child_affinity(77, {0, 1}, "0,1", proc_base=base) == (
            True,
            0,
        )
        assert calls == []

    def test_a_drifted_thread_is_repinned(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(os, "sched_setaffinity", lambda *a: calls.append(a))
        base = self._tree(tmp_path, {"77": "3"})
        assert CoreScheduler._verify_child_affinity(77, {0}, "0", proc_base=base) == (True, 1)
        assert calls == [(77, {0})]

    def test_a_repin_that_fails_is_reported(self, tmp_path, monkeypatch):
        def _boom(*_a):
            raise OSError("no permission")

        monkeypatch.setattr(os, "sched_setaffinity", _boom)
        base = self._tree(tmp_path, {"77": "3"})
        assert CoreScheduler._verify_child_affinity(77, {0}, "0", proc_base=base) == (False, 1)


class _FakeStat:
    def __init__(self, *texts):
        self._texts = list(texts)

    def read_text(self):
        text = self._texts.pop(0) if len(self._texts) > 1 else self._texts[0]
        if text is None:
            raise OSError("gone")
        return text


class TestCoreUsage:
    def test_a_real_cpu_reports_a_percentage(self):
        usage = CoreScheduler._read_core_usage(0)
        assert usage is not None
        assert 0.0 <= usage <= 100.0

    def test_an_absent_cpu_reports_nothing(self):
        assert CoreScheduler._read_core_usage(9999) is None

    def test_a_frozen_counter_reports_zero(self, monkeypatch, instant):
        monkeypatch.setattr(sched, "Path", lambda _p: _FakeStat("cpu0 1 2 3 4 5 6 7 8\n"))
        assert CoreScheduler._read_core_usage(0) == 0.0

    def test_an_unreadable_proc_reports_nothing(self, monkeypatch, instant):
        monkeypatch.setattr(sched, "Path", lambda _p: _FakeStat(None))
        assert CoreScheduler._read_core_usage(0) is None

    def test_a_cpu_that_vanishes_between_samples_reports_nothing(self, monkeypatch, instant):
        stat = _FakeStat("cpu0 1 2 3 4 5 6 7 8\n", "cpu1 1 2 3 4 5 6 7 8\n")
        monkeypatch.setattr(sched, "Path", lambda _p: stat)
        assert CoreScheduler._read_core_usage(0) is None


class TestIdlePeriod:
    def test_the_phase_change_is_announced(self, tmp_path, instant):
        runner = _sched(tmp_path)
        phases = []
        runner.on_phase_change = [lambda cid, name: phases.append((cid, name))]
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        runner._idle_period(0, 0.01, "inter-core idle")
        assert phases == [(0, "inter-core idle")]
        assert runner.core_status[0].current_phase == "inter-core idle"

    def test_a_stop_ends_the_idle_immediately(self, tmp_path, instant):
        runner = _sched(tmp_path)
        runner._stop_event.set()
        runner.detector = MagicMock()
        runner._idle_period(0, 60.0, "idle stability")
        assert not runner.detector.check_mce.called

    def test_an_overheat_during_idle_is_recorded(self, tmp_path, instant):
        runner = _sched(tmp_path, max_temperature=85.0)
        runner._check_temperature = lambda: False
        runner._idle_period(0, 60.0, "idle stability")
        assert runner.core_status[0].errors == 1
        assert "temperature" in runner.core_status[0].last_error
        assert runner._stop_event.is_set()

    def test_an_own_core_mce_during_idle_ends_it(self, tmp_path, instant):
        runner = _sched(tmp_path, stop_on_error=True)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=0)]
        runner._idle_period(0, 60.0, "idle stability")
        assert runner.core_status[0].errors == 1
        assert runner._stop_event.is_set()


class TestVariableLoad:
    def _run(self, runner, popen, monkeypatch, duration=0.02, work=None):
        monkeypatch.setattr(sched.subprocess, "Popen", popen)
        return runner._run_variable_load(0, 0, "0", duration, work)

    def test_a_full_load_segment_announces_its_phase_and_holds_affinity(
        self, tmp_path, instant, monkeypatch
    ):
        runner = _sched(tmp_path, variable_load_interval=0.05)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        repins = []
        runner._verify_child_affinity = lambda pid, cpus, lst: repins.append(pid) or (True, 0)
        phases = []
        runner.on_phase_change = [lambda cid, name: phases.append((cid, name))]
        proc = _proc(poll=None, returncode=-15, out="ok")
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, duration=0.06, work=tmp_path
        )
        assert (passed, error) == (True, None)
        assert phases == [(0, "variable load")]
        assert repins
        assert runner.core_status[0].current_phase == "variable load"

    def test_a_hung_drain_during_load_does_not_block_the_verdict(
        self, tmp_path, instant, monkeypatch
    ):
        runner = _sched(tmp_path, variable_load_interval=0.001)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        proc = _proc(poll=None, returncode=-15)
        proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 2)
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, duration=0.002, work=tmp_path
        )
        assert (passed, error) == (True, None)

    def test_a_backend_failure_during_load_is_attributed(self, tmp_path, instant, monkeypatch):
        runner = _sched(
            tmp_path,
            backend=_backend(parse=(False, "FATAL ERROR: rounding was 0.5")),
            variable_load_interval=0.001,
        )
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        proc = _proc(poll=0, returncode=0, out="some output")
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, work=tmp_path
        )
        assert passed is False
        assert "rounding" in error

    def test_an_external_kill_during_load_is_not_a_core_verdict(
        self, tmp_path, instant, monkeypatch
    ):
        runner = _sched(tmp_path, variable_load_interval=0.001)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        proc = _proc(poll=-9, returncode=-9, out="partial")
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, work=tmp_path
        )
        assert passed is False
        assert "killed externally" in error

    def test_a_process_that_will_not_launch_is_an_apparatus_fault(
        self, tmp_path, instant, monkeypatch
    ):
        runner = _sched(tmp_path, variable_load_interval=0.001)
        passed, error = self._run(
            runner,
            MagicMock(side_effect=OSError("no taskset")),
            monkeypatch,
            work=tmp_path,
        )
        assert passed is False
        assert "Failed to start variable load" in error

    def test_an_overheat_during_load_stops_the_run(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, variable_load_interval=5.0, max_temperature=85.0)
        runner._check_temperature = lambda: False
        proc = _proc(poll=None, returncode=-15)
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, duration=5.0, work=tmp_path
        )
        assert passed is False
        assert "temperature" in error
        assert runner._stop_event.is_set()

    def test_an_own_core_mce_during_load_stops_the_run(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, variable_load_interval=5.0, stop_on_error=True)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=0)]
        proc = _proc(poll=None, returncode=-15)
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, duration=5.0, work=tmp_path
        )
        assert passed is False
        assert "MCE during variable load" in error

    def test_an_overheat_during_the_idle_segment_stops_the_run(
        self, tmp_path, instant, monkeypatch
    ):
        runner = _sched(tmp_path, variable_load_interval=0.001, max_temperature=85.0)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        temps = itertools.chain([True], itertools.repeat(False))
        runner._check_temperature = lambda: next(temps)
        proc = _proc(poll=0, returncode=0, out="")
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, duration=5.0, work=tmp_path
        )
        assert passed is False
        assert "idle transition" in error

    def test_an_mce_during_the_idle_segment_stops_the_run(
        self, tmp_path, instant, monkeypatch
    ):
        runner = _sched(tmp_path, variable_load_interval=0.001, stop_on_error=True)
        runner.detector = MagicMock()
        events = itertools.chain([[]], itertools.repeat([_mce(cpu=0)]))
        runner.detector.check_mce.side_effect = lambda: next(events)
        proc = _proc(poll=0, returncode=0, out="")
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch, duration=5.0, work=tmp_path
        )
        assert passed is False
        assert "idle transition" in error


class TestTestCoreOrchestration:
    def _runner(self, tmp_path, **cfg):
        runner = _sched(tmp_path, **cfg)
        runner._run_stress_phase = MagicMock(return_value=(True, None))
        return runner

    def test_an_unknown_core_is_skipped(self, tmp_path):
        runner = self._runner(tmp_path)
        runner.core_status[9] = sched.CoreTestStatus(core_id=9)
        runner.results[9] = []
        runner._test_core(9, 0)
        assert runner.core_status[9].state == "skipped"

    def test_the_stress_phase_is_announced(self, tmp_path):
        runner = self._runner(tmp_path)
        phases = []
        runner.on_phase_change = [lambda cid, name: phases.append(name)]
        runner._test_core(0, 0)
        assert phases[0] == "stress"
        assert runner.core_status[0].state == "passed"

    def test_a_variable_load_failure_fails_the_core(self, tmp_path):
        runner = self._runner(tmp_path, variable_load=True, seconds_per_core=3)
        runner._run_variable_load = MagicMock(return_value=(False, "instability in transition"))
        runner._test_core(0, 0)
        assert runner.core_status[0].state == "failed"
        assert runner.core_status[0].last_error == "instability in transition"
        assert runner.results[0][0].error_type == "load_transition"

    def test_an_idle_stability_error_fails_the_core(self, tmp_path):
        runner = self._runner(tmp_path, idle_stability_test=0.01)

        def _idle(core_id, _duration, _phase):
            runner.core_status[core_id].errors += 1
            runner.core_status[core_id].last_error = "MCE during idle stability"

        runner._idle_period = _idle
        runner._test_core(0, 0)
        assert runner.core_status[0].state == "failed"
        assert runner.results[0][0].error_type == "mce"

    def test_a_clean_idle_stability_pass_keeps_the_core(self, tmp_path):
        runner = self._runner(tmp_path, idle_stability_test=0.01)
        runner._idle_period = MagicMock()
        runner._test_core(0, 0)
        assert runner.core_status[0].state == "passed"


class TestStressPhase:
    def _run(self, runner, proc, monkeypatch):
        monkeypatch.setattr(sched.subprocess, "Popen", MagicMock(return_value=proc))
        status = runner.core_status[0]
        return runner._run_stress_phase(0, 0, "0", runner.work_dir, status)

    def test_a_stop_request_ends_the_phase(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, seconds_per_core=60)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        runner._stop_event.set()
        passed, error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is True
        assert error is None

    def test_an_overheat_fails_the_core(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, seconds_per_core=60, max_temperature=85.0)
        runner._check_temperature = lambda: False
        passed, error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is False
        assert "temperature" in error
        assert runner.core_status[0].errors == 1

    def test_a_stall_fails_the_core(self, tmp_path, instant, monkeypatch):
        runner = _sched(
            tmp_path, seconds_per_core=60, stall_timeout=0.0, stop_on_error=True
        )
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        runner._read_core_usage = lambda _cpu: 0.0
        monkeypatch.setattr(sched, "_STALL_GRACE_SECONDS", 0.0)
        stalled = []
        runner.on_stall_detected = [stalled.append]
        passed, error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is False
        assert "stalled" in error
        assert stalled == [0]
        assert runner._stop_event.is_set()

    def test_a_live_backend_error_ends_the_phase_early(self, tmp_path, instant, monkeypatch):
        runner = _sched(
            tmp_path,
            seconds_per_core=60,
            stop_on_error=True,
            backend=_backend(poll_error="FATAL ERROR: rounding was 0.5"),
        )
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        monkeypatch.setattr(sched, "_STALL_GRACE_SECONDS", 1e6)
        passed, error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is False
        assert "rounding" in error
        assert runner._stop_event.is_set()

    def test_an_own_core_mce_ends_the_phase(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, seconds_per_core=60, stop_on_error=True)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=0)]
        monkeypatch.setattr(sched, "_STALL_GRACE_SECONDS", 1e6)
        passed, error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is False
        assert "MCE during stress" in error

    def test_status_subscribers_see_progress_and_repins(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, seconds_per_core=60)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        runner._verify_child_affinity = lambda *_a, **_kw: (True, 3)
        monkeypatch.setattr(sched, "_STALL_GRACE_SECONDS", 1e6)
        updates = []
        runner.on_status_update = [lambda cid, st: updates.append(cid)]
        stop_after = itertools.count()

        def _poll_mce(_own, _phase):
            if next(stop_after) > 1:
                runner._stop_event.set()
            return None

        runner._poll_mce = _poll_mce
        passed, _error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is True
        assert updates

    def test_a_hung_drain_does_not_block_the_verdict(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, seconds_per_core=0)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        proc = _proc(poll=None, returncode=-15)
        proc.communicate.side_effect = subprocess.TimeoutExpired("cmd", 2)
        passed, error = self._run(runner, proc, monkeypatch)
        assert passed is True
        assert error is None

    def test_an_mce_arriving_during_the_kill_still_counts(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, seconds_per_core=0)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=0)]
        passed, error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is False
        assert "MCE during stress" in error
        assert runner.core_status[0].errors == 1

    def test_an_external_kill_is_reported_as_such(self, tmp_path, instant, monkeypatch):
        runner = _sched(tmp_path, seconds_per_core=0, stop_on_error=True)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        passed, error = self._run(runner, _proc(poll=-9, returncode=-9), monkeypatch)
        assert passed is False
        assert "killed externally" in error
        assert runner._stop_event.is_set()

    def test_a_backend_failure_stops_the_run_when_asked(self, tmp_path, instant, monkeypatch):
        runner = _sched(
            tmp_path,
            seconds_per_core=0,
            stop_on_error=True,
            backend=_backend(parse=(False, "FATAL ERROR: rounding was 0.5")),
        )
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        passed, error = self._run(runner, _proc(poll=None, returncode=-15), monkeypatch)
        assert passed is False
        assert "rounding" in error
        assert runner._stop_event.is_set()


class TestProcessTeardown:
    def test_a_process_that_survives_sigkill_is_reported(self, tmp_path, caplog):
        runner = _sched(tmp_path)
        proc = _proc(poll=None)
        proc.wait.side_effect = subprocess.TimeoutExpired("cmd", 3)
        runner._process = proc
        with caplog.at_level("WARNING", logger="corecycler.engine.scheduler"):
            runner._kill_current()
        assert "did not exit after SIGKILL" in caplog.text

    def test_an_already_dead_process_is_left_alone(self, tmp_path, monkeypatch):
        runner = _sched(tmp_path)
        proc = _proc(poll=0)
        runner._process = proc
        runner._kill_current()
        assert runner._we_killed_it is False

    def test_a_vanished_process_group_is_tolerated(self, tmp_path, monkeypatch):
        def _boom(_pid):
            raise ProcessLookupError("gone")

        monkeypatch.setattr(os, "getpgid", _boom)
        runner = _sched(tmp_path)
        proc = _proc(poll=None)
        runner._process = proc
        runner._kill_current()
        assert runner._we_killed_it is False

    def test_reaping_stops_when_no_child_has_exited(self, monkeypatch):
        monkeypatch.setattr(os, "waitpid", lambda *_a: (0, 0))
        CoreScheduler._reap_zombies()

    def test_reaping_tolerates_having_no_children(self, monkeypatch):
        def _boom(*_a):
            raise ChildProcessError("none")

        monkeypatch.setattr(os, "waitpid", _boom)
        CoreScheduler._reap_zombies()


class TestRapidTransitions:
    def _run(self, runner, popen, monkeypatch, **kw):
        monkeypatch.setattr(sched.subprocess, "Popen", popen)
        defaults = {"cores": [0, 1], "total_duration": 0.02, "load_seconds": 0.01,
                    "idle_seconds": 0.01}
        defaults.update(kw)
        return runner.run_rapid_transitions(**defaults)

    def test_a_stop_requested_before_entry_exits_immediately(self, tmp_path, monkeypatch):
        runner = _sched(tmp_path)
        runner._stop_event.set()
        popen = MagicMock()
        assert self._run(runner, popen, monkeypatch) == (True, None)
        assert not popen.called

    def test_drifted_threads_are_repinned_during_the_load_segment(
        self, tmp_path, monkeypatch
    ):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        repins = []
        runner._verify_child_affinity = lambda pid, cpus, lst: repins.append(pid) or (True, 0)
        proc = _proc(poll=None, returncode=-15)
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch,
            total_duration=0.05, load_seconds=0.02,
        )
        assert passed is True
        assert error is None
        assert repins

    def test_a_stop_during_the_load_segment_ends_the_run(self, tmp_path, monkeypatch):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        proc = _proc(poll=None, returncode=-15)
        runner._stop_event = _StopAfter(1)
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch,
            total_duration=60.0, load_seconds=0.01,
        )
        assert (passed, error) == (True, None)

    def test_a_stop_during_the_idle_segment_ends_the_run(self, tmp_path, monkeypatch):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        proc = _proc(poll=None, returncode=-15)
        runner._stop_event = _StopAfter(1)
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch,
            total_duration=60.0, load_seconds=0.0, idle_seconds=0.001,
        )
        assert (passed, error) == (True, None)
        assert not runner.detector.check_mce.called

    def test_a_crash_exit_ends_the_run(self, tmp_path, monkeypatch):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = []
        proc = _proc(poll=0, returncode=-11)
        passed, error = self._run(runner, MagicMock(return_value=proc), monkeypatch)
        assert passed is False
        assert "Crash during rapid transition" in error

    def test_a_harness_exception_is_never_a_core_verdict(self, tmp_path, monkeypatch):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        passed, error = self._run(
            runner, MagicMock(side_effect=OSError("no taskset")), monkeypatch
        )
        assert passed is False
        assert "Rapid transition harness error" in error
        assert CoreScheduler._classify_error(error) == "startup"

    def test_an_mce_during_the_idle_segment_ends_the_run(self, tmp_path, monkeypatch):
        runner = _sched(tmp_path)
        runner.detector = MagicMock()
        runner.detector.check_mce.return_value = [_mce(cpu=1)]
        proc = _proc(poll=None, returncode=-15)
        passed, error = self._run(
            runner, MagicMock(return_value=proc), monkeypatch,
            total_duration=60.0, load_seconds=0.001, idle_seconds=0.001,
        )
        assert passed is False
        assert "MCE during idle phase" in error


class _StopAfter:
    """A stop event that reports 'set' only after N wait() calls."""

    def __init__(self, after):
        self._after = after
        self._set = False

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def is_set(self):
        return self._set

    def wait(self, _timeout=None):
        self._after -= 1
        if self._after <= 0:
            self._set = True
        return self._set


class TestErrorClassification:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (None, "unknown"),
            ("Failed to start stress test: boom", "startup"),
            ("MCE during stress", "mce"),
            ("CPU temperature exceeded", "thermal"),
            ("Stress test stalled on core 3", "stall"),
            ("FATAL ERROR: rounding was 0.5", "computation"),
            ("operation timeout", "timeout"),
            ("Stress process killed externally (code -9)", "killed"),
            ("process exited with code -11", "crash"),
            ("error during idle stability", "idle_instability"),
            ("failure in load transition segment", "load_transition"),
            ("something else entirely", "unknown"),
        ],
    )
    def test_a_message_maps_to_its_class(self, message, expected):
        assert CoreScheduler._classify_error(message) == expected
