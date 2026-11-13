"""Headless CLI: argument handling, forwarding to the tuner driver, status and doctor."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from corecycler import cli
from corecycler.history.db import HistoryDB
from tests._machine import this_boot
from tests._old_rows import seed_old_session
from tests._tuner_world import save_machine_session


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

    @pytest.mark.parametrize(
        "command",
        ["doctor", "status", "margins", "tune", "resume", "validate", "continue", "restart", "abort", "hang-watch"],
    )
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


def sessions(db, statuses):
    for number, status in enumerate(statuses, start=1):
        machine_session(db, number, status)


class TestResumeInterrupted:
    @pytest.mark.req("INV.L28", "L28.a", "L28.d")
    def test_the_one_interrupted_session_resumes_with_writes_on_and_the_database_is_closed(self, monkeypatch, driven):
        closed = []

        class Own(HistoryDB):
            def close(self):
                closed.append(True)
                super().close()

        own = Own(":memory:")
        sessions(own, ["completed", "running", "paused", "quarantined", "aborted"])
        monkeypatch.setattr("corecycler.history.db.HistoryDB", lambda: own)
        assert cli.cli_main(["resume", "--interrupted"]) == 42
        assert driven == [["--resume", "--session", "2", "--write"]]
        assert closed == [True]

    @pytest.mark.req("INV.L28", "L28.d")
    @pytest.mark.parametrize("statuses", [[], ["paused"], ["completed", "quarantined", "aborted"]])
    def test_without_an_interrupted_session_nothing_resumes_and_the_boot_is_no_failure(
        self, db, driven, capsys, statuses
    ):
        sessions(db, statuses)
        assert cli.cmd_resume_interrupted([], db) == cli.EXIT_COMPLETED
        assert driven == []
        assert capsys.readouterr().out == "corecycler resume: no interrupted session\n"

    @pytest.mark.req("INV.L28", "L28.d")
    def test_two_interrupted_sessions_are_refused_by_name_and_neither_resumes(self, db, driven, capsys):
        sessions(db, ["running", "paused", "running"])
        assert cli.cmd_resume_interrupted([], db) == cli.EXIT_REFUSED
        assert driven == []
        err = capsys.readouterr().err
        assert {int(number) for number in re.findall(r"\b\d+\b", err)} == {1, 3}
        assert "resume one by hand" in err

    @pytest.mark.req("L28.d")
    @pytest.mark.parametrize("extra", [["3"], ["--db", "x.db"]])
    def test_anything_after_interrupted_is_refused_before_the_database_is_opened(
        self, monkeypatch, driven, capsys, extra
    ):
        monkeypatch.setattr("corecycler.history.db.HistoryDB", lambda: pytest.fail("the database was opened"))
        assert cli.cli_main(["resume", "--interrupted", *extra]) == cli.EXIT_REFUSED
        assert driven == []
        err = capsys.readouterr().err
        assert "--interrupted" in err
        assert " ".join(extra) in err

    @pytest.mark.req("L28.d")
    def test_the_boot_unit_retries_exactly_on_the_refused_and_locked_exits(self):
        module = (Path(__file__).parents[1] / "nix" / "module.nix").read_text()
        retried = re.findall(r'RestartForceExitStatus = "([0-9 ]+)";', module)
        assert [sorted(int(code) for code in codes.split()) for codes in retried] == [
            sorted([cli.EXIT_REFUSED, cli.EXIT_LOCKED])
        ]

    @pytest.mark.req("T23.history.uncapped")
    def test_an_interrupted_session_older_than_the_newest_fifty_resumes(self, db, driven):
        sessions(db, ["running"] + ["completed"] * 50)
        assert cli.cmd_resume_interrupted([], db) == 42
        assert driven == [["--resume", "--session", "1", "--write"]]


ZERO_SHAPER = "Min 0/0/0, Low 0/0/0, Medium 0/0/0, High 0/0/0, Max 0/0/0"


def session_with_shaper(db, root, shaper):
    from corecycler.tuner.live import Identity, Store

    saved = save_machine_session(db, root, "paused")
    setup = () if shaper is None else (("curve_shaper", shaper),)
    convicted = replace(saved.cores[0], value=-29, fb=-32, crash_count=1)
    recorded = replace(saved, setup=setup, cores=(convicted, *saved.cores[1:]))
    Store(db, root / "state", Identity("bios-1", "cpu-1", None)).save(recorded, this_boot())
    return saved.session_id, convicted.core


NO_EVIDENCE = json.dumps({"records": [], "unblamed_corrected": False})


def ran(db, session_id, value, verdict, cells, core=9):
    row = db.insert_tuner_test(session_id, "b", 1, "search", "1t", "1t", core, value, "[]", f"[{core}]", 20.0, "{}")
    if verdict is not None:
        coverage = None if cells is None else json.dumps({str(core): {"cells": cells, "unreadable_s": 0.0}})
        db.end_tuner_test(row, verdict, 20.0, "[]", None, NO_EVIDENCE, "", "[]", "", coverage_json=coverage)
    return row


class TestMargins:
    @pytest.mark.req("A2.margins")
    @pytest.mark.parametrize("shaper", [ZERO_SHAPER, "none on this generation"])
    def test_prints_each_core_s_margin_per_clock_when_the_session_ran_with_no_shaper_offset(
        self, db, tmp_path, capsys, shaper
    ):
        session_id, convicted = session_with_shaper(db, tmp_path, shaper)
        ran(db, session_id, -30, "pass", [[5400, 55, 20.0], [5800, 45, 1.0], [5900, 45, 0.5]])
        ran(db, session_id, -31, "silicon", [[5400, 60, 4.5], [5700, 45, 0.9]])
        ran(db, session_id, -35, "pass", [[5400, 55, 20.0]])
        ran(db, session_id, -33, None, None)
        ran(db, session_id, -29, "pass", None)
        ran(db, session_id, -33, "silicon", [[4800, 50, 3.0]], core=convicted)
        assert cli.cmd_margins([str(session_id)], db) == cli.EXIT_COMPLETED
        assert capsys.readouterr().out.splitlines() == [
            f"core {convicted}",
            "  the session holds -29, failure bound -32, crashes attributed 1",
            "  4800-4900 MHz at 50-55 C: nothing passed above the failure, failed -33 for 3.0 s",
            "core 9",
            "  5400-5500 MHz at 55-65 C: passed -30 for 20.0 s, failed -31 for 4.5 s;"
            " also passed at or below the failure: -35",
            "  5800-5900 MHz at 45-50 C: passed -30 for 1.0 s, no failure",
            "  clock bins under 1.0 s each, not shown: 2",
            "  no verdict (a hang, or a test still running) at -33",
            "  tests without a coverage record: 1",
        ]

    @pytest.mark.req("A2.margins")
    @pytest.mark.parametrize(
        ("shaper", "said"),
        [
            ("Min 0/0/0, Low -30/-27/-12, Medium 0/0/0, High 0/0/0, Max 0/0/0", "Min 0/0/0, Low -30/-27/-12"),
            ("unreadable: the SMU refused the read or its band check failed", "unreadable: the SMU refused"),
            (None, "not recorded"),
        ],
    )
    def test_a_session_run_with_any_other_shaper_is_refused(self, db, tmp_path, capsys, shaper, said):
        session_id, _ = session_with_shaper(db, tmp_path, shaper)
        ran(db, session_id, -30, "pass", [[5400, 55, 20.0]])
        assert cli.cmd_margins([str(session_id)], db) == cli.EXIT_REFUSED
        out = capsys.readouterr()
        assert f"ran with the Curve Shaper {said}" in out.err
        assert out.out == ""

    def test_a_session_with_no_single_core_test_says_so(self, db, tmp_path, capsys):
        session_id, _ = session_with_shaper(db, tmp_path, ZERO_SHAPER)
        assert cli.cmd_margins([str(session_id)], db) == cli.EXIT_COMPLETED
        assert capsys.readouterr().out == f"no single-core tests in session {session_id}\n"

    @pytest.mark.parametrize("rest", [[], ["x"], ["1", "2"]])
    def test_needs_exactly_one_session_id(self, db, capsys, rest):
        assert cli.cmd_margins(rest, db) == cli.EXIT_REFUSED
        assert "needs one SESSION_ID" in capsys.readouterr().err

    def test_an_unknown_session_is_refused_by_name(self, db, capsys):
        assert cli.cmd_margins(["99"], db) == cli.EXIT_REFUSED
        assert "session 99 is not stored" in capsys.readouterr().err

    @pytest.mark.req("UNREAD.margins")
    def test_a_session_stored_without_its_base_co_is_refused_by_name(self, db, tmp_path, capsys):
        session_id, _ = session_with_shaper(db, tmp_path, ZERO_SHAPER)
        db._execute_raw(
            "UPDATE tuner_sessions SET machine_json = json_remove(machine_json, '$.base_co') WHERE id = ?",
            (session_id,),
        )
        assert cli.cmd_margins([str(session_id)], db) == cli.EXIT_REFUSED
        out = capsys.readouterr()
        assert out.err == f"corecycler margins: session {session_id} could not be read: KeyError('base_co')\n"
        assert out.out == ""

    @pytest.mark.req("A2.margins")
    @pytest.mark.parametrize(
        "evidence",
        [
            None,
            "not json",
            "[]",
            '{"unblamed_corrected": false}',
            '{"records": [9], "unblamed_corrected": false}',
            '{"records": [{"corrected": true}], "unblamed_corrected": false}',
            '{"records": [{"core": "9", "corrected": true}], "unblamed_corrected": false}',
            '{"records": [{"core": true, "corrected": true}], "unblamed_corrected": false}',
            '{"records": [{"core": 9, "corrected": true}, {"corrected": true}], "unblamed_corrected": false}',
            '{"records": []}',
            '{"records": [], "unblamed_corrected": 0}',
        ],
    )
    def test_a_test_with_a_malformed_evidence_json_is_refused_naming_the_test_and_the_field(
        self, db, tmp_path, capsys, evidence
    ):
        session_id, _ = session_with_shaper(db, tmp_path, ZERO_SHAPER)
        row = ran(db, session_id, -30, "pass", [[5400, 55, 20.0]])
        db._execute_raw("UPDATE tuner_tests SET evidence_json = ? WHERE id = ?", (evidence, row))
        assert cli.cmd_margins([str(session_id)], db) == cli.EXIT_REFUSED
        out = capsys.readouterr()
        assert out.err == (
            f"corecycler margins: session {session_id} could not be read: test {row} has evidence_json {evidence!r},"
            " in no shape a build of this tool writes\n"
        )
        assert out.out == ""

    def test_the_command_reads_the_default_database_and_closes_it(self, monkeypatch, capsys):
        closed = []

        class Own(HistoryDB):
            def close(self):
                closed.append(True)
                super().close()

        monkeypatch.setattr("corecycler.history.db.HistoryDB", lambda: Own(":memory:"))
        assert cli.cli_main(["margins", "7"]) == cli.EXIT_REFUSED
        assert closed == [True]


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

    @pytest.mark.req("T23.history.uncapped")
    def test_every_session_is_listed(self, db, capsys):
        for number in range(1, 52):
            machine_session(db, number, "paused")
        assert cli.cmd_status(db) == cli.EXIT_COMPLETED
        lines = capsys.readouterr().out.splitlines()
        assert [int(line.split()[0]) for line in lines] == list(range(51, 0, -1))

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
