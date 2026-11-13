"""Edge spec for the execution engine's failure-path branches."""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from corecycler.engine import containment, execution
from corecycler.engine import scheduler as scheduler_mod
from corecycler.engine.backends.base import LaneSignal, Outcome, StressConfig
from corecycler.engine.execution import Lane, SuperviseHooks, ThermalWatch, _LaneRun
from corecycler.monitor.hwmon import SensorRead


class TestContainmentProbe:
    def test_no_systemd_run_probes_to_nothing(self):
        with patch.object(containment, "_systemd_run_path", return_value=None):
            assert containment._probe_mechanism() == containment.Probe(None, None)

    def test_a_probe_that_cannot_run_is_no_mechanism(self):
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", side_effect=OSError("no dbus")),
        ):
            assert containment._probe_mechanism() == containment.Probe(None, None)

    def test_a_probe_that_exits_nonzero_is_no_mechanism(self):
        failed = SimpleNamespace(returncode=1, stdout="", stderr="Failed to start scope")
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=failed),
        ):
            assert containment._probe_mechanism() == containment.Probe(None, None)

    def test_a_green_probe_names_the_user_mechanism_and_the_lanes_cgroup(self):
        good = SimpleNamespace(
            returncode=0,
            stdout="0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-u12.scope\nCpus_allowed_list:\t0\n",
            stderr="",
        )
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=good) as run,
            patch.object(containment.os, "geteuid", return_value=1000),
        ):
            probe = containment._probe_mechanism()
        assert probe.mechanism == containment.MECHANISM_USER
        assert probe.lane_cgroup_parent == "/user.slice/user-1000.slice/user@1000.service/app.slice"
        assert run.call_args[0][0][-3:] == ["cat", "/proc/self/cgroup", "/proc/self/status"]

    def test_a_green_probe_as_root_names_the_system_mechanism_and_the_system_slice(self):
        good = SimpleNamespace(returncode=0, stdout="0::/system.slice/run-u5.scope\nCpus_allowed_list:\t0\n", stderr="")
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=good),
            patch.object(containment.os, "geteuid", return_value=0),
        ):
            probe = containment._probe_mechanism()
        assert probe.mechanism == containment.MECHANISM_SYSTEM
        assert probe.lane_cgroup_parent == "/system.slice"

    def test_a_scope_at_the_root_names_the_root(self):
        good = SimpleNamespace(returncode=0, stdout="0::/run-u5.scope\nCpus_allowed_list:\t0\n", stderr="")
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=good),
            patch.object(containment.os, "geteuid", return_value=0),
        ):
            assert containment._probe_mechanism().lane_cgroup_parent == "/"

    def test_a_probe_without_a_unified_hierarchy_knows_no_lane_cgroup(self):
        good = SimpleNamespace(
            returncode=0, stdout="12:cpuset:/system.slice/run-u5.scope\nCpus_allowed_list:\t0\n", stderr=""
        )
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=good),
            patch.object(containment.os, "geteuid", return_value=0),
        ):
            probe = containment._probe_mechanism()
        assert probe.mechanism == containment.MECHANISM_SYSTEM
        assert probe.lane_cgroup_parent is None

    @pytest.mark.parametrize(
        ("status", "ran"),
        [("Cpus_allowed_list:\t0-9\n", "ran on CPUs 0,1,2,3,4,5,6,7,8,9"), ("", "ran on an unreadable set of CPUs")],
    )
    @pytest.mark.req("L01.7")
    def test_a_probe_whose_cpu_limit_is_not_enforced_is_no_mechanism_and_says_why(self, status, ran):
        loose = SimpleNamespace(
            returncode=0,
            stdout="0::/user.slice/user-1000.slice/user@1000.service/app.slice/run-u9.scope\n" + status,
            stderr="",
        )
        with (
            patch.object(containment, "_systemd_run_path", return_value="/bin/systemd-run"),
            patch.object(containment.subprocess, "run", return_value=loose),
            patch.object(containment.os, "geteuid", return_value=1000),
        ):
            probe = containment._probe_mechanism()
        assert probe.mechanism is None
        assert probe.lane_cgroup_parent == "/user.slice/user-1000.slice/user@1000.service/app.slice"
        assert ran in probe.problem
        assert "AllowedCPUs is not enforced" in probe.problem
        assert "does not delegate the cpuset controller" in probe.problem

    def test_the_refusal_names_the_controllers_the_lane_cgroup_offers(self, tmp_path):
        lane = tmp_path / "user.slice" / "app.slice"
        lane.mkdir(parents=True)
        (lane / "cgroup.controllers").write_text("cpu io memory pids\n")
        assert "(the lane cgroup offers: cpu io memory pids)" in containment._not_enforced(
            set(range(4)), "/user.slice/app.slice", True, cgroup_base=tmp_path
        )
        assert "offers: unreadable (No such file or directory)" in containment._not_enforced(
            {0, 1}, "/gone", True, cgroup_base=tmp_path
        )
        assert "offers: unknown, the probe reported no cgroup" in containment._not_enforced({0, 1}, None, False)


