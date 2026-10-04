"""Edge coverage for the tuner persistence wrappers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.tuner import persistence


class TestPersistenceWrappers:
    def test_get_session_offsets_delegates(self):
        db = MagicMock()
        db.get_tuner_session_offsets.return_value = {0: -5}
        assert persistence.get_session_offsets(db, 7) == {0: -5}
        db.get_tuner_session_offsets.assert_called_once_with(7)
