"""CoreCycler — Per-core CPU stability tester and PBO Curve Optimizer tuner."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

# direct-file execution (docs' from-source flow): make the corecycler package importable
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _bootstrap_sudo_display() -> None:
    """Derive a usable display handshake for root under ``sudo``.

    sudo strips XDG_RUNTIME_DIR/XAUTHORITY (and often WAYLAND_DISPLAY), so Qt
    can neither reach the user's Wayland socket nor authenticate to X11 — it
    then qFatal-aborts (SIGABRT) at QApplication construction. Point the
    handshake at the INVOKING user's session; root's uid bypasses the socket
    permissions, so this is sufficient on both Wayland and X11.
    """
    import os

    if os.geteuid() != 0:
        return
    sudo_uid = os.environ.get("SUDO_UID", "")
    if not sudo_uid.isdigit():
        return
    run_dir = Path(f"/run/user/{sudo_uid}")
    if run_dir.is_dir():
        os.environ.setdefault("XDG_RUNTIME_DIR", str(run_dir))
        if "WAYLAND_DISPLAY" not in os.environ:
            for sock in sorted(run_dir.glob("wayland-*")):
                if sock.is_socket():
                    os.environ["WAYLAND_DISPLAY"] = sock.name
                    break
    if "XAUTHORITY" not in os.environ:
        from corecycler.config.paths import user_home

        xauth = user_home() / ".Xauthority"
        if xauth.exists():
            os.environ["XAUTHORITY"] = str(xauth)


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

    # Suppress Qt/KDE warnings when running under sudo (no D-Bus session)
    os.environ.setdefault(
        "QT_LOGGING_RULES",
        "qt.qpa.services.warning=false;kf.windowsystem.warning=false",
    )

    _bootstrap_sudo_display()

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

    # dark theme
    app.setStyleSheet(_dark_stylesheet(assets_dir))

    from corecycler.config import tools
    from corecycler.gui.main_window import MainWindow

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


def _dark_stylesheet(assets_dir: Path) -> str:
    # Qt QSS requires forward slashes even on Windows
    a = str(assets_dir).replace("\\", "/")
    return f"""
        QMainWindow, QWidget {{
            background-color: #1e1e1e;
            color: #ddd;
        }}
        QTabWidget::pane {{
            border: none;
            border-top: 1px solid #333;
            background: #1e1e1e;
        }}
        QTabBar {{
            background: transparent;
        }}
        QTabBar::tab {{
            background: transparent;
            color: #888;
            padding: 8px 18px;
            border: none;
            border-bottom: 2px solid transparent;
            margin-right: 2px;
        }}
        QTabBar::tab:selected {{
            color: #fff;
            border-bottom: 2px solid #4fc3f7;
        }}
        QTabBar::tab:hover:!selected {{
            color: #ccc;
            border-bottom: 2px solid #555;
        }}
        QGroupBox {{
            border: 1px solid #333;
            border-radius: 4px;
            margin-top: 12px;
            padding-top: 12px;
            font-weight: bold;
            color: #aaa;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 4px;
        }}
        QTableWidget {{
            background-color: #252525;
            alternate-background-color: #2a2a2a;
            gridline-color: #333;
            border: 1px solid #333;
            color: #ddd;
        }}
        QTableWidget::item:selected {{
            background-color: #1a3a5c;
        }}
        QHeaderView::section {{
            background-color: #2d2d2d;
            color: #aaa;
            padding: 4px;
            border: 1px solid #333;
            font-weight: bold;
        }}
        QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
            background-color: #2d2d2d;
            color: #ddd;
            border: 1px solid #444;
            border-radius: 3px;
            padding: 4px 8px;
        }}
        QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QLineEdit:focus {{
            border-color: #4fc3f7;
        }}
        /* --- QComboBox dropdown --- */
        QComboBox::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 24px;
            border-left: 1px solid #444;
            border-top-right-radius: 3px;
            border-bottom-right-radius: 3px;
            background: #353535;
        }}
        QComboBox::drop-down:hover {{
            background: #3d3d3d;
        }}
        QComboBox::down-arrow {{
            image: url({a}/arrow-down.svg);
            width: 10px;
            height: 6px;
        }}
        QComboBox::down-arrow:hover {{
            image: url({a}/arrow-down-hover.svg);
        }}
        QComboBox::down-arrow:disabled {{
            image: url({a}/arrow-down-disabled.svg);
        }}
        /* --- QSpinBox / QDoubleSpinBox buttons --- */
        QSpinBox::up-button, QDoubleSpinBox::up-button {{
            subcontrol-origin: padding;
            subcontrol-position: top right;
            width: 20px;
            border-left: 1px solid #444;
            border-bottom: 1px solid #444;
            border-top-right-radius: 3px;
            background: #353535;
        }}
        QSpinBox::down-button, QDoubleSpinBox::down-button {{
            subcontrol-origin: padding;
            subcontrol-position: bottom right;
            width: 20px;
            border-left: 1px solid #444;
            border-bottom-right-radius: 3px;
            background: #353535;
        }}
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
        QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
            background: #3d3d3d;
        }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
            image: url({a}/arrow-up.svg);
            width: 10px;
            height: 6px;
        }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
            image: url({a}/arrow-down.svg);
            width: 10px;
            height: 6px;
        }}
        QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {{
            image: url({a}/arrow-up-hover.svg);
        }}
        QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {{
            image: url({a}/arrow-down-hover.svg);
        }}
        QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off,
        QDoubleSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:off {{
            image: url({a}/arrow-up-disabled.svg);
        }}
        QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off,
        QDoubleSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:off {{
            image: url({a}/arrow-down-disabled.svg);
        }}
        /* --- Buttons --- */
        QPushButton, QToolButton {{
            background-color: #2d2d2d;
            color: #ddd;
            border: 1px solid #444;
            border-radius: 4px;
            padding: 6px 12px;
        }}
        QPushButton:hover, QToolButton:hover {{
            background-color: #353535;
        }}
        QPushButton:pressed, QToolButton:pressed {{
            background-color: #1a1a1a;
        }}
        QPushButton:disabled, QToolButton:disabled {{
            color: #555;
            background-color: #222;
        }}
        QCheckBox {{
            color: #ddd;
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 16px;
            height: 16px;
        }}
        QPlainTextEdit {{
            background-color: #1a1a1a;
            color: #ddd;
            border: 1px solid #333;
        }}
        QScrollBar:vertical {{
            background: #1e1e1e;
            width: 10px;
        }}
        QScrollBar::handle:vertical {{
            background: #444;
            border-radius: 5px;
            min-height: 20px;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QStatusBar {{
            background: #252525;
            color: #aaa;
            border-top: 1px solid #333;
        }}
        QToolBar {{
            background: #252525;
            border-bottom: 1px solid #333;
            spacing: 8px;
            padding: 4px;
        }}
        QLabel {{
            color: #ddd;
        }}
        QScrollArea {{
            border: none;
        }}
        QSplitter::handle {{
            background: #333;
            height: 2px;
        }}
    """


if __name__ == "__main__":
    sys.exit(main())
