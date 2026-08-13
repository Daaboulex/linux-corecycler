"""External tool resolution -- order, refusals, and the issue #12 regression.

The regression that matters: y-cruncher extracted into $HOME, PATH scrubbed to
sudo's secure_path. Resolution must report it absent (never guess), discovery
must find it, and recording it must make it resolve from then on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from corecycler.config import tools

_REAL_SEARCH_ROOTS = tools.search_roots

SECURE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def home(exec_tmp_path, monkeypatch, tool_search_roots):
    """A fake invoking-user home, and search roots that look only there."""
    fake = exec_tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(tools, "user_home", lambda: fake)
    tool_search_roots.append(fake)
    return fake


class TestEnvironmentVariableNames:
    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("mprime", "CORECYCLER_MPRIME_BIN"),
            ("y-cruncher", "CORECYCLER_Y_CRUNCHER_BIN"),
            ("stress-ng", "CORECYCLER_STRESS_NG_BIN"),
            ("notify-send", "CORECYCLER_NOTIFY_SEND_BIN"),
        ],
    )
    def test_name_is_the_key_uppercased_and_separated(self, key, expected):
        assert tools.env_var(key) == expected

    def test_every_tool_has_a_distinct_variable(self):
        names = [tools.env_var(key) for key in tools.TOOLS]
        assert len(names) == len(set(names))


class TestResolutionOrder:
    def test_environment_beats_path(self, exec_tmp_path, on_path, monkeypatch):
        on_path({"mprime": "/usr/bin/mprime"})
        pinned = _executable(exec_tmp_path / "pinned-mprime")
        monkeypatch.setenv("CORECYCLER_MPRIME_BIN", str(pinned))
        assert tools.resolve("mprime") == tools.Resolution("mprime", pinned, tools.ORIGIN_ENV)

    def test_configured_path_beats_path(self, exec_tmp_path, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        chosen = _executable(exec_tmp_path / "chosen-mprime")
        tools.set_configured_paths({"mprime": str(chosen)})
        resolution = tools.resolve("mprime")
        assert (resolution.path, resolution.origin) == (chosen, tools.ORIGIN_CONFIG)

    def test_environment_beats_configured_path(self, exec_tmp_path, on_path, monkeypatch):
        on_path({})
        pinned = _executable(exec_tmp_path / "pinned")
        chosen = _executable(exec_tmp_path / "chosen")
        monkeypatch.setenv("CORECYCLER_MPRIME_BIN", str(pinned))
        tools.set_configured_paths({"mprime": str(chosen)})
        assert tools.resolve("mprime").path == pinned

    def test_path_is_searched_in_declared_name_order(self, on_path):
        on_path({"y_cruncher": "/bin/y_cruncher"})
        resolution = tools.resolve("y-cruncher")
        assert (str(resolution.path), resolution.origin) == ("/bin/y_cruncher", tools.ORIGIN_PATH)

    def test_absent_says_so_and_offers_no_path(self, on_path):
        on_path({})
        resolution = tools.resolve("stress-ng")
        assert resolution.path is None
        assert resolution.origin == tools.ORIGIN_ABSENT
        assert resolution.problem == "not found on PATH"


class TestExplicitPathsAreRefusedNotReplaced:
    def test_missing_environment_target_is_refused(self, tmp_path, on_path, monkeypatch):
        on_path({"mprime": "/usr/bin/mprime"})
        monkeypatch.setenv("CORECYCLER_MPRIME_BIN", str(tmp_path / "nope"))
        resolution = tools.resolve("mprime")
        assert resolution.path is None, "a broken override must never fall back to PATH"
        assert "refused" in resolution.problem
        assert resolution.origin == tools.ORIGIN_ENV

    def test_non_executable_configured_target_is_refused(self, tmp_path, on_path):
        on_path({"mprime": "/usr/bin/mprime"})
        plain = tmp_path / "not-executable"
        plain.write_text("")
        tools.set_configured_paths({"mprime": str(plain)})
        resolution = tools.resolve("mprime")
        assert resolution.path is None
        assert "not an executable file" in resolution.problem

    def test_a_directory_is_not_a_binary(self, tmp_path, on_path, monkeypatch):
        on_path({})
        monkeypatch.setenv("CORECYCLER_MPRIME_BIN", str(tmp_path))
        assert tools.resolve("mprime").path is None


class TestConfiguredPaths:
    def test_unknown_keys_are_dropped(self):
        tools.set_configured_paths({"mprime": "/a", "not-a-tool": "/b"})
        assert tools.configured_paths() == {"mprime": "/a"}

    def test_installing_replaces_the_previous_set(self):
        tools.set_configured_paths({"mprime": "/a"})
        tools.set_configured_paths({"stress-ng": "/b"})
        assert tools.configured_paths() == {"stress-ng": "/b"}


class TestDiscovery:
    def test_finds_an_extracted_tarball_in_home(self, home):
        binary = _executable(home / "y-cruncher" / "y-cruncher")
        assert tools.discover("y-cruncher") == [binary]

    def test_finds_the_versioned_directory_upstream_ships(self, home):
        binary = _executable(home / "y-cruncher v0.8.7.9547-static" / "y-cruncher")
        assert tools.discover("y-cruncher") == [binary]

    def test_newest_versioned_directory_is_offered_first(self, home):
        _executable(home / "y-cruncher v0.8.6.9545-static" / "y-cruncher")
        newest = _executable(home / "y-cruncher v0.8.7.9547-static" / "y-cruncher")
        assert tools.discover("y-cruncher")[0] == newest

    def test_a_directory_named_like_the_binary_is_not_a_candidate(self, home):
        (home / "y-cruncher").mkdir()
        assert tools.discover("y-cruncher") == []

    def test_a_non_executable_file_is_not_a_candidate(self, home):
        (home / "y-cruncher").write_text("tarball")
        assert tools.discover("y-cruncher") == []

    def test_scan_is_bounded(self, home):
        for i in range(5):
            _executable(home / f"y-cruncher-{i}" / "y-cruncher")
        assert len(tools.discover("y-cruncher", limit=2)) == 2

    def test_tools_the_distros_package_are_never_scanned_for(self, home):
        _executable(home / "stress-ng")
        assert tools.discover("stress-ng") == []

    def test_an_unreadable_root_is_skipped_not_fatal(self, home, monkeypatch):
        def deny(self, pattern):
            raise PermissionError(pattern)

        monkeypatch.setattr(Path, "glob", deny)
        assert tools.discover("y-cruncher") == []

    def test_search_roots_follow_the_invoking_user(self, home):
        roots = _REAL_SEARCH_ROOTS()
        assert roots[0] == home
        assert home / "Downloads" in roots


class TestRequirements:
    def _resolutions(self, present):
        return [
            tools.Resolution(
                key,
                Path(f"/usr/bin/{key}") if key in present else None,
                tools.ORIGIN_PATH if key in present else tools.ORIGIN_ABSENT,
                None if key in present else "not found on PATH",
            )
            for key in tools.TOOLS
        ]

    def test_one_backend_and_the_containment_tools_are_enough(self):
        assert tools.unmet_requirements(self._resolutions({"stress-ng", "systemd-run", "setpriv"})) == []

    def test_no_backend_is_unmet(self):
        unmet = tools.unmet_requirements(self._resolutions({"systemd-run", "setpriv"}))
        assert len(unmet) == 1
        assert "no stress backend" in unmet[0]

    def test_missing_containment_tools_are_unmet(self):
        unmet = tools.unmet_requirements(self._resolutions({"mprime"}))
        assert any(u.startswith("systemd-run is required") for u in unmet)
        assert any(u.startswith("setpriv is required") for u in unmet)

    def test_optional_tools_are_never_unmet(self):
        assert tools.unmet_requirements(self._resolutions({"mprime", "systemd-run", "setpriv"})) == []


class TestCommandConstruction:
    def test_resolved_tools_run_by_absolute_path(self, on_path):
        on_path({"setpriv": "/usr/bin/setpriv"})
        assert tools.command_name("setpriv") == "/usr/bin/setpriv"

    def test_absent_tool_keeps_its_name_so_exec_fails_naming_it(self, on_path):
        on_path({})
        assert tools.command_name("setpriv") == "setpriv"

    def test_report_covers_every_tool(self, on_path):
        on_path({})
        assert [r.key for r in tools.report()] == list(tools.TOOLS)


class TestIssue12SudoScrubsThePath:
    """y-cruncher on the user's PATH, then sudo replaces PATH with secure_path."""

    @pytest.fixture
    def extracted(self, home, monkeypatch):
        monkeypatch.setenv("PATH", SECURE_PATH)
        return _executable(home / "y-cruncher" / "y-cruncher")

    def test_the_scrubbed_path_cannot_find_it(self, extracted):
        assert tools.resolve("y-cruncher").path is None

    def test_discovery_finds_it_in_the_invoking_users_home(self, extracted):
        assert tools.discover("y-cruncher") == [extracted]

    def test_recording_it_makes_it_resolve_under_sudo(self, extracted):
        tools.set_configured_paths({"y-cruncher": str(extracted)})
        resolution = tools.resolve("y-cruncher")
        assert resolution.path == extracted
        assert resolution.origin == tools.ORIGIN_CONFIG


