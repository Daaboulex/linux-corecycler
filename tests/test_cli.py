"""Headless CLI: argument handling, forwarding to the tuner driver, status and doctor."""

from __future__ import annotations

from pathlib import Path

import pytest

from corecycler import cli
from corecycler.history.db import HistoryDB
from tests._old_rows import seed_old_session


@pytest.fixture
def db():
    d = HistoryDB(":memory:")
    yield d
    d.close()


@pytest.fixture
def driven(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "drive", lambda argv: calls.append(argv) or 42)
    return calls


def machine_session(db, session_id, status):
    db.save_machine_session(
        session_id,
        status=status,
        config_json="{}",
        machine_json="{}",
        boot_id="b",
        bios_version="bios-1",
        cpu_model="cpu-1",
        context_id=None,
    )


class TestArgHandling:
    def test_unknown_command_refused(self, capsys):
        assert cli.cli_main(["bogus"]) == cli.EXIT_REFUSED
        assert "headless commands" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["doctor", "status", "tune", "resume", "validate", "continue", "restart"])
    def test_the_binary_hands_every_command_to_the_cli(self, monkeypatch, command):
        from corecycler import main as entry

        seen = []
        monkeypatch.setattr(entry, "setup_logging", lambda: None)
        monkeypatch.setattr(entry, "_bootstrap_sudo_session", lambda: pytest.fail(f"{command} reached the GUI"))
        monkeypatch.setattr(entry.sys, "argv", ["corecycler", command, "x"])
        monkeypatch.setattr(cli, "cli_main", lambda argv: seen.append(argv) or 9)
        assert entry.main() == 9
        assert seen == [[command, "x"]]

    @pytest.mark.parametrize("command", cli.SESSION_COMMANDS)
    @pytest.mark.parametrize("rest", [[], ["four"], ["--db", "x.db"], ["-1"]])
    def test_a_session_command_needs_an_id_first(self, command, rest, driven, capsys):
        assert cli.cli_main([command, *rest]) == cli.EXIT_REFUSED
        assert "needs a SESSION_ID first" in capsys.readouterr().err
        assert driven == []


class TestForwarding:
    @pytest.mark.req("E6.cli")
    def test_tune_starts_the_driver_with_writes_on(self, driven):
        assert cli.cli_main(["tune", "--config", "c.json", "--acknowledge"]) == 42
        assert driven == [["--start", "--write", "--config", "c.json", "--acknowledge"]]

    @pytest.mark.parametrize("command", cli.SESSION_COMMANDS)
    def test_a_session_command_names_its_session_and_keeps_the_rest(self, command, driven):
        assert cli.cli_main([command, "12", "--db", "x.db"]) == 42
        assert driven == [[f"--{command}", "--session", "12", "--write", "--db", "x.db"]]

    def test_drive_runs_the_live_driver(self, monkeypatch):
        from corecycler.tuner import live_hw

        seen = []
        monkeypatch.setattr(live_hw, "main", lambda argv: seen.append(argv) or 7)
        assert cli.drive(["--resume", "--session", "1"]) == 7
        assert seen == [["--resume", "--session", "1"]]


class TestStatus:
    def test_empty_db(self, db, capsys):
        assert cli.cmd_status(db) == cli.EXIT_COMPLETED
        assert capsys.readouterr().out == "no tuning sessions\n"

    def test_lists_machine_sessions_newest_first_with_link_and_last_event(self, db, capsys):
        machine_session(db, 1, "superseded")
        machine_session(db, 2, "paused")
        db.link_session(2, 1, "continue")
        db.insert_tuner_event(1, "started")
        db.insert_tuner_event(1, "superseded by session 2 (continue)")
        assert cli.cmd_status(db) == cli.EXIT_COMPLETED
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("   2  paused ")
        assert lines[0].endswith("continue of 1  ")
        assert lines[1].startswith("   1  superseded ")
        assert lines[1].endswith("  superseded by session 2 (continue)")

    def test_old_engine_sessions_are_not_listed(self, db, capsys):
        seed_old_session(db)
        assert cli.cmd_status(db) == cli.EXIT_COMPLETED
        assert capsys.readouterr().out == "no tuning sessions\n"

    def test_opens_and_closes_its_own_db(self, monkeypatch):
        closed = []

        class Own(HistoryDB):
            def close(self):
                closed.append(True)
                super().close()

        monkeypatch.setattr("corecycler.history.db.HistoryDB", lambda: Own(":memory:"))
        assert cli.cli_main(["status"]) == cli.EXIT_COMPLETED
        assert closed == [True]


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

    def test_report_lists_a_candidate_for_an_absent_tool(self, exec_tmp_path, tool_search_roots):
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


class ShaperSmu:
    def __init__(self, problem=None):
        self.problem, self.written = problem, []

    def set_curve_shaper(self, bands):
        if any(len(band) != 3 for band in bands):
            raise ValueError("a Curve Shaper is five bands of three margins within -50..30")
        self.written.append(bands)
        return self.problem


@pytest.fixture
def shaper(monkeypatch):
    from contextlib import contextmanager

    from corecycler.engine import topology
    from corecycler.smu import writer_lock
    from corecycler.tuner import live_hw

    state = {"smu": ShaperSmu(), "busy": False}

    @contextmanager
    def lock():
        if state["busy"]:
            raise writer_lock.WriterBusy("another CO writer holds tuner.lock")
        yield

    monkeypatch.setattr(topology, "detect_topology", lambda: "topology")
    monkeypatch.setattr(live_hw, "build_smu", lambda topo: state["smu"])
    monkeypatch.setattr(live_hw, "shaper_fact", lambda smu: f"table after {len(smu.written)} writes")
    monkeypatch.setattr(writer_lock, "co_writer", lock)
    return state


ZERO = ["0/0/0"] * 5


@pytest.mark.parametrize(
    ("argv", "busy", "problem", "code", "written"),
    [
        ([], False, None, cli.EXIT_COMPLETED, []),
        (["--set", *ZERO], False, None, cli.EXIT_COMPLETED, [((0, 0, 0),) * 5]),
        (["--set", *ZERO], True, None, cli.EXIT_LOCKED, []),
        (["--set", *ZERO], False, "the SMU refused band Min", cli.EXIT_REFUSED, [((0, 0, 0),) * 5]),
        (["--set", *ZERO[:4]], False, None, cli.EXIT_REFUSED, []),
        (["--set", "a/0/0", *ZERO[1:]], False, None, cli.EXIT_REFUSED, []),
        (["--set", "0/0", *ZERO[1:]], False, None, cli.EXIT_REFUSED, []),
    ],
)
def test_the_shaper_command_prints_or_sets_the_table_and_refuses_what_it_cannot_do(
    shaper, capsys, argv, busy, problem, code, written
):
    shaper["busy"], shaper["smu"].problem = busy, problem
    assert cli.cli_main(["shaper", *argv]) == code
    assert shaper["smu"].written == written
    out = capsys.readouterr()
    if code == cli.EXIT_COMPLETED:
        assert out.out == f"table after {len(written)} writes\n"
    else:
        assert out.err.startswith("corecycler shaper: ")


def test_the_shaper_command_refuses_without_an_smu(shaper, capsys):
    shaper["smu"] = None
    assert cli.cli_main(["shaper"]) == cli.EXIT_REFUSED
    assert "the SMU or its core map is unavailable" in capsys.readouterr().err
