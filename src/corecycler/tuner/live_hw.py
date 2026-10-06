from __future__ import annotations

import argparse
import random
import signal
import sys
import time
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.engine import detector as kernel
from corecycler.engine.backends import get_backend, ycruncher
from corecycler.engine.backends.base import OutcomeKind, StressConfig, StressMode
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.engine.containment import lane_cgroup_parent
from corecycler.engine.execution import STDOUT_LOG, Lane, Supervisor, ThermalWatch, read_cpu_temperature
from corecycler.engine.memory_budget import MemoryBudgetUnavailable, memory_budget
from corecycler.tuner import stages, telemetry
from corecycler.tuner.live import LiveRig, Store
from corecycler.tuner.machine.types import (
    Abort,
    Close,
    Evidence,
    Group,
    Klass,
    Load,
    MachineConfig,
    Mode,
    PauseRequest,
    Report,
)
from corecycler.tuner.orders import default_method

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from corecycler.tuner.machine.types import TestSpec

CPU = Path("/sys/devices/system/cpu")
SAMPLE_S = 0.25
IDLE_SAMPLE_S = 0.5
ONE_T = {"tests": ("SFTv4", "FFTv4", "N63"), "seconds_per_test": 20, "memory_mb": 13}
TWO_T = {"tests": ("SFTv4", "N63", "VT3"), "seconds_per_test": 30, "memory_mb": 26, "binary": "19-ZN2 ~ Kagari"}
GROUPS = {
    OutcomeKind.PASS: Group.PASS,
    OutcomeKind.SILICON: Group.SILICON,
    OutcomeKind.ENVIRONMENT: Group.ENVIRONMENT,
    OutcomeKind.APPARATUS: Group.APPARATUS,
}


