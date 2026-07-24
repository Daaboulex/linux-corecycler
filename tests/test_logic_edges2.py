"""Edge coverage for history context, settings profiles, and the run logger."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.config import settings
from corecycler.history import context
from corecycler.history.logger import TestRunLogger


class TestHistoryContextEdges:
    def test_read_bios_version_unreadable(self, tmp_path):
        d = tmp_path / "bios_version"
        d.mkdir()  # exists() True, read_text() raises OSError
        assert context.read_bios_version(d) == ""

    def test_capture_context_when_every_smu_read_fails(self, tmp_path):
        smu = MagicMock()
        smu.get_all_co_offsets.side_effect = RuntimeError("x")
        smu.get_pbo_scalar.side_effect = RuntimeError("x")
        smu.get_boost_limit.side_effect = RuntimeError("x")
        with patch("corecycler.smu.pmtable.read_power_limits", side_effect=RuntimeError("x")):
            rec = context.capture_system_context(smu=smu, num_cores=4, bios_path=tmp_path / "none")
        assert rec.co_hash == ""


class TestSettingsCoProfile:
    def test_save_then_load_roundtrip(self, tmp_path):
        p = tmp_path / "sub" / "co.json"
        settings.save_co_profile({0: -30, 5: -15}, p, cpu_model="Ryzen", source="tuner")
        assert p.exists()
        assert settings.load_co_profile(p) == {0: -30, 5: -15}


class TestLoggerEarlyReturns:
    def _logger(self) -> TestRunLogger:
        lg = TestRunLogger.__new__(TestRunLogger)
        lg._active_result_ids = {}
        return lg

    def test_on_status_updated_unknown_core_returns(self):
        self._logger().on_status_updated(99, None)

    def test_update_core_telemetry_peaks_unknown_core_returns(self):
        self._logger().update_core_telemetry_peaks(99)
