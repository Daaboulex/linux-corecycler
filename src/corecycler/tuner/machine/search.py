from __future__ import annotations

from dataclasses import dataclass, replace

from corecycler.tuner.machine.types import CoreRecord, Klass, MachineConfig, Phase

CRASH_STEPS = 3


@dataclass(frozen=True, slots=True)
class CoreStep:
    record: CoreRecord
    stock_unstable: bool = False
    first_value: bool = False


def resolve_start(rec: CoreRecord, cfg: MachineConfig) -> CoreRecord:
    if rec.phase is not Phase.NOT_STARTED:
        return rec
    s = rec.start
    if s != 0 and rec.inside(s, cfg.max_offset):
        return replace(rec, phase=Phase.COARSE, value=s)
    s = 0
    if rec.inside(s - cfg.coarse_step, cfg.max_offset):
        return replace(rec, phase=Phase.COARSE, value=s - cfg.coarse_step, start=s)
    if rec.inside(s - cfg.fine_step, cfg.max_offset):
        return replace(rec, phase=Phase.FINE, value=s - cfg.fine_step, start=s)
    return replace(rec, phase=Phase.PROVEN, value=0, start=s, count=0)


def _prove_at(rec: CoreRecord, value: int) -> CoreRecord:
    if value == 0:
        return replace(rec, phase=Phase.PROVEN, value=0, count=0)
    return replace(rec, phase=Phase.PROVE, value=value, count=0)


def _over_budget(rec: CoreRecord, cfg: MachineConfig) -> bool:
    return rec.walk_seconds > cfg.walk_budget_s


def _settle_walk(rec: CoreRecord) -> CoreRecord:
    return _prove_at(rec, rec.best if rec.best is not None else 0)


def _reclaimed(rec: CoreRecord) -> CoreRecord:
    return replace(rec, phase=Phase.PROVEN, value=rec.reclaim_floor, count=0, skipped=None, reclaim_floor=None)


def on_pass(rec: CoreRecord, cfg: MachineConfig, klass: Klass, seconds: float) -> CoreStep:
    v = rec.value
    if rec.phase in (Phase.COARSE, Phase.FINE):
        first = rec.walk_tests == 0
        rec = replace(rec, best=v, walk_seconds=rec.walk_seconds + seconds, walk_tests=rec.walk_tests + 1)
        if _over_budget(rec, cfg):
            return CoreStep(_settle_walk(rec), first_value=first)
        step = cfg.coarse_step if rec.phase is Phase.COARSE else cfg.fine_step
        if rec.inside(v - step, cfg.max_offset):
            return CoreStep(replace(rec, value=v - step), first_value=first)
        if rec.phase is Phase.COARSE and rec.inside(v - cfg.fine_step, cfg.max_offset):
            return CoreStep(replace(rec, phase=Phase.FINE, value=v - cfg.fine_step), first_value=first)
        return CoreStep(_prove_at(rec, v), first_value=first)
    if rec.phase is Phase.PROVE:
        count = rec.count + 1
        if count >= cfg.k(klass):
            deeper = v - cfg.fine_step
            if rec.skipped is not None and rec.inside(deeper, cfg.max_offset):
                return CoreStep(replace(rec, value=deeper, count=0, reclaim_floor=v))
            return CoreStep(replace(rec, phase=Phase.PROVEN, count=count, skipped=None, reclaim_floor=None))
        return CoreStep(replace(rec, count=count))
    if rec.phase is Phase.PROVEN:
        return CoreStep(replace(rec, count=rec.count + 1))
    raise ValueError(f"core {rec.core} has no test to pass in phase {rec.phase}")


def on_fail(rec: CoreRecord, cfg: MachineConfig, klass: Klass, seconds: float) -> CoreStep:
    if rec.phase is Phase.NOT_STARTED:
        raise ValueError(f"core {rec.core} has no test to fail in phase {rec.phase}")
    v = rec.value
    if v == 0:
        return CoreStep(rec, stock_unstable=True)
    first_value = rec.phase in (Phase.COARSE, Phase.FINE) and rec.walk_tests == 0
    if rec.phase in (Phase.COARSE, Phase.FINE):
        rec = replace(rec, fb=v, walk_seconds=rec.walk_seconds + seconds, walk_tests=rec.walk_tests + 1)
        if rec.best is not None:
            if rec.phase is Phase.COARSE and not _over_budget(rec, cfg):
                nxt = rec.best - cfg.fine_step
                if rec.inside(nxt, cfg.max_offset):
                    return CoreStep(replace(rec, phase=Phase.FINE, value=nxt), first_value=first_value)
            return CoreStep(_prove_at(rec, rec.best), first_value=first_value)
        if rec.start != 0 and not _over_budget(rec, cfg):
            restarted = resolve_start(replace(rec, phase=Phase.NOT_STARTED, start=0), cfg)
            return CoreStep(restarted, first_value=first_value)
        if rec.phase is Phase.COARSE and not _over_budget(rec, cfg) and rec.inside(-cfg.fine_step, cfg.max_offset):
            return CoreStep(replace(rec, phase=Phase.FINE, value=-cfg.fine_step), first_value=first_value)
        return CoreStep(_prove_at(rec, 0), first_value=first_value)
    if rec.reclaim_floor is not None:
        return CoreStep(_reclaimed(replace(rec, fb=v)))
    return CoreStep(_prove_at(replace(rec, fb=v), min(0, v + cfg.fine_step)))


def on_crash(rec: CoreRecord, cfg: MachineConfig) -> CoreStep:
    v = rec.value
    if v == 0:
        return CoreStep(rec, stock_unstable=True)
    backed = replace(rec, fb=v, crash_count=rec.crash_count + 1, skipped=v)
    if rec.reclaim_floor is not None:
        return CoreStep(_reclaimed(backed))
    landing = min(0, v + CRASH_STEPS * cfg.fine_step)
    if landing == 0 and backed.inside(-cfg.fine_step, cfg.max_offset):
        return CoreStep(replace(_prove_at(backed, -cfg.fine_step), reclaim_floor=0))
    return CoreStep(_prove_at(backed, landing))


def back_off_in_place(rec: CoreRecord, cfg: MachineConfig, steps: int) -> CoreRecord:
    if rec.value == 0:
        return rec
    fb = rec.value if rec.fb is None or rec.value > rec.fb else rec.fb
    return replace(rec, fb=fb, value=min(0, rec.value + steps * cfg.fine_step), count=0)


def loosen(rec: CoreRecord, cfg: MachineConfig, steps: int = 1) -> CoreRecord:
    return replace(rec, value=min(0, rec.value + steps * cfg.fine_step), count=0)


def begin_2t(rec: CoreRecord) -> CoreRecord:
    return _prove_at(rec, rec.value)


def class_done(cores: tuple[CoreRecord, ...]) -> bool:
    return all(r.phase is Phase.PROVEN for r in cores)
