"""Core cycling scheduler — runs stress tests per-core with error detection."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING

from .backends.base import StressConfig, StressResult
from .detector import ErrorDetector

if TYPE_CHECKING:
    from .backends.base import StressBackend
    from .topology import CPUTopology

log = logging.getLogger(__name__)


class TestState(Enum):
    IDLE = auto()
    RUNNING = auto()
    PAUSED = auto()
    STOPPING = auto()
    FINISHED = auto()


@dataclass(slots=True)
class CoreTestStatus:
    core_id: int
    ccd: int | None = None
    state: str = "pending"  # pending, testing, passed, failed, skipped
    iterations: int = 0
    errors: int = 0
    last_error: str | None = None
    elapsed_seconds: float = 0.0
    current_fft: int | None = None


@dataclass(slots=True)
class SchedulerConfig:
    seconds_per_core: int = 360  # 6 minutes default
    iterations_per_core: int = 0  # 0 = time-based only
    cores_to_test: list[int] | None = None  # None = all physical cores
    test_smt_siblings: bool = False  # also test SMT thread?
    stop_on_error: bool = False
    cycle_count: int = 1  # how many full cycles through all cores
    poll_interval: float = 1.0  # seconds between status checks
    max_temperature: float = 95.0  # celsius — pause/stop if exceeded
    stall_timeout: float = 30.0  # seconds of near-zero CPU before declaring stall


class CoreScheduler:
    """Orchestrates per-core stress testing with cycling and error detection."""

    def __init__(
        self,
        topology: CPUTopology,
        backend: StressBackend,
        stress_config: StressConfig,
        scheduler_config: SchedulerConfig,
        work_dir: Path | None = None,
    ) -> None:
        self.topology = topology
        self.backend = backend
        self.stress_config = stress_config
        self.config = scheduler_config
        self.work_dir = work_dir or Path("/tmp/linux-corecycler")
        self.detector = ErrorDetector()

        self.state = TestState.IDLE
        self.results: dict[int, list[StressResult]] = {}
        self.core_status: dict[int, CoreTestStatus] = {}
        self._process: subprocess.Popen | None = None
        self._current_core: int | None = None
        self._current_cycle: int = 0
        self._stop_requested = False
        self._thermal_paused = False

        # callbacks for GUI integration
        self.on_core_start: list = []  # (core_id, cycle) -> None
        self.on_core_finish: list = []  # (core_id, result) -> None
        self.on_status_update: list = []  # (core_id, status) -> None
        self.on_cycle_complete: list = []  # (cycle_num) -> None
        self.on_test_complete: list = []  # (results) -> None
        self.on_thermal_throttle: list = []  # (temperature) -> None
        self.on_stall_detected: list = []  # (core_id) -> None

        self._init_core_status()

    def _init_core_status(self) -> None:
        cores = self._get_test_cores()
        for core_id in cores:
            core_info = self.topology.cores.get(core_id)
            self.core_status[core_id] = CoreTestStatus(
                core_id=core_id,
                ccd=core_info.ccd if core_info else None,
            )
            self.results[core_id] = []

    def _get_test_cores(self) -> list[int]:
        if self.config.cores_to_test is not None:
            return sorted(self.config.cores_to_test)
        return sorted(self.topology.cores.keys())

    def run(self) -> dict[int, list[StressResult]]:
        """Run the full test cycle. Blocks until complete. Use run_async() for GUI."""
        self.state = TestState.RUNNING
        self._stop_requested = False
        self.detector.reset()
        self.work_dir.mkdir(parents=True, exist_ok=True)

        cores = self._get_test_cores()

        try:
            for cycle in range(self.config.cycle_count):
                self._current_cycle = cycle
                if self._stop_requested:
                    break

                for core_id in cores:
                    if self._stop_requested:
                        break
                    self._test_core(core_id, cycle)

                for cb in self.on_cycle_complete:
                    cb(cycle)

        finally:
            self.state = TestState.FINISHED
            for cb in self.on_test_complete:
                cb(self.results)

        return self.results

    def stop(self) -> None:
        """Request graceful stop after current core finishes."""
        self._stop_requested = True
        self.state = TestState.STOPPING
        self._kill_current()

    def force_stop(self) -> None:
        """Immediately kill the running stress test."""
        self._stop_requested = True
        self.state = TestState.STOPPING
        self._kill_current()

    # ------------------------------------------------------------------
    # Temperature monitoring
    # ------------------------------------------------------------------

    @staticmethod
    def _read_cpu_temperature() -> float | None:
        """Read CPU temperature from hwmon (Tctl/Tdie for AMD, coretemp for Intel).

        Returns temperature in celsius or None if unavailable.
        """
        hwmon_base = Path("/sys/class/hwmon")
        if not hwmon_base.exists():
            return None

        try:
            for hwmon_dir in hwmon_base.iterdir():
                name_file = hwmon_dir / "name"
                if not name_file.exists():
                    continue
                try:
                    name = name_file.read_text().strip()
                except OSError:
                    continue

                # AMD: k10temp exposes Tctl/Tdie; Intel: coretemp
                if name not in ("k10temp", "coretemp", "zenpower"):
                    continue

                # find highest temp input
                max_temp = 0.0
                for temp_input in sorted(hwmon_dir.glob("temp*_input")):
                    try:
                        millideg = int(temp_input.read_text().strip())
                        temp_c = millideg / 1000.0
                        if temp_c > max_temp:
                            max_temp = temp_c
                    except (ValueError, OSError):
                        continue

                if max_temp > 0:
                    return max_temp
        except OSError:
            pass
        return None

    def _check_temperature(self) -> bool:
        """Check CPU temperature against the safety limit.

        Returns True if temperature is safe, False if over limit.
        When over limit, fires the on_thermal_throttle callback.
        """
        temp = self._read_cpu_temperature()
        if temp is None:
            return True  # can't read -> don't block

        if temp >= self.config.max_temperature:
            log.warning(
                "CPU temperature %.1f C exceeds safety limit %.1f C — stopping test",
                temp,
                self.config.max_temperature,
            )
            self._thermal_paused = True
            for cb in self.on_thermal_throttle:
                cb(temp)
            return False
        return True

    # ------------------------------------------------------------------
    # Stall detection
    # ------------------------------------------------------------------

    @staticmethod
    def _read_core_usage(logical_cpu: int) -> float | None:
        """Read instantaneous CPU usage for a logical CPU from /proc/stat.

        Returns a rough busy percentage (0-100) by sampling twice with a
        short interval, or None on error.
        """
        try:
            def _read_cpu_times(cpu_id: int) -> tuple[int, int] | None:
                stat = Path("/proc/stat").read_text()
                prefix = f"cpu{cpu_id} "
                for line in stat.splitlines():
                    if line.startswith(prefix):
                        parts = line.split()
                        # user nice system idle iowait irq softirq steal
                        vals = [int(x) for x in parts[1:]]
                        idle = vals[3] + vals[4]  # idle + iowait
                        total = sum(vals)
                        return idle, total
                return None

            t1 = _read_cpu_times(logical_cpu)
            if t1 is None:
                return None
            time.sleep(0.25)
            t2 = _read_cpu_times(logical_cpu)
            if t2 is None:
                return None

            idle_delta = t2[0] - t1[0]
            total_delta = t2[1] - t1[1]
            if total_delta == 0:
                return 0.0
            return 100.0 * (1.0 - idle_delta / total_delta)
        except (OSError, ValueError, IndexError):
            return None

    # ------------------------------------------------------------------
    # Core test execution
    # ------------------------------------------------------------------

    def _test_core(self, core_id: int, cycle: int) -> None:
        self._current_core = core_id
        status = self.core_status[core_id]
        status.state = "testing"

        for cb in self.on_core_start:
            cb(core_id, cycle)

        core_info = self.topology.cores.get(core_id)
        if not core_info:
            status.state = "skipped"
            return

        # use first logical CPU of this physical core
        logical_cpu = core_info.logical_cpus[0]

        # prepare backend work directory for this core
        core_work_dir = self.work_dir / f"core_{core_id}"
        self.backend.prepare(core_work_dir, self.stress_config)

        # build command with taskset for CPU pinning
        cmd = self.backend.get_command(self.stress_config, core_work_dir)
        full_cmd = ["taskset", "-c", str(logical_cpu)] + cmd

        start_time = time.monotonic()
        stdout_data = ""
        stderr_data = ""
        error_msg = None
        passed = True
        last_active_time = start_time  # for stall detection

        try:
            self._process = subprocess.Popen(
                full_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(core_work_dir),
                preexec_fn=os.setsid,  # own process group for clean kill
            )

            deadline = start_time + self.config.seconds_per_core

            while self._process.poll() is None:
                if self._stop_requested:
                    break

                now = time.monotonic()
                if now >= deadline:
                    break

                # --- temperature safety check ---
                if not self._check_temperature():
                    passed = False
                    error_msg = (
                        f"CPU temperature exceeded {self.config.max_temperature} C "
                        f"safety limit — test stopped"
                    )
                    status.errors += 1
                    status.last_error = error_msg
                    self._stop_requested = True
                    break

                # --- stall watchdog ---
                usage = self._read_core_usage(logical_cpu)
                if usage is not None:
                    if usage > 5.0:
                        last_active_time = now
                    elif now - last_active_time > self.config.stall_timeout:
                        log.warning(
                            "Stall detected on core %d (CPU%d): "
                            "near-zero usage for %.0f s",
                            core_id,
                            logical_cpu,
                            now - last_active_time,
                        )
                        for cb in self.on_stall_detected:
                            cb(core_id)
                        passed = False
                        error_msg = (
                            f"Stress test stalled on core {core_id} "
                            f"(CPU usage near 0 for {self.config.stall_timeout:.0f}s)"
                        )
                        status.errors += 1
                        status.last_error = error_msg
                        break

                # periodic MCE check
                mce_events = self.detector.check_mce(target_cpu=logical_cpu)
                if mce_events:
                    passed = False
                    error_msg = f"MCE detected on CPU {logical_cpu}: {mce_events[0].message}"
                    status.errors += 1
                    status.last_error = error_msg
                    if self.config.stop_on_error:
                        break

                # update elapsed time
                status.elapsed_seconds = now - start_time
                for cb in self.on_status_update:
                    cb(core_id, status)

                time.sleep(self.config.poll_interval)

            # kill process if still running (timeout or stop requested)
            self._kill_current()

            # collect output
            try:
                stdout_data, stderr_data = self._process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                stdout_data, stderr_data = self._process.communicate()

            # parse backend output for errors
            if passed:  # only check output if no MCE already detected
                returncode = self._process.returncode or 0
                backend_passed, backend_error = self.backend.parse_output(
                    stdout_data, stderr_data, returncode
                )
                if not backend_passed:
                    passed = False
                    error_msg = backend_error
                    status.errors += 1
                    status.last_error = error_msg

        except OSError as e:
            passed = False
            error_msg = f"Failed to start stress test: {e}"
            status.errors += 1
            status.last_error = error_msg
        finally:
            self._process = None
            # Reap any zombies from this process group
            self._reap_zombies()

        elapsed = time.monotonic() - start_time
        status.elapsed_seconds = elapsed
        status.iterations += 1
        status.state = "passed" if passed else "failed"

        result = StressResult(
            core_id=core_id,
            passed=passed,
            duration_seconds=elapsed,
            error_message=error_msg,
            error_type=self._classify_error(error_msg) if error_msg else None,
            iterations_completed=status.iterations,
        )
        self.results[core_id].append(result)

        for cb in self.on_core_finish:
            cb(core_id, result)

        self.backend.cleanup(core_work_dir)

    def _kill_current(self) -> None:
        """Kill the current stress test process and all children in its group.

        Uses SIGTERM first, escalates to SIGKILL, and always calls wait()
        to prevent zombie processes.
        """
        proc = self._process
        if proc is None or proc.poll() is not None:
            return

        pid = proc.pid
        try:
            pgid = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            # Already gone
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            return

        # SIGTERM the whole process group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            # Escalate to SIGKILL
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.warning("Process %d did not exit after SIGKILL", pid)

        # Close pipe fds to avoid resource leaks
        for stream in (proc.stdout, proc.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass

    @staticmethod
    def _reap_zombies() -> None:
        """Reap any zombie child processes to prevent accumulation."""
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            # No child processes — normal
            pass

    @staticmethod
    def _classify_error(msg: str | None) -> str:
        if not msg:
            return "unknown"
        msg_lower = msg.lower()
        if "mce" in msg_lower or "machine check" in msg_lower:
            return "mce"
        if "temperature" in msg_lower or "thermal" in msg_lower:
            return "thermal"
        if "stall" in msg_lower:
            return "stall"
        if any(w in msg_lower for w in ("rounding", "fatal", "illegal", "sumout", "mismatch")):
            return "computation"
        if "timeout" in msg_lower:
            return "timeout"
        if "crash" in msg_lower or "signal" in msg_lower:
            return "crash"
        return "unknown"
