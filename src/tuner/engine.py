"""Automated PBO Curve Optimizer tuner — core state machine and orchestrator.

Drives the coarse-to-fine search: big steps first, fine steps after failure,
confirmation at the settled value. Every state transition persists to SQLite
before acting, so the tuner resumes exactly where it left off after crash/reboot.

Test execution runs on a QThread so the GUI remains responsive.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from engine.backends.base import StressConfig
from engine.scheduler import CoreScheduler, SchedulerConfig
from monitor.msr import MSRReader

from . import persistence as tp
from .config import TunerConfig
from .state import CoreState, TunerPhase

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

    @property
    def scheduler(self) -> CoreScheduler:
        return self._scheduler

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
    co_drift_detected = Signal(str)  # JSON-encoded {core_id: {expected, actual}}
    validation_progress = Signal(int, int, int)  # stage, current_index, total
    worker_started = Signal(int)  # core_id — emitted when mprime actually starts

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
        self._work_dir = work_dir or Path("/tmp/corecycler/tuner")

        self._msr = MSRReader()

        self._session_id: int | None = None
        self._core_states: dict[int, CoreState] = {}
        self._status: str = "idle"
        self._paused = False
        self._abort_requested = False
        self._consecutive_start_failures = 0
        self._worker: _TunerWorker | None = None
        self._last_tested_core: int | None = None
        self._ccd_last_tested: dict[int, int | None] = {}  # CCD index → last core_id tested in that CCD
        self._co_applied: dict[int, int | None] = {}  # core_id → last CO value written to SMU (None = unknown)

        # Multi-core validation state
        self._validation_stage: int = 0  # 0 = not validating, 1/2/3 = stage
        self._validation_core_index: int = 0  # index into _validation_core_order for stage 1
        self._validation_core_order: list[int] = []  # cores to cycle through in stage 1
        self._validation_half_index: int = 0  # which half to test in stage 3
        self._validation_halves: list[list[int]] = []  # [half_a, half_b] for stage 3

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
        self._validation_stage = 0

        # Validate config
        errors = self._config.validate()
        if errors:
            self.log_message.emit(f"Invalid tuner config: {'; '.join(errors)}")
            return

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
            cs = CoreState(core_id=core_id, current_offset=start, baseline_offset=start)
            self._core_states[core_id] = cs
            tp.save_core_state(self._db, self._session_id, cs)
            self._co_applied[core_id] = None  # unknown — SMU state not yet managed

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
        self._validation_stage = 0
        self._session_id = session_id

        session = tp.get_session(self._db, session_id)
        if session is None:
            self.log_message.emit(f"Session {session_id} not found")
            return

        self._config = TunerConfig.from_json(session.config_json)
        if self._smu is not None:
            self._config.clamp_max_offset(self._smu.commands.co_range)

        self._core_states = tp.load_core_states(self._db, session_id)

        # Check for CO drift — warn if SMU values don't match expected baselines.
        # This catches cases where the user manually changed CO (via Curve Optimizer
        # tab) or ran other tools between pause and resume.
        if self._smu is not None:
            import json as _json
            drift: dict[int, dict[str, int]] = {}
            for cs in self._core_states.values():
                actual = self._smu.get_co_offset(cs.core_id)
                if actual is not None and actual != cs.baseline_offset:
                    drift[cs.core_id] = {"expected": cs.baseline_offset, "actual": actual}
            if drift:
                self.log_message.emit(
                    f"CO drift detected on {len(drift)} core(s) — "
                    f"SMU values differ from session baselines. "
                    f"Baselines will be restored."
                )
                self.co_drift_detected.emit(_json.dumps(drift))

        # Step 1: Advance ONLY the core that was actively being tested.
        # The in_test flag is set when a test starts and cleared when it
        # finishes. If the system crashed, the flag is still True for the
        # core that was running — that core's offset is dangerous.
        # Other cores in active phases (queued, not yet tested at their
        # current_offset) must NOT be advanced — they haven't failed.
        for cs in list(self._core_states.values()):
            if cs.in_test:
                self.log_message.emit(
                    f"Core {cs.core_id} was actively testing at offset {cs.current_offset} "
                    f"— treating crash as failure and backing off"
                )
                cs.in_test = False
                self._advance_core(cs.core_id, passed=False)

        # Step 2: Restore all cores to their baseline offsets.
        # After a crash and reboot, SMU SRAM is zeroed. Apply the known-stable
        # baselines (captured from BIOS/inherit_current at session start) so the
        # CPU runs at its proven-stable config. _run_next() will apply the test
        # offset only to the core being tested.
        if self._smu is not None:
            failed_cores: list[int] = []
            baselines: dict[int, int] = {}
            for cs in self._core_states.values():
                baselines[cs.core_id] = cs.baseline_offset
                if cs.baseline_offset == 0:
                    self._co_applied[cs.core_id] = 0  # SMU already at 0 after reboot
                    continue
                try:
                    success = self._smu.set_co_offset(cs.core_id, cs.baseline_offset)
                    if success:
                        self._co_applied[cs.core_id] = cs.baseline_offset
                    else:
                        failed_cores.append(cs.core_id)
                        self.log_message.emit(
                            f"Baseline restore failed for core {cs.core_id} at offset "
                            f"{cs.baseline_offset} — read-back mismatch or SMU rejection"
                        )
                except Exception as e:
                    failed_cores.append(cs.core_id)
                    log.warning("Failed to restore baseline for core %d: %s", cs.core_id, e)
                    self.log_message.emit(
                        f"Baseline restore error for core {cs.core_id}: {e}"
                    )
            if failed_cores:
                self.log_message.emit(
                    f"WARNING: Baselines could not be restored for cores {failed_cores}. "
                    f"SMU access may have changed since last session."
                )
            else:
                self.log_message.emit(f"Restored baselines: {baselines}")

        # Check if all cores are confirmed — if so, we were paused during
        # validation and should re-enter validation instead of per-core search.
        all_confirmed = all(cs.phase == TunerPhase.CONFIRMED for cs in self._core_states.values())
        if all_confirmed and self._config.auto_validate and len(self._core_states) > 1:
            profile = {
                cs.core_id: cs.best_offset
                for cs in self._core_states.values()
                if cs.best_offset is not None
            }
            self.log_message.emit(
                f"Resumed session {session_id} — "
                f"all cores confirmed, re-entering validation"
            )
            self._enter_auto_validation(profile)
        else:
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
        """Stop immediately, revert CO to baseline, save state."""
        self._abort_requested = True
        tested_core: int | None = None
        if self._worker is not None:
            tested_core = self._last_tested_core
            # Disconnect signal FIRST to prevent _on_test_finished firing during cleanup
            with contextlib.suppress(RuntimeError):
                self._worker.finished.disconnect(self._on_test_finished)
            if self._worker.isRunning():
                # Stop the scheduler first — kills stress process and lets worker exit cleanly
                with contextlib.suppress(Exception):
                    self._worker.scheduler.force_stop()
                if not self._worker.wait(5000):
                    # Worker didn't exit after scheduler stop — force terminate
                    self._worker.terminate()
                    self._worker.wait(3000)
            self._worker.deleteLater()
            self._worker = None
        # Clear in_test flag and revert to baseline so no aggressive offset
        # lingers in SMU after abort
        if tested_core is not None:
            cs = self._core_states.get(tested_core)
            if cs is not None:
                cs.in_test = False
            self._revert_core_to_baseline(tested_core)
        self._validation_stage = 0
        self._set_status("idle")
        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "aborted")
        self.log_message.emit("Tuner aborted")

    def validate_profile(self, session_id: int) -> None:
        """Re-test all confirmed values from a completed session."""
        self._abort_requested = False
        self._paused = False
        self._validation_stage = 0
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
        # Reset CO tracking — SMU state is unknown, force fresh writes
        self._co_applied = {core_id: None for core_id in self._core_states}
        for core_id, offset in profile.items():
            if core_id in self._core_states:
                cs = self._core_states[core_id]
                cs.phase = TunerPhase.CONFIRMING
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
            case TunerPhase.NOT_STARTED:
                # First step: enter coarse search
                cs.phase = TunerPhase.COARSE_SEARCH
                # Use inherited offset as base when inherit_current is active
                base = cs.current_offset if (cfg.inherit_current and cs.current_offset != 0) else cfg.start_offset
                cs.current_offset = base + direction * cfg.coarse_step
                if self._exceeds_max(cs.current_offset):
                    cs.current_offset = cfg.max_offset

            case TunerPhase.COARSE_SEARCH:
                if passed:
                    cs.best_offset = cs.current_offset
                    next_offset = cs.current_offset + direction * cfg.coarse_step
                    if self._exceeds_max(next_offset):
                        # Hit the limit — settle here
                        cs.phase = TunerPhase.SETTLED
                    else:
                        cs.current_offset = next_offset
                else:
                    # Coarse search failed
                    cs.coarse_fail_offset = cs.current_offset
                    if cs.best_offset is None:
                        # Never passed — check abort threshold
                        if cs.current_offset == cfg.start_offset + direction * cfg.coarse_step:
                            self._consecutive_start_failures += 1
                        cs.phase = TunerPhase.SETTLED  # nothing we can do
                    else:
                        # Fine search between best_offset and coarse_fail
                        cs.phase = TunerPhase.FINE_SEARCH
                        cs.current_offset = cs.best_offset + direction * cfg.fine_step

            case TunerPhase.FINE_SEARCH:
                if passed:
                    cs.best_offset = cs.current_offset
                    next_offset = cs.current_offset + direction * cfg.fine_step
                    # Stop if we'd reach or pass the coarse fail point
                    if cs.coarse_fail_offset is not None and (
                        (direction < 0 and next_offset <= cs.coarse_fail_offset)
                        or (direction > 0 and next_offset >= cs.coarse_fail_offset)
                    ):
                        cs.phase = TunerPhase.SETTLED
                    elif self._exceeds_max(next_offset):
                        cs.phase = TunerPhase.SETTLED
                    else:
                        cs.current_offset = next_offset
                else:
                    # Fine search failed — settle at last good value
                    cs.phase = TunerPhase.SETTLED

            case TunerPhase.SETTLED:
                # Move to confirmation
                if cs.best_offset is not None:
                    cs.phase = TunerPhase.CONFIRMING
                    cs.current_offset = cs.best_offset
                else:
                    # No passing value found at all — mark confirmed at start
                    cs.phase = TunerPhase.CONFIRMED
                    cs.best_offset = cfg.start_offset
                    cs.current_offset = cfg.start_offset

            case TunerPhase.CONFIRMING:
                if passed:
                    cs.phase = TunerPhase.CONFIRMED
                    cs.confirm_attempts = 0
                else:
                    cs.confirm_attempts += 1
                    if cs.confirm_attempts >= cfg.max_confirm_retries:
                        # Back off and re-enter fine search
                        cs.phase = TunerPhase.FAILED_CONFIRM
                    # else: retry confirmation (stays in confirming)

            case TunerPhase.FAILED_CONFIRM:
                # Back off by one fine step and enter backoff preconfirm
                if cs.best_offset is not None:
                    new_best = cs.best_offset - direction * cfg.fine_step
                    if self._at_or_past_baseline(new_best, cs):
                        # Can't back off further
                        cs.phase = TunerPhase.CONFIRMED
                        cs.best_offset = cs.baseline_offset
                        cs.current_offset = cs.baseline_offset
                    else:
                        cs.best_offset = new_best
                        cs.current_offset = new_best
                        cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                        cs.backoff_mode = True
                        cs.confirm_attempts = 0
                        cs.consecutive_backoff_fails = 0
                else:
                    cs.phase = TunerPhase.CONFIRMED
                    cs.best_offset = cs.baseline_offset
                    cs.current_offset = cs.baseline_offset

            case TunerPhase.BACKOFF_PRECONFIRM:
                if passed:
                    had_pass_bound = cs.backoff_pass_bound is not None
                    cs.backoff_pass_bound = cs.best_offset
                    if had_pass_bound and cs.backoff_fail_bound is not None:
                        # Binary search active — jump to midpoint
                        gap = abs(cs.backoff_fail_bound - cs.backoff_pass_bound)
                        if gap <= cfg.fine_step:
                            cs.phase = TunerPhase.CONFIRMED
                        else:
                            mid = cs.backoff_pass_bound + direction * (gap // 2)
                            cs.best_offset = mid
                            cs.current_offset = mid
                            # Stay in backoff_preconfirm for next test
                    else:
                        # First pass in backoff — enter confirmation
                        cs.phase = TunerPhase.BACKOFF_CONFIRMING
                        cs.current_offset = cs.best_offset
                        cs.confirm_attempts = 0
                else:
                    cs.consecutive_backoff_fails += 1
                    # Check midpoint jump threshold
                    if cs.consecutive_backoff_fails >= cfg.midpoint_jump_threshold:
                        # Jump to midpoint between current and baseline
                        cs.backoff_fail_bound = cs.best_offset
                        midpoint = cs.best_offset - direction * (
                            abs(cs.best_offset - cs.baseline_offset) // 2
                        )
                        if self._at_or_past_baseline(midpoint, cs) or midpoint == cs.best_offset:
                            cs.phase = TunerPhase.CONFIRMED
                            cs.best_offset = cs.baseline_offset
                            cs.current_offset = cs.baseline_offset
                        else:
                            cs.best_offset = midpoint
                            cs.current_offset = midpoint
                            cs.consecutive_backoff_fails = 0
                    else:
                        # Back off one more step
                        new_offset = cs.best_offset - direction * cfg.fine_step
                        if self._at_or_past_baseline(new_offset, cs):
                            cs.phase = TunerPhase.CONFIRMED
                            cs.best_offset = cs.baseline_offset
                            cs.current_offset = cs.baseline_offset
                        else:
                            cs.best_offset = new_offset
                            cs.current_offset = new_offset

            case TunerPhase.BACKOFF_CONFIRMING:
                if passed:
                    # Confirmed at this offset — check binary search
                    if cs.backoff_fail_bound is not None and cs.backoff_pass_bound is not None:
                        # Binary search: try midpoint between pass and fail bounds
                        gap = abs(cs.backoff_fail_bound - cs.backoff_pass_bound)
                        if gap <= cfg.fine_step:
                            # Converged
                            cs.phase = TunerPhase.CONFIRMED
                        else:
                            mid = cs.backoff_pass_bound + direction * (gap // 2)
                            cs.best_offset = mid
                            cs.current_offset = mid
                            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    else:
                        cs.phase = TunerPhase.CONFIRMED
                else:
                    # Confirm failed — back to preconfirm, back off
                    cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    new_offset = cs.best_offset - direction * cfg.fine_step
                    if self._at_or_past_baseline(new_offset, cs):
                        cs.phase = TunerPhase.CONFIRMED
                        cs.best_offset = cs.baseline_offset
                        cs.current_offset = cs.baseline_offset
                    else:
                        cs.best_offset = new_offset
                        cs.current_offset = new_offset

        # Persist
        if self._session_id:
            tp.save_core_state(self._db, self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)

    def _exceeds_max(self, offset: int) -> bool:
        """Check if offset exceeds max_offset in the configured direction."""
        if self._config.direction < 0:
            return offset < self._config.max_offset
        return offset > self._config.max_offset

    def _at_or_past_baseline(self, offset: int, cs: CoreState) -> bool:
        """Check if offset is at or past the core's baseline in the configured direction."""
        if self._config.direction < 0:
            return offset >= cs.baseline_offset
        return offset <= cs.baseline_offset

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
            case "ccd_round_robin":
                return self._pick_ccd_round_robin()
            case _:
                return self._pick_sequential()

    def _pick_sequential(self) -> int | None:
        """Finish each core completely before moving to next (pure selector)."""
        # Pass 1: active phases (not confirmed, not settled)
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase not in (TunerPhase.CONFIRMED, TunerPhase.SETTLED):
                return core_id
        # Pass 2: settled cores needing confirmation
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase == TunerPhase.SETTLED:
                return core_id
        return None

    def _pick_round_robin(self) -> int | None:
        """Cycle through all cores, one test each per round (pure selector)."""
        active = sorted(
            cid for cid, cs in self._core_states.items()
            if cs.phase != TunerPhase.CONFIRMED
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
            if cs.phase == TunerPhase.CONFIRMED:
                continue
            score = {
                TunerPhase.FINE_SEARCH: 0, TunerPhase.FAILED_CONFIRM: 0,
                TunerPhase.BACKOFF_PRECONFIRM: 0, TunerPhase.BACKOFF_CONFIRMING: 1,
                TunerPhase.CONFIRMING: 1, TunerPhase.COARSE_SEARCH: 2,
                TunerPhase.SETTLED: 3, TunerPhase.NOT_STARTED: 4,
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
            if cs.phase == TunerPhase.CONFIRMED:
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
            if cs.phase == TunerPhase.CONFIRMED:
                ccd_confirmed[ccd] = ccd_confirmed.get(ccd, 0) + 1

        sorted_ccds = sorted(ccd_cores.keys(), key=lambda c: ccd_confirmed.get(c, 0))
        return ccd_cores[sorted_ccds[0]][0]

    def _pick_ccd_round_robin(self) -> int | None:
        """Round-robin with CCD interleaving — one test per core, alternating CCDs.

        Order: CCD0[0]→CCD1[0]→CCD0[1]→CCD1[1]→CCD0[2]→CCD1[2]...
        Each core gets cool-down time between tests.
        """
        ccd_cores: dict[int, list[int]] = {}
        for core_id, cs in self._core_states.items():
            if cs.phase == TunerPhase.CONFIRMED:
                continue
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            ccd_cores.setdefault(ccd, []).append(core_id)

        if not ccd_cores:
            return None

        for ccd in ccd_cores:
            ccd_cores[ccd].sort()

        sorted_ccds = sorted(ccd_cores.keys())

        if len(sorted_ccds) < 2:
            return self._pick_round_robin()

        # Pick CCD: alternate from last tested core's CCD
        if self._last_tested_core is not None:
            last_info = self._topology.cores.get(self._last_tested_core)
            last_ccd = last_info.ccd if last_info and last_info.ccd is not None else 0
            other_ccds = [c for c in sorted_ccds if c != last_ccd and c in ccd_cores]
            target_ccd = other_ccds[0] if other_ccds else sorted_ccds[0]
        else:
            target_ccd = sorted_ccds[0]

        cores = ccd_cores[target_ccd]

        # Within this CCD, rotate from last tested position
        last_in_ccd = self._ccd_last_tested.get(target_ccd)
        if last_in_ccd is not None and last_in_ccd in cores:
            idx = cores.index(last_in_ccd)
            rotated = cores[idx + 1:] + cores[:idx + 1]
            return rotated[0]
        return cores[0]

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
        if cs.phase == TunerPhase.NOT_STARTED:
            self._advance_core(core_id, passed=False)  # → coarse_search
            cs = self._core_states[core_id]
        elif cs.phase == TunerPhase.SETTLED:
            self._advance_core(core_id, passed=False)  # → confirming
            cs = self._core_states[core_id]
        self._last_tested_core = core_id
        cs.in_test = True
        tp.save_core_state(self._db, self._session_id, cs)
        # Track per-CCD position for ccd_round_robin
        core_info = self._topology.cores.get(core_id)
        if core_info and core_info.ccd is not None:
            self._ccd_last_tested[core_info.ccd] = core_id
        self._emit_progress()
        self.log_message.emit(
            f"Testing core {core_id} at offset {cs.current_offset} "
            f"(phase: {cs.phase})"
        )

        # CO offset application — two modes:
        # 1. During validation: apply ALL confirmed offsets (testing interactions)
        # 2. During search: isolate tested core (only it has non-baseline offset)
        if self._smu is not None:
            if self._status == "validating":
                # Validation mode: apply all confirmed offsets to test interactions
                if not self._apply_validation_offsets(core_id, cs.current_offset):
                    return
            else:
                # Search mode: isolate to prevent false blame on crash
                if not self._apply_co_isolation(core_id, cs.current_offset):
                    return

        # Determine test duration based on phase
        if cs.phase in (TunerPhase.CONFIRMING, TunerPhase.BACKOFF_CONFIRMING):
            duration = self._config.confirm_duration_seconds
        elif cs.phase == TunerPhase.BACKOFF_PRECONFIRM:
            duration = int(self._config.search_duration_seconds * self._config.backoff_preconfirm_multiplier)
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
            self._on_test_finished(core_id, False, f"Core {core_id} not found", "", 0.0, 0.0)
            return

        stress_config = StressConfig(
            mode=self._get_stress_mode(),
            fft_preset=self._get_fft_preset(),
            threads=len(core_info.logical_cpus),
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
            self._on_test_finished(core_id, False, str(e), "", 0.0, 0.0)
            return

        logical_cpu = core_info.logical_cpus[0] if core_info.logical_cpus else core_id
        self._worker = _TunerWorker(
            core_id, logical_cpu, scheduler,
            msr=self._msr if self._config.stretch_threshold_pct > 0 else None,
            parent=self,
        )
        self._worker.finished.connect(self._on_test_finished)
        self._worker.start()
        self.worker_started.emit(core_id)

    @Slot(int, bool, str, str, float, float)
    def _on_test_finished(
        self,
        core_id: int,
        passed: bool,
        error_msg: str,
        error_type: str,
        duration: float,
        peak_stretch_pct: float,
    ) -> None:
        """Process test result — log, advance state machine, continue."""
        # Check abort FIRST — if abort() already ran, don't touch any state.
        # The signal may fire after abort() disconnected it (Qt queued delivery).
        if self._abort_requested:
            # Still clean up the worker if it exists
            if self._worker is not None:
                self._worker.wait(1000)
                self._worker.deleteLater()
                self._worker = None
            return

        # Clean up worker reference
        if self._worker is not None:
            self._worker.wait(1000)
            self._worker.deleteLater()
            self._worker = None

        cs = self._core_states.get(core_id)
        if cs is None:
            return

        cs.in_test = False

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
            TunerPhase.COARSE_SEARCH: "coarse",
            TunerPhase.FINE_SEARCH: "fine",
            TunerPhase.CONFIRMING: "confirm",
            TunerPhase.BACKOFF_PRECONFIRM: "backoff_preconfirm",
            TunerPhase.BACKOFF_CONFIRMING: "backoff_confirm",
        }
        if self._status == "validating" and self._validation_stage > 0:
            log_phase = f"validate_s{self._validation_stage}"
        else:
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

        # Revert tested core to baseline — no aggressive offset should linger.
        # Skip during validation: we want all confirmed offsets to stay applied.
        if self._status != "validating":
            self._revert_core_to_baseline(core_id)

        # Reset consecutive failure counter on any pass
        if passed:
            self._consecutive_start_failures = 0

        # Multi-core validation uses its own flow — don't advance per-core state machine
        if self._validation_stage > 0:
            self._on_validation_test_finished(core_id, passed)
            return

        # Advance state machine
        self._advance_core(core_id, passed)

        # Continue with next test
        self._run_next()

    def _complete_session(self) -> None:
        """All cores done — enter auto-validation or finalize session."""
        profile = {}
        for cs in self._core_states.values():
            if cs.best_offset is not None:
                profile[cs.core_id] = cs.best_offset

        # If auto_validate is on and we just finished per-core search (not
        # already validating), enter multi-core validation instead of completing.
        if (
            self._config.auto_validate
            and self._status != "validating"
            and len(profile) > 1  # single-core has nothing to cross-validate
        ):
            self.log_message.emit(
                f"All {len(profile)} cores confirmed — entering multi-core validation"
            )
            self._enter_auto_validation(profile)
            return

        self._finalize_session(profile)

    def _finalize_session(self, profile: dict[int, int]) -> None:
        """Apply confirmed profile to SMU and emit completion."""
        if self._smu is not None and profile:
            failed: list[int] = []
            for core_id, offset in profile.items():
                try:
                    success = self._smu.set_co_offset(core_id, offset)
                    if success:
                        self._co_applied[core_id] = offset
                    else:
                        failed.append(core_id)
                except Exception as e:
                    log.warning("Failed to apply confirmed offset for core %d: %s", core_id, e)
                    failed.append(core_id)
            if failed:
                self.log_message.emit(
                    f"WARNING: Could not apply confirmed offsets for cores {failed}"
                )
            else:
                self.log_message.emit("Applied confirmed CO profile to SMU")

        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "completed")

        self._validation_stage = 0
        self._set_status("idle")
        self._emit_progress()
        self.log_message.emit(
            f"Tuner complete — {len(profile)} cores confirmed"
        )
        import json
        self.session_completed.emit(json.dumps(profile))

    # ------------------------------------------------------------------
    # Multi-core validation (3-stage)
    # ------------------------------------------------------------------

    def _enter_auto_validation(self, profile: dict[int, int]) -> None:
        """Begin the 3-stage multi-core validation sequence.

        Stage 1: Per-core with all offsets live — stress each core individually
                 while all other cores hold their confirmed offsets.
        Stage 2: All-core simultaneous — all cores stressed at once.
        Stage 3: Alternating half-core load — half loaded / half idle, rotating.
        """
        # Apply all confirmed offsets (validation mode — no isolation)
        self._set_status("validating")
        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "validating")

        # Set up core order for stage 1 (follows test_order from config)
        self._validation_core_order = sorted(profile.keys())
        self._validation_core_index = 0

        # Set up halves for stage 3 (split by CCD if available, else even/odd)
        # Filter out empty halves to prevent IndexError with odd core counts
        self._validation_halves = [
            h for h in self._split_cores_into_halves(profile) if h
        ]
        self._validation_half_index = 0

        self._validation_stage = 1
        self.log_message.emit("Validation stage 1: per-core with all offsets live")
        self.validation_progress.emit(1, 0, len(self._validation_core_order))
        self._run_validation_next()

    def _split_cores_into_halves(self, profile: dict[int, int]) -> list[list[int]]:
        """Split confirmed cores into two halves for stage 3.

        Uses CCD boundaries when available (tests cross-CCD power interactions).
        Falls back to even index / odd index split.
        """
        cores = sorted(profile.keys())
        ccd_groups: dict[int, list[int]] = {}
        for core_id in cores:
            core_info = self._topology.cores.get(core_id)
            ccd = core_info.ccd if core_info and core_info.ccd is not None else 0
            ccd_groups.setdefault(ccd, []).append(core_id)

        if len(ccd_groups) >= 2:
            # Split by CCD — half_a = first CCD(s), half_b = remaining
            sorted_ccds = sorted(ccd_groups.keys())
            mid = len(sorted_ccds) // 2
            half_a = []
            half_b = []
            for i, ccd in enumerate(sorted_ccds):
                if i < mid:
                    half_a.extend(ccd_groups[ccd])
                else:
                    half_b.extend(ccd_groups[ccd])
            return [sorted(half_a), sorted(half_b)]

        # Single CCD — split by index
        return [cores[::2], cores[1::2]]

    def _run_validation_next(self) -> None:
        """Dispatch the next validation test based on current stage."""
        if self._abort_requested or self._paused:
            return

        match self._validation_stage:
            case 1:
                self._run_validation_stage1()
            case 2:
                self._run_validation_stage2()
            case 3:
                self._run_validation_stage3()
            case _:
                # All stages complete
                profile = {
                    cs.core_id: cs.best_offset
                    for cs in self._core_states.values()
                    if cs.best_offset is not None
                }
                self.log_message.emit("All validation stages passed")
                self._finalize_session(profile)

    def _run_validation_stage1(self) -> None:
        """Stage 1: test each core individually with all offsets applied."""
        if self._validation_core_index >= len(self._validation_core_order):
            # Stage 1 complete — advance to stage 2
            self._validation_stage = 2
            self.log_message.emit(
                "Validation stage 1 passed — "
                "stage 2: all-core simultaneous stress"
            )
            QTimer.singleShot(0, self._run_validation_next)
            return

        core_id = self._validation_core_order[self._validation_core_index]
        cs = self._core_states[core_id]
        offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset

        self.log_message.emit(
            f"Validation 1/{len(self._validation_core_order)}: "
            f"core {core_id} at offset {offset} (all offsets live)"
        )
        self.validation_progress.emit(
            1, self._validation_core_index, len(self._validation_core_order)
        )

        # Apply all confirmed offsets
        if self._smu is not None:
            if not self._apply_validation_offsets(core_id, offset):
                return

        self._last_tested_core = core_id
        self._start_worker(core_id, self._config.validate_duration_seconds)

    def _run_validation_stage2(self) -> None:
        """Stage 2: all cores stressed simultaneously.

        Uses CoreScheduler with all confirmed core IDs — full power draw.
        Picks the first core as the "reported" core for the worker signal,
        but all cores are stressed.
        """
        cores = self._validation_core_order
        self.log_message.emit(
            f"Validation stage 2: stressing all {len(cores)} cores simultaneously"
        )
        self.validation_progress.emit(2, 0, 1)

        # Apply all confirmed offsets
        if self._smu is not None:
            first_core = cores[0]
            cs = self._core_states[first_core]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first_core, offset):
                return

        self._last_tested_core = cores[0]
        self._start_multi_core_worker(cores, self._config.validate_duration_seconds)

    def _run_validation_stage3(self) -> None:
        """Stage 3: alternating half-core load — catches voltage transients."""
        if self._validation_half_index >= len(self._validation_halves):
            # Stage 3 complete — all validation passed
            self._validation_stage = 4  # sentinel → _run_validation_next finalizes
            QTimer.singleShot(0, self._run_validation_next)
            return

        half = self._validation_halves[self._validation_half_index]
        half_label = "A" if self._validation_half_index == 0 else "B"
        self.log_message.emit(
            f"Validation stage 3{half_label}: stressing cores {half} "
            f"(half loaded, half idle — catching boost ramp transients)"
        )
        self.validation_progress.emit(
            3, self._validation_half_index, len(self._validation_halves)
        )

        # Apply all confirmed offsets (even idle cores hold their offsets)
        if self._smu is not None:
            first_core = half[0]
            cs = self._core_states[first_core]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first_core, offset):
                return

        self._last_tested_core = half[0]
        self._start_multi_core_worker(half, self._config.validate_duration_seconds)

    def _start_multi_core_worker(self, cores: list[int], duration: int) -> None:
        """Launch a worker that stresses multiple cores simultaneously.

        Uses CoreScheduler with multiple cores_to_test. The finished signal
        reports the first core ID — the engine treats pass/fail as applying
        to the whole set.
        """
        stress_config = StressConfig(
            mode=self._get_stress_mode(),
            fft_preset=self._get_fft_preset(),
            threads=0,  # let scheduler figure out threads per core
        )

        # For multi-core: each core gets its own threads via scheduler
        scheduler_config = SchedulerConfig(
            seconds_per_core=duration,
            cores_to_test=cores,
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
            self._on_test_finished(cores[0], False, str(e), "", 0.0, 0.0)
            return

        core_info = self._topology.cores.get(cores[0])
        logical_cpu = core_info.logical_cpus[0] if core_info and core_info.logical_cpus else cores[0]
        self._worker = _TunerWorker(
            cores[0], logical_cpu, scheduler,
            msr=None,  # stretch detection not meaningful for multi-core
            parent=self,
        )
        self._worker.finished.connect(self._on_test_finished)
        self._worker.start()

    def _find_most_aggressive_core(self) -> int | None:
        """Find the confirmed core with the highest absolute offset that can be backed off.

        Skips cores already at their baseline_offset (nothing to give).
        """
        best_core = None
        best_abs = -1
        for cs in self._core_states.values():
            if cs.best_offset is not None and cs.best_offset != cs.baseline_offset:
                if abs(cs.best_offset) > best_abs:
                    best_abs = abs(cs.best_offset)
                    best_core = cs.core_id
        return best_core

    def _backoff_core(self, core_id: int) -> bool:
        """Back off a core's best_offset by one fine_step.

        Returns False if the offset is already at baseline (can't back off further).
        """
        cs = self._core_states[core_id]
        cfg = self._config
        if cs.best_offset is None:
            return False
        if cs.best_offset == cs.baseline_offset:
            return False  # already at baseline — nothing to back off

        old_offset = cs.best_offset
        new_offset = cs.best_offset - cfg.direction * cfg.fine_step
        # Clamp to baseline if we've backed off past it
        if self._at_or_past_baseline(new_offset, cs):
            cs.best_offset = cs.baseline_offset
            cs.current_offset = cs.baseline_offset
        else:
            cs.best_offset = new_offset
            cs.current_offset = new_offset

        if self._session_id:
            tp.save_core_state(self._db, self._session_id, cs)

        self.log_message.emit(
            f"Backed off core {core_id}: offset {cs.best_offset} (was {old_offset})"
        )
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
        return True

    def _on_validation_test_finished(self, core_id: int, passed: bool) -> None:
        """Handle test result during multi-core validation stages."""
        if passed:
            match self._validation_stage:
                case 1:
                    self._validation_core_index += 1
                case 2:
                    # Stage 2 passed — advance to stage 3
                    self._validation_stage = 3
                    self._validation_half_index = 0
                    self.log_message.emit(
                        "Validation stage 2 passed — "
                        "stage 3: alternating half-core load"
                    )
                case 3:
                    self._validation_half_index += 1
            # Use QTimer to break the call stack (this is called from _on_test_finished)
            QTimer.singleShot(0, self._run_validation_next)
            return

        # Validation failure — back off and restart
        target: int | None = None
        match self._validation_stage:
            case 1:
                # Stage 1: the tested core failed — back it off
                target = core_id
            case 2 | 3:
                # Stage 2/3: multi-core failure — back off most aggressive core
                target = self._find_most_aggressive_core()

        if target is None or not self._backoff_core(target):
            # Nothing to back off — finalize with what we have
            self.log_message.emit(
                "Validation failed but no core can be backed off further — finalizing"
            )
            profile = {
                cs.core_id: cs.best_offset
                for cs in self._core_states.values()
                if cs.best_offset is not None
            }
            self._finalize_session(profile)
            return

        self.log_message.emit(
            f"Validation stage {self._validation_stage} failed — "
            f"backed off core {target}, restarting stage 1"
        )

        # Restart from stage 1
        self._validation_stage = 1
        self._validation_core_index = 0
        self._validation_half_index = 0
        self.log_message.emit("Validation stage 1: per-core with all offsets live (retry)")
        QTimer.singleShot(0, self._run_validation_next)

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
        done = sum(1 for cs in self._core_states.values() if cs.phase == TunerPhase.CONFIRMED)
        total = len(self._core_states)
        self.progress_updated.emit(done, total)

    def _apply_validation_offsets(self, test_core_id: int, test_offset: int) -> bool:
        """Apply ALL confirmed offsets during validation — testing interactions.

        Unlike isolation mode, non-tested cores keep their confirmed (best)
        offsets instead of reverting to baseline. This catches power delivery
        issues that only appear when multiple cores run aggressive offsets.

        On failure, reverts all cores to baseline to leave SMU in a known
        state, then pauses the tuner.
        """
        for core_id, cs in self._core_states.items():
            if core_id == test_core_id:
                continue
            # Use best_offset (confirmed value) if available, else baseline
            target = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if self._co_applied.get(core_id) == target:
                continue
            try:
                success = self._smu.set_co_offset(core_id, target)
            except Exception as e:
                self.log_message.emit(
                    f"Failed to apply validated offset for core {core_id}: {e}. "
                    f"Reverting to baselines and pausing."
                )
                self._revert_all_to_baseline()
                self.pause()
                return False
            if not success:
                self.log_message.emit(
                    f"Validation offset write failed for core {core_id} at {target}. "
                    f"Reverting to baselines and pausing."
                )
                self._revert_all_to_baseline()
                self.pause()
                return False
            self._co_applied[core_id] = target

        # Apply test offset to target core
        try:
            success = self._smu.set_co_offset(test_core_id, test_offset)
        except Exception as e:
            self.log_message.emit(
                f"Failed to set CO for core {test_core_id}: {e}. "
                f"Reverting to baselines and pausing."
            )
            self._revert_all_to_baseline()
            self.pause()
            return False
        if not success:
            self.log_message.emit(
                f"CO write failed for core {test_core_id} at {test_offset}. "
                f"Reverting to baselines and pausing."
            )
            self._revert_all_to_baseline()
            self.pause()
            return False
        self._co_applied[test_core_id] = test_offset
        return True

    def _apply_co_isolation(self, test_core_id: int, test_offset: int) -> bool:
        """Isolate CO for testing: baseline all other cores, apply test offset.

        Returns True if all SMU writes succeeded, False if any failed.
        On failure, PAUSES the tuner instead of advancing the state machine —
        the test was never run, so recording a "failure" at this offset would
        corrupt the binary search.
        """
        # Revert non-tested cores to baseline (skip if already there)
        for core_id, cs in self._core_states.items():
            if core_id == test_core_id:
                continue
            if self._co_applied.get(core_id) == cs.baseline_offset:
                continue  # already at baseline, skip redundant SMU write
            try:
                success = self._smu.set_co_offset(core_id, cs.baseline_offset)
            except Exception as e:
                self.log_message.emit(
                    f"CO isolation failed: core {core_id} baseline revert error — {e}. "
                    f"Pausing tuner (SMU issue, not a core stability failure)."
                )
                self.pause()
                return False
            if not success:
                self.log_message.emit(
                    f"CO isolation failed: core {core_id} baseline revert to "
                    f"{cs.baseline_offset} — read-back mismatch. "
                    f"Pausing tuner (SMU issue, not a core stability failure)."
                )
                self.pause()
                return False
            self._co_applied[core_id] = cs.baseline_offset

        # Apply test offset to target core
        try:
            success = self._smu.set_co_offset(test_core_id, test_offset)
        except Exception as e:
            self.log_message.emit(
                f"Failed to set CO for core {test_core_id}: {e}. "
                f"Pausing tuner."
            )
            self.pause()
            return False
        if not success:
            self.log_message.emit(
                f"CO write failed or read-back mismatch for core {test_core_id} "
                f"at offset {test_offset} — SMU did not apply the value. "
                f"Pausing tuner."
            )
            self.pause()
            return False
        self._co_applied[test_core_id] = test_offset
        return True

    def _revert_core_to_baseline(self, core_id: int) -> None:
        """Revert a single core to its baseline offset after a test."""
        if self._smu is None:
            return
        cs = self._core_states.get(core_id)
        if cs is None:
            return
        if self._co_applied.get(core_id) == cs.baseline_offset:
            return  # already at baseline
        try:
            success = self._smu.set_co_offset(core_id, cs.baseline_offset)
            if success:
                self._co_applied[core_id] = cs.baseline_offset
            else:
                log.warning(
                    "Post-test baseline revert failed for core %d (offset %d)",
                    core_id, cs.baseline_offset,
                )
        except Exception as e:
            log.warning("Post-test baseline revert error for core %d: %s", core_id, e)

    def _revert_all_to_baseline(self) -> None:
        """Best-effort revert of all cores to baseline — used after partial CO failure."""
        if self._smu is None:
            return
        for core_id, cs in self._core_states.items():
            if self._co_applied.get(core_id) == cs.baseline_offset:
                continue
            try:
                success = self._smu.set_co_offset(core_id, cs.baseline_offset)
                if success:
                    self._co_applied[core_id] = cs.baseline_offset
            except Exception:
                pass  # best-effort — log is noisy enough from the caller

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
