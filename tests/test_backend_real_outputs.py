from __future__ import annotations

from pathlib import Path

import pytest

from corecycler.engine.backends.stress_ng import StressNgBackend
from corecycler.engine.backends.stressapptest import StressapptestBackend

REAL = Path(__file__).parent / "fixtures" / "backends"


@pytest.mark.req("EC.real")
@pytest.mark.parametrize(
    ("backend", "captured"),
    [
        (StressNgBackend, "stress-ng-0.22.01-cpu-fft-clean.txt"),
        (StressNgBackend, "stress-ng-0.22.01-vm-clean.txt"),
        (StressapptestBackend, "stressapptest-1.0.11-clean.txt"),
    ],
)
def test_a_real_clean_run_raises_no_finding(backend, captured):
    assert backend().findings((REAL / captured).read_text(), Path()) == ()
