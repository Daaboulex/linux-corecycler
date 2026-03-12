"""Automated PBO Curve Optimizer tuner — core state machine and orchestrator.

Drives the coarse-to-fine search: big steps first, fine steps after failure,
confirmation at the settled value. Every state transition persists to SQLite
before acting, so the tuner resumes exactly where it left off after crash/reboot.

Test execution runs on a QThread so the GUI remains responsive.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal, Slot

from engine.backends.base import StressConfig
from engine.scheduler import CoreScheduler, SchedulerConfig

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


class _TunerWorker(QThread):
    """Runs one CoreScheduler test on a background thread."""

    finished = Signal(int, bool, str, str, float)  # core_id, passed, error_msg, error_type, duration

    def __init__(
        self,
        core_id: int,
        scheduler: CoreScheduler,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._core_id = core_id
        self._scheduler = scheduler

    def run(self) -> None:
        try:
            start = time.monotonic()
            results = self._scheduler.run()
            elapsed = time.monotonic() - start

            core_results = results.get(self._core_id, [])
            if core_results:
                r = core_results[0]
                self.finished.emit(
                    self._core_id, r.passed,
                    r.error_message or "", r.error_type or "", elapsed,
                )
            else:
                self.finished.emit(
                    self._core_id, False, "No result returned", "", elapsed,
                )
        except Exception as e:
            log.exception("Tuner worker crashed for core %d", self._core_id)
            self.finished.emit(self._core_id, False, str(e), "crash", 0.0)


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
    session_completed = Signal(dict)  # {core_id: best_offset}
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

        self._session_id: int | None = None
        self._core_states: dict[int, CoreState] = {}
        self._status: str = "idle"
        self._paused = False
        self._abort_requested = False
        self._consecutive_start_failures = 0
        self._worker: _TunerWorker | None = None

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
        for core_id in cores:
            cs = CoreState(core_id=core_id, current_offset=self._config.start_offset)
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
        """Resume a crashed/paused session."""
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

        # Re-apply CO offsets from saved state
        if self._smu is not None:
            for cs in self._core_states.values():
                if cs.phase not in ("not_started", "confirmed"):
                    try:
                        self._smu.set_co_offset(cs.core_id, cs.current_offset)
                    except Exception:
                        log.warning("Failed to re-apply CO for core %d", cs.core_id)

        # Find cores that were mid-test (in an active phase) — treat as failure
        for cs in self._core_states.values():
            if cs.phase in ("coarse_search", "fine_search", "confirming"):
                self.log_message.emit(
                    f"Core {cs.core_id} was interrupted at offset {cs.current_offset} "
                    f"— treating as failure"
                )
                self._advance_core(cs.core_id, passed=False)

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
                cs.current_offset = cfg.start_offset + direction * cfg.coarse_step
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
            case _:
                return self._pick_sequential()

    def _pick_sequential(self) -> int | None:
        """Finish each core completely before moving to next."""
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase not in ("confirmed", "settled"):
                if cs.phase == "not_started":
                    self._advance_core(core_id, passed=False)  # trigger first step
                return core_id
        # Check for settled cores that need confirmation
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase == "settled":
                self._advance_core(core_id, passed=False)  # trigger settled→confirming
                if cs.phase == "confirming":
                    return core_id
        return None

    def _pick_round_robin(self) -> int | None:
        """Cycle through all cores, one test each per round."""
        active = [
            cid for cid, cs in sorted(self._core_states.items())
            if cs.phase not in ("confirmed",)
        ]
        if not active:
            return None
        for core_id in active:
            cs = self._core_states[core_id]
            if cs.phase == "not_started":
                self._advance_core(core_id, passed=False)
            if cs.phase == "settled":
                self._advance_core(core_id, passed=False)
            if cs.phase not in ("confirmed",):
                return core_id
        return None

    def _pick_weakest_first(self) -> int | None:
        """Prioritize cores closest to settling."""
        candidates = []
        for core_id, cs in self._core_states.items():
            if cs.phase == "not_started":
                self._advance_core(core_id, passed=False)
            if cs.phase == "settled":
                self._advance_core(core_id, passed=False)
            if cs.phase in ("confirmed",):
                continue
            # Score: fine_search > coarse_search > confirming
            score = {"fine_search": 0, "confirming": 1, "coarse_search": 2,
                     "failed_confirm": 0}.get(cs.phase, 3)
            candidates.append((score, core_id))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

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
        self._emit_progress()
        self.log_message.emit(
            f"Testing core {core_id} at offset {cs.current_offset} "
            f"(phase: {cs.phase})"
        )

        # Apply CO offset via SMU
        if self._smu is not None:
            try:
                self._smu.set_co_offset(core_id, cs.current_offset)
            except Exception as e:
                self.log_message.emit(
                    f"Failed to set CO for core {core_id}: {e}"
                )
                # Treat as failure and continue
                self._on_test_finished(core_id, False, str(e), "", 0.0)
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

        self._worker = _TunerWorker(core_id, scheduler, parent=self)
        self._worker.finished.connect(self._on_test_finished)
        self._worker.start()

    @Slot(int, bool, str, str, float)
    def _on_test_finished(
        self,
        core_id: int,
        passed: bool,
        error_msg: str,
        error_type: str,
        duration: float,
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
        self.log_message.emit(
            f"Core {core_id} offset {cs.current_offset}: {status_str}"
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
        self.session_completed.emit(profile)

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
