"""The stress work root is per-user, never a shared /tmp landmine."""

from __future__ import annotations

from pathlib import Path

from corecycler.config import paths
from corecycler.config.settings import load_settings


class TestResolveWorkDir:
    def test_a_configured_path_wins(self):
        assert paths.resolve_work_dir("/scratch/cc") == Path("/scratch/cc")

    def test_the_runtime_dir_is_used_when_it_belongs_to_us(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert paths.resolve_work_dir() == tmp_path / "corecycler" / "work"

    def test_a_foreign_runtime_dir_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        monkeypatch.setattr(paths.os, "geteuid", lambda: 999999)
        resolved = paths.resolve_work_dir()
        assert resolved == paths.user_home() / ".cache" / "corecycler" / "work"

    def test_no_runtime_dir_falls_back_to_the_user_cache(self, monkeypatch):
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        resolved = paths.resolve_work_dir()
        assert resolved == paths.user_home() / ".cache" / "corecycler" / "work"
        assert "/tmp/corecycler" not in str(resolved)

    def test_ensure_creates_the_tree_and_repairs_ownership(self, tmp_path, monkeypatch):
        chowned: list[Path] = []
        monkeypatch.setattr(paths, "fix_sudo_ownership", lambda *p: chowned.extend(p))
        work = paths.ensure_work_dir(str(tmp_path / "deep" / "work"))
        assert work.is_dir()
        assert work in chowned


class TestOldDefaultMigration:
    def test_the_old_tmp_default_reads_as_auto(self, tmp_path, monkeypatch):
        monkeypatch.setattr("corecycler.config.settings.CONFIG_DIR", tmp_path)
        (tmp_path / "settings.json").write_text('{"work_dir": "/tmp/corecycler"}')
        assert load_settings().work_dir == ""

    def test_a_deliberate_custom_dir_survives_loading(self, tmp_path, monkeypatch):
        monkeypatch.setattr("corecycler.config.settings.CONFIG_DIR", tmp_path)
        (tmp_path / "settings.json").write_text('{"work_dir": "/scratch/cc"}')
        assert load_settings().work_dir == "/scratch/cc"

    def test_a_profileless_settings_file_is_not_destroyed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("corecycler.config.settings.CONFIG_DIR", tmp_path)
        (tmp_path / "settings.json").write_text('{"theme": "dark"}')
        loaded = load_settings()
        assert loaded.theme == "dark"
        assert len(loaded.profiles) == 1
        assert (tmp_path / "settings.json").exists()
        assert not (tmp_path / "settings.json.corrupt").exists()
