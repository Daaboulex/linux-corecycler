"""mprime (Prime95 CLI) stress test backend."""

from __future__ import annotations

import re
import textwrap
from typing import TYPE_CHECKING

from engine.backends import register_backend

from .base import CRASH_SIGNALS, FFTPreset, KILLED_BY_US_CODES, StressBackend, StressConfig, StressMode

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

# torture test type mapping for prime.txt
MODE_TO_TORTURE: dict[StressMode, int] = {
    StressMode.SSE: 0,    # no AVX
    StressMode.AVX: 1,    # AVX
    StressMode.AVX2: 2,   # AVX2 (FMA3)
    StressMode.AVX512: 3, # AVX-512
}


@register_backend("mprime")
class MprimeBackend(StressBackend):
    name = "mprime"

    def __init__(self) -> None:
        self._binary: str | None = None
        self._last_work_dir: Path | None = None

    def is_available(self) -> bool:
        self._binary = self.find_binary("mprime")
        return self._binary is not None

    def get_command(self, config: StressConfig, work_dir: Path) -> list[str]:
        if not self._binary:
            self.is_available()
        if not self._binary:
            raise RuntimeError("mprime binary not found")
        return [self._binary, "-t", "-W" + str(work_dir)]

    def get_supported_modes(self) -> list[StressMode]:
        return [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]

    def get_supported_fft_presets(self) -> list[FFTPreset]:
        return list(FFTPreset)

    def prepare(self, work_dir: Path, config: StressConfig) -> None:
        work_dir.mkdir(parents=True, exist_ok=True)
        self._last_work_dir = work_dir

        # determine FFT range
        if config.fft_preset == FFTPreset.CUSTOM and config.fft_min and config.fft_max:
            fft_min, fft_max = config.fft_min, config.fft_max
        else:
            fft_min, fft_max = FFT_RANGES.get(config.fft_preset, (4, 8192))

        torture_type = MODE_TO_TORTURE.get(config.mode, 0)

        # write local.txt — mprime config
        # NumCPUs=1 + CoresPerTest=1 are CRITICAL: mprime ignores TortureThreads
        # and taskset, spawning workers per detected core with explicit
        # sched_setaffinity() calls that override our taskset pinning.
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
                CpuSupportsAVX=1
                CpuSupportsAVX2=1
                CpuSupportsAVX512=1
                CpuSupportsFMA3=1
                TortureWeak={torture_type}
            """)
        )

        # write prime.txt — needed for mprime to not prompt
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
                TortureWeak={torture_type}
                ResultsFile=results.txt
                LogFile=prime.log
            """)
        )

    def parse_output(self, stdout: str, stderr: str, returncode: int) -> tuple[bool, str | None]:
        combined = stdout + "\n" + stderr

        # also check results.txt if available (mprime writes errors there)
        if self._last_work_dir:
            results_file = self._last_work_dir / "results.txt"
            if results_file.exists():
                try:
                    combined += "\n" + results_file.read_text()
                except OSError:
                    pass

        # check for fatal errors — comprehensive Prime95/mprime patterns
        # These cover all known CO/PBO instability signatures from Windows CoreCycler
        fatal_patterns = [
            r"FATAL ERROR",
            r"Rounding was [\d.]+ expected less than",
            r"Rounding check failed",
            r"Hardware failure detected",
            r"Possible hardware failure",
            r"ILLEGAL SUMOUT",
            r"SUM\(INPUTS?\) != SUM\(OUTPUTS?\)",  # handles singular/plural
            r"SUMINP\w* error",  # SUMINP/SUMINPUT variants
            r"SUMOUT\w* error",
            r"ERROR: ILLEGAL",
            r"Jacobi error check failed",
            r"torture test completed \d+ tests?,\s*[1-9]\d*\s+errors?",  # summary with errors>0
            r"Bad ending value",  # memory corruption — common CO failure
            r"Worker stopped",
            r"Worker threads? died",
            r"Disassociation",  # memory allocation failure
            r"Assignment check failed",
            r"Coefficient.*mismatch",
            r"Final multiplier.*error",
        ]
        for pattern in fatal_patterns:
            match = re.search(pattern, combined, re.IGNORECASE)
            if match:
                return False, f"mprime error: {match.group(0)}"

        # check for successful iterations
        if re.search(r"Self-test \d+ passed", combined):
            return True, None
        if re.search(r"torture test passed", combined, re.IGNORECASE):
            return True, None

        # if process was killed (by us, timeout) with no errors, consider it passed
        if returncode in KILLED_BY_US_CODES:
            return True, None

        # SIGSEGV/SIGABRT/SIGBUS = likely CO instability crash
        if returncode in CRASH_SIGNALS:
            return False, f"mprime crashed with {CRASH_SIGNALS[returncode]} (exit {returncode})"

        # unknown state — check return code
        if returncode != 0:
            return False, f"mprime exited with code {returncode}"

        return True, None

    def cleanup(self, work_dir: Path, *, preserve_on_error: bool = False) -> None:
        # If preserving on error, keep results.txt and prime.log for post-mortem analysis
        skip = {"results.txt", "prime.log"} if preserve_on_error else set()
        for f in ("prime.txt", "local.txt", "prime.log", "results.txt", "prime.spl"):
            if f in skip:
                continue
            p = work_dir / f
            if p.exists():
                p.unlink()
