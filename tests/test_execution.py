"""Behavior spec for the one supervised execution engine."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from corecycler.config.tools import Resolution
from corecycler.engine import containment, execution
from corecycler.engine.backends.base import StressBackend, StressConfig, StressMode
from corecycler.engine.execution import (
    Lane,
    SuperviseHooks,
    Supervisor,
    ThermalWatch,
    busy_fraction,
    classify_error,
    cpu_times,
    kill_process_group,
    watch_idle,
)

if TYPE_CHECKING:
    from pathlib import Path


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class FakeBackend(StressBackend):
    name = "fake"

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        parse=(True, None),
        poll_error: str | None = None,
        prepare_exc: Exception | None = None,
        assert_exc: Exception | None = None,
    ) -> None:
        super().__init__()
        self.command = command or _child("import time; time.sleep(0.4)")
        self.parse = parse
        self.poll_error = poll_error
        self.prepare_exc = prepare_exc
        self.assert_exc = assert_exc
        self.prepared: list[Path] = []
        self.cleaned: list[tuple[Path, bool]] = []

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        return list(self.command)

    def parse_output(self, stdout: str, stderr: str, returncode: int):
        return self.parse

    def get_supported_modes(self):
        return [StressMode.SSE]

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        if self.prepare_exc is not None:
            raise self.prepare_exc
        work_dir.mkdir(parents=True, exist_ok=True)
        self.prepared.append(work_dir)

    def assert_prepared(self, work_dir: Path) -> None:
        if self.assert_exc is not None:
            raise self.assert_exc

    def poll_errors(self, work_dir: Path) -> str | None:
        return self.poll_error

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        self.cleaned.append((work_dir, preserve_on_error))


class FakeDetector:
    def __init__(self, batches: list[list] | None = None) -> None:
        self.batches = list(batches or [])

    def reset(self) -> None:
        pass

    def check_mce(self):
        if self.batches:
            return self.batches.pop(0)
        return []


class Event:
    def __init__(self, cpu: int, message: str = "boom") -> None:
        self.cpu = cpu
        self.bank = 0
        self.message = message
        self.corrected = False
        self.timestamp = 0.0
        self.raw_ts = 0.0


def cool_thermal() -> ThermalWatch:
    return ThermalWatch(
        max_temperature=95.0,
        grace_seconds=3.0,
        hard_margin=8.0,
        require_sensor=False,
        read=lambda: 40.0,
    )


def make_supervisor(
    backend: FakeBackend,
    *,
    detector: FakeDetector | None = None,
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


def lane(tmp_path: Path, core_id: int = 0, cpus: tuple[int, ...] = (0,)) -> Lane:
    return Lane(core_id=core_id, cpus=cpus, work_dir=tmp_path / f"core_{core_id}")


def run_one(supervisor: Supervisor, one: Lane, duration: float):
    return supervisor.run([one], lambda _lane: StressConfig(), duration)[one.core_id]


class TestLaunchRefusals:
    def test_prepare_failure_is_a_startup_fault(self, tmp_path):
        backend = FakeBackend(prepare_exc=OSError("read-only work dir"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "startup"
        assert "read-only work dir" in verdict.error_message

    def test_unreadable_config_refuses_the_launch(self, tmp_path):
        backend = FakeBackend(assert_exc=OSError("local.txt is missing"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict is not None and verdict.error_type == "startup"
        assert "local.txt" in verdict.error_message

    def test_missing_containment_refuses_the_launch(self, tmp_path):
        backend = FakeBackend()

        def refuse(cpus):
            raise containment.ContainmentUnavailable("no systemd-run")

        supervisor, _, _ = make_supervisor(backend, containment_for=refuse)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict is not None and verdict.error_type == "startup"
        assert "no systemd-run" in verdict.error_message

    def test_missing_binary_is_a_startup_fault(self, tmp_path):
        backend = FakeBackend(command=["/nonexistent/stress-binary"])
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict is not None and verdict.error_type == "startup"
        assert "Failed to start" in verdict.error_message


class TestVerdicts:
    def test_a_process_killed_at_the_deadline_passes(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.5)"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 0.15)
        assert verdict is not None and verdict.passed

    def test_an_instant_nonzero_exit_proves_nothing(self, tmp_path):
        backend = FakeBackend(_child("import sys; sys.exit(3)"))
        supervisor, _, _ = make_supervisor(backend)
        with patch.object(execution, "STARTUP_WINDOW_SECONDS", 60.0):
            verdict = run_one(supervisor, lane(tmp_path), 1.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "startup"
        assert "verdict unavailable" in verdict.error_message or "at startup" in verdict.error_message

    def test_a_parsed_failure_is_attributed(self, tmp_path):
        backend = FakeBackend(
            _child("import time; time.sleep(0.3)"),
            parse=(False, "fake error: FATAL"),
        )
        supervisor, _, _ = make_supervisor(backend)
        with patch.object(execution, "STARTUP_WINDOW_SECONDS", 0.05):
            verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict is not None and not verdict.passed
        assert "FATAL" in verdict.error_message

    def test_a_clean_exit_after_startup_passes(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.3)"))
        supervisor, _, _ = make_supervisor(backend)
        with patch.object(execution, "STARTUP_WINDOW_SECONDS", 0.05):
            verdict = run_one(supervisor, lane(tmp_path), 2.0)
        assert verdict is not None and verdict.passed

    def test_a_live_error_file_beats_a_clean_exit(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.5)"), poll_error="fake error: SUMOUT")
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 0.2)
        assert verdict is not None and not verdict.passed
        assert "SUMOUT" in verdict.error_message

    def test_an_external_kill_is_a_failure_not_a_verdict(self, tmp_path):
        backend = FakeBackend(_child("import os, signal, time; time.sleep(0.1); os.kill(os.getpid(), signal.SIGKILL)"))
        supervisor, _, _ = make_supervisor(backend)
        verdict = run_one(supervisor, lane(tmp_path), 4.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "killed"

    def test_an_interrupted_lane_gets_no_invented_verdict(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(5)"))
        supervisor, stop, _ = make_supervisor(backend)
        threading.Timer(0.08, stop.set).start()
        verdict = run_one(supervisor, lane(tmp_path), 10.0)
        assert verdict is None

    def test_no_stress_process_outlives_the_run(self, tmp_path):
        marker = tmp_path / "pid"
        backend = FakeBackend(_child(f"import os, time; open('{marker}', 'w').write(str(os.getpid())); time.sleep(30)"))
        supervisor, stop, _ = make_supervisor(backend)
        threading.Timer(0.2, stop.set).start()
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


class TestMceAttribution:
    def test_an_own_cpu_event_fails_the_lane(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(5)"))
        detector = FakeDetector([[Event(cpu=16)]])
        supervisor, _, seen = make_supervisor(backend, detector=detector)
        verdict = run_one(supervisor, lane(tmp_path, cpus=(0, 16)), 5.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "mce"
        assert len(seen) == 1

    def test_a_foreign_event_is_evidence_not_a_verdict(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.4)"))
        detector = FakeDetector([[Event(cpu=9)]])
        supervisor, _, seen = make_supervisor(backend, detector=detector)
        verdict = run_one(supervisor, lane(tmp_path, cpus=(0, 16)), 0.15)
        assert verdict is not None and verdict.passed
        assert [e.cpu for e in seen] == [9]

    def test_an_unattributed_event_stops_the_batch_without_blame(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(60)"))
        detector = FakeDetector([[Event(cpu=-1)]])
        supervisor, _, _ = make_supervisor(backend, detector=detector)
        verdicts = supervisor.run(
            [lane(tmp_path, 0, (0,)), lane(tmp_path, 5, (5,))],
            lambda _lane: StressConfig(),
            60.0,
        )
        assert verdicts[0] is not None
        assert verdicts[0].error_type == "mce_unattributed"
        assert verdicts[5] is None

    def test_late_events_drained_at_teardown_still_flip_the_verdict(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(60)"))
        detector = FakeDetector([[Event(cpu=0)]])
        supervisor, _, _ = make_supervisor(backend, detector=detector, poll_interval=1.0)
        verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 0.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "mce"


class TestStallWatchdog:
    def test_an_idle_lane_is_failed_as_stalled(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, stall_timeout=0.05)
        with (
            patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
            patch.object(
                execution,
                "cpu_times",
                side_effect=[(i * 100, i * 100) for i in range(1, 400)],
            ),
        ):
            stalls: list[int] = []
            supervisor.hooks = SuperviseHooks(on_stall=stalls.append)
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "stall"
        assert stalls == [0]

    def test_a_busy_lane_is_not_accused(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.4)"))
        supervisor, _, _ = make_supervisor(backend, stall_timeout=0.05)
        with (
            patch.object(execution, "STARTUP_WINDOW_SECONDS", 0.05),
            patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
            patch.object(
                execution,
                "cpu_times",
                side_effect=[(0, i * 100) for i in range(1, 400)],
            ),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict is not None and verdict.passed

    def test_an_unreadable_cpu_cannot_accuse_a_lane(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.4)"))
        supervisor, _, _ = make_supervisor(backend, stall_timeout=0.05)
        with (
            patch.object(execution, "STARTUP_WINDOW_SECONDS", 0.05),
            patch.object(execution, "STALL_GRACE_SECONDS", 0.0),
            patch.object(execution, "cpu_times", return_value=None),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict is not None and verdict.passed


class TestContainmentWatchdog:
    def _contained(self, cpus):
        return containment.Containment(prefix=[], unit="cc-test")

    def test_a_widened_kernel_record_is_a_containment_fault(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value={0, 3, 7}),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "startup"
        assert "containment fault" in verdict.error_message

    def test_a_matching_kernel_record_raises_no_alarm(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(0.4)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(execution, "STARTUP_WINDOW_SECONDS", 0.05),
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value={0}),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict is not None and verdict.passed

    def test_a_scope_that_never_adopts_the_payload_is_a_fault(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value=None),
            patch.object(execution, "CONTAINMENT_GRACE_SECONDS", 0.05),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict is not None and not verdict.passed
        assert "never adopted" in verdict.error_message

    def test_a_vanished_kernel_record_is_a_fault(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(30)"))
        supervisor, _, _ = make_supervisor(backend, containment_for=self._contained)
        with (
            patch.object(containment, "payload_cgroup", return_value="/user.slice/cc-test.scope"),
            patch.object(containment, "scope_effective_cpus", return_value=None),
        ):
            verdict = run_one(supervisor, lane(tmp_path, cpus=(0,)), 5.0)
        assert verdict is not None and not verdict.passed
        assert "vanished" in verdict.error_message


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

    def test_a_trip_fails_the_batch_and_fires_the_hook(self, tmp_path):
        backend = FakeBackend(_child("import time; time.sleep(5)"))
        hot = ThermalWatch(
            max_temperature=95,
            grace_seconds=0,
            hard_margin=8,
            require_sensor=False,
            read=lambda: 104.0,
        )
        seen_temps: list[float] = []
        supervisor, _, _ = make_supervisor(backend, thermal=hot, hooks=SuperviseHooks(on_thermal=seen_temps.append))
        verdict = run_one(supervisor, lane(tmp_path), 5.0)
        assert verdict is not None and not verdict.passed
        assert verdict.error_type == "thermal"
        assert seen_temps == [104.0]


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
        start = time.monotonic()
        assert watch_idle(**self._args(duration=5.0, stop_event=stop)) is None
        assert time.monotonic() - start < 1.0

    def test_an_overheat_during_idle_is_reported(self):
        hot = ThermalWatch(
            max_temperature=95,
            grace_seconds=0,
            hard_margin=8,
            require_sensor=False,
            read=lambda: 104.0,
        )
        error = watch_idle(**self._args(thermal=hot, duration=5.0))
        assert error is not None and "temperature" in error

    def test_an_own_cpu_mce_ends_the_idle(self):
        error = watch_idle(**self._args(detector=FakeDetector([[Event(cpu=0)]]), duration=5.0))
        assert error is not None and "MCE during idle stability" in error

    def test_a_foreign_mce_is_recorded_not_reported(self):
        seen: list = []
        error = watch_idle(**self._args(detector=FakeDetector([[Event(cpu=9)]]), observed=seen))
        assert error is None
        assert [e.cpu for e in seen] == [9]


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

    def test_kill_process_group_terminates_a_group(self):
        proc = subprocess.Popen(
            _child("import time; time.sleep(30)"),
            preexec_fn=execution.make_preexec(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        kill_process_group(proc)
        assert proc.poll() is not None

    def test_kill_process_group_tolerates_an_already_dead_process(self):
        proc = subprocess.Popen(_child("pass"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        proc.wait(timeout=5)
        kill_process_group(proc)
        assert proc.poll() is not None

    def test_kill_process_group_escalates_to_sigkill(self):
        proc = subprocess.Popen(
            _child("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"),
            preexec_fn=execution.make_preexec(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        start = time.monotonic()
        kill_process_group(proc)
        assert proc.poll() is not None
        assert time.monotonic() - start < 10

    def test_kill_process_group_escalation_and_stream_close_deterministic(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        stdout = MagicMock()
        stderr = MagicMock()
        proc = SimpleNamespace(
            pid=4321,
            stdout=stdout,
            stderr=stderr,
            poll=lambda: None,
            wait=MagicMock(side_effect=[subprocess.TimeoutExpired("cmd", 3), None]),
        )
        sent: list = []
        monkeypatch.setattr(execution.os, "getpgid", lambda _pid: 4321)
        monkeypatch.setattr(execution.os, "killpg", lambda pgid, s: sent.append(s))
        kill_process_group(proc)
        assert signal.SIGTERM in sent and signal.SIGKILL in sent
        stdout.close.assert_called_once()
        stderr.close.assert_called_once()

    def test_kill_process_group_handles_a_vanished_group(self, monkeypatch):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        wait = MagicMock(return_value=None)
        proc = SimpleNamespace(pid=4321, stdout=None, stderr=None, poll=lambda: None, wait=wait)
        monkeypatch.setattr(execution.os, "getpgid", MagicMock(side_effect=ProcessLookupError))
        killpg = MagicMock()
        monkeypatch.setattr(execution.os, "killpg", killpg)
        kill_process_group(proc)
        killpg.assert_not_called()
        wait.assert_called_once_with(timeout=1)

    def test_reap_zombies_never_raises(self):
        execution.reap_zombies()


class TestClassifyError:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (None, "unknown"),
            ("Failed to start stress test: nope", "startup"),
            ("stress exited at startup (code 2) with no work done — verdict unavailable", "startup"),
            (
                "stress process escaped its CPU boundary to 3 (allowed 0) — containment fault, not a core verdict",
                "startup",
            ),
            ("Machine check without core attribution during stress: bang", "mce_unattributed"),
            ("MCE during stress (CPU 0,16): bang", "mce"),
            ("CPU temperature exceeded 95.0 C safety limit during stress", "thermal"),
            ("Stress test stalled on core 3 (CPU usage near 0 on CPUs 3,19 for 30s)", "stall"),
            ("mprime error: FATAL ERROR: Rounding was 0.5", "computation"),
            ("Stress process killed externally (code 137) — possible OOM or system issue", "killed"),
            ("mprime crashed with SIGSEGV (exit -11)", "crash"),
            ("stress exited with code -9", "crash"),
            ("MCE during idle stability: x", "mce"),
            ("error during idle stability test", "idle_instability"),
            ("failure in variable segment", "load_transition"),
            ("something else entirely", "unknown"),
        ],
    )
    def test_classification(self, message, expected):
        assert classify_error(message) == expected


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

    def test_contain_builds_a_user_scope_prefix(self, tmp_path):
        systemd_run = tmp_path / "systemd-run"
        systemd_run.write_text("#!/bin/sh\n")
        systemd_run.chmod(0o755)
        setpriv = tmp_path / "setpriv"
        setpriv.write_text("#!/bin/sh\n")
        setpriv.chmod(0o755)

        def fake_resolve(key):
            path = {"systemd-run": systemd_run, "setpriv": setpriv}.get(key)
            return Resolution(key=key, path=path, origin="path")

        with (
            patch.object(containment.tools, "resolve", side_effect=fake_resolve),
            patch.object(containment, "available_mechanism", return_value=containment.MECHANISM_USER),
        ):
            prefix = containment.contain((16, 0))
        assert prefix.prefix[0] == str(systemd_run)
        assert "--user" in prefix.prefix
        assert "--unit" in prefix.prefix
        assert prefix.unit.startswith("corecycler-")
        assert "AllowedCPUs=0,16" in prefix.prefix
        assert prefix.prefix[-1] == "--"
        assert str(setpriv) in prefix.prefix
        assert "--pdeathsig" in prefix.prefix

    def test_probe_failure_is_cached_but_refreshable(self):
        containment._probe_cache.clear()
        calls = []

        def fake_probe():
            calls.append(1)
            return None

        with patch.object(containment, "_probe_mechanism", side_effect=fake_probe):
            assert containment.available_mechanism() is None
            assert containment.available_mechanism() is None
            assert containment.available_mechanism(refresh=True) is None
        containment._probe_cache.clear()
        assert len(calls) == 2

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