class TestContainmentProblem:
    @pytest.fixture(autouse=True)
    def fresh_probe(self):
        containment._probe_cache.clear()
        yield
        containment._probe_cache.clear()

    @pytest.mark.parametrize(
        ("probe", "expected"),
        [
            (containment.Probe(containment.MECHANISM_USER, "/app.slice"), None),
            (containment.Probe(None, "/app.slice", "AllowedCPUs is not enforced"), "AllowedCPUs is not enforced"),
            (containment.Probe(None, None), "no systemd scope could be started (the probe failed)"),
        ],
    )
    def test_the_problem_is_the_probe_s_own_reason(self, probe, expected):
        with patch.object(containment, "_probe_mechanism", return_value=probe):
            problem = containment.containment_problem()
        assert problem == expected or (expected is not None and expected in problem)

    def test_the_probe_runs_once_until_a_refresh_is_asked_for(self):
        probe = containment.Probe(None, "/a", "AllowedCPUs is not enforced")
        with patch.object(containment, "_probe_mechanism", return_value=probe) as run:
            containment.containment_problem()
            containment.containment_problem()
            assert run.call_count == 1
            containment.containment_problem(refresh=True)
            assert run.call_count == 2

    @pytest.mark.req("L01.7")
    def test_a_lane_problem_names_containment_before_memory_accounting(self):
        with (
            patch.object(containment, "containment_problem", return_value="cannot contain"),
            patch.object(containment, "memory_accounting_problem", return_value="cannot count"),
        ):
            assert containment.lane_problem() == "cannot contain"
        with (
            patch.object(containment, "containment_problem", return_value=None),
            patch.object(containment, "memory_accounting_problem", return_value="cannot count"),
        ):
            assert containment.lane_problem() == "cannot count"

    def test_contain_refuses_naming_the_probe_s_reason(self):
        with (
            patch.object(
                containment,
                "_probe_mechanism",
                return_value=containment.Probe(None, "/a", "AllowedCPUs is not enforced"),
            ),
            pytest.raises(containment.ContainmentUnavailable, match="AllowedCPUs is not enforced; refusing to launch"),
        ):
            containment.contain((0,))


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


class TestPayloadCgroup:
    def test_the_named_scope_cgroup_is_returned_once_adopted(self, tmp_path):
        (tmp_path / "900").mkdir()
        (tmp_path / "900" / "cgroup").write_text("0::/user.slice/user-1000.slice/app.slice/corecycler-1-1.scope\n")
        assert containment.payload_cgroup(900, "corecycler-1-1", proc_base=tmp_path) == (
            "/user.slice/user-1000.slice/app.slice/corecycler-1-1.scope"
        )

    def test_before_adoption_it_reports_none(self, tmp_path):
        (tmp_path / "901").mkdir()
        (tmp_path / "901" / "cgroup").write_text("0::/user.slice/session-3.scope\n")
        assert containment.payload_cgroup(901, "corecycler-1-1", proc_base=tmp_path) is None

    def test_an_unreadable_proc_reports_none(self, tmp_path):
        assert containment.payload_cgroup(4242, "corecycler-1-1", proc_base=tmp_path) is None


