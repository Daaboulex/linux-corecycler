"""ConfigTab: the presets, the program's own choices, the cores and the profile round trip."""

from __future__ import annotations

import sys as _sys

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.backends import load_all
from corecycler.engine.topology import CPUTopology, PhysicalCore


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo() -> CPUTopology:
    topo = CPUTopology(model_name="Test 8C/16T", family=26, model=0x44, physical_cores=8, ccds=2)
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0 if cid < 4 else 1, ccx=None, logical_cpus=(cid, cid + 8))
    return topo


def _tab():
    _qapp()
    load_all()
    from corecycler.gui.config_tab import ConfigTab

    return ConfigTab(_topo())


class TestPresets:
    def test_default_profile_is_valid_and_standard(self):
        tab = _tab()
        p = tab.get_profile()
        assert p.test_mode == "STANDARD"
        assert p.seconds_per_core == 600
        assert p.cycle_count == 1
        assert p.max_temperature == 95.0
        assert p.cores_to_test is None

    @pytest.mark.parametrize(
        "preset,seconds,cycles,variable,idle,pause",
        [
            ("QUICK", 120, 1, False, 0, 0),
            ("STANDARD", 600, 1, False, 0, 0),
            ("THOROUGH", 1800, 2, False, 0, 5),
            ("FULL_SPECTRUM", 1200, 3, True, 60, 10),
        ],
    )
    def test_preset_applies_its_values(self, preset, seconds, cycles, variable, idle, pause):
        tab = _tab()
        tab._preset.setCurrentIndex(tab._preset.findData(preset))
        p = tab.get_profile()
        assert (p.seconds_per_core, p.cycle_count, p.variable_load) == (seconds, cycles, variable)
        assert (p.idle_stability_test, p.idle_between_cores) == (idle, pause)
        assert p.test_mode == preset

    @pytest.mark.req("UI3")
    def test_each_preset_s_line_names_its_own_values(self):
        from corecycler.gui.config_tab import PRESETS

        assert PRESETS["QUICK"].summary() == "Each core 2 min, 1 round. Finds clear errors fast and misses rare ones."
        assert PRESETS["FULL_SPECTRUM"].summary() == (
            "Each core 20 min, 3 rounds, changing load, an idle watch of 1 min after each core, 10 s between cores."
            " Adds load changes and idle time, closest to daily use."
        )

    def test_a_changed_value_shows_your_own_settings_and_back(self):
        tab = _tab()
        tab._time_spin.setValue(999)
        assert tab.get_profile().test_mode == "CUSTOM"
        assert tab._preset.currentText() == "Your own settings"
        assert tab._preset_about.text() == "Every setting below as you set it."
        tab._time_spin.setValue(120)
        assert tab.get_profile().test_mode == "QUICK"
        assert tab._preset_about.text().startswith("Each core 2 min, 1 round.")

    def test_a_setting_no_preset_holds_keeps_the_preset(self):
        tab = _tab()
        tab._max_temp_spin.setValue(90.0)
        tab._stop_on_error.setChecked(True)
        assert tab.get_profile().test_mode == "STANDARD"

    def test_choosing_your_own_settings_keeps_the_values(self):
        tab = _tab()
        tab._preset.setCurrentIndex(tab._preset.findData("CUSTOM"))
        assert tab.get_profile().seconds_per_core == 600
        assert tab.get_profile().test_mode == "STANDARD"


