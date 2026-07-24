"""Comprehensive tests for all stress test backends."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends.base import (
    KILLED_BY_US_CODES,
    FFTPreset,
    StressBackend,
    StressConfig,
    StressMode,
    StressResult,
)
from corecycler.engine.backends.mprime import FFT_RANGES, MODE_TO_TORTURE, MprimeBackend
from corecycler.engine.backends.stress_ng import StressNgBackend, _mode_to_method
from corecycler.engine.backends.ycruncher import MODE_TO_ALGORITHMS, YCruncherBackend

# ===========================================================================
# Base class tests
# ===========================================================================


class TestStressConfig:
    def test_defaults(self):
        cfg = StressConfig()
        assert cfg.mode == StressMode.SSE
        assert cfg.fft_preset == FFTPreset.SMALL
        assert cfg.threads == 1
        assert cfg.fft_min is None
        assert cfg.fft_max is None
        assert cfg.memory_mb is None

    def test_custom_config(self):
        cfg = StressConfig(
            mode=StressMode.AVX512,
            fft_preset=FFTPreset.CUSTOM,
            fft_min=100,
            fft_max=500,
            threads=4,
            memory_mb=2048,
        )
        assert cfg.mode == StressMode.AVX512
        assert cfg.fft_min == 100
        assert cfg.fft_max == 500


class TestStressResult:
    def test_defaults(self):
        r = StressResult(core_id=0, passed=True, duration_seconds=60.0)
        assert r.error_message is None
        assert r.error_type is None
        assert r.iterations_completed == 0
        assert r.last_fft_size is None


class TestStressMode:
    def test_all_modes(self):
        assert StressMode.SSE
        assert StressMode.AVX
        assert StressMode.AVX2
        assert StressMode.AVX512
        assert StressMode.CUSTOM


class TestFFTPreset:
    def test_all_presets(self):
        assert FFTPreset.SMALLEST.value == "smallest"
        assert FFTPreset.SMALL.value == "small"
        assert FFTPreset.LARGE.value == "large"
        assert FFTPreset.HUGE.value == "huge"
        assert FFTPreset.ALL.value == "all"
        assert FFTPreset.MODERATE.value == "moderate"
        assert FFTPreset.HEAVY.value == "heavy"
        assert FFTPreset.HEAVY_SHORT.value == "heavy_short"
        assert FFTPreset.CUSTOM.value == "custom"


class TestBaseBackendFindBinary:
    def test_find_binary_success(self):
        """find_binary should return path when binary exists."""
        backend = MprimeBackend()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="/usr/bin/echo\n")
            result = backend.find_binary("echo")
        assert result == "/usr/bin/echo"

    def test_find_binary_not_found(self):
        backend = MprimeBackend()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = backend.find_binary("nonexistent_binary_xyz")
        assert result is None

    def test_find_binary_timeout(self):
        import subprocess

        backend = MprimeBackend()
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("which", 5)):
            result = backend.find_binary("test")
        assert result is None

    def test_find_binary_file_not_found(self):
        backend = MprimeBackend()
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = backend.find_binary("test")
        assert result is None

    def test_default_get_supported_fft_presets(self):
        """Base class returns empty list by default."""

        class DummyBackend(StressBackend):
            name = "dummy"

            def is_available(self):
                return True

            def get_command(self, config, work_dir):
                return []

            def parse_output(self, stdout, stderr, returncode):
                return True, None

            def get_supported_modes(self):
                return []

        backend = DummyBackend()
        assert backend.get_supported_fft_presets() == []

    def test_default_prepare_and_cleanup(self, tmp_path):
        """Base class prepare/cleanup are no-ops."""

        class DummyBackend(StressBackend):
            name = "dummy"

            def is_available(self):
                return True

            def get_command(self, config, work_dir):
                return []

            def parse_output(self, stdout, stderr, returncode):
                return True, None

            def get_supported_modes(self):
                return []

        backend = DummyBackend()
        cfg = StressConfig()
        # should not raise
        backend.prepare(tmp_path, cfg)
        backend.cleanup(tmp_path)


# ===========================================================================
# mprime backend tests
# ===========================================================================


class TestMprimeBackend:
    def test_name(self):
        assert MprimeBackend.name == "mprime"

    def test_is_available_found(self):
        backend = MprimeBackend()
        with patch.object(backend, "find_binary", return_value="/usr/bin/mprime"):
            assert backend.is_available() is True
            assert backend._binary == "/usr/bin/mprime"

    def test_is_available_not_found(self):
        backend = MprimeBackend()
        with patch.object(backend, "find_binary", return_value=None):
            assert backend.is_available() is False

    def test_get_command(self, tmp_path):
        backend = MprimeBackend()
        backend._binary = "/usr/bin/mprime"
        cfg = StressConfig()
        cmd = backend.get_command(cfg, tmp_path)
        assert cmd == ["/usr/bin/mprime", "-t", f"-W{tmp_path}"]

    def test_get_command_no_binary_triggers_search(self, tmp_path):
        backend = MprimeBackend()
        backend._binary = None
        with (
            patch.object(backend, "find_binary", return_value=None),
            pytest.raises(RuntimeError, match="mprime binary not found"),
        ):
            backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = MprimeBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 in modes

    def test_get_supported_fft_presets(self):
        backend = MprimeBackend()
        presets = backend.get_supported_fft_presets()
        assert FFTPreset.SMALL in presets
        assert FFTPreset.LARGE in presets
        assert FFTPreset.CUSTOM in presets

    # --- prepare tests ---

    @pytest.mark.parametrize(
        "preset,expected_min,expected_max",
        [
            (FFTPreset.SMALLEST, 4, 21),
            (FFTPreset.SMALL, 36, 248),
            (FFTPreset.LARGE, 426, 8192),
            (FFTPreset.HUGE, 8960, 65536),
            (FFTPreset.ALL, 4, 65536),
            (FFTPreset.MODERATE, 1344, 4096),
            (FFTPreset.HEAVY, 4, 1344),
            (FFTPreset.HEAVY_SHORT, 4, 160),
        ],
    )
    def test_prepare_fft_ranges(self, tmp_path, preset, expected_min, expected_max):
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=preset)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "local.txt").read_text()
        assert f"MinTortureFFT={expected_min}" in content
        assert f"MaxTortureFFT={expected_max}" in content

    def test_prepare_custom_fft(self, tmp_path):
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=FFTPreset.CUSTOM, fft_min=100, fft_max=500)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "local.txt").read_text()
        assert "MinTortureFFT=100" in content
        assert "MaxTortureFFT=500" in content

    def test_prepare_custom_fft_no_range_uses_default(self, tmp_path):
        """CUSTOM preset without fft_min/fft_max should fall back to default."""
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=FFTPreset.CUSTOM, fft_min=None, fft_max=None)
        backend.prepare(tmp_path, cfg)
        content = (tmp_path / "local.txt").read_text()
        # should use fallback (4, 8192)
        assert "MinTortureFFT=4" in content
        assert "MaxTortureFFT=8192" in content

    @pytest.mark.parametrize(
        "mode,expected_torture",
        [
            (StressMode.SSE, 0),
            (StressMode.AVX, 1),
            (StressMode.AVX2, 2),
            (StressMode.AVX512, 3),
        ],
    )
    def test_prepare_torture_type(self, tmp_path, mode, expected_torture):
        backend = MprimeBackend()
        cfg = StressConfig(mode=mode)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "local.txt").read_text()
        assert f"TortureWeak={expected_torture}" in content

    def test_prepare_thread_count(self, tmp_path):
        backend = MprimeBackend()
        cfg = StressConfig(threads=4)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "local.txt").read_text()
        assert "TortureThreads=4" in content

    def test_prepare_creates_both_files(self, tmp_path):
        backend = MprimeBackend()
        backend.prepare(tmp_path, StressConfig())
        assert (tmp_path / "local.txt").exists()
        assert (tmp_path / "prime.txt").exists()

    def test_prepare_prime_txt_content(self, tmp_path):
        backend = MprimeBackend()
        cfg = StressConfig(fft_preset=FFTPreset.SMALL, mode=StressMode.AVX2, threads=2)
        backend.prepare(tmp_path, cfg)

        content = (tmp_path / "prime.txt").read_text()
        assert "UsePrimenet=0" in content
        assert "StressTester=1" in content
        assert "MinTortureFFT=36" in content
        assert "TortureThreads=2" in content
        assert "TortureWeak=2" in content
        # ResultsFile=/LogFile= were never real Prime95 keys — output stays at
        # the defaults (results.txt / prime.log in the work dir)
        assert "ResultsFile" not in content
        assert "LogFile" not in content

    def test_prepare_creates_work_dir(self, tmp_path):
        backend = MprimeBackend()
        work = tmp_path / "sub" / "dir"
        backend.prepare(work, StressConfig())
        assert work.exists()

    # --- parse_output tests ---

    @pytest.mark.parametrize(
        "output",
        [
            "FATAL ERROR: Rounding was 0.5, expected less than 0.4",
            "FATAL ERROR: Final result was 0000ABCD, expected: 0000EF01.",
            "ERROR: ILLEGAL SUMOUT",
            "Possible hardware failure, consult readme.txt file, restarting test.",
            "Hardware failure detected running 288K FFT size, consult stress.txt file.",
            "Maximum number of warnings exceeded.",
            "TORTURE TEST FAILED on worker #2.",
            "Torture Test completed 20 tests in 2 hours, 15 minutes - 1 errors, 0 warnings.",
            "ERROR: SUM(INPUTS) != SUM(OUTPUTS), 1.5 != 1.6",
            "ERROR: Jacobi error check failed!",
            "Warning: SUMOUT MISMATCH",
        ],
    )
    def test_parse_output_fatal_errors(self, output):
        backend = MprimeBackend()
        passed, msg = backend.parse_output(output, "", 1)
        assert not passed
        assert msg is not None
        assert "mprime error" in msg

    def test_parse_output_error_in_stderr(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "FATAL ERROR: test", 1)
        assert not passed

    @pytest.mark.parametrize(
        "line",
        [
            "[Worker #1] Self-test 240K passed!",       # K-suffixed FFT (usual)
            "Self-test 42 passed!",                     # sub-1K FFT
            "Self-test 4K (thread 2 of 2) passed!",     # hyperthreaded variant
        ],
    )
    def test_parse_output_self_test_passed(self, line):
        backend = MprimeBackend()
        passed, msg = backend.parse_output(line + "\n", "", 0)
        assert passed
        assert msg is None

    def test_parse_output_torture_summary_clean(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output(
            "Torture Test completed 20 tests in 15 minutes - 0 errors, 0 warnings.", "", 0
        )
        assert passed
        assert msg is None

    def test_parse_output_benign_worker_stop_is_not_an_error(self):
        """"Worker stopped." is Prime95's graceful-stop line (commonb.c:3143),
        not a fatal error."""
        backend = MprimeBackend()
        passed, msg = backend.parse_output(
            "[Worker #1] Self-test 240K passed!\n[Worker #1] Worker stopped.\n", "", -15
        )
        assert passed
        assert msg is None

    @pytest.mark.parametrize("code", sorted(KILLED_BY_US_CODES))
    def test_parse_output_killed_signals(self, code):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "", code)
        assert passed

    def test_parse_output_unknown_error_code(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "", 42)
        assert not passed
        assert "exited with code 42" in msg

    def test_parse_output_clean_exit_no_output(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("", "", 0)
        assert passed
        assert msg is None

    # --- cleanup tests ---

    def test_cleanup_removes_files(self, tmp_path):
        backend = MprimeBackend()
        for f in ("prime.txt", "local.txt", "prime.log", "results.txt", "prime.spl"):
            (tmp_path / f).write_text("data")
        backend.cleanup(tmp_path)
        for f in ("prime.txt", "local.txt", "prime.log", "results.txt", "prime.spl"):
            assert not (tmp_path / f).exists()

    def test_cleanup_ignores_missing_files(self, tmp_path):
        backend = MprimeBackend()
        # should not raise
        backend.cleanup(tmp_path)

    def test_cleanup_preserves_other_files(self, tmp_path):
        backend = MprimeBackend()
        (tmp_path / "important.dat").write_text("keep me")
        backend.cleanup(tmp_path)
        assert (tmp_path / "important.dat").exists()

    def test_cleanup_on_error_renames_postmortem_files(self, tmp_path):
        """A preserved results.txt must be RENAMED, never left in place: mprime
        appends to results.txt, so a stale FATAL ERROR would be re-parsed by
        every later run in this work dir as its own failure."""
        backend = MprimeBackend()
        (tmp_path / "results.txt").write_text("FATAL ERROR: Rounding was 0.5")
        (tmp_path / "prime.log").write_text("log")
        backend.cleanup(tmp_path, preserve_on_error=True)
        assert not (tmp_path / "results.txt").exists()
        assert not (tmp_path / "prime.log").exists()
        assert "FATAL ERROR" in (tmp_path / "failed-results.txt").read_text()
        assert (tmp_path / "failed-prime.log").read_text() == "log"

    def test_prepare_removes_stale_run_files(self, tmp_path):
        """prepare() must clean leftovers (abort/hard crash skips cleanup) so a
        new run never inherits the previous run's errors or savefile."""
        backend = MprimeBackend()
        for f in ("results.txt", "prime.log", "prime.spl"):
            (tmp_path / f).write_text("stale")
        backend.prepare(tmp_path, StressConfig())
        for f in ("results.txt", "prime.log", "prime.spl"):
            assert not (tmp_path / f).exists()
        # the failed-* post-mortem copies are kept
        (tmp_path / "failed-results.txt").write_text("post-mortem")
        backend.prepare(tmp_path, StressConfig())
        assert (tmp_path / "failed-results.txt").exists()

    # --- live error polling ---

    def test_poll_errors_detects_fatal(self, tmp_path):
        backend = MprimeBackend()
        (tmp_path / "results.txt").write_text(
            "[Worker #1] Self-test 240K passed!\n"
            "FATAL ERROR: Rounding was 0.4999, expected less than 0.4\n"
        )
        msg = backend.poll_errors(tmp_path)
        assert msg is not None and "FATAL ERROR" in msg

    def test_poll_errors_clean_run(self, tmp_path):
        backend = MprimeBackend()
        (tmp_path / "results.txt").write_text("[Worker #1] Self-test 240K passed!\n")
        assert backend.poll_errors(tmp_path) is None

    def test_poll_errors_no_file(self, tmp_path):
        assert MprimeBackend().poll_errors(tmp_path) is None


