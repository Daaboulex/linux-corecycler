"""Comprehensive tests for all stress test backends."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine import topology
from corecycler.engine.backends import ycruncher
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
from corecycler.engine.backends.ycruncher import ALL_TESTS, MODE_TO_ALGORITHMS, YCruncherBackend


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
        assert r.output == ""
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
            Outcome.HELD_TOO_LONG: "held_too_long",
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


_ZEN1_FLAGS = (
    "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ht "
    "syscall nx mmxext fxsr_opt pdpe1gb rdtscp lm constant_tsc rep_good nopl nonstop_tsc cpuid pni pclmulqdq "
    "monitor ssse3 fma cx16 sse4_1 sse4_2 movbe popcnt aes xsave avx f16c rdrand lahf_lm svm abm sse4a "
    "misalignsse 3dnowprefetch topoext perfctr_core cpb hw_pstate fsgsbase bmi1 avx2 smep bmi2 rdseed adx "
    "smap clflushopt sha_ni xsaveopt xsavec xgetbv1 xsaves clzero arat npt overflow_recov succor smca"
)
_ZEN3_FLAGS = _ZEN1_FLAGS + " clwb wbnoinvd umip pku ospke vaes vpclmulqdq rdpid fsrm"
_ZEN4_FLAGS = _ZEN3_FLAGS + (
    " avx512f avx512dq avx512ifma avx512cd avx512bw avx512vl avx512_bf16 avx512vbmi avx512_vbmi2 gfni"
    " avx512_vnni avx512_bitalg avx512_vpopcntdq"
)
_ZEN5_FLAGS = _ZEN4_FLAGS + " movdiri movdir64b avx512_vp2intersect"
_HASWELL_FLAGS = (
    "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse "
    "sse2 ss ht tm pbe syscall nx pdpe1gb rdtscp lm constant_tsc arch_perfmon rep_good nopl xtopology "
    "nonstop_tsc cpuid aperfmperf pni pclmulqdq monitor vmx est ssse3 fma cx16 pcid sse4_1 sse4_2 x2apic "
    "movbe popcnt aes xsave avx f16c rdrand lahf_lm abm fsgsbase bmi1 avx2 smep bmi2 erms invpcid xsaveopt"
)
_ALDER_LAKE_FLAGS = _HASWELL_FLAGS + " adx rdseed smap clflushopt clwb sha_ni umip gfni vaes vpclmulqdq rdpid"
_SKYLAKE_X_FLAGS = _HASWELL_FLAGS + " adx rdseed smap clflushopt clwb avx512f avx512dq avx512cd avx512bw avx512vl"
_BULLDOZER_FLAGS = (
    "fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 ht "
    "syscall nx mmxext fxsr_opt pdpe1gb rdtscp lm constant_tsc rep_good nopl nonstop_tsc cpuid pni pclmulqdq "
    "monitor ssse3 cx16 sse4_1 sse4_2 popcnt aes xsave avx lahf_lm svm abm sse4a misalignsse 3dnowprefetch "
    "xop fma4 topoext perfctr_core"
)


def _without(flags: str, *absent: str) -> str:
    return " ".join(flag for flag in flags.split() if flag not in absent)


class TestMprimeBackend:
    @pytest.fixture(autouse=True)
    def zen4_cpu(self, monkeypatch, tmp_path):
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN4_FLAGS)

    @pytest.mark.parametrize(
        ("mode", "flags", "missing"),
        [
            pytest.param(StressMode.AVX512, _ZEN3_FLAGS, "avx512f", id="avx512-on-zen3"),
            pytest.param(StressMode.AVX2, _ZEN3_FLAGS.replace(" fma ", " "), "fma", id="avx2-without-fma"),
        ],
    )
    def test_a_mode_whose_forced_flags_the_cpu_lacks_is_refused(self, tmp_path, monkeypatch, mode, flags, missing):
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", flags)
        work = tmp_path / "run"
        work.mkdir()
        with pytest.raises(OSError, match=missing):
            MprimeBackend().prepare(work, StressConfig(mode=mode))
        assert not (work / "prime.txt").exists()

    def test_avx512_runs_on_a_cpu_that_reports_it(self, tmp_path):
        MprimeBackend().prepare(tmp_path, StressConfig(mode=StressMode.AVX512))
        assert "CpuSupportsAVX512F=1" in (tmp_path / "prime.txt").read_text()

    def test_sse_forces_no_flag_and_needs_none(self, tmp_path, monkeypatch):
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("processor\t: 0\nFeatures\t: fp asimd\n")
        monkeypatch.setattr(topology, "CPUINFO", cpuinfo)
        MprimeBackend().prepare(tmp_path, StressConfig(mode=StressMode.SSE))
        assert "CpuSupportsAVX=0" in (tmp_path / "prime.txt").read_text()

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

    @pytest.mark.req("INT.mprime")
    def test_prepare_keeps_the_torture_threads_at_the_nice_they_start_with(self, tmp_path):
        MprimeBackend().prepare(tmp_path, StressConfig())
        assert "EnableSetPriority=0" in (tmp_path / "prime.txt").read_text().splitlines()

    @pytest.mark.parametrize(("threads", "hyperthreading"), [(1, 0), (2, 1)])
    def test_prepare_asks_for_one_worker_through_the_keys_t_reads(self, tmp_path, threads, hyperthreading):
        MprimeBackend().prepare(tmp_path, StressConfig(threads=threads))

        for name in ("local.txt", "prime.txt"):
            lines = (tmp_path / name).read_text().splitlines()
            assert "TortureCores=1" in lines
            assert f"TortureHyperthreading={hyperthreading}" in lines
            assert not [line for line in lines if line.startswith(("TortureThreads=", "CoresPerTest="))]

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
        assert "TortureCores=1" in content
        assert "CpuSupportsAVX2=1" in content
        assert "CpuSupportsAVX512F=0" in content
        assert "EnableSetAffinity=0" in content
        assert "ResultsFile" not in content
        assert "LogFile" not in content

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
            "ERROR: Shift counter corrupt.",
            "ERROR: Illegal double encountered.",
            "ERROR: FFT data has been zeroed!",
            "ERROR: Jacobi error check failed!",
            "Warning: ILLEGAL SUMOUT",
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


def _ng_summary(passed: str, failed: str, skipped: str, verdict: str) -> str:
    return "".join(
        f"stress-ng: info:  [100] {line}\n"
        for line in (
            f"skipped: {skipped}",
            f"passed: {passed}",
            f"failed: {failed}",
            "metrics untrustworthy: 0",
            f"{verdict} run completed in 0.52 secs",
        )
    )


NG_CLEAN_STOP = _ng_summary("2: cpu (1) matrix (1)", "0", "0", "successful")
NG_ONE_FAILED = _ng_summary("1: matrix (1)", "1: cpu (1)", "0", "unsuccessful")


def _strongest(found: tuple[LaneSignal, ...]) -> LaneSignal | None:
    return min(found, key=lambda signal: signal.outcome.rank, default=None)


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

    def test_the_command_ends_every_stressor_when_one_fails(self, tmp_path):
        backend = StressNgBackend()
        backend._binary = "/usr/bin/stress-ng"
        assert "--abort" in backend.get_command(StressConfig(), tmp_path)

    @pytest.mark.parametrize("mode", [StressMode.SSE, StressMode.AVX, StressMode.AVX2])
    def test_one_cpu_stressor_runs_per_lane_cpu(self, tmp_path, mode):
        backend = StressNgBackend()
        backend._binary = "/usr/bin/stress-ng"
        cmd = backend.get_command(StressConfig(mode=mode, threads=3, cpus=(0, 1, 2)), tmp_path)
        assert cmd[cmd.index("--cpu") + 1] == "3"

    def test_sse_adds_the_checked_matrix_twin_one_per_lane_cpu(self, tmp_path):
        backend = StressNgBackend()
        backend._binary = "/usr/bin/stress-ng"
        cmd = backend.get_command(StressConfig(mode=StressMode.SSE, threads=3, cpus=(0, 1, 2)), tmp_path)
        assert "--matrix" in cmd
        assert cmd[cmd.index("--matrix") + 1] == "3"

    @pytest.mark.parametrize(
        "line",
        [
            "stress-ng: fail:  [101] cpu: sqrt error detected on sqrt(48271)",
            "stress-ng: fail:  [102] matrix: prod: data difference between identical matrix computations",
        ],
    )
    def test_a_verify_failure_is_a_computation_error(self, line, tmp_path):
        found = StressNgBackend().findings(line + "\n" + NG_ONE_FAILED, tmp_path)
        assert _strongest(found).outcome is Outcome.COMPUTATION
        assert line in {signal.text for signal in found}

    def test_a_clean_stop_finds_nothing(self, tmp_path):
        assert StressNgBackend().findings(NG_CLEAN_STOP, tmp_path) == ()

    @pytest.mark.parametrize("number,name", [(4, "SIGILL"), (7, "SIGBUS"), (8, "SIGFPE"), (11, "SIGSEGV")])
    def test_a_stressor_signal_line_is_a_cpu_signal(self, number, name, tmp_path):
        line = f"stress-ng: info:  [101] stressor terminated with unexpected signal {number} '{name}'"
        found = StressNgBackend().findings(line + "\n" + NG_ONE_FAILED, tmp_path)
        assert _strongest(found) == LaneSignal(Outcome.CPU_SIGNAL, line)

    def test_an_abort_signal_line_is_unexplained_never_silicon(self, tmp_path):
        line = "stress-ng: info:  [101] stressor terminated with unexpected signal 6 'SIGABRT'"
        found = StressNgBackend().findings(f"{line}\nbacktrace:\n  stress-ng(+0x1c2f4)\n{NG_ONE_FAILED}", tmp_path)
        assert _strongest(found) == LaneSignal(Outcome.UNEXPLAINED, line)
        assert not [signal for signal in found if signal.outcome.kind is OutcomeKind.SILICON]

    def test_a_failed_stressor_read_before_the_summary_is_a_computation_error(self, tmp_path):
        line = "stress-ng: error: [100] cpu: [101] terminated with an error, exit status=2 (stressor failed)"
        assert StressNgBackend().findings(line + "\n", tmp_path) == (LaneSignal(Outcome.COMPUTATION, line),)

    def test_the_failure_summary_alone_is_a_computation_error(self, tmp_path):
        found = StressNgBackend().findings(NG_ONE_FAILED, tmp_path)
        assert found == (
            LaneSignal(Outcome.COMPUTATION, "stress-ng: info:  [100] unsuccessful run completed in 0.52 secs"),
        )

    @pytest.mark.parametrize(
        "skip",
        [
            "stress-ng: info:  [101] cpu: failed to mmap 4096 bytes, errno=12 (Cannot allocate memory), "
            "skipping stressor\nstress-ng: warn:  [100] cpu: [101] aborted early, no system resources\n"
            + _ng_summary("1: matrix (1)", "0", "1: cpu (1)", "successful"),
            "stress-ng: fail:  [102] matrix: matrix allocation failed, out of memory (12.0M mem free, 0 swap free), "
            "errno=12 (Cannot allocate memory)\nstress-ng: warn:  [100] matrix: [102] aborted early, no system "
            "resources\n" + _ng_summary("1: cpu (1)", "0", "1: matrix (1)", "successful"),
        ],
        ids=["cpu-mmap-refused", "matrix-out-of-memory"],
    )
    def test_no_system_resources_is_not_started_never_a_computation_error(self, skip, tmp_path):
        found = StressNgBackend().findings(skip, tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.NOT_STARTED]
        assert "aborted early, no system resources" in found[0].text

    @pytest.mark.parametrize(
        "refusal",
        [
            "option cpu-method choice 'fft' not known, choices are: ackermann apery bitops",
            "option matrix-method choice 'prod' not known, there are none available (stressor unimplemented)",
        ],
        ids=["method-not-built", "stressor-unimplemented"],
    )
    def test_an_unavailable_method_is_not_started(self, refusal, tmp_path):
        assert StressNgBackend().findings("\n" + refusal + "\n", tmp_path) == (
            LaneSignal(Outcome.NOT_STARTED, refusal),
        )


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

_MEASURED_STARTUP_HEAD = """\
Running from console...


