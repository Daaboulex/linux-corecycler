from __future__ import annotations

import pytest

from corecycler.engine.backends.base import StressConfig
from corecycler.engine.backends.ycruncher import YCruncherBackend
from tests.test_backends import (
    _BULLDOZER_FLAGS,
    _ZEN5_FLAGS,
    _config_fields,
    _install,
    _on_cpu,
    _prepared,
)

ONE_T = StressConfig(cpus=(3,), tests=("SFTv4", "FFTv4", "N63"), seconds_per_test=20)


def test_a_named_test_list_and_seconds_per_test_reach_the_stress_config(tmp_path, monkeypatch):
    launcher = _install(tmp_path / "yc")
    _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN5_FLAGS)
    _, work_dir = _prepared(launcher, tmp_path, ONE_T)
    text = (work_dir / "stress.cfg").read_text()
    assert _config_fields(work_dir)["SecondsPerTest"] == "20"
    assert [tag for tag in ("BKT", "SFTv4", "FFTv4", "N63", "VT3") if f'"{tag}"' in text] == ["SFTv4", "FFTv4", "N63"]


def test_an_unknown_test_name_is_refused_before_anything_runs(tmp_path, monkeypatch):
    launcher = _install(tmp_path / "yc")
    _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN5_FLAGS)
    with pytest.raises(ValueError, match="Lucas"):
        _prepared(launcher, tmp_path, StressConfig(cpus=(3,), tests=("Lucas",)))


def test_a_named_binary_is_used_instead_of_the_auto_choice(tmp_path, monkeypatch):
    launcher = _install(tmp_path / "yc")
    _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", _ZEN5_FLAGS)
    backend, work_dir = _prepared(launcher, tmp_path, StressConfig(cpus=(3, 19), binary="19-ZN2 ~ Kagari"))
    assert backend.get_command(StressConfig(cpus=(3, 19)), work_dir)[0].endswith("19-ZN2 ~ Kagari")
    (tmp_path / "auto").mkdir()
    auto, auto_dir = _prepared(launcher, tmp_path / "auto", StressConfig(cpus=(3,)))
    assert auto.get_command(StressConfig(cpus=(3,)), auto_dir)[0].endswith("24-ZN5 ~ Komari")


@pytest.mark.parametrize(
    ("flags", "installed", "name", "says"),
    [
        (_ZEN5_FLAGS, None, "99-XX ~ Nobody", "no binary named"),
        (_BULLDOZER_FLAGS, None, "19-ZN2 ~ Kagari", "lacks"),
        (_ZEN5_FLAGS, ("24-ZN5 ~ Komari",), "19-ZN2 ~ Kagari", "is not in"),
    ],
)
def test_a_named_binary_that_cannot_run_here_is_refused_naming_why(tmp_path, monkeypatch, flags, installed, name, says):
    launcher = _install(tmp_path / "yc", installed) if installed else _install(tmp_path / "yc")
    _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", flags)
    with pytest.raises(OSError, match=says):
        _prepared(launcher, tmp_path, StressConfig(cpus=(3,), binary=name))


@pytest.mark.req("K9.capability")
@pytest.mark.parametrize(
    ("flags", "installed", "name", "says"),
    [
        (_ZEN5_FLAGS, None, "19-ZN2 ~ Kagari", None),
        (_ZEN5_FLAGS, None, "99-XX ~ Nobody", "no binary named"),
        (_BULLDOZER_FLAGS, None, "19-ZN2 ~ Kagari", "lacks"),
        (_ZEN5_FLAGS, ("24-ZN5 ~ Komari",), "19-ZN2 ~ Kagari", "is not in"),
    ],
)
@pytest.mark.req("D51.capability")
def test_a_binary_s_problem_is_known_before_any_test_runs(tmp_path, monkeypatch, flags, installed, name, says):
    launcher = _install(tmp_path / "yc", installed) if installed else _install(tmp_path / "yc")
    _on_cpu(monkeypatch, tmp_path, "AuthenticAMD", flags)
    backend = YCruncherBackend()
    backend._binary = str(launcher)
    problem = backend.binary_problem(name)
    assert problem is None if says is None else says in problem
