"""Headless commands: tune, resume and inspect sessions without a display."""

from __future__ import annotations

import json
import math
import os
import sys
import textwrap
import threading
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.config import tools

if TYPE_CHECKING:
    from collections.abc import Callable

    from corecycler.config.tools import Resolution
    from corecycler.history.db import HistoryDB
    from corecycler.tuner.machine.types import CoreRecord

EXIT_COMPLETED = 0
EXIT_PAUSED = 3
EXIT_QUARANTINED = 4
EXIT_REFUSED = 5
EXIT_ABORTED = 6
EXIT_LOCKED = 7
EXIT_VALIDATION_ABORTED = 8  # equivalent-mutant: any free code below 128 reads the same through EXIT_MEANINGS
EXIT_ABORTED_UNREAD = 9  # equivalent-mutant: any free code below 128 reads the same through EXIT_MEANINGS
EXIT_SIGNAL = 130

EXIT_MEANINGS = {
    EXIT_COMPLETED: "completed",
    EXIT_PAUSED: "paused (needs attention)",
    EXIT_QUARANTINED: "quarantined",
    EXIT_REFUSED: "refused (bad config or environment)",
    EXIT_ABORTED: "aborted",
    EXIT_LOCKED: "another CO writer holds the lock",
    EXIT_VALIDATION_ABORTED: "validation aborted; the session stays completed",
    EXIT_ABORTED_UNREAD: "aborted unread; the session could not be read, and the log says what became of the CO",
    EXIT_SIGNAL: "stopped by a signal; every owned core is sent back to its baseline",
}

SESSION_COMMANDS = ("resume", "validate", "continue", "restart", "abort")
COMMANDS = ("doctor", "status", "shaper", "margins", "tune", "hang-watch", *SESSION_COMMANDS)

SYSRQ_TRIGGER = Path("/proc/sysrq-trigger")
HANG_STALE_S = 120.0
HANG_EVERY_S = 15.0
RESET_AFTER_SYNC_S = 2.0
PROBE_NAME = "hang-watch-probe"
LOG_BACKLOG = 64  # equivalent-mutant: any backlog above the few lines a check says behaves the same

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
                                   goes back to its baseline; no test runs. One that
                                   cannot be read ends aborted unread, kept as stored:
                                   every core its CO journal shows it wrote in this
                                   boot goes to stock, and when the journal cannot be
                                   read or another session wrote after it, no CO is
                                   written: reboot or check the CO before tuning again
  corecycler hang-watch            every 15 s (--every S), reset the machine through
                                   sysrq when this user's session, in a test or between
                                   tests, has written no heartbeat for more than 120 s
                                   (--stale-after S), or no write to the directory of
                                   its database has ended for that long, while its
                                   process lives and is not stopped

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
    if command == "hang-watch":
        return cmd_hang_watch(rest)
    if command in SESSION_COMMANDS:
        return cmd_session(command, rest)
    print(USAGE, file=sys.stderr)
    return EXIT_REFUSED


def drive(argv: list[str]) -> int:
    from corecycler.tuner.live_hw import main

    return main(argv)