# ===========================================================================
# stress-ng backend tests
# ===========================================================================


class TestStressNgBackend:
    def test_name(self):
        assert StressNgBackend.name == "stress-ng"

    def test_is_available_found(self):
        backend = StressNgBackend()
        with patch.object(backend, "find_binary", return_value="/usr/bin/stress-ng"):
            assert backend.is_available() is True

    def test_is_available_not_found(self):
        backend = StressNgBackend()
        with patch.object(backend, "find_binary", return_value=None):
            assert backend.is_available() is False

    def test_get_command(self, tmp_path):
        backend = StressNgBackend()
        backend._binary = "/usr/bin/stress-ng"
        cfg = StressConfig(mode=StressMode.SSE, threads=2)
        cmd = backend.get_command(cfg, tmp_path)
        assert cmd[0] == "/usr/bin/stress-ng"
        assert "--cpu" in cmd
        assert "2" in cmd
        assert "--cpu-method" in cmd
        assert "matrixprod" in cmd
        assert "--verify" in cmd
        assert "--metrics-brief" in cmd
        assert "--temp-path" in cmd
        assert str(tmp_path) in cmd

    def test_get_command_avx_method(self, tmp_path):
        backend = StressNgBackend()
        backend._binary = "/usr/bin/stress-ng"
        cfg = StressConfig(mode=StressMode.AVX)
        cmd = backend.get_command(cfg, tmp_path)
        idx = cmd.index("--cpu-method")
        assert cmd[idx + 1] == "fft"

    def test_get_command_no_binary_raises(self, tmp_path):
        backend = StressNgBackend()
        backend._binary = None
        with (
            patch.object(backend, "find_binary", return_value=None),
            pytest.raises(RuntimeError, match="stress-ng binary not found"),
        ):
            backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = StressNgBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 not in modes

    def test_prepare(self, tmp_path):
        backend = StressNgBackend()
        work = tmp_path / "work"
        backend.prepare(work, StressConfig())
        assert work.exists()

    # --- parse_output ---

    @pytest.mark.parametrize(
        "output",
        [
            "3 FAILED during stress test",
            "verification error on cpu 0",
            "computation mismatch detected",
            "error: incorrect result",
        ],
    )
    def test_parse_output_errors(self, output):
        backend = StressNgBackend()
        passed, msg = backend.parse_output(output, "", 1)
        assert not passed
        assert "stress-ng error" in msg

    def test_parse_output_error_in_stderr(self):
        backend = StressNgBackend()
        passed, msg = backend.parse_output("", "FAILED test", 1)
        assert not passed

    @pytest.mark.parametrize("code", sorted(KILLED_BY_US_CODES) + [0])
    def test_parse_output_success_codes(self, code):
        backend = StressNgBackend()
        passed, msg = backend.parse_output("completed", "", code)
        assert passed
        assert msg is None

    def test_parse_output_unknown_exit_code(self):
        backend = StressNgBackend()
        passed, msg = backend.parse_output("", "", 99)
        assert not passed
        assert "exited with code 99" in msg

    def test_cleanup_noop(self, tmp_path):
        backend = StressNgBackend()
        (tmp_path / "test.dat").write_text("data")
        backend.cleanup(tmp_path)
        assert (tmp_path / "test.dat").exists()


