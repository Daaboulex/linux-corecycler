"""Headless commands: tune, resume and inspect sessions without a display."""

from __future__ import annotations

import os
import sys
import textwrap
from typing import TYPE_CHECKING

from corecycler.config import tools

if TYPE_CHECKING:
    from corecycler.config.tools import Resolution
    from corecycler.history.db import HistoryDB
    from corecycler.tuner.machine.types import CoreRecord

EXIT_COMPLETED = 0
EXIT_PAUSED = 3
EXIT_QUARANTINED = 4
EXIT_REFUSED = 5
EXIT_ABORTED = 6
EXIT_LOCKED = 7
EXIT_SIGNAL = 130

EXIT_MEANINGS = {
    EXIT_COMPLETED: "completed",
    EXIT_PAUSED: "paused (needs attention)",
    EXIT_QUARANTINED: "quarantined",
    EXIT_REFUSED: "refused (bad config or environment)",
    EXIT_ABORTED: "aborted",
    EXIT_LOCKED: "another CO writer holds the lock",
    EXIT_SIGNAL: "stopped by a signal; every owned core is sent back to its baseline",
}

SESSION_COMMANDS = ("resume", "validate", "continue", "restart", "abort")
COMMANDS = ("doctor", "status", "shaper", "margins", "tune", *SESSION_COMMANDS)

USAGE = f"""\
corecycler headless commands:

  corecycler doctor                report every external tool and where it resolved
  corecycler status                list tuning sessions and their state
  corecycler shaper [--set BANDS]  print the Curve Shaper, or set its five bands Min Low
                                   Medium High Max, each -5C/50C/90C like -30/-27/-12;
                                   volatile, refused while a tuning session runs
  corecycler margins SESSION_ID    per core and 100 MHz of its clock, the deepest offset
                                   that passed above the shallowest that failed in the
                                   session's single-core tests; refused for a session
                                   that ran with a Curve Shaper other than 0
  corecycler tune OPTIONS          start a new tuning session from --config FILE, a whole
                                   configuration as JSON, or from --cores 0,1,... with
                                   --max-offset N, --k1 N, --k2 N and --wait-between-cores S
                                   where the defaults do not fit
  corecycler resume SESSION_ID     resume a paused, interrupted or aborted session; a
                                   quarantined one only with --reengage, each core from
                                   its last proven value or from stock
  corecycler resume --interrupted  resume the one session a crash left running, from the
                                   default database; none is no failure, two or more
                                   are refused; takes no other argument
  corecycler validate SESSION_ID   prove a completed session's values again
  corecycler continue SESSION_ID   carry a session paused on a changed setup into a new one
  corecycler restart SESSION_ID    start a session paused on a changed setup over
  corecycler abort SESSION_ID      stop a held session without a driver as aborted: its
                                   last boot is attributed first, then every owned core
                                   goes back to its baseline; no test runs

tune and restart take --acknowledge to start over a CO the BIOS already sets.
Every tuning command but resume --interrupted takes --db FILE and
--state-dir DIR; one runs at a time.

{textwrap.fill("Exit codes: " + ", ".join(f"{code} {meaning}" for code, meaning in EXIT_MEANINGS.items()) + ".", 72)}

Running the binary with no command opens the GUI.
"""


def cli_main(argv: list[str]) -> int:
    command, rest = argv[0], argv[1:]
    if command == "doctor":
        return cmd_doctor()
    if command == "status":
        return cmd_status()
    if command == "shaper":
        return cmd_shaper(rest)
    if command == "margins":
        return cmd_margins(rest)
    if command == "tune":
        return drive(["--start", "--write", *rest])
    if command in SESSION_COMMANDS:
        return cmd_session(command, rest)
    print(USAGE, file=sys.stderr)
    return EXIT_REFUSED


def drive(argv: list[str]) -> int:
    from corecycler.tuner.live_hw import main

    return main(argv)


def cmd_shaper(rest: list[str]) -> int:
    from corecycler.engine.topology import detect_topology
    from corecycler.smu.writer_lock import WriterBusy, co_writer
    from corecycler.tuner.live_hw import build_smu, shaper_fact

    if rest and (rest[0] != "--set" or len(rest) != 6):
        print("corecycler shaper: --set takes five bands, Min Low Medium High Max, like 0/0/0", file=sys.stderr)
        return EXIT_REFUSED
    smu = build_smu(detect_topology())
    if smu is None:
        print("corecycler shaper: the SMU or its core map is unavailable", file=sys.stderr)
        return EXIT_REFUSED
    if rest:
        try:
            bands = tuple(tuple(int(margin) for margin in band.split("/")) for band in rest[1:])
            with co_writer():
                problem = smu.set_curve_shaper(bands)
        except WriterBusy as e:
            print(f"corecycler shaper: {e}", file=sys.stderr)
            return EXIT_LOCKED
        except ValueError as e:
            print(f"corecycler shaper: {e}", file=sys.stderr)
            return EXIT_REFUSED
        if problem is not None:
            print(f"corecycler shaper: {problem}", file=sys.stderr)
            return EXIT_REFUSED
    print(shaper_fact(smu))
    return EXIT_COMPLETED


