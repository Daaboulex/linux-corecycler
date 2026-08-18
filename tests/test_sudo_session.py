"""The desktop appearance a sudo run must recover before Qt starts (issue #14).

Under sudo the desktop identity and the config search path are gone, so the
toolkit renders in its default light theme however the user's desktop is set.
Recovery is read-only: the config HOME stays root's, so their settings are read
and never written.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.main import _bus_is_usable, _session_appearance, _session_env, _wayland_socket

SESSION = {
    "XDG_CURRENT_DESKTOP": "KDE",
    "XDG_SESSION_TYPE": "wayland",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    "XDG_CONFIG_DIRS": "/home/rie/.config/kdedefaults:/etc/xdg",
    "XDG_CONFIG_HOME": "/home/rie/.config",
    "WAYLAND_DISPLAY": "wayland-0",
    "DISPLAY": ":0",
    "SSH_AUTH_SOCK": "/run/user/1000/keyring/ssh",
    "QT_PLUGIN_PATH": "/home/rie/plugins",
    "LD_PRELOAD": "/home/rie/evil.so",
}

HOME = Path("/home/rie")


def _fake_process(root: Path, pid: str, env: dict[str, str] | None) -> None:
    entry = root / pid
    entry.mkdir()
    if env is not None:
        (entry / "environ").write_bytes(b"".join(f"{k}={v}\0".encode() for k, v in env.items()))


class TestSessionLookup:
    def test_reads_the_graphical_session_of_that_user(self, tmp_path):
        _fake_process(tmp_path, "412", SESSION)
        assert _session_env(os.getuid(), tmp_path)["XDG_CURRENT_DESKTOP"] == "KDE"

    def test_the_graphical_session_answers_not_a_detached_shell(self, tmp_path):
        _fake_process(tmp_path, "300", {"XDG_CURRENT_DESKTOP": "GNOME"})
        _fake_process(tmp_path, "412", SESSION)
        _fake_process(tmp_path, "900", {"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":1"})
        assert _session_env(os.getuid(), tmp_path)["XDG_CURRENT_DESKTOP"] == "KDE"

    def test_another_users_session_is_never_read(self, tmp_path):
        _fake_process(tmp_path, "412", SESSION)
        assert _session_env(os.getuid() + 1, tmp_path) == {}

    def test_an_unreadable_or_malformed_entry_is_skipped(self, tmp_path):
        _fake_process(tmp_path, "100", None)
        (tmp_path / "200").mkdir()
        (tmp_path / "200" / "environ").write_bytes(b"GARBAGE\x00\xff\xfe\x00")
        _fake_process(tmp_path, "412", SESSION)
        assert _session_env(os.getuid(), tmp_path)["XDG_CURRENT_DESKTOP"] == "KDE"


class TestWhatRootImports:
    def test_takes_the_desktop_identity(self):
        applied = _session_appearance(SESSION, HOME, {})
        assert applied["XDG_CURRENT_DESKTOP"] == "KDE"
        assert applied["XDG_SESSION_TYPE"] == "wayland"
        assert applied["DISPLAY"] == ":0"

    def test_leaves_behind_anything_that_could_redirect_code_loading(self):
        applied = _session_appearance(SESSION, HOME, {})
        assert "QT_PLUGIN_PATH" not in applied
        assert "LD_PRELOAD" not in applied
        assert "SSH_AUTH_SOCK" not in applied

    def test_the_config_home_stays_root_own_so_nothing_is_written_there(self):
        applied = _session_appearance(SESSION, HOME, {})
        assert "XDG_CONFIG_HOME" not in applied

    def test_the_users_settings_join_the_read_only_search_path(self):
        applied = _session_appearance(SESSION, HOME, {})
        assert applied["XDG_CONFIG_DIRS"].split(":") == [
            "/home/rie/.config/kdedefaults",
            "/etc/xdg",
            "/home/rie/.config",
        ]

    def test_a_session_without_a_config_home_falls_back_to_the_home_directory(self):
        env = {k: v for k, v in SESSION.items() if k != "XDG_CONFIG_HOME"}
        assert str(HOME / ".config") in _session_appearance(env, HOME, {})["XDG_CONFIG_DIRS"].split(":")

    def test_a_session_without_a_search_path_keeps_the_system_default(self):
        env = {k: v for k, v in SESSION.items() if k != "XDG_CONFIG_DIRS"}
        assert _session_appearance(env, HOME, {})["XDG_CONFIG_DIRS"].split(":") == [
            "/etc/xdg",
            "/home/rie/.config",
        ]

    def test_an_identity_already_in_the_environment_wins(self):
        applied = _session_appearance(SESSION, HOME, {"XDG_CURRENT_DESKTOP": "GNOME"})
        assert "XDG_CURRENT_DESKTOP" not in applied

    def test_a_search_path_already_in_the_environment_is_kept_as_well(self):
        applied = _session_appearance(SESSION, HOME, {"XDG_CONFIG_DIRS": "/opt/site/xdg"})
        assert applied["XDG_CONFIG_DIRS"].split(":") == [
            "/home/rie/.config/kdedefaults",
            "/etc/xdg",
            "/home/rie/.config",
            "/opt/site/xdg",
        ]

    def test_no_entry_is_ever_duplicated(self):
        applied = _session_appearance(SESSION, HOME, {"XDG_CONFIG_DIRS": "/etc/xdg"})
        entries = applied["XDG_CONFIG_DIRS"].split(":")
        assert len(entries) == len(set(entries))

    def test_no_session_found_changes_nothing(self):
        assert _session_appearance({}, HOME, {"XDG_CONFIG_DIRS": "/etc/xdg"}) == {}

    def test_the_session_bus_is_not_imported_blindly(self):
        assert "DBUS_SESSION_BUS_ADDRESS" not in _session_appearance(SESSION, HOME, {})


class TestWaylandSocket:
    def test_hands_back_an_absolute_path_so_no_runtime_dir_is_needed(self, tmp_path):
        import socket

        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(tmp_path / "wayland-0"))
            found = _wayland_socket(tmp_path)
        assert found == str(tmp_path / "wayland-0")

    def test_a_lock_file_beside_the_socket_is_not_mistaken_for_it(self, tmp_path):
        import socket

        (tmp_path / "wayland-0.lock").write_text("")
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(str(tmp_path / "wayland-1"))
            assert _wayland_socket(tmp_path) == str(tmp_path / "wayland-1")

    def test_no_socket_is_no_answer(self, tmp_path):
        (tmp_path / "wayland-0.lock").write_text("")
        assert _wayland_socket(tmp_path) is None


class TestSessionBusProbe:
    """A bus that refuses this uid must be reported unusable, not handed on:
    every portal call on an unusable address fails loudly (issue #14)."""

    @staticmethod
    def _fake_bus(path, reply: bytes):
        import socket
        import threading

        server = socket.socket(socket.AF_UNIX)
        server.bind(str(path))
        server.listen(1)

        def serve():
            conn, _ = server.accept()
            with conn:
                conn.recv(128)
                conn.sendall(reply)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        return server, thread

    def test_an_accepted_handshake_is_usable(self, tmp_path):
        server, thread = self._fake_bus(tmp_path / "bus", b"OK 1234deadbeef\r\n")
        try:
            assert _bus_is_usable(f"unix:path={tmp_path}/bus", os.getuid()) is True
        finally:
            thread.join(timeout=2)
            server.close()

    def test_a_refused_handshake_is_not_usable(self, tmp_path):
        server, thread = self._fake_bus(tmp_path / "bus", b"REJECTED EXTERNAL\r\n")
        try:
            assert _bus_is_usable(f"unix:path={tmp_path}/bus", 0) is False
        finally:
            thread.join(timeout=2)
            server.close()

    def test_an_address_with_nothing_listening_is_not_usable(self, tmp_path):
        assert _bus_is_usable(f"unix:path={tmp_path}/absent", os.getuid()) is False

    def test_an_address_with_no_unix_endpoint_is_not_usable(self):
        assert _bus_is_usable("tcp:host=localhost,port=1234", os.getuid()) is False

    def test_an_empty_address_is_not_usable(self):
        assert _bus_is_usable("", os.getuid()) is False
