"""Thermal-safety regression tests.

Covers the debounced temperature check (a load-ramp spike must not stop a test
and read as a core failure during auto-tuning) and the tuner's thermal config.
The tuner-side retry behavior is exercised in test_tuner_engine.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from engine.backends.base import StressConfig
from engine.scheduler import CoreScheduler, SchedulerConfig
from engine.topology import CPUTopology, PhysicalCore
from tuner.config import TunerConfig


def _topology(num_cores: int = 4) -> CPUTopology:
    topo = CPUTopology()
    topo.physical_cores = num_cores
    topo.smt_enabled = False
    topo.logical_cpus_count = num_cores
    for i in range(num_cores):
        topo.cores[i] = PhysicalCore(core_id=i, ccd=0, ccx=None, logical_cpus=(i,))
    return topo


def _scheduler(tmp_path, **cfg) -> CoreScheduler:
    cfg.setdefault("max_temperature", 95.0)
    return CoreScheduler(
        topology=_topology(),
        backend=MagicMock(),
        stress_config=StressConfig(),
        scheduler_config=SchedulerConfig(**cfg),
        work_dir=tmp_path,
    )


class TestTemperatureDebounce:
    """_check_temperature tolerates transient spikes, stops sustained over-temp."""

    def test_transient_spike_within_grace_does_not_trip(self, tmp_path):
        sched = _scheduler(tmp_path, over_temp_grace_seconds=3.0)
        clock = [0.0]
        with (
            patch.object(sched, "_read_cpu_temperature", return_value=100.0),
            patch("time.monotonic", side_effect=lambda: clock[0]),
        ):
            assert sched._check_temperature() is True  # t=0: window opens
            clock[0] = 1.0
            assert sched._check_temperature() is True  # 1s < 3s grace
            clock[0] = 2.5
            assert sched._check_temperature() is True  # 2.5s < 3s grace
        assert sched._thermal_tripped is False

    def test_sustained_over_temp_trips_after_grace(self, tmp_path):
        sched = _scheduler(tmp_path, over_temp_grace_seconds=3.0)
        clock = [0.0]
        with (
            patch.object(sched, "_read_cpu_temperature", return_value=100.0),
            patch("time.monotonic", side_effect=lambda: clock[0]),
        ):
            assert sched._check_temperature() is True  # window opens
            clock[0] = 3.1
            assert sched._check_temperature() is False  # sustained >= grace → trip
        assert sched._thermal_tripped is True

    def test_spike_then_recovery_resets_window(self, tmp_path):
        sched = _scheduler(tmp_path, over_temp_grace_seconds=3.0)
        temps = [100.0, 100.0, 80.0, 100.0]
        idx = [0]
        clock = [0.0]
        with (
            patch.object(sched, "_read_cpu_temperature", side_effect=lambda: temps[idx[0]]),
            patch("time.monotonic", side_effect=lambda: clock[0]),
        ):
            idx[0], clock[0] = 0, 0.0
            assert sched._check_temperature() is True  # window opens
            idx[0], clock[0] = 1, 1.0
            assert sched._check_temperature() is True
            idx[0], clock[0] = 2, 2.0
            assert sched._check_temperature() is True  # recovered → window reset
            idx[0], clock[0] = 3, 2.5
            assert sched._check_temperature() is True  # new window, 0s elapsed
        assert sched._thermal_tripped is False

    def test_hard_ceiling_trips_immediately(self, tmp_path):
        sched = _scheduler(tmp_path, over_temp_grace_seconds=3.0, over_temp_hard_margin=8.0)
        clock = [0.0]
        with (
            patch.object(sched, "_read_cpu_temperature", return_value=103.5),
            patch("time.monotonic", side_effect=lambda: clock[0]),
        ):
            # 103.5 >= 95 + 8 = 103 → instant trip despite the grace window
            assert sched._check_temperature() is False
        assert sched._thermal_tripped is True

    def test_hysteresis_governs_resume(self, tmp_path):
        sched = _scheduler(tmp_path, over_temp_grace_seconds=0.0)
        clock = [0.0]
        with patch("time.monotonic", side_effect=lambda: clock[0]):
            with patch.object(sched, "_read_cpu_temperature", return_value=100.0):
                assert sched._check_temperature() is False  # grace 0 → trips now
            with patch.object(sched, "_read_cpu_temperature", return_value=92.0):
                assert sched._check_temperature() is False  # 92 > 90 → stay tripped
            with patch.object(sched, "_read_cpu_temperature", return_value=89.0):
                assert sched._check_temperature() is True  # 89 < 95-5 → resume
        assert sched._thermal_tripped is False

    def test_unreadable_temperature_does_not_block(self, tmp_path):
        sched = _scheduler(tmp_path)
        with patch.object(sched, "_read_cpu_temperature", return_value=None):
            assert sched._check_temperature() is True


class TestThermalTunerConfig:
    """The tuner exposes the safety limit + retry policy (so it reaches the scheduler)."""

    def test_thermal_defaults(self):
        c = TunerConfig()
        assert c.max_temperature_c == 95.0
        assert c.over_temp_grace_seconds == 3.0
        assert c.over_temp_hard_margin_c == 8.0
        assert c.max_thermal_retries == 3
        assert c.thermal_cooldown_seconds == 5.0

    def test_out_of_range_temperature_rejected(self):
        assert any("max_temperature_c" in e for e in TunerConfig(max_temperature_c=200).validate())
        assert any("max_temperature_c" in e for e in TunerConfig(max_temperature_c=40).validate())

    def test_negative_retries_rejected(self):
        assert any("max_thermal_retries" in e for e in TunerConfig(max_thermal_retries=-1).validate())

    def test_negative_cooldown_rejected(self):
        assert any(
            "thermal_cooldown_seconds" in e
            for e in TunerConfig(thermal_cooldown_seconds=-1).validate()
        )

    def test_valid_thermal_config_has_no_errors(self):
        cfg = TunerConfig(
            max_temperature_c=90.0,
            over_temp_grace_seconds=2.0,
            over_temp_hard_margin_c=5.0,
            max_thermal_retries=2,
        )
        assert cfg.validate() == []

    def test_json_roundtrip_preserves_thermal(self):
        c = TunerConfig(max_temperature_c=88.0, max_thermal_retries=5)
        restored = TunerConfig.from_json(c.to_json())
        assert restored.max_temperature_c == 88.0
        assert restored.max_thermal_retries == 5
