"""config.paths sudo identity + ownership repair.

Under sudo, persistent state must resolve to the INVOKING user (never /root),
and root must never leave the files it created owned by root -- but the repair
must never raise, since a root-owned file beats losing the write.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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


class TestHandBack:
    USER = 1000

    def run(self, tmp_path, monkeypatch, owners, leaf, chown=None):
        monkeypatch.setenv("SUDO_UID", str(self.USER))
        monkeypatch.setenv("SUDO_GID", str(self.USER))
        real_lstat = Path.lstat

        def lstat(path):
            got = real_lstat(path)
            return SimpleNamespace(st_uid=owners.get(path, got.st_uid))

        calls = []
        monkeypatch.setattr(Path, "lstat", lstat)
        with patch("os.geteuid", return_value=0), patch("os.chown", side_effect=chown) as fake:
            paths.hand_back(leaf)
            calls = [(c.args[0], c.kwargs) for c in fake.call_args_list]
        return calls

    def tree(self, tmp_path):
        leaf = tmp_path / "home" / "a" / "b" / "f"
        leaf.parent.mkdir(parents=True)
        leaf.write_text("x")
        return leaf, dict.fromkeys([leaf, *leaf.parents], 0)

    def test_root_created_entries_below_the_users_own_directory_change_hands_top_down(self, tmp_path, monkeypatch):
        leaf, owners = self.tree(tmp_path)
        owners[tmp_path / "home"] = self.USER
        calls = self.run(tmp_path, monkeypatch, owners, leaf)
        assert [path for path, _ in calls] == [leaf.parents[1], leaf.parent, leaf]
        assert all(kwargs == {"follow_symlinks": False} for _, kwargs in calls)

    def test_nothing_outside_the_users_own_tree_changes_hands(self, tmp_path, monkeypatch):
        leaf, owners = self.tree(tmp_path)
        assert self.run(tmp_path, monkeypatch, owners, leaf) == []
        owners[tmp_path] = self.USER
        assert [path for path, _ in self.run(tmp_path, monkeypatch, owners, leaf)][-1] == leaf

    def test_an_entry_another_user_owns_keeps_its_owner(self, tmp_path, monkeypatch):
        leaf, owners = self.tree(tmp_path)
        owners[tmp_path / "home"] = self.USER
        owners[leaf.parent] = self.USER + 1
        calls = self.run(tmp_path, monkeypatch, owners, leaf)
        assert [path for path, _ in calls] == [leaf.parents[1], leaf]

    def test_a_leaf_the_user_owns_such_as_a_planted_link_hands_nothing_over(self, tmp_path, monkeypatch):
        leaf, owners = self.tree(tmp_path)
        owners[leaf] = self.USER
        assert self.run(tmp_path, monkeypatch, owners, leaf) == []
        owners[leaf] = 0
        owners[leaf.parent] = self.USER
        assert [path for path, _ in self.run(tmp_path, monkeypatch, owners, leaf)] == [leaf]

    def test_a_failed_chown_is_logged_with_its_path_and_not_raised(self, tmp_path, monkeypatch, caplog):
        leaf, owners = self.tree(tmp_path)
        owners[leaf.parent] = self.USER
        with caplog.at_level("WARNING", logger="corecycler.config.paths"):
            self.run(tmp_path, monkeypatch, owners, leaf, chown=PermissionError("read-only"))
        assert str(leaf) in caplog.text and "read-only" in caplog.text

    def test_without_sudo_or_a_readable_invoker_nothing_changes_hands(self, tmp_path, monkeypatch):
        leaf, owners = self.tree(tmp_path)
        owners[leaf.parent] = self.USER
        assert [path for path, _ in self.run(tmp_path, monkeypatch, owners, leaf)] == [leaf]
        assert self.run(tmp_path, monkeypatch, owners, tmp_path / "missing") == []
        monkeypatch.setenv("SUDO_UID", "notnum")
        with patch("os.geteuid", return_value=0), patch("os.chown") as chown:
            paths.hand_back(leaf)
        with patch("os.geteuid", return_value=self.USER), patch("os.chown") as plain:
            paths.hand_back(leaf)
        assert chown.call_count == plain.call_count == 0