class TestScopeEffectiveCpus:
    def test_the_effective_cpuset_is_parsed(self, tmp_path):
        scope = tmp_path / "user.slice" / "corecycler-1-1.scope"
        scope.mkdir(parents=True)
        (scope / "cpuset.cpus.effective").write_text("4,20\n")
        assert containment.scope_effective_cpus("/user.slice/corecycler-1-1.scope", cgroup_base=tmp_path) == {4, 20}

    def test_a_missing_cpuset_file_reports_none(self, tmp_path):
        assert containment.scope_effective_cpus("/user.slice/gone.scope", cgroup_base=tmp_path) is None


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
        (target / "task" / "600" / "status").write_text("Cpus_allowed_list:\t,bogus,3-z,7\n")
        (target / "stat").write_text("600 (x) S 1 600 600 0 -1\n")
        assert containment.observed_tree_cpus(600, proc_base=tmp_path) == {7}


class TestThermalEdges:
    def test_a_tripped_watch_stays_tripped_while_still_over(self):
        watch = ThermalWatch(
            max_temperature=95,
            grace_seconds=0.0,
            hard_margin=8,
            require_sensor=False,
        )
        assert watch.safe(96.0) is False
        assert watch.safe(96.0) is False


def _idle_supervisor(backend=None, **overrides):
    from test_execution import FakeBackend, FakeDetector

    kwargs = dict(
        backend=backend or FakeBackend(),
        detector=FakeDetector(),
        thermal=ThermalWatch(
            max_temperature=95,
            grace_seconds=3,
            hard_margin=8,
            require_sensor=False,
        ),
        sensors=lambda: SensorRead(hottest=40.0),
        stop_event=threading.Event(),
        observed=[],
        settle_s=0.0,
        poll_interval=0.01,
        containment_for=lambda cpus: None,
    )
    kwargs.update(overrides)
    return execution.Supervisor(**kwargs)