def cmd_margins(rest: list[str], db: HistoryDB | None = None) -> int:
    from corecycler.history.db import HistoryDB
    from corecycler.tuner.live import margins, read_stored
    from corecycler.tuner.live_hw import shaper_at_zero

    if len(rest) != 1 or not rest[0].isdigit():
        print("corecycler margins: needs one SESSION_ID; corecycler status lists them", file=sys.stderr)
        return EXIT_REFUSED
    session_id = int(rest[0])
    own = HistoryDB() if db is None else db
    try:
        try:
            stored = read_stored(own, session_id)
        except ValueError as e:
            print(f"corecycler margins: {e}", file=sys.stderr)
            return EXIT_REFUSED
        shaper = dict(stored.setup).get("curve_shaper")
        if not shaper_at_zero(shaper):
            print(
                f"corecycler margins: session {session_id} ran with the Curve Shaper {shaper or 'not recorded'},"
                " so its offsets are not the whole offset and no margin is read from them",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        found = margins(own.list_tuner_tests(session_id))
    finally:
        if db is None:
            own.close()
    results = {record.core: record for record in stored.cores}
    print("\n".join(margin_lines(found, results)) if found else f"no single-core tests in session {session_id}")
    return EXIT_COMPLETED


MARGIN_SHOWN_S = 1.0


def margin_lines(found: dict[int, dict], results: dict[int, CoreRecord]) -> list[str]:
    from corecycler.tuner.live import MHZ_BIN

    lines = []
    for core, record in found.items():
        lines.append(f"core {core}")
        result = results.get(core)
        if result is not None:
            bound = "none" if result.fb is None else result.fb
            lines.append(
                f"  the session holds {result.value}, failure bound {bound}, crashes attributed {result.crash_count}"
            )
        shown = {mhz: cell for mhz, cell in record["bins"].items() if cell["seconds"] >= MARGIN_SHOWN_S}
        for mhz, cell in shown.items():
            passed = (
                "nothing passed above the failure"
                if cell["passed"] is None
                else f"passed {cell['passed']} for {cell['passed_s']} s"
            )
            failed = "no failure" if cell["failed"] is None else f"failed {cell['failed']} for {cell['failed_s']} s"
            deeper = ", ".join(map(str, cell["deeper"]))
            low, high = cell["temp"]
            lines.append(
                f"  {mhz}-{mhz + MHZ_BIN} MHz at {low}-{high} C: {passed}, {failed}"
                + (f"; also passed at or below the failure: {deeper}" if deeper else "")
            )
        if len(shown) < len(record["bins"]):
            lines.append(f"  clock bins under {MARGIN_SHOWN_S} s each, not shown: {len(record['bins']) - len(shown)}")
        if record["open"]:
            lines.append(f"  no verdict (a hang, or a test still running) at {', '.join(map(str, record['open']))}")
        if record["unrecorded"]:
            lines.append(f"  tests without a coverage record: {record['unrecorded']}")
    return lines


def cmd_session(command: str, rest: list[str]) -> int:
    if command == "resume" and rest[:1] == ["--interrupted"]:
        return cmd_resume_interrupted(rest[1:])
    if not rest or not rest[0].isdigit():
        print(f"corecycler {command}: needs a SESSION_ID first; corecycler status lists them", file=sys.stderr)
        return EXIT_REFUSED
    return drive([f"--{command}", "--session", rest[0], "--write", *rest[1:]])


def cmd_resume_interrupted(rest: list[str], db: HistoryDB | None = None) -> int:
    from corecycler.history.db import HistoryDB

    if rest:
        print(f"corecycler resume: --interrupted takes no other argument: {' '.join(rest)}", file=sys.stderr)
        return EXIT_REFUSED
    own = HistoryDB() if db is None else db
    try:
        sessions = own.list_machine_sessions()
    finally:
        if db is None:
            own.close()
    interrupted = [str(s.id) for s in sessions if s.status == "running"]
    if not interrupted:
        print("corecycler resume: no interrupted session")
        return EXIT_COMPLETED
    if len(interrupted) > 1:
        print(
            f"corecycler resume: sessions {', '.join(interrupted)} were all interrupted; resume one by hand",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    return drive(["--resume", "--session", interrupted[0], "--write"])


def cmd_status(db: HistoryDB | None = None) -> int:
    from corecycler.history.db import HistoryDB

    own = HistoryDB() if db is None else db
    try:
        sessions = own.list_machine_sessions()
    finally:
        if db is None:
            own.close()
    if not sessions:
        print("no tuning sessions")
        return EXIT_COMPLETED
    for s in sessions:
        link = f"{s.origin} of {s.parent_session_id}  " if s.parent_session_id is not None else ""
        print(f"{s.id:>4}  {s.status:<11} {s.updated_at}  {link}{s.last_event or ''}")
    return EXIT_COMPLETED


def doctor_lines(resolutions: list[Resolution], unmet: list[str]) -> list[str]:
    """The dependency report, one tool per line, grouped by how much it matters."""
    width = max(len(r.key) for r in resolutions)
    lines = ["corecycler doctor", ""]
    for kind in (tools.BACKEND, tools.CORE, tools.OPTIONAL):
        lines.append(kind)
        for resolution in [r for r in resolutions if tools.TOOLS[r.key].kind == kind]:
            detail = str(resolution.path) if resolution.path else resolution.problem
            lines.append(f"  {resolution.key:<{width}}  {resolution.origin:<7} {detail}")
            if resolution.path is None:
                lines += [f"    candidate: {c}" for c in tools.discover(resolution.key)]
        lines.append("")
    if os.geteuid() == 0:
        lines += [tools.SUDO_PATH_NOTE, ""]
    lines.append("Pin a path with CORECYCLER_<TOOL>_BIN, or in the GUI when a backend is missing.")
    lines.append("")
    lines += [f"doctor: FAILED -- {problem}" for problem in unmet] or ["doctor: ok"]
    return lines


def cmd_doctor() -> int:
    tools.load_configured_paths()
    resolutions = tools.report()
    unmet = tools.unmet_requirements(resolutions)
    for line in doctor_lines(resolutions, unmet):
        print(line)
    return EXIT_REFUSED if unmet else EXIT_COMPLETED
