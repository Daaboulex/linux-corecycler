"""CoreCycler — Per-core CPU stability tester and PBO Curve Optimizer tuner."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

# direct-file execution (docs' from-source flow): make the corecycler package importable
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


_SESSION_IDENTITY_KEYS = (
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP",
    "XDG_SESSION_TYPE",
    "DESKTOP_SESSION",
    "KDE_FULL_SESSION",
    "QT_QPA_PLATFORMTHEME",
    "XDG_CONFIG_DIRS",
    "DISPLAY",
)

_XDG_CONFIG_DIRS_DEFAULT = "/etc/xdg"


def _session_env(uid: int, proc_root: Path) -> dict[str, str]:
    """Environment of the invoking user's graphical session, read from /proc.

    Only a process of that user holding a display connection answers, so a
    detached shell never does.
    """
    for entry in sorted(proc_root.glob("[0-9]*"), key=lambda p: int(p.name)):
        try:
            if entry.stat().st_uid != uid:
                continue
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        env = dict(v.split("=", 1) for v in raw.decode("utf-8", "replace").split("\0") if "=" in v)
        if env.get("XDG_CURRENT_DESKTOP") and (env.get("WAYLAND_DISPLAY") or env.get("DISPLAY")):
            return env
    return {}


def _session_appearance(env: dict[str, str], home: Path, current: dict[str, str]) -> dict[str, str]:
    """The environment root must apply to render in the invoking user's appearance.

    sudo's ``env_reset`` drops the desktop identity and the config search path,
    so a root run falls back to the toolkit's default light theme however the
    user's desktop is set (issue #14). Their config home joins the SEARCH path
    only: KConfig writes to XDG_CONFIG_HOME, which stays root's own, so their
    settings are read and never written. Nothing that redirects where Qt loads
    code from is imported. A value already in the environment wins, except the
    search path, which is merged so neither side's entries are lost.
    """
    if not env:
        return {}
    apply = {key: env[key] for key in _SESSION_IDENTITY_KEYS if env.get(key) and key not in current}
    config_home = env.get("XDG_CONFIG_HOME") or str(home / ".config")
    search: list[str] = []
    for entry in (
        *(env.get("XDG_CONFIG_DIRS") or _XDG_CONFIG_DIRS_DEFAULT).split(":"),
        config_home,
        *current.get("XDG_CONFIG_DIRS", "").split(":"),
    ):
        if entry and entry not in search:
            search.append(entry)
    apply["XDG_CONFIG_DIRS"] = ":".join(search)
    return apply


def _private_config_home() -> Path | None:
    """An empty config home of root's own, so the invoking user's settings decide.

    KConfig reads XDG_CONFIG_HOME before the search path, so a kdeglobals that any
    earlier root run of a Qt or KDE app left in root's own config home picks the
    color scheme and the recovered session settings are never reached. A fresh
    directory per run also keeps one invoking user's leftovers off the next.
    """
    import atexit
    import shutil
    import tempfile

    try:
        target = Path(tempfile.mkdtemp(prefix="corecycler-config-"))
    except OSError:
        return None
    atexit.register(shutil.rmtree, target, True)
    return target


def _wayland_socket(run_dir: Path) -> str | None:
    """The invoking user's Wayland socket as an ABSOLUTE path, or None.

    An absolute WAYLAND_DISPLAY is connected to directly, so root needs no
    XDG_RUNTIME_DIR of its own -- and Qt then never reports the user's runtime
    directory as one it does not own, which it says once per lookup.
    """
    for sock in sorted(run_dir.glob("wayland-*")):
        if sock.is_socket():
            return str(sock)
    return None


def _bus_is_usable(address: str, uid: int) -> bool:
    """Whether this process may actually connect to that D-Bus session bus.

    A user's bus authenticates by peer credentials and commonly refuses another
    uid, so root is handed an address it cannot use and every portal call then
    fails loudly. Probe with the EXTERNAL handshake libdbus itself performs;
    anything but an accepted greeting is a no.
    """
    import socket

    endpoint = ""
    for part in address.split(","):
        if part.startswith("unix:path="):
            endpoint = part[len("unix:path=") :]
        elif part.startswith("unix:abstract="):
            endpoint = "\0" + part[len("unix:abstract=") :]
    if not endpoint:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(2.0)
            probe.connect(endpoint)
            probe.sendall(b"\0AUTH EXTERNAL " + str(uid).encode().hex().encode() + b"\r\n")
            return probe.recv(64).startswith(b"OK")
    except (OSError, ValueError):
        return False


def _bootstrap_sudo_session() -> None:
    """Derive a usable session handshake for root under ``sudo``.

    sudo strips XAUTHORITY, DISPLAY and WAYLAND_DISPLAY, so Qt can neither
    reach the user's Wayland socket nor authenticate to X11 — it then
    qFatal-aborts (SIGABRT) at QApplication construction. It strips the desktop
    identity and config search path too, which is what the desktop's appearance
    is read from. Point all of it at the INVOKING user's session; root's uid
    bypasses the socket permissions, so this is sufficient on both Wayland and
    X11. A session bus address is kept only if root may really use it, so
    nothing is handed a connection that can only fail.
    """
    import logging
    import os

    log = logging.getLogger(__name__)
    if os.geteuid() != 0:
        return
    sudo_uid = os.environ.get("SUDO_UID", "")
    if not sudo_uid.isdigit():
        return
    if "WAYLAND_DISPLAY" not in os.environ:
        socket_path = _wayland_socket(Path(f"/run/user/{sudo_uid}"))
        if socket_path:
            os.environ["WAYLAND_DISPLAY"] = socket_path
    from corecycler.config.paths import user_home

    home = user_home()
    if "XAUTHORITY" not in os.environ:
        xauth = home / ".Xauthority"
        if xauth.exists():
            os.environ["XAUTHORITY"] = str(xauth)
    session = _session_env(int(sudo_uid), Path("/proc"))
    appearance = _session_appearance(session, home, dict(os.environ))
    os.environ.update(appearance)
    if appearance:
        private_config = _private_config_home()
        if private_config:
            os.environ["XDG_CONFIG_HOME"] = str(private_config)
    log.debug(
        "sudo session appearance: %s (config home %s)",
        appearance or "none recovered",
        os.environ.get("XDG_CONFIG_HOME", "root's own"),
    )
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or session.get("DBUS_SESSION_BUS_ADDRESS", "")
    if address and _bus_is_usable(address, os.geteuid()):
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = address
    elif address:
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        log.debug("session bus %s refuses this user; dropped so nothing tries it", address)


def _install_exception_hooks(window) -> None:
    """Uncaught exceptions must SURFACE, never vanish: log the full traceback,
    make the hardware safe (stop the tuner, which reverts CO toward baselines),
    and tell the user. Silently continuing in an unknown state is how a
    poisoned session gets written; this is the top of the fail-closed chain.
    """
    import logging
    import threading
    import traceback

    hook_log = logging.getLogger("corecycler.excepthook")

    def _handle(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        hook_log.critical("UNCAUGHT EXCEPTION", exc_info=(exc_type, exc, tb))
        try:
            if window._tuner_tab.is_running:
                window._tuner_tab.force_stop()
        except Exception:
            hook_log.critical("Emergency tuner stop failed", exc_info=True)
        try:
            from PySide6.QtWidgets import QMessageBox

            detail = "".join(traceback.format_exception_only(exc_type, exc)).strip()
            QMessageBox.critical(
                window,
                "Internal Error",
                f"An internal error occurred:\n\n{detail}\n\n"
                "The auto-tuner was stopped (CO offsets reverted toward "
                "baseline). The full traceback is in the terminal/journal "
                "log. This is a bug to report, not a tuning result.",
            )
        except Exception:
            hook_log.critical("Could not display the error dialog", exc_info=True)

    def _thread_handle(args) -> None:
        # Non-GUI thread: no dialog (Qt forbids it off the main thread) — the
        # traceback still lands in the log instead of dying silently.
        hook_log.critical(
            "UNCAUGHT EXCEPTION in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _handle
    threading.excepthook = _thread_handle


def _parse_auto_resume(argv: list[str]) -> int | None:
    """--auto-resume [seconds]: resume the active mid-run session after a
    settle delay (the login-autostart path). Returns None when absent."""
    if "--auto-resume" not in argv:
        return None
    i = argv.index("--auto-resume")
    if i + 1 < len(argv):
        with contextlib.suppress(ValueError):
            return max(0, int(argv[i + 1]))
    return 120


def setup_logging() -> None:
    import logging

    # Two log surfaces from one root: the human narrative at INFO on stderr,
    # and a rotating DEBUG file capturing what verdicts drop (detector polls,
    # CO writes, cursor saves, lane lifecycle) for after-the-fact forensics.
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(fmt)
    handlers: list[logging.Handler] = [stderr_handler]
    try:
        from logging.handlers import RotatingFileHandler

        from corecycler.config.paths import fix_sudo_ownership, user_home

        log_dir = user_home() / ".local" / "share" / "corecycler" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_dir / "corecycler.log", maxBytes=5_000_000, backupCount=3)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        handlers.append(file_handler)
        fix_sudo_ownership(log_dir, log_dir / "corecycler.log")
    except OSError as e:
        print(f"corecycler: debug log unavailable: {e}", file=sys.stderr)
    logging.basicConfig(level=logging.DEBUG, handlers=handlers)


def main() -> int:
    import os

    setup_logging()

    argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        from corecycler.cli import USAGE

        print(USAGE)
        return 0
    if argv and argv[0] in ("doctor", "status", "tune", "resume"):
        from corecycler.cli import cli_main

        return cli_main(argv)

    # Silence the warning categories that fire for session services root cannot use
    os.environ.setdefault(
        "QT_LOGGING_RULES",
        "qt.qpa.services.warning=false;kf.windowsystem.warning=false",
    )

    _bootstrap_sudo_session()

    # Preflight: with no display reachable Qt aborts the whole process
    # (SIGABRT) — fail closed with an actionable message instead. Skipped when
    # the user explicitly chose a Qt platform (offscreen/vnc/linuxfb/eglfs
    # need no display server at all).
    if (
        not os.environ.get("QT_QPA_PLATFORM")
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    ):
        print(
            "corecycler: no display found (DISPLAY and WAYLAND_DISPLAY are both "
            "unset).\nRun it from a graphical session. Under sudo, the invoking "
            "user's session env is derived automatically; if that failed, try "
            "'sudo -E corecycler'.",
            file=sys.stderr,
        )
        return 1

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    # high DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("CoreCycler")
    app.setOrganizationName("corecycler")

    # One instance only — two engines would fight over the SMU.
    from PySide6.QtCore import QLockFile

    from corecycler.config.paths import user_home

    lock_dir = user_home() / ".local" / "share" / "corecycler"
    lock_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(lock_dir / "corecycler.lock"))
    if not instance_lock.tryLock(0):
        print("corecycler: another instance is already running.", file=sys.stderr)
        return 1
    app._corecycler_instance_lock = instance_lock

    # Locate assets — dev mode (src/../assets) or installed ($out/share/...)
    assets_dir = _find_assets_dir()

    # app icon
    from PySide6.QtGui import QIcon

    icon_path = assets_dir / "icon.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    from corecycler.config import tools
    from corecycler.gui import style
    from corecycler.gui.main_window import MainWindow

    style.follow(app)

    tools.load_configured_paths()

    window = MainWindow()

    auto_resume_delay = _parse_auto_resume(sys.argv[1:])
    if auto_resume_delay is not None:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(auto_resume_delay * 1000, window.attempt_auto_resume)

    import atexit
    import signal

    def _cleanup_on_exit():
        """Kill any running stress processes on forced exit."""
        # Each subsystem wrapped independently — one failure must not block others
        try:
            if window._worker and window._worker.isRunning():
                window._worker.scheduler.force_stop()
                if not window._worker.wait(3000):
                    window._worker.terminate()
                    window._worker.wait(2000)
        except Exception as e:
            print(f"exit cleanup: stress worker stop failed: {e}", file=sys.stderr)

        try:
            if window._tuner_tab.is_running:
                window._tuner_tab.force_stop()
        except Exception as e:
            print(f"exit cleanup: tuner stop failed: {e}", file=sys.stderr)

        try:
            window._memory_tab.force_stop()
        except Exception as e:
            print(f"exit cleanup: memory stop failed: {e}", file=sys.stderr)

    atexit.register(_cleanup_on_exit)

    # Handle SIGTERM/SIGINT/SIGHUP gracefully — save tuner state on exit
    def _signal_handler(signum, frame):
        _cleanup_on_exit()
        app.quit()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _signal_handler)

    _install_exception_hooks(window)

    window.show()

    return app.exec()


def _find_assets_dir() -> Path:
    """Find assets directory — works in dev mode and Nix-installed."""
    # Dev mode: src/corecycler/../../assets
    dev_assets = Path(__file__).resolve().parents[2] / "assets"
    if dev_assets.is_dir():
        return dev_assets
    # Nix installed: __file__ is $out/lib/python3.x/site-packages/corecycler/main.py
    # so go up 5 levels to $out, then into share/corecycler/assets
    nix_assets = Path(__file__).resolve().parents[4] / "share" / "corecycler" / "assets"
    if nix_assets.is_dir():
        return nix_assets
    return dev_assets  # fallback


if __name__ == "__main__":
    sys.exit(main())
