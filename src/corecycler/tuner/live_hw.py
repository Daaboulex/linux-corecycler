from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.engine import detector as kernel
from corecycler.engine.backends.base import OutcomeKind, StressConfig, StressMode
from corecycler.engine.execution import Lane, Supervisor, ThermalWatch, read_cpu_temperature
from corecycler.tuner.live import LiveRig, Store
from corecycler.tuner.machine.types import (
    Abort,
    Close,
    Evidence,
    Group,
    Klass,
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


def report_from(result, observed, lane_cpus: tuple[int, ...], core_of_cpu: dict[int, int]) -> Report:
    evidence = tuple(
        Evidence(core_of_cpu[event.cpu], event.corrected)
        for event in observed
        if isinstance(event, kernel.CoreMachineCheck) and event.cpu not in lane_cpus and event.cpu in core_of_cpu
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
    )


class YCruncherRunner:
    def __init__(self, backend_factory, work_dir: Path, core_of_cpu: dict[int, int], supervisor=Supervisor) -> None:
        self.backend_factory = backend_factory
        self.work_dir = work_dir
        self.core_of_cpu = core_of_cpu
        self.supervisor = supervisor

    def run(self, spec: TestSpec, stop: threading.Event) -> Report:
        settings = ONE_T if spec.klass is Klass.T1 else TWO_T
        config = StressConfig(mode=StressMode.CUSTOM, **settings)
        lane = Lane(core_id=spec.core, cpus=spec.cpus, work_dir=self.work_dir / f"core_{spec.core}")
        observed: list = []
        watch = kernel.ErrorDetector()
        watch.reset()
        try:
            supervisor = self.supervisor(
                backend=self.backend_factory(),
                detector=watch,
                thermal=ThermalWatch(max_temperature=95.0, grace_seconds=3.0, hard_margin=8.0, require_sensor=True),
                stop_event=stop,
                observed=observed,
                phase=f"{spec.stage.value} {spec.klass.value}",
            )
            result = supervisor.run([lane], config, spec.seconds)[spec.core]
        finally:
            watch.close()
        return report_from(result, observed, spec.cpus, self.core_of_cpu)


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
        runner=YCruncherRunner(backend_factory, args.state_dir / "runs", core_of_cpu),
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
