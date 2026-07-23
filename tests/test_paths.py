"""config.paths sudo identity + ownership repair.

Under sudo, persistent state must resolve to the INVOKING user (never /root),
and root must never leave the files it created owned by root -- but the repair
must never raise, since a root-owned file beats losing the write.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from corecycler.config import paths


class TestUserHome:
    def test_non_root_uses_invoker_home(self):
        with patch("os.geteuid", return_value=1000), patch("pathlib.Path.home", return_value=Path("/home/inv")):
            assert paths.user_home() == Path("/home/inv")

    def test_root_with_sudo_user_resolves_invoker(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "alice")
        fake = type("E", (), {"pw_dir": "/home/alice"})()
        with patch("os.geteuid", return_value=0), patch("pwd.getpwnam", return_value=fake):
            assert paths.user_home() == Path("/home/alice")

    def test_root_sudo_user_root_falls_back(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "root")
        with patch("os.geteuid", return_value=0), patch("pathlib.Path.home", return_value=Path("/home/inv")):
            assert paths.user_home() == Path("/home/inv")

    def test_root_no_sudo_user_falls_back(self, monkeypatch):
        monkeypatch.delenv("SUDO_USER", raising=False)
        with patch("os.geteuid", return_value=0), patch("pathlib.Path.home", return_value=Path("/home/inv")):
            assert paths.user_home() == Path("/home/inv")

    def test_root_stale_sudo_user_falls_back(self, monkeypatch):
        monkeypatch.setenv("SUDO_USER", "ghost")
        with (
            patch("os.geteuid", return_value=0),
            patch("pwd.getpwnam", side_effect=KeyError),
            patch("pathlib.Path.home", return_value=Path("/home/inv")),
        ):
            assert paths.user_home() == Path("/home/inv")


class TestFixSudoOwnership:
    def test_non_root_is_noop(self):
        with patch("os.geteuid", return_value=1000), patch("os.chown") as chown:
            paths.fix_sudo_ownership(Path("/tmp/x"))
        chown.assert_not_called()

    def test_root_chowns_existing_paths(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        f = tmp_path / "db"
        f.write_text("x")
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(f)
        chown.assert_called_once_with(f, 1000, 1000)

    def test_non_digit_uid_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "notnum")
        monkeypatch.setenv("SUDO_GID", "1000")
        f = tmp_path / "db"
        f.write_text("x")
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(f)
        chown.assert_not_called()

    def test_missing_path_not_chowned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.fix_sudo_ownership(tmp_path / "nope")
        chown.assert_not_called()

    def test_chown_oserror_is_swallowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SUDO_UID", "1000")
        monkeypatch.setenv("SUDO_GID", "1000")
        f = tmp_path / "db"
        f.write_text("x")
        with patch("os.geteuid", return_value=0), patch("os.chown", side_effect=OSError):
            paths.fix_sudo_ownership(f)
