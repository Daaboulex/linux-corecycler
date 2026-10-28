"""y-cruncher stress test backend."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.engine import topology
from corecycler.engine.backends import register_backend

from .base import LaneSignal, Outcome, StressBackend, StressConfig, StressMode, scan

if TYPE_CHECKING:
    from .base import FindingTable

CONFIG_FILE = "stress.cfg"
_STARTUP_OPTIONS = ("skip-warnings", "pause:-2", "colors:0", "status:none")
_DEFAULT_MEMORY_MIB = 1024
_PER_TEST_SECONDS = 30

ALL_TESTS: tuple[str, ...] = ("BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3")

MODE_TO_ALGORITHMS: dict[StressMode, tuple[str, ...]] = {
    StressMode.SSE: ("BKT",),
    StressMode.AVX: ("BKT", "BBP", "SFTv4", "SNT", "SVT"),
    StressMode.AVX2: ALL_TESTS,
    StressMode.AVX512: ALL_TESTS,
    StressMode.CUSTOM: ALL_TESTS,
}

_A64 = frozenset({"lm", "pni"})
_NHM = _A64 | {"ssse3", "sse4_1"}
_SNB = _NHM | {"sse4_2", "avx"}
_BD2 = _SNB | {"abm", "fma"}
_HSW = _BD2 | {"bmi1", "bmi2", "avx2"}
_BDW = _HSW | {"adx"}
_SKX = _BDW | {"avx512f", "avx512cd", "avx512vl", "avx512bw", "avx512dq"}
_CNL = _SKX | {"avx512ifma", "avx512vbmi"}
_ZN4 = _CNL | {"gfni", "avx512_vbmi2"}
_EITHER_VENDOR = (("11-SNB ~ Hina", _SNB), ("08-NHM ~ Ushio", _NHM), ("05-A64 ~ Kasumi", _A64))

LAUNCHER_ORDER: dict[str, tuple[tuple[str, frozenset[str]], ...]] = {
    "AuthenticAMD": (
        ("24-ZN5 ~ Komari", _ZN4 | {"avx512_vp2intersect"}),
        ("22-ZN4 ~ Kizuna", _ZN4),
        ("19-ZN2 ~ Kagari", _BDW | {"rdpid"}),
        ("17-ZN1 ~ Yukina", _BDW),
        ("12-BD2 ~ Miyu", _BD2),
        *_EITHER_VENDOR,
    ),
    "GenuineIntel": (
        ("18-CNL ~ Shinoa", _CNL),
        ("17-SKX ~ Kotori", _SKX),
        ("14-BDW ~ Kurumi", _BDW),
        ("13-HSW ~ Airi", _HSW),
        *_EITHER_VENDOR,
    ),
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_NAMED_CPU_RE = re.compile(r"Error\(s\) encountered on logical core (\d+)\.")
_SPEED_RE = re.compile(r"Running (\S+): (Passed|Failed)\s+Test Speed:\s+([0-9.]+) \* 10\^(\d+)\s+(?:bits|terms) / sec")
_FINISHED_RE = re.compile(r"Running \S+: (?:Passed|Failed)")

FINDINGS: FindingTable = (
    *(
        (re.compile(pattern, re.IGNORECASE), Outcome.COMPUTATION)
        for pattern in (
            r"Error\(s\) encountered",
            r"AlgorithmFailedException",
            r"StressTestFailedException",
            r"Running \w+: Failed",
            r"Stress test failed with \d+ error",
            r"Coefficient is too large",
        )
    ),
    *(
        (re.compile(pattern, re.IGNORECASE), Outcome.NOT_STARTED)
        for pattern in (
            r"Invalid Parameter",
            r"Invalid core id",
            r"InvalidParametersException",
            r"KeyNotFoundException",
            r"Unknown Test: ",
            r"Invalid core #: ",
        )
    ),
)


def named_cpus(output: str) -> tuple[int, ...]:
    return tuple(sorted({int(cpu) for cpu in _NAMED_CPU_RE.findall(_ANSI_RE.sub("", output))}))


def named_binary(binaries_dir: Path, name: str, cpu_flags: frozenset[str], vendor: str) -> Path:
    required = dict(LAUNCHER_ORDER.get(vendor, ())).get(name)
    if required is None:
        raise OSError(f"y-cruncher has no binary named {name!r} for {vendor or 'an unknown vendor'}")
    missing = sorted(required - cpu_flags)
    if missing:
        raise OSError(f"the y-cruncher binary {name!r} needs CPU flags this CPU lacks: {missing}")
    candidate = binaries_dir / name
    if not candidate.is_file():
        raise OSError(f"the y-cruncher binary {name!r} is not in {binaries_dir}")
    return candidate


def compute_binary(binaries_dir: Path, cpu_flags: frozenset[str], vendor: str) -> Path | None:
    for name, required in LAUNCHER_ORDER.get(vendor, ()):
        candidate = binaries_dir / name
        if required <= cpu_flags and candidate.is_file():
            return candidate
    return None


def _binaries_dir(launcher: Path) -> Path:
    candidates = (launcher.parent / "Binaries", launcher.parent.parent / "lib" / "y-cruncher" / "Binaries")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise OSError(f"no y-cruncher Binaries directory at {candidates[0]} or {candidates[1]}")


def _stress_config(config: StressConfig) -> str:
    memory_mib = config.memory_mb if config.memory_mb and config.memory_mb > 0 else _DEFAULT_MEMORY_MIB
    tests = config.tests or MODE_TO_ALGORITHMS[config.mode]
    unknown = [tag for tag in tests if tag not in ALL_TESTS]
    if unknown:
        raise ValueError(f"y-cruncher has no test named {unknown}; it offers {list(ALL_TESTS)}")
    seconds = config.seconds_per_test if config.seconds_per_test else _PER_TEST_SECONDS
    lines = [
        "{",
        '    Action : "StressTest"',
        "    StressTest : {",
        '        AllocateLocally : "true"',
        f"        LogicalCores : [{' '.join(str(cpu) for cpu in config.cpus)}]",
        f"        TotalMemory : {memory_mib * 1024 * 1024}",
        f"        SecondsPerTest : {seconds}",
        "        SecondsTotal : 0",
        '        StopOnError : "true"',
        "        Tests : [",
        *(f'            "{tag}"' for tag in tests),
        "        ]",
        "    }",
        "}",
    ]
    return "\n".join(lines) + "\n"


@register_backend("y-cruncher")
class YCruncherBackend(StressBackend):
    name = "y-cruncher"
    _compute: Path | None = None

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        if not config.cpus:
            raise RuntimeError("y-cruncher pins one thread per lane CPU, and this lane names no CPUs")
        if config.binary:
            compute = self._named(config.binary)
        else:
            vendor, flags = topology.common_cpu_flags()
            binaries = _binaries_dir(Path(self.require_binary()))
            compute = compute_binary(binaries, flags, vendor)
            if compute is None:
                raise OSError(f"no y-cruncher binary for this CPU ({vendor or 'unknown vendor'}) in {binaries}")
        (work_dir / CONFIG_FILE).write_text(_stress_config(config))
        self._compute = compute

    def _named(self, name: str) -> Path:
        vendor, flags = topology.common_cpu_flags()
        return named_binary(_binaries_dir(Path(self.require_binary())), name, flags, vendor)

    def binary_problem(self, name: str) -> str | None:
        try:
            self._named(name)
        except (OSError, RuntimeError) as exc:
            return str(exc)
        return None

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        if self._compute is None:
            raise RuntimeError("y-cruncher has no compute binary yet: prepare chooses it for this CPU")
        return [str(self._compute), *_STARTUP_OPTIONS, "config", str(work_dir / CONFIG_FILE)]

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def findings(self, output: str, work_dir: Path) -> tuple[LaneSignal, ...]:
        return scan(_ANSI_RE.sub("", output), FINDINGS)

    def progress_window(self, config: StressConfig) -> float | None:
        """One test by y-cruncher's own clock, which runs through a freeze, the longest planned rest that clock can run
        out in, and one more test for the iteration it then finishes; none when a rest can last a whole test, since
        that rest can fill the iteration's allowance with no run time."""
        seconds = config.seconds_per_test if config.seconds_per_test else _PER_TEST_SECONDS
        if config.longest_rest_s >= seconds:
            return None
        return 2.0 * seconds + config.longest_rest_s

    def finished(self, output: str) -> int:
        return len(_FINISHED_RE.findall(_ANSI_RE.sub("", output)))

    def speeds(self, output: str) -> tuple[tuple[str, float, bool], ...]:
        return tuple(
            (test, float(mantissa) * 10 ** int(exponent), verdict == "Passed")
            for test, verdict, mantissa, exponent in _SPEED_RE.findall(_ANSI_RE.sub("", output))
        )
