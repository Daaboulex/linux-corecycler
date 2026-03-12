"""Test configuration tab — backend, mode, timing, core selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.settings import TestProfile
from engine.backends.base import FFTPreset, StressMode

if TYPE_CHECKING:
    from engine.topology import CPUTopology


class ConfigTab(QWidget):
    """Configuration panel for stress test settings."""

    profile_changed = Signal(TestProfile)

    def __init__(self, topology: CPUTopology | None = None) -> None:
        super().__init__()
        self._topology = topology
        self._building = False  # prevent signal loops during build
        self._setup_ui()

    def _setup_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)

        # stress test backend
        backend_group = QGroupBox("Stress Test Backend")
        backend_layout = QFormLayout(backend_group)

        self._backend_combo = QComboBox()
        self._backend_combo.addItems(["mprime", "stress-ng", "y-cruncher"])
        self._backend_combo.currentTextChanged.connect(self._on_change)
        backend_layout.addRow("Backend:", self._backend_combo)

        self._mode_combo = QComboBox()
        for mode in StressMode:
            self._mode_combo.addItem(mode.name)
        self._mode_combo.currentTextChanged.connect(self._on_change)
        backend_layout.addRow("Stress Mode:", self._mode_combo)

        self._fft_combo = QComboBox()
        for preset in FFTPreset:
            self._fft_combo.addItem(preset.name, preset.value)
        self._fft_combo.currentTextChanged.connect(self._on_fft_change)
        backend_layout.addRow("FFT Preset:", self._fft_combo)

        # custom FFT range
        fft_range_widget = QWidget()
        fft_range_layout = QHBoxLayout(fft_range_widget)
        fft_range_layout.setContentsMargins(0, 0, 0, 0)
        self._fft_min_spin = QSpinBox()
        self._fft_min_spin.setRange(4, 65536)
        self._fft_min_spin.setValue(4)
        self._fft_min_spin.setSuffix("K")
        self._fft_min_spin.valueChanged.connect(self._on_change)
        self._fft_max_spin = QSpinBox()
        self._fft_max_spin.setRange(4, 65536)
        self._fft_max_spin.setValue(8192)
        self._fft_max_spin.setSuffix("K")
        self._fft_max_spin.valueChanged.connect(self._on_change)
        fft_range_layout.addWidget(QLabel("Min:"))
        fft_range_layout.addWidget(self._fft_min_spin)
        fft_range_layout.addWidget(QLabel("Max:"))
        fft_range_layout.addWidget(self._fft_max_spin)
        self._fft_range_widget = fft_range_widget
        self._fft_range_widget.setVisible(False)
        backend_layout.addRow("Custom Range:", self._fft_range_widget)

        self._threads_spin = QSpinBox()
        self._threads_spin.setRange(1, 2)
        self._threads_spin.setValue(1)
        self._threads_spin.valueChanged.connect(self._on_change)
        backend_layout.addRow("Threads:", self._threads_spin)

        layout.addWidget(backend_group)

        # timing
        timing_group = QGroupBox("Timing")
        timing_layout = QFormLayout(timing_group)

        self._time_spin = QSpinBox()
        self._time_spin.setRange(10, 86400)
        self._time_spin.setValue(360)
        self._time_spin.setSuffix(" seconds")
        self._time_spin.valueChanged.connect(self._on_change)
        timing_layout.addRow("Time per core:", self._time_spin)

        self._cycles_spin = QSpinBox()
        self._cycles_spin.setRange(1, 100)
        self._cycles_spin.setValue(1)
        self._cycles_spin.valueChanged.connect(self._on_change)
        timing_layout.addRow("Cycles:", self._cycles_spin)

        layout.addWidget(timing_group)

        # behavior
        behavior_group = QGroupBox("Behavior")
        behavior_layout = QFormLayout(behavior_group)

        self._stop_on_error = QCheckBox("Stop testing when first error occurs")
        self._stop_on_error.stateChanged.connect(self._on_change)
        behavior_layout.addRow(self._stop_on_error)

        self._test_smt = QCheckBox("Also test SMT sibling threads")
        self._test_smt.stateChanged.connect(self._on_change)
        behavior_layout.addRow(self._test_smt)

        layout.addWidget(behavior_group)

        # core selection
        cores_group = QGroupBox("Core Selection")
        cores_layout = QVBoxLayout(cores_group)

        cores_layout.addWidget(QLabel("Leave empty to test all cores. Comma-separated core IDs:"))
        self._cores_input = QLineEdit()
        self._cores_input.setPlaceholderText("e.g., 0,1,4,5 (physical core IDs)")
        self._cores_input.textChanged.connect(self._on_change)
        cores_layout.addWidget(self._cores_input)

        layout.addWidget(cores_group)
        layout.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _on_fft_change(self) -> None:
        is_custom = self._fft_combo.currentText() == "CUSTOM"
        self._fft_range_widget.setVisible(is_custom)
        self._on_change()

    def _on_change(self) -> None:
        if self._building:
            return
        self.profile_changed.emit(self.get_profile())

    def get_profile(self) -> TestProfile:
        cores_text = self._cores_input.text().strip()
        cores = None
        if cores_text:
            try:
                cores = [int(c.strip()) for c in cores_text.split(",") if c.strip()]
            except ValueError:
                cores = None

        return TestProfile(
            backend=self._backend_combo.currentText(),
            stress_mode=self._mode_combo.currentText(),
            fft_preset=self._fft_combo.currentText(),
            fft_min=self._fft_min_spin.value() if self._fft_combo.currentText() == "CUSTOM" else None,
            fft_max=self._fft_max_spin.value() if self._fft_combo.currentText() == "CUSTOM" else None,
            threads=self._threads_spin.value(),
            seconds_per_core=self._time_spin.value(),
            cycle_count=self._cycles_spin.value(),
            stop_on_error=self._stop_on_error.isChecked(),
            test_smt=self._test_smt.isChecked(),
            cores_to_test=cores,
        )

    def set_profile(self, profile: TestProfile) -> None:
        self._building = True
        self._backend_combo.setCurrentText(profile.backend)
        self._mode_combo.setCurrentText(profile.stress_mode)
        self._fft_combo.setCurrentText(profile.fft_preset)
        if profile.fft_min:
            self._fft_min_spin.setValue(profile.fft_min)
        if profile.fft_max:
            self._fft_max_spin.setValue(profile.fft_max)
        self._threads_spin.setValue(profile.threads)
        self._time_spin.setValue(profile.seconds_per_core)
        self._cycles_spin.setValue(profile.cycle_count)
        self._stop_on_error.setChecked(profile.stop_on_error)
        self._test_smt.setChecked(profile.test_smt)
        if profile.cores_to_test:
            self._cores_input.setText(",".join(str(c) for c in profile.cores_to_test))
        else:
            self._cores_input.clear()
        self._building = False
