"""config.paths sudo identity + ownership repair.

Under sudo, persistent state must resolve to the INVOKING user (never /root),
and root must never leave the files it created owned by root -- but the repair
must never raise, since a root-owned file beats losing the write.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


class TestAtomicWrite:
    def test_the_content_lands_synced_and_handed_back_with_no_temp_file_left(self, tmp_path, monkeypatch):
        synced, handed = [], []
        real_fsync = paths.os.fsync
        monkeypatch.setattr(paths.os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
        monkeypatch.setattr(paths, "hand_back", handed.append)
        target = tmp_path / "settings.json"
        paths.atomic_write(target, '{"a": 1}')
        assert target.read_text() == '{"a": 1}'
        assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]
        assert len(synced) == 2
        assert handed == [target]

    def test_a_temp_file_a_crash_left_does_not_block_the_next_write(self, tmp_path):
        target = tmp_path / "settings.json"
        (tmp_path / "settings.json.tmp").write_text("half a write")
        paths.atomic_write(target, "whole")
        assert target.read_text() == "whole"
        assert not (tmp_path / "settings.json.tmp").exists()

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp_file(self, tmp_path, monkeypatch):
        target = tmp_path / "settings.json"
        target.write_text("old")

        def refuse(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(paths.os, "replace", refuse)
        with pytest.raises(OSError, match="disk full"):
            paths.atomic_write(target, "new")
        assert target.read_text() == "old"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]

    def test_two_targets_in_one_folder_never_share_a_temp_file(self, tmp_path, monkeypatch):
        seen = []
        real_replace = paths.os.replace
        monkeypatch.setattr(paths.os, "replace", lambda src, dst: seen.append(Path(src).name) or real_replace(src, dst))
        paths.atomic_write(tmp_path / "profile.json", "a")
        paths.atomic_write(tmp_path / "profile.yaml", "b")
        assert seen == ["profile.json.tmp", "profile.yaml.tmp"]


class TestHandedBackLog:
    def test_a_rollover_hands_the_new_log_file_back(self, tmp_path, monkeypatch):
        handed = []
        monkeypatch.setattr(paths, "hand_back", handed.append)
        target = tmp_path / "corecycler.log"
        handler = paths.HandedBackLog(target, maxBytes=10, backupCount=1)
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "a line longer than ten bytes", None, None)
        handler.emit(record)
        handler.emit(record)
        handler.close()
        assert handed == [target]
        assert (tmp_path / "corecycler.log.1").exists()


@pytest.mark.req("HANG.path")
@pytest.mark.parametrize("uid", [0, 1000, 60001])
def test_the_heartbeat_lives_in_the_user_s_runtime_tmpfs_whatever_home_and_xdg_say(tmp_path, monkeypatch, uid):
    monkeypatch.setenv("HOME", str(tmp_path / "moved-home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    assert paths.heartbeat_path(uid) == Path(f"/run/user/{uid}/corecycler/heartbeat.json")