class _ExitedProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class TestSupervisorInternalsEdges:
    def test_status_hooks_report_running_lanes(self, tmp_path):
        from test_execution import FakeBackend

        seen: list[tuple[int, float]] = []
        backend = FakeBackend()
        supervisor = _idle_supervisor(
            backend=backend,
            hooks=SuperviseHooks(on_status=lambda cid, el: seen.append((cid, el))),
        )
        lane = Lane(core_id=3, cpus=(3,), work_dir=tmp_path / "core_3")
        supervisor.run([lane], StressConfig(), 5.0)
        assert seen and all(cid == 3 for cid, _ in seen)

    def test_containment_fault_ignores_a_dead_or_unverified_lane(self, tmp_path):
        supervisor = _idle_supervisor()
        ghost = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        assert supervisor._containment_fault(ghost, time.monotonic()) is None
        ghost.proc = SimpleNamespace(pid=1)
        assert supervisor._containment_fault(ghost, time.monotonic()) is None

    def test_a_matching_kernel_record_is_never_a_fault(self, tmp_path):
        supervisor = _idle_supervisor()
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0, 16), work_dir=tmp_path))
        run.proc = SimpleNamespace(pid=1234)
        run.contained = containment.Containment(prefix=[], unit="cc-test", scope="/user.slice/cc-test.scope")
        run.started_at = time.monotonic()
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value={0, 16}),
        ):
            assert supervisor._containment_fault(run, time.monotonic()) is None
        assert run.cgroup == "/user.slice/cc-test.scope"

    @pytest.mark.real_cpu_time
    def test_a_busy_payload_sample_is_never_a_stall(self, tmp_path):
        supervisor = _idle_supervisor(stall_timeout=0.0)
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        run.proc = SimpleNamespace(pid=4321)
        with patch.object(containment, "process_group_cpu_seconds", side_effect=[0.0, 60.0, 60.0]):
            now = time.monotonic()
            supervisor._sample_cpu(run, now)
            supervisor._sample_cpu(run, now + 60.0)
            assert run.last_active == now + 60.0
            supervisor._sample_cpu(run, now + 120.0)
        assert run.last_active == now + 60.0

    @staticmethod
    def _watched_run(tmp_path, started: float) -> _LaneRun:
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path))
        run.proc = SimpleNamespace(pid=4321, returncode=None, poll=lambda: None)
        run.started_at = run.last_active = run.cpu_sampled_at = started
        run.cpu_seconds = 0.0
        run.last_watchdog = float("inf")
        return run

    @pytest.mark.req("T22.F15")
    def test_a_stall_needs_its_whole_window_of_cpu_time_measured_near_zero(self, tmp_path):
        supervisor = _idle_supervisor(stall_timeout=30.0)
        started = 1000.0
        run = self._watched_run(tmp_path, started)
        seen = []
        with patch.object(containment, "process_group_cpu_seconds", side_effect=[0.0, None, 0.0]):
            for at in (30.0, 40.0, 41.0):
                supervisor._check(run, started + at, started)
                seen.append([found.outcome for found in run.signals])
        assert seen == [[], [], [Outcome.STALL]]

    @pytest.mark.req("T22.F15")
    def test_cpu_time_unread_for_the_stall_window_is_named_unverified(self, tmp_path):
        supervisor = _idle_supervisor(stall_timeout=30.0)
        started = 1000.0
        run = self._watched_run(tmp_path, started)
        last_read = 20.0
        seen = []
        with patch.object(containment, "process_group_cpu_seconds", side_effect=[0.0, None, None, None]):
            for at in (last_read, 35.0, 50.0, 51.0):
                supervisor._check(run, started + at, started)
                seen.append(list(run.signals))
        unread = LaneSignal(
            Outcome.UNVERIFIED,
            f"payload CPU time on CPUs 0 unreadable for {51.0 - last_read:.0f}s: verdict unavailable",
        )
        assert seen == [[], [], [], [unread]]

    @pytest.mark.req("T22.F15")
    def test_unread_cpu_time_is_judged_from_the_end_of_the_stall_grace(self, tmp_path):
        supervisor = _idle_supervisor(stall_timeout=1.0)
        started = 1000.0
        run = self._watched_run(tmp_path, started)
        seen = []
        with patch.object(containment, "process_group_cpu_seconds", return_value=None):
            for at in (execution.STALL_GRACE_SECONDS - 2.0, execution.STALL_GRACE_SECONDS):
                supervisor._check(run, started + at, started)
                seen.append([found.outcome for found in run.signals])
        assert seen == [[], [Outcome.UNVERIFIED]]

    def test_a_late_unattributed_event_reaches_every_lane(self, tmp_path):
        from test_execution import FakeBackend, FakeDetector, unblamed

        backend = FakeBackend([sys.executable, "-c", "import time; time.sleep(60)"])
        supervisor = _idle_supervisor(
            backend=backend,
            detector=FakeDetector([[unblamed()]]),
            poll_interval=1.0,
        )
        lanes = [Lane(core_id=core, cpus=(core,), work_dir=tmp_path / f"core_{core}") for core in (2, 3)]
        verdicts = supervisor.run(lanes, StressConfig(), 0.0)
        assert [verdicts[core].outcome for core in (2, 3)] == [Outcome.UNATTRIBUTED_MCE] * 2

    def test_our_own_stop_before_the_deadline_is_cut_short(self, tmp_path):
        supervisor = _idle_supervisor()
        run = _LaneRun(
            lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path), deadline=10.0, stopped_at=5.0, sent=signal.SIGTERM
        )
        run.proc = _ExitedProc(-15)
        supervisor._note_exit(run)
        assert supervisor._grade(run).outcome is Outcome.CUT_SHORT

    @pytest.mark.req("VERDICT.10")
    def test_a_lane_never_reaped_after_sigkill_keeps_did_not_stop(self, tmp_path):
        supervisor = _idle_supervisor()
        run = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path), stopped_at=5.0, sent=signal.SIGKILL)
        run.proc = SimpleNamespace(returncode=None)
        run.signals.append(LaneSignal(Outcome.DID_NOT_STOP, "stress process did not stop"))
        supervisor._note_exit(run)
        verdict = supervisor._grade(run)
        assert verdict.outcome is Outcome.DID_NOT_STOP
        assert verdict.detail == "stress process did not stop"

    def test_a_killed_lane_keeps_the_failure_its_output_holds(self, tmp_path):
        from test_execution import FakeBackend

        supervisor = _idle_supervisor(
            backend=FakeBackend(found=(LaneSignal(Outcome.COMPUTATION, "fake error: FATAL"),))
        )
        run = _LaneRun(
            lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path), run_dir=tmp_path, stopped_at=5.0, sent=signal.SIGKILL
        )
        (tmp_path / execution.STDOUT_LOG).write_bytes(b"")
        (tmp_path / execution.STDERR_LOG).write_bytes(b"")
        run.proc = _ExitedProc(-9)
        run.signals.append(LaneSignal(Outcome.DID_NOT_STOP, "stress process did not stop"))
        supervisor._note_exit(run)
        supervisor._scan_output(run)
        verdict = supervisor._grade(run)
        assert verdict.outcome is Outcome.COMPUTATION
        assert "FATAL" in verdict.detail
        assert "did not stop" in verdict.detail

    @pytest.mark.real_cpu_time
    def test_no_load_window_ever_proves_load(self, tmp_path):
        supervisor = _idle_supervisor()
        no_deadline = _LaneRun(lane=Lane(core_id=0, cpus=(0,), work_dir=tmp_path), stopped_at=5.0)
        no_deadline.proc = _ExitedProc(0)
        no_deadline.cpu_seconds = 1e9
        empty_window = _LaneRun(
            lane=Lane(core_id=1, cpus=(1,), work_dir=tmp_path),
            started_at=5.0,
            deadline=5.0,
            stopped_at=5.0,
            sent=signal.SIGTERM,
        )
        empty_window.proc = _ExitedProc(-15)
        empty_window.cpu_seconds = 0.0
        for run in (no_deadline, empty_window):
            verdict = supervisor._grade(run)
            assert verdict.outcome is Outcome.NO_WORK_DONE
            assert "no work done" in verdict.detail


