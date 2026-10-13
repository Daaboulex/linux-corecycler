"""MainWindow construction and control flow against a hermetic environment.

test_main_window.py drives the slots bound to a SimpleNamespace. This file
builds the REAL window, with every door to the user's machine replaced: the
history database is in-memory, settings are never written, topology, MSR and
hwmon are stand-ins, and the worker thread is never started.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys as _sys
from unittest.mock import MagicMock, patch

import pytest

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("GUI tests require real PySide6", allow_module_level=True)

from corecycler.config.settings import AppSettings, save_profile
from corecycler.engine.backends.base import Outcome, StressResult
from corecycler.engine.scheduler import CoreTestStatus
from corecycler.engine.topology import CPUTopology, PhysicalCore
from corecycler.gui import main_window as mw
from corecycler.history.db import HistoryDB, RunRecord
from tests._old_rows import seed_old_session
from tests._sensors import FakeClocks, FakeSensors, FakeTicker
from tests._tuner_world import save_machine_session


def _qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _topo(cores: int = 2) -> CPUTopology:
    topo = CPUTopology(
        model_name="Test 8C X3D",
        family=26,
        model=0x44,
        physical_cores=cores,
        logical_cpus_count=cores * 2,
        smt_enabled=True,
        ccds=2,
        is_x3d=True,
        vcache_ccd=0,
    )
    for cid in range(cores):
        topo.cores[cid] = PhysicalCore(core_id=cid, ccd=cid, ccx=None, logical_cpus=(cid, cid + 8))
    return topo


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def no_modal(monkeypatch):
    monkeypatch.setattr(mw, "QMessageBox", MagicMock())
    return mw.QMessageBox


def _settings(tmp_path, **over):
    s = AppSettings(work_dir=str(tmp_path / "work"))
    for key, value in over.items():
        setattr(s, key, value)
    return s


def _build(
    monkeypatch,
    tmp_path,
    *,
    db=None,
    db_factory=None,
    settings=None,
    topology=None,
    bios=(False, "", ""),
    adopt=None,
):
    _qapp()
    monkeypatch.setattr(mw, "load_all", MagicMock())
    monkeypatch.setattr(mw, "load_settings", lambda: settings or _settings(tmp_path))
    monkeypatch.setattr(mw, "save_settings", MagicMock())
    monkeypatch.setattr(mw, "HistoryDB", db_factory or (lambda: db))
    monkeypatch.setattr(mw, "adopt_legacy_root_db", adopt or (lambda _d: None))
    monkeypatch.setattr(mw, "detect_topology", lambda: topology or _topo())
    monkeypatch.setattr(mw, "detect_bios_change", bios if callable(bios) else (lambda _d: bios))
    monkeypatch.setattr(mw, "Ticker", FakeTicker)
    monkeypatch.setattr(mw, "Sensors", FakeSensors)
    monkeypatch.setattr(mw, "Clocks", lambda: FakeClocks({0: 4400.0, 8: 4500.0, 1: 4400.0}))
    monkeypatch.setattr(mw, "lane_problem", lambda: None)
    return mw.MainWindow()


@pytest.fixture
def window(monkeypatch, tmp_path, db):
    win = _build(monkeypatch, tmp_path, db=db)
    yield win
    win._history_db = None


def _mock_worker(running: bool = True):
    worker = MagicMock()
    worker.isRunning.return_value = running
    worker.wait.return_value = False
    return worker


class TestWorkerBridge:
    def test_every_scheduler_callback_reaches_a_signal(self):
        _qapp()
        scheduler = MagicMock()
        worker = mw.TestWorker(scheduler)
        seen = {}
        worker.core_started.connect(lambda c, cyc: seen.setdefault("start", (c, cyc)))
        worker.core_finished.connect(lambda c, r: seen.setdefault("finish", c))
        worker.status_updated.connect(lambda c, s: seen.setdefault("status", c))
        worker.cycle_completed.connect(lambda cyc: seen.setdefault("cycle", cyc))
        worker.test_completed.connect(lambda payload: seen.setdefault("done", payload))

        result = StressResult(core_id=0, outcome=Outcome.PASS, detail="", duration_seconds=1.0)
        scheduler.on_core_start[0](0, 1)
        scheduler.on_core_finish[0](0, result)
        scheduler.on_status_update[0](0, CoreTestStatus(core_id=0))
        scheduler.on_cycle_complete[0](2)
        scheduler.on_test_complete[0]({0: [result]})

        assert seen["start"] == (0, 1)
        assert seen["finish"] == 0
        assert seen["status"] == 0
        assert seen["cycle"] == 2
        assert json.loads(seen["done"])["0"][0]["passed"] is True

    def test_a_failed_result_keeps_its_outcome_and_output_in_the_payload(self):
        _qapp()
        scheduler = MagicMock()
        worker = mw.TestWorker(scheduler)
        seen = []
        worker.test_completed.connect(seen.append)
        failed = StressResult(
            core_id=1, outcome=Outcome.COMPUTATION, detail="rounding", duration_seconds=2.0, output="FATAL ERROR"
        )
        scheduler.on_test_complete[0]({1: [failed]})
        row = json.loads(seen[0])["1"][0]
        assert (row["passed"], row["outcome"], row["detail"]) == (False, "computation", "rounding")
        assert row["output"] == "FATAL ERROR"

    def test_run_delegates_to_the_scheduler(self):
        _qapp()
        scheduler = MagicMock()
        worker = mw.TestWorker(scheduler)
        worker.run()
        assert scheduler.run.called


class TestConstruction:
    def test_the_window_wires_every_tab(self, window):
        assert window._tabs.count() == 7
        assert window._history_db is not None
        assert window._topology.model_name == "Test 8C X3D"
        assert window._start_btn.isEnabled()
        assert not window._stop_btn.isEnabled()

    def test_a_bios_change_reaches_the_history_tab(self, monkeypatch, tmp_path, db):
        db.create_run(RunRecord(started_at="2026-07-20T10:00:00+00:00", status="running"))
        win = _build(
            monkeypatch,
            tmp_path,
            db=db,
            bios=(True, "2401", "2402"),
            adopt=lambda _d: {"runs": 2},
        )
        assert win._bios_changed
        assert win._history_tab._bios_warning == "BIOS changed: 2401 -> 2402"
        win._history_db = None

    def test_adoption_and_bios_failures_do_not_stop_startup_and_are_logged(self, monkeypatch, tmp_path, db, caplog):
        def _boom(_d):
            raise OSError("unreadable")

        with caplog.at_level("ERROR", logger="corecycler.gui.main_window"):
            win = _build(monkeypatch, tmp_path, db=db, bios=_boom, adopt=_boom)
        assert win._history_db is db
        assert not win._bios_changed
        assert "Failed to detect a BIOS change" in caplog.text
        win._history_db = None

    def test_the_old_sessions_are_archived_at_start_and_a_failure_is_logged(self, monkeypatch, tmp_path, caplog):
        on_disk = HistoryDB(tmp_path / "history.db")
        seed_old_session(on_disk)
        with caplog.at_level("INFO", logger="corecycler.gui.main_window"):
            win = _build(monkeypatch, tmp_path, db=on_disk)
        assert (tmp_path / "history.db.old-tuner-sessions.json").exists()
        assert "Old tuner sessions archived to" in caplog.text
        win._history_db = None

        def _boom(_d):
            raise OSError("read-only")

        monkeypatch.setattr(mw, "archive_old_sessions", _boom)
        with caplog.at_level("ERROR", logger="corecycler.gui.main_window"):
            again = _build(monkeypatch, tmp_path, db=on_disk)
        assert "Archiving the old tuner sessions failed; their rows stay in the database" in caplog.text
        assert again._history_db is on_disk
        again._history_db = None
        on_disk.close()

    def test_an_unusable_database_leaves_the_window_running(self, monkeypatch, tmp_path):
        def _boom():
            raise OSError("locked")

        win = _build(monkeypatch, tmp_path, db_factory=_boom)
        assert win._history_db is None
        assert win._tabs.count() == 7

    def test_a_database_from_a_newer_build_is_refused_on_screen(self, monkeypatch, tmp_path):
        from PySide6.QtWidgets import QLabel

        path = tmp_path / "history.db"
        HistoryDB(path).close()
        newer = HistoryDB.SCHEMA_VERSION + 1
        raw = sqlite3.connect(path)
        raw.execute("UPDATE schema_version SET version=?", (newer,))
        raw.commit()
        raw.close()

        win = _build(monkeypatch, tmp_path, db_factory=lambda: HistoryDB(path))

        shown = [label.text() for label in win._status_bar.findChildren(QLabel)]
        assert win._history_db is None
        assert any(f"version {newer}" in text for text in shown)

    def test_history_recording_can_be_switched_off(self, monkeypatch, tmp_path, db):
        win = _build(monkeypatch, tmp_path, db=db, settings=_settings(tmp_path, record_history=False))
        assert win._history_db is None

    def test_a_readable_msr_is_left_out_of_the_privilege_warning(self, monkeypatch, tmp_path, db):
        from PySide6.QtWidgets import QLabel

        real_open = os.open

        def fake_open(path, *args, **kwargs):
            if path == "/dev/cpu/0/msr":
                return real_open(os.devnull, os.O_RDONLY)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", fake_open)
        win = _build(monkeypatch, tmp_path, db=db)
        warnings = [label.text() for label in win._status_bar.findChildren(QLabel) if "unavailable" in label.text()]
        assert not any("MSR" in text for text in warnings)
        win._history_db = None


class TestStartTest:
    def _ready(self, window, monkeypatch, *, available=True):
        backend = MagicMock()
        backend.is_available.return_value = available
        monkeypatch.setattr(mw, "get_backend", lambda _n: backend)
        monkeypatch.setattr(mw, "TestWorker", MagicMock())
        return backend

    def test_refuses_without_topology(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch)
        window._topology = None
        window._start_test()
        assert window._worker is None
        assert no_modal.warning.called

    def test_refuses_while_memory_stress_runs(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch)
        window._memory_tab._stress_worker = _mock_worker()
        window._start_test()
        assert window._worker is None
        assert no_modal.warning.call_args.args[1] == "Memory Stress Active"

    def test_a_host_whose_lanes_cannot_count_oom_kills_is_refused(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch)
        problem = "the lane cgroup /user.slice/app.slice does not give its scopes the memory controller"
        monkeypatch.setattr(mw, "lane_problem", lambda: problem)
        window._start_test()
        assert window._worker is None
        assert no_modal.warning.call_args.args[2] == f"Cannot start: {problem}"

    def test_no_ticked_core_is_refused(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch)
        for box in window._config_tab._core_boxes.values():
            box.setChecked(False)
        window._start_test()
        assert window._worker is None
        assert no_modal.warning.call_args.args[1] == "No cores"

    def test_an_active_tuner_session_can_be_declined(self, window, monkeypatch, no_modal, tmp_path):
        self._ready(window, monkeypatch)
        saved = save_machine_session(window._history_db, tmp_path, "paused")
        window._tuner_tab.refresh()
        no_modal.question.return_value = no_modal.StandardButton.No
        window._start_test()
        assert window._worker is None
        assert f"Tuning session {saved.session_id} is paused" in no_modal.question.call_args.args[2]

    def test_an_unknown_backend_is_refused(self, window, monkeypatch, no_modal):
        def _missing(_n):
            raise KeyError("nope")

        monkeypatch.setattr(mw, "get_backend", _missing)
        monkeypatch.setattr(mw, "TestWorker", MagicMock())
        window._start_test()
        assert window._worker is None
        assert "Unknown backend" in no_modal.warning.call_args.args[2]

    def test_an_uninstalled_backend_is_refused(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch, available=False)
        asked = []
        monkeypatch.setattr(mw, "ensure_tool", lambda parent, key: asked.append(key) or False)
        window._start_test()
        assert window._worker is None
        assert len(asked) == 1, "the user must be offered a path before the test is refused"

    def test_an_uninstalled_backend_starts_once_the_user_supplies_a_path(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch, available=False)
        monkeypatch.setattr(mw, "ensure_tool", lambda parent, key: True)
        window._start_test()
        assert window._worker is not None

    def test_a_scheduler_that_cannot_start_is_reported(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch)

        def _boom(**_kwargs):
            raise RuntimeError("no work dir")

        monkeypatch.setattr(mw, "CoreScheduler", _boom)
        window._start_test()
        assert window._worker is None
        assert "Failed to initialize scheduler" in no_modal.warning.call_args.args[2]

    def test_an_uncreatable_work_dir_is_reported_not_crashed(self, window, monkeypatch, no_modal):
        self._ready(window, monkeypatch)

        def _denied(_configured):
            raise PermissionError("read-only work root")

        monkeypatch.setattr(mw, "ensure_work_dir", _denied)
        window._start_test()
        assert window._worker is None
        assert "Work directory unavailable" in no_modal.warning.call_args.args[1]
        assert "read-only work root" in no_modal.warning.call_args.args[2]

    def test_a_started_test_locks_the_ui_and_logs_history(self, window, monkeypatch):
        self._ready(window, monkeypatch)
        window._start_test()
        assert window._worker is not None
        assert window._worker.start.called
        assert not window._start_btn.isEnabled()
        assert window._stop_btn.isEnabled()
        assert window._logger is not None
        assert window._elapsed_timer.isActive()
        window._elapsed_timer.stop()

    def test_a_broken_history_logger_does_not_stop_the_test(self, window, monkeypatch):
        self._ready(window, monkeypatch)

        def _boom(*_args, **_kwargs):
            raise OSError("db gone")

        monkeypatch.setattr(mw, "TestRunLogger", _boom)
        window._start_test()
        assert window._worker is not None
        assert window._logger is None
        window._elapsed_timer.stop()


class TestStopTest:
    def test_stopping_without_a_worker_is_a_noop(self, window):
        window._stop_test()
        assert window._worker is None

    def test_stopping_flushes_telemetry_and_signals_the_scheduler(self, window):
        worker = _mock_worker()
        window._worker = worker
        logger = MagicMock()
        window._logger = logger
        window._core_telemetry[0] = {
            "max_freq": 5000.0,
            "max_temp": 80.0,
            "min_vcore": 1.1,
            "max_vcore": 1.3,
        }
        window._stop_test()
        assert logger.update_core_telemetry_peaks.called
        assert logger.on_test_stopped.called
        assert worker.scheduler.stop.called
        assert window._logger is None
        assert window._core_telemetry == {}

    def test_a_failed_peak_write_is_logged_and_the_stop_still_recorded(self, window, caplog):
        window._worker = _mock_worker()
        logger = MagicMock()
        logger.update_core_telemetry_peaks.side_effect = OSError("db gone")
        window._logger = logger
        window._core_telemetry[3] = {"max_freq": 5000.0, "max_temp": 80.0, "min_vcore": 1.1, "max_vcore": 1.3}
        with caplog.at_level("ERROR", logger="corecycler.gui.main_window"):
            window._stop_test()
        assert "Failed to record telemetry peaks of core 3" in caplog.text
        assert logger.on_test_stopped.called

    def test_one_signal_that_was_never_connected_leaves_the_others_disconnected(self, window):
        worker = _mock_worker()
        worker.core_started.disconnect.side_effect = RuntimeError("not connected")
        window._worker = worker
        window._logger = MagicMock()
        window._stop_test()
        for name in ("core_finished", "status_updated", "cycle_completed", "test_completed"):
            assert getattr(worker, name).disconnect.called, name

    def test_a_logger_that_fails_to_record_the_stop_is_surfaced(self, window, caplog):
        window._worker = _mock_worker()
        logger = MagicMock()
        logger.on_test_stopped.side_effect = OSError("db gone")
        window._logger = logger
        with caplog.at_level("ERROR", logger="corecycler.gui.main_window"):
            window._stop_test()
        assert "Failed to record test stop" in caplog.text


class TestCoreFinished:
    def _telemetry(self):
        return {
            "max_freq": 5000.0,
            "max_stretch_pct": 2.5,
            "core_watts": 21.0,
            "max_temp": 81.5,
            "last_vcore": 1.2,
            "min_vcore": 1.1,
            "max_vcore": 1.3,
        }

    def test_a_finished_core_logs_its_peaks(self, window):
        window._core_status_cache[0] = CoreTestStatus(core_id=0, state="passed")
        window._core_telemetry[0] = self._telemetry()
        logger = MagicMock()
        window._logger = logger
        window._on_core_finished(0, StressResult(core_id=0, outcome=Outcome.PASS, detail="", duration_seconds=10.0))
        assert logger.update_core_telemetry_peaks.called
        assert 0 not in window._core_telemetry

    def test_a_failed_peak_write_is_surfaced(self, window, caplog):
        window._core_telemetry[0] = self._telemetry()
        logger = MagicMock()
        logger.update_core_telemetry_peaks.side_effect = OSError("db gone")
        window._logger = logger
        with caplog.at_level("ERROR", logger="corecycler.gui.main_window"):
            window._on_core_finished(
                0, StressResult(core_id=0, outcome=Outcome.COMPUTATION, detail="rounding", duration_seconds=1.0)
            )
        assert "Failed to record telemetry peaks" in caplog.text


class TestStatusSlots:
    def test_status_update_reaches_the_core_grid(self, window):
        window._on_status_updated(0, CoreTestStatus(core_id=0, state="testing"))
        assert window._core_grid._cells[0] is not None

    def test_cycle_completion_is_announced(self, window):
        window._on_cycle_completed(2)
        assert window._status_msg.text() == "Cycle 3 complete"

    def test_a_non_numeric_core_key_is_skipped(self, window):
        window._config_tab.set_failed_cores = MagicMock()
        window._on_test_completed(json.dumps({"abc": [{"passed": False}], "2": [{"passed": False}]}))
        assert window._config_tab.set_failed_cores.call_args.args == ([2],)

    def test_worker_completion_restores_the_ui(self, window):
        window._worker = _mock_worker()
        logger = window._logger = MagicMock()
        window._stop_btn.setEnabled(True)
        window._on_worker_finished()
        assert window._status_msg.text() == "Test complete"
        assert window._start_btn.isEnabled()
        assert window._worker is None
        assert window._logger is None
        logger.write_telemetry.assert_called_once_with()

    def test_a_failing_last_telemetry_write_is_logged_and_the_ui_still_restores(self, window, caplog):
        window._worker = _mock_worker()
        window._logger = MagicMock()
        window._logger.write_telemetry.side_effect = sqlite3.OperationalError("disk I/O error")
        window._stop_btn.setEnabled(True)
        window._on_worker_finished()
        assert window._logger is None
        assert window._start_btn.isEnabled()
        assert "Failed to write the run's last telemetry to history" in caplog.text

    def test_worker_completion_after_a_stop_says_so(self, window):
        window._worker = _mock_worker()
        window._stop_btn.setEnabled(False)
        window._on_worker_finished()
        assert window._status_msg.text() == "Test stopped"


class TestTunerAndMemoryBridges:
    def test_tuner_elapsed_reaches_the_core_grid(self, window):
        window._on_tuner_core_elapsed(1, 12.5)
        assert window._core_grid._cells[1] is not None

    def test_memory_stress_completion_releases_the_starters(self, window):
        window._start_btn.setEnabled(False)
        window._on_memory_stress_done(True)
        assert window._start_btn.isEnabled()

    def test_memory_stress_completion_keeps_start_locked_during_a_run(self, window):
        window._worker = _mock_worker()
        window._on_memory_stress_done(False)
        assert not window._start_btn.isEnabled()

    def test_a_loaded_profile_switches_to_the_curve_tuner(self, window):
        window._on_load_co_profile({0: -30})
        assert window._tabs.currentWidget() is window._smu_tab

    def test_opening_the_curve_tuner_rereads_the_offsets(self, window):
        window._smu_tab._read_all_co = MagicMock()
        window._on_tab_changed(window._tabs.indexOf(window._smu_tab))
        assert window._smu_tab._read_all_co.called

    def test_other_tabs_do_not_reread_the_offsets(self, window):
        window._smu_tab._read_all_co = MagicMock()
        window._on_tab_changed(window._tabs.indexOf(window._results_tab))
        assert not window._smu_tab._read_all_co.called


class TestOneTick:
    def test_one_timer_on_the_poll_interval_feeds_every_view_the_same_tick(self, monkeypatch, tmp_path, db):
        window = _build(monkeypatch, tmp_path, db=db, settings=_settings(tmp_path, poll_interval=0.25))
        assert window._tick_timer.isActive()
        assert window._tick_timer.interval() == 250
        window._monitor_tab.on_tick = MagicMock()
        window._memory_tab.on_tick = MagicMock()
        window._active_test_core = 0
        window._on_tick()
        tick = window._monitor_tab.on_tick.call_args.args[0]
        window._memory_tab.on_tick.assert_called_once_with(tick)
        assert window._core_telemetry[0]["max_freq"] == 4500.0
        window._history_db = None


class TestElapsedWatchdog:
    def test_without_a_worker_nothing_happens(self, window):
        window._update_elapsed()
        assert window._status_msg.text() == "Ready"

    def test_a_worker_that_exited_is_cleaned_up(self, window):
        window._worker = _mock_worker(running=False)
        window._stop_btn.setEnabled(True)
        window._update_elapsed()
        assert window._worker is None
        assert "worker exited unexpectedly" in window._status_msg.text()

    def test_telemetry_for_an_unknown_core_is_skipped(self, window):
        window._active_test_core = 99
        window._feed_core_grid_telemetry(window._ticker.read([]))
        assert window._core_telemetry == {}

    def test_a_telemetry_sample_is_recorded(self, window):
        window._sensors.give(tctl=65.0, tccd=((0, 72.0),), vcore=1.2)
        logger = MagicMock()
        window._logger = logger
        window._active_test_core = 0
        window._feed_core_grid_telemetry(window._ticker.read([]))
        logger.record_telemetry_sample.assert_called_once_with(0, 4500.0, 72.0, 1.2, effective_max_mhz=None)
        assert window._core_telemetry[0]["max_freq"] == 4500.0

    def test_a_failing_telemetry_write_is_logged_once_and_its_recovery_is_logged(self, window, caplog):
        window._logger = MagicMock()
        window._logger.record_telemetry_sample.side_effect = [sqlite3.OperationalError("disk full")] * 2 + [None]
        window._active_test_core = 0
        with caplog.at_level("INFO", logger="corecycler.gui.main_window"):
            for _ in range(3):
                window._feed_core_grid_telemetry(window._ticker.read([]))
        assert caplog.text.count("disk full") == 1
        assert "recorded again" in caplog.text

    def test_a_core_with_no_temperature_read_records_none_not_zero(self, window):
        window._sensors.give()
        window._logger = MagicMock()
        window._active_test_core = 0
        window._feed_core_grid_telemetry(window._ticker.read([]))
        assert window._logger.record_telemetry_sample.call_args.args[2] is None
        assert window._core_telemetry[0]["max_temp"] == 0.0


class TestProfileIo:
    def test_a_cancelled_save_writes_nothing(self, window, tmp_path):
        with patch("corecycler.gui.main_window.QFileDialog.getSaveFileName", return_value=("", "")):
            window._save_profile()
        assert not list(tmp_path.glob("*.json"))

    def test_a_profile_is_saved(self, window, tmp_path):
        out = tmp_path / "profile.json"
        with patch(
            "corecycler.gui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(out), ""),
        ):
            window._save_profile()
        written = json.loads(out.read_text())
        assert written["seconds_per_core"] == window._config_tab.get_profile().seconds_per_core

    def test_a_failed_save_is_reported(self, window, tmp_path, no_modal, monkeypatch):
        import corecycler.config.settings as settings

        def _boom(*_args, **_kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(settings, "save_profile", _boom)
        with patch(
            "corecycler.gui.main_window.QFileDialog.getSaveFileName",
            return_value=(str(tmp_path / "p.json"), ""),
        ):
            window._save_profile()
        assert "Failed to save profile" in no_modal.warning.call_args.args[2]

    def test_a_cancelled_load_changes_nothing(self, window):
        before = window._config_tab.get_profile()
        with patch("corecycler.gui.main_window.QFileDialog.getOpenFileName", return_value=("", "")):
            window._load_profile()
        assert window._config_tab.get_profile() == before

    def test_a_profile_is_loaded(self, window, tmp_path):
        src = tmp_path / "profile.json"
        profile = window._config_tab.get_profile()
        profile.seconds_per_core = 123
        save_profile(profile, src)
        with patch(
            "corecycler.gui.main_window.QFileDialog.getOpenFileName",
            return_value=(str(src), ""),
        ):
            window._load_profile()
        assert window._config_tab.get_profile().seconds_per_core == 123

    def test_a_failed_load_is_reported(self, window, tmp_path, no_modal):
        with patch(
            "corecycler.gui.main_window.QFileDialog.getOpenFileName",
            return_value=(str(tmp_path / "missing.json"), ""),
        ):
            window._load_profile()
        assert "Failed to load profile" in no_modal.warning.call_args.args[2]


class TestAutoResume:
    def test_the_window_hands_auto_resume_to_the_tuner_tab(self, window, monkeypatch, tmp_path):
        saved = save_machine_session(window._history_db, tmp_path, "running")
        launched = []
        monkeypatch.setattr(window._tuner_tab, "_launch", lambda args, auto=False: launched.append((args, auto)))
        window.attempt_auto_resume()
        assert launched == [(["--resume", "--session", str(saved.session_id)], True)]


class TestCloseEvent:
    def test_an_idle_window_closes_cleanly(self, window):
        event = MagicMock()
        window.closeEvent(event)
        assert event.accept.called
        assert window._ticker.closed
        assert not window._tick_timer.isActive()

    def test_a_declined_close_is_cancelled(self, window, no_modal):
        window._worker = _mock_worker()
        no_modal.question.return_value = no_modal.StandardButton.No
        event = MagicMock()
        window.closeEvent(event)
        assert event.ignore.called
        assert not event.accept.called

    def test_an_accepted_close_force_stops_the_worker(self, window, no_modal):
        worker = _mock_worker()
        window._worker = worker
        logger = MagicMock()
        window._logger = logger
        window._core_telemetry[2] = {"max_freq": 5100.0, "max_temp": 82.0, "min_vcore": 1.1, "max_vcore": 1.3}
        no_modal.question.return_value = no_modal.StandardButton.Yes
        event = MagicMock()
        window.closeEvent(event)
        assert logger.update_core_telemetry_peaks.call_args.args == (2,)
        assert logger.on_test_stopped.called
        assert worker.scheduler.force_stop.called
        assert worker.terminate.called
        assert event.accept.called


HALF_SCREEN = 960


def _whole_text_shown(label) -> bool:
    if label.wordWrap():
        return label.heightForWidth(label.width()) <= label.height()
    return label.fontMetrics().horizontalAdvance(label.text()) <= label.width()


@pytest.mark.req("T23.halfscreen")
def test_the_window_fits_a_half_screen_tile_and_keeps_its_text(monkeypatch, tmp_path, db):
    from PySide6.QtWidgets import QApplication, QLabel, QScrollArea

    win = _build(monkeypatch, tmp_path, db=db, topology=_topo(16))
    win.show()
    win._tabs.setCurrentWidget(win._tuner_tab)
    scroll = win._tuner_tab.findChild(QScrollArea)
    for advanced in (False, True):
        win._tuner_tab._advanced_toggle.setChecked(advanced)
        QApplication.processEvents()
        narrowest = win.minimumSizeHint().width()
        assert narrowest <= HALF_SCREEN - 20
        win.resize(narrowest, 1000)
        QApplication.processEvents()
        assert scroll.viewport().width() >= scroll.widget().minimumSizeHint().width() > 0
        texts = [label for label in win.centralWidget().findChildren(QLabel) if label.isVisible() and label.text()]
        assert texts
        assert [label.text() for label in texts if not _whole_text_shown(label)] == []
    warnings = [label for label in win.statusBar().findChildren(QLabel) if label.toolTip()]
    assert warnings
    assert all(label.isVisible() and label.width() > 0 for label in warnings)
    win._history_db = None


@pytest.mark.req("T23.startstop")
def test_the_toolbar_s_start_and_stop_are_plain_words_on_one_line(monkeypatch, tmp_path, db):
    from PySide6.QtWidgets import QApplication

    win = _build(monkeypatch, tmp_path, db=db)
    win.show()
    QApplication.processEvents()
    start, stop = win._start_btn, win._stop_btn
    assert start.text().isascii() and stop.text().isascii()
    assert start.text().split()[0] == "Start" and stop.text().split()[0] == "Stop"
    assert (start.y(), start.height()) == (stop.y(), stop.height())
    win._history_db = None


@pytest.mark.req("T23.style")
def test_a_style_fallback_is_said_in_the_status_bar(monkeypatch, tmp_path, db):
    from PySide6.QtWidgets import QApplication, QLabel

    win = _build(monkeypatch, tmp_path, db=db)
    win.show()
    win.show_style_fallback(None)
    QApplication.processEvents()
    assert not [label for label in win.statusBar().findChildren(QLabel) if "Fusion" in label.text()]
    win.show_style_fallback("system Qt style crashed in a probe (SIGSEGV)")
    QApplication.processEvents()
    said = [label for label in win.statusBar().findChildren(QLabel) if "Fusion" in label.text()]
    assert [label.toolTip() for label in said] == ["system Qt style crashed in a probe (SIGSEGV): running in Fusion"]
    assert said[0].isVisible()
    win._history_db = None
