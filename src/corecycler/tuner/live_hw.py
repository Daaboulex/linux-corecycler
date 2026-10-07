from __future__ import annotations

import argparse
import os
import random
import signal
import struct
import sys
import time
from collections import deque
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.engine import detector as kernel
from corecycler.engine.backends import get_backend, ycruncher
from corecycler.engine.backends.base import OutcomeKind, StressConfig, StressMode
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.engine.containment import lane_cgroup_parent, memory_accounting_problem
from corecycler.engine.execution import Lane, Supervisor, ThermalWatch
from corecycler.engine.memory_budget import MemoryBudgetUnavailable, memory_budget
from corecycler.history.context import read_bios_version
from corecycler.history.db import HistoryDB
from corecycler.monitor.frequency import Clocks, PowerState, fastest, read_power_state
from corecycler.monitor.hwmon import SensorRead, Sensors
from corecycler.smu.commands import tjmax_c
from corecycler.smu.driver import core_map_blocked
from corecycler.smu.pmtable import PMTableReader, read_power_limits
from corecycler.tuner import stages, telemetry
from corecycler.tuner.live import BootFacts, Identity, LiveRig, Store
from corecycler.tuner.machine.types import (
    Abort,
    Close,
    Evidence,
    Group,
    Klass,
    Load,
    MachineConfig,
    Mode,
    PauseReason,
    PauseRequest,
    Report,
)
from corecycler.tuner.orders import default_method

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from corecycler.tuner.machine.types import TestSpec

CPU = Path("/sys/devices/system/cpu")
HWMON = Path("/sys/class/hwmon")
PCI = Path("/sys/bus/pci/devices")
DMI = Path("/sys/class/dmi/id")
MODULES = Path("/sys/module")
NOTES = Path("/sys/kernel/notes")
CPUINFO = Path("/proc/cpuinfo")
NT_GNU_BUILD_ID = 3
IGPU_IDS = frozenset({("0x1002", "0x13c0"), ("0x1002", "0x164e")})
FREEZE_TICK_S = 0.05
ONE_T = {"tests": ("SFTv4", "FFTv4", "N63"), "seconds_per_test": 20, "memory_mb": 13}
TWO_T = {"tests": ("SFTv4", "N63", "VT3"), "seconds_per_test": 30, "memory_mb": 26, "binary": "19-ZN2 ~ Kagari"}
GROUPS = {
    OutcomeKind.PASS: Group.PASS,
    OutcomeKind.SILICON: Group.SILICON,
    OutcomeKind.ENVIRONMENT: Group.ENVIRONMENT,
    OutcomeKind.APPARATUS: Group.APPARATUS,
}


def lane_budget(instances: int, launch_minimum_mb: int):
    return memory_budget(instances, launch_minimum_mb, lane_cgroup=lane_cgroup_parent())