class TestSchedulerHookGlue:
    def _scheduler(self, tmp_path, monkeypatch, **config):
        from test_scheduler import ScriptedSupervisor, make_scheduler

        monkeypatch.setattr(scheduler_mod, "Supervisor", ScriptedSupervisor)
        ScriptedSupervisor.script = []
        ScriptedSupervisor.created = []
        return make_scheduler(tmp_path, **config), ScriptedSupervisor

    def test_engine_hooks_reach_the_scheduler_callbacks(self, tmp_path, monkeypatch):
        from test_scheduler import ok

        sched, scripted = self._scheduler(tmp_path, monkeypatch, cores_to_test=[0])
        stalls: list[int] = []
        temps: list[float] = []
        phases: list[tuple[int, str]] = []
        statuses: list[float] = []
        sched.on_stall_detected.append(stalls.append)
        sched.on_thermal_throttle.append(temps.append)
        sched.on_phase_change.append(lambda cid, ph: phases.append((cid, ph)))
        sched.on_status_update.append(lambda cid, st: statuses.append(st.elapsed_seconds))

        def hook_driving(sup, lanes, config, duration):
            hooks = sup.kwargs["hooks"]
            hooks.on_status(0, 1.5)
            hooks.on_status(99, 1.0)
            hooks.on_stall(0)
            hooks.on_thermal(99.0)
            return {0: ok(0)}

        scripted.script = [hook_driving]
        sched.run()
        assert 1.5 in statuses
        assert stalls == [0]
        assert temps == [99.0]
        assert phases[0] == (0, "stress")

    def test_a_stop_between_cycles_ends_the_run(self, tmp_path, monkeypatch):
        from test_scheduler import ok

        sched, scripted = self._scheduler(tmp_path, monkeypatch, cycle_count=3)

        def stop_after(sup, lanes, config, duration):
            sup.kwargs["stop_event"].set()
            return {one.core_id: ok(one.core_id) for one in lanes}

        scripted.script = [stop_after]
        results = sched.run()
        assert len(results[0]) == 1
        assert results[1] == []

    def test_variable_stop_on_error_latches_the_stop(self, tmp_path, monkeypatch):
        from test_scheduler import bad, ok

        sched, scripted = self._scheduler(
            tmp_path, monkeypatch, cores_to_test=[0], variable_load=True, variable_load_interval=0.1, stop_on_error=True
        )
        scripted.script = [
            lambda sup, lanes, c, d: {0: ok(0)},
            lambda sup, lanes, c, d: {0: bad(0)},
        ]
        results = sched.run()
        assert results[0][0].outcome is Outcome.COMPUTATION
        assert sched._stop_requested

    def test_a_variable_idle_error_latches_the_stop(self, tmp_path, monkeypatch):
        from test_scheduler import ok

        found = LaneSignal(Outcome.OWN_CORE_MCE, "MCE during idle transition: bang")
        monkeypatch.setattr(execution, "watch_idle", lambda **kwargs: found)
        sched, scripted = self._scheduler(
            tmp_path, monkeypatch, cores_to_test=[0], variable_load=True, variable_load_interval=0.1, stop_on_error=True
        )
        scripted.script = [
            lambda sup, lanes, c, d: {0: ok(0)},
            lambda sup, lanes, c, d: {0: ok(0)},
        ]
        results = sched.run()
        assert results[0][0].outcome is Outcome.OWN_CORE_MCE
        assert "idle transition" in results[0][0].detail
        assert sched._stop_requested

    def test_an_idle_error_with_stop_on_error_latches_the_stop(self, tmp_path, monkeypatch):
        from test_scheduler import ok

        found = LaneSignal(Outcome.OWN_CORE_MCE, "MCE during inter-core idle: bang")
        monkeypatch.setattr(
            execution, "watch_idle", lambda **kwargs: found if kwargs["phase"] == "inter-core idle" else None
        )
        sched, scripted = self._scheduler(tmp_path, monkeypatch, idle_between_cores=0.01, stop_on_error=True)
        scripted.script = [lambda sup, lanes, c, d: {0: ok(0)}]
        results = sched.run()
        assert results[0][0].passed is True
        assert sched._stop_requested
        assert results[1] == []


