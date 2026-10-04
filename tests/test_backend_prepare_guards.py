"""A backend launch must refuse when its prepared config is not readable."""

from __future__ import annotations

import pytest

from corecycler.engine.backends.base import StressBackend, StressConfig
from corecycler.engine.backends.mprime import MprimeBackend
from corecycler.engine.backends.stress_ng import StressNgBackend


class TestMprimeAssertPrepared:
    def test_a_prepared_dir_passes(self, tmp_path):
        backend = MprimeBackend()
        backend._binary = "/bin/true"
        backend.prepare(tmp_path, StressConfig())
        backend.assert_prepared(tmp_path)

    def test_a_missing_local_txt_refuses(self, tmp_path):
        backend = MprimeBackend()
        backend._binary = "/bin/true"
        backend.prepare(tmp_path, StressConfig())
        (tmp_path / "local.txt").unlink()
        with pytest.raises(OSError, match="local.txt"):
            backend.assert_prepared(tmp_path)

    def test_a_missing_prime_txt_refuses(self, tmp_path):
        backend = MprimeBackend()
        backend._binary = "/bin/true"
        backend.prepare(tmp_path, StressConfig())
        (tmp_path / "prime.txt").unlink()
        with pytest.raises(OSError, match="prime.txt"):
            backend.assert_prepared(tmp_path)

    def test_an_empty_dir_names_the_fallback_danger(self, tmp_path):
        backend = MprimeBackend()
        with pytest.raises(OSError, match="self-pinned worker per core"):
            backend.assert_prepared(tmp_path)


class TestConfiglessBackendsHaveNoGuard:
    def test_the_base_guard_accepts_anything(self, tmp_path):
        StressBackend.assert_prepared(StressNgBackend(), tmp_path)

    def test_a_configless_backend_reads_no_results_file(self, tmp_path):
        (tmp_path / "results.txt").write_text("FATAL ERROR: Rounding was 0.5\n")
        assert StressNgBackend().findings("", tmp_path) == ()