Checking processor/OS features...

Reading Hardware Topology...

Logical Cores:
    0-31


"""

_MEASURED_UNKNOWN_TEST = (
    _MEASURED_STARTUP_HEAD + "Exception Encountered: InvalidParametersException\n\nUnknown Test: NOSUCHTEST\n\n"
)
_MEASURED_BAD_CORE = (
    _MEASURED_STARTUP_HEAD
    + "Exception Encountered: InvalidParametersException\n\nIn Function: CoreSelector::load_config()\n\n"
    + "Invalid core #: 96\n\n"
)
_MEASURED_MISSING_KEY = _MEASURED_STARTUP_HEAD + "Exception Encountered: KeyNotFoundException\n\nTests\n\n"

_CAPTURED_INVALID_PARAM_OUTPUT = """\
Reading Hardware Topology...

Logical Cores:
    0 1 2 3

Invalid Parameter: SSE
Press ENTER to continue . . .
"""

_CAPTURED_ALGORITHM_FAILURE = """\
Exception Encountered: AlgorithmFailedException
In Function: FFTv4::fixup_bottom()
Bottom word mismatch.
"""

_YCRUNCHER_BINARIES = (
    "24-ZN5 ~ Komari",
    "22-ZN4 ~ Kizuna",
    "19-ZN2 ~ Kagari",
    "17-ZN1 ~ Yukina",
    "12-BD2 ~ Miyu",
    "18-CNL ~ Shinoa",
    "17-SKX ~ Kotori",
    "14-BDW ~ Kurumi",
    "13-HSW ~ Airi",
    "11-SNB ~ Hina",
    "08-NHM ~ Ushio",
    "05-A64 ~ Kasumi",
)


def _install(root: Path, names: tuple[str, ...] = _YCRUNCHER_BINARIES) -> Path:
    (root / "Binaries").mkdir(parents=True)
    for name in names:
        (root / "Binaries" / name).write_text("")
    launcher = root / "y-cruncher"
    launcher.write_text("")
    return launcher


def _on_cpu(monkeypatch, tmp_path: Path, vendor: str, *flag_lines: str) -> None:
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text(
        "".join(
            f"processor\t: {n}\nvendor_id\t: {vendor}\nflags\t\t: {flags}\n\n" for n, flags in enumerate(flag_lines)
        )
    )
    monkeypatch.setattr(topology, "CPUINFO", cpuinfo)


def _prepared(launcher: Path, tmp_path: Path, config: StressConfig | None = None) -> tuple[YCruncherBackend, Path]:
    backend = YCruncherBackend()
    backend._binary = str(launcher)
    work_dir = tmp_path / "run"
    work_dir.mkdir(exist_ok=True)
    backend.prepare(work_dir, config or StressConfig(cpus=(3,)))
    return backend, work_dir


def _config_fields(work_dir: Path) -> dict[str, str]:
    return dict(re.findall(r"^\s*(\w+) : (.+)$", (work_dir / "stress.cfg").read_text(), re.MULTILINE))


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

    def test_prepare_picks_the_best_binary_this_cpu_can_run(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        backend, work_dir = _prepared(launcher, tmp_path)
        command = backend.get_command(StressConfig(cpus=(3,)), work_dir)
        assert command[0] == str(launcher.parent / "Binaries" / "19-ZN2 ~ Kagari")

    def test_prepare_refuses_when_no_binary_fits(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc", ("24-ZN5 ~ Komari", "22-ZN4 ~ Kizuna"))
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        with pytest.raises(OSError, match=r"no y-cruncher binary for this CPU \(AuthenticAMD\)"):
            _prepared(launcher, tmp_path)

    def test_the_config_names_exactly_the_lane_cpus(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        _, work_dir = _prepared(launcher, tmp_path, StressConfig(cpus=(3, 19)))
        assert _config_fields(work_dir)["LogicalCores"] == "[3 19]"

    def test_the_command_starts_the_compute_binary_not_the_launcher(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        backend, work_dir = _prepared(launcher, tmp_path)
        assert backend.get_command(StressConfig(cpus=(3,)), work_dir) == [
            str(launcher.parent / "Binaries" / "19-ZN2 ~ Kagari"),
            "skip-warnings",
            "pause:-2",
            "colors:0",
            "status:none",
            "config",
            str(work_dir / "stress.cfg"),
        ]

    @pytest.mark.parametrize(
        ("vendor", "flags", "absent", "expected"),
        [
            pytest.param("AuthenticAMD", _ZEN5_FLAGS, (), "24-ZN5 ~ Komari", id="zen5-gets-the-vp2intersect-build"),
            pytest.param("AuthenticAMD", _ZEN4_FLAGS, (), "22-ZN4 ~ Kizuna", id="zen4-lacks-vp2intersect"),
            pytest.param("AuthenticAMD", _ZEN1_FLAGS, (), "17-ZN1 ~ Yukina", id="zen1-lacks-rdpid"),
            pytest.param("GenuineIntel", _ZEN5_FLAGS, (), "18-CNL ~ Shinoa", id="intel-never-gets-an-amd-build"),
            pytest.param("GenuineIntel", _HASWELL_FLAGS, (), "13-HSW ~ Airi", id="haswell-lacks-adx"),
            pytest.param("GenuineIntel", _ALDER_LAKE_FLAGS, (), "14-BDW ~ Kurumi", id="gfni-without-avx512"),
            pytest.param("GenuineIntel", _SKYLAKE_X_FLAGS, (), "17-SKX ~ Kotori", id="skylake-x-lacks-ifma-and-vbmi"),
            pytest.param(
                "GenuineIntel", _without(_HASWELL_FLAGS, "avx2"), (), "11-SNB ~ Hina", id="haswell-without-avx2"
            ),
            pytest.param("AuthenticAMD", _BULLDOZER_FLAGS, (), "11-SNB ~ Hina", id="bulldozer-lacks-fma3"),
            pytest.param("AuthenticAMD", _without(_ZEN1_FLAGS, "avx2"), (), "12-BD2 ~ Miyu", id="zen1-without-avx2"),
            pytest.param("AuthenticAMD", _without(_ZEN1_FLAGS, "adx"), (), "12-BD2 ~ Miyu", id="zen1-without-adx"),
            pytest.param("AuthenticAMD", _without(_ZEN1_FLAGS, "avx"), (), "08-NHM ~ Ushio", id="zen1-without-avx"),
            pytest.param(
                "AuthenticAMD", _ZEN3_FLAGS, ("19-ZN2 ~ Kagari",), "17-ZN1 ~ Yukina", id="absent-best-falls-to-next"
            ),
            pytest.param("HygonGenuine", _ZEN3_FLAGS, (), None, id="unknown-vendor-gets-none"),
            pytest.param("AuthenticAMD", "fpu vme de pse tsc", (), None, id="no-x64-gets-none"),
        ],
    )
    def test_compute_binary_walks_the_launchers_order(self, tmp_path, vendor, flags, absent, expected):
        launcher = _install(tmp_path / "yc", tuple(name for name in _YCRUNCHER_BINARIES if name not in absent))
        binaries = launcher.parent / "Binaries"
        found = ycruncher.compute_binary(binaries, frozenset(flags.split()), vendor)
        assert found == (None if expected is None else binaries / expected)

    def test_only_flags_every_cpu_reports_count(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS, _ZEN1_FLAGS)
        backend, work_dir = _prepared(launcher, tmp_path)
        assert Path(backend.get_command(StressConfig(cpus=(3,)), work_dir)[0]).name == "17-ZN1 ~ Yukina"

    def test_a_cpuinfo_without_flags_starts_nothing(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("processor\t: 0\nvendor_id\t: AuthenticAMD\nFeatures\t: fp asimd\n")
        monkeypatch.setattr(topology, "CPUINFO", cpuinfo)
        with pytest.raises(OSError, match="no y-cruncher binary for this CPU"):
            _prepared(launcher, tmp_path)

    def test_a_cpuinfo_without_a_vendor_says_so_when_no_binary_fits(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text("processor\t: 0\nflags\t: fpu sse sse2 avx avx2\n")
        monkeypatch.setattr(topology, "CPUINFO", cpuinfo)
        with pytest.raises(OSError, match=r"no y-cruncher binary for this CPU \(unknown vendor\)"):
            _prepared(launcher, tmp_path)

    def test_an_unreadable_cpuinfo_starts_nothing(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        monkeypatch.setattr(topology, "CPUINFO", tmp_path / "absent-cpuinfo")
        with pytest.raises(OSError, match="absent-cpuinfo"):
            _prepared(launcher, tmp_path)

    def test_prepare_finds_the_binaries_of_a_nixpkgs_install(self, tmp_path, monkeypatch):
        root = tmp_path / "store-path"
        _install(root / "lib" / "y-cruncher")
        wrapper = root / "bin" / "y-cruncher"
        wrapper.parent.mkdir()
        wrapper.write_text("")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        backend, work_dir = _prepared(wrapper, tmp_path)
        command = backend.get_command(StressConfig(cpus=(3,)), work_dir)
        assert command[0] == str(root / "lib" / "y-cruncher" / "Binaries" / "19-ZN2 ~ Kagari")

    def test_prepare_names_both_places_when_no_binaries_dir_exists(self, tmp_path, monkeypatch):
        launcher = tmp_path / "bin" / "y-cruncher"
        launcher.parent.mkdir()
        launcher.write_text("")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        with pytest.raises(OSError) as refused:
            _prepared(launcher, tmp_path)
        assert str(tmp_path / "bin" / "Binaries") in str(refused.value)
        assert str(tmp_path / "lib" / "y-cruncher" / "Binaries") in str(refused.value)

    def test_prepare_refuses_a_lane_without_cpus(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        with pytest.raises(RuntimeError, match="CPUs"):
            _prepared(launcher, tmp_path, StressConfig(cpus=()))
        assert not (tmp_path / "run" / "stress.cfg").exists()

    def test_prepare_without_a_launcher_raises(self, tmp_path, on_path):
        on_path({})
        with pytest.raises(RuntimeError, match="y-cruncher binary not found"):
            YCruncherBackend().prepare(tmp_path, StressConfig(cpus=(3,)))

    def test_the_command_needs_a_prepared_binary(self, tmp_path):
        backend = YCruncherBackend()
        backend._binary = "/bin/y-cruncher"
        with pytest.raises(RuntimeError, match="prepare"):
            backend.get_command(StressConfig(cpus=(3,)), tmp_path)

    def test_the_config_runs_until_stopped_and_stops_at_the_first_error(self, tmp_path, monkeypatch):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        _, work_dir = _prepared(launcher, tmp_path)
        fields = _config_fields(work_dir)
        assert fields["Action"] == '"StressTest"'
        assert fields["SecondsTotal"] == "0"
        assert fields["StopOnError"] == '"true"'
        assert fields["AllocateLocally"] == '"true"'

    @pytest.mark.parametrize(("memory_mb", "expected_bytes"), [(2048, 2048 * 2**20), (512, 512 * 2**20)])
    def test_the_config_holds_the_requested_memory(self, tmp_path, monkeypatch, memory_mb, expected_bytes):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        _, work_dir = _prepared(launcher, tmp_path, StressConfig(cpus=(3,), memory_mb=memory_mb))
        assert int(_config_fields(work_dir)["TotalMemory"]) == expected_bytes

    @pytest.mark.parametrize("memory_mb", [None, 0])
    def test_an_unset_memory_still_names_a_budget(self, tmp_path, monkeypatch, memory_mb):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        _, work_dir = _prepared(launcher, tmp_path, StressConfig(cpus=(3,), memory_mb=memory_mb))
        assert int(_config_fields(work_dir)["TotalMemory"]) >= 2**20

    @pytest.mark.parametrize("mode", list(StressMode))
    def test_the_config_runs_the_modes_tests(self, tmp_path, monkeypatch, mode):
        launcher = _install(tmp_path / "yc")
        _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN3_FLAGS)
        _, work_dir = _prepared(launcher, tmp_path, StressConfig(mode=mode, cpus=(3,)))
        listed = re.search(r"Tests : \[(.*?)\]", (work_dir / "stress.cfg").read_text(), re.DOTALL)
        assert listed is not None
        named = re.findall(r'"(\w+)"', listed[1])
        assert named == list(MODE_TO_ALGORITHMS[mode])
        assert named and set(named) <= set(ALL_TESTS)

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
            ("\x1b[01;31mCoefficient is too large\x1b[0m\n", "Coefficient is too large"),
            ("Running SFTv4: Failed  Test Speed:  5.2 * 10^08  bits / sec\n", "Running SFTv4: Failed"),
            ("Stress test failed with 1 error.\n", "Stress test failed with 1 error"),
            ("Exception Encountered: StressTestFailedException\n", "StressTestFailedException"),
        ],
    )
    def test_an_error_line_is_a_computation_error(self, output, named, tmp_path):
        found = YCruncherBackend().findings(output, tmp_path)
        assert found and {signal.outcome for signal in found} == {Outcome.COMPUTATION}
        assert any(named in signal.text for signal in found)
        assert not any("\x1b" in signal.text for signal in found)

    def test_an_exception_encountered_line_is_a_computation_error(self, tmp_path):
        found = YCruncherBackend().findings(_CAPTURED_ALGORITHM_FAILURE, tmp_path)
        assert {signal.outcome for signal in found} == {Outcome.COMPUTATION}
        assert any("AlgorithmFailedException" in signal.text for signal in found)

    def test_a_config_error_in_an_exception_block_is_not_a_computation_error(self, tmp_path):
        output = "Exception Encountered: InvalidParametersException\nIn Function: core_to_node()\nInvalid core id\n"
        found = YCruncherBackend().findings(output, tmp_path)
        assert {signal.outcome for signal in found} == {Outcome.NOT_STARTED}

    @pytest.mark.parametrize(
        ("output", "reason"),
        [
            (_MEASURED_UNKNOWN_TEST, "Unknown Test: NOSUCHTEST"),
            (_MEASURED_BAD_CORE, "Invalid core #: 96"),
            (_MEASURED_MISSING_KEY, "Exception Encountered: KeyNotFoundException"),
        ],
        ids=["unknown-test", "bad-core", "missing-key"],
    )
    def test_a_startup_exception_the_pinned_binary_prints_is_not_started(self, tmp_path, output, reason):
        found = YCruncherBackend().findings(output, tmp_path)
        assert {signal.outcome for signal in found} == {Outcome.NOT_STARTED}
        assert reason in {signal.text for signal in found}

    def test_invalid_parameter_is_not_started(self, tmp_path):
        found = YCruncherBackend().findings(_CAPTURED_INVALID_PARAM_OUTPUT, tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.NOT_STARTED]
        assert "Invalid Parameter: SSE" in found[0].text

    def test_an_invalid_core_id_is_not_started(self, tmp_path):
        found = YCruncherBackend().findings("Invalid core id\n", tmp_path)
        assert [signal.outcome for signal in found] == [Outcome.NOT_STARTED]


class TestYCruncherModeMapping:
    @pytest.mark.parametrize("mode", list(StressMode))
    def test_every_mode_maps(self, mode):
        assert mode in MODE_TO_ALGORITHMS

    def test_sse_is_scalar_only(self):
        assert MODE_TO_ALGORITHMS[StressMode.SSE] == ("BKT",)

    @pytest.mark.parametrize("mode", [StressMode.AVX2, StressMode.AVX512, StressMode.CUSTOM])
    def test_a_mode_without_its_own_list_names_every_test(self, mode):
        assert MODE_TO_ALGORITHMS[mode] == ALL_TESTS

    def test_every_mode_names_only_tests_the_binary_offers(self):
        for algos in MODE_TO_ALGORITHMS.values():
            assert algos and set(algos) <= set(ALL_TESTS)


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
