"""Headless commands — tune, resume and inspect sessions without a display."""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from corecycler.config import tools
from corecycler.config.settings import load_settings

if TYPE_CHECKING:
    from corecycler.config.tools import Resolution

EXIT_COMPLETED = 0
EXIT_PAUSED = 3
EXIT_QUARANTINED = 4
EXIT_REFUSED = 5
EXIT_ENGINE_ABORTED = 6
EXIT_LOCKED = 7
EXIT_SIGNAL = 130

USAGE = """\
corecycler headless commands:

  corecycler doctor               report every external tool and where it resolved
  corecycler status               list tuner sessions and their state
  corecycler tune [--config F]    start a NEW tuning session and run to the end
  corecycler resume [SESSION_ID]  resume a mid-run/paused session (newest if omitted)

Exit codes: 0 completed, 3 paused (needs attention), 4 quarantined,
5 refused (bad config/environment), 6 engine aborted, 7 already running,
130 stopped by signal (offsets reverted to baseline).

Running the binary with no command opens the GUI.
"""


def cli_main(argv: list[str]) -> int:
    command = argv[0]
    if command == "doctor":
        return cmd_doctor()
    if command == "status":
        return cmd_status()
    if command == "tune":
        config_path = _flag_value(argv[1:], "--config")
        if config_path is _INVALID:
            print("corecycler tune: --config requires a file path", file=sys.stderr)
            return EXIT_REFUSED
        return cmd_run(config_path=config_path, resume_id=None, auto_resume=False)
    if command == "resume":
        rest = [a for a in argv[1:] if not a.startswith("-")]
        if len(rest) > 1:
            print("corecycler resume: at most one SESSION_ID", file=sys.stderr)
            return EXIT_REFUSED
        if rest:
            try:
                session_id = int(rest[0])
            except ValueError:
                print(f"corecycler resume: invalid session id {rest[0]!r}", file=sys.stderr)
                return EXIT_REFUSED
            return cmd_run(config_path=None, resume_id=session_id, auto_resume=False)
        return cmd_run(config_path=None, resume_id=None, auto_resume=True)
    print(USAGE, file=sys.stderr)
    return EXIT_REFUSED


_INVALID = object()


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


def _flag_value(args: list[str], flag: str):
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args):
        return _INVALID
    return args[i + 1]


def cmd_status(db=None) -> int:
    from corecycler.history.db import HistoryDB
    from corecycler.tuner import persistence as tp

    own_db = db is None
    if db is None:
        db = HistoryDB()
    try:
        sessions = db.list_tuner_sessions(limit=50)
        if not sessions:
            print("no tuner sessions")
            return EXIT_COMPLETED
        for sess in sessions:
            states = tp.load_core_states(db, sess.id)
            done = sum(1 for cs in states.values() if cs.phase in ("confirmed", "hardened"))
            print(
                f"#{sess.id}  {sess.status:<12} {done}/{len(states)} cores done  "
                f"created {sess.created_at[:19]}  {sess.cpu_model or ''}"
            )
        return EXIT_COMPLETED
    finally:
        if own_db:
            db.close()


def _build_smu(topology):
    from corecycler.smu.commands import detect_generation, get_commands
    from corecycler.smu.driver import RyzenSMU, core_map_blocked

    commands = get_commands(detect_generation(topology.family, topology.model, topology.model_name))
    if commands is None or not commands.has_co or not RyzenSMU.is_available():
        return None
    smu = RyzenSMU(commands)
    smu.set_topology(topology)
    map_err = core_map_blocked(smu)
    if map_err is not None:
        print(f"corecycler: per-core CO disabled: {map_err}", file=sys.stderr)
        return None
    return smu


