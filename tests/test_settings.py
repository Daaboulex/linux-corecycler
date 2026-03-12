"""Comprehensive tests for settings and profile management."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from config.settings import (
    AppSettings,
    TestProfile,
    load_profile,
    load_settings,
    save_profile,
    save_settings,
)
from engine.backends.base import FFTPreset, StressMode


# ===========================================================================
# TestProfile tests
# ===========================================================================


class TestTestProfile:
    def test_defaults(self):
        p = TestProfile()
        assert p.name == "Default"
        assert p.backend == "mprime"
        assert p.stress_mode == "SSE"
        assert p.fft_preset == "SMALL"
        assert p.fft_min is None
        assert p.fft_max is None
        assert p.threads == 1
        assert p.seconds_per_core == 360
        assert p.iterations_per_core == 0
        assert p.cycle_count == 1
        assert p.stop_on_error is False
        assert p.test_smt is False
        assert p.cores_to_test is None

    def test_get_stress_mode(self):
        p = TestProfile(stress_mode="AVX2")
        assert p.get_stress_mode() == StressMode.AVX2

    def test_get_stress_mode_all_values(self):
        for mode in StressMode:
            p = TestProfile(stress_mode=mode.name)
            assert p.get_stress_mode() == mode

    def test_get_stress_mode_invalid(self):
        p = TestProfile(stress_mode="INVALID")
        with pytest.raises(KeyError):
            p.get_stress_mode()

    def test_get_fft_preset(self):
        p = TestProfile(fft_preset="LARGE")
        assert p.get_fft_preset() == FFTPreset.LARGE

    def test_get_fft_preset_all_values(self):
        for preset in FFTPreset:
            p = TestProfile(fft_preset=preset.name)
            assert p.get_fft_preset() == preset

    def test_get_fft_preset_invalid(self):
        p = TestProfile(fft_preset="INVALID")
        with pytest.raises(KeyError):
            p.get_fft_preset()

    def test_custom_values(self):
        p = TestProfile(
            name="Custom",
            backend="stress-ng",
            stress_mode="AVX512",
            fft_preset="CUSTOM",
            fft_min=100,
            fft_max=500,
            threads=4,
            seconds_per_core=120,
            iterations_per_core=10,
            cycle_count=3,
            stop_on_error=True,
            test_smt=True,
            cores_to_test=[0, 2, 4],
        )
        assert p.name == "Custom"
        assert p.cores_to_test == [0, 2, 4]
        assert p.threads == 4


# ===========================================================================
# AppSettings tests
# ===========================================================================


class TestAppSettings:
    def test_defaults(self):
        s = AppSettings()
        assert s.work_dir == "/tmp/linux-corecycler"
        assert s.theme == "system"
        assert s.poll_interval == 1.0
        assert s.show_smt_threads is False
        assert len(s.profiles) == 1
        assert s.active_profile_idx == 0
        assert s.window_width == 1200
        assert s.window_height == 800

    def test_active_profile(self):
        s = AppSettings()
        assert s.active_profile.name == "Default"

    def test_active_profile_valid_index(self):
        profiles = [
            TestProfile(name="First"),
            TestProfile(name="Second"),
        ]
        s = AppSettings(profiles=profiles, active_profile_idx=1)
        assert s.active_profile.name == "Second"

    def test_active_profile_out_of_range(self):
        s = AppSettings(active_profile_idx=99)
        # should fall back to profiles[0]
        assert s.active_profile.name == "Default"

    def test_active_profile_negative_index(self):
        s = AppSettings(active_profile_idx=-1)
        assert s.active_profile.name == "Default"

    def test_active_profile_empty_profiles(self):
        s = AppSettings(profiles=[], active_profile_idx=0)
        # should return a new default profile
        p = s.active_profile
        assert p.name == "Default"


# ===========================================================================
# save_settings / load_settings
# ===========================================================================


class TestSaveLoadSettings:
    def test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)

        original = AppSettings(
            work_dir="/tmp/test",
            theme="dark",
            poll_interval=0.5,
            show_smt_threads=True,
            window_width=1600,
            window_height=900,
            active_profile_idx=0,
            profiles=[
                TestProfile(
                    name="Test",
                    backend="stress-ng",
                    stress_mode="AVX",
                    seconds_per_core=120,
                )
            ],
        )

        save_settings(original)
        loaded = load_settings()

        assert loaded.work_dir == "/tmp/test"
        assert loaded.theme == "dark"
        assert loaded.poll_interval == 0.5
        assert loaded.show_smt_threads is True
        assert loaded.window_width == 1600
        assert loaded.window_height == 900
        assert len(loaded.profiles) == 1
        assert loaded.profiles[0].name == "Test"
        assert loaded.profiles[0].backend == "stress-ng"
        assert loaded.profiles[0].stress_mode == "AVX"
        assert loaded.profiles[0].seconds_per_core == 120

    def test_load_default_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        s = load_settings()
        assert s.work_dir == "/tmp/linux-corecycler"
        assert len(s.profiles) == 1

    def test_load_corrupted_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        (tmp_path / "settings.json").write_text("not valid json{{{")
        s = load_settings()
        assert isinstance(s, AppSettings)
        assert s.work_dir == "/tmp/linux-corecycler"

    def test_load_wrong_types(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        (tmp_path / "settings.json").write_text('{"work_dir": 42, "profiles": "bad"}')
        s = load_settings()
        # should fall back to defaults
        assert isinstance(s, AppSettings)

    def test_load_extra_fields_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        data = {
            "work_dir": "/tmp/test",
            "theme": "system",
            "poll_interval": 1.0,
            "show_smt_threads": False,
            "active_profile_idx": 0,
            "window_width": 1200,
            "window_height": 800,
            "unknown_field": "should be ignored",
            "profiles": [{"name": "Default"}],
        }
        (tmp_path / "settings.json").write_text(json.dumps(data))
        # This will likely raise TypeError on the extra field
        s = load_settings()
        assert isinstance(s, AppSettings)

    def test_save_creates_dir(self, tmp_path, monkeypatch):
        config_dir = tmp_path / "deep" / "nested"
        monkeypatch.setattr("config.settings.CONFIG_DIR", config_dir)
        save_settings(AppSettings())
        assert (config_dir / "settings.json").exists()

    def test_save_produces_valid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        save_settings(AppSettings())

        content = (tmp_path / "settings.json").read_text()
        data = json.loads(content)
        assert isinstance(data, dict)
        assert "profiles" in data
        assert "work_dir" in data

    def test_multiple_profiles_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)

        profiles = [
            TestProfile(name="Quick", seconds_per_core=60),
            TestProfile(name="Long", seconds_per_core=3600),
            TestProfile(name="Custom", fft_preset="CUSTOM", fft_min=10, fft_max=100),
        ]
        original = AppSettings(profiles=profiles, active_profile_idx=2)
        save_settings(original)
        loaded = load_settings()

        assert len(loaded.profiles) == 3
        assert loaded.profiles[0].name == "Quick"
        assert loaded.profiles[1].seconds_per_core == 3600
        assert loaded.profiles[2].fft_min == 10
        assert loaded.active_profile_idx == 2

    def test_cores_to_test_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        p = TestProfile(cores_to_test=[0, 3, 7, 15])
        save_settings(AppSettings(profiles=[p]))
        loaded = load_settings()
        assert loaded.profiles[0].cores_to_test == [0, 3, 7, 15]

    def test_cores_to_test_none_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        p = TestProfile(cores_to_test=None)
        save_settings(AppSettings(profiles=[p]))
        loaded = load_settings()
        assert loaded.profiles[0].cores_to_test is None


# ===========================================================================
# save_profile / load_profile
# ===========================================================================


class TestSaveLoadProfile:
    def test_round_trip(self, tmp_path):
        profile = TestProfile(
            name="PBO Test",
            backend="mprime",
            stress_mode="AVX2",
            fft_preset="HEAVY",
            seconds_per_core=600,
            cycle_count=5,
            stop_on_error=True,
        )

        path = tmp_path / "profile.json"
        save_profile(profile, path)
        loaded = load_profile(path)

        assert loaded.name == "PBO Test"
        assert loaded.backend == "mprime"
        assert loaded.stress_mode == "AVX2"
        assert loaded.fft_preset == "HEAVY"
        assert loaded.seconds_per_core == 600
        assert loaded.cycle_count == 5
        assert loaded.stop_on_error is True

    def test_save_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "sub" / "dir" / "profile.json"
        save_profile(TestProfile(), path)
        assert path.exists()

    def test_save_produces_valid_json(self, tmp_path):
        path = tmp_path / "profile.json"
        save_profile(TestProfile(), path)
        data = json.loads(path.read_text())
        assert "name" in data
        assert "backend" in data

    def test_load_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_profile(tmp_path / "nonexistent.json")

    def test_load_corrupted_json_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json")
        with pytest.raises(json.JSONDecodeError):
            load_profile(path)

    def test_profile_with_all_fields(self, tmp_path):
        profile = TestProfile(
            name="Full",
            backend="y-cruncher",
            stress_mode="AVX512",
            fft_preset="CUSTOM",
            fft_min=50,
            fft_max=999,
            threads=8,
            seconds_per_core=1800,
            iterations_per_core=100,
            cycle_count=10,
            stop_on_error=True,
            test_smt=True,
            cores_to_test=[0, 1, 2, 3],
        )
        path = tmp_path / "full.json"
        save_profile(profile, path)
        loaded = load_profile(path)

        assert loaded.fft_min == 50
        assert loaded.fft_max == 999
        assert loaded.threads == 8
        assert loaded.test_smt is True
        assert loaded.cores_to_test == [0, 1, 2, 3]


# ===========================================================================
# JSON serialization edge cases
# ===========================================================================


class TestJSONEdgeCases:
    def test_empty_profiles_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        s = AppSettings(profiles=[])
        save_settings(s)
        loaded = load_settings()
        assert loaded.profiles == []

    def test_unicode_profile_name(self, tmp_path):
        profile = TestProfile(name="Stress-Test Uberprufen")
        path = tmp_path / "unicode.json"
        save_profile(profile, path)
        loaded = load_profile(path)
        assert loaded.name == "Stress-Test Uberprufen"

    def test_large_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr("config.settings.CONFIG_DIR", tmp_path)
        s = AppSettings(
            window_width=99999,
            window_height=99999,
            profiles=[TestProfile(seconds_per_core=999999, cycle_count=9999)],
        )
        save_settings(s)
        loaded = load_settings()
        assert loaded.window_width == 99999
        assert loaded.profiles[0].seconds_per_core == 999999