class TestRemainingLoopEdges:
    def test_the_in_loop_output_scan_fails_the_lane_mid_run(self, tmp_path):
        from test_execution import FakeBackend

        backend = FakeBackend(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            found=(LaneSignal(Outcome.COMPUTATION, "fake error: SUMOUT"),),
        )
        supervisor = _idle_supervisor(backend=backend)
        lane = Lane(core_id=0, cpus=(0,), work_dir=tmp_path / "core_0")
        with patch.object(execution, "OUTPUT_SCAN_INTERVAL", 0.0):
            verdict = supervisor.run([lane], StressConfig(), 5.0)[0]
        assert verdict.outcome is Outcome.COMPUTATION
        assert "SUMOUT" in verdict.detail
        assert verdict.duration_seconds < 5.0


CUT_NAME = b"\xe2\x82x) R 1 2"


def proc_stat(pid: int, ppid: int, *, pgrp: int = 0, ticks: tuple[int, ...] = (0, 0, 0, 0)) -> bytes:
    rest = ["S", str(ppid), str(pgrp), *["0"] * 8, *map(str, ticks), *["0"] * 40]
    return f"{pid} (".encode() + CUT_NAME + b") " + " ".join(rest).encode() + b"\n"


class TestNamesCutInsideACharacter:
    def test_the_tree_walk_and_its_reads_keep_a_process_whose_name_is_not_utf8(self, tmp_path):
        for pid, ppid in ((700, 1), (701, 700)):
            task = tmp_path / str(pid) / "task" / str(pid)
            task.mkdir(parents=True)
            (tmp_path / str(pid) / "stat").write_bytes(proc_stat(pid, ppid))
            (task / "stat").write_bytes(proc_stat(pid, ppid))
            (task / "status").write_bytes(b"Name:\t" + CUT_NAME + b"\nCpus_allowed_list:\t" + str(pid - 697).encode())
        assert containment.observed_tree_cpus(700, proc_base=tmp_path) == {3, 4}
        assert sorted(
            tid for _name, tid, _priority, _policy in containment.tree_scheduling(700, proc_base=tmp_path)
        ) == [
            700,
            701,
        ]

    @pytest.mark.real_cpu_time
    def test_a_group_member_whose_name_is_not_utf8_is_counted(self, tmp_path):
        (tmp_path / "800").mkdir()
        (tmp_path / "800" / "stat").write_bytes(proc_stat(800, 1, pgrp=800, ticks=(100, 50, 7, 3)))
        assert containment.process_group_cpu_seconds(800, proc_base=tmp_path) == 160 / os.sysconf("SC_CLK_TCK")
