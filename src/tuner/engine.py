"""Automated PBO Curve Optimizer tuner — core state machine and orchestrator.

Drives the coarse-to-fine search: big steps first, fine steps after failure,
confirmation at the settled value. Every state transition persists to SQLite
before acting, so the tuner resumes exactly where it left off after crash/reboot.

Test execution runs on a QThread so the GUI remains responsive.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal, Slot

from engine.backends.base import StressConfig
from engine.scheduler import CoreScheduler, SchedulerConfig
from monitor.msr import MSRReader

from . import persistence as tp
from .config import TunerConfig
from .state import CoreState

if TYPE_CHECKING:
    from engine.backends.base import StressBackend
    from engine.topology import CPUTopology
    from history.db import HistoryDB
    from smu.driver import RyzenSMU

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Worker thread — runs a single core test without blocking the GUI
# ------------------------------------------------------------------


_STRETCH_WARMUP_SECONDS = 5  # skip startup noise (process exec, turbo ramp)
_STRETCH_SAMPLE_INTERVAL = 5  # seconds between APERF/MPERF samples


class _TunerWorker(QThread):
    """Runs one CoreScheduler test on a background thread.

    Optionally samples APERF/MPERF clock stretch during the test via a
    background sampler thread. The sampler waits for turbo to stabilise
    after process startup, then takes periodic 5-second windows and
    reports the **peak** stretch observed — not the average over the
    whole test. This avoids false positives from startup overhead,
    turbo ramp-up, and C-state transitions before load reaches 100%.
    """

    # core_id, passed, error_msg, error_type, duration, peak_stretch_pct
    finished = Signal(int, bool, str, str, float, float)

    def __init__(
        self,
        core_id: int,
        logical_cpu: int,
        scheduler: CoreScheduler,
        msr: MSRReader | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._core_id = core_id
        self._logical_cpu = logical_cpu
        self._scheduler = scheduler
        self._msr = msr

    def run(self) -> None:
        try:
            # Start background stretch sampler (if MSR available)
            stretch_samples: list[float] = []
            stop_event = threading.Event()

            if self._msr and self._msr.is_available():
                sampler = threading.Thread(
                    target=self._stretch_sampler,
                    args=(stretch_samples, stop_event),
                    daemon=True,
                )
                sampler.start()

            start = time.monotonic()
            results = self._scheduler.run()
            elapsed = time.monotonic() - start

            # Stop sampler and collect results
            stop_event.set()
            peak_stretch = max(stretch_samples) if stretch_samples else 0.0

            core_results = results.get(self._core_id, [])
            if core_results:
                r = core_results[0]
                self.finished.emit(
                    self._core_id, r.passed,
                    r.error_message or "", r.error_type or "", elapsed,
                    peak_stretch,
                )
            else:
                self.finished.emit(
                    self._core_id, False, "No result returned", "", elapsed,
                    peak_stretch,
                )
        except Exception as e:
            log.exception("Tuner worker crashed for core %d", self._core_id)
            self.finished.emit(self._core_id, False, str(e), "crash", 0.0, 0.0)

    def _stretch_sampler(
        self, samples: list[float], stop: threading.Event
    ) -> None:
        """Background thread: sample APERF/MPERF stretch during sustained load.

        Waits for warmup (turbo ramp + process startup), then re-primes
        the baseline and samples every interval. Each sample covers only
        its own window — startup noise is discarded.
        """
        cpu = self._logical_cpu
        msr = self._msr
        if not msr:
            return

        # Wait for warmup — let stress process start and turbo stabilise
        if stop.wait(_STRETCH_WARMUP_SECONDS):
            return  # test ended before warmup finished (very short test)

        # Prime fresh baseline AFTER warmup (discards startup noise)
        msr.read_clock_stretch([cpu])

        # Sample at intervals until test ends
        while not stop.wait(_STRETCH_SAMPLE_INTERVAL):
            readings = msr.read_clock_stretch([cpu])
            reading = readings.get(cpu)
            if reading:
                samples.append(reading.stretch_pct)


# ------------------------------------------------------------------
# Engine
# ------------------------------------------------------------------


class TunerEngine(QObject):
    """Orchestrates the automated CO search.

    Emits Qt signals for GUI updates; each individual core test runs
    on a _TunerWorker QThread. This class manages the state machine
    and persists every transition.
    """

    # Signals
    core_state_changed = Signal(int, str, int)  # core_id, phase, offset
    test_completed = Signal(int, int, bool)  # core_id, offset, passed
    session_completed = Signal(str)  # JSON-encoded {core_id: best_offset}
    status_changed = Signal(str)  # global status
    progress_updated = Signal(int, int)  # cores_done, cores_total
    log_message = Signal(str)  # human-readable log entry

    def __init__(
        self,
        db: HistoryDB,
        topology: CPUTopology,
        smu: RyzenSMU | None,
        backend: StressBackend,
        config: TunerConfig | None = None,
        work_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._db = db
        self._topology = topology
        self._smu = smu
        self._backend = backend
        self._config = config or TunerConfig()
        self._work_dir = work_dir or Path("/tmp/corecyclerlx/tuner")

        self._msr = MSRReader()

        self._session_id: int | None = None
        self._core_states: dict[int, CoreState] = {}
        self._status: str = "idle"
        self._paused = False
        self._abort_requested = False
        self._consecutive_start_failures = 0
        self._worker: _TunerWorker | None = None
        self._last_tested_core: int | None = None

        # Clamp max_offset to CPU generation range
        if smu is not None:
            self._config.clamp_max_offset(smu.commands.co_range)

    @property
    def status(self) -> str:
        return self._status

    @property
    def session_id(self) -> int | None:
        return self._session_id

    @property
    def core_states(self) -> dict[int, CoreState]:
        return self._core_states

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start a new tuner session."""
        from history.context import capture_system_context, find_or_create_context

        self._abort_requested = False
        self._paused = False
        self._consecutive_start_failures = 0

        # Capture system context
        num_cores = len(self._topology.cores)
        ctx = capture_system_context(self._smu, num_cores)
        context_id = find_or_create_context(self._db, ctx)

        # Create session
        self._session_id = tp.create_session(
            self._db,
            self._config,
            bios_version=ctx.bios_version,
            cpu_model=self._topology.model_name,
            context_id=context_id,
        )

        # Initialize core states
        cores = self._get_cores_to_test()
        self._core_states = {}

        # Read current CO offsets from SMU if inheriting
        current_offsets: dict[int, int] = {}
        if self._config.inherit_current and self._smu is not None:
            for core_id in cores:
                val = self._smu.get_co_offset(core_id)
                if val is not None:
                    current_offsets[core_id] = val
            self.log_message.emit(
                f"Inherited current CO offsets from SMU: {current_offsets}"
            )

        for core_id in cores:
            start = current_offsets.get(core_id, self._config.start_offset)
            cs = CoreState(core_id=core_id, current_offset=start)
            self._core_states[core_id] = cs
            tp.save_core_state(self._db, self._session_id, cs)

        self._set_status("running")
        self.log_message.emit(
            f"Started tuner session {self._session_id} — "
            f"{len(cores)} cores, coarse step {self._config.coarse_step}, "
            f"fine step {self._config.fine_step}"
        )

        self._run_next()

    def resume(self, session_id: int) -> None:
        """Resume a crashed/paused session.

        Order matters for crash safety:
        1. Advance interrupted cores FIRST (treat crash as failure, back off)
        2. THEN re-apply only safe offsets for cores at known-good values
        This prevents re-applying the exact offset that caused a crash,
        which could crash the system again even at idle with extreme values.
        """
        self._abort_requested = False
        self._paused = False
        self._session_id = session_id

        session = tp.get_session(self._db, session_id)
        if session is None:
            self.log_message.emit(f"Session {session_id} not found")
            return

        self._config = TunerConfig.from_json(session.config_json)
        if self._smu is not None:
            self._config.clamp_max_offset(self._smu.commands.co_range)

        self._core_states = tp.load_core_states(self._db, session_id)

        # Step 1: Advance interrupted cores BEFORE touching SMU.
        # Cores in active test phases crashed at their current_offset — that
        # offset is potentially dangerous. Advance the state machine (treating
        # the crash as a test failure) so it backs off to a safe value.
        for cs in list(self._core_states.values()):
            if cs.phase in ("coarse_search", "fine_search", "confirming"):
                self.log_message.emit(
                    f"Core {cs.core_id} was interrupted at offset {cs.current_offset} "
                    f"— treating as failure and backing off"
                )
                self._advance_core(cs.core_id, passed=False)

        # Step 2: Re-apply CO offsets that are now at safe (backed-off) values.
        # Skip "not_started" and "confirmed" cores — they don't need CO set.
        # _run_next() will apply the correct offset when testing resumes.
        if self._smu is not None:
            failed_cores: list[int] = []
            for cs in self._core_states.values():
                if cs.phase in ("not_started", "confirmed"):
                    continue
                # Only re-apply if the core has a non-zero offset to restore
                if cs.current_offset == 0:
                    continue
                try:
                    success = self._smu.set_co_offset(cs.core_id, cs.current_offset)
                    if not success:
                        failed_cores.append(cs.core_id)
                        self.log_message.emit(
                            f"CO re-apply failed for core {cs.core_id} at offset "
                            f"{cs.current_offset} — read-back mismatch or SMU rejection"
                        )
                except Exception as e:
                    failed_cores.append(cs.core_id)
                    log.warning("Failed to re-apply CO for core %d: %s", cs.core_id, e)
                    self.log_message.emit(
                        f"CO re-apply error for core {cs.core_id}: {e}"
                    )
            if failed_cores:
                self.log_message.emit(
                    f"WARNING: CO offsets could not be re-applied for cores {failed_cores}. "
                    f"SMU access may have changed since last session. "
                    f"These cores will be treated as failures."
                )

        self._set_status("running")
        tp.update_session_status(self._db, session_id, "running")
        self.log_message.emit(f"Resumed session {session_id}")
        self._run_next()

    def pause(self) -> None:
        """Pause after the current test completes."""
        self._paused = True
        self._set_status("paused")
        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "paused")
        self.log_message.emit("Tuner paused — will stop after current test")

    def abort(self) -> None:
        """Stop immediately, save state."""
        self._abort_requested = True
        # Stop the running worker if any
        if self._worker is not None and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(3000)
            self._worker = None
        self._set_status("idle")
        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "aborted")
        self.log_message.emit("Tuner aborted")

    def validate_profile(self, session_id: int) -> None:
        """Re-test all confirmed values from a completed session."""
        self._abort_requested = False
        self._paused = False
        self._session_id = session_id

        profile = tp.get_best_profile(self._db, session_id)
        if not profile:
            self.log_message.emit("No confirmed cores to validate")
            return

        session = tp.get_session(self._db, session_id)
        if session:
            self._config = TunerConfig.from_json(session.config_json)
            if self._smu is not None:
                self._config.clamp_max_offset(self._smu.commands.co_range)

        # Reset confirmed cores to "confirming" for re-validation
        self._core_states = tp.load_core_states(self._db, session_id)
        for core_id, offset in profile.items():
            if core_id in self._core_states:
                cs = self._core_states[core_id]
                cs.phase = "confirming"
                cs.current_offset = offset
                cs.best_offset = offset
                cs.confirm_attempts = 0
                tp.save_core_state(self._db, self._session_id, cs)

        self._set_status("validating")
        tp.update_session_status(self._db, session_id, "validating")
        self.log_message.emit(
            f"Validating {len(profile)} core(s) from session {session_id}"
        )
        self._run_next()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _advance_core(self, core_id: int, passed: bool) -> None:
        """State machine transitions for a single core."""
        cs = self._core_states[core_id]
        cfg = self._config
        direction = cfg.direction  # -1 for undervolting

        match cs.phase:
            case "not_started":
                # First step: enter coarse search
                cs.phase = "coarse_search"
                # Use inherited offset as base when inherit_current is active
                base = cs.current_offset if (cfg.inherit_current and cs.current_offset != 0) else cfg.start_offset
                cs.current_offset = base + direction * cfg.coarse_step
                if self._exceeds_max(cs.current_offset):
                    cs.current_offset = cfg.max_offset

            case "coarse_search":
                if passed:
                    cs.best_offset = cs.current_offset
                    next_offset = cs.current_offset + direction * cfg.coarse_step
                    if self._exceeds_max(next_offset):
                        # Hit the limit — settle here
                        cs.phase = "settled"
                    else:
                        cs.current_offset = next_offset
                else:
                    # Coarse search failed
                    cs.coarse_fail_offset = cs.current_offset
                    if cs.best_offset is None:
                        # Never passed — check abort threshold
                        if cs.current_offset == cfg.start_offset + direction * cfg.coarse_step:
                            self._consecutive_start_failures += 1
                        cs.phase = "settled"  # nothing we can do
                    else:
                        # Fine search between best_offset and coarse_fail
                        cs.phase = "fine_search"
                        cs.current_offset = cs.best_offset + direction * cfg.fine_step

            case "fine_search":
                if passed:
                    cs.best_offset = cs.current_offset
                    next_offset = cs.current_offset + direction * cfg.fine_step
                    # Stop if we'd reach or pass the coarse fail point
                    if cs.coarse_fail_offset is not None and (
                        (direction < 0 and next_offset <= cs.coarse_fail_offset)
                        or (direction > 0 and next_offset >= cs.coarse_fail_offset)
                    ):
                        cs.phase = "settled"
                    elif self._exceeds_max(next_offset):
                        cs.phase = "settled"
                    else:
                        cs.current_offset = next_offset
                else:
                    # Fine search failed — settle at last good value
                    cs.phase = "settled"

            case "settled":
                # Move to confirmation
                if cs.best_offset is not None:
                    cs.phase = "confirming"
                    cs.current_offset = cs.best_offset
                else:
                    # No passing value found at all — mark confirmed at start
                    cs.phase = "confirmed"
                    cs.best_offset = cfg.start_offset
                    cs.current_offset = cfg.start_offset

            case "confirming":
                if passed:
                    cs.phase = "confirmed"
                    cs.confirm_attempts = 0
                else:
                    cs.confirm_attempts += 1
                    if cs.confirm_attempts >= cfg.max_confirm_retries:
                        # Back off and re-enter fine search
                        cs.phase = "failed_confirm"
                    # else: retry confirmation (stays in "confirming")

            case "failed_confirm":
                # Back off by one fine step and re-enter fine search
                if cs.best_offset is not None:
                    cs.best_offset = cs.best_offset - direction * cfg.fine_step
                    if cs.best_offset == cfg.start_offset or (
                        direction < 0 and cs.best_offset > cfg.start_offset
                    ) or (
                        direction > 0 and cs.best_offset < cfg.start_offset
                    ):
                        # Can't back off further
                        cs.phase = "confirmed"
                        cs.current_offset = cfg.start_offset
                    else:
                        cs.phase = "fine_search"
                        cs.current_offset = cs.best_offset
                        cs.confirm_attempts = 0
                else:
                    cs.phase = "confirmed"
                    cs.best_offset = cfg.start_offset
                    cs.current_offset = cfg.start_offset

        # Persist
        if self._session_id:
            tp.save_core_state(self._db, self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)

    def _exceeds_max(self, offset: int) -> bool:
        """Check if offset exceeds max_offset in the configured direction."""
        if self._config.direction < 0:
            return offset < self._config.max_offset
        return offset > self._config.max_offset

    def _pick_next_core(self) -> int | None:
        """Select next core to test based on test_order config."""
        match self._config.test_order:
            case "sequential":
                return self._pick_sequential()
            case "round_robin":
                return self._pick_round_robin()
            case "weakest_first":
                return self._pick_weakest_first()
            case "ccd_alternating":
                return self._pick_ccd_alternating()
            case _:
                return self._pick_sequential()

    def _pick_sequential(self) -> int | None:
        """Finish each core completely before moving to next (pure selector)."""
        # Pass 1: active phases (not confirmed, not settled)
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase not in ("confirmed", "settled"):
                return core_id
        # Pass 2: settled cores needing confirmation
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase == "settled":
                return core_id
        return None

    def _pick_round_robin(self) -> int | None:
        """Cycle through all cores, one test each per round (pure selector)."""
        active = sorted(
            cid for cid, cs in self._core_states.items()
            if cs.phase not in ("confirmed",)
        )
        if not active:
            return None
        if self._last_tested_core is not None and self._last_tested_core in active:
            idx = active.index(self._last_tested_core)
            rotated = active[idx + 1:] + active[:idx + 1]
            return rotated[0]
        return active[0]

    def _pick_weakest_first(self) -> int | None:
        """Prioritize cores closest to settling (pure selector)."""
        candidates = []
        for core_id, cs in self._core_states.items():
            if cs.phase == "confirmed":
                continue
            score = {
                "fine_search": 0, "failed_confirm": 0,
                "confirming": 1, "coarse_search": 2,
                "settled": 3, "not_started": 4,
            }.get(cs.phase, 5)
            candidates.append((score, core_id))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def _pick_ccd_alternating(self) -> int | None:
        """Alternate between CCDs: picks from the CCD with fewest confirmed cores."""
        ccd_cores: dict[int, list[int]] = {}
        for core_id, cs in self._core_states.items():
            if cs.phase == "confirmed":
                continue
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            ccd_cores.setdefault(ccd, []).append(core_id)

        if not ccd_cores:
            return None

        for ccd in ccd_cores:
            ccd_cores[ccd].sort()

        ccd_confirmed: dict[int, int] = {}
        for core_id, cs in self._core_states.items():
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            if cs.phase == "confirmed":
                ccd_confirmed[ccd] = ccd_confirmed.get(ccd, 0) + 1

        sorted_ccds = sorted(ccd_cores.keys(), key=lambda c: ccd_confirmed.get(c, 0))
        return ccd_cores[sorted_ccds[0]][0]

    # ------------------------------------------------------------------
    # Test execution
    # ------------------------------------------------------------------

    def _run_next(self) -> None:
        """Pick next core, apply CO, run test on a worker thread."""
        if self._abort_requested or self._paused:
            return

        # Check abort-on-consecutive-failures
        if (
            self._config.abort_on_consecutive_failures > 0
            and self._consecutive_start_failures >= self._config.abort_on_consecutive_failures
        ):
            self.log_message.emit(
                f"Aborting: {self._consecutive_start_failures} consecutive cores "
                f"failed at start offset {self._config.start_offset}"
            )
            self.abort()
            return

        core_id = self._pick_next_core()
        if core_id is None:
            self._complete_session()
            return

        cs = self._core_states[core_id]
        if cs.phase == "not_started":
            self._advance_core(core_id, passed=False)  # → coarse_search
            cs = self._core_states[core_id]
        elif cs.phase == "settled":
            self._advance_core(core_id, passed=False)  # → confirming
            cs = self._core_states[core_id]
        self._last_tested_core = core_id
        self._emit_progress()
        self.log_message.emit(
            f"Testing core {core_id} at offset {cs.current_offset} "
            f"(phase: {cs.phase})"
        )

        # Apply CO offset via SMU — verify it actually took effect
        if self._smu is not None:
            try:
                success = self._smu.set_co_offset(core_id, cs.current_offset)
            except Exception as e:
                self.log_message.emit(
                    f"Failed to set CO for core {core_id}: {e}"
                )
                self._on_test_finished(core_id, False, str(e), "", 0.0)
                return

            if not success:
                msg = (
                    f"CO write failed or read-back mismatch for core {core_id} "
                    f"at offset {cs.current_offset} — SMU did not apply the value"
                )
                self.log_message.emit(msg)
                self._on_test_finished(core_id, False, msg, "co_write_failed", 0.0)
                return

        # Determine test duration based on phase
        if cs.phase == "confirming":
            duration = self._config.confirm_duration_seconds
        elif self._status == "validating":
            duration = self._config.validate_duration_seconds
        else:
            duration = self._config.search_duration_seconds

        # Run single-core test on a worker thread
        self._start_worker(core_id, duration)

    def _start_worker(self, core_id: int, duration: int) -> None:
        """Launch a _TunerWorker thread for the given core."""
        core_info = self._topology.cores.get(core_id)
        if not core_info:
            self._on_test_finished(core_id, False, f"Core {core_id} not found", "", 0.0)
            return

        stress_config = StressConfig(
            mode=self._get_stress_mode(),
            fft_preset=self._get_fft_preset(),
        )
        scheduler_config = SchedulerConfig(
            seconds_per_core=duration,
            cores_to_test=[core_id],
            stop_on_error=True,
            cycle_count=1,
        )

        try:
            scheduler = CoreScheduler(
                topology=self._topology,
                backend=self._backend,
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=self._work_dir,
            )
        except Exception as e:
            self._on_test_finished(core_id, False, str(e), "", 0.0)
            return

        logical_cpu = core_info.logical_cpus[0] if core_info.logical_cpus else core_id
        self._worker = _TunerWorker(
            core_id, logical_cpu, scheduler,
            msr=self._msr if self._config.stretch_threshold_pct > 0 else None,
            parent=self,
        )
        self._worker.finished.connect(self._on_test_finished)
        self._worker.start()

    @Slot(int, bool, str, str, float, float)
    def _on_test_finished(
        self,
        core_id: int,
        passed: bool,
        error_msg: str,
        error_type: str,
        duration: float,
        peak_stretch_pct: float = 0.0,
    ) -> None:
        """Process test result — log, advance state machine, continue."""
        # Clean up worker reference
        if self._worker is not None:
            self._worker.wait(1000)
            self._worker.deleteLater()
            self._worker = None

        if self._abort_requested:
            return

        cs = self._core_states.get(core_id)
        if cs is None:
            return

        # Clock stretch check — if stress test "passed" but core was stretching
        # badly, treat it as a failure (CO too aggressive, voltage drooping)
        threshold = self._config.stretch_threshold_pct
        if passed and threshold > 0 and peak_stretch_pct > threshold:
            passed = False
            error_msg = f"clock stretch {peak_stretch_pct:.1f}% > {threshold:.1f}% threshold"
            error_type = "clock_stretch"
            log.info(
                "Core %d offset %d: stress passed but stretch %.1f%% exceeds threshold — marking FAIL",
                core_id, cs.current_offset, peak_stretch_pct,
            )

        # Determine log phase
        phase_map = {
            "coarse_search": "coarse",
            "fine_search": "fine",
            "confirming": "confirm",
        }
        log_phase = phase_map.get(cs.phase, "validate" if self._status == "validating" else cs.phase)

        # Log to DB
        if self._session_id:
            tp.log_test_result(
                self._db,
                self._session_id,
                core_id,
                cs.current_offset,
                log_phase,
                passed,
                error_msg=error_msg or None,
                error_type=error_type or None,
                duration=duration,
            )

        status_str = "PASS" if passed else "FAIL"
        stretch_info = f" stretch:{peak_stretch_pct:.1f}%" if peak_stretch_pct > 0 else ""
        self.log_message.emit(
            f"Core {core_id} offset {cs.current_offset}: {status_str}{stretch_info}"
            + (f" ({error_msg})" if error_msg else "")
        )
        self.test_completed.emit(core_id, cs.current_offset, passed)

        # Reset consecutive failure counter on any pass
        if passed:
            self._consecutive_start_failures = 0

        # Advance state machine
        self._advance_core(core_id, passed)

        # Continue with next test
        self._run_next()

    def _complete_session(self) -> None:
        """All cores done — finalize session."""
        profile = {}
        for cs in self._core_states.values():
            if cs.best_offset is not None:
                profile[cs.core_id] = cs.best_offset

        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "completed")

        self._set_status("idle")
        self._emit_progress()
        self.log_message.emit(
            f"Tuner complete — {len(profile)} cores confirmed"
        )
        import json
        self.session_completed.emit(json.dumps(profile))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_cores_to_test(self) -> list[int]:
        if self._config.cores_to_test is not None:
            return sorted(self._config.cores_to_test)
        return sorted(self._topology.cores.keys())

    def _set_status(self, status: str) -> None:
        self._status = status
        self.status_changed.emit(status)

    def _emit_progress(self) -> None:
        done = sum(1 for cs in self._core_states.values() if cs.phase == "confirmed")
        total = len(self._core_states)
        self.progress_updated.emit(done, total)

    def _get_stress_mode(self):
        from engine.backends.base import StressMode
        try:
            return StressMode[self._config.stress_mode.upper()]
        except KeyError:
            return StressMode.SSE

    def _get_fft_preset(self):
        from engine.backends.base import FFTPreset
        try:
            return FFTPreset[self._config.fft_preset.upper()]
        except KeyError:
            return FFTPreset.SMALL
