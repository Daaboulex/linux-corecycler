"""Worker reports and kernel events for engine intake specs."""

from __future__ import annotations

from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.detector import MCEEvent
from corecycler.tuner.engine import WorkerReport


def mce(cpu: int, *, corrected: bool = True, message: str = "corrected", bank: int = 0) -> MCEEvent:
    return MCEEvent(timestamp=1.0, cpu=cpu, bank=bank, message=message, corrected=corrected, raw_ts=1.0)


def report(
    core_id: int = 0,
    outcome: Outcome = Outcome.PASS,
    detail: str = "",
    duration: float = 1.0,
    *,
    stretch: float = 0.0,
    events: tuple[MCEEvent, ...] = (),
    others: tuple[StressResult, ...] = (),
) -> WorkerReport:
    lane = StressResult(core_id=core_id, outcome=outcome, detail=detail, duration_seconds=duration)
    return WorkerReport(lanes=(lane, *others), peak_stretch_pct=stretch, kernel_events=events)
