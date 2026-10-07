"""Property-based value injection into the GUI input surfaces.

Any string a user can type, any profile a saved file can hold, and any config a
resumed session can carry must never crash the UI and never yield an offset or
core id outside the hardware's supported range.
"""

from __future__ import annotations

import sys as _sys
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.engine.backends import load_all
from corecycler.engine.topology import CPUTopology, PhysicalCore

_CACHE: dict = {}


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo() -> CPUTopology:
    topo = CPUTopology(model_name="Test 8C", family=26, model=0x44, physical_cores=8, ccds=2)
    for cid in range(8):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=0 if cid < 4 else 1, ccx=None, logical_cpus=(cid, cid + 8))
    return topo


def _config_tab():
    if "cfg" not in _CACHE:
        _qapp()
        load_all()
        from corecycler.gui.config_tab import ConfigTab

        _CACHE["cfg"] = ConfigTab(_topo())
    return _CACHE["cfg"]


def _smu_tab():
    if "smu" not in _CACHE:
        _qapp()
        from corecycler.gui.smu_tab import SMUTab

        _CACHE["smu"] = SMUTab(_topo())
    return _CACHE["smu"]


def _tuner_tab():
    if "tuner" not in _CACHE:
        _qapp()
        from corecycler.gui.tuner_tab import TunerTab

        _CACHE["tuner"] = TunerTab(None, _topo(), (-50, 10))
    return _CACHE["tuner"]


class TestCoreInputParser:
    @settings(deadline=None)
    @given(st.text(max_size=48))
    def test_any_string_yields_none_or_valid_cores(self, text):
        tab = _config_tab()
        tab._cores_input.setText(text)
        cores = tab.get_profile().cores_to_test
        assert cores is None or all(0 <= c <= 7 for c in cores)

    @settings(deadline=None)
    @given(st.lists(st.integers(-50, 50), max_size=12))
    def test_integer_lists_filter_to_present_cores(self, ids):
        tab = _config_tab()
        tab._cores_input.setText(",".join(str(i) for i in ids))
        cores = tab.get_profile().cores_to_test
        assert cores is None or set(cores) <= set(range(8))


class TestCoProfileInjection:
    @settings(deadline=None)
    @given(st.dictionaries(st.integers(-5, 12), st.integers(-1000, 1000), max_size=10))
    def test_profile_always_clamps_within_co_range(self, profile):
        tab = _smu_tab()
        lo, hi = tab._commands.co_range
        with patch("corecycler.gui.smu_tab.QMessageBox.warning"):
            tab.set_co_profile(profile)
        for spin in tab._spinboxes.values():
            assert lo <= spin.value() <= hi


class TestTunerSettings:
    @settings(deadline=None, max_examples=50)
    @given(st.data())
    def test_no_typed_value_yields_a_knob_below_its_floor(self, data):
        from corecycler.tuner.machine.types import KNOB_FLOOR

        tab = _tuner_tab()
        for widget in tab._knobs.values():
            if hasattr(widget, "setValue"):
                widget.setValue(data.draw(st.integers(-(10**7), 10**7)))
        cfg = tab.config()
        assert all(getattr(cfg, name) >= floor for name, floor in KNOB_FLOOR.items())
        assert -50 <= cfg.max_offset <= -1
