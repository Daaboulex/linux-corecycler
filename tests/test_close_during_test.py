"""Worker-lifecycle scenario matrix: start, stop, finish, crash, and close
in every combination the window can meet them, against the real widgets."""

from __future__ import annotations

import io
import json
import os
import sys as _sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

_sys.path.insert(0, str(Path(__file__).parent))

from test_main_window_wiring import _build, _qapp

from corecycler.engine.scheduler import CoreTestStatus
from corecycler.gui import main_window as mw
from corecycler.history.db import HistoryDB


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def no_modal(monkeypatch):
    monkeypatch.setattr(mw, "QMessageBox", MagicMock())
    return mw.QMessageBox


@pytest.fixture
def window(monkeypatch, tmp_path, db):
    win = _build(monkeypatch, tmp_path, db=db)
    yield win
    win._history_db = None


def _running_worker() -> MagicMock:
    worker = MagicMock()
    worker.isRunning.return_value = True
    worker.wait.return_value = True
    return worker


def _close(window) -> MagicMock:
    event = MagicMock()
    window.closeEvent(event)
    return event


def _answer(no_modal, button: str) -> None:
    no_modal.question.return_value = getattr(no_modal.StandardButton, button)


class TestCloseDuringTest:
    def test_the_queued_finished_signal_after_close_is_inert(self, window, no_modal):
        window._worker = _running_worker()
        _answer(no_modal, "Yes")
        event = _close(window)
        assert event.accept.called
        window._on_worker_finished()
        assert window._closing is True

    def test_close_disconnects_the_finished_handler(self, window, no_modal):
        worker = _running_worker()
        window._worker = worker
        _answer(no_modal, "Yes")
        _close(window)
        worker.finished.disconnect.assert_any_call(window._on_worker_finished)

    def test_close_stops_the_worker_before_closing_the_database(self, window, no_modal):
        order: list[str] = []
        worker = _running_worker()
        worker.scheduler.force_stop.side_effect = lambda: order.append("stop")
        worker.wait.side_effect = lambda _ms: (order.append("wait"), True)[1]
        window._worker = worker
        close = window._history_db.close
        window._history_db = MagicMock()
        window._history_db.close.side_effect = lambda: order.append("db-close")
        _answer(no_modal, "Yes")
        _close(window)
        assert order == ["stop", "wait", "db-close"]
        close()

    def test_answering_no_keeps_everything_alive(self, window, no_modal, db):
        window._worker = _running_worker()
        _answer(no_modal, "No")
        event = _close(window)
        assert event.ignore.called
        assert not event.accept.called
        assert window._closing is False
        assert db.list_runs() == []
        window._worker = None
        window._on_worker_finished()

    def test_an_idle_close_saves_settings_and_closes_the_database(self, window, db):
        event = _close(window)
        assert event.accept.called
        assert mw.save_settings.called
        with pytest.raises(Exception, match="[Cc]losed"):
            db.list_runs()

    @pytest.mark.req("E6.B2.close")
    def test_close_leaves_a_tuner_driver_running_and_stops_memory_stress(self, window, no_modal, monkeypatch):
        from corecycler.smu.writer_lock import co_writer, running_driver

        memory_stop = MagicMock()
        monkeypatch.setattr(window._memory_tab, "force_stop", memory_stop)
        window._memory_tab._stress_worker = _running_worker()
        _answer(no_modal, "Yes")
        with co_writer(record=True):
            event = _close(window)
            assert running_driver() == os.getpid()
        assert event.accept.called
        assert memory_stop.called

    def test_a_tuner_driver_alone_does_not_hold_the_close(self, window, no_modal):
        from corecycler.smu.writer_lock import co_writer

        with co_writer(record=True):
            event = _close(window)
        assert event.accept.called
        assert not no_modal.question.called


