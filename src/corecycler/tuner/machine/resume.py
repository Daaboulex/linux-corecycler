from __future__ import annotations

from dataclasses import dataclass

from corecycler.tuner.machine.bisect import current_group
from corecycler.tuner.machine.types import ALLCORE, CrashEvidenceRead, Evidence, SessionState, Stage


@dataclass(frozen=True, slots=True)
class Attribution:
    convict: tuple[int, ...] = ()
    held: int | None = None
    incident: bool = False
    flow_crash: bool = False
    stale_fail: tuple[int, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    at_stock: bool = False


def resident(s: SessionState, core: int) -> int:
    flow = s.flow
    owned = {r.core: r for r in s.cores}
    if core not in owned:
        raise KeyError(f"core {core} is not owned by this session")
    rec = owned[core]
    if flow.stage is Stage.SEARCH:
        return rec.value if rec.in_test else s.baseline_of(core)
    if flow.stage is Stage.LIVE1:
        return s.baseline_of(core) if flow.live1 is not None and flow.live1.solo is not None else rec.value
    if flow.stage is Stage.BISECT:
        run = flow.bisect
        group = current_group(run)
        if core not in group:
            return s.baseline_of(core)
        return rec.value if run.rerun is not None else dict(run.values)[core]
    if flow.stage in ALLCORE or flow.stage is Stage.MEMORY:
        if not rec.in_test:
            return s.baseline_of(core)
        cursor = flow.allcore
        trial = flow.stage is Stage.TIGHTEN and cursor is not None and cursor.item == core
        return rec.value - s.config.fine_step if trial else rec.value
    return rec.value if flow.applied else s.baseline_of(core)


def attribute(s: SessionState, e: CrashEvidenceRead) -> Attribution:
    in_test = tuple(r.core for r in s.cores if r.in_test)
    owned = {r.core for r in s.cores}
    if e.same_boot:
        return Attribution(stale_fail=in_test if e.stale_db else (), evidence=e.evidence)
    if e.other_session_wrote:
        return Attribution()
    if not e.dirty_end:
        return Attribution(evidence=e.evidence)
    if e.stale_db:
        return Attribution(stale_fail=in_test, incident=True, evidence=e.evidence, at_stock=True)
    corrected = tuple(ev for ev in e.evidence if ev.corrected)
    uncorrected = [ev for ev in e.evidence if not ev.corrected]
    for ev in uncorrected:
        if ev.core not in owned:
            return Attribution(held=ev.core, evidence=corrected)
    convicted = tuple(ev.core for ev in uncorrected if resident(s, ev.core) != 0)
    stock_records = tuple(ev for ev in uncorrected if resident(s, ev.core) == 0)
    if convicted:
        return Attribution(convict=convicted, evidence=corrected + stock_records)
    evidence = corrected + stock_records
    if e.shared_uncorrected and not uncorrected:
        return Attribution(incident=True, evidence=evidence)
    stage = s.flow.stage
    if stage is Stage.BISECT and in_test:
        group = current_group(s.flow.bisect)
        if len(group) == 1:
            return Attribution(convict=group, evidence=evidence)
    if stage is Stage.SEARCH and len(in_test) == 1:
        return Attribution(convict=in_test, evidence=evidence)
    suspects = tuple(core for core in e.suspects if core in owned)
    if suspects:
        return Attribution(convict=suspects, evidence=evidence)
    if in_test:
        return Attribution(flow_crash=True, evidence=evidence)
    return Attribution(incident=e.was_running, evidence=evidence)