def seconds(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(text)
    return value


def cmd_hang_watch(rest: list[str]) -> int:
    import argparse
    import time

    from corecycler.config.paths import heartbeat_path, invoking_uid
    from corecycler.smu.writer_lock import process_state

    parser = argparse.ArgumentParser(prog="corecycler hang-watch")
    parser.add_argument("--stale-after", type=seconds, default=HANG_STALE_S)
    parser.add_argument("--every", type=seconds, default=HANG_EVERY_S)
    try:
        args = parser.parse_args(rest)
    except SystemExit as refused:
        return EXIT_REFUSED if refused.code else EXIT_COMPLETED
    hang_watch(
        heartbeat_path(invoking_uid()),
        args.stale_after,
        args.every,
        clock=lambda: time.clock_gettime(time.CLOCK_MONOTONIC),
        state_of=process_state,
        sysrq=lambda key: SYSRQ_TRIGGER.write_text(key),
        sysrq_ready=lambda: os.access(SYSRQ_TRIGGER, os.W_OK),
        probe=write_probe,
        say=lambda line: print(line, file=sys.stderr),
        pause=time.sleep,
        stop=threading.Event(),
    )
    return EXIT_COMPLETED


def hang_watch(
    heartbeat: Path,
    stale_after: float,
    every: float,
    *,
    clock: Callable[[], float],
    state_of: Callable[[int], tuple[str, str] | None],
    sysrq: Callable[[str], None],
    sysrq_ready: Callable[[], bool],
    probe: Callable[[Path], None],
    say: Callable[[str], None],
    pause: Callable[[float], None],
    stop: threading.Event,
) -> None:
    """Read the heartbeat every ``every`` seconds until ``stop`` and, while a session drives, ``probe`` the directory
    it names through a DiskProbe. Only two checks at least ``every`` apart that find the same beat stale, or the same
    last answer of the disk more than ``stale_after`` seconds old, its process live and not stopped on both, prove a
    hang: one check can land in the seconds a stopped driver needs, once it runs again, to beat. Every line goes
    through a Log, which gets the time between checks to write and no more, so a ``say`` that blocks holds neither a
    check nor the reset. A log that failed ends the watch, for systemd to start it again, only at the end of a check
    that suspects no hang and probes no disk, so a restart never loses a hang being proven or a silence being timed."""
    log = Log(say)
    disk = DiskProbe(probe, clock)
    said = None
    was_ready = True
    suspect: tuple[str, float, float] | None = None
    while True:
        ready = sysrq_ready()
        if was_ready and not ready:
            log(
                f"{SYSRQ_TRIGGER} is not writable for this user, so a hung session would not be reset: "
                "hangReset grants it to the corecycler group"
            )
        was_ready = ready
        now = clock()
        reason, stale_at, directory = hang_reason(heartbeat, stale_after, now, state_of)
        evidence = None if stale_at is None else ("its last beat", stale_at)
        answer = disk.check(directory, now)
        if answer is not None:
            what, answered, error = answer
            if error is not None:
                reason = f"{reason}; writing to {directory} failed: {error}"
            if evidence is None and now - answered > stale_after:
                evidence = (what, answered)
                reason = f"the disk stopped answering: no write to {directory} ended while {reason}"
        if evidence is None:
            suspect = None
        elif suspect is None or suspect[:2] != evidence:
            suspect = (*evidence, now)
        proven = suspect is not None and now - suspect[2] >= every
        if proven and ready:
            what, since, _ = suspect
            line = f"resetting the machine: {reason}, {what} {now - since:.0f} s ago"
            reset(line, sysrq=sysrq, log=log, clock=clock, pause=pause)
        else:
            if proven:
                reason = f"{reason}: not reset, {SYSRQ_TRIGGER} is not writable"
            if reason != said:
                log(reason)
        said = reason
        log.settle(every)
        if log.failed is not None and suspect is None and directory is None:
            raise log.failed
        if stop.wait(max(0.0, every - (clock() - now))):
            return


def hang_reason(
    heartbeat: Path, stale_after: float, now: float, state_of: Callable[[int], tuple[str, str] | None]
) -> tuple[str, float | None, Path | None]:
    """Why the heartbeat shows a hang or does not, the time of its beat when that shows one, and the directory to
    probe while a session drives. A session drives while its heartbeat says running or between and the very process
    that wrote it lives, not stopped; only such a beat more than ``stale_after`` seconds behind ``now`` shows a hang.
    A beat that names no directory, written before the driver named one, is watched without a probe."""
    try:
        beat = json.loads(heartbeat.read_bytes())
    except FileNotFoundError:
        return f"no heartbeat at {heartbeat}: no session is watched", None, None
    except OSError as exc:
        return f"the heartbeat {heartbeat} cannot be read ({exc.strerror}): no session is watched", None, None
    except ValueError:
        return f"the heartbeat {heartbeat} is not JSON: no session is watched", None, None
    malformed = f"the heartbeat {heartbeat} is malformed: no session is watched", None, None
    match beat:
        case {"state": "idle"}:
            return "no session drives", None, None
        case {"state": "running", "test": str(test)}:
            who = test
        case {"state": "between"}:
            who = "the session between tests"
        case _:
            return malformed
    pid, started, at, probe = (beat.get(field) for field in ("pid", "started", "at", "probe_dir"))
    if not (
        type(pid) is int
        and isinstance(started, str)
        and type(at) is float
        and math.isfinite(at)
        and (probe is None or isinstance(probe, str) and os.path.isabs(probe))
    ):
        return malformed
    found = state_of(pid)
    if found is None or found[1] != started or found[0] in ("Z", "X"):
        return f"pid {pid} of {who} is gone or reused, so its heartbeat proves no hang", None, None
    if found[0] in ("T", "t"):
        return f"pid {pid} of {who} is stopped, so its heartbeat proves no hang", None, None
    directory = None if probe is None else Path(probe)
    if now - at <= stale_after:
        return f"{who} runs in pid {pid}", None, directory
    return f"{who} in pid {pid} stopped beating while its process lives", at, directory


def reset(
    line: str,
    *,
    sysrq: Callable[[str], None],
    log: Log,
    clock: Callable[[], float],
    pause: Callable[[float], None],
) -> None:
    """Sync, say why at critical priority so journald syncs the line, and reset RESET_AFTER_SYNC_S after the sync,
    never later: the line waits in the log at most that long, so a log stuck on a disk, one shape of a hang, cannot
    hold the reset, and a log that fails holds nothing."""
    sysrq("s")
    due = clock() + RESET_AFTER_SYNC_S
    try:
        log(f"<2>{line}")
        log.settle(RESET_AFTER_SYNC_S)
    finally:
        pause(max(0.0, due - clock()))
        sysrq("b")


class DiskProbe:
    """While a session drives, writes in the directory its heartbeat names on a daemon thread of its own, one write
    at a time, and keeps when the last write ended: the watch only reads that and never waits on the thread, so a
    write stuck in the kernel holds neither a check nor the reset. A write that fails with an error ends like one
    that lands: an error is an answer."""

    def __init__(self, write: Callable[[Path], None], clock: Callable[[], float]) -> None:
        self.write = write
        self.clock = clock
        self.writing: threading.Thread | None = None
        self.answer: tuple[float, str | None] = (-math.inf, None)
        self.since: float | None = None

    def check(self, directory: Path | None, now: float) -> tuple[str, float, str | None] | None:
        """None while no session drives; else what the disk's silence counts from, its last answer before this check
        or, when none ended since the session began driving, that beginning, with its time and the last answer's
        error; a write starts unless one is still in flight."""
        if directory is None:
            self.since = None
            return None
        if self.since is None:
            self.since = now
        answered, error = self.answer
        if self.writing is None or not self.writing.is_alive():
            self.writing = threading.Thread(target=self._write, args=(directory,), daemon=True)
            self.writing.start()
        if answered < self.since:
            return "driving began", self.since, error
        return "its last answer", answered, error

    def _write(self, directory: Path) -> None:
        try:
            self.write(directory)
        except (OSError, ValueError) as exc:
            self.answer = (self.clock(), str(exc))
        else:
            self.answer = (self.clock(), None)


class Log:
    """Says each line in order on a daemon thread of its own, so a ``say`` that blocks holds neither a check nor the
    reset: a line that finds LOG_BACKLOG lines unwritten, the one being written included, is dropped and counted,
    and the count is said ahead of the next line the log has room for. A line ``say`` fails to write ends the thread
    and keeps its error in ``failed``; later lines raise nothing and wait in the backlog."""

    def __init__(self, say: Callable[[str], None]) -> None:
        self.say = say
        self.lines: deque[str] = deque()
        self.changed = threading.Condition()
        self.dropped = 0
        self.failed: Exception | None = None
        threading.Thread(target=self._write, daemon=True).start()

    def __call__(self, line: str) -> None:
        with self.changed:
            if self.dropped and len(self.lines) < LOG_BACKLOG:
                self.lines.append(f"{self.dropped} lines of the hang watch were dropped while its log blocked")
                self.dropped = 0
            if not self.dropped and len(self.lines) < LOG_BACKLOG:
                self.lines.append(line)
            else:
                self.dropped += 1
            self.changed.notify_all()

    def settle(self, seconds: float) -> None:
        """Wait at most ``seconds`` for every line to be written or the writing to fail."""
        with self.changed:
            self.changed.wait_for(lambda: not self.lines or self.failed is not None, seconds)

    def _write(self) -> None:
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.lines)
                line = self.lines[0]
            try:
                self.say(line)
            except Exception as exc:
                with self.changed:
                    self.failed = exc
                    self.changed.notify_all()
                return
            with self.changed:
                self.lines.popleft()
                self.changed.notify_all()