class TestWorkerFinishAlive:
    def test_a_finish_while_alive_refreshes_history(self, window, monkeypatch):
        refresh = MagicMock()
        monkeypatch.setattr(window._history_tab, "refresh", refresh)
        window._on_worker_finished()
        assert refresh.called
        assert "complete" in window._status_msg.text().lower() or ("stopped" in window._status_msg.text().lower())

    def test_a_crashing_scheduler_surfaces_instead_of_dying_silently(self):
        _qapp()
        scheduler = MagicMock()
        scheduler.run.side_effect = RuntimeError("boom")
        worker = mw.TestWorker(scheduler, io.BytesIO())
        seen: list[str] = []
        worker.crashed.connect(seen.append)
        worker.run()
        assert seen == ["boom"]

    def test_a_crash_message_reaches_the_status_bar(self, window):
        window._on_worker_crashed("boom")
        assert "boom" in window._status_msg.text()

    @pytest.mark.req("T23.window.crash")
    def test_a_crash_still_reads_crashed_after_the_worker_finishes(self, window):
        window._on_worker_crashed("boom")
        window._on_worker_finished()
        assert window._status_msg.text() == "Test worker crashed: boom"

    def test_a_crash_after_close_stays_silent(self, window):
        window._closing = True
        before = window._status_msg.text()
        window._on_worker_crashed("boom")
        assert window._status_msg.text() == before


class TestHonestSummary:
    def _summary(self, window, monkeypatch, payload: dict, rounds: tuple[int, int] = (1, 1)) -> dict:
        update = MagicMock()
        monkeypatch.setattr(window._results_tab, "update_summary", update)
        window._test_start_time = time.monotonic()
        window._run_rounds, window._rounds_done = rounds
        window._on_test_completed(json.dumps(payload))
        return update.call_args.kwargs

    @pytest.mark.req("T23.results.summary")
    def test_a_run_stopped_in_its_first_round_says_no_round_completed_of_the_run_s_rounds(self, window, monkeypatch):
        window._config_tab._cycles_spin.setValue(7)
        kwargs = self._summary(window, monkeypatch, {"0": [{"passed": True}], "1": []}, rounds=(3, 0))
        assert (kwargs["cycle"], kwargs["total_cycles"]) == (0, 3)

    @pytest.mark.req("T23.results.summary")
    def test_the_live_summary_counts_the_run_s_cores_and_the_round_under_test(self, window, monkeypatch):
        update = MagicMock()
        monkeypatch.setattr(window._results_tab, "update_summary", update)
        window._worker = MagicMock(isRunning=MagicMock(return_value=True))
        window._test_start_time = time.monotonic()
        window._run_cores, window._run_rounds = 8, 3
        window._config_tab._cycles_spin.setValue(5)
        window._on_core_started(0, 1)
        window._on_status_cached(0, CoreTestStatus(core_id=0, state="passed"))
        window._update_elapsed()
        kwargs = update.call_args.kwargs
        assert (kwargs["total"], kwargs["passed"], kwargs["cycle"], kwargs["total_cycles"]) == (8, 1, 2, 3)
        window._worker = None

    def test_a_stopped_core_is_not_counted_as_failed(self, window, monkeypatch):
        kwargs = self._summary(window, monkeypatch, {"0": [{"passed": True}], "1": []})
        assert kwargs["total"] == 1
        assert kwargs["passed"] == 1
        assert kwargs["failed"] == 0

    def test_a_failed_core_still_counts_and_is_retestable(self, window, monkeypatch):
        retest = MagicMock()
        monkeypatch.setattr(window._config_tab, "set_failed_cores", retest)
        kwargs = self._summary(
            window,
            monkeypatch,
            {"0": [{"passed": False, "error_message": "mprime error: FATAL"}], "1": []},
        )
        assert kwargs["total"] == 1
        assert kwargs["failed"] == 1
        retest.assert_called_once_with([0])

    def test_a_fully_stopped_run_reports_nothing_tested(self, window, monkeypatch):
        kwargs = self._summary(window, monkeypatch, {"0": [], "1": []})
        assert kwargs["total"] == 0
        assert kwargs["failed"] == 0
