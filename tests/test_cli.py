"""Headless CLI: argument handling, exit codes, engine outcome mapping."""

from __future__ import annotations

import sys as _sys
from pathlib import Path

import pytest

_sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

if not hasattr(_sys.modules.get("PySide6", None), "__path__"):
    pytest.skip("CLI tests require real PySide6", allow_module_level=True)

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from corecycler import cli
from corecycler.history.db import HistoryDB
from corecycler.tuner import persistence as tp
from corecycler.tuner.config import TunerConfig
from corecycler.tuner.state import CoreState, TunerPhase


@pytest.fixture(autouse=True, scope="module")
def _qapp():
    # cmd_run reuses QCoreApplication.instance(); a bare QCoreApplication would
    # abort the later GUI tests that need a QApplication. Create the richer
    # QApplication up front so both share one instance.
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def _isolated_lock(tmp_path, monkeypatch):
    import corecycler.config.paths as paths

    monkeypatch.setattr(paths, "user_home", lambda: tmp_path)


class FakeEngine(QObject):
    log_message = Signal(str)
    session_completed = Signal(str)
    status_changed = Signal(str)

    def __init__(self, behavior: str) -> None:
        super().__init__()
        self.behavior = behavior
        self.status = "idle"
        self.resumed_with: int | None = None

    def start(self) -> None:
        self._act()

    def resume(self, session_id: int) -> None:
        self.resumed_with = session_id
        self._act()

    def abort(self) -> None:
        self.status = "idle"

    def _act(self) -> None:
        if self.behavior == "completes":
            self.status = "running"
            QTimer.singleShot(0, lambda: self.session_completed.emit("{}"))
        elif self.behavior == "pauses":
            self.status = "paused"
            self.status_changed.emit("paused")
        elif self.behavior == "quarantines":
            self.status = "quarantined"
            self.status_changed.emit("quarantined")
        elif self.behavior == "aborts":
            self.status = "idle"
            self.status_changed.emit("idle")
        elif self.behavior == "runs":
            self.status = "running"


class TestArgHandling:
    def test_unknown_command_refused(self, capsys):
        assert cli.cli_main(["bogus"]) == cli.EXIT_REFUSED
        assert "headless commands" in capsys.readouterr().err

    def test_tune_config_flag_needs_value(self):
        assert cli.cli_main(["tune", "--config"]) == cli.EXIT_REFUSED

    def test_resume_rejects_non_integer_id(self):
        assert cli.cli_main(["resume", "four"]) == cli.EXIT_REFUSED

    def test_resume_rejects_multiple_ids(self):
        assert cli.cli_main(["resume", "1", "2"]) == cli.EXIT_REFUSED


class TestStatus:
    def test_empty_db(self, db, capsys):
        assert cli.cmd_status(db=db) == 0
        assert "no tuner sessions" in capsys.readouterr().out

    def test_lists_sessions_with_done_counts(self, db, capsys):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0, 1]), "", "")
        tp.save_core_state(db, sid, CoreState(
            core_id=0, phase=TunerPhase.HARDENED, current_offset=-10,
            best_offset=-10, baseline_offset=0,
        ))
        tp.save_core_state(db, sid, CoreState(
            core_id=1, phase=TunerPhase.COARSE_SEARCH, current_offset=-5,
            baseline_offset=0,
        ))
        tp.update_session_status(db, sid, "paused")
        assert cli.cmd_status(db=db) == 0
        out = capsys.readouterr().out
        assert f"#{sid}" in out
        assert "paused" in out
        assert "1/2 cores done" in out