class TestAnUnregisteredTool:
    """A backend with no registry entry degrades to absent, never a crash."""

    def test_resolution_says_it_is_not_registered(self):
        resolution = tools.resolve("not-a-tool")
        assert resolution.path is None
        assert resolution.problem == "not a registered external tool"

    def test_nothing_is_discovered_for_it(self, home):
        _executable(home / "not-a-tool")
        assert tools.discover("not-a-tool") == []

    def test_it_still_has_a_command_name(self):
        assert tools.command_name("not-a-tool") == "not-a-tool"


class TestTheRecordedPathsFile:
    @pytest.fixture
    def recorded(self, tmp_path, monkeypatch):
        target = tmp_path / "config" / "tool-paths.json"
        monkeypatch.setattr(tools, "paths_file", lambda: target)
        return target

    def test_recording_writes_and_installs_it(self, recorded, exec_tmp_path):
        binary = _executable(exec_tmp_path / "y-cruncher")
        tools.record_path("y-cruncher", str(binary))
        assert json.loads(recorded.read_text()) == {"y-cruncher": str(binary)}
        assert tools.configured_paths() == {"y-cruncher": str(binary)}

    def test_recording_keeps_the_other_tools(self, recorded, exec_tmp_path):
        tools.record_path("y-cruncher", str(_executable(exec_tmp_path / "y-cruncher")))
        tools.record_path("mprime", str(_executable(exec_tmp_path / "mprime")))
        assert set(json.loads(recorded.read_text())) == {"y-cruncher", "mprime"}

    def test_an_absent_file_is_not_an_error(self, recorded):
        assert tools.load_configured_paths() == {}

    def test_a_corrupt_file_is_ignored_not_obeyed(self, recorded):
        recorded.parent.mkdir(parents=True)
        recorded.write_text("{not json")
        assert tools.load_configured_paths() == {}

    def test_a_non_object_file_is_ignored(self, recorded):
        recorded.parent.mkdir(parents=True)
        recorded.write_text('["y-cruncher"]')
        assert tools.load_configured_paths() == {}

    def test_non_string_entries_are_dropped(self, recorded):
        recorded.parent.mkdir(parents=True)
        recorded.write_text('{"y-cruncher": 7, "mprime": "/usr/bin/mprime"}')
        assert tools.load_configured_paths() == {"mprime": "/usr/bin/mprime"}

    def test_it_lives_beside_the_other_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(tools, "user_home", lambda: tmp_path)
        assert tools.paths_file() == tmp_path / ".config" / "corecycler" / "tool-paths.json"
