"""Comprehensive tests for all stress test backends."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engine.backends.base import CRASH_SIGNALS, FFTPreset, KILLED_BY_US_CODES, StressBackend, StressConfig, StressMode, StressResult
from engine.backends.mprime import FFT_RANGES, MODE_TO_TORTURE, MprimeBackend
from engine.backends.stress_ng import StressNgBackend, _mode_to_method
from engine.backends.ycruncher import YCruncherBackend, _mode_flag


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
        with patch.object(backend, "find_binary", return_value=None):
            with pytest.raises(RuntimeError, match="mprime binary not found"):
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
        assert "ResultsFile=results.txt" in content
        assert "LogFile=prime.log" in content

    def test_prepare_creates_work_dir(self, tmp_path):
        backend = MprimeBackend()
        work = tmp_path / "sub" / "dir"
        backend.prepare(work, StressConfig())
        assert work.exists()

    # --- parse_output tests ---

    @pytest.mark.parametrize(
        "output",
        [
            "FATAL ERROR: something went wrong",
            "Rounding was 0.5 expected less than 0.4",
            "Hardware failure detected running test",
            "Possible hardware failure during test",
            "ILLEGAL SUMOUT detected",
            "SUM(INPUTS) != SUM(OUTPUTS)",
            "ERROR: ILLEGAL operation",
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

    def test_parse_output_self_test_passed(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("Self-test 42 passed\n", "", 0)
        assert passed
        assert msg is None

    def test_parse_output_torture_passed(self):
        backend = MprimeBackend()
        passed, msg = backend.parse_output("Torture test passed!", "", 0)
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
        with patch.object(backend, "find_binary", return_value=None):
            with pytest.raises(RuntimeError, match="stress-ng binary not found"):
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

    def test_get_command(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = "/bin/y-cruncher"
        cfg = StressConfig(mode=StressMode.AVX2, threads=8)
        cmd = backend.get_command(cfg, tmp_path)
        assert cmd == ["/bin/y-cruncher", "stress", "-M", "AVX2", "-T", "8"]

    def test_get_command_no_binary_raises(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = None
        with patch.object(backend, "find_binary", return_value=None):
            with pytest.raises(RuntimeError, match="y-cruncher binary not found"):
                backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = YCruncherBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 in modes

    # --- parse_output ---

    @pytest.mark.parametrize(
        "output",
        [
            "Failed some test",
            "FAILED some test",
            "Error during computation",
            "Verification test FAIL",
            "Result: FAIL",
        ],
    )
    def test_parse_output_errors(self, output):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output(output, "", 1)
        assert not passed
        assert "y-cruncher error" in msg

    @pytest.mark.parametrize("code", sorted(KILLED_BY_US_CODES) + [0])
    def test_parse_output_success_codes(self, code):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("All tests completed", "", code)
        assert passed

    def test_parse_output_unknown_exit_code(self):
        backend = YCruncherBackend()
        passed, msg = backend.parse_output("", "", 7)
        assert not passed
        assert "exited with code 7" in msg

    def test_prepare(self, tmp_path):
        backend = YCruncherBackend()
        work = tmp_path / "ycruncher_work"
        backend.prepare(work, StressConfig())
        assert work.exists()

    def test_cleanup_noop(self, tmp_path):
        backend = YCruncherBackend()
        backend.cleanup(tmp_path)


class TestModeFlag:
    @pytest.mark.parametrize(
        "mode,expected",
        [
            (StressMode.SSE, "SSE"),
            (StressMode.AVX, "AVX"),
            (StressMode.AVX2, "AVX2"),
            (StressMode.AVX512, "AVX512"),
            (StressMode.CUSTOM, "SSE"),
        ],
    )
    def test_mode_flags(self, mode, expected):
        assert _mode_flag(mode) == expected


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
