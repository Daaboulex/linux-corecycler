"""Simultaneous per-core stress: one pinned process per core, per-core verdicts."""

from __future__ import annotations

import contextlib
import copy
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .backends.base import KILLED_BY_US_CODES, StressConfig, StressResult
from .detector import ErrorDetector, MCEEvent
from .scheduler import CoreScheduler, SchedulerConfig

if TYPE_CHECKING:
    from .backends.base import StressBackend
    from .topology import CPUTopology

log = logging.getLogger(__name__)

_ERROR_POLL_INTERVAL = 5.0
_STALL_GRACE_SECONDS = 5.0


@dataclass(slots=True)
class _Lane:
    core_id: int
    cpus: set[int]
    cpu_list: str
    work_dir: Path
    proc: subprocess.Popen | None = None
    verdict: StressResult | None = None
    last_active: float = 0.0
    prev_times: tuple[int, int] | None = None


class ParallelStress:
    """Runs every configured core's stress process at the same time.

    A failure on any lane (backend error file, own-core MCE, crash signal,
    external kill, stall) stops the whole batch and is attributed to exactly
    that core; lanes without a verdict when the batch stops stay absent from
    the results rather than being invented.
    """

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
        self.stress_config = copy.copy(stress_config)
        self.config = scheduler_config
        self.work_dir = work_dir or Path("/tmp/corecycler")
        self.detector = ErrorDetector()
        self.observed_mce: list[MCEEvent] = []
        self._stop_event = threading.Event()
        self._lanes: dict[int, _Lane] = {}
        self._we_killed = False
        self._thermal_over_since: float | None = None

    def stop(self) -> None:
        self._stop_event.set()
        self._kill_all()

    force_stop = stop

    def run(self) -> dict[int, StressResult]:
        self._stop_event.clear()
        self.observed_mce = []
        self.detector.reset()
        self.work_dir.mkdir(parents=True, exist_ok=True)

        cores = sorted(self.config.cores_to_test or self.topology.cores.keys())
        cpu_to_core: dict[int, int] = {}
        for core_id in cores:
            info = self.topology.cores.get(core_id)
            if not info or not info.logical_cpus:
                return {core_id: self._startup_fail(core_id, f"core {core_id} not in topology")}
            cpus = set(info.logical_cpus)
            for c in cpus:
                cpu_to_core[c] = core_id
            self._lanes[core_id] = _Lane(
                core_id=core_id,
                cpus=cpus,
                cpu_list=",".join(str(c) for c in sorted(cpus)),
                work_dir=self.work_dir / f"core_{core_id}",
            )

        start = time.monotonic()
        try:
            for lane in self._lanes.values():
                cfg = copy.copy(self.stress_config)
                cfg.threads = len(lane.cpus)
                lane.work_dir.mkdir(parents=True, exist_ok=True)
                self.backend.prepare(lane.work_dir, cfg)
                cmd = ["taskset", "-c", lane.cpu_list] + self.backend.get_command(cfg, lane.work_dir)
                try:
                    lane.proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        cwd=str(lane.work_dir),
                        preexec_fn=CoreScheduler._make_preexec(),
                    )
                except (OSError, RuntimeError, TypeError) as e:
                    self._kill_all()
                    return {lane.core_id: self._startup_fail(lane.core_id, f"Failed to start stress test: {e}")}
                lane.last_active = time.monotonic()

            deadline = start + self.config.seconds_per_core
            last_error_poll = start
            while not self._stop_event.is_set() and time.monotonic() < deadline:
                if not self._check_temperature():
                    first = min(self._lanes)
                    self._fail_lane(
                        self._lanes[first],
                        f"CPU temperature exceeded {self.config.max_temperature} C safety limit",
                        elapsed=time.monotonic() - start,
                    )
                    break

                events = self.detector.check_mce()
                if events:
                    self.observed_mce.extend(events)
                    hit = False
                    for e in events:
                        core = cpu_to_core.get(e.cpu, min(self._lanes) if e.cpu == -1 else None)
                        lane = self._lanes.get(core) if core is not None else None
                        if lane is not None and lane.verdict is None:
                            self._fail_lane(
                                lane,
                                f"MCE during parallel stress: {e.message}",
                                elapsed=time.monotonic() - start,
                            )
                            hit = True
                    if hit:
                        break

                now = time.monotonic()
                if now - last_error_poll >= _ERROR_POLL_INTERVAL:
                    last_error_poll = now
                    if self._poll_backend_errors(start):
                        break

                if self._poll_exits_and_stalls(start):
                    break

                time.sleep(self.config.poll_interval)
        finally:
            self._kill_all()
            drained = self.detector.check_mce()
            if drained:
                self.observed_mce.extend(drained)
            elapsed = time.monotonic() - start
            results: dict[int, StressResult] = {}
            for lane in self._lanes.values():
                if lane.verdict is None and lane.proc is not None:
                    lane.verdict = self._final_verdict(lane, elapsed)
                if lane.verdict is not None:
                    results[lane.core_id] = lane.verdict
                self.backend.cleanup(
                    lane.work_dir,
                    preserve_on_error=lane.verdict is not None and not lane.verdict.passed,
                )
            self._lanes = {}
        return results

    def _startup_fail(self, core_id: int, msg: str) -> StressResult:
        return StressResult(
            core_id=core_id, passed=False, duration_seconds=0.0,
            error_message=msg, error_type="startup",
        )

    def _fail_lane(self, lane: _Lane, msg: str, elapsed: float) -> None:
        lane.verdict = StressResult(
            core_id=lane.core_id,
            passed=False,
            duration_seconds=elapsed,
            error_message=msg,
            error_type=CoreScheduler._classify_error(msg),
        )
        self._stop_event.set()

    def _poll_backend_errors(self, start: float) -> bool:
        for lane in self._lanes.values():
            if lane.verdict is not None:
                continue
            err = self.backend.poll_errors(lane.work_dir)
            if err:
                self._fail_lane(lane, err, elapsed=time.monotonic() - start)
                return True
        return False

    def _poll_exits_and_stalls(self, start: float) -> bool:
        now = time.monotonic()
        for lane in self._lanes.values():
            if lane.verdict is not None or lane.proc is None:
                continue
            rc = lane.proc.poll()
            if rc is not None:
                out, err = "", ""
                with contextlib.suppress(Exception):
                    out, err = lane.proc.communicate(timeout=2)
                if not self._we_killed and rc in KILLED_BY_US_CODES:
                    self._fail_lane(lane, f"Stress process killed externally (code {rc})", now - start)
                    return True
                if rc != 0 and now - start < 2.0:
                    lane.verdict = self._startup_fail(
                        lane.core_id, f"stress exited with code {rc} at startup"
                    )
                    self._stop_event.set()
                    return True
                passed, msg = self.backend.parse_output(out or "", err or "", rc)
                if passed:
                    lane.verdict = StressResult(
                        core_id=lane.core_id, passed=True, duration_seconds=now - start,
                    )
                    continue
                self._fail_lane(lane, msg or f"stress exited with code {rc}", now - start)
                return True
            if now - start >= _STALL_GRACE_SECONDS:
                primary = min(lane.cpus)
                cur = _cpu_times(primary)
                busy = _busy(lane.prev_times, cur)
                lane.prev_times = cur
                if busy is None or busy > 0.05:
                    lane.last_active = now
                elif now - lane.last_active > self.config.stall_timeout:
                    self._fail_lane(
                        lane,
                        f"Stress test stalled on core {lane.core_id} "
                        f"(CPU usage near 0 for {self.config.stall_timeout:.0f}s)",
                        now - start,
                    )
                    return True
        return False

    def _final_verdict(self, lane: _Lane, elapsed: float) -> StressResult | None:
        out, err = "", ""
        with contextlib.suppress(Exception):
            out, err = lane.proc.communicate(timeout=2)
        rc = lane.proc.returncode or 0
        # taskset masks a missing/broken target binary: it launches fine and
        # exits nonzero within moments — that ran nothing and proves nothing.
        if rc != 0 and rc not in KILLED_BY_US_CODES and elapsed < 2.0:
            return self._startup_fail(
                lane.core_id, f"stress exited with code {rc} at startup"
            )
        if self._stop_event.is_set() and lane.verdict is None and rc in KILLED_BY_US_CODES:
            live_err = self.backend.poll_errors(lane.work_dir)
            if live_err:
                return StressResult(
                    core_id=lane.core_id, passed=False, duration_seconds=elapsed,
                    error_message=live_err,
                    error_type=CoreScheduler._classify_error(live_err),
                )
            passed, msg = self.backend.parse_output(out or "", err or "", rc)
            if not passed and msg:
                return StressResult(
                    core_id=lane.core_id, passed=False, duration_seconds=elapsed,
                    error_message=msg, error_type=CoreScheduler._classify_error(msg),
                )
            return StressResult(core_id=lane.core_id, passed=True, duration_seconds=elapsed)
        passed, msg = self.backend.parse_output(out or "", err or "", rc)
        return StressResult(
            core_id=lane.core_id, passed=passed, duration_seconds=elapsed,
            error_message=None if passed else msg,
            error_type=None if passed else CoreScheduler._classify_error(msg),
        )

    def _check_temperature(self) -> bool:
        temp = CoreScheduler._read_cpu_temperature()
        if temp is None:
            return not self.config.require_thermal_sensor
        limit = self.config.max_temperature
        if temp >= limit + self.config.over_temp_hard_margin:
            return False
        if temp >= limit:
            now = time.monotonic()
            if self._thermal_over_since is None:
                self._thermal_over_since = now
            return now - self._thermal_over_since < self.config.over_temp_grace_seconds
        self._thermal_over_since = None
        return True

    def _kill_all(self) -> None:
        self._we_killed = True
        for lane in self._lanes.values():
            proc = lane.proc
            if proc is None or proc.poll() is not None:
                continue
            import os
            import signal

            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        for lane in self._lanes.values():
            proc = lane.proc
            if proc is None:
                continue
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                import os
                import signal

                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                with contextlib.suppress(Exception):
                    proc.wait(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    with contextlib.suppress(OSError):
                        stream.close()


def _cpu_times(cpu_id: int) -> tuple[int, int] | None:
    try:
        with open("/proc/stat") as f:
            prefix = f"cpu{cpu_id} "
            for line in f:
                if line.startswith(prefix):
                    vals = [int(x) for x in line.split()[1:]]
                    return vals[3] + vals[4], sum(vals)
    except (OSError, ValueError, IndexError):
        pass
    return None


def _busy(prev: tuple[int, int] | None, now: tuple[int, int] | None) -> float | None:
    if prev is None or now is None:
        return None
    d_total = now[1] - prev[1]
    if d_total <= 0:
        return None
    return 1.0 - ((now[0] - prev[0]) / d_total)