def cmd_run(
    config_path: str | None,
    resume_id: int | None,
    auto_resume: bool,
    *,
    engine_factory=None,
    db=None,
) -> int:
    from PySide6.QtCore import QCoreApplication, QLockFile

    from corecycler.config.paths import user_home

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

    lock_dir = user_home() / ".local" / "share" / "corecycler"
    lock_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(lock_dir / "corecycler.lock"))
    if not instance_lock.tryLock(0):
        print("corecycler: another instance is already running.", file=sys.stderr)
        return EXIT_LOCKED

    from corecycler.engine.backends import get_backend, load_all
    from corecycler.engine.topology import detect_topology
    from corecycler.history.db import HistoryDB
    from corecycler.tuner import persistence as tp
    from corecycler.tuner.config import TunerConfig
    from corecycler.tuner.engine import TunerEngine

    config = TunerConfig()
    if config_path is not None:
        try:
            config = TunerConfig.from_json(Path(config_path).read_text())
        except OSError as e:
            print(f"corecycler: cannot read config: {e}", file=sys.stderr)
            return EXIT_REFUSED
    errors = config.validate()
    if errors:
        print("corecycler: invalid config: " + "; ".join(errors), file=sys.stderr)
        return EXIT_REFUSED

    load_all()
    tools.load_configured_paths()
    if db is None:
        db = HistoryDB()
    if engine_factory is not None:
        engine = engine_factory(db, config)
    else:
        topology = detect_topology()
        if topology is None or not topology.cores:
            print("corecycler: CPU topology detection failed", file=sys.stderr)
            return EXIT_REFUSED
        smu = _build_smu(topology)
        if smu is None:
            print(
                "corecycler: per-core SMU access is unavailable — the tuner "
                "needs it (any refusal reason is printed above; otherwise "
                "check modprobe ryzen_smu and device permissions).",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        try:
            backend = get_backend(config.backend)
        except KeyError:
            print(f"corecycler: unknown backend {config.backend!r}", file=sys.stderr)
            return EXIT_REFUSED
        if not backend.is_available():
            resolution = backend.resolution()
            print(
                f"corecycler: backend {config.backend!r} {resolution.problem} -- "
                f"install {tools.TOOLS[config.backend].package}, or set "
                f"{tools.env_var(config.backend)}; run 'corecycler doctor' for the full report",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        engine = TunerEngine(db=db, topology=topology, smu=smu, backend=backend, config=config)

    outcome: dict[str, int] = {}

    def finish(code: int, *, force: bool = False) -> None:
        if force:
            outcome["exit"] = code
        else:
            outcome.setdefault("exit", code)
        app.quit()

    engine.log_message.connect(lambda m: print(m, flush=True))
    engine.session_completed.connect(lambda _p: finish(EXIT_COMPLETED, force=True))

    def on_status(status: str) -> None:
        if status == "paused":
            finish(EXIT_PAUSED)
        elif status == "quarantined":
            finish(EXIT_QUARANTINED)
        elif status == "idle":
            finish(EXIT_ENGINE_ABORTED)

    engine.status_changed.connect(on_status)

    def on_signal(signum, _frame) -> None:
        print(f"corecycler: signal {signum} — aborting (offsets revert)", flush=True)
        outcome["exit"] = EXIT_SIGNAL
        engine.abort()
        app.quit()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    if resume_id is not None:
        engine.resume(resume_id)
    elif auto_resume:
        session = tp.pick_auto_resume_session(db)
        if session is None:
            sessions = db.list_resumable_tuner_sessions()
            if not sessions:
                print("corecycler: no resumable session", file=sys.stderr)
                return EXIT_REFUSED
            session = sessions[0]
        engine.resume(session.id)
    else:
        engine.start()

    if "exit" in outcome:
        return outcome["exit"]
    if engine.status not in ("running", "validating", "hunting"):
        print(
            f"corecycler: engine did not start (status {engine.status}) — see log",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    app.exec()
    code = outcome.get("exit", EXIT_ENGINE_ABORTED)
    _notify_outcome(code)
    return code


_OUTCOME_NOTES = {
    EXIT_COMPLETED: ("Tuning complete", "The session finished and confirmed a profile.", "normal"),
    EXIT_PAUSED: ("Tuning paused", "The tuner stopped for attention — check the log.", "critical"),
    EXIT_QUARANTINED: ("Tuning quarantined", "The profile is unsafe; cores forced to stock.", "critical"),
    EXIT_ENGINE_ABORTED: ("Tuning aborted", "The engine stopped itself — check the log.", "critical"),
}


def _notify_outcome(code: int) -> None:
    note = _OUTCOME_NOTES.get(code)
    if note is None:
        return
    try:
        if not load_settings().notify_on_completion:
            return
        from corecycler.notify import desktop_notify

        title, body, urgency = note
        desktop_notify(title, body, urgency=urgency)
    except Exception as e:
        print(f"notification failed: {e}", file=sys.stderr)
