"""Edge coverage for history context, settings profiles, and the run logger."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.config import settings
from corecycler.history import context
from corecycler.history.logger import TestRunLogger


class TestHistoryContextEdges:
    def test_capture_context_when_every_smu_read_comes_back_unread(self, tmp_path, caplog):
        smu = MagicMock()
        smu.get_all_co_offsets.return_value = dict.fromkeys(range(4))
        smu.get_pbo_scalar.return_value = None
        smu.get_boost_limit.return_value = None
        smu.commands.has_co = True
        with patch("corecycler.smu.pmtable.read_power_limits", return_value=(None, None, None)):
            rec = context.capture_system_context(smu=smu, num_cores=4, bios_path=tmp_path / "none")
        assert rec.co_hash == ""
        assert "the CO of cores [0, 1, 2, 3] could not be read" in caplog.text

    def test_a_cpu_without_co_reads_leaves_no_warning(self, tmp_path, caplog):
        smu = MagicMock()
        smu.get_all_co_offsets.return_value = dict.fromkeys(range(4))
        smu.get_pbo_scalar.return_value = None
        smu.get_boost_limit.return_value = None
        smu.commands.has_co = False
        with patch("corecycler.smu.pmtable.read_power_limits", return_value=(None, None, None)):
            context.capture_system_context(smu=smu, num_cores=4, bios_path=tmp_path / "none")
        assert "could not be read" not in caplog.text

    def test_an_unexpected_error_in_a_read_is_not_swallowed(self, tmp_path):
        smu = MagicMock()
        smu.get_all_co_offsets.side_effect = RuntimeError("driver bug")
        with pytest.raises(RuntimeError, match="driver bug"):
            context.capture_system_context(smu=smu, num_cores=4, bios_path=tmp_path / "none")


class TestSettingsCoProfile:
    def test_save_then_load_roundtrip(self, tmp_path):
        p = tmp_path / "sub" / "co.json"
        settings.save_co_profile({0: -30, 5: -15}, p, cpu_model="Ryzen", source="tuner")
        assert p.exists()
        assert settings.load_co_profile(p) == {0: -30, 5: -15}


class TestLoggerEarlyReturns:
    def _logger(self) -> TestRunLogger:
        lg = TestRunLogger.__new__(TestRunLogger)
        lg._result_id_by_core = {}
        return lg

    def test_on_status_updated_unknown_core_returns(self):
        self._logger().on_status_updated(99, None)

    def test_update_core_telemetry_peaks_unknown_core_returns(self):
        self._logger().update_core_telemetry_peaks(99)