class TestRunOutcomes:
    def _run(self, db, behavior, **kw):
        made = []

        def factory(_db, _config):
            eng = FakeEngine(behavior)
            made.append(eng)
            return eng

        code = cli.cmd_run(
            kw.pop("config_path", None),
            kw.pop("resume_id", None),
            kw.pop("auto_resume", False),
            engine_factory=factory,
            db=db,
        )
        return code, (made[0] if made else None)

    def test_completed_session_exits_zero(self, db):
        code, _ = self._run(db, "completes")
        assert code == cli.EXIT_COMPLETED

    def test_engine_pause_maps_to_paused_exit(self, db):
        code, _ = self._run(db, "pauses")
        assert code == cli.EXIT_PAUSED

    def test_quarantine_maps_to_quarantined_exit(self, db):
        code, _ = self._run(db, "quarantines")
        assert code == cli.EXIT_QUARANTINED

    def test_engine_refusal_maps_to_refused_exit(self, db):
        code, _ = self._run(db, "refuses")
        assert code == cli.EXIT_REFUSED

    def test_resume_by_id_reaches_engine(self, db):
        code, eng = self._run(db, "completes", resume_id=7)
        assert code == cli.EXIT_COMPLETED
        assert eng.resumed_with == 7

    def test_auto_resume_with_no_sessions_refused(self, db):
        code, eng = self._run(db, "completes", auto_resume=True)
        assert code == cli.EXIT_REFUSED
        assert eng is None or eng.resumed_with is None

    def test_invalid_config_file_refused(self, db, tmp_path):
        bad = tmp_path / "cfg.json"
        bad.write_text('{"fine_step": 0}')
        code = cli.cmd_run(str(bad), None, False, engine_factory=lambda d, c: None, db=db)
        assert code == cli.EXIT_REFUSED

    def test_unreadable_config_refused(self, db, tmp_path):
        code = cli.cmd_run(
            str(tmp_path / "missing.json"), None, False,
            engine_factory=lambda d, c: None, db=db,
        )
        assert code == cli.EXIT_REFUSED

    def test_second_instance_locked(self, db, tmp_path):
        from PySide6.QtCore import QLockFile

        lock_dir = tmp_path / ".local" / "share" / "corecycler"
        lock_dir.mkdir(parents=True)
        held = QLockFile(str(lock_dir / "corecycler.lock"))
        assert held.tryLock(0)
        try:
            code, _ = self._run(db, "completes")
            assert code == cli.EXIT_LOCKED
        finally:
            held.unlock()