class TestProgram:
    def test_only_the_program_s_own_instruction_sets_are_offered(self):
        from corecycler.engine.backends import get_backend

        tab = _tab()
        for name in ("stressapptest", "y-cruncher"):
            tab._backend_combo.setCurrentText(name)
            offered = [tab._stress_mode_combo.itemData(i) for i in range(tab._stress_mode_combo.count())]
            assert offered == [mode.name for mode in get_backend(name).get_supported_modes()]

    def test_a_kept_instruction_set_stays_chosen(self):
        tab = _tab()
        tab._backend_combo.setCurrentText("y-cruncher")
        tab._stress_mode_combo.setCurrentIndex(tab._stress_mode_combo.findData("AVX2"))
        tab._backend_combo.setCurrentText("stress-ng")
        assert tab.get_profile().stress_mode == "AVX2"
        tab._backend_combo.setCurrentText("stressapptest")
        assert tab.get_profile().stress_mode == "SSE"

    def test_the_fft_size_shows_only_for_a_program_that_has_one(self):
        tab = _tab()
        tab._backend_combo.setCurrentText("mprime")
        assert not tab._fft_box.isHidden()
        tab._backend_combo.setCurrentText("y-cruncher")
        assert tab._fft_box.isHidden()

    def test_an_fft_size_is_named_with_its_range(self):
        from corecycler.engine.backends.base import FFTPreset
        from corecycler.gui.config_tab import fft_label

        assert fft_label(FFTPreset.SMALLEST) == "Smallest, 4K to 21K"
        assert fft_label(FFTPreset.CUSTOM) == "Your own range"


class TestFftRange:
    def _custom(self, tab):
        tab._fft_combo.setCurrentIndex(tab._fft_combo.findData("CUSTOM"))

    def test_custom_fft_exposes_range_and_reports_it(self):
        tab = _tab()
        self._custom(tab)
        assert not tab._fft_range_widget.isHidden()
        tab._fft_min_spin.setValue(64)
        tab._fft_max_spin.setValue(512)
        p = tab.get_profile()
        assert (p.fft_preset, p.fft_min, p.fft_max) == ("CUSTOM", 64, 512)

    def test_non_custom_fft_reports_none_range(self):
        tab = _tab()
        tab._fft_combo.setCurrentIndex(tab._fft_combo.findData("SMALL"))
        p = tab.get_profile()
        assert (p.fft_preset, p.fft_min, p.fft_max) == ("SMALL", None, None)
        assert tab._fft_range_widget.isHidden()

    def test_min_above_max_is_clamped(self):
        tab = _tab()
        self._custom(tab)
        tab._fft_max_spin.setValue(256)
        tab._fft_min_spin.setValue(4096)
        assert tab._fft_max_spin.value() == 4096


class TestCores:
    def test_every_ticked_core_is_tested(self):
        tab = _tab()
        tab._core_boxes[2].setChecked(False)
        tab._core_boxes[5].setChecked(False)
        assert tab.get_profile().cores_to_test == [0, 1, 3, 4, 6, 7]

    def test_no_ticked_core_is_an_empty_choice(self):
        tab = _tab()
        for box in tab._core_boxes.values():
            box.setChecked(False)
        assert tab.get_profile().cores_to_test == []


class TestRoundTrip:
    def test_set_then_get_profile_round_trips(self):
        from corecycler.config.settings import TestProfile

        tab = _tab()
        src = TestProfile(
            backend="mprime",
            stress_mode="AVX",
            fft_preset="LARGE",
            threads=2,
            seconds_per_core=42,
            cycle_count=3,
            stop_on_error=True,
            max_temperature=88.0,
            cores_to_test=[2, 3],
            variable_load=True,
            idle_stability_test=15.0,
            idle_between_cores=7.0,
            test_mode="CUSTOM",
        )
        tab.set_profile(src)
        out = tab.get_profile()
        assert (out.backend, out.stress_mode, out.fft_preset, out.threads) == ("mprime", "AVX", "LARGE", 2)
        assert out.seconds_per_core == 42
        assert out.cycle_count == 3
        assert out.stop_on_error is True
        assert out.max_temperature == 88.0
        assert out.cores_to_test == [2, 3]
        assert out.variable_load is True
        assert int(out.idle_stability_test) == 15
        assert int(out.idle_between_cores) == 7
        assert out.test_mode == "CUSTOM"

    def test_a_stored_preset_s_values_show_that_preset(self):
        from corecycler.config.settings import TestProfile

        tab = _tab()
        tab.set_profile(TestProfile(seconds_per_core=1800, cycle_count=2, idle_between_cores=5.0, test_mode="CUSTOM"))
        assert tab.get_profile().test_mode == "THOROUGH"
        assert tab.get_profile().cores_to_test is None


