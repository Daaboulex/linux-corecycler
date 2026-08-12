"""Ring B backend drift tests — the real packaged mprime on real hardware.

The config contract was verified against mprime 31.04; a version outside the
verified set means the CpuSupports/EnableSetAffinity semantics must be
re-proven before its verdicts are trusted.
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from corecycler.engine import containment, execution
from corecycler.engine.backends.base import StressConfig, StressMode
from corecycler.engine.backends.mprime import VERIFIED_MPRIME_VERSIONS, MprimeBackend

pytestmark = pytest.mark.contract

_MPRIME_OVERRIDE = os.environ.get("CORECYCLER_MPRIME_BIN", "")

MODE_TO_FFT_MARKER = {
    StressMode.SSE: "type-1 FFT",
    StressMode.AVX: "AVX FFT",
    StressMode.AVX2: "FMA3 FFT",
    StressMode.AVX512: "AVX-512 FFT",
}

CPUS = (5, 21)


def _backend() -> MprimeBackend:
    backend = MprimeBackend()
    if _MPRIME_OVERRIDE:
        backend._binary = _MPRIME_OVERRIDE
        return backend
    if not backend.is_available():
        pytest.skip("mprime not installed on this host")
    return backend


def test_the_packaged_mprime_is_a_verified_version():
    version = _backend().installed_version()
    assert version is not None, "mprime -v produced no parseable version"
    assert version in VERIFIED_MPRIME_VERSIONS, (
        f"mprime {version} is outside the verified set {sorted(VERIFIED_MPRIME_VERSIONS)}; "
        "re-prove the config-key semantics before trusting its verdicts"
    )


@pytest.mark.parametrize("mode", list(MODE_TO_FFT_MARKER))
def test_each_mode_produces_its_own_fft_path(mode, tmp_path):
    backend = _backend()
    if containment.available_mechanism(refresh=True) is None:
        pytest.skip("no systemd cgroup scope available on this host")
    config = StressConfig(mode=mode, threads=2)
    backend.prepare(tmp_path, config)
    backend.assert_prepared(tmp_path)
    log_path = tmp_path / "startup.log"
    cmd = containment.contain(CPUS).prefix + backend.get_command(config, tmp_path)
    with log_path.open("w") as sink:
        proc = subprocess.Popen(
            cmd,
            stdout=sink,
            stderr=subprocess.STDOUT,
            cwd=str(tmp_path),
            preexec_fn=execution.make_preexec(),
        )
    try:
        deadline = time.monotonic() + 20
        line = ""
        while time.monotonic() < deadline:
            for candidate in log_path.read_text().splitlines():
                if "FFT length" in candidate:
                    line = candidate
                    break
            if line:
                break
            time.sleep(0.3)
        assert line, f"mprime printed no FFT line for {mode.name} within 20s"
        assert MODE_TO_FFT_MARKER[mode] in line, (mode.name, line)
        observed = containment.observed_tree_cpus(proc.pid)
        assert observed and observed <= set(CPUS), (
            f"{mode.name}: threads on {sorted(observed)}, allowed {CPUS}"
        )
    finally:
        execution.kill_process_group(proc)
        backend.cleanup(tmp_path)


def _flags_in_help(binary_key: str, names: tuple[str, ...], flags: set[str]) -> None:
    from corecycler.config import tools

    resolution = tools.resolve(binary_key)
    if resolution.path is None:
        pytest.skip(f"{binary_key} not installed on this host")
    result = subprocess.run(
        [str(resolution.path), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    help_text = result.stdout + result.stderr
    missing = sorted(f for f in flags if f not in help_text)
    assert not missing, (
        f"{binary_key} {names} no longer documents {missing}; the backend's "
        "command construction assumes these flags -- re-verify against its docs"
    )


def test_stress_ng_still_documents_every_flag_the_backend_uses():
    _flags_in_help(
        "stress-ng",
        ("stress-ng",),
        {
            "--cpu", "--cpu-method", "--verify", "--metrics-brief", "--temp-path",
            "--matrix", "--matrix-method", "--vm", "--vm-bytes", "--timeout",
        },
    )


def test_stress_ng_still_offers_the_cpu_methods_the_modes_map_to():
    from corecycler.config import tools
    from corecycler.engine.backends.stress_ng import _mode_to_method

    resolution = tools.resolve("stress-ng")
    if resolution.path is None:
        pytest.skip("stress-ng not installed on this host")
    result = subprocess.run(
        [str(resolution.path), "--cpu-method", "which"],
        capture_output=True, text=True, timeout=30,
    )
    offered = set((result.stdout + result.stderr).split())
    wanted = {_mode_to_method(m) for m in (StressMode.SSE, StressMode.AVX, StressMode.AVX2)}
    missing = sorted(wanted - offered)
    assert not missing, f"stress-ng no longer offers cpu-method(s) {missing}"


def test_stressapptest_still_documents_every_flag_the_backend_uses():
    _flags_in_help("stressapptest", ("stressapptest",), {"-M", "-s", "-W"})
