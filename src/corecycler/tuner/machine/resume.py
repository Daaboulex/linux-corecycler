from __future__ import annotations

from dataclasses import dataclass

from corecycler.tuner.machine.allcore import round_members
from corecycler.tuner.machine.bisect import current_group
from corecycler.tuner.machine.types import ALLCORE, CrashEvidenceRead, Evidence, SessionState, Stage


@dataclass(frozen=True, slots=True)
class Attribution:
    convict: tuple[int, ...] = ()
    held: int | None = None
    incident: bool = False
    flow_crash: bool = False
    lost: int | None = None
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
        if core not in current_group(run) or not any(r.in_test for r in s.cores):
            return s.baseline_of(core)
        return rec.value if run.rerun is not None else dict(run.values)[core]
    if flow.stage in ALLCORE or flow.stage is Stage.MEMORY:
        if not rec.in_test:
            return s.baseline_of(core)
        cursor = flow.allcore
        trial = flow.stage is Stage.TIGHTEN and cursor is not None and core in round_members(s.cores, s.config, cursor)
        return rec.value - s.config.fine_step if trial else rec.value
    return rec.value if flow.applied else s.baseline_of(core)


def attribute(s: SessionState, e: CrashEvidenceRead) -> Attribution:
    in_test = tuple(r.core for r in s.cores if r.in_test)
    owned = {r.core for r in s.cores}
    if e.same_boot:
        return Attribution(lost=in_test[0] if e.stale_db and in_test else None, evidence=e.evidence)
    if e.other_session_wrote:
        return Attribution()
    if not e.dirty_end:
        return Attribution(evidence=e.evidence)
    if e.stale_db:
        return Attribution(lost=in_test[0] if in_test else None, incident=True, evidence=e.evidence, at_stock=True)
    held = next((i for i, ev in enumerate(e.evidence) if not ev.corrected and ev.core not in owned), None)
    records = e.evidence if held is None else (*e.evidence[:held], *e.evidence[held + 1 :])
    others = tuple(ev for ev in records if ev.corrected or ev.core not in owned)
    uncorrected = [ev for ev in records if not ev.corrected and ev.core in owned]
    bounds = {r.core: r.fb for r in s.cores}
    stage = s.flow.stage
    slot = current_group(s.flow.bisect) if stage is Stage.BISECT and in_test else ()
    full_set = stage is Stage.BISECT and slot == s.flow.bisect.universe
    convicted = tuple(
        dict.fromkeys(
            ev.core
            for ev in uncorrected
            if not full_set
            and resident(s, ev.core) != 0
            and ev.charged != 0
            and (ev.charged is None or bounds[ev.core] is None or ev.charged > bounds[ev.core])
        )
    )
    evidence = others + tuple(uncorrected)
    if held is not None:
        return Attribution(convict=convicted, held=e.evidence[held].core, evidence=evidence)
    if convicted:
        return Attribution(convict=convicted, evidence=evidence)
    if e.shared_uncorrected and not uncorrected:
        return Attribution(incident=True, evidence=evidence)
    if len(slot) == 1:
        return Attribution(convict=slot, evidence=evidence)
    if stage is Stage.SEARCH and len(in_test) == 1:
        return Attribution(convict=in_test, evidence=evidence)
    suspects = tuple(core for core in e.suspects if core in owned and core not in slot)
    if suspects:
        return Attribution(convict=suspects, evidence=evidence)
    if in_test:
        return Attribution(flow_crash=True, evidence=evidence)
    return Attribution(incident=e.was_running, evidence=evidence)
