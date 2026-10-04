"""Edge coverage for backend helpers and tuner persistence wrappers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.engine.backends.base import StressConfig
from corecycler.engine.backends.stressapptest import StressapptestBackend
from corecycler.tuner import persistence


class TestStressapptestPrepareCleanup:
    def test_prepare_creates_work_dir(self, tmp_path):
        work = tmp_path / "sat"
        StressapptestBackend().prepare(work, StressConfig())
        assert work.exists()

    def test_cleanup_noop(self, tmp_path):
        StressapptestBackend().cleanup(tmp_path)


class TestPersistenceWrappers:
    def test_get_session_offsets_delegates(self):
        db = MagicMock()
        db.get_tuner_session_offsets.return_value = {0: -5}
        assert persistence.get_session_offsets(db, 7) == {0: -5}
        db.get_tuner_session_offsets.assert_called_once_with(7)
