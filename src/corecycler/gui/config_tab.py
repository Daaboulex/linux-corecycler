"""Configuration tab: the settings of a manual stress test run, from a preset or set by hand."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QGroupBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from corecycler.config.settings import TestProfile
from corecycler.engine.backends import available_backends, get_backend
from corecycler.engine.backends.base import FFTPreset, StressMode
from corecycler.engine.backends.mprime import FFT_RANGES
from corecycler.gui.widgets import add_setting, note, setting_grid, spoken

if TYPE_CHECKING:
    from corecycler.engine.topology import CPUTopology

DEGREES = "°C"
BOX_WIDTH = 110
CUSTOM = "CUSTOM"
CUSTOM_NAME = "Your own settings"
CUSTOM_ABOUT = "Every setting below as you set it."


def duration(seconds: float) -> str:
    return spoken(seconds) or f"{seconds:.0f} s"


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    purpose: str
    seconds: int
    rounds: int
    changing_load: bool
    idle_watch: int
    pause: int

    def values(self) -> tuple[int, int, bool, int, int]:
        return (self.seconds, self.rounds, self.changing_load, self.idle_watch, self.pause)

    def summary(self) -> str:
        parts = [f"Each core {duration(self.seconds)}", f"{self.rounds} round" + ("s" if self.rounds != 1 else "")]
        if self.changing_load:
            parts.append("changing load")
        if self.idle_watch:
            parts.append(f"an idle watch of {duration(self.idle_watch)} after each core")
        if self.pause:
            parts.append(f"{duration(self.pause)} between cores")
        return ", ".join(parts) + f". {self.purpose}"


PRESETS: dict[str, Preset] = {
    "QUICK": Preset("Quick check", "Finds clear errors fast and misses rare ones.", 120, 1, False, 0, 0),
    "STANDARD": Preset("Standard", "A good first check after changing a value.", 600, 1, False, 0, 0),
    "THOROUGH": Preset("Thorough", "Catches errors that show only now and then.", 1800, 2, False, 0, 5),
    "FULL_SPECTRUM": Preset(
        "Everything", "Adds load changes and idle time, closest to daily use.", 1200, 3, True, 60, 10
    ),
}
MODE_LABELS = {
    StressMode.SSE: "SSE, lightest",
    StressMode.AVX: "AVX",
    StressMode.AVX2: "AVX2",
    StressMode.AVX512: "AVX-512, heaviest",
    StressMode.CUSTOM: "Custom",
}
FFT_NAMES = {
    FFTPreset.SMALLEST: "Smallest",
    FFTPreset.SMALL: "Small",
    FFTPreset.LARGE: "Large",
    FFTPreset.HUGE: "Huge",
    FFTPreset.ALL: "All sizes",
    FFTPreset.MODERATE: "Moderate",
    FFTPreset.HEAVY: "Heavy",
    FFTPreset.HEAVY_SHORT: "Heavy, short",
    FFTPreset.CUSTOM: "Your own range",
}


def fft_label(preset: FFTPreset) -> str:
    if preset not in FFT_RANGES:
        return FFT_NAMES[preset]
    low, high = FFT_RANGES[preset]
    return f"{FFT_NAMES[preset]}, {low}K to {high}K"


FORM_FIELDS = (
    ("backend", "Program"),
    ("stress_mode", "Instruction set"),
    ("fft_preset", "FFT size"),
    ("fft_min", "Smallest FFT"),
    ("fft_max", "Largest FFT"),
    ("threads", "Threads per core"),
    ("seconds_per_core", "Time per core"),
    ("cycle_count", "Rounds"),
    ("stop_on_error", "Stop at the first error"),
    ("cores_to_test", "Cores to test"),
    ("max_temperature", "Temperature limit"),
    ("variable_load", "Changing load"),
    ("idle_stability_test", "Idle watch after each core"),
    ("idle_between_cores", "Pause between cores"),
)


class ConfigTab(QWidget):
    def __init__(self, topology: CPUTopology | None = None) -> None:
        super().__init__()
        self._topology = topology
        self._applying = False
        self._last_failed_cores: list[int] = []
        self._core_boxes: dict[int, QCheckBox] = {}
        self._setup_ui()
        self._preset.setCurrentIndex(self._preset.findData("STANDARD"))

    def _setup_ui(self) -> None:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)
        layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        preset_group = QGroupBox("Preset")
        preset_column = QVBoxLayout(preset_group)
        self._preset = QComboBox()
        self._preset.setAccessibleName("Preset")
        for key, preset in PRESETS.items():
            self._preset.addItem(preset.name, key)
        self._preset.addItem(CUSTOM_NAME, CUSTOM)
        self._preset.currentIndexChanged.connect(self._on_preset)
        preset_column.addWidget(self._preset)
        self._preset_about = note(CUSTOM_ABOUT)
        preset_column.addWidget(self._preset_about)
        layout.addWidget(preset_group)

        program_group = QGroupBox("Test program")
        program = setting_grid()
        self._backend_combo = QComboBox()
        self._backend_combo.addItems(available_backends())
        self._backend_combo.currentTextChanged.connect(self._on_program)
        add_setting(program, 0, "Program", self._backend_combo, "", "The program that puts the load on each core.")
        self._stress_mode_combo = QComboBox()
        add_setting(
            program,
            1,
            "Instruction set",
            self._stress_mode_combo,
            "",
            "Which CPU instructions the test uses. Heavier ones draw more power and heat.",
        )
        threads = (
            max((len(core.logical_cpus) for core in self._topology.cores.values()), default=1) if self._topology else 1
        )
        self._threads_spin = _spin(1, threads, 1)
        hint = "1 tests one thread of each core, 2 both." if threads > 1 else "This CPU runs one thread per core."
        add_setting(program, 2, "Threads per core", self._threads_spin, "threads", hint)
        program_group.setLayout(program)
        self._fft_box = QWidget()
        fft = setting_grid()
        fft.setContentsMargins(0, 0, 0, 0)
        self._fft_combo = QComboBox()
        for preset in FFTPreset:
            self._fft_combo.addItem(fft_label(preset), preset.name)
        self._fft_combo.currentIndexChanged.connect(self._on_fft_change)
        add_setting(
            fft,
            0,
            "FFT size",
            self._fft_combo,
            "",
            "The size of the numbers the program works on. Small sizes stay in the core's cache and heat it most;"
            " large ones also load memory.",
        )
        self._fft_range_widget = QWidget()
        custom = setting_grid()
        custom.setContentsMargins(0, 0, 0, 0)
        self._fft_min_spin = _spin(4, 65536, 4)
        self._fft_min_spin.valueChanged.connect(self._on_fft_range_change)
        self._fft_max_spin = _spin(4, 65536, 8192)
        self._fft_max_spin.valueChanged.connect(self._on_fft_range_change)
        add_setting(custom, 0, "Smallest FFT", self._fft_min_spin, "K", "The first size of your own range.")
        add_setting(custom, 1, "Largest FFT", self._fft_max_spin, "K", "The last size of your own range.")
        self._fft_range_widget.setLayout(custom)
        self._fft_range_widget.setVisible(False)
        fft_column = QVBoxLayout(self._fft_box)
        fft_column.setContentsMargins(0, 0, 0, 0)
        fft_column.addLayout(fft)
        fft_column.addWidget(self._fft_range_widget)
        program.addWidget(self._fft_box, 3, 0, 1, 2)
        layout.addWidget(program_group)

        length_group = QGroupBox("Length")
        length = setting_grid()
        self._time_spin = _spin(10, 86400, 600)
        add_setting(length, 0, "Time per core", self._time_spin, "s", "How long each core is tested in each round.")
        self._cycles_spin = _spin(1, 100, 1)
        add_setting(length, 1, "Rounds", self._cycles_spin, "rounds", "How many times every chosen core is tested.")
        self._idle_between_spin = _spin(0, 60, 0)
        add_setting(
            length,
            2,
            "Pause between cores",
            self._idle_between_spin,
            "s",
            "Rest before the next core, so the last test's heat does not carry over.",
        )
        self._idle_stability_spin = _spin(0, 300, 0)
        add_setting(
            length,
            3,
            "Idle watch after each core",
            self._idle_stability_spin,
            "s",
            "After its test the core rests this long while errors are watched. 0 turns it off.",
        )
        length_group.setLayout(length)
        layout.addWidget(length_group)

        safety_group = QGroupBox("Safety and behaviour")
        safety = setting_grid()
        self._max_temp_spin = QDoubleSpinBox()
        self._max_temp_spin.setRange(50.0, 115.0)
        self._max_temp_spin.setDecimals(1)
        self._max_temp_spin.setValue(95.0)
        self._max_temp_spin.setFixedWidth(BOX_WIDTH)
        add_setting(
            safety, 0, "Temperature limit", self._max_temp_spin, DEGREES, "A test stops when the CPU gets this hot."
        )
        self._variable_load = QCheckBox()
        add_setting(
            safety,
            1,
            "Changing load",
            self._variable_load,
            "",
            "Stops and restarts the load every few seconds, where many Curve Optimizer errors show.",
        )
        self._stop_on_error = QCheckBox()
        add_setting(
            safety,
            2,
            "Stop at the first error",
            self._stop_on_error,
            "",
            "Ends the whole run at the first error instead of testing every core.",
        )
        safety_group.setLayout(safety)
        layout.addWidget(safety_group)

        cores_group = QGroupBox("Cores to test")
        cores_column = QVBoxLayout(cores_group)
        boxes = QGridLayout()
        for index, core in enumerate(sorted(self._topology.cores) if self._topology else ()):
            box = QCheckBox(f"C{core}")
            box.setChecked(True)
            self._core_boxes[core] = box
            boxes.addWidget(box, index // 8, index % 8)
        cores_column.addLayout(boxes)
        if self._core_boxes:
            cores_column.addWidget(note("Every ticked core is tested; at least one must be ticked."))
        else:
            unread = "The CPU's cores could not be read, so no core can be ticked and no test can start."
            cores_column.addWidget(note(unread))
        self._retest_failed_btn = QPushButton("Retest only the failed cores")
        self._retest_failed_btn.setEnabled(False)
        self._retest_failed_btn.clicked.connect(self._on_retest_failed)
        cores_column.addWidget(self._retest_failed_btn)
        layout.addWidget(cores_group)
        layout.addStretch()

        for widget in (self._time_spin, self._cycles_spin, self._idle_between_spin, self._idle_stability_spin):
            widget.valueChanged.connect(self._show_preset)
        self._variable_load.toggled.connect(self._show_preset)
        self._on_program(self._backend_combo.currentText())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _preset_values(self) -> tuple[int, int, bool, int, int]:
        return (
            self._time_spin.value(),
            self._cycles_spin.value(),
            self._variable_load.isChecked(),
            self._idle_stability_spin.value(),
            self._idle_between_spin.value(),
        )

    def _on_preset(self) -> None:
        preset = PRESETS.get(self._preset.currentData())
        if preset is not None:
            self._applying = True
            self._time_spin.setValue(preset.seconds)
            self._cycles_spin.setValue(preset.rounds)
            self._variable_load.setChecked(preset.changing_load)
            self._idle_stability_spin.setValue(preset.idle_watch)
            self._idle_between_spin.setValue(preset.pause)
            self._applying = False
        self._show_preset()

    def _show_preset(self) -> None:
        if self._applying:
            return
        values = self._preset_values()
        key = next((key for key, preset in PRESETS.items() if preset.values() == values), CUSTOM)
        self._preset.blockSignals(True)
        self._preset.setCurrentIndex(self._preset.findData(key))
        self._preset.blockSignals(False)
        self._preset_about.setText(PRESETS[key].summary() if key in PRESETS else CUSTOM_ABOUT)

    def _on_program(self, name: str) -> None:
        backend = get_backend(name)
        chosen = self._stress_mode_combo.currentData()
        self._stress_mode_combo.clear()
        for mode in backend.get_supported_modes():
            self._stress_mode_combo.addItem(MODE_LABELS[mode], mode.name)
        index = self._stress_mode_combo.findData(chosen)
        self._stress_mode_combo.setCurrentIndex(max(index, 0))
        self._fft_box.setVisible(bool(backend.get_supported_fft_presets()))

    def _on_fft_change(self) -> None:
        self._fft_range_widget.setVisible(self._fft_combo.currentData() == FFTPreset.CUSTOM.name)

    def _on_fft_range_change(self) -> None:
        if self._fft_min_spin.value() > self._fft_max_spin.value():
            self._fft_max_spin.setValue(self._fft_min_spin.value())

    def get_profile(self) -> TestProfile:
        chosen = [core for core, box in self._core_boxes.items() if box.isChecked()]
        custom_fft = self._fft_combo.currentData() == FFTPreset.CUSTOM.name
        return TestProfile(
            backend=self._backend_combo.currentText(),
            stress_mode=self._stress_mode_combo.currentData(),
            fft_preset=self._fft_combo.currentData(),
            fft_min=self._fft_min_spin.value() if custom_fft else None,
            fft_max=self._fft_max_spin.value() if custom_fft else None,
            threads=self._threads_spin.value(),
            seconds_per_core=self._time_spin.value(),
            cycle_count=self._cycles_spin.value(),
            stop_on_error=self._stop_on_error.isChecked(),
            test_smt=False,
            cores_to_test=None if len(chosen) == len(self._core_boxes) else chosen,
            max_temperature=self._max_temp_spin.value(),
            test_mode=self._preset.currentData(),
            variable_load=self._variable_load.isChecked(),
            idle_stability_test=self._idle_stability_spin.value(),
            idle_between_cores=self._idle_between_spin.value(),
        )

    def set_profile(self, profile: TestProfile) -> tuple[str, ...]:
        before = self.get_profile()
        self._show_profile(profile)
        shown = self.get_profile()
        changed = tuple(
            f"{label}: the profile has {getattr(profile, name)!r}, the form would show {getattr(shown, name)!r}"
            for name, label in FORM_FIELDS
            if self._held(profile, name) != self._held(shown, name)
        )
        if changed:
            self._show_profile(before)
        return changed

    def _held(self, profile: TestProfile, name: str) -> object:
        value = getattr(profile, name)
        if name == "cores_to_test":
            return sorted(self._core_boxes) if value is None else sorted(value)
        return value

    def _show_profile(self, profile: TestProfile) -> None:
        self._backend_combo.setCurrentText(profile.backend)
        self._stress_mode_combo.setCurrentIndex(max(self._stress_mode_combo.findData(profile.stress_mode), 0))
        self._fft_combo.setCurrentIndex(max(self._fft_combo.findData(profile.fft_preset), 0))
        if profile.fft_min:
            self._fft_min_spin.setValue(profile.fft_min)
        if profile.fft_max:
            self._fft_max_spin.setValue(profile.fft_max)
        self._threads_spin.setValue(profile.threads)
        self._applying = True
        self._time_spin.setValue(profile.seconds_per_core)
        self._cycles_spin.setValue(profile.cycle_count)
        self._variable_load.setChecked(profile.variable_load)
        self._idle_stability_spin.setValue(int(profile.idle_stability_test))
        self._idle_between_spin.setValue(int(profile.idle_between_cores))
        self._applying = False
        self._show_preset()
        self._stop_on_error.setChecked(profile.stop_on_error)
        self._max_temp_spin.setValue(profile.max_temperature)
        self._tick(profile.cores_to_test or None)

    def _tick(self, cores: list[int] | None) -> None:
        for core, box in self._core_boxes.items():
            box.setChecked(cores is None or core in cores)

    def set_failed_cores(self, failed_cores: list[int]) -> None:
        self._last_failed_cores = failed_cores
        self._retest_failed_btn.setEnabled(bool(failed_cores))
        count = len(failed_cores)
        self._retest_failed_btn.setText(
            f"Retest only the {count} failed core{'s' if count != 1 else ''}"
            if failed_cores
            else "Retest only the failed cores"
        )

    def _on_retest_failed(self) -> None:
        if self._last_failed_cores:
            self._tick(self._last_failed_cores)


def _spin(low: int, high: int, value: int) -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(low, high)
    spin.setValue(value)
    spin.setFixedWidth(BOX_WIDTH)
    return spin
