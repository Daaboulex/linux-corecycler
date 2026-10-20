from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
fit = importlib.import_module("fit")

ROOMY = {"MemTotal": 32000, "MemAvailable": 30000}


def test_the_meminfo_text_reads_in_mib():
    text = "MemTotal:       32548096 kB\nMemAvailable:   14486592 kB\nHugePages_Total:       0\n"
    assert fit.meminfo(text) == {"MemTotal": 31785, "MemAvailable": 14147, "HugePages_Total": 0}


HOLDS_MEMORY_UNDER_A_CUT_NAME = """
import sys, time
with open("/proc/self/comm", "wb") as comm:
    comm.write(b"\\xe2\\x82x) R 1 2")
held = bytearray(64 * 1024 * 1024)
held[::4096] = b"x" * len(held[::4096])
print(flush=True)
time.sleep(30)
"""


def test_the_memory_of_a_process_whose_name_is_not_utf8_is_read():
    child = subprocess.Popen([sys.executable, "-c", HOLDS_MEMORY_UNDER_A_CUT_NAME], stdout=subprocess.PIPE)
    try:
        child.stdout.readline()
        assert Path(f"/proc/{child.pid}/comm").read_bytes() == b"\xe2\x82x) R 1 2\n"
        held = fit._rss_mb(child.pid)
    finally:
        child.kill()
        child.wait()
    assert held >= 64, held


@pytest.mark.parametrize(
    ("available", "most", "expected"), [(20000, None, 4), (60000, None, 10), (3000, None, 1), (20000, 1, 1)]
)
def test_the_worker_count_follows_the_cores_and_the_memory_above_the_floor(available, most, expected):
    assert fit.workers({"MemTotal": 32000, "MemAvailable": available}, 10, 4000, most) == expected


def scripted(monkeypatch, steps):
    calls = []

    def step(cmd, cwd, env, timeout, sink, limit):
        kind, code, peak, lowest, text = steps[len(calls)]
        calls.append(cmd)
        sink.write(text)
        return kind, code, peak, lowest

    monkeypatch.setattr(fit, "_step", step)
    monkeypatch.setattr(fit, "_room", lambda peak: ROOMY)
    monkeypatch.setattr(fit, "learned_peak_mb", 4000)
    monkeypatch.setattr(fit.os, "cpu_count", lambda: 10)
    return calls


def spread(cmd):
    return int(cmd[cmd.index("-n") + 1]) if "-n" in cmd else 1


def test_a_step_stopped_for_memory_gives_no_verdict_and_runs_again_sized_to_the_peak_it_saw(monkeypatch, tmp_path):
    calls = scripted(monkeypatch, [("short", None, 9000, 2000, "partial"), ("done", 0, 3000, 20000, "1 passed")])
    assert fit.run(["t.py"], 60, tmp_path) == (True, "1 passed")
    assert [spread(cmd) for cmd in calls] == [6, 2]


@pytest.mark.parametrize(("lowest", "runs", "verdict"), [(5000, 2, True), (20000, 1, False)])
def test_a_crashed_worker_counts_as_a_memory_stop_only_when_memory_ran_short(
    monkeypatch, tmp_path, lowest, runs, verdict
):
    crashed = ("done", 1, 3000, lowest, "[gw2] node down: Not properly terminated")
    calls = scripted(monkeypatch, [crashed, ("done", 0, 3000, 20000, "1 passed")])
    assert fit.run(["t.py"], 60, tmp_path)[0] is verdict
    assert len(calls) == runs


def test_a_worker_that_takes_half_the_room_by_itself_fails_the_step_without_a_rerun(monkeypatch, tmp_path):
    calls = scripted(monkeypatch, [("short", None, 13400, 2000, "partial")])
    passed, output = fit.run(["t.py"], 60, tmp_path)
    assert (passed, len(calls), fit.learned_peak_mb) == (False, 1, 4000)
    assert output.endswith("the test selection outgrew its memory: one worker held 13400 MiB of 26800 MiB")


def test_a_worker_past_ten_times_the_measured_peak_fails_its_step_at_once_and_teaches_nothing(monkeypatch, tmp_path):
    calls = scripted(monkeypatch, [("outgrew", None, 41000, 20000, "partial")])
    passed, output = fit.run(["t.py"], 60, tmp_path)
    assert (passed, len(calls), fit.learned_peak_mb) == (False, 1, 4000)
    assert output.endswith("one worker held 41000 MiB, over 10 times the 4000 MiB measured")


