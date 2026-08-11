"""Edge spec for the execution engine's failure-path branches."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from corecycler.engine import containment, execution
from corecycler.engine import scheduler as scheduler_mod
from corecycler.engine.backends.base import StressConfig, StressResult
from corecycler.engine.execution import Lane, SuperviseHooks, ThermalWatch, _LaneRun
from corecycler.engine.scheduler import CoreScheduler


class Event:
    def __init__(self, cpu: int, message: str = "boom") -> None:
        self.cpu = cpu
        self.bank = 0
        self.message = message
        self.corrected = False
        self.timestamp = 0.0
        self.raw_ts = 0.0


class TestContainmentProbe:
    def test_no_systemd_run_probes_to_nothing(self):
        with patch.object(containment, "_systemd_run_path", return_value=None):
            assert containment._probe_mechanism() is None

    def test_a_probe_that_cannot_run_is_no_mechanism(self):
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", side_effect=OSError("no dbus")),
        ):
            assert containment._probe_mechanism() is None

    def test_a_probe_that_exits_nonzero_is_no_mechanism(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="Failed to start scope")
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=failed),
        ):
            assert containment._probe_mechanism() is None

    def test_a_green_probe_names_the_user_mechanism(self):
        good = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=good),
            patch.object(containment.os, "geteuid", return_value=1000),
        ):
            assert containment._probe_mechanism() == containment.MECHANISM_USER

    def test_a_green_probe_as_root_names_the_system_mechanism(self):
        good = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=good),
            patch.object(containment.os, "geteuid", return_value=0),
        ):
            assert containment._probe_mechanism() == containment.MECHANISM_SYSTEM


class TestContainRefusals:
    def test_systemd_run_vanishing_after_the_probe_refuses(self):
        with (
            patch.object(containment, "available_mechanism", return_value=containment.MECHANISM_USER),
            patch.object(containment, "_systemd_run_path", return_value=None),
            pytest.raises(containment.ContainmentUnavailable, match="disappeared"),
        ):
            containment.contain((0,))

    def test_a_missing_setpriv_refuses(self, tmp_path):
        systemd_run = tmp_path / "systemd-run"
        systemd_run.write_text("#!/bin/sh\n")
        systemd_run.chmod(0o755)

        def fake_resolve(key):
            from corecycler.config.tools import Resolution

            path = systemd_run if key == "systemd-run" else None
            return Resolution(key=key, path=path, origin="path")

        with (
            patch.object(containment, "available_mechanism", return_value=containment.MECHANISM_USER),
            patch.object(containment.tools, "resolve", side_effect=fake_resolve),
            pytest.raises(containment.ContainmentUnavailable, match="setpriv"),
        ):
            containment.contain((0,))


class TestObservedTreeEdges:
    def test_a_tid_without_a_status_file_is_skipped(self, tmp_path):
        (tmp_path / "300" / "task" / "301").mkdir(parents=True)
        (tmp_path / "300" / "stat").write_text("300 (x) S 1 300 300 0 -1\n")
        assert containment.observed_tree_cpus(300, proc_base=tmp_path) == set()

    def test_malformed_neighbours_never_break_the_walk(self, tmp_path):
        target = tmp_path / "400"
        (target / "task" / "400").mkdir(parents=True)
        (target / "task" / "400" / "status").write_text("Cpus_allowed_list:\t2\n")
        (target / "stat").write_text("400 (x) S 1 400 400 0 -1\n")
        (tmp_path / "500").mkdir()
        (tmp_path / "501").mkdir()
        (tmp_path / "501" / "stat").write_text("501 (short) S\n")
        (tmp_path / "502").mkdir()
        (tmp_path / "502" / "stat").write_text("502 (bad) S abc 1 2\n")
        (tmp_path / "notapid").mkdir()
        assert containment.observed_tree_cpus(400, proc_base=tmp_path) == {2}

    def test_garbled_cpu_ranges_keep_the_readable_parts(self, tmp_path):
        target = tmp_path / "600"
        (target / "task" / "600").mkdir(parents=True)
        (target / "task" / "600" / "status").write_text(
            "Cpus_allowed_list:\t,bogus,3-z,7\n"
        )
        (target / "stat").write_text("600 (x) S 1 600 600 0 -1\n")
        assert containment.observed_tree_cpus(600, proc_base=tmp_path) == {7}


class TestThermalEdges:
    def test_a_tripped_watch_stays_tripped_while_still_over(self):
        watch = ThermalWatch(
            max_temperature=95, grace_seconds=0.0, hard_margin=8,
            require_sensor=False, read=lambda: 96.0,
        )
        assert watch.safe() is False
        assert watch.safe() is False


class TestTemperatureSourceEdges:
    def test_a_nameless_or_unreadable_hwmon_node_is_skipped(self, tmp_path):
        nameless = tmp_path / "hwmon0"
        nameless.mkdir()
        unreadable = tmp_path / "hwmon1"
        unreadable.mkdir()
        (unreadable / "name").mkdir()
        real = tmp_path / "hwmon2"
        real.mkdir()
        (real / "name").write_text("k10temp\n")
        (real / "temp1_input").write_text("51000\n")
        with patch.object(execution, "Path", lambda _p: tmp_path):
            assert execution.read_cpu_temperature() == 51.0


class TestReapEdges:
    def test_an_unwaited_child_is_reaped(self):
        proc = subprocess.Popen(["true"])
        deadline = time.monotonic() + 5
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        execution.reap_zombies()
        execution.reap_zombies()


def _idle_supervisor(backend=None, **overrides):
    from test_execution import FakeBackend, FakeDetector

    kwargs = dict(
        backend=backend or FakeBackend(),
        detector=FakeDetector(),
        thermal=ThermalWatch(
            max_temperature=95, grace_seconds=3, hard_margin=8,
            require_sensor=False, read=lambda: 40.0,
        ),
        stop_event=threading.Event(),
        observed=[],
        poll_interval=0.01,
        containment_prefix=lambda cpus: [],
        watch_escapes=False,
    )
    kwargs.update(overrides)
    return execution.Supervisor(**kwargs)


class TestSupervisorInternalsEdges:
    def test_error_poll_skips_decided_lanes(self, tmp_path):
        from test_execution import FakeBackend

        backend = FakeBackend(poll_error="fake error: SUMOUT")
        supervisor = _idle_supervisor(backend=backend)
        decided = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        decided.verdict = StressResult(core_id=0, passed=True, duration_seconds=0.1)
        open_run = _LaneRun(lane=Lane(core_id=1, cpus=(1,), work_dir=tmp_path))
        open_run.proc = SimpleNamespace(returncode=None)
        assert supervisor._poll_backend_errors([decided, open_run], time.monotonic()) is True
        assert open_run.verdict is not None and not open_run.verdict.passed
        assert decided.verdict.passed is True

    def test_a_clean_error_poll_reports_nothing(self, tmp_path):
        supervisor = _idle_supervisor()
        open_run = _LaneRun(lane=Lane(core_id=1, cpus=(1,), work_dir=tmp_path))
        assert supervisor._poll_backend_errors([open_run], time.monotonic()) is False

    def test_status_hooks_report_running_lanes(self, tmp_path):
        from test_execution import FakeBackend

        seen: list[tuple[int, float]] = []
        backend = FakeBackend()
        supervisor = _idle_supervisor(
            backend=backend,
            hooks=SuperviseHooks(on_status=lambda cid, el: seen.append((cid, el))),
        )
        lane = Lane(core_id=3, cpus=(3,), work_dir=tmp_path / "core_3")
        supervisor.run([lane], lambda _lane: StressConfig(), 0.1)
        assert seen and all(cid == 3 for cid, _ in seen)

    def test_an_unattributed_event_with_every_lane_decided_changes_nothing(self, tmp_path):
        from test_execution import FakeDetector

        supervisor = _idle_supervisor(detector=FakeDetector([[Event(cpu=-1)]]))
        decided = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        decided.verdict = StressResult(core_id=0, passed=False, duration_seconds=0.1)
        assert supervisor._apply_mce_events([decided], time.monotonic()) is False

    def test_escape_observation_handles_a_dead_process(self, tmp_path):
        supervisor = _idle_supervisor()
        ghost = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        assert supervisor._escaped_cpus(ghost) == set()
        ghost.proc = SimpleNamespace(pid=1)
        with patch.object(containment, "observed_tree_cpus", side_effect=OSError):
            assert supervisor._escaped_cpus(ghost) == set()

    def test_a_late_unattributed_event_lands_on_the_anchor(self, tmp_path):
        from test_execution import FakeBackend, FakeDetector

        backend = FakeBackend(
            [sys.executable, "-c", "import time; time.sleep(0.3)"]
        )
        supervisor = _idle_supervisor(
            backend=backend,
            detector=FakeDetector([[], [Event(cpu=-1)]]),
            poll_interval=0.5,
        )
        lane = Lane(core_id=2, cpus=(2,), work_dir=tmp_path / "core_2")
        verdict = supervisor.run([lane], lambda _lane: StressConfig(), 0.1)[2]
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "mce_unattributed"

    def test_final_verdict_flags_an_external_kill(self, tmp_path):
        supervisor = _idle_supervisor()
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        run.proc = SimpleNamespace(returncode=-9)
        verdict = supervisor._final_verdict(run, elapsed=5.0, interrupted=False)
        assert verdict is not None and verdict.error_type == "killed"

    def test_final_verdict_flags_an_instant_nonzero_exit(self, tmp_path):
        supervisor = _idle_supervisor()
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        run.proc = SimpleNamespace(returncode=4)
        verdict = supervisor._final_verdict(run, elapsed=0.5, interrupted=False)
        assert verdict is not None and verdict.error_type == "startup"

    def test_final_verdict_prefers_the_live_error_file(self, tmp_path):
        from test_execution import FakeBackend

        supervisor = _idle_supervisor(backend=FakeBackend(poll_error="fake error: SUMOUT"))
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        run.proc = SimpleNamespace(returncode=0)
        supervisor._we_killed = True
        verdict = supervisor._final_verdict(run, elapsed=5.0, interrupted=False)
        assert verdict is not None and "SUMOUT" in verdict.error_message

    def test_classify_names_a_timeout(self):
        assert execution.classify_error("test timeout reached") == "timeout"


class TestSchedulerHookGlue:
    def _scheduler(self, tmp_path, monkeypatch, **config):
        from test_scheduler import ScriptedSupervisor, make_scheduler

        monkeypatch.setattr(scheduler_mod, "Supervisor", ScriptedSupervisor)
        ScriptedSupervisor.script = []
        ScriptedSupervisor.created = []
        return make_scheduler(tmp_path, **config), ScriptedSupervisor

    def test_engine_hooks_reach_the_scheduler_callbacks(self, tmp_path, monkeypatch):
        sched, scripted = self._scheduler(tmp_path, monkeypatch, cores_to_test=[0])
        stalls: list[int] = []
        temps: list[float] = []
        phases: list[tuple[int, str]] = []
        statuses: list[float] = []
        sched.on_stall_detected.append(stalls.append)
        sched.on_thermal_throttle.append(temps.append)
        sched.on_phase_change.append(lambda cid, ph: phases.append((cid, ph)))
        sched.on_status_update.append(lambda cid, st: statuses.append(st.elapsed_seconds))

        def hook_driving(sup, lanes, config_for, duration):
            hooks = sup.kwargs["hooks"]
            hooks.on_status(0, 1.5)
            hooks.on_status(99, 1.0)
            hooks.on_stall(0)
            hooks.on_thermal(99.0)
            return {0: StressResult(core_id=0, passed=True, duration_seconds=0.1)}

        scripted.script = [hook_driving]
        sched.run()
        assert 1.5 in statuses
        assert stalls == [0]
        assert temps == [99.0]
        assert phases[0] == (0, "stress")

    def test_a_stop_between_cycles_ends_the_run(self, tmp_path, monkeypatch):
        sched, scripted = self._scheduler(tmp_path, monkeypatch, cycle_count=3)

        def stop_after(sup, lanes, config_for, duration):
            sup.kwargs["stop_event"].set()
            return {one.core_id: StressResult(core_id=one.core_id, passed=True, duration_seconds=0.1)
                    for one in lanes}

        scripted.script = [stop_after]
        results = sched.run()
        assert len(results[0]) == 1
        assert results[1] == []

    def test_variable_stop_on_error_latches_the_stop(self, tmp_path, monkeypatch):
        from test_scheduler import bad, ok

        sched, scripted = self._scheduler(
            tmp_path, monkeypatch, cores_to_test=[0], variable_load=True, stop_on_error=True
        )
        scripted.script = [
            lambda sup, lanes, c, d: {0: ok(0)},
            lambda sup, lanes, c, d: {0: bad(0)},
        ]
        results = sched.run()
        assert results[0][0].passed is False
        assert sched._stop_requested

    def test_a_variable_idle_error_latches_the_stop(self, tmp_path, monkeypatch):
        from test_scheduler import ok

        monkeypatch.setattr(
            execution, "watch_idle", lambda **kwargs: "MCE during idle transition: bang"
        )
        sched, scripted = self._scheduler(
            tmp_path, monkeypatch, cores_to_test=[0], variable_load=True, stop_on_error=True
        )
        scripted.script = [
            lambda sup, lanes, c, d: {0: ok(0)},
            lambda sup, lanes, c, d: {0: ok(0)},
        ]
        results = sched.run()
        assert results[0][0].passed is False
        assert "idle transition" in results[0][0].error_message
        assert sched._stop_requested

    def test_rapid_honors_a_stop_landing_mid_cycle(self, tmp_path, monkeypatch):
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: None)
        sched, scripted = self._scheduler(tmp_path, monkeypatch)

        def stop_during(sup, lanes, config_for, duration):
            time.sleep(0.02)
            sup.kwargs["stop_event"].set()
            return {lanes[0].core_id: None}

        scripted.script = [stop_during]
        passed, error = sched.run_rapid_transitions([0], total_duration=5.0, load_seconds=0.02)
        assert (passed, error) == (True, None)

    def test_the_temperature_delegate_reads_through_the_engine(self, monkeypatch):
        monkeypatch.setattr(execution, "read_cpu_temperature", lambda: 33.5)
        assert CoreScheduler._read_cpu_temperature() == 33.5

    def test_an_idle_error_with_stop_on_error_latches_the_stop(self, tmp_path, monkeypatch):
        from test_scheduler import ok

        monkeypatch.setattr(
            execution, "watch_idle", lambda **kwargs: "MCE during inter-core idle: bang"
        )
        sched, scripted = self._scheduler(
            tmp_path, monkeypatch, idle_between_cores=0.01, stop_on_error=True
        )
        scripted.script = [lambda sup, lanes, c, d: {0: ok(0)}]
        results = sched.run()
        assert results[0][0].passed is True
        assert sched._stop_requested
        assert results[1] == []


class TestRemainingLoopEdges:
    def test_a_lone_nameless_hwmon_node_reads_as_no_sensor(self, tmp_path):
        (tmp_path / "hwmon0").mkdir()
        with patch.object(execution, "Path", lambda _p: tmp_path):
            assert execution.read_cpu_temperature() is None

    def test_a_lone_unreadable_name_reads_as_no_sensor(self, tmp_path):
        node = tmp_path / "hwmon0"
        node.mkdir()
        (node / "name").mkdir()
        with patch.object(execution, "Path", lambda _p: tmp_path):
            assert execution.read_cpu_temperature() is None

    def test_a_lone_foreign_sensor_reads_as_no_cpu_sensor(self, tmp_path):
        node = tmp_path / "hwmon0"
        node.mkdir()
        (node / "name").write_text("nvme\n")
        (node / "temp1_input").write_text("99000\n")
        with patch.object(execution, "Path", lambda _p: tmp_path):
            assert execution.read_cpu_temperature() is None

    def test_reap_leaves_a_live_child_alone(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            execution.reap_zombies()
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_the_in_loop_error_poll_fails_the_lane_mid_run(self, tmp_path):
        from test_execution import FakeBackend

        backend = FakeBackend(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            poll_error="fake error: SUMOUT",
        )
        supervisor = _idle_supervisor(backend=backend)
        lane = Lane(core_id=0, cpus=(0,), work_dir=tmp_path / "core_0")
        with patch.object(execution, "ERROR_POLL_INTERVAL", 0.0):
            verdict = supervisor.run([lane], lambda _lane: StressConfig(), 5.0)[0]
        assert verdict is not None and not verdict.passed
        assert "SUMOUT" in verdict.error_message

    def test_exit_polling_skips_decided_and_unstarted_lanes(self, tmp_path):
        supervisor = _idle_supervisor()
        decided = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        decided.verdict = StressResult(core_id=0, passed=True, duration_seconds=0.1)
        unstarted = _LaneRun(lane=Lane(core_id=1, cpus=(1,), work_dir=tmp_path))
        assert (
            supervisor._poll_exits_stalls_watchdog([decided, unstarted], time.monotonic())
            is False
        )

    def test_final_verdict_attributes_a_parse_failure(self, tmp_path):
        from test_execution import FakeBackend

        supervisor = _idle_supervisor(backend=FakeBackend(parse=(False, "fake error: BAD")))
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        run.proc = SimpleNamespace(returncode=0)
        verdict = supervisor._final_verdict(run, elapsed=5.0, interrupted=False)
        assert verdict is not None and not verdict.passed
        assert "BAD" in verdict.error_message