def write_probe(directory: Path) -> None:
    """Write PROBE_NAME in ``directory``, never through a link, sync it to the disk and remove it: a disk that takes
    the write answers."""
    path = directory / PROBE_NAME
    handle = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,  # equivalent-mutant: the file lives for one write
    )
    try:
        os.write(handle, b"corecycler hang-watch\n")
        os.fsync(handle)
    finally:
        os.close(handle)
        path.unlink()


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
    from corecycler.tuner.live import UnreadableEvidenceError, load_stored, margins
    from corecycler.tuner.live_hw import shaper_at_zero

    if len(rest) != 1 or not rest[0].isdigit():
        print("corecycler margins: needs one SESSION_ID; corecycler status lists them", file=sys.stderr)
        return EXIT_REFUSED
    session_id = int(rest[0])
    own = HistoryDB() if db is None else db
    try:
        loaded, unread = load_stored(own, session_id)
        if loaded is None:
            print(f"corecycler margins: {unread or f'session {session_id} is not stored'}", file=sys.stderr)
            return EXIT_REFUSED
        stored = loaded[0]
        shaper = dict(stored.setup).get("curve_shaper")
        if not shaper_at_zero(shaper):
            print(
                f"corecycler margins: session {session_id} ran with the Curve Shaper {shaper or 'not recorded'},"
                " so its offsets are not the whole offset and no margin is read from them",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        found = margins(own.list_tuner_tests(session_id))
    except UnreadableEvidenceError as exc:
        print(f"corecycler margins: session {session_id} could not be read: {exc}", file=sys.stderr)
        return EXIT_REFUSED
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
