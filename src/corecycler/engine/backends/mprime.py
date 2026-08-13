"""mprime (Prime95 CLI) stress test backend."""

from __future__ import annotations

import os
import re
import textwrap
from typing import TYPE_CHECKING

from corecycler.engine.backends import register_backend

from .base import (
    CRASH_SIGNALS,
    KILLED_BY_US_CODES,
    FFTPreset,
    StressBackend,
    StressConfig,
    StressMode,
)

if TYPE_CHECKING:
    from pathlib import Path

# FFT ranges in K for each preset (Prime95 30.x conventions)
FFT_RANGES: dict[FFTPreset, tuple[int, int]] = {
    FFTPreset.SMALLEST: (4, 21),
    FFTPreset.SMALL: (36, 248),
    FFTPreset.LARGE: (426, 8192),
    FFTPreset.HUGE: (8960, 65536),
    FFTPreset.ALL: (4, 65536),
    FFTPreset.MODERATE: (1344, 4096),
    FFTPreset.HEAVY: (4, 1344),
    FFTPreset.HEAVY_SHORT: (4, 160),
}

# Instruction-set selection, verified against mprime 31.04b02 live (2026-08-11):
# these CpuSupports* overrides steer the FFT implementation (SSE -> Pentium4
# type-1, AVX -> AVX, AVX2 -> FMA3, AVX512 -> AVX-512); TortureWeak is not an
# mprime option. CpuSupportsAVX512F is the real key name, not CpuSupportsAVX512.
MODE_TO_CPU_FLAGS: dict[StressMode, dict[str, int]] = {
    StressMode.SSE: {
        "CpuSupportsAVX": 0,
        "CpuSupportsFMA3": 0,
        "CpuSupportsFMA4": 0,
        "CpuSupportsAVX2": 0,
        "CpuSupportsAVX512F": 0,
    },
    StressMode.AVX: {
        "CpuSupportsAVX": 1,
        "CpuSupportsFMA3": 0,
        "CpuSupportsFMA4": 0,
        "CpuSupportsAVX2": 0,
        "CpuSupportsAVX512F": 0,
    },
    StressMode.AVX2: {
        "CpuSupportsAVX": 1,
        "CpuSupportsFMA3": 1,
        "CpuSupportsFMA4": 0,
        "CpuSupportsAVX2": 1,
        "CpuSupportsAVX512F": 0,
    },
    StressMode.AVX512: {
        "CpuSupportsAVX": 1,
        "CpuSupportsFMA3": 1,
        "CpuSupportsFMA4": 0,
        "CpuSupportsAVX2": 1,
        "CpuSupportsAVX512F": 1,
    },
}

VERIFIED_MPRIME_VERSIONS: frozenset[str] = frozenset({"31.4"})

_VERSION_RE = re.compile(r"\bv(\d+\.\d+)")

# Fatal error signatures, verified against the Prime95 30.19b20 source
# (commonb.c SELFFAIL*/ERRMSG* constants; torture-test errors are written to
# BOTH stdout and results.txt via OutputBoth). "Worker stopped." is Prime95's
# BENIGN graceful-stop line and must never be treated as an error.
FATAL_PATTERNS: list[str] = [
    # torture test (SELFFAIL*): covers "FATAL ERROR: Rounding was ...",
    # "FATAL ERROR: Final result was ..." and the <=30.8 "Resulting sum" form
    r"FATAL ERROR",
    r"ERROR: ILLEGAL SUMOUT",  # SELFFAIL1 / ERRMSG1A
    r"Possible hardware failure",  # SELFFAIL4 / ERRMSG2
    r"Hardware failure detected",  # SELFFAIL5 (FFT-size form >=30.7)
    r"Maximum number of warnings exceeded",  # SELFFAIL6
    r"TORTURE TEST FAILED",  # SELFFAIL7 (>=30.19)
    # torture summary with a nonzero error count (stdout)
    r"Torture Test completed .* - [1-9]\d* errors",
    # production-work error lines that can also land in results.txt (ERRMSG1*)
    r"ERROR: SUM\(INPUTS\) != SUM\(OUTPUTS\)",  # ERRMSG1B (<=30.8)
    r"ERROR: Shift counter corrupt",
    r"ERROR: Illegal double encountered",
    r"ERROR: FFT data has been zeroed",
    r"ERROR: Jacobi error check failed",
    # roundoff-timing warnings (commonb.c OutputBoth calls)
    r"Warning: ILLEGAL SUMOUT",
    r"Warning: SUMOUT MISMATCH",
]