class TestCliMainDispatch:
    def test_status_dispatches(self, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "cmd_status", lambda: seen.append("s") or 0)
        assert cli.cli_main(["status"]) == 0
        assert seen == ["s"]

    def test_tune_dispatches_with_no_config(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_run", lambda **k: seen.update(k) or 0)
        assert cli.cli_main(["tune"]) == 0
        assert seen == {"config_path": None, "resume_id": None, "auto_resume": False}

    def test_tune_forwards_config_path(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_run", lambda **k: seen.update(k) or 0)
        cli.cli_main(["tune", "--config", "/tmp/x.json"])
        assert seen["config_path"] == "/tmp/x.json"

    def test_resume_by_id_dispatches(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_run", lambda **k: seen.update(k) or 0)
        cli.cli_main(["resume", "7"])
        assert seen["resume_id"] == 7 and seen["auto_resume"] is False

    def test_resume_no_id_is_auto(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(cli, "cmd_run", lambda **k: seen.update(k) or 0)
        cli.cli_main(["resume"])
        assert seen["resume_id"] is None and seen["auto_resume"] is True


class TestBuildSmu:
    def test_returns_none_when_smu_unavailable(self, monkeypatch):
        from corecycler.engine.topology import CPUTopology

        topo = CPUTopology(family=26, model=0x44, model_name="Test 9950X")
        monkeypatch.setattr(
            "corecycler.smu.driver.RyzenSMU.is_available", staticmethod(lambda *a, **k: False)
        )
        assert cli._build_smu(topo) is None


class TestCmdStatusOwnDb:
    def test_opens_and_closes_its_own_db(self, monkeypatch):
        own = HistoryDB(":memory:")
        monkeypatch.setattr("corecycler.history.db.HistoryDB", lambda *a, **k: own)
        assert cli.cmd_status() == cli.EXIT_COMPLETED


def _fake_topology():
    from corecycler.engine.topology import CPUTopology, PhysicalCore

    topo = CPUTopology(model_name="AMD Ryzen 9 9950X3D 16-Core Processor", family=26, model=0x44)
    topo.cores = {
        cid: PhysicalCore(core_id=cid, ccd=cid // 8, ccx=None, logical_cpus=(cid,))
        for cid in range(16)
    }
    return topo


class TestRunPreflightRefusals:
    def _run(self, db):
        return cli.cmd_run(None, None, False, db=db)

    def test_topology_detection_failure_refused(self, db, monkeypatch, capsys):
        monkeypatch.setattr("corecycler.engine.topology.detect_topology", lambda: None)
        assert self._run(db) == cli.EXIT_REFUSED
        assert "topology detection failed" in capsys.readouterr().err

    def test_smu_unavailable_refused(self, db, monkeypatch, capsys):
        monkeypatch.setattr("corecycler.engine.topology.detect_topology", _fake_topology)
        monkeypatch.setattr(cli, "_build_smu", lambda _t: None)
        assert self._run(db) == cli.EXIT_REFUSED
        assert "per-core SMU access is unavailable" in capsys.readouterr().err

    def test_unknown_backend_refused(self, db, monkeypatch, capsys):
        monkeypatch.setattr("corecycler.engine.topology.detect_topology", _fake_topology)
        monkeypatch.setattr(cli, "_build_smu", lambda _t: object())

        def boom(name):
            raise KeyError(name)

        monkeypatch.setattr("corecycler.engine.backends.get_backend", boom)
        assert self._run(db) == cli.EXIT_REFUSED
        assert "unknown backend" in capsys.readouterr().err

    def test_backend_not_installed_refused(self, db, monkeypatch, capsys):
        from unittest.mock import MagicMock

        monkeypatch.setattr("corecycler.engine.topology.detect_topology", _fake_topology)
        monkeypatch.setattr(cli, "_build_smu", lambda _t: object())
        from corecycler.config.tools import Resolution

        backend = MagicMock()
        backend.is_available.return_value = False
        backend.resolution.return_value = Resolution("mprime", None, "absent", "not found on PATH")
        monkeypatch.setattr("corecycler.engine.backends.get_backend", lambda _n: backend)
        assert self._run(db) == cli.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "not found on PATH" in err
        assert "CORECYCLER_MPRIME_BIN" in err

    def test_build_smu_returns_none_on_unsupported_cpu(self):
        from corecycler.engine.topology import CPUTopology

        assert cli._build_smu(CPUTopology(model_name="Intel", family=6, model=1)) is None

    def test_build_smu_constructs_driver_when_available(self, monkeypatch):
        from corecycler.smu import driver as drv

        monkeypatch.setattr(drv.RyzenSMU, "is_available", staticmethod(lambda *a, **k: True))
        assert cli._build_smu(_fake_topology()) is not None


class TestRunStatusAndSignal:
    def test_idle_status_maps_to_engine_aborted(self, db):
        code = cli.cmd_run(None, None, False, engine_factory=lambda *_: FakeEngine("aborts"), db=db)
        assert code == cli.EXIT_ENGINE_ABORTED

    def test_signal_handler_aborts_with_signal_exit(self, db, monkeypatch):
        import signal as signal_mod

        captured: dict = {}
        real = signal_mod.signal

        def fake_signal(sig, handler):
            captured[sig] = handler
            return real(sig, handler)

        monkeypatch.setattr(signal_mod, "signal", fake_signal)
        made = []

        def factory(_db, _config):
            eng = FakeEngine("runs")
            made.append(eng)
            QTimer.singleShot(10, lambda: captured[signal_mod.SIGINT](signal_mod.SIGINT, None))
            return eng

        code = cli.cmd_run(None, None, False, engine_factory=factory, db=db)
        assert code == cli.EXIT_SIGNAL
        assert made[0].status == "idle"

    def test_auto_resume_falls_back_to_first_resumable(self, db):
        sid = tp.create_session(db, TunerConfig(cores_to_test=[0]), "", "")
        tp.update_session_status(db, sid, "paused")
        made = []

        def factory(_db, _config):
            eng = FakeEngine("completes")
            made.append(eng)
            return eng

        code = cli.cmd_run(None, None, True, engine_factory=factory, db=db)
        assert code == cli.EXIT_COMPLETED
        assert made[0].resumed_with == sid


class TestRunEngineConstruction:
    def test_db_constructed_when_not_injected(self, monkeypatch):
        own = HistoryDB(":memory:")
        monkeypatch.setattr("corecycler.history.db.HistoryDB", lambda *a, **k: own)
        code = cli.cmd_run(
            None, None, False, engine_factory=lambda *_: FakeEngine("completes"), db=None
        )
        assert code == cli.EXIT_COMPLETED

    def test_real_engine_built_when_preflight_passes(self, db, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.setattr("corecycler.engine.topology.detect_topology", _fake_topology)
        monkeypatch.setattr(cli, "_build_smu", lambda _t: object())
        backend = MagicMock()
        backend.is_available.return_value = True
        monkeypatch.setattr("corecycler.engine.backends.get_backend", lambda _n: backend)
        built = []

        def fake_engine(**kw):
            built.append(kw)
            return FakeEngine("completes")

        monkeypatch.setattr("corecycler.tuner.engine.TunerEngine", fake_engine)
        assert cli.cmd_run(None, None, False, db=db) == cli.EXIT_COMPLETED
        assert built and built[0]["backend"] is backend


class TestNotifyOutcome:
    def test_unknown_code_is_silent(self, capsys):
        cli._notify_outcome(999)
        assert capsys.readouterr().err == ""

    def test_disabled_by_setting(self, monkeypatch, capsys):
        from types import SimpleNamespace


        monkeypatch.setattr(
            cli, "load_settings", lambda: SimpleNamespace(notify_on_completion=False)
        )
        cli._notify_outcome(cli.EXIT_COMPLETED)
        assert capsys.readouterr().err == ""

    def test_failure_surfaces_on_stderr(self, monkeypatch, capsys):

        def boom():
            raise RuntimeError("no settings")

        monkeypatch.setattr(cli, "load_settings", boom)
        cli._notify_outcome(cli.EXIT_COMPLETED)
        assert "notification failed" in capsys.readouterr().err


class TestDoctor:
    def _resolutions(self, present):
        from corecycler.config import tools

        return [
            tools.Resolution(
                key,
                Path(f"/usr/bin/{key}") if key in present else None,
                tools.ORIGIN_PATH if key in present else tools.ORIGIN_ABSENT,
                None if key in present else "not found on PATH",
            )
            for key in tools.TOOLS
        ]

    def test_dispatches(self, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "cmd_doctor", lambda: seen.append("d") or 0)
        assert cli.cli_main(["doctor"]) == 0
        assert seen == ["d"]

    def test_report_groups_tools_and_names_where_each_resolved(self):
        lines = cli.doctor_lines(self._resolutions({"stress-ng", "setpriv"}), [])
        assert "backend" in lines
        assert "core" in lines
        assert "optional" in lines
        assert any("stress-ng" in ln and "/usr/bin/stress-ng" in ln for ln in lines)
        assert any("mprime" in ln and "not found on PATH" in ln for ln in lines)
        assert lines[-1] == "doctor: ok"

    def test_report_lists_a_candidate_for_an_absent_tool(
        self, exec_tmp_path, tool_search_roots
    ):
        binary = exec_tmp_path / "y-cruncher" / "y-cruncher"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        tool_search_roots.append(exec_tmp_path)
        lines = cli.doctor_lines(self._resolutions({"setpriv"}), [])
        assert f"    candidate: {binary}" in lines

    def test_report_ends_in_the_unmet_requirements(self):
        lines = cli.doctor_lines(self._resolutions(set()), ["setpriv is required"])
        assert lines[-1] == "doctor: FAILED -- setpriv is required"

    def test_root_is_told_that_sudo_scrubbed_the_path(self, monkeypatch):
        from corecycler.config import tools

        monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
        assert tools.SUDO_PATH_NOTE in cli.doctor_lines(self._resolutions({"setpriv"}), [])

    def test_a_usable_system_exits_zero(self, monkeypatch, capsys):
        monkeypatch.setattr(
            cli.tools,
            "report",
            lambda: self._resolutions({"stress-ng", "systemd-run", "setpriv"}),
        )
        assert cli.cmd_doctor() == cli.EXIT_COMPLETED
        assert "doctor: ok" in capsys.readouterr().out

    def test_a_system_without_a_backend_is_refused(self, monkeypatch, capsys):
        monkeypatch.setattr(cli.tools, "report", lambda: self._resolutions({"setpriv"}))
        assert cli.cmd_doctor() == cli.EXIT_REFUSED
        assert "doctor: FAILED" in capsys.readouterr().out
