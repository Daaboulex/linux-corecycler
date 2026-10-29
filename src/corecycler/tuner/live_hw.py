from __future__ import annotations

import argparse
import json
import os
import platform
import random
import re
import secrets
import signal
import struct
import sys
import time
from collections import deque
from contextlib import nullcontext
from dataclasses import replace
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.cli import (
    EXIT_ABORTED,
    EXIT_ABORTED_UNREAD,
    EXIT_COMPLETED,
    EXIT_LOCKED,
    EXIT_PAUSED,
    EXIT_QUARANTINED,
    EXIT_REFUSED,
    EXIT_SIGNAL,
    EXIT_VALIDATION_ABORTED,
)
from corecycler.config import tools
from corecycler.config.paths import data_dir, heartbeat_path, invoking_uid
from corecycler.config.settings import load_settings
from corecycler.engine import detector as kernel
from corecycler.engine.backends import get_backend, ycruncher
from corecycler.engine.backends.base import OutcomeKind, StressConfig, StressMode
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.engine.containment import lane_cgroup_parent, lane_problem
from corecycler.engine.execution import Frozen, Heartbeat, HoldCap, Lane, Supervisor, ThermalWatch
from corecycler.engine.heavy_test_lock import HeavyTestRefused, take_heavy_test_lock
from corecycler.engine.memory_budget import MemoryBudgetUnavailable, memory_budget
from corecycler.history.context import read_bios_version
from corecycler.history.db import HistoryDB
from corecycler.monitor.cpu_usage import CPUUsageReader
from corecycler.monitor.frequency import Clocks, PowerState, fastest, read_power_state
from corecycler.monitor.hwmon import SensorRead, Sensors
from corecycler.smu.commands import detect_generation, get_commands, tjmax_c
from corecycler.smu.driver import SHAPER_BAND_NAMES, RyzenSMU, core_map_blocked, physical_ccds
from corecycler.smu.pmtable import (
    PMTableData,
    PMTableReader,
    core_reads,
    package_watts,
    rail_volts,
    read_power_limits,
)
from corecycler.smu.writer_lock import WriterBusy, co_writer
from corecycler.tuner import stages, telemetry
from corecycler.tuner.live import BootFacts, Identity, LiveRig, Store, config_from_json
from corecycler.tuner.machine.session import HELD
from corecycler.tuner.machine.types import (
    Abort,
    Close,
    Evidence,
    Group,
    Klass,
    Lifecycle,
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
    from collections.abc import Callable, Collection

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
TWO_T = {"tests": ("SFTv4", "N63", "VT3"), "seconds_per_test": 30, "memory_mb": 41, "binary": "19-ZN2 ~ Kagari"}
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
        *,
        vcache: str,
    ) -> None:
        self.smu = smu
        self.write = write
        self.say = say
        self.sensors = sensors
        self.cpu_root = cpu_root
        self.tools = tools
        self.budget = budget
        self.vcache = vcache

    def preflight(self) -> tuple[str | None, tuple[str, ...]]:
        return preflight(self.smu, self.sensors, self.tools, self.budget, self.power())

    def revert_problem(self) -> str | None:
        return revert_problem(self.smu)

    def read_co(self, core: int) -> int | None:
        return self.smu.get_co_offset(core)

    def write_co(self, core: int, value: int) -> str | None:
        if not self.write:
            self.say(f"dry: core {core} would be set to {value}")
            return None
        try:
            accepted = self.smu.set_co_offset(core, value)
        except (OSError, ValueError) as exc:
            return f"the SMU write raised {exc}"
        if not accepted:
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

    def boot_end(self, boot_id: str) -> kernel.BootEnd:
        return kernel.boot_end(boot_id)

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
            ("curve_shaper", shaper_fact(self.smu)),
            ("kernel", kernel_build()),
            ("microcode", microcode()),
            ("pbo", pbo),
            ("pm_table", pm_table_version()),
            ("python", platform.python_version()),
            ("qt", qt_version()),
            ("ryzen_smu", " ".join(smu)),
            ("smu_firmware", _setting(Path(SMU_DRIVER) / "version")),
            ("topology", f"online {_setting(self.cpu_root / 'online')}; {cores}"),
            ("vcache", self.vcache),
            ("zenpower", _module_version("zenpower")),
        )

    def boot_facts(self, boot_id: str) -> BootFacts:
        pm = PMTableReader().read()
        clocks = pm if pm is not None and pm.is_calibrated else None
        reasons, problem = kernel.reset_reasons(boot_id)
        return BootFacts(
            bios_version=read_bios_version(),
            fclk_mhz=None if clocks is None else clocks.fclk_mhz or None,
            uclk_mhz=None if clocks is None else clocks.uclk_mhz or None,
            mclk_mhz=None if clocks is None else clocks.mclk_mhz or None,
            reset_reasons=reasons,
            reset_problem=problem,
            igpu=igpu(),
        )

    def harvest(
        self, boot_id: str, since_ns: int | None, next_boot: bool, through_ns: int | None
    ) -> kernel.JournalHarvest:
        after = None if since_ns is None else kernel.HarvestFrom(mono_ns=since_ns, realtime_us=None)
        return kernel.harvest_after(boot_id, after, next_boot=next_boot, through_mono_ns=through_ns)

    def asleep_seconds(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def vcache_fact(topology) -> str:
    if not topology.is_x3d:
        return "none"
    if topology.vcache_ccd is None:
        return "unknown"
    return f"ccd {topology.vcache_ccd}"


def with_pm_table(
    sensors: Callable[[], SensorRead], pm_table: Callable[[], PMTableData | None], core_ids: Collection[int]
) -> Callable[[], SensorRead]:
    def read() -> SensorRead:
        reading = sensors()
        data = pm_table()
        if data is None:
            return reading
        return replace(
            reading,
            volts=(*reading.volts, *rail_volts(data)),
            watts=package_watts(data),
            cores=core_reads(data, core_ids),
        )

    return read


def shaper_words(bands: tuple[tuple[int, int, int], ...]) -> str:
    return ", ".join(
        f"{name} {'/'.join(map(str, margins))}" for name, margins in zip(SHAPER_BAND_NAMES, bands, strict=True)
    )


NO_SHAPER = "none on this generation"


def shaper_at_zero(fact: str | None) -> bool:
    return fact in (NO_SHAPER, shaper_words(((0, 0, 0),) * len(SHAPER_BAND_NAMES)))


def shaper_read(smu) -> tuple[tuple[tuple[int, int, int], ...] | None, str]:
    if smu.commands.get_curve_shaper_cmd is None:
        return None, NO_SHAPER
    bands = smu.get_curve_shaper()
    if bands is None:
        return None, "unreadable: the SMU refused the read or its band check failed"
    return bands, shaper_words(bands)


def shaper_fact(smu) -> str:
    return shaper_read(smu)[1]


def shaper_problem(smu) -> str | None:
    if smu.commands.get_curve_shaper_cmd is None or smu.get_curve_shaper() is not None:
        return None
    return "the BIOS Curve Shaper could not be read, so a session could not record the Shaper it tunes under"


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


def qt_version() -> str:
    try:
        return metadata.version("PySide6")
    except metadata.PackageNotFoundError:
        return "not installed"


def pm_table_version() -> str:
    data = PMTableReader().read()
    return "unreadable" if data is None else f"0x{data.pm_table_version:08x}"


def _module_version(name: str) -> str:
    if not (MODULES / name).exists():
        return "not loaded"
    return " ".join(_setting(MODULES / name / part) for part in ("version", "srcversion"))


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
SCHED_EXT = Path("/sys/kernel/sched_ext")
BACKGROUND_WINDOW_S = 5.0
BACKGROUND_BUSY_PERCENT = 5.0
EPP_SAVING = re.compile(r"\b(balance_)?power\b")


def background_load(wait=time.sleep) -> dict[int, float]:
    reader = CPUUsageReader()
    reader.read()
    wait(BACKGROUND_WINDOW_S)
    return reader.read()


def _others(proc: Path) -> list[Path]:
    return [pid for pid in proc.iterdir() if pid.name.isdigit() and int(pid.name) != os.getpid()]


SMU_TOOLS = frozenset({"ryzenadj", "ryzen_monitor", "ryzen_monitor_ng", "zenstates", "zenstates.py"})


def smu_holder(proc: Path = PROC) -> tuple[str | None, int]:
    by_name_only = 0
    for pid in _others(proc):
        try:
            name = (pid / "comm").read_text().strip()
            argv = (pid / "cmdline").read_bytes().split(b"\0")
        except FileNotFoundError, ProcessLookupError:
            continue
        if SMU_TOOLS & {name, *(Path(arg.decode(errors="replace")).name for arg in argv if arg)}:
            return (
                f"{name} (pid {pid.name}) is a known SMU tool, so its writes and this session's could collide",
                by_name_only,
            )
        try:
            targets = [os.readlink(fd) for fd in (pid / "fd").iterdir()]
        except FileNotFoundError, ProcessLookupError:
            continue
        except PermissionError:
            by_name_only += 1
            continue
        if any(target.startswith(SMU_DRIVER) for target in targets):
            return (
                f"{name} (pid {pid.name}) holds the SMU driver open, so its writes and this session's could collide",
                by_name_only,
            )
    return None, by_name_only


def sched_ext_note(root: Path = SCHED_EXT) -> str | None:
    try:
        state = (root / "state").read_text().strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"sched_ext state unreadable: {e}"
    if state != "enabled":
        return None
    try:
        name = (root / "root" / "ops").read_text().strip()
    except OSError as e:
        name = f"(name unreadable: {e})"
    return f"sched_ext scheduler {name} is running; tests run under it"


def scheduling_problem() -> str | None:
    policy = os.sched_getscheduler(0)
    if policy in (os.SCHED_IDLE, os.SCHED_BATCH):
        return "the tool runs under an idle or batch scheduling policy, so its own watch could be starved by the load"
    return None


def revert_problem(smu, proc: Path = PROC) -> str | None:
    """The first precondition reverting the CO needs that refuses: the core map, then no other SMU writer, whose
    mailbox writes could interleave with the revert's. An abort runs no test, so it checks these alone and never a
    gate of preflight only a test needs: the reverted CO is the safe state."""
    return core_map_blocked(smu) or smu_holder(proc)[0]


def preflight(
    smu, sensors, tools, budget, power: PowerState, proc: Path = PROC, sched_ext: Path = SCHED_EXT, load=background_load
) -> tuple[str | None, tuple[str, ...]]:
    """The first gate of L01 that refuses (in L01 order, later gates not run), and the warnings."""
    by_name_only: list[int] = []

    def holder() -> str | None:
        problem, count = smu_holder(proc)
        by_name_only.append(count)
        return problem

    gates = (
        lambda: core_map_blocked(smu),
        kernel.kernel_log_problem,
        kernel.mce_poll_problem,
        lane_problem,
        lambda: (
            None
            if sensors().hottest is not None
            else "no CPU temperature can be read, so nothing would stop a load that overheats"
        ),
        holder,
        scheduling_problem,
        lambda: shaper_problem(smu),
        *(
            lambda tool=tool: (
                None if tool.is_available() else f"{tool.name} is not installed: {tool.resolution().problem}"
            )
            for tool in tools
        ),
    )
    refusal = next((problem for gate in gates if (problem := gate()) is not None), None)
    warnings = []
    if by_name_only and by_name_only[0]:
        warnings.append(f"SMU tool check by name only for {by_name_only[0]} processes: their open files need root")
    scheduler = sched_ext_note(sched_ext)
    if scheduler is not None:
        warnings.append(scheduler)
    try:
        usable_mb = budget(1, 0).usable_mb  # equivalent-mutant: usable_mb ignores the instance count and launch minimum
    except MemoryBudgetUnavailable as exc:
        if refusal is None:
            refusal = f"the memory budget is unavailable: {exc}"
    else:
        hot_mb = stages.hot_memory_mb(usable_mb)
        if hot_mb < stages.HOT_MEMORY_CAP_MB:
            warnings.append(
                f"HOT would run y-cruncher with {hot_mb} MB now, under its {stages.HOT_MEMORY_CAP_MB} MB cap"
            )
    epp = dict(power.named()).get("EPP")
    if epp is not None and EPP_SAVING.search(epp):
        warnings.append(f"EPP is {epp}, which holds the clocks under performance")
    busy = [
        f"CPU {cpu} {percent:.0f} %" for cpu, percent in sorted(load().items()) if percent > BACKGROUND_BUSY_PERCENT
    ]
    if busy:
        warnings.append(
            f"background load above {BACKGROUND_BUSY_PERCENT:g} % over {BACKGROUND_WINDOW_S:g} s: {', '.join(busy)}"
        )
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


def ran_detail(detail: str, ran: float, wall: float) -> str:
    share = ran / wall if wall else 0.0
    said = f"the load ran {ran:.1f} of {wall:.1f} s ({share:.0%})"
    return f"{detail}; {said}" if detail else said


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
        self.last: float | None = None
        self.ring: deque[telemetry.Sample] = deque(maxlen=telemetry.RING)
        self.next_sample = 0.0

    def describe(self) -> str:
        _, cool = self.segment.hold
        name = "Tctl" if self.segment.hold_on_tctl else "the hottest input"
        last = "unread" if self.last is None else f"{self.last:.1f} C"
        return f"the hold releases below {cool:.1f} C on {name}, last read {last}"

    def __call__(self, elapsed: float, reading: SensorRead) -> Frozen:
        sampling = elapsed >= self.next_sample
        temperature = reading.tctl if self.segment.hold_on_tctl else reading.hottest
        self.last = temperature
        if self.segment.hold is not None and temperature is not None:
            hot, cool = self.segment.hold
            if temperature >= hot:
                self.held = True
            elif temperature < cool:
                self.held = False
        planned = Frozen.PLANNED if stages.frozen_at(self.segment, elapsed) else Frozen.NO
        heat = Frozen.HEAT if self.held else planned
        frozen = Frozen.UNREAD if self.segment.hold_on_tctl and temperature is None else heat
        if sampling:
            self.next_sample = elapsed + self.every
            clocks = self.clocks()
            per_core = ((core, fastest(clocks, cpus)) for core, cpus in sorted(self.cpus_of.items()))
            sample = telemetry.Sample(
                frozen is Frozen.NO,
                tuple((core, mhz) for core, mhz in per_core if mhz is not None),
                reading.tctl,
                time.monotonic_ns(),
                reading.tccd,
                reading.volts,
                reading.watts,
                reading.cores,
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
        say: Callable[[str], None] = print,
        heartbeat: Heartbeat | None = None,
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
        self.say = say
        self.heartbeat = heartbeat or Heartbeat()
        self.read_ns: int | None = None
        self.sink: Callable[[telemetry.Sample], None] | None = None
        self.started_ns = kernel.monotonic_ns()
        self.kernel_events: list[kernel.KernelEvent] = []

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
        hold_cap: HoldCap | None = None,
        must_finish_a_test: bool = False,
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
                settle_s=kernel.mce_settle_s(),
                poll_interval=poll,
                phase=f"{spec.stage.value} {(spec.load or spec.klass).value}",
                freeze=freeze,
                hold_cap=hold_cap,
                must_finish_a_test=must_finish_a_test,
                heartbeat=self.heartbeat,
            )
            results = supervisor.run(lanes, config, seconds)
            self.read_ns = watch.read_started_ns
        finally:
            watch.close()
        self.kernel_events.extend(observed)
        return results, observed

    def run(
        self, spec: TestSpec, stop: threading.Event, sink: Callable[[telemetry.Sample], None] | None = None
    ) -> Report:
        if spec.thermal_limit_c is None:
            raise ValueError("a test spec carries no thermal limit, so its run would have no thermal stop")
        self.read_ns = None
        self.sink = sink
        self.started_ns = kernel.monotonic_ns()
        self.kernel_events = []
        report = self._run(spec, stop)
        faults = [event for event in self.kernel_events if isinstance(event, kernel.ProgramFault)]
        seen = kernel.ProgramFaults(self.started_ns, *kernel.kept_faults(faults))
        return replace(report, kernel_read_ns=self.read_ns, program_faults=seen)

    def _run(self, spec: TestSpec, stop: threading.Event) -> Report:
        if spec.load is Load.SOAK:
            return self._soak(spec, stop)
        if spec.load in (None, Load.ONE_T, Load.TWO_T):
            settings = ONE_T if spec.klass is Klass.T1 else TWO_T
            frozen = stages.record_freezes(spec.seconds) if spec.klass is Klass.T1 else ()
            config = StressConfig(mode=StressMode.CUSTOM, longest_rest_s=stages.longest_rest(frozen), **settings)
            lane = Lane(core_id=spec.core, cpus=spec.cpus, work_dir=self.work_dir / f"core_{spec.core}")
            sampler = None
            if self.sink is not None or frozen:
                flat = stages.Segment(
                    spec.cpus,
                    settings["tests"],
                    settings["seconds_per_test"],
                    settings["memory_mb"],
                    spec.seconds,
                    frozen,
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
        if spec.plan_seed is None:
            raise ValueError(f"the {spec.load.value} test carries no plan seed, so its pattern could not be replayed")
        rest_c = self.sensors().tctl if spec.load is Load.WARM else None
        if spec.load is Load.WARM and rest_c is None:
            unread = "Tctl could not be read before the warm load, so its hold has no release point"
            return Report(Group.APPARATUS, 0.0, detail=unread)
        rng = random.Random(spec.plan_seed)
        segments = stages.plan(
            spec.load,
            spec.seconds,
            spec.cpus,
            firsts,
            self.budget(1, 0).usable_mb,  # equivalent-mutant: usable memory ignores both arguments
            rng,
            spec.thermal_limit_c,
            quick=spec.quick,
            rest_c=rest_c,
        )
        memory = ", ".join(str(mb) for mb in sorted({segment.memory_mb for segment in segments}))
        self.say(f"{spec.load.value} runs y-cruncher with {memory} MB")
        done = 0.0
        ran = 0.0
        speeds: list[tuple[str, float, bool]] = []
        for segment in segments:
            config = StressConfig(
                mode=StressMode.CUSTOM,
                tests=segment.tests,
                seconds_per_test=segment.seconds_per_test,
                memory_mb=segment.memory_mb,
                longest_rest_s=stages.longest_rest(segment.frozen),
            )
            lane = Lane(core_id=spec.core, cpus=segment.cpus, work_dir=self.work_dir / f"all_{spec.load.value}")
            hold = Hold(segment, self.clocks, self.core_of_cpu, telemetry.SAMPLE_S, self.sink)
            cap = HoldCap(segment.hold_cap_s, hold.describe, segment.hold_timeout, segment.hold_in_wall_time)
            results, observed = self._supervise(
                spec,
                stop,
                [lane],
                config,
                segment.duration,
                hold,
                poll=FREEZE_TICK_S,
                hold_cap=cap,
                must_finish_a_test=segment.must_finish_a_test,
            )
            result = results[lane.core_id]
            done += result.duration_seconds
            ran += result.duration_seconds - result.frozen_seconds
            speeds.extend(result.speeds)
            if result.outcome.kind is not OutcomeKind.PASS or observed:
                assigned = tuple(sorted({self.core_of_cpu[c] for c in segment.cpus}))
                named = named_cores(result, self.core_of_cpu)
                steady = spec.load is Load.HOT
                context = telemetry.context(list(hold.ring), steady, assigned, named) if hold.ring else None
                report = report_from(result, observed, segment.cpus, self.core_of_cpu, context, all_core=True)
                if result.outcome.kind is not OutcomeKind.PASS or report.evidence:
                    detail = ran_detail(report.detail, ran, done)
                    return replace(report, seconds=done, named=named, speeds=tuple(speeds), detail=detail)
        return Report(Group.PASS, done, detail=ran_detail("", ran, done), speeds=tuple(speeds))

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
        beats = self.heartbeat
        try:
            started = time.monotonic()
            end = started + spec.seconds
            while time.monotonic() < end and not stop.wait(min(1.0, max(0.0, end - time.monotonic()))):
                beats.running("soak watch", time.monotonic())
                read = watch.read_new()
                observed.extend(read.events)
                problem = problem or read.problem
            watched = time.monotonic() - started
            stop.wait(kernel.mce_settle_s())
            read = watch.read_new(final=True)
            observed.extend(read.events)
            problem = problem or read.problem
            self.read_ns = watch.read_started_ns
        finally:
            beats.ended(time.monotonic())
            watch.close()
        self.kernel_events.extend(observed)
        evidence = tuple(
            Evidence(self.core_of_cpu[e.cpu], e.corrected)
            for e in observed
            if isinstance(e, kernel.CoreMachineCheck) and e.cpu in self.core_of_cpu
        )
        if stop.is_set():
            return Report(Group.APPARATUS, watched, evidence, "the soak was stopped before its end")
        if problem is not None:
            return Report(Group.APPARATUS, watched, evidence, f"the kernel log could not be read: {problem}")
        unblamed = any(isinstance(e, (kernel.UnblamedMachineCheck, kernel.KernelTrouble)) for e in observed)
        group = Group.ENVIRONMENT if unblamed else Group.PASS
        return Report(group, watched, evidence, "soak watch")


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m corecycler.tuner.live_hw")
    parser.add_argument("--state-dir", type=Path, default=data_dir() / "tuner")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--session", type=int)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true")
    action.add_argument("--resume", action="store_true")
    action.add_argument("--validate", action="store_true")
    action.add_argument("--continue", dest="continue_on", action="store_true")
    action.add_argument("--restart", action="store_true")
    action.add_argument("--abort", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--cores")
    parser.add_argument("--max-offset", dest="max_offset", type=int)
    parser.add_argument("--k1", dest="k_1t", type=int)
    parser.add_argument("--k2", dest="k_2t", type=int)
    parser.add_argument("--wait-between-cores", dest="wait_between_cores_s", type=float)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--acknowledge", action="store_true")
    parser.add_argument("--reengage", action="store_true")
    args = parser.parse_args(argv)
    for named, flag in (
        ("resume", "resume"),
        ("validate", "validate"),
        ("continue_on", "continue"),
        ("restart", "restart"),
        ("abort", "abort"),
    ):
        if getattr(args, named) and args.session is None:
            parser.error(f"--{flag} needs --session")
    shorthand = [name for name in ("cores", *SHORTHAND) if getattr(args, name) is not None]
    if not args.start and (args.config is not None or shorthand):
        parser.error("--config, --cores and the configuration flags go with --start only")
    if args.start and args.config is not None and shorthand:
        parser.error("--config holds the whole configuration; leave out --cores and the configuration flags")
    if args.start and args.config is None and args.cores is None:
        parser.error("--start needs --config FILE or --cores")
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
    check_interval_s: int,
) -> LiveRig:
    cpus_of = tuple(sorted((core, tuple(info.logical_cpus)) for core, info in topology.cores.items()))
    domain_of = tuple(sorted((core, info.ccd) for core, info in topology.cores.items()))
    core_of_cpu = {cpu: core for core, cpus in cpus_of for cpu in cpus}
    sensors = with_pm_table(Sensors().read, PMTableReader().read, topology.cores)
    beats = Heartbeat(heartbeat_path(invoking_uid()), db.path.absolute().parent)
    return LiveRig(
        hardware=SmuHardware(
            smu,
            args.write,
            say,
            sensors,
            tools=(backend_factory(), get_backend("stressapptest")),
            vcache=vcache_fact(topology),
        ),
        runner=StressRunner(
            backend_factory,
            args.state_dir / "runs",
            core_of_cpu,
            sensors,
            Clocks().read,
            say=say,
            heartbeat=beats,
        ),
        store=Store(db, args.state_dir, identity),
        cpus_of=cpus_of,
        domain_of=domain_of,
        physical_ccd_of=tuple(sorted(physical_ccds(smu, topology).items())),
        co_range=smu.commands.co_range,
        tjmax_c=tjmax_c(topology.model_name),
        say=say,
        two_t_problem=backend_factory().binary_problem(TWO_T["binary"]),
        clock=time.monotonic,
        check_interval_s=check_interval_s,
        heartbeat=beats,
    )


def install_signals(rig: LiveRig) -> None:
    signal.signal(signal.SIGINT, lambda *_: rig.signals.append(Close()))
    signal.signal(signal.SIGTERM, lambda *_: rig.signals.append(Close()))
    signal.signal(signal.SIGHUP, lambda *_: rig.signals.append(Close()))
    signal.signal(signal.SIGUSR1, lambda *_: rig.signals.append(PauseRequest()))
    signal.signal(signal.SIGUSR2, lambda *_: rig.signals.append(Abort()))


SHORTHAND = ("max_offset", "k_1t", "k_2t", "wait_between_cores_s")

EXIT_OF = {
    Lifecycle.COMPLETED: EXIT_COMPLETED,
    Lifecycle.PAUSED: EXIT_PAUSED,
    Lifecycle.QUARANTINED: EXIT_QUARANTINED,
    Lifecycle.ABORTED: EXIT_ABORTED,
    Lifecycle.IDLE: EXIT_SIGNAL,
}

OUTCOME_NOTES = {
    EXIT_COMPLETED: ("Tuning complete", "The session finished and confirmed a profile.", "normal"),
    EXIT_PAUSED: ("Tuning paused", "The tuner stopped for attention: check the log.", "critical"),
    EXIT_QUARANTINED: (
        "Tuning quarantined",
        "The machine crashed again and again with no test surviving; every owned core is sent back to stock. "
        "The causes and any failed write are in the log.",
        "critical",
    ),
    EXIT_ABORTED: (
        "Tuning aborted",
        "Every owned core is sent back to its baseline. A failed write is in the log.",
        "critical",
    ),
    EXIT_VALIDATION_ABORTED: (
        "Validation aborted",
        "The validation stopped before it ended; the session stays completed and its values are sent back. "
        "A failed write is in the log.",
        "normal",
    ),
    EXIT_ABORTED_UNREAD: (
        "Tuning aborted unread",
        "The session could not be read, so it ended aborted by its status alone. The log says which cores its CO "
        "journal named went to stock, or why no CO was written; then reboot or check the CO before tuning again.",
        "critical",
    ),
}


def start_config(args: argparse.Namespace, domain_of: tuple[tuple[int, int | None], ...]) -> MachineConfig:
    if args.config is not None:
        return config_from_json(json.loads(args.config.read_text()))
    selected = tuple(int(core) for core in args.cores.split(",") if core)
    given = {name: getattr(args, name) for name in SHORTHAND if getattr(args, name) is not None}
    return MachineConfig(Mode.CO, default_method(selected, dict(domain_of)), selected, **given)


def run(args: argparse.Namespace, rig: LiveRig, say: Callable[[str], None]) -> int:
    try:
        return _run(args, rig, say)
    except BaseException:
        if rig.state.lifecycle in HELD:
            say(
                "the driver stopped on an unexpected error: closing the session, "
                "every owned core goes back to its baseline"
            )
            with rig.heartbeat.driven():
                rig.post(Close())
        raise


def _run(args: argparse.Namespace, rig: LiveRig, say: Callable[[str], None]) -> int:
    if args.start:
        try:
            config = start_config(args, rig.domain_of)
        except (OSError, ValueError, KeyError, TypeError) as e:
            say(f"refused: the start configuration: {e}")
            return EXIT_REFUSED
        session = rig.store.new_session_id()
        seed, origin = args.seed, "given"
        if seed is None:
            seed, origin = secrets.randbelow(1 << 31), "drawn at Start"  # equivalent-mutant: any positive bound works
        rig.start(config, session, seed, args.acknowledge)
        if rig.refused is None:
            say(f"order seed {seed}, {origin}")
    elif args.validate:
        rig.validate(args.session)
    elif args.abort:
        if rig.resume_for_abort(args.session, args.reengage) and rig.refused is None:
            return EXIT_ABORTED_UNREAD
    else:
        rig.resume(args.session, args.reengage, carry=args.resume or args.continue_on)
    if rig.refused is not None:
        return EXIT_REFUSED
    if args.abort:
        rig.post(Abort())
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
        if rig.refused is not None:
            return EXIT_REFUSED
        final = rig.run_until_rest()
    reason = f" ({final.pause.reason.value}: {final.pause.detail})" if final.pause is not None else ""
    say(f"rest: {final.lifecycle.value}{reason}")
    say("values: " + ", ".join(f"{r.core}={r.value} ({r.phase.value}, fb {r.fb})" for r in final.cores))
    if final.lifecycle is Lifecycle.COMPLETED and rig.abort_taken:
        return EXIT_VALIDATION_ABORTED
    return EXIT_OF[final.lifecycle]


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as stop:
        return EXIT_REFUSED if stop.code else EXIT_COMPLETED
    try:
        with co_writer(record=True), nullcontext() if args.abort else take_heavy_test_lock("corecycler-tuner"):
            code = _drive(args)
    except WriterBusy as busy:
        print(f"live: {busy}", file=sys.stderr)
        return EXIT_LOCKED
    except HeavyTestRefused as refused:
        print(f"live: {refused}", file=sys.stderr)
        return EXIT_REFUSED
    notify_outcome(code)
    return code


def notify_outcome(code: int) -> None:
    note = OUTCOME_NOTES.get(code)
    if note is None:
        return
    try:
        if not load_settings().notify_on_completion:
            return
        from corecycler.notify import desktop_notify

        title, body, urgency = note
        desktop_notify(title, body, urgency=urgency)
    except Exception as e:
        print(f"notification failed: {e}", file=sys.stderr)


def build_smu(topology):
    commands = get_commands(detect_generation(topology.family, topology.model, topology.model_name))
    if commands is None or not commands.has_co or not RyzenSMU.is_available():
        return None
    smu = RyzenSMU(commands)
    smu.set_topology(topology)
    map_err = core_map_blocked(smu)
    if map_err is not None:
        print(f"corecycler: per-core CO disabled: {map_err}", file=sys.stderr)
        return None
    return smu


def _drive(args: argparse.Namespace) -> int:
    from corecycler.engine.backends import get_backend, load_all
    from corecycler.engine.topology import detect_topology

    tools.load_configured_paths()
    topology = detect_topology()
    smu = build_smu(topology)
    if smu is None:
        print("live: the SMU or its core map is unavailable", file=sys.stderr)
        return EXIT_REFUSED
    load_all()
    db = HistoryDB() if args.db is None else HistoryDB(args.db)

    def say(line: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)

    try:
        identity = read_identity(db, smu, topology)
        with kernel.fast_mce_poll(say) as check_interval_s:
            rig = build_rig(args, topology, smu, lambda: get_backend("y-cruncher"), say, db, identity, check_interval_s)
            install_signals(rig)
            return run(args, rig, say)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