@register_backend("mprime")
class MprimeBackend(StressBackend):
    name = "mprime"

    def __init__(self) -> None:
        super().__init__()
        self._last_work_dir: Path | None = None

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        return [self.require_binary(), "-t", "-W" + str(work_dir)]

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def get_supported_fft_presets(self) -> list[FFTPreset]:
        return list(FFTPreset)

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        self._last_work_dir = work_dir

        # A stale results.txt from an earlier run (abort, hard crash, or a
        # preserved failure that was never renamed) would be re-read by
        # parse_output/poll_errors and turn every subsequent test into a
        # false FAIL at full duration. Each run must start with a clean slate.
        for leftover in ("results.txt", "prime.log", "prime.spl"):
            p = work_dir / leftover
            if p.exists():
                p.unlink()

        # determine FFT range
        if config.fft_preset == FFTPreset.CUSTOM and config.fft_min and config.fft_max:
            fft_min, fft_max = config.fft_min, config.fft_max
        else:
            fft_min, fft_max = FFT_RANGES.get(config.fft_preset, (4, 8192))

        cpu_flags = MODE_TO_CPU_FLAGS.get(config.mode, MODE_TO_CPU_FLAGS[StressMode.SSE])
        flags_block = "".join(f"{key}={value}\n" for key, value in cpu_flags.items())

        # NumCPUs=1 + CoresPerTest=1 keep mprime to one worker instead of one
        # per detected core; EnableSetAffinity=0 stops it re-pinning its
        # threads to core 0's SMT pair, so the load stays where it was placed.
        local_txt = work_dir / "local.txt"
        local_txt.write_text(
            textwrap.dedent(f"""\
                ErrorCheck=1
                SumInputsErrorCheck=1
                V30OptionsConverted=1
                StressTester=1
                UsePrimenet=0
                NumCPUs=1
                CoresPerTest=1
                MinTortureFFT={fft_min}
                MaxTortureFFT={fft_max}
                TortureHyperthreading={1 if config.threads > 1 else 0}
                TortureThreads={config.threads}
            """)
            + flags_block
        )

        prime_txt = work_dir / "prime.txt"
        prime_txt.write_text(
            textwrap.dedent(f"""\
                V30OptionsConverted=1
                StressTester=1
                UsePrimenet=0
                NumCPUs=1
                CoresPerTest=1
                MinTortureFFT={fft_min}
                MaxTortureFFT={fft_max}
                TortureHyperthreading={1 if config.threads > 1 else 0}
                TortureThreads={config.threads}
            """)
            + flags_block
            + "EnableSetAffinity=0\n"
        )
        # Output files stay at Prime95's defaults (results.txt / prime.log in
        # the work dir). The real override keys are literally "results.txt="
        # and "prime.log=" (commonc.c); ResultsFile=/LogFile= are never read
        # by mprime.

    @staticmethod
    def parse_version(text: str) -> str | None:
        match = _VERSION_RE.search(text)
        return match.group(1) if match else None

    def installed_version(self) -> str | None:
        import subprocess

        try:
            result = subprocess.run(
                [self.require_binary(), "-v"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            return None
        return self.parse_version(result.stdout + result.stderr)

    def assert_prepared(self, work_dir: Path) -> None:
        for name in ("local.txt", "prime.txt"):
            path = work_dir / name
            if not path.is_file() or not os.access(path, os.R_OK):
                raise OSError(
                    f"refusing to launch mprime: {path} is missing or unreadable, "
                    "and mprime without config spawns one self-pinned worker per core"
                )

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = stdout + "\n" + stderr

        # also check results.txt if available (mprime writes errors there)
        if self._last_work_dir:
            results_file = self._last_work_dir / "results.txt"
            if results_file.exists():
                try:
                    combined += "\n" + results_file.read_text()
                except OSError as e:
                    # Fail closed: without results.txt a real error could pass
                    # unseen. This is an apparatus fault, not a verdict — the
                    # engine pauses on it instead of advancing the search.
                    return False, f"Failed to read results.txt ({e}) — verdict unavailable"

        for pattern in FATAL_PATTERNS:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return False, f"mprime error: {match.group(0)}"

        # A crash signal means the worker died from instability — this OVERRIDES any
        # earlier "Self-test N passed" line in the output (the process passed some
        # iterations and then crashed, which is still a failure). Checked before the
        # success patterns so a crash is never masked by a prior pass.
        if returncode in CRASH_SIGNALS:
            return False, f"mprime crashed with {CRASH_SIGNALS[returncode]} (exit {returncode})"

        # if process was killed (by us, timeout) with no errors, consider it passed
        if returncode in KILLED_BY_US_CODES:
            return True, None

        # Successful iterations — the real line is "Self-test 4K passed!"
        # (K-suffixed FFT size, optional "(thread N of M)"), commonb.c SELFPASS.
        if re.search(r"Self-test \d+K?.* passed!", combined):
            return True, None
        if re.search(r"Torture Test completed \d+ tests", combined):
            return True, None

        # unknown state — check return code
        if returncode != 0:
            return False, f"mprime exited with code {returncode}"

        return True, None

    def poll_errors(self, work_dir: Path) -> str | None:
        """Scan results.txt for fatal errors while the test is running.

        mprime torture tests keep running after a computation error (the error
        lands only in results.txt), so without live polling a soft failure is
        detected only at the end-of-test parse — burning the full test duration.
        prepare() guarantees the file belongs to the current run.
        """
        results_file = work_dir / "results.txt"
        if not results_file.exists():
            return None
        try:
            content = results_file.read_text()
        except OSError:
            return None
        for pattern in FATAL_PATTERNS:
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                return f"mprime error: {match.group(0)}"
        return None

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        # On failure, preserve results.txt/prime.log for post-mortem — but RENAMED,
        # so a later run in the same work dir can never re-parse the old error as
        # its own.
        if preserve_on_error:
            for f in ("results.txt", "prime.log"):
                p = work_dir / f
                if p.exists():
                    p.replace(work_dir / f"failed-{f}")
        for f in ("prime.txt", "local.txt", "prime.log", "results.txt", "prime.spl"):
            p = work_dir / f
            if p.exists():
                p.unlink()
