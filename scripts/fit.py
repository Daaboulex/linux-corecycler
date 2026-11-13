#!/usr/bin/env python3
"""Run pytest on as many workers as the cores and the free memory allow, decided again at every step.

Before a step it takes the cores and the memory free above the desktop's floor, divided by the
largest worker seen so far. While the step runs it reads free memory twice a second; when free
memory falls under the floor it stops the step, waits for room and runs the step again, smaller.
A step stopped that way gives no verdict. Every worker runs at a low priority, so other work keeps
the CPU, and is marked first to kill.

    python3 scripts/fit.py -- -q -m "not slow" tests/test_cli.py
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FLOOR_SHARE = 0.10
FIRST_PEAK_MB = 1024
READ_EVERY_S = 0.5
NICE = 10
GRACE_S = 5.0
STOPS_AT_ONE = 3
RUNAWAY_FACTOR = 10
WAIT_SAY_S = 60.0
CRASHED = ("node down", "replacing crashed worker")

learned_peak_mb: int | None = None


class Starved(RuntimeError):
    pass


def meminfo(text: str) -> dict[str, int]:
    return {key: int(value.split()[0]) // 1024 for key, value in (line.split(":", 1) for line in text.splitlines())}


def floor_mb(info: dict[str, int]) -> int:
    return int(info["MemTotal"] * FLOOR_SHARE)


def workers(info: dict[str, int], cores: int, peak_mb: int, most: int | None = None) -> int:
    room = (info["MemAvailable"] - floor_mb(info)) // max(peak_mb, 1)
    count = max(1, min(cores, room))
    return count if most is None else min(count, most)


def _memory() -> dict[str, int]:
    return meminfo(Path("/proc/meminfo").read_text())


def _tree(pid: int) -> list[int]:
    found, todo = [], [pid]
    while todo:
        current = todo.pop()
        found.append(current)
        for children in Path(f"/proc/{current}/task").glob("*/children"):
            try:
                todo.extend(int(child) for child in children.read_text().split())
            except OSError:
                continue
    return found


def _rss_mb(pid: int) -> int:
    try:
        status = Path(f"/proc/{pid}/status").read_bytes()
    except OSError:
        return 0
    return next((int(line.split()[1]) // 1024 for line in status.splitlines() if line.startswith(b"VmRSS:")), 0)


def _yield() -> None:
    Path("/proc/self/oom_score_adj").write_text("1000")
    os.nice(NICE)


def _say(text: str) -> None:
    print(f"fit: {text}", file=sys.stderr, flush=True)


def _stop(child: subprocess.Popen) -> None:
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(GRACE_S)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def _step(
    cmd: list[str], cwd: Path, env: dict | None, timeout: int, sink, limit_mb: int | None = None
) -> tuple[str, int | None, int, int]:
    started = time.monotonic()
    child = subprocess.Popen(
        cmd, cwd=cwd, env=env, stdout=sink, stderr=subprocess.STDOUT, start_new_session=True, preexec_fn=_yield
    )
    peak, lowest = 0, None
    while child.poll() is None:
        info = _memory()
        lowest = info["MemAvailable"] if lowest is None else min(lowest, info["MemAvailable"])
        peak = max(peak, *(_rss_mb(pid) for pid in _tree(child.pid)))
        if limit_mb is not None and peak > limit_mb:
            _stop(child)
            return "outgrew", None, peak, lowest
        if info["MemAvailable"] < floor_mb(info):
            _stop(child)
            return "short", None, peak, lowest
        if time.monotonic() - started > timeout:
            _stop(child)
            return "late", None, peak, lowest
        time.sleep(READ_EVERY_S)
    info = _memory()
    return "done", child.returncode, peak, info["MemAvailable"] if lowest is None else lowest


def _room(peak_mb: int) -> dict[str, int]:
    waited = 0.0
    while True:
        info = _memory()
        if info["MemAvailable"] - floor_mb(info) >= peak_mb:
            return info
        if waited % WAIT_SAY_S == 0:
            _say(f"waiting: {info['MemAvailable']} MiB free, a worker needs {peak_mb} MiB above the floor")
        time.sleep(READ_EVERY_S)
        waited += READ_EVERY_S


def run(args: list[str], timeout: int, cwd: Path, env: dict | None = None, most: int | None = None) -> tuple[bool, str]:
    global learned_peak_mb
    stops_at_one = 0
    while True:
        per_worker = FIRST_PEAK_MB if learned_peak_mb is None else learned_peak_mb
        info = _room(per_worker)
        room = info["MemAvailable"] - floor_mb(info)
        count = workers(info, os.cpu_count() or 1, per_worker, most)
        _say(
            f"{count} workers: {info['MemAvailable']} MiB free, the floor {floor_mb(info)} MiB,"
            f" {per_worker} MiB per worker"
        )
        spread = ["-n", str(count), "--dist", "worksteal"] if count > 1 else []
        limit = None if learned_peak_mb is None else RUNAWAY_FACTOR * learned_peak_mb
        with tempfile.TemporaryFile("w+") as sink:
            kind, code, peak, lowest = _step(
                [sys.executable, "-m", "pytest", *spread, *args], cwd, env, timeout, sink, limit
            )
            sink.seek(0)
            output = sink.read()
        if kind == "outgrew":
            return False, output + (
                f"\nthe test selection outgrew its memory: one worker held {peak} MiB,"
                f" over {RUNAWAY_FACTOR} times the {learned_peak_mb} MiB measured"
            )
        if kind == "short" and 2 * peak >= room:
            return False, output + f"\nthe test selection outgrew its memory: one worker held {peak} MiB of {room} MiB"
        learned_peak_mb = peak if learned_peak_mb is None else max(learned_peak_mb, peak)
        if kind == "late":
            return False, output + f"\nthe test selection did not finish within {timeout} s"
        short = kind == "short" or (
            code != 0 and any(mark in output for mark in CRASHED) and lowest < 2 * floor_mb(info)
        )
        if not short:
            return code == 0, output
        _say(f"stopped at {count} workers: free memory fell to {lowest} MiB, the floor is {floor_mb(info)} MiB")
        if count == 1:
            stops_at_one += 1
            if stops_at_one == STOPS_AT_ONE:
                raise Starved(f"stopped {STOPS_AT_ONE} times at one worker: free memory stays under the floor")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=7200)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    args = options.pytest_args[1:] if options.pytest_args[:1] == ["--"] else options.pytest_args
    try:
        passed, output = run(args, options.timeout, Path.cwd())
    except Starved as starved:
        _say(str(starved))
        return 3
    print(output, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