class SmuHardware:
    def __init__(self, smu, write: bool, say: Callable[[str], None], cpu_root: Path = CPU) -> None:
        self.smu = smu
        self.write = write
        self.say = say
        self.cpu_root = cpu_root

    def read_co(self, core: int) -> int | None:
        return self.smu.get_co_offset(core)

    def write_co(self, core: int, value: int) -> str | None:
        if not self.write:
            self.say(f"dry: core {core} would be set to {value}")
            return None
        if not self.smu.set_co_offset(core, value):
            return "the SMU refused the write"
        back = self.smu.get_co_offset(core)
        self.say(f"core {core} set to {value}, read back {back}")
        if back != value:
            return f"the read-back gave {back}"
        return None

    def temperature(self) -> float | None:
        return read_cpu_temperature()

    def frequency_capped(self) -> bool:
        freq = self.cpu_root / "cpu0" / "cpufreq"
        boost = self.cpu_root / "cpufreq" / "boost"
        if boost.exists() and boost.read_text().strip() == "0":
            return True
        top = freq / "amd_pstate_max_freq"
        if not top.exists():
            top = freq / "cpuinfo_max_freq"
        return int((freq / "scaling_max_freq").read_text()) < 0.98 * int(top.read_text())

    def boot_id(self) -> str:
        return kernel.read_boot_id()

    def boot_ended_clean(self, boot_id: str) -> bool:
        return kernel.boot_end(boot_id).clean

    def asleep_seconds(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def report_from(
    result, observed, lane_cpus: tuple[int, ...], core_of_cpu: dict[int, int], context=None, all_core: bool = False
) -> Report:
    evidence = tuple(
        Evidence(core_of_cpu[event.cpu], event.corrected)
        for event in observed
        if isinstance(event, kernel.CoreMachineCheck)
        and (all_core or event.cpu not in lane_cpus)
        and event.cpu in core_of_cpu
    )
    unblamed = [event for event in observed if isinstance(event, kernel.UnblamedMachineCheck)]
    trouble = any(isinstance(event, kernel.KernelTrouble) for event in observed)
    group = Group.THERMAL if result.outcome.value == "thermal" else GROUPS[result.outcome.kind]
    return Report(
        group=group,
        seconds=result.duration_seconds,
        evidence=evidence,
        detail=f"{result.outcome.value}: {result.detail}",
        unblamed_corrected=bool(unblamed) and all(event.corrected for event in unblamed) and not trouble,
        context=context,
    )


def named_cores(result, core_of_cpu: dict[int, int]) -> tuple[int, ...]:
    if result.outcome.kind is not OutcomeKind.SILICON or result.evidence_dir is None:
        return ()
    cpus = ycruncher.named_cpus((result.evidence_dir / STDOUT_LOG).read_text(errors="replace"))
    unknown = [cpu for cpu in cpus if cpu not in core_of_cpu]
    if unknown:
        raise ValueError(f"y-cruncher named CPUs {unknown}, which the topology does not have")
    return tuple(sorted({core_of_cpu[cpu] for cpu in cpus}))


def free_mb(meminfo: Path = Path("/proc/meminfo")) -> int:
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise OSError("/proc/meminfo has no MemAvailable line")


def read_clocks(cpu_root: Path = CPU) -> dict[int, float]:
    return {
        int(freq.parent.parent.name[3:]): int(freq.read_text()) / 1000
        for freq in cpu_root.glob("cpu[0-9]*/cpufreq/scaling_cur_freq")
    }


class Hold:
    def __init__(
        self,
        segment: stages.Segment,
        read: Callable[[], float | None],
        clocks: Callable[[], dict[int, float]],
        core_of_cpu: dict[int, int],
        every: float,
    ) -> None:
        self.segment = segment
        self.read = read
        self.clocks = clocks
        self.core_of_cpu = core_of_cpu
        self.every = every
        self.held = False
        self.ring: deque[telemetry.Sample] = deque(maxlen=telemetry.RING)
        self.next_sample = 0.0

    def __call__(self, elapsed: float) -> bool:
        sampling = elapsed >= self.next_sample
        temperature = self.read() if self.segment.hold is not None or sampling else None
        if self.segment.hold is not None and temperature is not None:
            hot, cool = self.segment.hold
            if temperature >= hot:
                self.held = True
            elif temperature < cool:
                self.held = False
        frozen = self.held or stages.frozen_at(self.segment, elapsed)
        if sampling:
            self.next_sample = elapsed + self.every
            per_core: dict[int, float] = {}
            for cpu, mhz in self.clocks().items():
                core = self.core_of_cpu.get(cpu)
                if core is not None:
                    per_core[core] = max(per_core.get(core, 0.0), mhz)
            self.ring.append(telemetry.Sample(not frozen, tuple(sorted(per_core.items())), temperature))
        return frozen


class StressRunner:
    def __init__(
        self,
        backend_factory,
        work_dir: Path,
        core_of_cpu: dict[int, int],
        supervisor=Supervisor,
        detector=None,
        read_temperature=None,
        available_mb=None,
        clocks=None,
        memory_backend=None,
        budget=None,
    ) -> None:
        self.backend_factory = backend_factory
        self.work_dir = work_dir
        self.core_of_cpu = core_of_cpu
        self.supervisor = supervisor
        self.detector = detector or kernel.ErrorDetector
        self.read_temperature = read_temperature or read_cpu_temperature
        self.available_mb = available_mb or free_mb
        self.clocks = clocks or read_clocks
        self.memory_backend = memory_backend or (lambda: get_backend("stressapptest"))
        self.budget = budget or (lambda n, floor: memory_budget(n, floor, lane_cgroup=lane_cgroup_parent()))
        self.runs = 0

    def _supervise(
        self,
        spec: TestSpec,
        stop: threading.Event,
        lanes: list[Lane],
        config: StressConfig,
        seconds: float,
        freeze=None,
        poll: float = 1.0,
        backend=None,
        hold_cap_s: float | None = None,
    ):
        observed: list = []
        watch = self.detector()
        watch.reset()
        try:
            supervisor = self.supervisor(
                backend=backend or self.backend_factory(),
                detector=watch,
                thermal=ThermalWatch(max_temperature=95.0, grace_seconds=3.0, hard_margin=8.0, require_sensor=True),
                stop_event=stop,
                observed=observed,
                poll_interval=poll,
                phase=f"{spec.stage.value} {(spec.load or spec.klass).value}",
                freeze=freeze,
                hold_cap_s=hold_cap_s,
            )
            results = supervisor.run(lanes, config, seconds)
        finally:
            watch.close()
        return results, observed

    def run(self, spec: TestSpec, stop: threading.Event) -> Report:
        self.runs += 1
        if spec.load is Load.SOAK:
            return self._soak(spec, stop)
        if spec.load in (None, Load.ONE_T, Load.TWO_T):
            settings = ONE_T if spec.klass is Klass.T1 else TWO_T
            config = StressConfig(mode=StressMode.CUSTOM, **settings)
            lane = Lane(core_id=spec.core, cpus=spec.cpus, work_dir=self.work_dir / f"core_{spec.core}")
            results, observed = self._supervise(spec, stop, [lane], config, spec.seconds)
            return report_from(results[lane.core_id], observed, spec.cpus, self.core_of_cpu)
        if spec.load is Load.MEMORY:
            return self._memory(spec, stop)
        firsts = tuple(
            sorted(
                {
                    min(c for c in spec.cpus if self.core_of_cpu[c] == core)
                    for core in {self.core_of_cpu[c] for c in spec.cpus}
                }
            )
        )
        rng = random.Random(f"{spec.stage.value}:{spec.load.value}:{spec.vector}:{self.runs}")
        segments = stages.plan(spec.load, spec.seconds, spec.cpus, firsts, self.available_mb(), rng)
        done = 0.0
        for segment in segments:
            config = StressConfig(
                mode=StressMode.CUSTOM,
                tests=segment.tests,
                seconds_per_test=segment.seconds_per_test,
                memory_mb=segment.memory_mb,
            )
            lane = Lane(core_id=spec.core, cpus=segment.cpus, work_dir=self.work_dir / f"all_{spec.load.value}")
            every = IDLE_SAMPLE_S if spec.load is Load.IDLE else SAMPLE_S
            hold = Hold(segment, self.read_temperature, self.clocks, self.core_of_cpu, every)
            results, observed = self._supervise(
                spec, stop, [lane], config, segment.duration, hold, poll=0.05, hold_cap_s=segment.hold_cap_s
            )
            result = results[lane.core_id]
            done += result.duration_seconds
            if result.outcome.kind is not OutcomeKind.PASS or observed:
                context = telemetry.context(list(hold.ring), spec.load is Load.HOT) if hold.ring else None
                report = report_from(result, observed, segment.cpus, self.core_of_cpu, context, all_core=True)
                if result.outcome.kind is not OutcomeKind.PASS or report.evidence:
                    return replace(report, seconds=done, named=named_cores(result, self.core_of_cpu))
        return Report(Group.PASS, done)

    def _memory(self, spec: TestSpec, stop: threading.Event) -> Report:
        backend = self.memory_backend()
        if not backend.is_available():
            return Report(Group.PASS, 0.0, skipped=f"stressapptest is not installed: {backend.resolution().problem}")
        by_core: dict[int, list[int]] = {}
        for cpu in spec.cpus:
            by_core.setdefault(self.core_of_cpu[cpu], []).append(cpu)
        lanes = [
            Lane(core_id=core, cpus=tuple(cpus), work_dir=self.work_dir / f"memory_{core}")
            for core, cpus in sorted(by_core.items())
        ]
        threads = max(len(lane.cpus) for lane in lanes)
        try:
            budget = self.budget(len(lanes), StressapptestBackend.minimum_memory_mb(threads))
        except MemoryBudgetUnavailable as exc:
            return Report(Group.PASS, 0.0, skipped=f"the memory budget is unavailable: {exc}")
        if not budget.fits:
            floor = budget.minimum_per_instance_mb
            reason = f"{budget.per_instance_mb} MB per instance is below the floor of {floor} MB ({budget.describe()})"
            return Report(Group.PASS, 0.0, skipped=reason)
        config = StressConfig(threads=threads, memory_mb=budget.per_instance_mb)
        results, observed = self._supervise(spec, stop, lanes, config, spec.seconds, backend=backend)
        named = tuple(sorted(core for core, r in results.items() if r.outcome.kind is OutcomeKind.SILICON))
        worst = min(results.values(), key=lambda r: r.outcome.rank)
        report = report_from(worst, observed, spec.cpus, self.core_of_cpu, all_core=True)
        failed = [f"core {core}: {r.detail}" for core, r in sorted(results.items()) if not r.passed]
        detail = "; ".join([*failed, f"memory budget: {budget.describe()}"]) if failed else ""
        seconds = max(r.duration_seconds for r in results.values())
        group = Group.SILICON if named else report.group
        return replace(report, group=group, seconds=seconds, named=named, detail=detail)

    def _soak(self, spec: TestSpec, stop: threading.Event) -> Report:
        watch = self.detector()
        watch.reset()
        observed: list = []
        problem = None
        try:
            end = time.monotonic() + spec.seconds
            while time.monotonic() < end and not stop.wait(min(1.0, max(0.0, end - time.monotonic()))):
                read = watch.read_new()
                observed.extend(read.events)
                problem = problem or read.problem
            read = watch.read_new(final=True)
            observed.extend(read.events)
            problem = problem or read.problem
        finally:
            watch.close()
        evidence = tuple(
            Evidence(self.core_of_cpu[e.cpu], e.corrected)
            for e in observed
            if isinstance(e, kernel.CoreMachineCheck) and e.cpu in self.core_of_cpu
        )
        if stop.is_set():
            return Report(Group.APPARATUS, 0.0, evidence, "the soak was stopped before its end")
        if problem is not None:
            return Report(Group.APPARATUS, spec.seconds, evidence, f"the kernel log could not be read: {problem}")
        unblamed = any(isinstance(e, (kernel.UnblamedMachineCheck, kernel.KernelTrouble)) for e in observed)
        group = Group.ENVIRONMENT if unblamed else Group.PASS
        return Report(group, spec.seconds, evidence, "soak watch")


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m corecycler.tuner.live_hw")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--session", type=int, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true")
    action.add_argument("--resume", action="store_true")
    parser.add_argument("--cores", default="")
    parser.add_argument("--max-offset", type=int, default=-15)
    parser.add_argument("--k1", type=int, default=2)
    parser.add_argument("--k2", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--wait-between-cores", type=float, default=15.0)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--acknowledge", action="store_true")
    return parser.parse_args(argv)


def build_rig(args: argparse.Namespace, topology, smu, backend_factory, say: Callable[[str], None]) -> LiveRig:
    cpus_of = tuple(sorted((core, tuple(info.logical_cpus)) for core, info in topology.cores.items()))
    domain_of = tuple(sorted((core, info.ccd) for core, info in topology.cores.items()))
    core_of_cpu = {cpu: core for core, cpus in cpus_of for cpu in cpus}
    return LiveRig(
        hardware=SmuHardware(smu, args.write, say),
        runner=StressRunner(backend_factory, args.state_dir / "runs", core_of_cpu),
        store=Store(args.state_dir),
        cpus_of=cpus_of,
        domain_of=domain_of,
        say=say,
    )


def install_signals(rig: LiveRig) -> None:
    signal.signal(signal.SIGINT, lambda *_: rig.signals.append(Close()))
    signal.signal(signal.SIGTERM, lambda *_: rig.signals.append(Close()))
    signal.signal(signal.SIGUSR1, lambda *_: rig.signals.append(PauseRequest()))
    signal.signal(signal.SIGUSR2, lambda *_: rig.signals.append(Abort()))


def run(args: argparse.Namespace, rig: LiveRig, say: Callable[[str], None]) -> int:
    if args.start:
        selected = tuple(int(core) for core in args.cores.split(",") if core)
        method = default_method(selected, dict(rig.domain_of))
        config = MachineConfig(
            Mode.CO,
            method,
            selected,
            max_offset=args.max_offset,
            k_1t=args.k1,
            k_2t=args.k2,
            wait_between_cores_s=args.wait_between_cores,
        )
        rig.start(config, args.session, args.seed, args.acknowledge)
    else:
        rig.resume(args.session)
    final = rig.run_until_rest()
    reason = f" ({final.pause.reason.value}: {final.pause.detail})" if final.pause is not None else ""
    say(f"rest: {final.lifecycle.value}{reason}")
    say("values: " + ", ".join(f"{r.core}={r.value} ({r.phase.value}, fb {r.fb})" for r in final.cores))
    return 0


def main(argv: list[str] | None = None) -> int:
    from corecycler.cli import _build_smu
    from corecycler.engine.backends import get_backend, load_all
    from corecycler.engine.topology import detect_topology

    args = parse(sys.argv[1:] if argv is None else argv)
    topology = detect_topology()
    smu = _build_smu(topology)
    if smu is None:
        print("live: the SMU or its core map is unavailable", file=sys.stderr)
        return 2
    load_all()

    def say(line: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)

    rig = build_rig(args, topology, smu, lambda: get_backend("y-cruncher"), say)
    install_signals(rig)
    return run(args, rig, say)


if __name__ == "__main__":
    sys.exit(main())
