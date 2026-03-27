"""Test configuration tab — backend, mode, timing, core selection, test presets."""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config.settings import TestProfile
from engine.backends.base import FFTPreset, StressMode

if TYPE_CHECKING:
    from engine.topology import CPUTopology


# Pre-configured test mode descriptions
TEST_MODE_INFO: dict[str, str] = {
    "CUSTOM": "Configure all settings manually",
    "QUICK": "2 min/core, 1 cycle — fast screening, lower sensitivity",
    "STANDARD": "10 min/core, 1 cycle — good starting point for CO tuning",
    "THOROUGH": "30 min/core, 2 cycles — catches intermittent errors",
    "FULL_SPECTRUM": (
        "Stress + variable load + idle stability, 3 cycles — "
        "most comprehensive, tests all real-world scenarios"
    ),
}


class ConfigTab(QWidget):
    """Configuration panel for stress test settings."""

    profile_changed = Signal(TestProfile)

    def __init__(self, topology: CPUTopology | None = None) -> None:
        super().__init__()
        self._topology = topology
        self._building = False  # prevent signal loops during build
        self._setup_ui()
        # apply STANDARD defaults to widgets
        self._on_mode_change("STANDARD")

    def _setup_ui(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setSpacing(12)

        # test mode preset
        mode_group = QGroupBox("Test Mode")
        mode_layout = QVBoxLayout(mode_group)

        mode_row = QHBoxLayout()
        self._mode_combo = QComboBox()
        for mode_name, _desc in TEST_MODE_INFO.items():
            self._mode_combo.addItem(mode_name)
        self._mode_combo.setCurrentText("STANDARD")
        self._mode_combo.currentTextChanged.connect(self._on_mode_change)
        mode_row.addWidget(QLabel("Preset:"))
        mode_row.addWidget(self._mode_combo, stretch=1)
        mode_layout.addLayout(mode_row)

        self._mode_desc = QLabel(TEST_MODE_INFO["STANDARD"])
        self._mode_desc.setWordWrap(True)
        self._mode_desc.setStyleSheet("color: #888; padding: 4px;")
        mode_layout.addWidget(self._mode_desc)

        layout.addWidget(mode_group)

        # stress test backend
        backend_group = QGroupBox("Stress Test Backend")
        backend_layout = QFormLayout(backend_group)

        self._backend_combo = QComboBox()
        self._backend_combo.addItems(["mprime", "stress-ng", "y-cruncher"])
        self._backend_combo.currentTextChanged.connect(self._on_change)
        backend_layout.addRow("Backend:", self._backend_combo)

        self._stress_mode_combo = QComboBox()
        for mode in StressMode:
            self._stress_mode_combo.addItem(mode.name)
        self._stress_mode_combo.currentTextChanged.connect(self._on_change)
        backend_layout.addRow("Stress Mode:", self._stress_mode_combo)

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
        self._fft_min_spin.valueChanged.connect(self._on_fft_range_change)
        self._fft_max_spin = QSpinBox()
        self._fft_max_spin.setRange(4, 65536)
        self._fft_max_spin.setValue(8192)
        self._fft_max_spin.setSuffix("K")
        self._fft_max_spin.valueChanged.connect(self._on_fft_range_change)
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

        # safety
        safety_group = QGroupBox("Safety")
        safety_layout = QFormLayout(safety_group)

        self._max_temp_spin = QDoubleSpinBox()
        self._max_temp_spin.setRange(50.0, 115.0)
        self._max_temp_spin.setValue(95.0)
        self._max_temp_spin.setSuffix(" °C")
        self._max_temp_spin.setDecimals(1)
        self._max_temp_spin.valueChanged.connect(self._on_change)
        safety_layout.addRow("Max temperature:", self._max_temp_spin)

        layout.addWidget(safety_group)

        # advanced testing options
        advanced_group = QGroupBox("Advanced Testing")
        advanced_layout = QFormLayout(advanced_group)

        self._variable_load = QCheckBox("Variable load testing (stop/start stress periodically)")
        self._variable_load.setToolTip(
            "Simulates real-world load transitions. CO instability often manifests "
            "during frequency/voltage transitions, not under steady load."
        )
        self._variable_load.stateChanged.connect(self._on_change)
        advanced_layout.addRow(self._variable_load)

        self._idle_stability_spin = QSpinBox()
        self._idle_stability_spin.setRange(0, 300)
        self._idle_stability_spin.setValue(0)
        self._idle_stability_spin.setSuffix(" seconds")
        self._idle_stability_spin.setToolTip(
            "Time to monitor each core at idle after stress. Catches errors during "
            "C-state transitions — the #1 cause of CO-related crashes in daily use."
        )
        self._idle_stability_spin.valueChanged.connect(self._on_change)
        advanced_layout.addRow("Idle stability test:", self._idle_stability_spin)

        self._idle_between_spin = QSpinBox()
        self._idle_between_spin.setRange(0, 60)
        self._idle_between_spin.setValue(0)
        self._idle_between_spin.setSuffix(" seconds")
        self._idle_between_spin.setToolTip(
            "Idle pause between testing each core. Allows the CPU to cool and "
            "return to idle voltages before testing the next core."
        )
        self._idle_between_spin.valueChanged.connect(self._on_change)
        advanced_layout.addRow("Idle between cores:", self._idle_between_spin)

        layout.addWidget(advanced_group)

        # behavior
        behavior_group = QGroupBox("Behavior")
        behavior_layout = QFormLayout(behavior_group)

        self._stop_on_error = QCheckBox("Stop testing when first error occurs")
        self._stop_on_error.stateChanged.connect(self._on_change)
        behavior_layout.addRow(self._stop_on_error)

        layout.addWidget(behavior_group)

        # core selection
        cores_group = QGroupBox("Core Selection")
        cores_layout = QVBoxLayout(cores_group)

        cores_layout.addWidget(QLabel("Leave empty to test all cores. Comma-separated core IDs:"))
        self._cores_input = QLineEdit()
        self._cores_input.setPlaceholderText("e.g., 0,1,4,5 (physical core IDs)")
        self._cores_input.textChanged.connect(self._on_cores_changed)
        cores_layout.addWidget(self._cores_input)

        self._cores_error_label = QLabel("")
        self._cores_error_label.setStyleSheet("color: #f44336; font-size: 10px; padding: 2px;")
        self._cores_error_label.setVisible(False)
        cores_layout.addWidget(self._cores_error_label)

        self._retest_failed_btn = QPushButton("Retest Failed Cores Only")
        self._retest_failed_btn.setToolTip(
            "After a test run, populate the core selection with only the "
            "cores that failed — skip already-stable cores."
        )
        self._retest_failed_btn.setEnabled(False)
        self._retest_failed_btn.clicked.connect(self._on_retest_failed)
        cores_layout.addWidget(self._retest_failed_btn)

        layout.addWidget(cores_group)
        layout.addStretch()

        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

    def _on_mode_change(self, mode_name: str) -> None:
        """Apply a test mode preset."""
        self._mode_desc.setText(TEST_MODE_INFO.get(mode_name, ""))

        if mode_name == "CUSTOM":
            self._on_change()
            return

        self._building = True
        match mode_name:
            case "QUICK":
                self._time_spin.setValue(120)
                self._cycles_spin.setValue(1)
                self._variable_load.setChecked(False)
                self._idle_stability_spin.setValue(0)
                self._idle_between_spin.setValue(0)
            case "STANDARD":
                self._time_spin.setValue(600)
                self._cycles_spin.setValue(1)
                self._variable_load.setChecked(False)
                self._idle_stability_spin.setValue(0)
                self._idle_between_spin.setValue(0)
            case "THOROUGH":
                self._time_spin.setValue(1800)
                self._cycles_spin.setValue(2)
                self._variable_load.setChecked(False)
                self._idle_stability_spin.setValue(0)
                self._idle_between_spin.setValue(5)
            case "FULL_SPECTRUM":
                self._time_spin.setValue(1200)
                self._cycles_spin.setValue(3)
                self._variable_load.setChecked(True)
                self._idle_stability_spin.setValue(60)
                self._idle_between_spin.setValue(10)
        self._building = False
        self._on_change()

    def _on_fft_change(self) -> None:
        is_custom = self._fft_combo.currentText() == "CUSTOM"
        self._fft_range_widget.setVisible(is_custom)
        self._on_change()

    def _on_fft_range_change(self) -> None:
        """Enforce fft_min <= fft_max."""
        if self._fft_min_spin.value() > self._fft_max_spin.value():
            self._building = True
            self._fft_max_spin.setValue(self._fft_min_spin.value())
            self._building = False
        self._on_change()

    def _on_cores_changed(self) -> None:
        """Validate core IDs against topology and forward to _on_change."""
        cores_text = self._cores_input.text().strip()
        if not cores_text:
            self._cores_error_label.setVisible(False)
            self._on_change()
            return

        # Strip trailing commas gracefully
        cores_text = cores_text.rstrip(",").strip()

        try:
            cores = [int(c.strip()) for c in cores_text.split(",") if c.strip()]
        except ValueError:
            self._cores_error_label.setText("Invalid input — use comma-separated integers")
            self._cores_error_label.setVisible(True)
            self._on_change()
            return

        if self._topology:
            valid_ids = set(self._topology.cores.keys())
            invalid = [c for c in cores if c not in valid_ids]
            if invalid:
                max_id = max(valid_ids) if valid_ids else 0
                self._cores_error_label.setText(
                    f"Core(s) {', '.join(str(c) for c in invalid)} out of range "
                    f"(valid: 0-{max_id})"
                )
                self._cores_error_label.setVisible(True)
                self._on_change()
                return

        self._cores_error_label.setVisible(False)
        self._on_change()

    def _on_change(self) -> None:
        if self._building:
            return
        # Auto-switch to CUSTOM when user changes a preset-controlled parameter
        if self._mode_combo.currentText() != "CUSTOM":
            self._building = True
            self._mode_combo.setCurrentText("CUSTOM")
            self._mode_desc.setText(TEST_MODE_INFO["CUSTOM"])
            self._building = False
        self.profile_changed.emit(self.get_profile())

    def get_profile(self) -> TestProfile:
        cores_text = self._cores_input.text().strip().rstrip(",").strip()
        cores = None
        if cores_text:
            try:
                parsed = [int(c.strip()) for c in cores_text.split(",") if c.strip()]
                # Validate against topology — exclude out-of-range IDs
                if self._topology:
                    valid_ids = set(self._topology.cores.keys())
                    parsed = [c for c in parsed if c in valid_ids]
                cores = parsed if parsed else None
            except ValueError:
                cores = None

        return TestProfile(
            backend=self._backend_combo.currentText(),
            stress_mode=self._stress_mode_combo.currentText(),
            fft_preset=self._fft_combo.currentText(),
            fft_min=(
                self._fft_min_spin.value() if self._fft_combo.currentText() == "CUSTOM" else None
            ),
            fft_max=(
                self._fft_max_spin.value() if self._fft_combo.currentText() == "CUSTOM" else None
            ),
            threads=self._threads_spin.value(),
            seconds_per_core=self._time_spin.value(),
            cycle_count=self._cycles_spin.value(),
            stop_on_error=self._stop_on_error.isChecked(),
            test_smt=False,
            cores_to_test=cores,
            max_temperature=self._max_temp_spin.value(),
            test_mode=self._mode_combo.currentText(),
            variable_load=self._variable_load.isChecked(),
            idle_stability_test=self._idle_stability_spin.value(),
            idle_between_cores=self._idle_between_spin.value(),
        )

    def set_profile(self, profile: TestProfile) -> None:
        self._building = True
        self._backend_combo.setCurrentText(profile.backend)
        self._stress_mode_combo.setCurrentText(profile.stress_mode)
        self._fft_combo.setCurrentText(profile.fft_preset)
        if profile.fft_min:
            self._fft_min_spin.setValue(profile.fft_min)
        if profile.fft_max:
            self._fft_max_spin.setValue(profile.fft_max)
        self._threads_spin.setValue(profile.threads)
        self._time_spin.setValue(profile.seconds_per_core)
        self._cycles_spin.setValue(profile.cycle_count)
        self._stop_on_error.setChecked(profile.stop_on_error)
        if profile.cores_to_test:
            self._cores_input.setText(",".join(str(c) for c in profile.cores_to_test))
        else:
            self._cores_input.clear()
        if hasattr(profile, "max_temperature"):
            self._max_temp_spin.setValue(profile.max_temperature)
        if hasattr(profile, "test_mode"):
            self._mode_combo.setCurrentText(profile.test_mode)
        if hasattr(profile, "variable_load"):
            self._variable_load.setChecked(profile.variable_load)
        if hasattr(profile, "idle_stability_test"):
            self._idle_stability_spin.setValue(int(profile.idle_stability_test))
        if hasattr(profile, "idle_between_cores"):
            self._idle_between_spin.setValue(int(profile.idle_between_cores))
        self._building = False

    def set_failed_cores(self, failed_cores: list[int]) -> None:
        """Store failed cores from a test run and enable the retest button."""
        self._last_failed_cores = failed_cores
        self._retest_failed_btn.setEnabled(bool(failed_cores))
        if failed_cores:
            n = len(failed_cores)
            self._retest_failed_btn.setText(
                f"Retest {n} Failed Core{'s' if n != 1 else ''} Only"
            )
        else:
            self._retest_failed_btn.setText("Retest Failed Cores Only")

    def _on_retest_failed(self) -> None:
        """Populate core selection with only the failed cores."""
        cores = getattr(self, "_last_failed_cores", [])
        if cores:
            self._cores_input.setText(",".join(str(c) for c in sorted(cores)))
