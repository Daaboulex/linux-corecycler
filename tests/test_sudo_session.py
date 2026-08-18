"""The desktop identity a sudo run must recover before Qt starts (issue #14)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.main import _session_identity

SESSION = {
    "XDG_CURRENT_DESKTOP": "KDE",
    "XDG_SESSION_TYPE": "wayland",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "WAYLAND_DISPLAY": "wayland-0",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "QT_PLUGIN_PATH": "/home/user/plugins",
}


def _fake_process(root: Path, pid: str, env: dict[str, str] | None) -> None:
    entry = root / pid
    entry.mkdir()
    if env is not None:
        (entry / "environ").write_bytes(b"".join(f"{k}={v}\0".encode() for k, v in env.items()))


class TestSessionIdentity:
    def test_recovers_the_desktop_and_bus_of_the_session(self, tmp_path):
        _fake_process(tmp_path, "412", SESSION)
        recovered = _session_identity(os.getuid(), tmp_path)
        assert recovered["XDG_CURRENT_DESKTOP"] == "KDE"
        assert recovered["DBUS_SESSION_BUS_ADDRESS"] == SESSION["DBUS_SESSION_BUS_ADDRESS"]

    def test_takes_only_allowlisted_keys(self, tmp_path):
        _fake_process(tmp_path, "412", SESSION)
        assert set(_session_identity(os.getuid(), tmp_path)) == {
            "XDG_CURRENT_DESKTOP",
            "XDG_SESSION_TYPE",
            "DBUS_SESSION_BUS_ADDRESS",
        }

    def test_the_graphical_session_answers_not_a_detached_shell(self, tmp_path):
        _fake_process(tmp_path, "300", {"XDG_CURRENT_DESKTOP": "GNOME"})
        _fake_process(tmp_path, "412", SESSION)
        _fake_process(tmp_path, "900", {"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":1"})
        assert _session_identity(os.getuid(), tmp_path)["XDG_CURRENT_DESKTOP"] == "KDE"

    def test_another_users_session_is_never_read(self, tmp_path):
        _fake_process(tmp_path, "412", SESSION)
        assert _session_identity(os.getuid() + 1, tmp_path) == {}

    def test_an_unreadable_or_malformed_entry_is_skipped(self, tmp_path):
        _fake_process(tmp_path, "100", None)
        (tmp_path / "200").mkdir()
        (tmp_path / "200" / "environ").write_bytes(b"GARBAGE\x00\xff\xfe\x00")
        _fake_process(tmp_path, "412", SESSION)
        assert _session_identity(os.getuid(), tmp_path)["XDG_CURRENT_DESKTOP"] == "KDE"
