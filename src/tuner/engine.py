"""Automated PBO Curve Optimizer tuner — core state machine and orchestrator.

Drives the coarse-to-fine search: big steps first, fine steps after failure,
confirmation at the settled value. Every state transition persists to SQLite
before acting, so the tuner resumes exactly where it left off after crash/reboot.

Test execution runs on a QThread so the GUI remains responsive.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from engine.backends import get_backend, load_all
from engine.backends.base import StressConfig
from engine.detector import (
    ErrorDetector,
    MCEEvent,
    harvest_kernel_mce,
    last_boot_ended_cleanly,
)
from engine.parallel import ParallelStress
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


def _has_unattributed_mce(mce_json: str) -> bool:
    """True when the worker's MCE payload holds an event naming no CPU.

    Fail closed at the payload level only: a malformed payload is no evidence
    (matching _foreign_mce_by_core); an explicit cpu == -1 event is.
    """
    if not mce_json:
        return False
    try:
        raw = json.loads(mce_json)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(raw, list):
        return False
    return any(isinstance(item, dict) and item.get("cpu") == -1 for item in raw)


def _rebooted_since(iso_ts: str | None, stat_path: str = "/proc/stat") -> bool:
    """True when the machine booted AFTER the given ISO timestamp.

    Resume uses this to tell a hard crash (reboot happened — penalize the
    resident offsets) from a plain app exit mid-test (no reboot — penalizing
    would walk proven-good offsets away). Fail closed: if either side cannot
    be determined, assume a reboot so the crash detectors still run.
    """
    if not iso_ts:
        return True
    try:
        from datetime import datetime

        last_write = datetime.fromisoformat(iso_ts).timestamp()
    except ValueError:
        return True
    try:
        with open(stat_path) as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1]) > last_write
    except (OSError, ValueError, IndexError):
        pass
    return True


# ------------------------------------------------------------------
# Worker thread — runs a single core test without blocking the GUI
# ------------------------------------------------------------------


_STRETCH_WARMUP_SECONDS = 5  # skip startup noise (process exec, turbo ramp)
_STRETCH_SAMPLE_INTERVAL = 5  # seconds between APERF/MPERF samples
_STRETCH_MIN_BUSY = 0.9  # a sample window counts only under sustained load


def _read_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _read_cpu_times(cpu_id: int) -> tuple[int, int] | None:
    """(idle+iowait, total) jiffies for one logical CPU from /proc/stat."""
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


def _busy_fraction(
    prev: tuple[int, int] | None, now: tuple[int, int] | None
) -> float | None:
    """Busy fraction of the window between two /proc/stat samples."""
    if prev is None or now is None:
        return None
    d_idle = now[0] - prev[0]
    d_total = now[1] - prev[1]
    if d_total <= 0:
        return None
    return 1.0 - (d_idle / d_total)


def _pick_report(results: dict[int, list], primary: int) -> tuple[int, object | None]:
    """Choose which core's verdict a multi-core scheduler run reports.

    The primary core is the default, but a failure on ANY core in the batch
    outranks the primary's pass.
    """
    primary_results = results.get(primary, [])
    report = primary_results[0] if primary_results else None
    if report is None or report.passed:
        for cid in sorted(results):
            rs = results[cid]
            if rs and not rs[0].passed:
                return cid, rs[0]
    return primary, report


def _serialize_mce(events: list[MCEEvent]) -> str:
    """JSON-encode observed MCE events for the worker's finished signal."""
    if not events:
        return ""
    return json.dumps([
        {
            "cpu": e.cpu,
            "bank": e.bank,
            "corrected": e.corrected,
            "message": e.message,
            "raw_ts": e.raw_ts,
        }
        for e in events
    ])


class _TunerWorker(QThread):
    """Runs one CoreScheduler test on a background thread.

    Optionally samples APERF/MPERF clock stretch during the test via a
    background sampler thread. The sampler waits for turbo to stabilise
    after process startup, then takes periodic 5-second windows and
    reports the **peak** stretch observed — not the average over the
    whole test. This avoids false positives from startup overhead,
    turbo ramp-up, and C-state transitions before load reaches 100%.
    """

    # core_id, passed, error_msg, error_type, duration, peak_stretch_pct,
    # mce_json, results_json (per-core verdicts of a multi-core run)
    finished = Signal(int, bool, str, str, float, float, str, str)

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

            mce_json = _serialize_mce(self._scheduler.observed_mce)
            report_core, report = _pick_report(results, self._core_id)
            if report is not None:
                self.finished.emit(
                    report_core, report.passed,
                    report.error_message or "", report.error_type or "", elapsed,
                    peak_stretch if report_core == self._core_id else 0.0,
                    mce_json, "",
                )
            else:
                # The scheduler produced no verdict for this core (stopped early,
                # environment problem) — that is NOT a stability failure.
                self.finished.emit(
                    self._core_id, False, "No result returned", "startup", elapsed,
                    peak_stretch, mce_json, "",
                )
        except Exception as e:
            # A Python exception in the harness is an app/environment fault,
            # not CPU instability — must not advance the search state machine.
            log.exception("Tuner worker crashed for core %d", self._core_id)
            self.finished.emit(self._core_id, False, str(e), "startup", 0.0, 0.0, "", "")

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
        busy_prev = _read_cpu_times(cpu)

        # Sample at intervals until test ends
        while not stop.wait(_STRETCH_SAMPLE_INTERVAL):
            readings = msr.read_clock_stretch([cpu])
            busy_now = _read_cpu_times(cpu)
            busy = _busy_fraction(busy_prev, busy_now)
            busy_prev = busy_now
            reading = readings.get(cpu)
            # A window without sustained load measures boost/idle behavior,
            # not clock stretch — discard it rather than report a number
            # that is not evidence (the MSR read above still advanced the
            # baseline, so the next window stays self-contained).
            if reading and (busy is None or busy >= _STRETCH_MIN_BUSY):
                samples.append(reading.stretch_pct)