class SmuHardware:
    def __init__(
        self,
        smu,
        write: bool,
        say: Callable[[str], None],
        sensors: Callable[[], SensorRead],
        cpu_root: Path = CPU,
        tools: tuple = (),
        budget=lane_budget,
    ) -> None:
        self.smu = smu
        self.write = write
        self.say = say
        self.sensors = sensors
        self.cpu_root = cpu_root
        self.tools = tools
        self.budget = budget

    def preflight(self) -> tuple[str | None, tuple[str, ...]]:
        return preflight(self.smu, self.sensors, self.power, self.tools, self.budget)

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
        return self.sensors().hottest

    def power(self) -> PowerState:
        return read_power_state(self.cpu_root)

    def boot_id(self) -> str:
        return kernel.read_boot_id()

    def boot_ended_clean(self, boot_id: str) -> bool:
        return kernel.boot_end(boot_id).clean

    def setup(
        self, cpus_of: tuple[tuple[int, tuple[int, ...]], ...], domain_of: tuple[tuple[int, int | None], ...]
    ) -> tuple[tuple[str, str], ...]:
        ppt, tdc, edc = read_power_limits()
        pbo = (
            f"ppt {_number(ppt)} tdc {_number(tdc)} edc {_number(edc)} "
            f"scalar {_number(self.smu.get_pbo_scalar())} boost {_number(self.smu.get_boost_limit())}"
        )
        l3 = dict(domain_of)
        cores = "; ".join(f"{core}: {','.join(map(str, cpus))} l3 {l3.get(core)}" for core, cpus in cpus_of)
        bios = (_setting(DMI / name) for name in ("bios_vendor", "bios_version", "bios_date", "bios_release"))
        smu = (_setting(MODULES / "ryzen_smu" / name) for name in ("version", "srcversion"))
        return (
            ("app", app_version()),
            ("bios", " ".join(bios)),
            ("kernel", kernel_build()),
            ("microcode", microcode()),
            ("pbo", pbo),
            ("ryzen_smu", " ".join(smu)),
            ("smu_firmware", _setting(Path(SMU_DRIVER) / "version")),
            ("topology", f"online {_setting(self.cpu_root / 'online')}; {cores}"),
        )

    def boot_facts(self, boot_id: str) -> BootFacts:
        pm = PMTableReader().read()
        clocks = pm if pm is not None and pm.is_calibrated else None
        reasons, problem = kernel.reset_reasons(boot_id)
        return BootFacts(
            bios_version=read_bios_version(),
            fclk_mhz=None if clocks is None else clocks.fclk_mhz,
            uclk_mhz=None if clocks is None else clocks.uclk_mhz,
            mclk_mhz=None if clocks is None else clocks.mclk_mhz,
            reset_reasons=reasons,
            reset_problem=problem,
            igpu=igpu(),
        )

    def harvest(self, boot_id: str, since_ns: int | None, next_boot: bool) -> kernel.JournalHarvest:
        after = None if since_ns is None else kernel.HarvestFrom(mono_ns=since_ns, realtime_us=None)
        return kernel.harvest_after(boot_id, after, next_boot=next_boot)

    def asleep_seconds(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def _setting(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError as exc:
        return f"unreadable: {exc.strerror}"


def build_id(notes: bytes) -> str | None:
    at = 0
    while at + 12 <= len(notes):
        namesz, descsz, kind = struct.unpack_from("<III", notes, at)
        name_at = at + 12
        desc_at = name_at + (namesz + 3) // 4 * 4
        if kind == NT_GNU_BUILD_ID and notes[name_at : name_at + namesz] == b"GNU\0":
            return notes[desc_at : desc_at + descsz].hex()
        at = desc_at + (descsz + 3) // 4 * 4
    return None


def kernel_build(notes: Path = NOTES) -> str:
    try:
        found = build_id(notes.read_bytes())
    except OSError as exc:
        return f"{os.uname().release} build unreadable: {exc.strerror}"
    return f"{os.uname().release} build {found or 'id absent'}"


def microcode(cpuinfo: Path = CPUINFO) -> str:
    try:
        text = cpuinfo.read_text()
    except OSError as exc:
        return f"unreadable: {exc.strerror}"
    found = {line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("microcode")}
    return ",".join(sorted(found)) or "absent"


def app_version() -> str:
    try:
        return metadata.version("corecycler")
    except metadata.PackageNotFoundError:
        return "unreadable: not installed"


def _number(value: float | None) -> str:
    return "unreadable" if value is None else f"{value:.1f}"


def igpu(pci: Path = PCI) -> dict:
    if not pci.is_dir():
        return {"present": None, "problem": f"{pci} is not readable"}
    found = [dev for dev in sorted(pci.iterdir()) if (_setting(dev / "vendor"), _setting(dev / "device")) in IGPU_IDS]
    if len(found) > 1:
        return {"present": None, "problem": "more than one iGPU id: " + ", ".join(dev.name for dev in found)}
    if not found:
        return {"present": False}
    dev = found[0]
    driver = dev / "driver"
    return {
        "present": True,
        "address": dev.name,
        "device": _setting(dev / "device"),
        "driver": driver.resolve().name if driver.exists() else None,
        "vram_total": _setting(dev / "mem_info_vram_total"),
    }


SMU_DRIVER = "/sys/kernel/ryzen_smu_drv"
PROC = Path("/proc")


def _others(proc: Path) -> list[Path]:
    return [pid for pid in proc.iterdir() if pid.name.isdigit() and int(pid.name) != os.getpid()]


def smu_holder(proc: Path = PROC) -> str | None:
    for pid in _others(proc):
        try:
            targets = [os.readlink(fd) for fd in (pid / "fd").iterdir()]
        except FileNotFoundError, ProcessLookupError:
            continue
        except PermissionError as exc:
            return f"process {pid.name} cannot be inspected ({exc}), so another SMU driver holder cannot be ruled out"
        if any(target.startswith(SMU_DRIVER) for target in targets):
            name = (pid / "comm").read_text().strip()
            return f"{name} (pid {pid.name}) holds the SMU driver open, so its writes and this session's could collide"
    return None


def running(name: str, proc: Path = PROC) -> bool:
    for pid in _others(proc):
        try:
            if (pid / "comm").read_text().strip() == name:
                return True
        except FileNotFoundError:
            continue
    return False


def scheduling_problem() -> str | None:
    policy = os.sched_getscheduler(0)
    if policy in (os.SCHED_IDLE, os.SCHED_BATCH):
        return "the tool runs under an idle or batch scheduling policy, so its own watch could be starved by the load"
    return None


def preflight(smu, sensors, power, tools, budget, proc: Path = PROC) -> tuple[str | None, tuple[str, ...]]:
    """The first gate of L01 that refuses (in L01 order, later gates not run), and the warnings."""
    gates = (
        lambda: core_map_blocked(smu),
        kernel.kernel_log_problem,
        memory_accounting_problem,
        lambda: (
            None
            if sensors().hottest is not None
            else "no CPU temperature can be read, so nothing would stop a load that overheats"
        ),
        lambda: "a frequency cap or disabled boost would make every verdict meaningless" if power().capped() else None,
        lambda: smu_holder(proc),
        scheduling_problem,
        *(
            lambda tool=tool: (
                None if tool.is_available() else f"{tool.name} is not installed: {tool.resolution().problem}"
            )
            for tool in tools
        ),
    )
    refusal = next((problem for gate in gates if (problem := gate()) is not None), None)
    warnings = []
    if running("ananicy-cpp", proc):
        warnings.append("ananicy-cpp is running and may renice the stress tools while they run")
    hot_mb = budget(1, 0).usable_mb // 2
    if hot_mb < stages.HOT_MEMORY_CAP_MB:
        warnings.append(f"HOT runs y-cruncher with {hot_mb} MB, under its {stages.HOT_MEMORY_CAP_MB} MB cap")
    return refusal, tuple(warnings)


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
        detail="" if result.passed else f"{result.outcome.value}: {result.detail}",
        unblamed_corrected=bool(unblamed) and all(event.corrected for event in unblamed) and not trouble,
        context=context,
        speeds=result.speeds,
        output=result.output,
    )


def named_cores(result, core_of_cpu: dict[int, int]) -> tuple[int, ...]:
    if result.outcome.kind is not OutcomeKind.SILICON:
        return ()
    cpus = ycruncher.named_cpus(result.output)
    unknown = [cpu for cpu in cpus if cpu not in core_of_cpu]
    if unknown:
        raise ValueError(f"y-cruncher named CPUs {unknown}, which the topology does not have")
    return tuple(sorted({core_of_cpu[cpu] for cpu in cpus}))


class Hold:
    def __init__(
        self,
        segment: stages.Segment,
        clocks: Callable[[], dict[int, float | None]],
        core_of_cpu: dict[int, int],
        every: float,
        sink: Callable[[telemetry.Sample], None] | None = None,
    ) -> None:
        self.segment = segment
        self.clocks = clocks
        self.cpus_of: dict[int, list[int]] = {}
        for cpu, core in sorted(core_of_cpu.items()):
            self.cpus_of.setdefault(core, []).append(cpu)
        self.every = every
        self.sink = sink
        self.held = False
        self.ring: deque[telemetry.Sample] = deque(maxlen=telemetry.RING)
        self.next_sample = 0.0

    def __call__(self, elapsed: float, reading: SensorRead) -> bool:
        sampling = elapsed >= self.next_sample
        temperature = reading.hottest
        if self.segment.hold is not None and temperature is not None:
            hot, cool = self.segment.hold
            if temperature >= hot:
                self.held = True
            elif temperature < cool:
                self.held = False
        frozen = self.held or stages.frozen_at(self.segment, elapsed)
        if sampling:
            self.next_sample = elapsed + self.every
            clocks = self.clocks()
            per_core = ((core, fastest(clocks, cpus)) for core, cpus in sorted(self.cpus_of.items()))
            sample = telemetry.Sample(
                not frozen,
                tuple((core, mhz) for core, mhz in per_core if mhz is not None),
                reading.tctl,
                time.monotonic_ns(),
                reading.tccd,
                reading.volts,
            )
            self.ring.append(sample)
            if self.sink is not None:
                self.sink(sample)
        return frozen


class StressRunner:
    def __init__(
        self,
        backend_factory,
        work_dir: Path,
        core_of_cpu: dict[int, int],
        sensors: Callable[[], SensorRead],
        clocks: Callable[[], dict[int, float | None]],
        supervisor=Supervisor,
        detector=None,
        memory_backend=None,
        budget=None,
    ) -> None:
        self.backend_factory = backend_factory
        self.work_dir = work_dir
        self.core_of_cpu = core_of_cpu
        self.supervisor = supervisor
        self.detector = detector or kernel.ErrorDetector
        self.sensors = sensors
        self.clocks = clocks
        self.memory_backend = memory_backend or (lambda: get_backend("stressapptest"))
        self.budget = budget or lane_budget
        self.runs = 0
        self.read_ns: int | None = None
        self.sink: Callable[[telemetry.Sample], None] | None = None

    def _supervise(
        self,
        spec: TestSpec,
        stop: threading.Event,
        lanes: list[Lane],
        config: StressConfig,
        seconds: float,
        freeze,
        poll: float,
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
                thermal=ThermalWatch(
                    max_temperature=spec.thermal_limit_c, grace_seconds=3.0, hard_margin=8.0, require_sensor=True
                ),
                sensors=self.sensors,
                stop_event=stop,
                observed=observed,
                poll_interval=poll,
                phase=f"{spec.stage.value} {(spec.load or spec.klass).value}",
                freeze=freeze,
                hold_cap_s=hold_cap_s,
            )
            results = supervisor.run(lanes, config, seconds)
            self.read_ns = watch.read_started_ns
        finally:
            watch.close()
        return results, observed

    def run(
        self, spec: TestSpec, stop: threading.Event, sink: Callable[[telemetry.Sample], None] | None = None
    ) -> Report:
        if spec.thermal_limit_c is None:
            raise ValueError("a test spec carries no thermal limit, so its run would have no thermal stop")
        self.read_ns = None
        self.sink = sink
        report = self._run(spec, stop)
        return replace(report, kernel_read_ns=self.read_ns)

    def _run(self, spec: TestSpec, stop: threading.Event) -> Report:
        self.runs += 1
        if spec.load is Load.SOAK:
            return self._soak(spec, stop)
        if spec.load in (None, Load.ONE_T, Load.TWO_T):
            settings = ONE_T if spec.klass is Klass.T1 else TWO_T
            config = StressConfig(mode=StressMode.CUSTOM, **settings)
            lane = Lane(core_id=spec.core, cpus=spec.cpus, work_dir=self.work_dir / f"core_{spec.core}")
            sampler = None
            if self.sink is not None:
                flat = stages.Segment(
                    spec.cpus, settings["tests"], settings["seconds_per_test"], settings["memory_mb"], spec.seconds
                )
                sampler = Hold(flat, self.clocks, self.core_of_cpu, telemetry.SAMPLE_S, self.sink)
            results, observed = self._supervise(
                spec, stop, [lane], config, spec.seconds, sampler, poll=telemetry.SAMPLE_S
            )
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
        segments = stages.plan(
            spec.load, spec.seconds, spec.cpus, firsts, self.budget(1, 0).usable_mb, rng, spec.thermal_limit_c
        )
        done = 0.0
        speeds: list[tuple[str, float, bool]] = []
        for segment in segments:
            config = StressConfig(
                mode=StressMode.CUSTOM,
                tests=segment.tests,
                seconds_per_test=segment.seconds_per_test,
                memory_mb=segment.memory_mb,
            )
            lane = Lane(core_id=spec.core, cpus=segment.cpus, work_dir=self.work_dir / f"all_{spec.load.value}")
            hold = Hold(segment, self.clocks, self.core_of_cpu, telemetry.SAMPLE_S, self.sink)
            results, observed = self._supervise(
                spec, stop, [lane], config, segment.duration, hold, poll=FREEZE_TICK_S, hold_cap_s=segment.hold_cap_s
            )
            result = results[lane.core_id]
            done += result.duration_seconds
            speeds.extend(result.speeds)
            if result.outcome.kind is not OutcomeKind.PASS or observed:
                assigned = tuple(sorted({self.core_of_cpu[c] for c in segment.cpus}))
                named = named_cores(result, self.core_of_cpu)
                steady = spec.load is Load.HOT
                context = telemetry.context(list(hold.ring), steady, assigned, named) if hold.ring else None
                report = report_from(result, observed, segment.cpus, self.core_of_cpu, context, all_core=True)
                if result.outcome.kind is not OutcomeKind.PASS or report.evidence:
                    return replace(report, seconds=done, named=named, speeds=tuple(speeds))
        return Report(Group.PASS, done, speeds=tuple(speeds))

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
        sampler = None
        if self.sink is not None:
            flat = stages.Segment(spec.cpus, (), 0, budget.per_instance_mb, spec.seconds)
            sampler = Hold(flat, self.clocks, self.core_of_cpu, telemetry.SAMPLE_S, self.sink)
        results, observed = self._supervise(
            spec, stop, lanes, config, spec.seconds, sampler, poll=telemetry.SAMPLE_S, backend=backend
        )
        named = tuple(sorted(core for core, r in results.items() if r.outcome.kind is OutcomeKind.SILICON))
        worst = min(results.values(), key=lambda r: r.outcome.rank)
        report = report_from(worst, observed, spec.cpus, self.core_of_cpu, all_core=True)
        failed = [f"core {core}: {r.detail}" for core, r in sorted(results.items()) if not r.passed]
        detail = "; ".join([*failed, f"memory budget: {budget.describe()}"]) if failed else ""
        seconds = max(r.duration_seconds for r in results.values())
        group = Group.SILICON if named else report.group
        output = "\n".join(f"core {core}:\n{r.output}" for core, r in sorted(results.items()) if r.output)
        return replace(report, group=group, seconds=seconds, named=named, detail=detail, output=output)

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
            self.read_ns = watch.read_started_ns
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
    parser.add_argument("--db", type=Path)
    parser.add_argument("--session", type=int)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true")
    action.add_argument("--resume", action="store_true")
    action.add_argument("--validate", action="store_true")
    action.add_argument("--continue", dest="continue_on", action="store_true")
    action.add_argument("--restart", action="store_true")
    parser.add_argument("--cores", default="")
    parser.add_argument("--max-offset", type=int, default=-15)
    parser.add_argument("--k1", type=int, default=2)
    parser.add_argument("--k2", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--wait-between-cores", type=float, default=15.0)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--acknowledge", action="store_true")
    args = parser.parse_args(argv)
    for named, flag in (
        ("resume", "resume"),
        ("validate", "validate"),
        ("continue_on", "continue"),
        ("restart", "restart"),
    ):
        if getattr(args, named) and args.session is None:
            parser.error(f"--{flag} needs --session")
    return args


def read_identity(db: HistoryDB, smu, topology) -> Identity:
    from corecycler.history import context

    record = context.capture_system_context(smu, len(topology.cores))
    return Identity(record.bios_version, topology.model_name, context.find_or_create_context(db, record))


def build_rig(
    args: argparse.Namespace,
    topology,
    smu,
    backend_factory,
    say: Callable[[str], None],
    db: HistoryDB,
    identity: Identity,
) -> LiveRig:
    cpus_of = tuple(sorted((core, tuple(info.logical_cpus)) for core, info in topology.cores.items()))
    domain_of = tuple(sorted((core, info.ccd) for core, info in topology.cores.items()))
    core_of_cpu = {cpu: core for core, cpus in cpus_of for cpu in cpus}
    sensors = Sensors().read
    return LiveRig(
        hardware=SmuHardware(smu, args.write, say, sensors, tools=(backend_factory(), get_backend("stressapptest"))),
        runner=StressRunner(backend_factory, args.state_dir / "runs", core_of_cpu, sensors, Clocks().read),
        store=Store(db, args.state_dir, identity),
        cpus_of=cpus_of,
        domain_of=domain_of,
        tjmax_c=tjmax_c(topology.model_name),
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
        session = rig.store.new_session_id()
        say(f"session {session}")
        rig.start(config, session, args.seed, args.acknowledge)
    elif args.validate:
        rig.validate(args.session)
    else:
        rig.resume(args.session)
    final = rig.run_until_rest()
    if (
        (args.continue_on or args.restart)
        and final.pause is not None
        and final.pause.reason is PauseReason.SETUP_CHANGED
    ):
        if args.restart:
            rig.restart_on_setup(args.acknowledge)
        else:
            rig.continue_on_setup()
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
    db = HistoryDB() if args.db is None else HistoryDB(args.db)

    def say(line: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)

    try:
        identity = read_identity(db, smu, topology)
        rig = build_rig(args, topology, smu, lambda: get_backend("y-cruncher"), say, db, identity)
        install_signals(rig)
        return run(args, rig, say)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