class TestModeToMethod:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            (StressMode.SSE, "matrixprod"),
            (StressMode.AVX, "fft"),
            (StressMode.AVX2, "fft"),
            (StressMode.AVX512, "matrixprod"),
            (StressMode.CUSTOM, "matrixprod"),
        ],
    )
    def test_mode_mapping(self, mode, expected):
        assert _mode_to_method(mode) == expected


# ===========================================================================
# y-cruncher backend tests
# ===========================================================================


_CAPTURED_PASS_OUTPUT = """\
Auto-Selecting: 11-SNB ~ Hina

Component Stress Tester

  1   Logical Cores:      4
  2   Memory:              200 MiB  ( 50.0 MiB per thread )
  6   Stop on Error:      Enabled

  #  Tag   Test Name                   Mem/Thread  Component
 11  BKT   Basecase + Karatsuba          27.8 KiB  Scalar Integer
 16  FFTv4 Fast Fourier Transform (v4)   246 MiB   AVX Float

  0   Start Stress-Testing!

Allocating Memory...
  Core   0:  27.8 KiB

Iteration: 0  Total Elapsed Time: 0.001 seconds  ( 0.000 minutes )
Running BKT: Passed  Test Speed:  5.25 * 10^08  bits / sec

Iteration: 1  Total Elapsed Time: 3.054 seconds  ( 0.051 minutes )
Running BKT: Passed  Test Speed:  5.2 * 10^08  bits / sec
"""

