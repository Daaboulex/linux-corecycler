"""Worker reports and kernel events for engine intake specs."""

from __future__ import annotations

from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.detector import CoreMachineCheck, KernelEvent, KernelTrouble, UnblamedMachineCheck
from corecycler.tuner.engine import WorkerReport


def mce(cpu: int, *, corrected: bool = True, message: str = "corrected", bank: int = 0) -> CoreMachineCheck:
    return CoreMachineCheck(cpu=cpu, bank=bank, corrected=corrected, message=message, raw_ts=1.0)


def unblamed(*, corrected: bool = True, message: str = "corrected", bank: int = 21) -> UnblamedMachineCheck:
    return UnblamedMachineCheck(logged_by_cpu=0, bank=bank, corrected=corrected, message=message, raw_ts=1.0)


def trouble(message: str = "Kernel panic - not syncing: Fatal machine check") -> KernelTrouble:
    return KernelTrouble(message=message, raw_ts=1.0)


def report(
    core_id: int = 0,
    outcome: Outcome = Outcome.PASS,
    detail: str = "",
    duration: float = 1.0,
    *,
    stretch: float = 0.0,
    events: tuple[KernelEvent, ...] = (),
    others: tuple[StressResult, ...] = (),
    kernel_read_ns: int | None = None,
) -> WorkerReport:
    lane = StressResult(core_id=core_id, outcome=outcome, detail=detail, duration_seconds=duration)
    return WorkerReport(
        lanes=(lane, *others), peak_stretch_pct=stretch, kernel_events=events, kernel_read_ns=kernel_read_ns
    )