class TestRefusedProfile:
    @pytest.mark.req("T23.config.load")
    def test_a_profile_the_form_cannot_show_exactly_is_refused_and_the_form_keeps_its_values(self):
        from corecycler.config.settings import TestProfile

        tab = _tab()
        before = tab.get_profile()
        refused = tab.set_profile(TestProfile(max_temperature=40.0, cores_to_test=[1, 9], cycle_count=0))
        assert refused == (
            "Rounds: the profile has 0, the form would show 1",
            "Cores to test: the profile has [1, 9], the form would show [1]",
            "Temperature limit: the profile has 40.0, the form would show 50.0",
        )
        assert tab.get_profile() == before

    @pytest.mark.req("T23.config.load")
    def test_a_profile_the_form_shows_exactly_is_applied_with_nothing_refused(self):
        from corecycler.config.settings import TestProfile

        tab = _tab()
        assert tab.set_profile(TestProfile(max_temperature=80.0, cores_to_test=[1, 7], cycle_count=2)) == ()
        shown = tab.get_profile()
        assert (shown.max_temperature, shown.cores_to_test, shown.cycle_count) == (80.0, [1, 7], 2)


class TestThreads:
    @pytest.mark.req("T23.config.threads")
    @pytest.mark.parametrize(("cpus", "top"), [(((0, 8),), 2), (((0,),), 1)])
    def test_threads_per_core_offers_only_what_the_cpu_runs(self, cpus, top):
        from corecycler.gui.config_tab import ConfigTab

        _qapp()
        topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=1, ccds=1)
        topo.cores[0] = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=cpus[0])
        assert ConfigTab(topo)._threads_spin.maximum() == top

    @pytest.mark.req("T23.config.threads")
    @pytest.mark.parametrize(
        ("cpus", "hint"),
        [((0, 8), "1 tests one thread of each core, 2 both."), ((0,), "This CPU runs one thread per core.")],
    )
    def test_threads_per_core_starts_at_1_third_under_program_with_a_hint_that_fits_the_cpu(self, cpus, hint):
        from PySide6.QtWidgets import QGroupBox, QLabel

        from corecycler.gui.config_tab import ConfigTab

        _qapp()
        topo = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=1, ccds=1)
        topo.cores[0] = PhysicalCore(core_id=0, ccd=0, ccx=None, logical_cpus=cpus)
        tab = ConfigTab(topo)
        spin = tab._threads_spin
        assert (spin.minimum(), spin.value()) == (1, 1)
        grid = next(g for g in tab.findChildren(QGroupBox) if g.title() == "Test program").layout()
        assert grid.itemAtPosition(2, 0).widget().text() == "Threads per core"
        assert hint in [label.text() for label in tab.findChildren(QLabel)]

    @pytest.mark.req("T23.config.threads")
    def test_a_topology_with_no_cores_offers_one_thread(self):
        from corecycler.gui.config_tab import ConfigTab

        _qapp()
        empty = CPUTopology(model_name="Test", family=26, model=0x44, physical_cores=0, ccds=0)
        assert ConfigTab(empty)._threads_spin.maximum() == 1

    @pytest.mark.req("T23.config.threads")
    def test_without_a_readable_topology_one_thread_is_offered(self):
        from corecycler.gui.config_tab import ConfigTab

        _qapp()
        assert ConfigTab(None)._threads_spin.maximum() == 1


class TestRetestFailed:
    def test_failed_cores_enable_button_and_tick_only_them(self):
        tab = _tab()
        tab.set_failed_cores([3, 1])
        assert tab._retest_failed_btn.isEnabled()
        assert tab._retest_failed_btn.text() == "Retest only the 2 failed cores"
        tab._on_retest_failed()
        assert tab.get_profile().cores_to_test == [1, 3]
        tab.set_failed_cores([4])
        assert tab._retest_failed_btn.text() == "Retest only the 1 failed core"

    def test_no_failed_cores_disables_button(self):
        tab = _tab()
        tab.set_failed_cores([])
        assert not tab._retest_failed_btn.isEnabled()
        assert tab._retest_failed_btn.text() == "Retest only the failed cores"
        tab._on_retest_failed()
        assert tab.get_profile().cores_to_test is None