@pytest.mark.parametrize(("limit", "kind"), [(50, "outgrew"), (None, "done")])
def test_a_real_worker_over_its_limit_is_stopped_before_it_finishes(monkeypatch, tmp_path, limit, kind):
    monkeypatch.setattr(fit, "_memory", lambda: ROOMY)
    hog = [sys.executable, "-c", "import time; held = bytearray(300 * 1024 * 1024); time.sleep(3)"]
    with tempfile.TemporaryFile("w+") as sink:
        assert fit._step(hog, tmp_path, None, 60, sink, limit)[0] == kind


def test_the_first_measured_peak_replaces_the_starting_guess_and_later_ones_only_raise_it(monkeypatch, tmp_path):
    calls = scripted(monkeypatch, [("done", 0, 1000, 20000, "")] + [("done", 0, 500, 20000, "")] * 2)
    monkeypatch.setattr(fit, "learned_peak_mb", None)
    monkeypatch.setattr(fit, "FIRST_PEAK_MB", 4096)
    for _ in range(3):
        fit.run(["t.py"], 60, tmp_path)
    assert [spread(cmd) for cmd in calls] == [6, 10, 10]
    assert fit.learned_peak_mb == 1000


def test_three_stops_at_one_worker_fail_by_name_with_no_verdict(monkeypatch, tmp_path):
    calls = scripted(monkeypatch, [("short", None, 3000, 100, "")] * 3)
    with pytest.raises(fit.Starved, match="stopped 3 times at one worker"):
        fit.run(["t.py"], 60, tmp_path, most=1)
    assert len(calls) == 3


def test_a_step_past_its_time_fails_with_its_output_and_the_reason(monkeypatch, tmp_path):
    scripted(monkeypatch, [("late", None, 3000, 20000, "partial")])
    assert fit.run(["t.py"], 60, tmp_path) == (False, "partial\nthe test selection did not finish within 60 s")


def test_waiting_for_room_says_why_once_and_returns_when_a_worker_fits(monkeypatch, capsys):
    readings = iter([{"MemTotal": 32000, "MemAvailable": 5000}] * 2 + [{"MemTotal": 32000, "MemAvailable": 9000}])
    monkeypatch.setattr(fit, "_memory", lambda: next(readings))
    monkeypatch.setattr(fit.time, "sleep", lambda seconds: None)
    assert fit._room(4000)["MemAvailable"] == 9000
    assert capsys.readouterr().err.count("fit: waiting: 5000 MiB free") == 1


def grandchild_pid(tmp_path):
    marker = tmp_path / "pid"
    inner = f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    return marker, [sys.executable, "-c", f"import subprocess, sys; subprocess.run([sys.executable, '-c', {inner!r}])"]


@pytest.mark.parametrize(("available", "timeout", "kind"), [(1000, 60, "short"), (30000, 1, "late")])
def test_a_stopped_step_takes_its_whole_process_group_with_it(monkeypatch, tmp_path, available, timeout, kind):
    marker, cmd = grandchild_pid(tmp_path)
    monkeypatch.setattr(
        fit, "_memory", lambda: {"MemTotal": 32000, "MemAvailable": available if marker.exists() else 30000}
    )
    started = time.monotonic()
    with tempfile.TemporaryFile("w+") as sink:
        result = fit._step(cmd, tmp_path, None, timeout, sink)
    assert result[0:2] == (kind, None)
    assert time.monotonic() - started < timeout + fit.GRACE_S + 5
    for _ in range(40):
        if not marker.exists() or not Path(f"/proc/{marker.read_text()}").exists():
            break
        time.sleep(0.05)
    assert not Path(f"/proc/{marker.read_text()}").exists()


def test_a_real_step_runs_its_workers_low_and_first_to_kill_and_returns_pytest_s_verdict(monkeypatch, tmp_path):
    monkeypatch.setattr(fit, "learned_peak_mb", 64)
    adj = (
        "import os\nfrom pathlib import Path\n\n\n"
        "def test_adj():\n    assert Path('/proc/self/oom_score_adj').read_text() == '1000\\n'\n"
        f"    assert os.nice(0) == {min(19, os.nice(0) + fit.NICE)}\n"
    )
    (tmp_path / "test_adj.py").write_text(adj)
    (tmp_path / "test_red.py").write_text("def test_red():\n    assert 1 == 2\n")
    assert fit.run(["-q", "-p", "no:cacheprovider", "test_adj.py"], 120, tmp_path, most=1)[0] is True
    passed, output = fit.run(["-q", "-p", "no:cacheprovider", "test_red.py"], 120, tmp_path, most=1)
    assert (passed, "assert 1 == 2" in output) == (False, True)
