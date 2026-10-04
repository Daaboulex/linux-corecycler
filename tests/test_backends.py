"""Comprehensive tests for all stress test backends."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends.base import (
    FFTPreset,
    LaneSignal,
    Outcome,
    OutcomeKind,
    StressBackend,
    StressConfig,
    StressMode,
    StressResult,
)
from corecycler.engine.backends.mprime import FFT_RANGES, MODE_TO_CPU_FLAGS, MprimeBackend
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
        r = StressResult(core_id=0, outcome=Outcome.PASS, detail="", duration_seconds=60.0)
        assert r.evidence_dir is None
        assert r.iterations_completed == 0

    @pytest.mark.parametrize("outcome", list(Outcome))
    def test_only_the_pass_outcome_passes(self, outcome):
        result = StressResult(core_id=0, outcome=outcome, detail="x", duration_seconds=1.0)
        assert result.passed is (outcome is Outcome.PASS)

    def test_a_result_is_immutable(self):
        import dataclasses

        result = StressResult(core_id=0, outcome=Outcome.COMPUTATION, detail="x", duration_seconds=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.outcome = Outcome.PASS


class TestOutcome:
    def test_stored_rows_keep_their_vocabulary(self):
        stored = {
            Outcome.CPU_SIGNAL: "crash",
            Outcome.OWN_CORE_MCE: "mce",
            Outcome.UNATTRIBUTED_MCE: "mce_unattributed",
            Outcome.THERMAL: "thermal",
            Outcome.OUTSIDE_KILL: "killed",
            Outcome.STALL: "stall",
            Outcome.NOT_STARTED: "startup",
            Outcome.COMPUTATION: "computation",
            Outcome.CLOCK_STRETCH: "clock_stretch",
        }
        assert {outcome: outcome.value for outcome in stored} == stored
        assert all(outcome == value for outcome, value in stored.items())

    def test_every_value_is_distinct(self):
        assert len({outcome.value for outcome in Outcome}) == len(Outcome)

    def test_silicon_outranks_environment_outranks_apparatus_outranks_pass(self):
        order = [OutcomeKind.SILICON, OutcomeKind.ENVIRONMENT, OutcomeKind.APPARATUS, OutcomeKind.PASS]
        by_rank = sorted(Outcome, key=lambda outcome: outcome.rank)
        assert [order.index(outcome.kind) for outcome in by_rank] == sorted(
            order.index(outcome.kind) for outcome in Outcome
        )
        assert by_rank[-1] is Outcome.PASS

    def test_a_pass_is_never_a_signal(self):
        with pytest.raises(ValueError, match="never a signal"):
            LaneSignal(Outcome.PASS, "")


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


class TestBaseBackendBinaryResolution:
    def test_is_available_caches_the_resolved_path(self, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        backend = MprimeBackend()
        assert backend.is_available() is True
        assert backend._binary == "/usr/bin/mprime"

    def test_resolution_reports_why_a_backend_is_absent(self, on_path):
        on_path({})
        resolution = MprimeBackend().resolution()
        assert resolution.path is None
        assert resolution.problem == "not found on PATH"

    def test_require_binary_resolves_lazily(self, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        backend = MprimeBackend()
        assert backend.require_binary() == "/usr/bin/mprime"

    def test_default_get_supported_fft_presets(self):
        """Base class returns empty list by default."""

        class DummyBackend(StressBackend):
            name = "dummy"

            def is_available(self):
                return True

            def get_command(self, config, work_dir):
                return []

            def findings(self, output, work_dir):
                return ()

            def get_supported_modes(self):
                return []

        backend = DummyBackend()
        assert backend.get_supported_fft_presets() == []

    def test_default_prepare_is_a_noop(self, tmp_path):
        """Base class prepare is a no-op."""

        class DummyBackend(StressBackend):
            name = "dummy"

            def is_available(self):
                return True

            def get_command(self, config, work_dir):
                return []

            def findings(self, output, work_dir):
                return ()

            def get_supported_modes(self):
                return []

        backend = DummyBackend()
        cfg = StressConfig()
        backend.prepare(tmp_path, cfg)
        assert list(tmp_path.iterdir()) == []


# ===========================================================================
# mprime backend tests
# ===========================================================================


class TestMprimeBackend:
    def test_name(self):
        assert MprimeBackend.name == "mprime"

    def test_is_available_found(self, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        backend = MprimeBackend()
        assert backend.is_available() is True
        assert backend._binary == "/usr/bin/mprime"

    def test_is_available_not_found(self, on_path):
        on_path({})
        assert MprimeBackend().is_available() is False

    def test_get_command(self, tmp_path):
        backend = MprimeBackend()
        backend._binary = "/usr/bin/mprime"
        cfg = StressConfig()
        cmd = backend.get_command(cfg, tmp_path)
        assert cmd == ["/usr/bin/mprime", "-t", f"-W{tmp_path}"]

    def test_get_command_no_binary_triggers_search(self, tmp_path, on_path):
        on_path({})
        backend = MprimeBackend()
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
        "mode,expected_flags",
        [
            (
                StressMode.SSE,
                {"CpuSupportsAVX": 0, "CpuSupportsFMA3": 0, "CpuSupportsAVX2": 0, "CpuSupportsAVX512F": 0},
            ),
            (
                StressMode.AVX,
                {"CpuSupportsAVX": 1, "CpuSupportsFMA3": 0, "CpuSupportsAVX2": 0, "CpuSupportsAVX512F": 0},
            ),
            (
                StressMode.AVX2,
                {"CpuSupportsAVX": 1, "CpuSupportsFMA3": 1, "CpuSupportsAVX2": 1, "CpuSupportsAVX512F": 0},
            ),
            (
                StressMode.AVX512,
                {"CpuSupportsAVX": 1, "CpuSupportsFMA3": 1, "CpuSupportsAVX2": 1, "CpuSupportsAVX512F": 1},
            ),
        ],
    )
    def test_prepare_selects_the_instruction_set(self, tmp_path, mode, expected_flags):
        backend = MprimeBackend()
        cfg = StressConfig(mode=mode)
        backend.prepare(tmp_path, cfg)

        for name in ("local.txt", "prime.txt"):
            content = (tmp_path / name).read_text()
            for key, value in expected_flags.items():
                assert f"{key}={value}" in content
            assert "TortureWeak" not in content
        assert "EnableSetAffinity=0" in (tmp_path / "prime.txt").read_text()

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
        assert "CpuSupportsAVX2=1" in content
        assert "CpuSupportsAVX512F=0" in content
        assert "EnableSetAffinity=0" in content
        # ResultsFile=/LogFile= were never real Prime95 keys — output stays at
        # the defaults (results.txt / prime.log in the work dir)
        assert "ResultsFile" not in content
        assert "LogFile" not in content

    # --- output findings ---

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
    def test_every_fatal_signature_is_a_computation_error(self, output, tmp_path):
        found = MprimeBackend().findings(f"[Worker #1] Self-test 240K passed!\n{output}\n", tmp_path)
        assert found and {signal.outcome for signal in found} == {Outcome.COMPUTATION}
        assert found[0].text == output

    @pytest.mark.parametrize(
        "output",
        [
            "[Worker #1] Self-test 240K passed!",
            "Self-test 42 passed!",
            "Self-test 4K (thread 2 of 2) passed!",
            "Torture Test completed 20 tests in 15 minutes - 0 errors, 0 warnings.",
            "[Worker #1] Self-test 240K passed!\n[Worker #1] Worker stopped.",
        ],
    )
    def test_clean_lines_find_nothing(self, output, tmp_path):
        assert MprimeBackend().findings(output + "\n", tmp_path) == ()

    # --- results file ---

    def test_a_fatal_line_in_results_txt_is_a_computation_error(self, tmp_path):
        (tmp_path / "results.txt").write_text(
            "[Worker #1] Self-test 240K passed!\nFATAL ERROR: Rounding was 0.4999, expected less than 0.4\n"
        )
        found = MprimeBackend().findings("", tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.COMPUTATION]
        assert "Rounding was 0.4999" in found[0].text

    def test_a_clean_results_txt_finds_nothing(self, tmp_path):
        (tmp_path / "results.txt").write_text("[Worker #1] Self-test 240K passed!\n")
        assert MprimeBackend().findings("", tmp_path) == ()

    def test_no_results_txt_finds_nothing(self, tmp_path):
        assert MprimeBackend().findings("", tmp_path) == ()

    def test_each_mprime_lane_reads_its_own_results(self, tmp_path):
        backend = MprimeBackend()
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        backend.prepare(a, StressConfig())
        backend.prepare(b, StressConfig())
        (b / "results.txt").write_text("FATAL ERROR: Rounding was 0.5, expected less than 0.4\n")
        assert backend.findings("", a) == ()
        assert [signal.outcome for signal in backend.findings("", b)] == [Outcome.COMPUTATION]

    def test_a_bad_byte_in_results_txt_still_shows_the_error(self, tmp_path):
        (tmp_path / "results.txt").write_bytes(b"\xffFATAL ERROR: x\n")
        found = MprimeBackend().findings("", tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.COMPUTATION]
        assert "FATAL ERROR: x" in found[0].text

    def test_an_unreadable_results_txt_is_unverified(self, tmp_path):
        (tmp_path / "results.txt").mkdir()
        found = MprimeBackend().findings("FATAL ERROR: Rounding was 0.5", tmp_path)
        assert {signal.outcome for signal in found} == {Outcome.COMPUTATION, Outcome.UNVERIFIED}
        assert any("results.txt unreadable" in signal.text for signal in found)


# ===========================================================================
# stress-ng backend tests
# ===========================================================================


class TestStressNgBackend:
    def test_name(self):
        assert StressNgBackend.name == "stress-ng"

    def test_is_available_found(self, on_path):
        on_path({"stress-ng": "/usr/bin/stress-ng"})
        assert StressNgBackend().is_available() is True

    def test_is_available_not_found(self, on_path):
        on_path({})
        assert StressNgBackend().is_available() is False

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

    def test_get_command_no_binary_raises(self, tmp_path, on_path):
        on_path({})
        backend = StressNgBackend()
        with pytest.raises(RuntimeError, match="stress-ng binary not found"):
            backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = StressNgBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 not in modes

    # --- findings ---

    @pytest.mark.parametrize(
        "output",
        [
            "3 FAILED during stress test",
            "verification error on cpu 0",
            "computation mismatch detected",
            "error: incorrect result",
            "stress-ng: fail:  [1] cpu: verification failed",
        ],
    )
    def test_a_failure_line_is_a_computation_error(self, output, tmp_path):
        found = StressNgBackend().findings(output, tmp_path)
        assert found and {signal.outcome for signal in found} == {Outcome.COMPUTATION}

    def test_a_zero_failure_count_finds_nothing(self, tmp_path):
        assert StressNgBackend().findings("cpu 0 FAILED\ncompleted\n", tmp_path) == ()


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

    def test_is_available_first_name(self, on_path):
        on_path({"y-cruncher": "/bin/y-cruncher"})
        backend = YCruncherBackend()
        assert backend.is_available() is True
        assert backend._binary == "/bin/y-cruncher"

    def test_is_available_second_name(self, on_path):
        on_path({"y_cruncher": "/bin/y_cruncher"})
        backend = YCruncherBackend()
        assert backend.is_available() is True
        assert backend._binary == "/bin/y_cruncher"

    def test_is_available_not_found(self, on_path):
        on_path({})
        assert YCruncherBackend().is_available() is False

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

    def test_get_command_no_binary_raises(self, tmp_path, on_path):
        on_path({})
        backend = YCruncherBackend()
        with pytest.raises(RuntimeError, match="y-cruncher binary not found"):
            backend.get_command(StressConfig(), tmp_path)

    def test_get_supported_modes(self):
        backend = YCruncherBackend()
        modes = backend.get_supported_modes()
        assert StressMode.SSE in modes
        assert StressMode.AVX in modes
        assert StressMode.AVX2 in modes
        assert StressMode.AVX512 in modes

    def test_real_pass_output_finds_nothing(self, tmp_path):
        assert YCruncherBackend().findings(_CAPTURED_PASS_OUTPUT, tmp_path) == ()

    @pytest.mark.parametrize(
        ("output", "named"),
        [
            ("Iteration: 5\nError(s) encountered on logical core 3.\n", "Error(s) encountered"),
            ("Coefficient is too large\n", "Coefficient is too large"),
            (_CAPTURED_INVALID_PARAM_OUTPUT, "Invalid Parameter"),
            ("\x1b[01;31mCoefficient is too large\x1b[0m\n", "Coefficient is too large"),
        ],
    )
    def test_an_error_line_is_a_computation_error(self, output, named, tmp_path):
        found = YCruncherBackend().findings(output, tmp_path)
        assert found and {signal.outcome for signal in found} == {Outcome.COMPUTATION}
        assert any(named in signal.text for signal in found)
        assert not any("\x1b" in signal.text for signal in found)


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
# FFT_RANGES and MODE_TO_CPU_FLAGS constants tests
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

    def test_every_mode_has_a_cpu_flag_set(self):
        for mode in [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]:
            assert mode in MODE_TO_CPU_FLAGS

    def test_each_mode_disables_everything_above_it(self):
        order = [StressMode.SSE, StressMode.AVX, StressMode.AVX2, StressMode.AVX512]
        gates = ["CpuSupportsAVX", "CpuSupportsAVX2", "CpuSupportsAVX512F"]
        enabled = [[gate for gate in gates if MODE_TO_CPU_FLAGS[mode][gate]] for mode in order]
        assert enabled == [[], gates[:1], gates[:2], gates]
        for mode in order:
            assert MODE_TO_CPU_FLAGS[mode]["CpuSupportsFMA4"] == 0

    def test_version_parse_reads_the_live_format(self):
        line = "Mersenne Prime Test Program: Linux64,Untrusted Prime95,v31.4,build 2"
        assert MprimeBackend.parse_version(line) == "31.4"
        assert MprimeBackend.parse_version("no version here") is None

    def test_installed_version_reads_the_binary(self, monkeypatch, tmp_path):
        import subprocess
        from types import SimpleNamespace

        backend = MprimeBackend()
        backend._binary = "/usr/bin/mprime"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: SimpleNamespace(stdout="Prime95,v31.4,build 2", stderr=""),
        )
        assert backend.installed_version() == "31.4"

    def test_installed_version_survives_a_broken_binary(self, monkeypatch):
        import subprocess

        backend = MprimeBackend()
        backend._binary = "/usr/bin/mprime"

        def boom(*a, **k):
            raise OSError("exec failed")

        monkeypatch.setattr(subprocess, "run", boom)
        assert backend.installed_version() is None