class _RapidTransitionWorker(_TunerWorker):
    """Runs rapid transition validation on a background thread."""

    def __init__(
        self,
        core_id: int,
        logical_cpu: int,
        scheduler: CoreScheduler,
        cores: list[int],
        duration: float,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(core_id, logical_cpu, scheduler, msr=None, parent=parent)
        self._cores = cores
        self._duration = duration

    def run(self) -> None:
        try:
            start = time.monotonic()
            passed, error = self.scheduler.run_rapid_transitions(
                cores=self._cores,
                total_duration=self._duration,
            )
            elapsed = time.monotonic() - start
            self.finished.emit(
                self._core_id, passed, error or "", "", elapsed, 0.0,
                _serialize_mce(self.scheduler.observed_mce), "",
            )
        except Exception as e:
            log.exception("Rapid transition worker crashed for core %d", self._core_id)
            self.finished.emit(self._core_id, False, str(e), "crash", 0.0, 0.0, "", "")


class _ParallelWorker(_TunerWorker):
    """Runs one ParallelStress batch (all lanes simultaneously) on a thread."""

    def __init__(
        self,
        core_id: int,
        logical_cpu: int,
        runner: ParallelStress,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(core_id, logical_cpu, runner, msr=None, parent=parent)

    def run(self) -> None:
        try:
            start = time.monotonic()
            raw = self._scheduler.run()
            elapsed = time.monotonic() - start
            results = {c: [r] for c, r in raw.items()}
            mce_json = _serialize_mce(self._scheduler.observed_mce)
            results_json = json.dumps([
                {
                    "core": c,
                    "passed": r.passed,
                    "error_type": r.error_type,
                    "error_message": r.error_message,
                    "duration": r.duration_seconds,
                }
                for c, r in raw.items()
            ])
            report_core, report = _pick_report(results, self._core_id)
            if report is not None:
                self.finished.emit(
                    report_core, report.passed,
                    report.error_message or "", report.error_type or "", elapsed,
                    0.0, mce_json, results_json,
                )
            else:
                self.finished.emit(
                    self._core_id, False, "No result returned", "startup",
                    elapsed, 0.0, mce_json, results_json,
                )
        except Exception as e:
            log.exception("Parallel worker crashed for core %d", self._core_id)
            self.finished.emit(self._core_id, False, str(e), "startup", 0.0, 0.0, "", "")



class _SoakWorker(QThread):
    """Watches the kernel error stream with no load; any event ends the soak."""

    finished = Signal(int, bool, str, str, float, float, str, str)

    def __init__(self, core_id: int, duration: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._core_id = core_id
        self._duration = float(duration)
        self._stop = threading.Event()
        self.detector = ErrorDetector()
        self.scheduler = self  # abort() drives workers via scheduler.force_stop

    def stop(self) -> None:
        self._stop.set()

    force_stop = stop

    def run(self) -> None:
        try:
            self.detector.reset()
            start = time.monotonic()
            events: list[MCEEvent] = []
            while time.monotonic() - start < self._duration and not self._stop.is_set():
                events.extend(self.detector.check_mce())
                if events:
                    break
                self._stop.wait(5.0)
            elapsed = time.monotonic() - start
            if events:
                self.finished.emit(
                    self._core_id, False,
                    f"kernel error during soak: {events[0].message}", "mce",
                    elapsed, 0.0, _serialize_mce(events), "",
                )
            else:
                self.finished.emit(self._core_id, True, "", "", elapsed, 0.0, "", "")
        except Exception as e:
            log.exception("Soak worker crashed")
            self.finished.emit(self._core_id, False, str(e), "startup", 0.0, 0.0, "", "")

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
        self._boot_id = _read_boot_id()
        # Every narrative line becomes durable: the story survives the
        # terminal and is replayed on resume.
        self.log_message.connect(self._persist_narrative)

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
        # core_id → most-aggressive CO value proven survivable this session (the
        # machine lived a test with it resident). Seeds at 0 (stock is always
        # safe); rebuilt from the CO journal on resume. Baselines are NOT seeded
        # here — a baseline must earn "survived" like any other value.
        self._co_survived: dict[int, int] = {}

        # Multi-core validation state
        self._validation_stage: int = 0  # 0 = not validating, 1/2/3 = stage
        self._validation_thermal_aborts: int = 0  # consecutive thermal stops in validation
        self._apparatus_fault_streak: int = 0  # consecutive faults with no verdict
        self._validation_core_index: int = 0  # index into _validation_core_order for stage 1
        self._validation_core_order: list[int] = []  # cores to cycle through in stage 1
        self._validation_half_index: int = 0  # which half to test in stage 3
        self._validation_halves: list[list[int]] = []  # [half_a, half_b] for stage 3
        # Cores flagged in_test for the validation worker currently running, so a
        # hard crash during validation is attributed on resume (the confirmed
        # offsets it re-applies are journaled survived, so only in_test arms the
        # circuit breaker for a multi-core power-interaction crash).
        self._cores_under_stress: list[int] = []

        # Incremental validation: dirty = a back-off happened since the last
        # clean pass (DONE requires one full pass with dirty False); requeue =
        # cores owing a solo re-test because their offset changed.
        self._validation_dirty = False
        self._validation_requeue: list[int] = []
        self._in_requeue = False

        # Crash hunt: when a hard crash cannot be attributed by evidence, the
        # engine runs isolated per-core hunt slots instead of guessing.
        self._hunting = False
        self._hunt_queue: list[int] = []
        self._soaking = False
        # Post-reboot kernel-journal harvest, injectable for tests.
        self._forensics = harvest_kernel_mce

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
        self._validation_dirty = False
        self._validation_requeue = []
        self._in_requeue = False
        self._hunting = False
        self._hunt_queue = []
        self._soaking = False

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
        if any(v is not None for v in (ctx.ppt_limit_w, ctx.tdc_limit_a, ctx.edc_limit_a)):
            def _fmt(v: float | None, unit: str) -> str:
                return f"{v:.0f} {unit}" if v is not None else "unknown"
            self.log_message.emit(
                f"PBO power limits (recorded in tuning context): "
                f"PPT {_fmt(ctx.ppt_limit_w, 'W')}, "
                f"TDC {_fmt(ctx.tdc_limit_a, 'A')}, "
                f"EDC {_fmt(ctx.edc_limit_a, 'A')}"
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
        # A pause takes effect AFTER the in-flight test; resuming under a live
        # worker would rewrite SMU baselines beneath the running stress test
        # (false PASS at an untested offset) and orphan the worker thread.
        if self._worker is not None and self._worker.isRunning():
            self.log_message.emit(
                "Resume ignored: the current test is still finishing "
                "(pause takes effect after it completes)."
            )
            return
        self._abort_requested = False
        self._paused = False
        self._validation_stage = 0
        self._validation_dirty = False
        self._validation_requeue = []
        self._in_requeue = False
        self._hunting = False
        self._hunt_queue = []
        self._soaking = False
        self._session_id = session_id

        session = tp.get_session(self._db, session_id)
        if session is None:
            self.log_message.emit(f"Session {session_id} not found")
            return

        self._config = TunerConfig.from_json(session.config_json)
        if self._smu is not None:
            self._config.clamp_max_offset(self._smu.commands.co_range)

        # Fail closed on a corrupted/hand-edited config_json: from_json only
        # rejects wrong TYPES, so a well-typed but out-of-range value (e.g.
        # coarse_step=0, which makes the search non-convergent) survives it.
        # start() rejects those; resume must too.
        errors = self._config.validate()
        if errors:
            self.log_message.emit(f"Invalid tuner config: {'; '.join(errors)}")
            return

        self._core_states = tp.load_core_states(self._db, session_id)

        # Rebuild the proven-safe envelope from the CO journal: a value the machine
        # survived in a prior boot of this session is still safe (silicon stability
        # does not vanish across a reboot). Cores with no survived row stay at the
        # CO=0 default — baselines are never assumed safe without proof.
        if self._session_id is not None:
            for c, v in tp.journal_survived_values(self._db, self._session_id).items():
                self._co_survived[c] = v

        # Evidence reconciliation before anything acts on the loaded state.
        self._reconcile_confirmed_evidence()

        # One reboot verdict drives the drift check, crash detection, and the
        # baseline restore below — they must agree on what world they are in.
        rebooted = _rebooted_since(self._db.latest_session_activity(session_id))

        # Check for CO drift — warn only when the SMU differs from what the
        # TUNER last wrote (the CO journal); validation deliberately leaves the
        # confirmed offsets applied, so a baseline comparison would flag the
        # tuner's own work. Real drift = a third party (Curve Optimizer tab,
        # another tool) changed the values behind our back.
        if self._smu is not None:
            expected_values = tp.journal_values(self._db, session_id)
            drift: dict[int, dict[str, int]] = {}
            for cs in self._core_states.values():
                actual = self._smu.get_co_offset(cs.core_id)
                expected = expected_values.get(cs.core_id, cs.baseline_offset)
                if actual is None or actual == expected:
                    continue
                # After a reboot, actual == 0 is the EXPECTED state (SMU SRAM
                # is zeroed), not drift.
                if rebooted and actual == 0:
                    continue
                drift[cs.core_id] = {"expected": expected, "actual": actual}
            if drift:
                self.log_message.emit(
                    f"CO drift detected on {len(drift)} core(s) — SMU values differ "
                    f"from the tuner's last write; something outside the tuner "
                    f"changed them. The session's values will be re-applied."
                )
                self.co_drift_detected.emit(json.dumps(drift))

        # Step 1: Attribute the crash — evidence first, policy second, and when
        # neither applies, HUNT instead of guessing. Priority after a reboot:
        #   1. Kernel-journal forensics: MCE lines from the dead boot(s) name
        #      the faulting core directly — penalize exactly those cores.
        #   2. A persisted hunt slot: the box died while ONE core was stressed
        #      in isolation (every other core at stock) — proven culprit.
        #   3. A single in_test core in the SEARCH flow (isolation mode) — the
        #      only core away from baseline; penalize it.
        #   4. The CO write-ahead journal's un-survived residents.
        #   5. Anything else (multi-core in_test, or any crash under validation
        #      where all offsets are live and background load is uncontrolled):
        #      blame NOBODY — schedule an isolated per-core crash hunt.
        # Gate: crash handling only applies when the machine actually REBOOTED
        # since the session's last persisted write. A leftover in_test flag or
        # un-survived journal row with no reboot in between is a plain app exit
        # (window closed, SIGKILL mid-test) — penalizing it would walk good
        # offsets away on every restart.
        crashed: list[int] = []
        pending_hunt = False
        if rebooted:
            crashed, pending_hunt = self._attribute_crash_after_reboot(session)
        else:
            self._clear_all_in_test()
            if session.hunting_core is not None:
                # App exit mid-hunt without a reboot: no crash happened. The
                # hunt is abandoned; validation will re-expose the instability.
                tp.set_hunting_core(self._db, session_id, None)

        if crashed or pending_hunt:
            for core_id in crashed:
                self.log_message.emit(
                    f"Core {core_id} crash detected — applied penalty backoff"
                )
            self._set_status(
                f"resumed after crash (cores: {crashed})"
                if crashed else "resumed after unattributed crash"
            )
            # Circuit breaker: a resume that finds a fresh crash means the machine
            # died again on re-engage. Count consecutive crash-resumes (reset to 0
            # whenever a test completes — see _on_test_finished). After the
            # configured threshold, stop trying: force every core to stock (CO=0),
            # quarantine the session, and surface an honest unsafe verdict rather
            # than re-applying a profile that keeps crashing on every boot.
            streak = tp.get_resume_crash_streak(self._db, session_id) + 1
            tp.set_resume_crash_streak(self._db, session_id, streak)
            if streak >= self._config.resume_crash_quarantine_threshold:
                self._quarantine_session(streak)
                return
        else:
            tp.set_resume_crash_streak(self._db, session_id, 0)

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
                if cs.baseline_offset == 0 and rebooted:
                    # Reboot zeroes SMU SRAM, so 0 is already resident. Without
                    # a reboot the SMU holds whatever was live at app exit (a
                    # mid-test offset, e.g.) — it must be written back like any
                    # other baseline, never assumed.
                    self._co_applied[cs.core_id] = 0
                    continue
                try:
                    success = self._apply_co(cs.core_id, cs.baseline_offset)
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
                # Fail closed: continuing to test on a machine whose SMU cannot
                # even restore proven baselines would produce garbage verdicts
                # and leave unknown offsets resident.
                self.log_message.emit(
                    f"Baselines could not be restored for cores {failed_cores} — "
                    f"SMU access is broken or changed since last session. Pausing; "
                    f"fix SMU access (ryzen_smu module, permissions), then Resume."
                )
                self.pause()
                return
            self.log_message.emit(f"Restored baselines: {baselines}")

        # Restore the round-robin / CCD cycling cursor from the test log so the
        # cycling order continues across the reboot instead of restarting.
        self._reconstruct_scheduling_position()

        # A reboot mid-validation that produced no kernel evidence and left
        # nothing in-test (it hit between two slots, or the in-test mark was
        # lost with the crash) must not pass silently: the machine died with
        # the profile live. A provably clean shutdown is exempt — rebooting
        # deliberately is not an incident.
        unattributed_incident = (
            rebooted
            and not crashed
            and not pending_hunt
            and session.status == "validating"
            and not last_boot_ended_cleanly()
        )
        if unattributed_incident:
            n = tp.get_unattributed_crashes(self._db, session_id) + 1
            tp.set_unattributed_crashes(self._db, session_id, n)
            self.log_message.emit(
                f"The machine went down mid-validation with the profile live, "
                f"but no kernel evidence names a core and nothing was marked "
                f"in-test — recorded unattributed incident "
                f"{n}/{self._config.max_unattributed_crash_hunts}. The final "
                f"clean validation pass is owed again."
            )
            if n >= self._config.max_unattributed_crash_hunts:
                self.log_message.emit(
                    "The machine keeps dying around validation with no "
                    "attributable evidence. Pausing for your decision: rule "
                    "out an external cause (foreign load, another tool, "
                    "power/BIOS), or lower max_offset. Resume continues "
                    "validation."
                )
                # The owed clean pass survives the pause: persist dirty with
                # the session's own cursor (the engine's is not restored yet).
                tp.set_validation_position(
                    self._db, session_id,
                    session.validation_stage, session.validation_index,
                    session.validation_half, True,
                    session.validation_requeue or "[]",
                )
                self.pause()
                return

        # An unattributed crash outranks re-entering validation: find the
        # culprit in isolation first, or validation just crashes again.
        if pending_hunt:
            self.log_message.emit(f"Resumed session {session_id} — starting crash hunt")
            tp.update_session_status(self._db, session_id, "validating")
            self._start_hunt()
            return

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
            self._enter_auto_validation(profile, resume_from=session)
            if unattributed_incident and not self._validation_dirty:
                # The incident invalidates any clean-pass credit: even if the
                # remaining stages pass, one full clean pass is owed.
                self._validation_dirty = True
                self._save_validation_pos()
        else:
            self._set_status("running")
            tp.update_session_status(self._db, session_id, "running")
            self.log_message.emit(f"Resumed session {session_id}")
            self._run_next()

    def pause(self) -> None:
        """Pause after the current test completes."""
        if self._session_id:
            session = tp.get_session(self._db, self._session_id)
            if session is not None and session.status == "quarantined":
                self.log_message.emit(
                    "Pause ignored: the session is quarantined and stays that way."
                )
                return
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
        # Clear the in-flight flag and revert EVERY core to baseline so no
        # aggressive offset lingers in the SMU after abort. Reverting only the
        # tested core is wrong during validation, where all confirmed cores are
        # applied at once — the others would be left at aggressive CO.
        if tested_core is not None:
            cs = self._core_states.get(tested_core)
            if cs is not None:
                cs.in_test = False
        self._clear_cores_under_stress()
        self._revert_all_to_baseline()
        self._validation_stage = 0
        self._in_requeue = False
        self._soaking = False
        if self._hunting:
            self._hunting = False
            self._hunt_queue = []
            if self._session_id:
                tp.set_hunting_core(self._db, self._session_id, None)
        self._set_status("idle")
        if self._session_id:
            session = tp.get_session(self._db, self._session_id)
            if session is None or session.status != "quarantined":
                tp.update_session_status(self._db, self._session_id, "aborted")
        self.log_message.emit("Tuner aborted")

    def validate_profile(self, session_id: int) -> None:
        """Re-test all confirmed values from a completed session."""
        if self._worker is not None and self._worker.isRunning():
            self.log_message.emit(
                "Validate ignored: a test is still running — wait for it to finish."
            )
            return
        self._abort_requested = False
        self._paused = False
        self._validation_stage = 0
        self._validation_dirty = False
        self._validation_requeue = []
        self._in_requeue = False
        self._hunting = False
        self._hunt_queue = []
        self._soaking = False
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
            # Fail closed on a corrupted config_json, exactly as resume()/start().
            errors = self._config.validate()
            if errors:
                self.log_message.emit(f"Invalid tuner config: {'; '.join(errors)}")
                return

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

    def _get_active_stress_config(self, cs: CoreState) -> tuple[str, str, str]:
        """Return (backend, stress_mode, fft_preset) for the current core's phase."""
        if cs.phase in (TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2):
            tier = self._config.hardening_tiers[cs.hardening_tier_index]
            return tier["backend"], tier["stress_mode"], tier["fft_preset"]
        return self._config.backend, self._config.stress_mode, self._config.fft_preset

    def _get_backend_for_name(self, name: str) -> StressBackend:
        """Return the injected primary backend or instantiate a named tier backend."""
        if name == self._config.backend:
            return self._backend
        try:
            return get_backend(name)
        except KeyError:
            load_all()
            return get_backend(name)

    def _advance_core(self, core_id: int, passed: bool) -> None:
        """State machine transitions for a single core."""
        cs = self._core_states[core_id]
        cfg = self._config
        direction = cfg.direction  # -1 for undervolting

        # --- Hardening transitions (checked before the main match) ---
        if cs.phase in (TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2):
            if passed:
                next_tier = cs.hardening_tier_index + 1
                if next_tier >= len(cfg.hardening_tiers):
                    cs.phase = TunerPhase.HARDENED
                else:
                    cs.hardening_tier_index = next_tier
                    # Alternate between T1/T2 labels for any number of tiers
                    cs.phase = (
                        TunerPhase.HARDENING_T1 if next_tier % 2 == 0
                        else TunerPhase.HARDENING_T2
                    )
            else:
                # Back off linearly by fine_step
                new_offset = cs.current_offset - (direction * cfg.fine_step)
                if self._at_or_past_baseline(new_offset, cs):
                    cs.current_offset = cs.baseline_offset
                    cs.best_offset = cs.baseline_offset
                    cs.phase = TunerPhase.HARDENED
                else:
                    cs.current_offset = new_offset
                    cs.best_offset = new_offset
                    # Stay at current tier (T2 fail retries T2, not T1)
            # Persist
            if self._session_id:
                tp.save_core_state(self._db, self._session_id, cs)
            self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
            return

        # Total-function normalization: the backoff arithmetic assumes
        # best_offset is set (the crash penalty seeds it), but a persisted row
        # from an older version or a hand-edit can violate that. Fail closed to
        # the baseline instead of a TypeError mid-transition.
        if cs.best_offset is None and cs.phase in (
            TunerPhase.BACKOFF_PRECONFIRM,
            TunerPhase.BACKOFF_CONFIRMING,
        ):
            cs.best_offset = cs.baseline_offset

        # Contradictory-evidence guard: a PASS at/beyond the recorded fail
        # bound must never widen the bounds — failures outrank passes for
        # safety, and letting the pass through inverts the bounds so the
        # backoff binary search DIVERGES toward more aggressive values.
        if (
            passed
            and cs.phase in (TunerPhase.BACKOFF_PRECONFIRM, TunerPhase.BACKOFF_CONFIRMING)
            and self._handle_contradictory_pass(cs)
        ):
            if self._session_id:
                tp.save_core_state(self._db, self._session_id, cs)
            self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
            return

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
                    next_offset = cs.current_offset + direction * self._get_coarse_step(cs)
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
                        # Never start the first fine test past the safety cap.
                        if self._exceeds_max(cs.current_offset):
                            cs.current_offset = cfg.max_offset
                        # Don't re-test the known coarse-fail offset (guaranteed
                        # fail) — settle at the last good value instead.
                        if cs.coarse_fail_offset is not None and (
                            (direction < 0 and cs.current_offset <= cs.coarse_fail_offset)
                            or (direction > 0 and cs.current_offset >= cs.coarse_fail_offset)
                        ):
                            cs.phase = TunerPhase.SETTLED

            case TunerPhase.FINE_SEARCH:
                if passed:
                    cs.best_offset = cs.current_offset
                    next_offset = cs.current_offset + direction * cfg.fine_step
                    # Stop if we'd reach or pass the coarse fail point
                    if cs.coarse_fail_offset is not None and (
                        (direction < 0 and next_offset <= cs.coarse_fail_offset)
                        or (direction > 0 and next_offset >= cs.coarse_fail_offset)
                    ) or self._exceeds_max(next_offset):
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
                    if cfg.hardening_tiers:
                        cs.phase = TunerPhase.HARDENING_T1
                        cs.hardening_tier_index = 0
                    else:
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
                    # The value that just passed IS the proven best (handles the
                    # crash-before-any-pass case where best_offset was None).
                    cs.best_offset = cs.current_offset
                    had_pass_bound = cs.backoff_pass_bound is not None
                    cs.backoff_pass_bound = cs.best_offset
                    if had_pass_bound and cs.backoff_fail_bound is not None:
                        # Binary search active — jump to midpoint
                        gap = abs(cs.backoff_fail_bound - cs.backoff_pass_bound)
                        if gap <= cfg.fine_step:
                            # Converged: settle at the PROVEN pass bound, never an
                            # untested midpoint.
                            cs.best_offset = cs.backoff_pass_bound
                            cs.current_offset = cs.backoff_pass_bound
                            cs.phase = TunerPhase.CONFIRMED
                        else:
                            # Probe the midpoint as current ONLY; best stays at the
                            # proven pass bound until the midpoint itself passes.
                            mid = cs.backoff_pass_bound + direction * (gap // 2)
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
                        floor = self._backoff_floor(cs, midpoint)
                        if floor is not None:
                            cs.phase = TunerPhase.CONFIRMED
                            cs.best_offset = floor
                            cs.current_offset = floor
                        elif self._at_or_past_baseline(midpoint, cs) or midpoint == cs.best_offset:
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
                        floor = self._backoff_floor(cs, new_offset)
                        if floor is not None:
                            cs.phase = TunerPhase.CONFIRMED
                            cs.best_offset = floor
                            cs.current_offset = floor
                        elif self._at_or_past_baseline(new_offset, cs):
                            cs.phase = TunerPhase.CONFIRMED
                            cs.best_offset = cs.baseline_offset
                            cs.current_offset = cs.baseline_offset
                        else:
                            cs.best_offset = new_offset
                            cs.current_offset = new_offset

            case TunerPhase.BACKOFF_CONFIRMING:
                if passed:
                    # The confirmed value is proven — record it as best and pass bound.
                    cs.best_offset = cs.current_offset
                    cs.backoff_pass_bound = cs.current_offset
                    if cs.backoff_fail_bound is not None:
                        # Binary search: try midpoint between pass and fail bounds
                        gap = abs(cs.backoff_fail_bound - cs.backoff_pass_bound)
                        if gap <= cfg.fine_step:
                            # Converged: settle at the proven pass bound, then harden.
                            cs.best_offset = cs.backoff_pass_bound
                            cs.current_offset = cs.backoff_pass_bound
                            if cfg.hardening_tiers:
                                cs.phase = TunerPhase.HARDENING_T1
                                cs.hardening_tier_index = 0
                            else:
                                cs.phase = TunerPhase.CONFIRMED
                        else:
                            # Probe the midpoint as current ONLY (never recorded as
                            # best until it passes).
                            mid = cs.backoff_pass_bound + direction * (gap // 2)
                            cs.current_offset = mid
                            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    else:
                        if cfg.hardening_tiers:
                            cs.phase = TunerPhase.HARDENING_T1
                            cs.hardening_tier_index = 0
                        else:
                            cs.phase = TunerPhase.CONFIRMED
                else:
                    # Confirm failed — back to preconfirm, back off
                    cs.phase = TunerPhase.BACKOFF_PRECONFIRM
                    new_offset = cs.best_offset - direction * cfg.fine_step
                    floor = self._backoff_floor(cs, new_offset)
                    if floor is not None:
                        cs.phase = TunerPhase.CONFIRMED
                        cs.best_offset = floor
                        cs.current_offset = floor
                    elif self._at_or_past_baseline(new_offset, cs):
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

    def _handle_contradictory_pass(self, cs: CoreState) -> bool:
        """Handle a PASS at an offset at-or-beyond the recorded fail bound.

        That is contradictory evidence — intermittent instability, or stale
        persisted bounds. The conservative resolution: the failure stands, the
        pass is not allowed to widen the bounds; step back to just inside the
        fail bound and keep searching there. Returns True when handled.
        """
        fb = cs.backoff_fail_bound
        if fb is None or self._is_more_aggressive(fb, cs.current_offset):
            return False
        step_back = fb - self._config.direction * self._config.fine_step
        if self._at_or_past_baseline(step_back, cs):
            cs.phase = TunerPhase.CONFIRMED
            cs.best_offset = cs.baseline_offset
            cs.current_offset = cs.baseline_offset
        else:
            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
            cs.current_offset = step_back
            if cs.best_offset is not None and self._is_more_aggressive(
                cs.best_offset, step_back
            ):
                cs.best_offset = step_back
        return True

    def _get_coarse_step(self, cs: CoreState) -> int:
        """Get coarse step size, reducing near max_offset for safety."""
        distance = abs(cs.current_offset - self._config.max_offset)
        ramp_zone = self._config.coarse_step * 2
        if distance <= ramp_zone:
            return self._config.fine_step
        return self._config.coarse_step

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

    def _is_more_aggressive(self, a: int, b: int) -> bool:
        """Returns True if offset a is more aggressive than b."""
        if self._config.direction == -1:
            return a < b
        return a > b

    def _backoff_floor(self, cs: CoreState, new_offset: int) -> int | None:
        """A confirmed backoff pass_bound is a hard floor for the fail paths.

        Returns the pass_bound to settle at when ``new_offset`` would be less
        aggressive than it — a fully-confirmed offset must never be abandoned
        for a weaker one — else None (the new offset is safe to use).
        """
        pb = cs.backoff_pass_bound
        if pb is not None and self._is_more_aggressive(pb, new_offset):
            return pb
        return None

    def _apply_crash_penalty(
        self, cs: CoreState, *, steps: int | None = None, count_crash: bool = True
    ) -> None:
        """Apply crash penalty: backoff + hard fail bound (+ cooldown).

        ``steps`` overrides crash_penalty_steps for evidence-grade reactions —
        a corrected MCE is a warning, not a crash, so it backs off one step
        (count_crash=False keeps crash bookkeeping honest: nothing crashed).
        All safety invariants (fail-bound monotonicity, CO=0 floor, baseline
        descent, confirmation invalidation) apply identically.
        """
        crashed_offset = cs.current_offset
        # fail_bound tracks the LEAST aggressive offset known to fail. Stability is
        # monotonic (anything more aggressive than a failing offset also fails), so
        # this is the tightest SAFE bound, and it lets the backoff binary search
        # converge: a crash at a midpoint less aggressive than the old bound must
        # TIGHTEN it, otherwise the search oscillates forever.
        if cs.backoff_fail_bound is None or not self._is_more_aggressive(
            crashed_offset, cs.backoff_fail_bound
        ):
            cs.backoff_fail_bound = crashed_offset
        # Back off by crash_penalty_steps (or the caller's override)
        penalty = (
            steps if steps is not None else self._config.crash_penalty_steps
        ) * self._config.fine_step
        new_offset = crashed_offset - (self._config.direction * penalty)
        # CO=0 (stock voltage) is the only axiomatically safe state. Never let a
        # backoff overshoot past 0 to the opposite, more-aggressive side.
        if self._is_more_aggressive(0, new_offset):
            new_offset = 0
        if self._at_or_past_baseline(crashed_offset, cs):
            # The crash happened at or below the baseline's aggressiveness, so the
            # baseline itself is unstable — it is NOT a safe floor. Descend the
            # baseline toward 0 so the search can never again settle on the value
            # that just crashed the machine. This is what breaks the resume loop
            # where an unstable baseline is re-applied on every boot.
            cs.baseline_offset = new_offset
            cs.current_offset = new_offset
        elif self._at_or_past_baseline(new_offset, cs):
            # Normal search crash (more aggressive than baseline): stop at the
            # proven-stable baseline.
            cs.current_offset = cs.baseline_offset
        else:
            cs.current_offset = new_offset
        if count_crash:
            cs.crash_count += 1
            cs.crash_cooldown = 2
        # A core that crashed before ever passing has no proven-safe best yet; the
        # only known-safe value is its baseline. Seed it so the backoff math (which
        # assumes best_offset is set) never produces a None offset.
        if cs.best_offset is None:
            cs.best_offset = cs.baseline_offset
        # A value resident at a hard crash can never remain "best": validation and
        # finalize re-apply best_offset, so leaving it would re-crash the box on
        # every resume. Demote it to the penalized offset (backoff-candidate
        # semantics — it must still pass a test before being confirmed again).
        elif self._is_more_aggressive(cs.best_offset, cs.current_offset):
            cs.best_offset = cs.current_offset
        # Force into backoff — including CONFIRMING/CONFIRMED/HARDENED: a hard
        # crash at a confirmed value invalidates the confirmation, and the core
        # must re-earn it (otherwise validation re-applies the crashed value).
        if cs.phase in (
            TunerPhase.COARSE_SEARCH,
            TunerPhase.FINE_SEARCH,
            TunerPhase.CONFIRMING,
            TunerPhase.CONFIRMED,
            TunerPhase.BACKOFF_PRECONFIRM,
            TunerPhase.HARDENING_T1,
            TunerPhase.HARDENING_T2,
            TunerPhase.HARDENED,
        ):
            cs.phase = TunerPhase.BACKOFF_PRECONFIRM
            cs.backoff_mode = True

    def _apparatus_suspect(self, core_id: int) -> bool:
        """Trip on a physically implausible fail streak and recover from evidence.

        Post-fail steps only ADD voltage, so a healthy apparatus cannot fail
        ``apparatus_failure_streak`` times in a row on one core (a broken
        backend, stale results file, or dying disk can). Roll back to the most
        aggressive PROVEN pass (passes cannot be faked by a stale error file),
        clear the backoff bounds, and pause. Synthetic crash rows (duration
        NULL) are reboots, not apparatus verdicts, and do not count.

        Returns True when tripped (the caller must stop this flow).
        """
        threshold = self._config.apparatus_failure_streak
        if threshold <= 0 or self._session_id is None:
            return False
        rows = [
            r for r in tp.get_test_log(self._db, self._session_id, core_id=core_id)
            if r.get("duration_seconds") is not None
        ]
        streak = 0
        for r in reversed(rows):
            if r["passed"]:
                break
            streak += 1
        if streak < threshold:
            return False

        cs = self._core_states[core_id]
        rollback = self._rollback_core_to_evidence(cs)
        self.log_message.emit(
            f"APPARATUS SUSPECT: core {core_id} failed {streak} consecutive tests "
            f"while every step ADDED voltage — implausible for healthy tooling. "
            f"Rolled back to the most aggressive proven pass ({rollback}); backoff "
            f"bounds cleared; the core must re-confirm. Check the stress backend, "
            f"work directory and log, fix the cause, then Resume."
        )
        self.pause()
        return True

    def _most_aggressive_pass(self, core_id: int) -> int | None:
        """Most aggressive offset with a real logged PASS for this core, or None."""
        best_pass: int | None = None
        for r in tp.get_test_log(self._db, self._session_id, core_id=core_id):
            if r.get("duration_seconds") is None or not r["passed"]:
                continue
            if best_pass is None or self._is_more_aggressive(r["offset_tested"], best_pass):
                best_pass = r["offset_tested"]
        return best_pass

    def _rollback_core_to_evidence(self, cs: CoreState) -> int:
        """Reset a core to its most aggressive PROVEN pass (else baseline).

        Passes are the trustworthy evidence class — a broken apparatus can fake
        a FAIL but not a PASS. The rolled-back value still must re-earn
        confirmation (phase CONFIRMING); poisoned backoff bounds are cleared.
        Returns the rollback offset.
        """
        best_pass = self._most_aggressive_pass(cs.core_id)
        rollback = best_pass if best_pass is not None else cs.baseline_offset
        cs.current_offset = rollback
        cs.best_offset = rollback
        cs.phase = TunerPhase.CONFIRMING
        cs.confirm_attempts = 0
        cs.backoff_mode = False
        cs.consecutive_backoff_fails = 0
        cs.backoff_fail_bound = None
        cs.backoff_pass_bound = None
        tp.save_core_state(self._db, self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
        return rollback

    def _reconcile_confirmed_evidence(self) -> None:
        """State-estimator consistency check on resume: a core CLAIMING
        confirmed/hardened status must be backed by a logged pass at least as
        aggressive as its best_offset (its confirm run logged exactly that).
        best == baseline is exempt (the null result needs no proof; baseline is
        the ambient state). A claim without evidence — corruption, a hand-edited
        row, an upstream bug — is demoted to re-earn confirmation rather than
        being re-applied as truth by validation/finalize.
        """
        for cs in self._core_states.values():
            if cs.phase not in (TunerPhase.CONFIRMED, TunerPhase.HARDENED):
                continue
            if cs.best_offset is None or cs.best_offset == cs.baseline_offset:
                continue
            proof = self._most_aggressive_pass(cs.core_id)
            if proof is not None and not self._is_more_aggressive(cs.best_offset, proof):
                continue  # a pass at-or-beyond best exists — claim is backed
            claimed_phase, claimed_best = cs.phase, cs.best_offset
            rollback = self._rollback_core_to_evidence(cs)
            self.log_message.emit(
                f"EVIDENCE MISMATCH: core {cs.core_id} claimed {claimed_phase} at "
                f"best={claimed_best} with no logged pass to back it — demoted "
                f"to re-confirm at {rollback}."
            )

    def _attribute_crash_after_reboot(self, session) -> tuple[list[int], bool]:
        """Attribute a hard crash on the resume-after-reboot path.

        Returns (penalized_core_ids, pending_hunt). Evidence outranks policy:
        kernel-journal MCE lines name cores directly; a persisted hunt slot is
        proof by isolation; a single in-test core in the SEARCH flow is the
        only core away from baseline. A multi-core set — or any crash under
        validation, where every core holds offsets and background load is
        uncontrolled — is never guessed at: it returns pending_hunt=True so
        the caller runs the isolated crash hunt instead.
        """
        session_id = self._session_id
        crashed: list[int] = []
        pending_hunt = False
        forensic_events: list[MCEEvent] = []
        since = self._db.latest_session_activity(session_id)
        if since:
            forensic_events, forensic_ok = self._forensics(since)
            if not forensic_ok:
                self.log_message.emit(
                    "Kernel-journal forensics unavailable — falling back to "
                    "in-test/journal attribution."
                )
        forensic_by_core = self._events_by_core(forensic_events)
        if forensic_by_core:
            crashed = self._penalize_forensic_cores(forensic_by_core)
            self._clear_all_in_test()
            if session.hunting_core is not None:
                tp.set_hunting_core(self._db, session_id, None)
            tp.set_unattributed_crashes(self._db, session_id, 0)
        elif (
            session.hunting_core is not None
            and session.hunting_core in self._core_states
        ):
            culprit = self._core_states[session.hunting_core]
            self.log_message.emit(
                f"Crash during isolated hunt slot — core {culprit.core_id} is "
                f"the proven culprit (every other core was at stock)."
            )
            crashed = self._penalize_cores([culprit], "isolated hunt slot")
            self._clear_all_in_test()
            tp.set_hunting_core(self._db, session_id, None)
            tp.set_unattributed_crashes(self._db, session_id, 0)
        else:
            in_test = [cs for cs in self._core_states.values() if cs.in_test]
            ambiguous = session.status == "validating" or len(in_test) > 1
            if in_test and not ambiguous:
                crashed = self._penalize_cores(
                    in_test, "single in-test core, isolation mode"
                )
            elif in_test:
                self.log_message.emit(
                    f"Crash with {len(in_test)} core(s) under load in an "
                    f"all-offsets-live context — cannot attribute this by "
                    f"policy without punishing an innocent core. Scheduling "
                    f"an isolated crash hunt instead of guessing."
                )
                pending_hunt = True
            self._clear_all_in_test()
            journal_crashed = self._handle_journal_suspects(set(crashed))
            if journal_crashed:
                # Un-survived residents are real evidence — no hunt needed.
                crashed = sorted(set(crashed) | set(journal_crashed))
                pending_hunt = False
        return crashed, pending_hunt

    def _clear_all_in_test(self) -> None:
        """Clear and persist every in_test flag — the crash has been handled
        (or ruled out); a stale flag must not re-fire a detector later."""
        for cs in self._core_states.values():
            if cs.in_test:
                cs.in_test = False
                if self._session_id is not None:
                    tp.save_core_state(self._db, self._session_id, cs)

    def _cpu_to_core(self) -> dict[int, int]:
        """Logical CPU id -> physical core id, covering every SMT sibling."""
        mapping: dict[int, int] = {}
        for core_id, info in self._topology.cores.items():
            for lcpu in info.logical_cpus:
                mapping[lcpu] = core_id
        return mapping

    def _events_by_core(self, events: list[MCEEvent]) -> dict[int, list[MCEEvent]]:
        """Group kernel events by physical core; drop unattributable ones.

        Events with no CPU (kernel panic traces) prove a crash happened but
        name no core — they must not be turned into a per-core penalty.
        """
        cpu_map = self._cpu_to_core()
        out: dict[int, list[MCEEvent]] = {}
        for e in events:
            if e.cpu < 0:
                continue
            core = cpu_map.get(e.cpu)
            if core is None or core not in self._core_states:
                continue
            out.setdefault(core, []).append(e)
        return out

    def _penalize_forensic_cores(
        self, by_core: dict[int, list[MCEEvent]]
    ) -> list[int]:
        """Crash-penalize exactly the cores the kernel journal named.

        The penalty anchors at the CO value the journal says was resident at
        crash time — not whatever offset the persisted search state happens to
        hold — so the fail bound lands on the value that actually died.
        """
        journal = (
            tp.journal_values(self._db, self._session_id)
            if self._session_id is not None else {}
        )
        crashed: list[int] = []
        for core_id in sorted(by_core):
            cs = self._core_states[core_id]
            resident = journal.get(core_id, cs.current_offset)
            cs.current_offset = resident
            first = by_core[core_id][0]
            tp.log_test_result(
                self._db, self._session_id, core_id, resident, cs.phase.value,
                passed=False,
                error_msg=(
                    f"Reboot after hard crash; kernel journal names this core "
                    f"({len(by_core[core_id])} MCE line(s), e.g. "
                    f"'{first.message[:120]}'). Offset {resident} was resident."
                ),
                error_type="crash", duration=None,
            )
            self._apply_crash_penalty(cs)
            tp.save_core_state(self._db, self._session_id, cs)
            crashed.append(core_id)
            self.log_message.emit(
                f"Kernel forensics: core {core_id} named by MCE at offset "
                f"{resident} — crash penalty applied, now {cs.current_offset}."
            )
        return crashed

    def _penalize_cores(self, targets: list[CoreState], reason: str) -> list[int]:
        """Apply the crash penalty to attributed cores (synthetic log row each)."""
        crashed: list[int] = []
        for cs in targets:
            crashed_offset = cs.current_offset
            tp.log_test_result(
                self._db,
                self._session_id,
                cs.core_id,
                crashed_offset,
                cs.phase.value,
                passed=False,
                error_msg=(
                    f"System reboot detected ({reason}). "
                    f"Offset {crashed_offset} caused hard crash."
                ),
                error_type="crash",
                duration=None,
            )
            self._apply_crash_penalty(cs)
            tp.save_core_state(self._db, self._session_id, cs)
            crashed.append(cs.core_id)
            logging.warning(
                "Core %d: crash detected at offset %d — applied penalty, "
                "new offset %d, crash_count=%d",
                cs.core_id,
                crashed_offset,
                cs.current_offset,
                cs.crash_count,
            )
        return crashed

    def _handle_journal_suspects(self, already: set[int]) -> list[int]:
        """Penalize cores whose CO value was resident, un-survived, when the box died.

        The CO write-ahead journal records every value made resident in the SMU
        before the hardware write, so a hard crash with no in_test flag (idle,
        baseline restore, post-test revert, validation) is still caught here.
        ``already`` holds cores handled by in_test detection — skipped to avoid a
        double penalty. Returns the list of core ids penalized.
        """
        if self._session_id is None:
            return []
        handled: list[int] = []
        for core_id, value in tp.journal_suspects(self._db, self._session_id):
            if core_id in already:
                continue
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            # Anchor the penalty at the value that was actually resident at crash
            # time (the journal), which may differ from the persisted offset.
            cs.current_offset = value
            tp.log_test_result(
                self._db, self._session_id, core_id, value, cs.phase.value,
                passed=False,
                error_msg=(
                    f"Reboot detected. Offset {value} was resident (CO journal) "
                    f"and not proven survivable — treated as a hard crash."
                ),
                error_type="crash", duration=None,
            )
            self._apply_crash_penalty(cs)
            cs.in_test = False
            tp.save_core_state(self._db, self._session_id, cs)
            handled.append(core_id)
            logging.warning(
                "Core %d: CO-journal crash suspect at offset %d — penalty applied, "
                "new offset %d", core_id, value, cs.current_offset,
            )
        return handled

    # ------------------------------------------------------------------
    # Hardware-error evidence (cross-core MCE) and the isolated crash hunt
    # ------------------------------------------------------------------

    def _foreign_mce_by_core(self, tested_core: int, mce_json: str) -> dict[int, dict]:
        """Parse the worker's observed-MCE payload into evidence about cores
        OTHER than the tested one. Fail closed: malformed JSON is no evidence.

        Returns {core_id: {"corrected": bool, "messages": [...]}} where
        corrected is False when ANY event for that core was uncorrected.
        """
        if not mce_json:
            return {}
        try:
            raw = json.loads(mce_json)
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(raw, list):
            return {}
        cpu_map = self._cpu_to_core()
        out: dict[int, dict] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            cpu = item.get("cpu")
            if not isinstance(cpu, int) or cpu < 0:
                continue
            core = cpu_map.get(cpu)
            if core is None or core == tested_core or core not in self._core_states:
                continue
            entry = out.setdefault(core, {"corrected": True, "messages": []})
            if not item.get("corrected", False):
                entry["corrected"] = False
            msg = item.get("message")
            if isinstance(msg, str):
                entry["messages"].append(msg)
        return out

    def _apply_foreign_evidence(self, foreign: dict[int, dict]) -> None:
        """Act on kernel events that named other cores during a test."""
        for core_id in sorted(foreign):
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            resident = self._co_applied.get(core_id)
            if resident is None:
                resident = cs.current_offset
            info = foreign[core_id]
            self._apply_mce_evidence(core_id, resident, info["corrected"], info["messages"])

    def _apply_mce_evidence(
        self, core_id: int, resident: int, corrected: bool, messages: list[str]
    ) -> None:
        """React to a kernel hardware-error report naming this core while
        ``resident`` was its live CO value.

        Corrected error: the value is marginal — one-step penalty and re-earn
        confirmation (proportionate: the machine did not crash). Uncorrected:
        near-crash — full crash penalty. At stock (resident == 0) there is
        nothing to back off: the instability is not Curve Optimizer induced,
        so surface it loudly instead of walking a zero offset.
        """
        cs = self._core_states.get(core_id)
        if cs is None or self._session_id is None:
            return
        detail = messages[0] if messages else "kernel MCE"
        tp.log_test_result(
            self._db, self._session_id, core_id, resident, "mce_evidence",
            passed=False,
            error_msg=(
                f"Kernel reported a hardware error on this core at resident "
                f"offset {resident}: {detail}"
            ),
            error_type="mce", duration=None,
        )
        if resident == 0:
            self.log_message.emit(
                f"Core {core_id}: hardware error at STOCK settings (CO=0) — not a "
                f"Curve Optimizer problem. Check cooling, memory, or BIOS."
            )
            return
        cs.current_offset = resident
        if corrected:
            self._apply_crash_penalty(cs, steps=1, count_crash=False)
            self.log_message.emit(
                f"Core {core_id}: corrected hardware error at offset {resident} — "
                f"backed off one step to {cs.current_offset}; the core must "
                f"re-earn confirmation."
            )
        else:
            self._apply_crash_penalty(cs)
            self.log_message.emit(
                f"Core {core_id}: UNCORRECTED hardware error at offset {resident} — "
                f"crash-grade penalty applied, now {cs.current_offset}."
            )
        tp.save_core_state(self._db, self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)

    def _hunt_suspicion_key(self, core_id: int) -> tuple[int, int, int]:
        """Higher = more suspect: prior kernel-error rows, crash history, then
        deepest undervolt."""
        mce_rows = 0
        if self._session_id is not None:
            mce_rows = sum(
                1 for r in tp.get_test_log(self._db, self._session_id, core_id=core_id)
                if r.get("error_type") == "mce"
            )
        cs = self._core_states[core_id]
        aggressiveness = self._config.direction * (
            cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        )
        return (mce_rows, cs.crash_count, aggressiveness)

    def _start_hunt(self) -> None:
        """Hunt the culprit of an unattributed crash: each core alone at its
        tuned value, every other core at STOCK, under stress + load transitions
        + idle watch — a failure or crash in a slot names exactly one core."""
        order = sorted(
            self._core_states,
            key=self._hunt_suspicion_key,
            reverse=True,
        )
        self._hunt_queue = order
        self._hunting = True
        self._validation_stage = 0
        self._validation_thermal_aborts = 0
        self._set_status("hunting")
        self.log_message.emit(
            f"Crash hunt: isolated per-core slots ({self._config.hunt_slot_seconds}s "
            f"stress + transitions + idle each), most suspect first: {order}"
        )
        self._run_next_hunt_slot()

    def _run_next_hunt_slot(self) -> None:
        if self._abort_requested or self._paused:
            return
        if not self._hunt_queue:
            self._end_hunt_fruitless()
            return
        core_id = self._hunt_queue.pop(0)
        cs = self._core_states[core_id]
        target = cs.best_offset if cs.best_offset is not None else cs.baseline_offset

        if self._smu is not None:
            for other_id in self._core_states:
                if other_id == core_id or self._co_applied.get(other_id) == 0:
                    continue
                try:
                    ok = self._apply_co(other_id, 0)
                except Exception as e:
                    self.log_message.emit(
                        f"Hunt: failed to set core {other_id} to stock: {e}. "
                        f"Pausing (SMU issue, not a verdict)."
                    )
                    self.pause()
                    return
                if not ok:
                    self.log_message.emit(
                        f"Hunt: stock write rejected for core {other_id}. "
                        f"Pausing (SMU issue, not a verdict)."
                    )
                    self.pause()
                    return
                self._co_applied[other_id] = 0
            try:
                ok = self._apply_co(core_id, target)
            except Exception as e:
                self.log_message.emit(
                    f"Hunt: failed to set core {core_id} to {target}: {e}. "
                    f"Pausing (SMU issue, not a verdict)."
                )
                self.pause()
                return
            if not ok:
                self.log_message.emit(
                    f"Hunt: CO write rejected for core {core_id} at {target}. "
                    f"Pausing (SMU issue, not a verdict)."
                )
                self.pause()
                return
            self._co_applied[core_id] = target

        if self._session_id is not None:
            tp.set_hunting_core(self._db, self._session_id, core_id)
        cs.current_offset = target
        cs.in_test = True
        tp.save_core_state(self._db, self._session_id, cs)
        self._last_tested_core = core_id
        self._emit_progress()
        self.log_message.emit(
            f"Hunt slot: core {core_id} at {target}, all other cores at stock"
        )
        self._start_worker(core_id, self._config.hunt_slot_seconds, spectrum=True)

    def _end_hunt_fruitless(self) -> None:
        """Every hunt slot passed — the crash stays honestly unattributed."""
        self._hunting = False
        if self._session_id is None:
            return
        tp.set_hunting_core(self._db, self._session_id, None)
        n = tp.get_unattributed_crashes(self._db, self._session_id) + 1
        tp.set_unattributed_crashes(self._db, self._session_id, n)
        if n >= self._config.max_unattributed_crash_hunts:
            self.log_message.emit(
                f"Crash hunt found no culprit ({n} unattributed crash(es) in a "
                f"row). Pausing for your call instead of guessing: check the "
                f"kernel journal around the freeze, consider PSU/memory/"
                f"thermals, or lower max_offset, then Resume."
            )
            self.pause()
            return
        self.log_message.emit(
            "Crash hunt found no culprit — resuming validation; another "
            "unattributed crash will pause for your decision."
        )
        profile = {
            cs.core_id: cs.best_offset
            for cs in self._core_states.values()
            if cs.best_offset is not None
        }
        session = tp.get_session(self._db, self._session_id)
        self._enter_auto_validation(profile, resume_from=session)

    def _on_hunt_slot_finished(
        self, core_id: int, passed: bool, error_type: str, foreign: dict[int, dict]
    ) -> None:
        if self._session_id is not None:
            tp.set_hunting_core(self._db, self._session_id, None)
        if foreign:
            # Other cores are at stock during a hunt — any event on them is a
            # loud non-CO warning, handled (not penalized) by evidence logic.
            self._apply_foreign_evidence(foreign)
        if passed:
            QTimer.singleShot(0, self._run_next_hunt_slot)
            return

        cs = self._core_states.get(core_id)
        if cs is None:
            QTimer.singleShot(0, self._run_next_hunt_slot)
            return
        resident = self._co_applied.get(core_id, cs.current_offset)
        self.log_message.emit(
            f"Crash hunt: core {core_id} FAILED in isolation at {resident} "
            f"({error_type or 'fail'}) — culprit found."
        )
        cs.current_offset = resident
        if error_type == "crash":
            self._apply_crash_penalty(cs)
        else:
            self._apply_crash_penalty(cs, steps=1, count_crash=False)
        tp.save_core_state(self._db, self._session_id, cs)
        self.core_state_changed.emit(cs.core_id, cs.phase, cs.current_offset)
        tp.set_unattributed_crashes(self._db, self._session_id, 0)
        self._hunting = False
        self._set_status("running")
        tp.update_session_status(self._db, self._session_id, "running")
        QTimer.singleShot(0, self._run_next)

    def _quarantine_session(self, streak: int) -> None:
        """Force every core to stock (CO=0) and quarantine the session.

        Reached when the machine has hard-crashed on resume the configured number
        of consecutive times with no surviving test in between — no offset profile
        we can re-apply is safe. Fail closed: drive the SMU to CO=0 (always safe),
        mark the session 'quarantined' so it is neither silently re-applied nor
        offered for resume, and surface an honest unsafe verdict to the user.
        """
        for core_id, cs in self._core_states.items():
            cs.in_test = False
            try:
                if self._apply_co(core_id, 0):
                    self._co_applied[core_id] = 0
            except Exception as e:
                log.warning("Quarantine: failed to force core %d to CO=0: %s", core_id, e)
            if self._session_id is not None:
                tp.save_core_state(self._db, self._session_id, cs)
        if self._session_id is not None:
            tp.update_session_status(self._db, self._session_id, "quarantined")
        self._set_status("quarantined")
        self._emit_progress()
        self.log_message.emit(
            f"QUARANTINED after {streak} consecutive crash-resumes: no re-applied "
            f"CO profile stays stable. All cores forced to stock (CO=0). The last "
            f"offsets are unsafe on this machine — lower max_offset, improve "
            f"cooling, or check BIOS PBO before tuning again."
        )

    def _check_time_budget(self, cs: CoreState) -> bool:
        """Check if core has exceeded its time budget. Returns True if settled."""
        if cs.cumulative_test_time <= self._config.max_core_time_seconds:
            return False
        settled_offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        cs.current_offset = settled_offset
        cs.phase = TunerPhase.CONFIRMED
        cs.backoff_mode = False
        logging.warning(
            "Core %d: time budget exceeded (%.0fs > %ds) — settled at %d",
            cs.core_id, cs.cumulative_test_time, self._config.max_core_time_seconds,
            settled_offset,
        )
        return True

    def _accumulate_test_time(self, cs: CoreState, duration: float) -> None:
        """Add test duration to core's cumulative time (search phases only)."""
        if cs.phase in (
            TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2,
            TunerPhase.HARDENED,
        ):
            return
        cs.cumulative_test_time += duration

    def _is_core_available(self, cs: CoreState) -> bool:
        """Check if core is available for testing (not done, not in cooldown)."""
        if cs.crash_cooldown > 0:
            return False
        return cs.phase not in (TunerPhase.CONFIRMED, TunerPhase.HARDENED)

    def _decrement_cooldowns(self, picked_core: int) -> None:
        """Decrement crash cooldown for all cores except the one being tested."""
        for cs in self._core_states.values():
            if cs.core_id != picked_core and cs.crash_cooldown > 0:
                cs.crash_cooldown -= 1

    def _pick_next_core(self) -> int | None:
        """Select next core to test based on test_order config.

        Returns None if all cores are done (CONFIRMED/HARDENED) or all remaining
        active cores are in crash cooldown. Callers must distinguish these cases
        by checking whether any cooldown cores exist.
        """
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
        """Finish each core completely before moving to the next (pure selector).

        The lowest-id core that is neither done (CONFIRMED/HARDENED) nor in
        cooldown is driven all the way through — including its SETTLED -> CONFIRMING
        step. SETTLED is NOT deferred behind every other core's search (that would
        settle all cores first and confirm them all at the end, which is not
        "finish each core completely").
        """
        for core_id in sorted(self._core_states.keys()):
            cs = self._core_states[core_id]
            if cs.phase not in (TunerPhase.CONFIRMED, TunerPhase.HARDENED) and self._is_core_available(cs):
                return core_id
        return None

    def _pick_round_robin(self) -> int | None:
        """Cycle through all cores, one test each per round (pure selector).

        Rotation is by POSITION, not membership: when the cursor core itself
        just went terminal (or into cooldown), the cycle continues at the next
        higher id instead of snapping back to core 0 — otherwise every
        confirmation would restart the round and starve the high-id cores'
        cool-down fairness.
        """
        active = sorted(
            cid for cid, cs in self._core_states.items()
            if self._is_core_available(cs)
        )
        if not active:
            return None
        if self._last_tested_core is not None:
            after = [c for c in active if c > self._last_tested_core]
            return after[0] if after else active[0]
        return active[0]

    def _pick_weakest_first(self) -> int | None:
        """Prioritize cores closest to settling (pure selector).

        Scoring: lower score = higher priority. Crash history adds penalty
        of crash_count * 2 to deprioritize repeatedly-crashing cores.
        """
        candidates = []
        for core_id, cs in self._core_states.items():
            if not self._is_core_available(cs):
                continue
            base_phase_score = {
                TunerPhase.FINE_SEARCH: 0, TunerPhase.FAILED_CONFIRM: 0,
                TunerPhase.BACKOFF_PRECONFIRM: 0, TunerPhase.BACKOFF_CONFIRMING: 1,
                TunerPhase.CONFIRMING: 1, TunerPhase.COARSE_SEARCH: 2,
                TunerPhase.SETTLED: 3, TunerPhase.NOT_STARTED: 4,
                TunerPhase.HARDENING_T1: 0,
                TunerPhase.HARDENING_T2: 0,
            }.get(cs.phase, 5)
            score = base_phase_score + (cs.crash_count * 2)
            candidates.append((score, core_id))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][1]

    def _pick_ccd_alternating(self) -> int | None:
        """Alternate between CCDs for cross-CCD thermal balance.

        Primary rule is genuine alternation: prefer a CCD different from the one
        just tested so the previously-loaded CCD cools while the other works.
        Fewest-confirmed (then lowest index) is the tie-break among the
        alternation candidates, keeping the CCDs balanced over the run.
        """
        ccd_cores: dict[int, list[int]] = {}
        for core_id, cs in self._core_states.items():
            if not self._is_core_available(cs):
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
            if cs.phase in (TunerPhase.CONFIRMED, TunerPhase.HARDENED):
                ccd_confirmed[ccd] = ccd_confirmed.get(ccd, 0) + 1

        candidate_ccds = sorted(ccd_cores.keys())
        # Alternate away from the last-tested CCD when another CCD still has work.
        if self._last_tested_core is not None:
            last_info = self._topology.cores.get(self._last_tested_core)
            last_ccd = last_info.ccd if last_info and last_info.ccd is not None else 0
            others = [c for c in candidate_ccds if c != last_ccd]
            if others:
                candidate_ccds = others

        target_ccd = min(candidate_ccds, key=lambda c: (ccd_confirmed.get(c, 0), c))
        return ccd_cores[target_ccd][0]

    def _pick_ccd_round_robin(self) -> int | None:
        """Round-robin with CCD interleaving — one test per core, alternating CCDs.

        Order: CCD0[0]→CCD1[0]→CCD0[1]→CCD1[1]→CCD0[2]→CCD1[2]...
        Each core gets cool-down time between tests.
        """
        ccd_cores: dict[int, list[int]] = {}
        for core_id, cs in self._core_states.items():
            if not self._is_core_available(cs):
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

        # Within this CCD, rotate from the last tested POSITION (the cursor
        # core may itself have gone terminal — same rationale as round_robin).
        last_in_ccd = self._ccd_last_tested.get(target_ccd)
        if last_in_ccd is not None:
            after = [c for c in cores if c > last_in_ccd]
            return after[0] if after else cores[0]
        return cores[0]

    def _reconstruct_scheduling_position(self) -> None:
        """Re-derive the round-robin / CCD cycling position from the test log.

        ``_last_tested_core`` and ``_ccd_last_tested`` are in-memory cursors, not
        persisted state — the test log is their source of truth. Rebuilding them on
        resume keeps the cycling order (and its cross-CCD cool-down) continuous
        across a reboot instead of silently restarting from core 0. Synthetic
        crash-recovery rows (duration is NULL) are skipped — they are not real
        tests and must not move the cursor.
        """
        if self._session_id is None:
            return
        real = [
            e for e in tp.get_test_log(self._db, self._session_id)
            if e.get("duration_seconds") is not None
        ]
        if not real:
            return
        self._last_tested_core = real[-1]["core_id"]
        for entry in real:  # ascending by id: the last write per CCD wins
            core_info = self._topology.cores.get(entry["core_id"])
            if core_info and core_info.ccd is not None:
                self._ccd_last_tested[core_info.ccd] = entry["core_id"]

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
        while core_id is None:
            # Distinguish "all done" from "all active cores in cooldown"
            in_cooldown = any(
                cs.crash_cooldown > 0
                and cs.phase not in (TunerPhase.CONFIRMED, TunerPhase.HARDENED)
                for cs in self._core_states.values()
            )
            if not in_cooldown:
                self._complete_session()
                return
            # Drain all cooldowns by 1 and retry the picker
            for cs in self._core_states.values():
                if cs.crash_cooldown > 0:
                    cs.crash_cooldown -= 1
            core_id = self._pick_next_core()

        self._decrement_cooldowns(core_id)
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
        if cs.phase in (
            TunerPhase.CONFIRMING, TunerPhase.BACKOFF_CONFIRMING,
            TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2,
        ):
            duration = self._config.confirm_duration_seconds
        elif cs.phase == TunerPhase.BACKOFF_PRECONFIRM:
            duration = int(self._config.search_duration_seconds * self._config.backoff_preconfirm_multiplier)
        elif self._status == "validating":
            duration = self._config.validate_duration_seconds
        else:
            duration = self._config.search_duration_seconds

        # Run single-core test on a worker thread; a spectrum hardening tier
        # runs the light-load profile instead of sustained stress.
        spectrum = False
        if cs.phase in (TunerPhase.HARDENING_T1, TunerPhase.HARDENING_T2):
            tier = self._config.hardening_tiers[cs.hardening_tier_index]
            spectrum = tier.get("profile") == "spectrum"
        self._start_worker(core_id, duration, spectrum=spectrum)

    def _fail_test_async(self, core_id: int, message: str) -> None:
        """Deliver a start-time failure on a fresh event-loop stack, like the
        worker's queued finished signal — never re-enter _on_test_finished
        synchronously (which would recurse through _run_next on a core that
        always fails to start).
        """
        QTimer.singleShot(
            0, lambda: self._on_test_finished(core_id, False, message, "startup", 0.0, 0.0, "", "")
        )

    def _start_worker(self, core_id: int, duration: int, *, spectrum: bool = False) -> None:
        """Launch a _TunerWorker thread for the given core.

        ``spectrum`` adds the light-load spectrum to the slot (load transitions +
        idle watch) — the load class that exposes max-boost marginality, which
        sustained stress alone cannot reach.
        """
        core_info = self._topology.cores.get(core_id)
        if not core_info:
            self._fail_test_async(core_id, f"Core {core_id} not found")
            return

        cs = self._core_states.get(core_id)
        backend_name, stress_mode_str, fft_preset_str = (
            self._get_active_stress_config(cs)
            if cs is not None
            else (self._config.backend, self._config.stress_mode, self._config.fft_preset)
        )
        from engine.backends.base import FFTPreset, StressMode
        try:
            _stress_mode = StressMode[stress_mode_str.upper()]
        except KeyError:
            _stress_mode = StressMode.SSE
        try:
            _fft_preset = FFTPreset[fft_preset_str.upper()]
        except KeyError:
            _fft_preset = FFTPreset.SMALL

        stress_config = StressConfig(
            mode=_stress_mode,
            fft_preset=_fft_preset,
            threads=len(core_info.logical_cpus),
        )
        scheduler_config = SchedulerConfig(
            seconds_per_core=duration,
            cores_to_test=[core_id],
            stop_on_error=True,
            cycle_count=1,
            max_temperature=self._config.max_temperature_c,
            over_temp_grace_seconds=self._config.over_temp_grace_seconds,
            over_temp_hard_margin=self._config.over_temp_hard_margin_c,
            require_thermal_sensor=not self._config.allow_missing_thermal_sensor,
            variable_load=spectrum,
            variable_load_interval=5.0 if spectrum else 15.0,
            idle_stability_test=15.0 if spectrum else 0.0,
        )

        try:
            backend = self._get_backend_for_name(backend_name)
            scheduler = CoreScheduler(
                topology=self._topology,
                backend=backend,
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=self._work_dir,
            )
        except Exception as e:
            self._fail_test_async(core_id, str(e))
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

    @Slot(int, bool, str, str, float, float, str, str)
    def _on_test_finished(
        self,
        core_id: int,
        passed: bool,
        error_msg: str,
        error_type: str,
        duration: float,
        peak_stretch_pct: float,
        mce_json: str = "",
        results_json: str = "",
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
        # A validation worker marks its whole stressed set in_test; the box
        # survived this result, so clear and persist all of them (not just the
        # reported core) before advancing.
        self._clear_cores_under_stress()

        # Kernel events observed during the test that name OTHER cores are
        # evidence about those cores, independent of this test's verdict.
        foreign = self._foreign_mce_by_core(
            -1 if self._soaking else core_id, mce_json
        )

        # A start-time/environment failure (missing binary, scheduler
        # construction error, harness exception) is not a stability verdict —
        # and nothing RAN, so it proves nothing about the resident offsets:
        # it must be handled BEFORE the journal is marked survived or the
        # crash-resume streak is reset. Persist the cleared in_test flag and
        # revert the never-tested offset, then pause with the reason.
        if not passed and error_type == "startup":
            if self._session_id:
                tp.save_core_state(self._db, self._session_id, cs)
            if self._status == "validating":
                self._revert_all_to_baseline()
            else:
                self._revert_core_to_baseline(core_id)
            self.log_message.emit(
                f"Core {core_id}: test could not run — {error_msg}. "
                f"Pausing (environment issue, not a stability verdict)."
            )
            self.pause()
            return

        # Reaching this handler proves the machine survived the test — a hard
        # system crash would have killed the process before the worker's finished
        # signal could be delivered. So every CO value resident during this test
        # is now proven not-a-hard-crash: mark the journal survived, widen the
        # proven-safe envelope, and reset the resume-crash circuit breaker because
        # forward progress was made. (Holds for thermal stops and detected stress
        # failures too — both mean the box lived.)
        if self._session_id is not None:
            # Cores the kernel just named stay un-survived: surviving the test
            # does not clear an error the hardware reported minutes ago. A
            # machine check that names NO core taints every resident value —
            # fail closed and leave the whole set unproven for this test.
            if _has_unattributed_mce(mce_json):
                self.log_message.emit(
                    "Machine check without core attribution observed during the "
                    "test — resident offsets stay unproven for this run."
                )
            else:
                tp.journal_mark_survived(
                    self._db, self._session_id, exclude_cores=sorted(foreign)
                )
                for c, v in tp.journal_survived_values(self._db, self._session_id).items():
                    if self._is_more_aggressive(v, self._co_survived.get(c, 0)):
                        self._co_survived[c] = v
            if tp.get_resume_crash_streak(self._db, self._session_id) != 0:
                tp.set_resume_crash_streak(self._db, self._session_id, 0)

        # A thermal stop is not a stability verdict — advancing the state machine
        # or logging a fail here would push the offset the wrong way on a thermal
        # transient. Cool down and retry. Handled for the search flow,
        # validation, and hunt slots alike.
        if not passed and error_type == "thermal":
            if self._hunting:
                self._validation_thermal_aborts += 1
                if self._validation_thermal_aborts > self._config.max_thermal_retries:
                    self.log_message.emit(
                        "Crash hunt: thermal limit hit repeatedly — cooling cannot "
                        "sustain hunting. Fix cooling, then Resume."
                    )
                    self.abort()
                    return
                self._hunt_queue.insert(0, core_id)
                if self._session_id is not None:
                    tp.set_hunting_core(self._db, self._session_id, None)
                self.log_message.emit(
                    f"Crash hunt: core {core_id} thermal stop — cooling down, "
                    f"retrying the same slot"
                )
                QTimer.singleShot(
                    int(self._config.thermal_cooldown_seconds * 1000),
                    self._run_next_hunt_slot,
                )
            elif self._validation_stage == 0:
                self._handle_thermal_abort(core_id, cs, duration)
            else:
                self._handle_validation_thermal_abort(core_id)
            return

        # An apparatus fault is not a stability verdict: a stall means the load
        # never ran on the core, an external kill means something else stopped
        # the process, an unattributable machine check names nobody. Moving a
        # CO offset on any of them punishes an innocent core (85 back-offs in
        # one night came through the stall path). Retry without a verdict,
        # bounded, then stop honestly.
        if not passed and error_type in ("stall", "killed", "mce_unattributed"):
            self._handle_apparatus_fault(core_id, error_msg, error_type, foreign)
            return

        # Reached only on a non-thermal outcome → the thermal-retry streak for
        # this core is broken; reset so the cap counts CONSECUTIVE thermal stops
        # at one offset, not lifetime thermals across the whole search.
        cs.thermal_aborts = 0
        self._apparatus_fault_streak = 0

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
        if self._hunting:
            log_phase = "hunt"
        elif self._status == "validating" and self._validation_stage > 0:
            log_phase = f"validate_s{self._validation_stage}"
        else:
            log_phase = phase_map.get(cs.phase, "validate" if self._status == "validating" else cs.phase)

        # Log to DB (soak is a session-level watch, not one core's test — its
        # record is the narrative plus any mce_evidence rows)
        if self._session_id and not self._soaking:
            backend, stress_mode, fft_preset = self._get_active_stress_config(cs)
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
                backend=backend,
                stress_mode=stress_mode,
                fft_preset=fft_preset,
                peak_stretch_pct=peak_stretch_pct if peak_stretch_pct > 0 else None,
            )

        if results_json and self._session_id and self._validation_stage in (2, 3):
            self._log_parallel_rows(core_id, results_json, log_phase)

        status_str = "PASS" if passed else "FAIL"
        stretch_info = f" stretch:{peak_stretch_pct:.1f}%" if peak_stretch_pct > 0 else ""
        self.log_message.emit(
            f"Core {core_id} offset {cs.current_offset}: {status_str}{stretch_info}"
            + (f" ({error_msg})" if error_msg else "")
        )
        self.test_completed.emit(core_id, cs.current_offset, passed)

        # Revert tested core to baseline — no aggressive offset should linger.
        # Skip during validation (all confirmed offsets stay applied) and during
        # a hunt (the next slot manages the whole CO vector itself; a baseline
        # write here would put an unproven BIOS value back mid-hunt).
        if self._status not in ("validating", "hunting") and not self._revert_core_to_baseline(core_id):
            self.log_message.emit(
                f"Core {core_id}: test offset is still resident because the SMU "
                f"revert failed. Pausing (hardware-state fault, not a verdict)."
            )
            self.pause()
            return

        # Reset consecutive failure counter on any pass
        if passed:
            self._consecutive_start_failures = 0

        # Physically implausible fail streaks mean the APPARATUS is lying,
        # not the silicon — recover from evidence and stop before the state
        # machine walks proven offsets away (the stale-results.txt class).
        # Search flow only: validation failures are legitimate consecutive
        # backoffs, hunt fails are single by design, and isolation passes are
        # not valid evidence for the all-offsets-live context.
        if (
            not passed
            and self._validation_stage == 0
            and not self._hunting
            and self._apparatus_suspect(core_id)
        ):
            return

        # Hunt slots have their own flow — a fail here is a FOUND CULPRIT.
        if self._hunting:
            self._on_hunt_slot_finished(core_id, passed, error_type, foreign)
            return

        if self._soaking:
            self._soaking = False
            if foreign:
                self._apply_foreign_evidence(foreign)
                self._validation_dirty = True
                self._save_validation_pos()
                self.log_message.emit(
                    "Soak found hardware evidence — leaving validation; the "
                    "named core(s) re-earn confirmation first."
                )
                self._validation_stage_exit_to_search()
                return
            if passed:
                self.log_message.emit("Real-world soak passed — no kernel events")
                self._validation_stage = 7
                self._save_validation_pos()
                QTimer.singleShot(0, self._run_validation_next)
                return
            QTimer.singleShot(0, self._run_validation_next)
            return

        # Hardware evidence about OTHER cores outranks the normal flow: demote
        # the named cores, and if validation was running leave it so they
        # re-earn confirmation first (validation restarts once all are back).
        if foreign:
            self._apply_foreign_evidence(foreign)
            if self._validation_stage > 0:
                self.log_message.emit(
                    "Leaving validation: kernel evidence named other core(s); "
                    "they must re-earn confirmation, then validation restarts."
                )
                self._validation_stage = 0
                self._set_status("running")
                if self._session_id:
                    tp.update_session_status(self._db, self._session_id, "running")
                QTimer.singleShot(0, self._run_next)
                return

        # Multi-core validation uses its own flow — don't advance per-core state machine
        if self._validation_stage > 0:
            self._on_validation_test_finished(core_id, passed)
            return

        cs = self._core_states[core_id]
        self._accumulate_test_time(cs, duration)
        if self._check_time_budget(cs):
            tp.save_core_state(self._db, self._session_id, cs)
            QTimer.singleShot(0, self._run_next)
            return

        # Advance state machine
        self._advance_core(core_id, passed)

        # Continue with the next test on a fresh event-loop stack (matches the
        # validation path) so a synchronous start failure cannot recurse back
        # into _on_test_finished.
        QTimer.singleShot(0, self._run_next)

    def _log_parallel_rows(self, reported: int, results_json: str, phase: str) -> None:
        """Record every lane's verdict from a simultaneous stage, not only the
        reported core's. Fail closed: a malformed payload records nothing."""
        try:
            entries = json.loads(results_json)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(entries, list):
            return
        backend, stress_mode, fft_preset = (
            self._config.backend, self._config.stress_mode, self._config.fft_preset,
        )
        for e in entries:
            if not isinstance(e, dict):
                continue
            core = e.get("core")
            if not isinstance(core, int) or core == reported:
                continue
            cs = self._core_states.get(core)
            if cs is None:
                continue
            offset = self._co_applied.get(core)
            if offset is None:
                offset = cs.best_offset if cs.best_offset is not None else cs.current_offset
            duration = e.get("duration")
            tp.log_test_result(
                self._db, self._session_id, core, offset, phase,
                bool(e.get("passed")),
                error_msg=e.get("error_message"),
                error_type=e.get("error_type"),
                duration=float(duration) if isinstance(duration, (int, float)) else None,
                backend=backend, stress_mode=stress_mode, fft_preset=fft_preset,
            )

    def _handle_thermal_abort(self, core_id: int, cs: CoreState, duration: float) -> None:
        """Handle a test stopped by the thermal safety limit (not instability).

        Stopping on temperature says nothing about CO stability, so advancing the
        state machine or logging a fail would push the offset the wrong way on a
        thermal transient. Instead: revert the core, defer it (cool down while
        other cores test), and retry the SAME offset. If a core keeps hitting the
        limit, cooling cannot sustain the test — abort with a clear message rather
        than silently producing a bad tune.
        """
        cs.thermal_aborts += 1
        if not self._revert_core_to_baseline(core_id):
            self.log_message.emit(
                f"Core {core_id}: SMU revert failed during thermal handling. "
                f"Pausing (hardware-state fault)."
            )
            self.pause()
            return
        # The partial test still ran real seconds — count them so a thermal loop
        # is bounded by the per-core time budget too, not only the retry cap.
        self._accumulate_test_time(cs, duration)

        if cs.thermal_aborts > self._config.max_thermal_retries:
            self.log_message.emit(
                f"Core {core_id}: thermal limit hit {cs.thermal_aborts} times at "
                f"offset {cs.current_offset} — cooling cannot sustain testing. "
                f"Lower load/ambient or improve cooling, then resume."
            )
            log.warning(
                "Aborting tune: core %d hit thermal limit %d times (max_thermal_retries=%d)",
                core_id,
                cs.thermal_aborts,
                self._config.max_thermal_retries,
            )
            self.abort()
            return

        cs.crash_cooldown = max(cs.crash_cooldown, 2)  # prefer other cores meanwhile
        self.log_message.emit(
            f"Core {core_id} offset {cs.current_offset}: thermal abort "
            f"({cs.thermal_aborts}/{self._config.max_thermal_retries}) — cooling "
            f"down, will retry same offset"
        )
        tp.save_core_state(self._db, self._session_id, cs)
        # Real wall-clock cooldown before retrying the same offset: crash_cooldown
        # is only a pick-counter and gives no cooling when this is the last active
        # core. QTimer also breaks the _on_test_finished call stack (re-entrancy).
        QTimer.singleShot(
            int(self._config.thermal_cooldown_seconds * 1000), self._run_next
        )

    def _handle_validation_thermal_abort(self, core_id: int) -> None:
        """A thermal stop during validation is not a stability verdict.

        Backing off a confirmed core and restarting validation (the default
        fail path) on a thermal transient would degrade the tune. Instead: cool
        down and re-run the same validation stage. If the limit keeps tripping,
        cooling cannot sustain the test — abort with a clear message.
        """
        self._validation_thermal_aborts += 1
        if self._validation_thermal_aborts > self._config.max_thermal_retries:
            self.log_message.emit(
                "Validation: thermal limit hit repeatedly — cooling cannot "
                "sustain testing. Lower load/ambient or improve cooling, then resume."
            )
            log.warning(
                "Aborting validation: thermal limit hit %d times",
                self._validation_thermal_aborts,
            )
            self.abort()
            return
        self.log_message.emit(
            f"Validation: core {core_id} thermal abort "
            f"({self._validation_thermal_aborts}/{self._config.max_thermal_retries}) "
            f"— cooling down, re-running the same step"
        )
        QTimer.singleShot(
            int(self._config.thermal_cooldown_seconds * 1000),
            self._run_validation_requeue if self._in_requeue else self._run_validation_next,
        )

    def _handle_apparatus_fault(
        self, core_id: int, error_msg: str, error_type: str, foreign: dict[int, dict]
    ) -> None:
        """Retry the current step after a fault that proves nothing about the
        silicon; after max_apparatus_retries consecutive faults, stop honestly.

        No CO offset moves here: the search bounds, validation back-offs and
        crash penalties all stay untouched. Hardware evidence about OTHER
        cores that arrived with the fault is still applied — evidence outranks
        the retry.
        """
        self._soaking = False
        if foreign:
            self._apply_foreign_evidence(foreign)
            if self._validation_stage > 0:
                self.log_message.emit(
                    "Leaving validation: kernel evidence named other core(s); "
                    "they must re-earn confirmation, then validation restarts."
                )
                self._validation_stage = 0
                self._set_status("running")
                if self._session_id:
                    tp.update_session_status(self._db, self._session_id, "running")
                QTimer.singleShot(0, self._run_next)
                return

        self._apparatus_fault_streak += 1
        limit = self._config.max_apparatus_retries
        if self._apparatus_fault_streak > limit:
            self.log_message.emit(
                f"Stress apparatus failed {self._apparatus_fault_streak} times in "
                f"a row ({error_type}: {error_msg}) — the environment cannot run "
                f"this test, and repeating it would prove nothing. Stopping with "
                f"offsets reverted to baseline; fix the cause (backend install, "
                f"foreign load, permissions), then Resume."
            )
            self.abort()
            return

        self.log_message.emit(
            f"Core {core_id}: apparatus fault ({error_msg}) — retrying the same "
            f"step without a verdict "
            f"({self._apparatus_fault_streak}/{limit})"
        )
        if self._hunting:
            self._hunt_queue.insert(0, core_id)
            if self._session_id is not None:
                tp.set_hunting_core(self._db, self._session_id, None)
            QTimer.singleShot(0, self._run_next_hunt_slot)
        elif self._validation_stage > 0:
            QTimer.singleShot(
                0,
                self._run_validation_requeue if self._in_requeue
                else self._run_validation_next,
            )
        else:
            if not self._revert_core_to_baseline(core_id):
                self.log_message.emit(
                    f"Core {core_id}: test offset is still resident because the "
                    f"SMU revert failed. Pausing (hardware-state fault)."
                )
                self.pause()
                return
            QTimer.singleShot(0, self._run_next)

    def _complete_session(self) -> None:
        """All cores done — enter auto-validation or finalize session."""
        # With hardening configured, a core can reach CONFIRMED via a settling path
        # that skips the hardening entry — the smart-backoff convergence, the
        # crash-penalty backoff, or the time-budget cutoff all set CONFIRMED
        # directly (unlike the normal CONFIRMING -> HARDENING_T1 transition). Left
        # as-is, such a core is never picked again (CONFIRMED is unavailable) yet
        # never HARDENED, so the all-hardened gate below never opens and the tuner
        # stalls in "running" forever with no worker scheduled. Promote any
        # confirmed-but-not-hardened core into hardening and keep going. Hardening
        # only ever exits to HARDENED (never back to CONFIRMED), so this converges.
        if self._config.hardening_tiers:
            promoted = [
                cs for cs in self._core_states.values()
                if cs.phase == TunerPhase.CONFIRMED
            ]
            for cs in promoted:
                cs.phase = TunerPhase.HARDENING_T1
                cs.hardening_tier_index = 0
                if self._session_id is not None:
                    tp.save_core_state(self._db, self._session_id, cs)
            if promoted:
                self.log_message.emit(
                    f"Promoting {len(promoted)} confirmed core(s) to hardening"
                )
                QTimer.singleShot(0, self._run_next)
                return

        profile = {}
        for cs in self._core_states.values():
            if cs.best_offset is not None:
                profile[cs.core_id] = cs.best_offset

        # Gate: ensure all cores have reached a terminal phase.
        # With hardening tiers: cores must be HARDENED (confirmed + hardened stress).
        # Without tiers: CONFIRMED is terminal; HARDENED also accepted (belt + braces).
        done_phases = {TunerPhase.CONFIRMED, TunerPhase.HARDENED}
        if self._config.hardening_tiers:
            done_phases = {TunerPhase.HARDENED}
        all_done = all(cs.phase in done_phases for cs in self._core_states.values())
        if not all_done:
            return

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
            session = (
                tp.get_session(self._db, self._session_id)
                if self._session_id is not None else None
            )
            self._enter_auto_validation(profile, resume_from=session)
            return

        self._finalize_session(profile)

    def _finalize_session(self, profile: dict[int, int]) -> None:
        """Apply confirmed profile to SMU and emit completion."""
        if self._validation_dirty:
            # Invariant: DONE requires one final clean pass. The legit flow
            # (_run_validation_next's finalize sentinel) redirects a dirty pass
            # before ever calling here — reaching this guard is a bug upstream,
            # and completing anyway would publish an unproven profile.
            self.log_message.emit(
                "Refusing to declare completion: a final clean validation pass "
                "is still owed. Reverting to baselines and pausing — Resume to "
                "run the owed pass."
            )
            self._revert_all_to_baseline()
            self._save_validation_pos()
            self.pause()
            return
        if self._smu is not None and profile:
            failed: list[int] = []
            for core_id, offset in profile.items():
                try:
                    success = self._apply_co(core_id, offset)
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
            # A full clean pass just proved the profile; older unexplained
            # incidents are stale evidence and must not haunt the next resume.
            tp.set_unattributed_crashes(self._db, self._session_id, 0)

        self._validation_stage = 0
        self._validation_requeue = []
        self._save_validation_pos()
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

    def _enter_auto_validation(self, profile: dict[int, int], resume_from=None) -> None:
        """Begin or CONTINUE the multi-core validation sequence.

        Stage 1: Per-core with all offsets live — stress each core individually
                 while all other cores hold their confirmed offsets.
        Stage 2: All-core coverage with all offsets applied.
        Stage 3: Half-core load — half tested / half idle, rotating.

        ``resume_from`` (a TunerSession) restores the persisted cursor so a
        reboot, app restart, or a search interlude after a penalty continues
        where validation was — never a full stage-1 restart. Cores whose
        current best has no logged stage-1 pass (their offset changed since)
        are requeued for a solo re-test first; every other core's coverage is
        still valid — raising one core's voltage cannot destabilize others.
        """
        self._set_status("validating")
        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "validating")

        # Stage-1 order is deterministic (sorted), so a restored index means
        # the same cores; halves are CCD-split (or even/odd), also stable.
        self._validation_core_order = sorted(profile.keys())
        self._validation_halves = [
            h for h in self._split_cores_into_halves(profile) if h
        ]
        self._validation_thermal_aborts = 0

        if resume_from is not None and resume_from.validation_stage > 0:
            self._validation_stage = min(resume_from.validation_stage, 5)
            self._validation_core_index = max(
                0, min(resume_from.validation_index, len(self._validation_core_order))
            )
            self._validation_half_index = max(
                0, min(resume_from.validation_half, len(self._validation_halves))
            )
            self._validation_dirty = resume_from.validation_dirty
            try:
                raw = json.loads(resume_from.validation_requeue)
            except (json.JSONDecodeError, TypeError):
                raw = []  # fail closed: a corrupt cursor loses only the hint
            requeue = [
                c for c in raw
                if isinstance(c, int) and c in self._core_states
            ] if isinstance(raw, list) else []
            if self._validation_stage >= 2:
                for c in self._validation_core_order:
                    if c not in requeue and not self._has_stage1_pass_at_current_best(c):
                        requeue.append(c)
            self._validation_requeue = requeue
            self.log_message.emit(
                f"Continuing validation at stage {self._validation_stage} "
                f"(position preserved; {len(requeue)} core(s) owe a solo re-test)"
            )
            self._save_validation_pos()
            if requeue:
                self._run_validation_requeue()
            else:
                self._run_validation_next()
            return

        self._validation_core_index = 0
        self._validation_half_index = 0
        self._validation_stage = 1
        self._validation_dirty = False
        self._validation_requeue = []
        self.log_message.emit("Validation stage 1: per-core with all offsets live")
        self.validation_progress.emit(1, 0, len(self._validation_core_order))
        self._save_validation_pos()
        self._run_validation_next()

    def _save_validation_pos(self) -> None:
        """Persist the validation cursor after every transition, so progress
        survives power loss and app restarts alike."""
        if self._session_id is None:
            return
        tp.set_validation_position(
            self._db,
            self._session_id,
            self._validation_stage,
            self._validation_core_index,
            self._validation_half_index,
            self._validation_dirty,
            json.dumps(self._validation_requeue),
        )
        log.debug(
            "validation cursor: stage=%d index=%d half=%d dirty=%s requeue=%s",
            self._validation_stage, self._validation_core_index,
            self._validation_half_index, self._validation_dirty,
            self._validation_requeue,
        )

    def _has_stage1_pass_at_current_best(self, core_id: int) -> bool:
        """True when the test log holds a real stage-1 pass at the core's
        CURRENT best offset — the evidence a solo re-test would reproduce."""
        if self._session_id is None:
            return False
        cs = self._core_states.get(core_id)
        if cs is None or cs.best_offset is None:
            return False
        for r in tp.get_test_log(self._db, self._session_id, core_id=core_id):
            if (
                r.get("phase") == "validate_s1"
                and r.get("passed")
                and r.get("offset_tested") == cs.best_offset
                and r.get("duration_seconds") is not None
            ):
                return True
        return False

    def _run_validation_requeue(self) -> None:
        """Solo re-test (all offsets live) for cores whose offset changed —
        the only coverage a one-core back-off invalidates. When the queue
        drains, the pending stage reruns."""
        if self._abort_requested or self._paused:
            return
        if not self._validation_requeue:
            self._in_requeue = False
            self._save_validation_pos()
            QTimer.singleShot(0, self._run_validation_next)
            return
        self._in_requeue = True
        core_id = self._validation_requeue[0]
        cs = self._core_states[core_id]
        offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        self.log_message.emit(
            f"Validation re-test: core {core_id} solo at {offset} "
            f"({len(self._validation_requeue)} owed), then stage "
            f"{self._validation_stage} reruns"
        )
        if self._smu is not None and not self._apply_validation_offsets(core_id, offset):
            return
        self._last_tested_core = core_id
        self._mark_cores_under_stress([core_id])
        self._start_worker(core_id, self._config.validate_duration_seconds)

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

    def _get_validation_stage_count(self) -> int:
        """Total enabled validation stages (3 base + transitions/spectrum/soak)."""
        return (
            3
            + int(self._config.validate_transitions)
            + int(self._config.validate_spectrum)
            + int(self._config.validate_soak)
        )

    def _run_validation_stage4(self) -> None:
        """S4: Rapid transition stress — all cores, load/idle cycling.

        Runs rapid load/idle transitions on all confirmed cores using the
        scheduler's run_rapid_transitions(). This catches instability during
        idle↔boost transitions that sustained stress tests miss.
        """
        cores = self._validation_core_order
        self.log_message.emit(
            f"Validation stage 4: rapid load/idle transitions on {len(cores)} cores"
        )
        self.validation_progress.emit(4, 0, 1)

        # Apply all confirmed offsets
        if self._smu is not None:
            first_core = cores[0]
            cs = self._core_states[first_core]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first_core, offset):
                return

        # Build scheduler for rapid transitions
        stress_config = StressConfig(
            mode=self._get_stress_mode(),
            fft_preset=self._get_fft_preset(),
            threads=0,
        )
        scheduler_config = SchedulerConfig(
            seconds_per_core=self._config.validate_duration_seconds,
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
            self._fail_test_async(cores[0], str(e))
            return

        self._last_tested_core = cores[0]
        self._mark_cores_under_stress(cores)
        core_info = self._topology.cores.get(cores[0])
        logical_cpu = (
            core_info.logical_cpus[0]
            if core_info and core_info.logical_cpus
            else cores[0]
        )
        self._worker = _RapidTransitionWorker(
            cores[0],
            logical_cpu,
            scheduler,
            cores,
            float(self._config.validate_duration_seconds),
            parent=self,
        )
        self._worker.finished.connect(self._on_test_finished)
        self._worker.start()

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
            case 4:
                if self._config.validate_transitions:
                    self._run_validation_stage4()
                else:
                    self._validation_stage = 5
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
            case 5:
                if self._config.validate_spectrum:
                    self._run_validation_stage5()
                else:
                    self._validation_stage = 6
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
            case 6:
                # A dirty pass skips the soak; the final clean pass earns it.
                if self._config.validate_soak and not self._validation_dirty:
                    self._run_validation_soak()
                else:
                    self._validation_stage = 7
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
            case _:
                # All stages complete. If any back-off happened along the way,
                # the profile changed mid-pass — run ONE final complete pass
                # that must come through clean before DONE is declared.
                if self._validation_dirty:
                    self.log_message.emit(
                        "All stages passed, but cores were backed off along the "
                        "way — running one final clean validation pass to prove "
                        "the finished profile."
                    )
                    self._validation_dirty = False
                    self._validation_stage = 1
                    self._validation_core_index = 0
                    self._validation_half_index = 0
                    self._validation_requeue = []
                    self._save_validation_pos()
                    QTimer.singleShot(0, self._run_validation_next)
                    return
                profile = {
                    cs.core_id: cs.best_offset
                    for cs in self._core_states.values()
                    if cs.best_offset is not None
                }
                self.log_message.emit(
                    "All validation stages passed in one clean pass"
                )
                self._validation_core_index = 0
                self._validation_half_index = 0
                self._finalize_session(profile)

    def _run_validation_stage1(self) -> None:
        """Stage 1: test each core individually with all offsets applied."""
        if self._validation_core_index >= len(self._validation_core_order):
            # Stage 1 complete — advance to stage 2
            self._validation_stage = 2
            self._save_validation_pos()
            self.log_message.emit(
                "Validation stage 1 passed — stage 2: all-core coverage"
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
        if self._smu is not None and not self._apply_validation_offsets(core_id, offset):
            return

        self._last_tested_core = core_id
        self._mark_cores_under_stress([core_id])
        self._start_worker(core_id, self._config.validate_duration_seconds)

    def _run_validation_stage2(self) -> None:
        """Stage 2: all cores stressed simultaneously — full package power
        draw, one pinned process per core, per-core verdicts."""
        cores = self._validation_core_order
        self.log_message.emit(
            f"Validation stage 2: stressing all {len(cores)} cores "
            f"simultaneously ({self._config.validate_duration_seconds}s, "
            f"all offsets applied)"
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
        self._mark_cores_under_stress(cores)
        self._start_multi_core_worker(cores, self._config.validate_duration_seconds)

    def _run_validation_stage3(self) -> None:
        """Stage 3: alternating half-core load — catches voltage transients."""
        if self._validation_half_index >= len(self._validation_halves):
            # Stage 3 complete — advance to S4 (rapid transitions) or finalize
            self._validation_stage = 4
            self._save_validation_pos()
            self.log_message.emit("Validation stage 3 passed")
            QTimer.singleShot(0, self._run_validation_next)
            return

        half = self._validation_halves[self._validation_half_index]
        half_label = "A" if self._validation_half_index == 0 else "B"
        self.log_message.emit(
            f"Validation stage 3{half_label}: cores {half} loaded "
            f"simultaneously, the other half idle at their offsets"
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
        self._mark_cores_under_stress(half)
        self._start_multi_core_worker(half, self._config.validate_duration_seconds)

    def _run_validation_stage5(self) -> None:
        """Stage 5: per-core light-load spectrum with all offsets live —
        max-boost bursts, load transitions and idle watch."""
        order = self._validation_core_order
        if self._validation_core_index >= len(order):
            self._validation_stage = 6
            self._validation_core_index = 0
            self._save_validation_pos()
            self.log_message.emit("Validation stage 5 passed")
            QTimer.singleShot(0, self._run_validation_next)
            return
        core_id = order[self._validation_core_index]
        cs = self._core_states[core_id]
        offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
        self.log_message.emit(
            f"Validation 5/{len(order)}: core {core_id} spectrum at {offset} "
            f"(bursts + transitions + idle, all offsets live)"
        )
        self.validation_progress.emit(5, self._validation_core_index, len(order))
        if self._smu is not None and not self._apply_validation_offsets(core_id, offset):
            return
        self._last_tested_core = core_id
        self._mark_cores_under_stress([core_id])
        self._start_worker(core_id, self._config.spectrum_slot_seconds, spectrum=True)

    def _run_validation_soak(self) -> None:
        """Stage 6: no synthetic load — watch the kernel error stream while
        the machine is used normally. Zero events proves the profile."""
        cores = self._validation_core_order
        self.log_message.emit(
            f"Validation stage 6: real-world soak — watching the kernel error "
            f"stream for {self._config.soak_duration_seconds}s with no synthetic "
            f"load. Use the machine normally; any hardware whisper fails it."
        )
        self.validation_progress.emit(6, 0, 1)
        if self._smu is not None and cores:
            first = cores[0]
            cs = self._core_states[first]
            offset = cs.best_offset if cs.best_offset is not None else cs.baseline_offset
            if not self._apply_validation_offsets(first, offset):
                return
        self._soaking = True
        self._mark_cores_under_stress(cores)
        self._last_tested_core = cores[0] if cores else None
        self._worker = _SoakWorker(
            cores[0] if cores else 0, self._config.soak_duration_seconds, parent=self
        )
        self._worker.finished.connect(self._on_test_finished)
        self._worker.start()

    def _start_multi_core_worker(self, cores: list[int], duration: int) -> None:
        """Launch every core's stress process simultaneously (one pinned
        process per core) with per-core verdicts; the worker reports the
        first failing core, else the first core's pass."""
        stress_config = StressConfig(
            mode=self._get_stress_mode(),
            fft_preset=self._get_fft_preset(),
            threads=0,  # per-lane SMT width is set by the runner
        )
        scheduler_config = SchedulerConfig(
            seconds_per_core=duration,
            cores_to_test=cores,
            stop_on_error=True,
            cycle_count=1,
            max_temperature=self._config.max_temperature_c,
            over_temp_grace_seconds=self._config.over_temp_grace_seconds,
            over_temp_hard_margin=self._config.over_temp_hard_margin_c,
            require_thermal_sensor=not self._config.allow_missing_thermal_sensor,
        )

        try:
            runner = ParallelStress(
                topology=self._topology,
                backend=self._backend,
                stress_config=stress_config,
                scheduler_config=scheduler_config,
                work_dir=self._work_dir,
            )
        except Exception as e:
            self._fail_test_async(cores[0], str(e))
            return

        core_info = self._topology.cores.get(cores[0])
        logical_cpu = core_info.logical_cpus[0] if core_info and core_info.logical_cpus else cores[0]
        self._worker = _ParallelWorker(cores[0], logical_cpu, runner, parent=self)
        self._worker.finished.connect(self._on_test_finished)
        self._worker.start()

    def _mark_cores_under_stress(self, cores: list[int]) -> None:
        """Flag every core a validation worker is about to stress as in_test and
        persist it BEFORE the worker starts.

        A hard crash during validation completes no test, and the confirmed offsets
        validation re-applies are journaled ``survived`` — so neither resume crash
        detector would see it and the quarantine breaker would never engage,
        re-applying the same profile into the same crash forever. The in_test flag
        is the one signal that attributes such a crash, so set it on the full
        stressed set (a multi-core stage reports only its first core).
        """
        self._cores_under_stress = list(cores)
        marked = False
        for core_id in cores:
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            cs.in_test = True
            if self._session_id is not None:
                tp.save_core_state(self._db, self._session_id, cs)
                marked = True
        if marked:
            # The mark is the ONE signal that attributes a hard crash during
            # validation; a WAL commit alone can be lost to a freeze (the CO
            # journal's checkpoint runs BEFORE this write, so everything up to
            # it survives while the mark evaporates — observed live). Force it
            # to disk before any stress starts.
            tp.checkpoint(self._db)

    def _clear_cores_under_stress(self) -> None:
        """Clear and persist in_test for every core marked under validation stress.

        Reaching a test result (or an abort) proves the box survived, so the whole
        stressed set must be cleared — not just the reported core (_on_test_finished
        clears that one) — or a normal completion leaves a stale in_test that would
        wrongly fire the breaker on a later resume.
        """
        for core_id in self._cores_under_stress:
            cs = self._core_states.get(core_id)
            if cs is None:
                continue
            # Persist unconditionally: _on_test_finished clears the reported core's
            # flag in memory before calling this, so a guard on cs.in_test would skip
            # persisting that core and leave its DB row in_test=True.
            cs.in_test = False
            if self._session_id is not None:
                tp.save_core_state(self._db, self._session_id, cs)
        self._cores_under_stress = []

    def _find_most_aggressive_core(self) -> int | None:
        """Find the confirmed core with the highest absolute offset that can be backed off.

        Skips cores already at their baseline_offset (nothing to give).
        """
        best_core = None
        best_abs = -1
        for cs in self._core_states.values():
            if (
                cs.best_offset is not None
                and cs.best_offset != cs.baseline_offset
                and abs(cs.best_offset) > best_abs
            ):
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
        """Handle test result during multi-core validation stages.

        A back-off costs one solo re-test plus a rerun of the failed stage —
        never a full restart: raising one core's voltage cannot destabilize
        the others, so their existing coverage stays valid. The dirty flag
        remembers that back-offs happened; DONE still requires one final
        complete pass with zero back-offs (see the finalize sentinel).
        """
        if self._in_requeue:
            if passed:
                self._validation_thermal_aborts = 0
                if core_id in self._validation_requeue:
                    self._validation_requeue.remove(core_id)
                self._save_validation_pos()
                QTimer.singleShot(0, self._run_validation_requeue)
                return
            self._validation_dirty = True
            if not self._backoff_core(core_id):
                self._finalize_exhausted()
                return
            self._save_validation_pos()
            self.log_message.emit(
                f"Validation re-test: core {core_id} failed again — backed off "
                f"one more step, retrying its solo slot"
            )
            QTimer.singleShot(0, self._run_validation_requeue)
            return

        if passed:
            self._validation_thermal_aborts = 0  # streak broken by a clean pass
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
                case 4:
                    self._validation_stage = 5
                    self._validation_core_index = 0
                case 5:
                    self._validation_core_index += 1
            self._save_validation_pos()
            # Use QTimer to break the call stack (this is called from _on_test_finished)
            QTimer.singleShot(0, self._run_validation_next)
            return

        # Validation failure — back off the FAILING core. Stages 2/3 report
        # the failing core directly (per-core verdicts); stage 4's rapid
        # transition worker only knows the batch, so fall back to the most
        # aggressive offset there.
        target: int | None = None
        match self._validation_stage:
            case 1 | 2 | 3 | 5:
                target = core_id
            case 4:
                target = self._find_most_aggressive_core()

        if target is None or not self._backoff_core(target):
            self._finalize_exhausted()
            return

        self._validation_dirty = True
        if self._validation_stage in (1, 5):
            # The failed slot simply retries at the new offset — the cursor
            # has not advanced, and nobody else's coverage changed.
            self.log_message.emit(
                f"Validation stage {self._validation_stage}: core {target} backed "
                f"off — retrying its slot (position kept)"
            )
            self._save_validation_pos()
            QTimer.singleShot(0, self._run_validation_next)
            return

        self._validation_requeue = [target]
        self.log_message.emit(
            f"Validation stage {self._validation_stage} failed — core {target} "
            f"backed off; solo re-test, then stage {self._validation_stage} reruns"
        )
        self._save_validation_pos()
        QTimer.singleShot(0, self._run_validation_requeue)

    def _validation_stage_exit_to_search(self) -> None:
        """Leave validation so demoted cores re-earn; the cursor stays put."""
        self._set_status("running")
        if self._session_id:
            tp.update_session_status(self._db, self._session_id, "running")
        QTimer.singleShot(0, self._run_next)

    def _finalize_exhausted(self) -> None:
        """Validation failed with nothing left to back off — fail closed.

        Completing here would stamp "confirmed" on a profile whose validation
        just FAILED. Reaching this point means real failures persisted all the
        way down to baseline values, which indicts the baseline itself or the
        environment — either way not a tuner verdict to paper over.
        """
        self.log_message.emit(
            "Validation failed and no core can be backed off further — the "
            "profile cannot be proven. Reverting all cores to baseline and "
            "pausing: failures reached baseline values, so either the baseline "
            "itself is unstable or something else on this machine interfered. "
            "Investigate, then Resume to retry validation."
        )
        self._revert_all_to_baseline()
        self._save_validation_pos()
        self.pause()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_cores_to_test(self) -> list[int]:
        if self._config.cores_to_test is not None:
            return sorted(self._config.cores_to_test)
        return sorted(self._topology.cores.keys())

    def _persist_narrative(self, message: str) -> None:
        """A narrative write must never take the engine down with it."""
        if self._session_id is None:
            return
        try:
            tp.log_event(self._db, self._session_id, message, self._boot_id)
        except Exception:
            log.debug("narrative write failed", exc_info=True)

    def _set_status(self, status: str) -> None:
        self._status = status
        self.status_changed.emit(status)

    def _emit_progress(self) -> None:
        done = sum(
            1 for cs in self._core_states.values()
            if cs.phase in (TunerPhase.CONFIRMED, TunerPhase.HARDENED)
        )
        total = len(self._core_states)
        self.progress_updated.emit(done, total)

    def _apply_co(self, core_id: int, value: int) -> bool:
        """Apply a CO offset through the write-ahead journal.

        Records the intended (core, value) durably BEFORE the hardware write so a
        hard crash is always attributable to the exact value that was resident.
        A value within the core's proven-safe envelope (0 is always safe) is
        journaled survived; a more-aggressive value is journaled un-survived until
        a test completes with it resident. Returns the SMU write result; raised
        SMU exceptions propagate to the caller's existing handling (the intent is
        already journaled, so the value is treated as suspect on the next resume).
        """
        if self._smu is None:
            return False
        survived = not self._is_more_aggressive(value, self._co_survived.get(core_id, 0))
        if self._session_id is not None:
            tp.journal_co_intent(self._db, self._session_id, core_id, value, survived)
        log.debug("CO write: core=%d value=%d survived=%s", core_id, value, survived)
        return self._smu.set_co_offset(core_id, value)

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
                success = self._apply_co(core_id, target)
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
            success = self._apply_co(test_core_id, test_offset)
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
                success = self._apply_co(core_id, cs.baseline_offset)
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
            success = self._apply_co(test_core_id, test_offset)
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

    def _revert_core_to_baseline(self, core_id: int) -> bool:
        """Revert a single core to its baseline offset after a test.

        Returns False when the SMU write failed: the tested (aggressive)
        offset is then still RESIDENT, so the caller must stop the flow —
        silently marching on with poisoned hardware state is how a bad
        session gets written.
        """
        if self._smu is None:
            return True
        cs = self._core_states.get(core_id)
        if cs is None:
            return True
        if self._co_applied.get(core_id) == cs.baseline_offset:
            return True  # already at baseline
        try:
            success = self._apply_co(core_id, cs.baseline_offset)
        except Exception as e:
            self.log_message.emit(
                f"Post-test baseline revert error for core {core_id}: {e}"
            )
            return False
        if success:
            self._co_applied[core_id] = cs.baseline_offset
            return True
        self.log_message.emit(
            f"Post-test baseline revert failed for core {core_id} "
            f"(offset {cs.baseline_offset}) — read-back mismatch"
        )
        return False

    def _revert_all_to_baseline(self) -> None:
        """Best-effort revert of all cores to baseline — used after partial CO failure."""
        if self._smu is None:
            return
        for core_id, cs in self._core_states.items():
            if self._co_applied.get(core_id) == cs.baseline_offset:
                continue
            try:
                success = self._apply_co(core_id, cs.baseline_offset)
                if success:
                    self._co_applied[core_id] = cs.baseline_offset
                else:
                    log.warning("Revert-to-baseline rejected for core %d", core_id)
            except Exception:
                log.warning("Revert-to-baseline failed for core %d", core_id, exc_info=True)

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