_CAPTURED_INVALID_PARAM_OUTPUT = """\
Reading Hardware Topology...

Logical Cores:
    0 1 2 3

Invalid Parameter: SSE
Press ENTER to continue . . .
"""


class TestYCruncherBackend:
    def test_name(self):
        assert YCruncherBackend.name == "y-cruncher"

    def test_is_available_first_name(self):
        backend = YCruncherBackend()
        with patch.object(
            backend, "find_binary", side_effect=lambda n: "/bin/y-cruncher" if n == "y-cruncher" else None
        ):
            assert backend.is_available() is True
            assert backend._binary == "/bin/y-cruncher"

    def test_is_available_second_name(self):
        backend = YCruncherBackend()
        with patch.object(
            backend,
            "find_binary",
            side_effect=lambda n: "/bin/y_cruncher" if n == "y_cruncher" else None,
        ):
            assert backend.is_available() is True
            assert backend._binary == "/bin/y_cruncher"

    def test_is_available_not_found(self):
        backend = YCruncherBackend()
        with patch.object(backend, "find_binary", return_value=None):
            assert backend.is_available() is False

    def test_get_command_sse_selects_scalar_algorithm(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = "/bin/y-cruncher"
        cmd = backend.get_command(StressConfig(mode=StressMode.SSE), tmp_path)
        assert cmd == [
            "/bin/y-cruncher",
            "skip-warnings",
            "pause:-2",
            "status:none",
            "stress",
            "-M:1024M",
            "-D:30",
            "BKT",
        ]

    def test_get_command_avx2_enables_all_algorithms(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = "/bin/y-cruncher"
        cmd = backend.get_command(StressConfig(mode=StressMode.AVX2), tmp_path)
        assert cmd == [
            "/bin/y-cruncher",
            "skip-warnings",
            "pause:-2",
            "status:none",
            "stress",
            "-M:1024M",
            "-D:30",
        ]

    def test_get_command_is_headless_never_blocks(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = "/bin/y-cruncher"
        cmd = backend.get_command(StressConfig(mode=StressMode.AVX), tmp_path)
        assert "skip-warnings" in cmd
        assert "pause:-2" in cmd
        assert cmd.index("skip-warnings") < cmd.index("stress")
        assert cmd.index("pause:-2") < cmd.index("stress")

    def test_get_command_memory_override(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = "/bin/y-cruncher"
        cmd = backend.get_command(StressConfig(mode=StressMode.SSE, memory_mb=2048), tmp_path)
        assert "-M:2048M" in cmd

    def test_get_command_no_binary_raises(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = None
        with (
            patch.object(backend, "find_binary", return_value=None),
            pytest.raises(RuntimeError, match="y-cruncher binary not found"),
        ):
            backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = YCruncherBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 in modes

    def test_parse_real_pass_output_not_false_flagged(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(_CAPTURED_PASS_OUTPUT, "", -15)
        assert passed, msg
        assert msg is None

    @pytest.mark.parametrize("code", sorted(KILLED_BY_US_CODES))
    def test_parse_killed_is_pass(self, code):
        backend = YCruncherBackend()
        passed, _msg = backend.parse_output(_CAPTURED_PASS_OUTPUT, "", code)
        assert passed

    def test_parse_self_exit_zero_is_anomaly_not_silent_pass(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(_CAPTURED_PASS_OUTPUT, "", 0)
        assert not passed
        assert "verdict unavailable" in msg

    def test_parse_error_encountered(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(
            "Iteration: 5\nError(s) encountered on logical core 3.\n", "", 0
        )
        assert not passed
        assert "y-cruncher error" in msg

    def test_parse_coefficient_too_large(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("Coefficient is too large\n", "", 0)
        assert not passed
        assert "Coefficient is too large" in msg

    def test_parse_invalid_parameter(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(_CAPTURED_INVALID_PARAM_OUTPUT, "", 0)
        assert not passed
        assert "Invalid Parameter" in msg

    def test_parse_ansi_codes_stripped(self):
        backend = YCruncherBackend()
        passed, _msg = backend.parse_output("\x1b[01;31mCoefficient is too large\x1b[0m\n", "", 0)
        assert not passed

    @pytest.mark.parametrize("code", [-11, -6, -4])
    def test_parse_crash_signal_fails(self, code):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("", "", code)
        assert not passed
        assert "crashed" in msg

    def test_parse_unknown_exit_code_is_apparatus_fault(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("", "", 7)
        assert not passed
        assert "exited with code 7" in msg
        assert "verdict unavailable" in msg

    def test_prepare(self, tmp_path):
        backend = YCruncherBackend()
        work = tmp_path / "ycruncher_work"
        backend.prepare(work, StressConfig())
        assert work.exists()

    def test_cleanup_noop(self, tmp_path):
        backend = YCruncherBackend()
        backend.cleanup(tmp_path)


class TestYCruncherModeMapping:
    @pytest.mark.parametrize("mode", list(StressMode))
    def test_every_mode_maps(self, mode):
        assert mode in MODE_TO_ALGORITHMS

    def test_sse_is_scalar_only(self):
        assert MODE_TO_ALGORITHMS[StressMode.SSE] == ("BKT",)

    def test_avx2_enables_all_by_default(self):
        assert MODE_TO_ALGORITHMS[StressMode.AVX2] == ()

    def test_algorithms_are_valid_ycruncher_names(self):
        valid = {"BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "NTT63", "N63", "VSTv3", "VT3"}
        for algos in MODE_TO_ALGORITHMS.values():
            assert set(algos) <= valid


# ===========================================================================
# FFT_RANGES and MODE_TO_TORTURE constants tests
# ===========================================================================


class TestMprimeConstants:
    def test_fft_ranges_completeness(self):
        """All non-CUSTOM presets should be in FFT_RANGES."""
        for preset in FFTPreset:
            if preset != FFTPreset.CUSTOM:
                assert preset in FFT_RANGES

    def test_fft_ranges_valid(self):
        for preset, (lo, hi) in FFT_RANGES.items():
            assert lo < hi, f"{preset}: {lo} >= {hi}"
            assert lo > 0

    def test_mode_to_torture_completeness(self):
        for mode in [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]:
            assert mode in MODE_TO_TORTURE

    def test_mode_to_torture_values(self):
        assert MODE_TO_TORTURE[StressMode.SSE] == 0
        assert MODE_TO_TORTURE[StressMode.AVX] == 1
        assert MODE_TO_TORTURE[StressMode.AVX2] == 2
        assert MODE_TO_TORTURE[StressMode.AVX512] == 3


class TestFailClosedResultsRead:
    def test_unreadable_results_txt_is_not_a_pass(self, tmp_path):
        """Without results.txt a real error could pass unseen — an unreadable
        file must produce an apparatus-fault verdict (engine pauses on it),
        never a silent pass."""
        import os

        backend = MprimeBackend()
        backend._last_work_dir = tmp_path
        results = tmp_path / "results.txt"
        results.write_text("FATAL ERROR: Rounding was 0.5, expected less than 0.4")
        os.chmod(results, 0o000)
        try:
            passed, msg = backend.parse_output("", "", -15)
            assert passed is False
            assert "verdict unavailable" in msg
        finally:
            os.chmod(results, 0o644)  # let tmp_path cleanup succeed
