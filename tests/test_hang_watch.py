"""corecycler hang-watch: a running test whose heartbeat stopped while its process lives resets the machine."""

from __future__ import annotations

import errno
import itertools
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from corecycler import cli
from corecycler.config import paths
from corecycler.smu import writer_lock
from tests.test_execution import within

NOW = 1000.0
STALE = 120.0
EVERY = 15.0
TEST = "search 1t on CPUs 3,19"
PID = 4242
STARTED = "4242000"
LIVE = ("S", STARTED)
DIRECTORY = object()
BECAME_UNWRITABLE = "hangReset grants it"


class Ticks:
    def __init__(self, count: int = 1, between=()) -> None:
        self.left = count
        self.between = list(between)
        self.waits: list[float] = []
        self.clock: Clock | None = None
        self.settle = lambda: None

    def wait(self, seconds: float) -> bool:
        self.settle()
        self.waits.append(seconds)
        if self.clock is not None:
            self.clock.pause(seconds)
        self.left -= 1
        if self.between:
            self.between.pop(0)()
        return self.left <= 0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now

    def pause(self, seconds: float) -> None:
        self.now += seconds


def beat(**fields) -> str:
    return json.dumps({"state": "running", "test": TEST, "pid": PID, "started": STARTED, "at": 0.0, **fields})


def between(**fields) -> str:
    return json.dumps({"state": "between", "pid": PID, "started": STARTED, "at": 0.0, **fields})


class Disk:
    """The write the watch makes to prove the disk answers: while outcomes are given, it fails with the next error or
    lands at once for a None; once none is left it lands at once, or hangs until released when it hangs. Before each
    tick the watch waits on, the write each check started has arrived while outcomes are given, and every write that
    answers has answered."""

    def __init__(self, errors=(), hangs: bool = False) -> None:
        self.errors = list(errors)
        self.given = len(self.errors)
        self.settled = 0
        self.hangs = hangs
        self.asked: list[Path] = []
        self.threads: list[threading.Thread] = []
        self.held: set[threading.Thread] = set()
        self.released = threading.Event()

    def __call__(self, directory: Path) -> None:
        self.asked.append(directory)
        holds = self.hangs and not self.errors
        if holds:
            self.held.add(threading.current_thread())
        self.threads.append(threading.current_thread())
        if holds:
            self.released.wait(30)
            return
        error = self.errors.pop(0) if self.errors else None
        if error is not None:
            raise error

    def settle(self) -> None:
        self.settled += 1
        assert within(5.0, lambda: len(self.asked) >= min(self.settled, self.given)), (self.settled, self.asked)
        for thread in self.threads:
            if thread not in self.held:
                thread.join(5)


def beating(heartbeat: Path, contents) -> Ticks:
    """One check per content, each written afresh just before its check, its beat taken at that check's time."""
    put(heartbeat, contents[0](at=NOW))
    return Ticks(
        len(contents),
        [lambda k=k: heartbeat.write_text(contents[k](at=NOW + EVERY * k)) for k in range(1, len(contents))],
    )


def put(heartbeat: Path, content) -> Path:
    if content is DIRECTORY:
        heartbeat.mkdir()
    elif isinstance(content, bytes):
        heartbeat.write_bytes(content)
    elif content is not None:
        heartbeat.write_text(content)
    return heartbeat


def each(value):
    return iter(value) if isinstance(value, list) else itertools.repeat(value)


def watch(
    heartbeat: Path, *, found=LIVE, ready=True, ticks: Ticks | None = None, logging_takes=0.0, disk: Disk | None = None
):
    clock = Clock()
    disk = disk or Disk()
    seen = SimpleNamespace(keys=[], at={}, lines=[], said_after=[], probed=[])
    readiness, finds = each(ready), each(found)

    def state_of(pid: int):
        seen.probed.append(pid)
        return next(finds)

    def sysrq(key: str) -> None:
        seen.keys.append(key)
        seen.at[key] = clock()

    def say(line: str) -> None:
        seen.said_after.append(list(seen.keys))
        clock.pause(logging_takes)
        seen.lines.append(line)

    stop = ticks or Ticks()
    stop.clock = clock
    stop.settle = disk.settle
    cli.hang_watch(
        heartbeat,
        STALE,
        EVERY,
        clock=clock,
        state_of=state_of,
        sysrq=sysrq,
        sysrq_ready=lambda: next(readiness),
        probe=disk,
        say=say,
        pause=clock.pause,
        stop=stop,
    )
    return seen


@pytest.mark.req("HANG.watch", "HANG.reset")
@pytest.mark.parametrize("state", ["R", "S", "D"])
def test_a_running_test_silent_past_the_limit_on_two_checks_while_its_process_lives_resets_once(tmp_path, state):
    seen = watch(put(tmp_path / "heartbeat.json", beat(at=NOW - 300.0)), found=(state, STARTED), ticks=Ticks(2))
    assert seen.keys == ["s", "b"]
    assert seen.probed == [PID, PID]
    reset = seen.lines[-1]
    assert reset.startswith("<2>") and TEST in reset and str(PID) in reset and "315 s" in reset
    assert seen.said_after[-1] == ["s"]


@pytest.mark.req("HANG.watch", "HANG.between")
@pytest.mark.parametrize(("at", "resets"), [(NOW - 300.0, True), (NOW - 10.0, False)], ids=["stale", "fresh"])
def test_a_session_between_tests_resets_like_a_running_test_once_two_checks_find_its_beat_stale(tmp_path, at, resets):
    seen = watch(put(tmp_path / "heartbeat.json", between(at=at)), ticks=Ticks(2))
    assert seen.keys == (["s", "b"] if resets else [])
    assert seen.probed == [PID, PID]
    assert all(str(PID) in line for line in seen.lines), seen.lines


def driving(make, directory: Path):
    return lambda at: make(at=at, probe_dir=str(directory))


def idle(at: float) -> str:
    return json.dumps({"state": "idle", "pid": PID, "started": STARTED, "at": at})


@pytest.mark.req("HANG.disk")
@pytest.mark.parametrize("make", [beat, between], ids=["running", "between"])
@pytest.mark.parametrize(("checks", "resets"), [(10, False), (11, True)], ids=["silent past it once", "twice"])
def test_a_disk_that_stops_answering_while_a_session_drives_resets_once_two_checks_find_it_silent_past_the_limit(
    tmp_path, make, checks, resets
):
    directory = tmp_path / "history"
    disk = Disk(hangs=True)
    heartbeat = tmp_path / "heartbeat.json"
    try:
        seen = watch(heartbeat, ticks=beating(heartbeat, [driving(make, directory)] * checks), disk=disk)
    finally:
        disk.released.set()
    assert seen.keys == (["s", "b"] if resets else [])
    assert disk.asked == [directory]
    assert disk.threads[0].daemon and disk.threads[0] is not threading.main_thread()
    if resets:
        reset = seen.lines[-1]
        assert reset.startswith("<2>") and "the disk stopped answering" in reset and str(directory) in reset
        assert reset.endswith(", driving began 150 s ago") and str(PID) in reset, reset


@pytest.mark.req("HANG.disk", "HANG.reset")
def test_a_disk_that_answered_once_driving_began_and_then_stopped_is_reset_on_its_last_answer(tmp_path):
    directory = tmp_path / "history"
    disk = Disk(errors=[None], hangs=True)
    heartbeat = tmp_path / "heartbeat.json"
    try:
        seen = watch(heartbeat, ticks=beating(heartbeat, [driving(between, directory)] * 11), disk=disk)
    finally:
        disk.released.set()
    assert seen.keys == ["s", "b"]
    assert disk.asked == [directory, directory]
    assert seen.lines[-1].endswith(", its last answer 150 s ago"), seen.lines


NOT_DRIVING = {
    "idle": (idle, LIVE),
    "a stopped driver": (beat, ("T", STARTED)),
    "a gone driver": (between, None),
    "a reused pid": (between, ("S", "4243117")),
    "a zombie": (beat, ("Z", STARTED)),
}


@pytest.mark.req("HANG.disk")
@pytest.mark.parametrize(("make", "found"), list(NOT_DRIVING.values()), ids=list(NOT_DRIVING))
def test_no_disk_is_written_and_nothing_resets_while_no_session_drives(tmp_path, make, found):
    disk = Disk(hangs=True)
    heartbeat = tmp_path / "heartbeat.json"
    make_one = make if make is idle else driving(make, tmp_path / "history")
    try:
        seen = watch(heartbeat, found=found, ticks=beating(heartbeat, [make_one] * 12), disk=disk)
    finally:
        disk.released.set()
    assert seen.keys == []
    assert disk.asked == []
    assert seen.lines


@pytest.mark.req("HANG.disk")
def test_a_disk_write_that_fails_is_an_answer_said_once_per_change_and_never_resets(tmp_path):
    directory = tmp_path / "history"
    failed = OSError(errno.EIO, "Input/output error")
    disk = Disk(errors=[failed] * 12 + [None] * 3 + [failed] * 5)
    heartbeat = tmp_path / "heartbeat.json"
    seen = watch(heartbeat, ticks=beating(heartbeat, [driving(between, directory)] * 20), disk=disk)
    assert seen.keys == []
    assert disk.asked == [directory] * 20
    told = [line for line in seen.lines if "Input/output error" in line]
    assert len(told) == 2 and all(str(directory) in line for line in told), seen.lines
    assert len(seen.lines) == 4, seen.lines


@pytest.mark.req("HANG.disk")
def test_a_disk_that_answers_before_the_second_check_does_not_reset(tmp_path):
    directory = tmp_path / "history"
    disk = Disk(hangs=True)
    heartbeat = tmp_path / "heartbeat.json"
    ticks = beating(heartbeat, [driving(beat, directory)] * 12)
    refresh = ticks.between[9]

    def answered() -> None:
        disk.released.set()
        disk.threads[0].join(5)
        refresh()

    ticks.between[9] = answered
    try:
        seen = watch(heartbeat, ticks=ticks, disk=disk)
    finally:
        disk.released.set()
    assert seen.keys == []
    assert any("the disk stopped answering" in line for line in seen.lines), seen.lines


@pytest.mark.req("HANG.disk")
def test_a_session_that_drives_again_gives_the_disk_its_whole_limit_again(tmp_path):
    directory = tmp_path / "history"
    disk = Disk(hangs=True)
    heartbeat = tmp_path / "heartbeat.json"
    contents = [driving(between, directory)] * 6 + [idle] * 2 + [driving(between, directory)] * 9
    try:
        seen = watch(heartbeat, ticks=beating(heartbeat, contents), disk=disk)
    finally:
        disk.released.set()
    assert seen.keys == []
    assert disk.asked == [directory]


@pytest.mark.req("HANG.watch")
@pytest.mark.parametrize(
    ("contents", "found", "resets"),
    [
        ([beat(at=NOW - 300.0), json.dumps({"state": "idle", "pid": PID, "at": NOW})], LIVE, False),
        ([beat(at=NOW - 300.0), beat(at=NOW + EVERY - 1.0)], LIVE, False),
        ([beat(at=NOW - 300.0), beat(at=NOW - 299.0)], LIVE, False),
        ([beat(at=NOW - 300.0)] * 2, [LIVE, ("T", STARTED)], False),
        ([beat(at=NOW - 300.0)] * 3, [LIVE, ("T", STARTED), LIVE], False),
        ([beat(at=NOW - 300.0)] * 2, LIVE, True),
        ([beat(at=NOW - 300.0)] * 3, [("T", STARTED), LIVE, LIVE], True),
    ],
    ids=[
        "stale then idle",
        "stale then fresh",
        "stale twice with another beat time",
        "stale then stopped",
        "stale, stopped, stale",
        "stale twice with the same beat time",
        "stopped, then stale twice",
    ],
)
def test_only_two_checks_an_interval_apart_that_find_the_same_beat_stale_reset(tmp_path, contents, found, resets):
    heartbeat = put(tmp_path / "heartbeat.json", contents[0])
    ticks = Ticks(len(contents), [lambda content=content: heartbeat.write_text(content) for content in contents[1:]])
    seen = watch(heartbeat, found=found, ticks=ticks)
    assert seen.keys == (["s", "b"] if resets else [])


@pytest.mark.req("HANG.reset")
@pytest.mark.parametrize("logging_takes", [0.0, 0.5, 1.9])
def test_the_reset_comes_two_seconds_after_the_sync_however_long_the_log_line_took(tmp_path, logging_takes):
    seen = watch(put(tmp_path / "heartbeat.json", beat(at=NOW - 300.0)), ticks=Ticks(2), logging_takes=logging_takes)
    assert seen.keys == ["s", "b"]
    assert seen.at["b"] - seen.at["s"] == pytest.approx(2.0, abs=1e-9)


@pytest.mark.req("HANG.log")
@pytest.mark.parametrize("logging_takes", [0.5, 1.9])
def test_the_log_gets_the_time_between_checks_and_the_next_check_still_comes_one_period_after_the_last(
    tmp_path, logging_takes
):
    ticks = Ticks(2)
    seen = watch(put(tmp_path / "heartbeat.json", beat(at=NOW - 300.0)), ticks=ticks, logging_takes=logging_takes)
    assert seen.lines[0] and seen.keys == ["s", "b"]
    assert ticks.waits[0] == pytest.approx(EVERY - logging_takes, abs=1e-9)
    assert seen.at["s"] == pytest.approx(NOW + EVERY, abs=1e-9)


@pytest.mark.req("HANG.reset")
def test_the_reset_line_is_said_at_critical_priority_and_no_other_line_is(tmp_path):
    seen = watch(put(tmp_path / "heartbeat.json", beat(at=NOW - 300.0)), ticks=Ticks(2))
    assert seen.keys == ["s", "b"]
    assert [line.startswith("<2>") for line in seen.lines] == [False, True]


@pytest.mark.req("HANG.watch")
def test_a_proven_hang_with_a_trigger_this_user_may_not_write_is_said_to_be_left_unreset(tmp_path):
    seen = watch(put(tmp_path / "heartbeat.json", beat(at=NOW - 300.0)), ready=False, ticks=Ticks(3))
    assert seen.keys == []
    unreset = [line for line in seen.lines if TEST in line and "not reset" in line]
    assert len(unreset) == 1 and str(cli.SYSRQ_TRIGGER) in unreset[0]


NO_RESET = {
    "fresh": (beat(at=NOW - 10.0), LIVE),
    "exactly the limit": (beat(at=NOW - STALE), LIVE),
    "idle": (json.dumps({"state": "idle", "pid": PID, "started": STARTED, "at": 0.0}), LIVE),
    "missing": (None, LIVE),
    "unreadable": (DIRECTORY, LIVE),
    "not json": ("{", LIVE),
    "not text": (b"\xff\xff", LIVE),
    "a list": ("[]", LIVE),
    "an unknown state": (beat(state="busy"), LIVE),
    "no test": (json.dumps({"state": "running", "pid": PID, "started": STARTED, "at": 0.0}), LIVE),
    "no start time": (json.dumps({"state": "running", "test": TEST, "pid": PID, "at": 0.0}), LIVE),
    "a start time the driver could not read": (beat(started=None), LIVE),
    "a pid that is true": (beat(pid=True), LIVE),
    "a pid in text": (beat(pid=str(PID)), LIVE),
    "a time in text": (beat(at="880.0"), LIVE),
    "a time of minus infinity": (beat(at=-math.inf), LIVE),
    "a time that is no number": (beat(at=math.nan), LIVE),
    "a directory to probe that is a number": (beat(probe_dir=5), LIVE),
    "a directory to probe that is relative": (beat(probe_dir="history"), LIVE),
    "between tests with no start time": (json.dumps({"state": "between", "pid": PID, "at": 0.0}), LIVE),
    "a gone pid": (beat(), None),
    "a reused pid": (beat(), ("S", "4243117")),
    "a zombie pid": (beat(), ("Z", STARTED)),
    "a dead pid": (beat(), ("X", STARTED)),
    "a stopped pid": (beat(), ("T", STARTED)),
    "a traced pid": (beat(), ("t", STARTED)),
}


@pytest.mark.req("HANG.watch")
@pytest.mark.parametrize(("content", "found"), list(NO_RESET.values()), ids=list(NO_RESET))
def test_nothing_but_a_proven_hang_resets_and_each_reason_is_said(tmp_path, content, found):
    seen = watch(put(tmp_path / "heartbeat.json", content), found=found, ticks=Ticks(2))
    assert seen.keys == []
    assert seen.lines and all(line and BECAME_UNWRITABLE not in line for line in seen.lines)


@pytest.mark.req("HANG.watch")
def test_each_reason_is_said_once_per_change_and_the_heartbeat_is_read_every_check(tmp_path):
    heartbeat = put(tmp_path / "heartbeat.json", json.dumps({"state": "idle", "pid": PID, "at": NOW}))
    ticks = Ticks(
        5,
        [
            lambda: None,
            lambda: heartbeat.write_text(beat(at=NOW - 1.0)),
            lambda: None,
            lambda: heartbeat.unlink(),
        ],
    )
    seen = watch(heartbeat, ticks=ticks)
    assert seen.keys == []
    assert len(seen.lines) == 3 and len(set(seen.lines)) == 3 and TEST in seen.lines[1]
    assert ticks.waits == [EVERY] * 5


@pytest.mark.req("HANG.watch")
def test_a_trigger_this_user_may_not_write_is_said_each_time_it_becomes_so_and_resets_only_while_writable(tmp_path):
    seen = watch(
        put(tmp_path / "heartbeat.json", beat(at=NOW - 300.0)), ready=[False, False, True, False], ticks=Ticks(4)
    )
    assert seen.keys == ["s", "b"]
    assert len([line for line in seen.lines if BECAME_UNWRITABLE in line]) == 2


@pytest.mark.req("HANG.reset")
def test_a_log_that_never_returns_still_gets_the_reset_at_two_seconds_after_the_sync(tmp_path):
    heartbeat = put(tmp_path / "heartbeat.json", beat(at=time.monotonic() - 300.0))
    at: dict[str, float] = {}
    stop = threading.Event()
    stuck = threading.Event()
    sinks: list[threading.Thread] = []

    def sysrq(key: str) -> None:
        at[key] = time.monotonic()
        if key == "b":
            stop.set()

    def say(line: str) -> None:
        if line.startswith("<2>"):
            sinks.append(threading.current_thread())
            stuck.wait()

    kwargs = {
        "clock": time.monotonic,
        "state_of": lambda _pid: ("D", STARTED),
        "sysrq": sysrq,
        "sysrq_ready": lambda: True,
        "probe": Disk(),
        "say": say,
        "pause": time.sleep,
        "stop": stop,
    }
    threading.Thread(target=cli.hang_watch, args=(heartbeat, STALE, 0.05), kwargs=kwargs, daemon=True).start()
    try:
        assert within(5.0, lambda: "b" in at), f"no reset: the trigger saw {sorted(at)}"
        assert 2.0 <= at["b"] - at["s"] < 3.0
        assert sinks and all(thread.daemon for thread in sinks)
    finally:
        stop.set()
        stuck.set()


@pytest.mark.req("HANG.log", "HANG.reset")
def test_a_log_that_blocks_on_every_line_holds_neither_a_check_nor_the_reset(tmp_path):
    heartbeat = put(tmp_path / "heartbeat.json", beat(at=time.monotonic() - 300.0))
    at: dict[str, float] = {}
    checked: list[float] = []
    stopped = threading.Event()
    stuck = threading.Event()

    class Stop:
        def wait(self, seconds: float) -> bool:
            checked.append(time.monotonic())
            return stopped.wait(seconds)

    def sysrq(key: str) -> None:
        at[key] = time.monotonic()
        if key == "b":
            stopped.set()

    kwargs = {
        "clock": time.monotonic,
        "state_of": lambda _pid: LIVE,
        "sysrq": sysrq,
        "sysrq_ready": lambda: True,
        "probe": Disk(),
        "say": lambda _line: stuck.wait(),
        "pause": time.sleep,
        "stop": Stop(),
    }
    threading.Thread(target=cli.hang_watch, args=(heartbeat, STALE, 0.05), kwargs=kwargs, daemon=True).start()
    try:
        assert within(5.0, lambda: "b" in at), f"no reset: {len(checked)} checks, the trigger saw {sorted(at)}"
        assert 2.0 <= at["b"] - at["s"] < 3.0
        assert checked and 0.0 <= at["s"] - checked[0] < 1.0, (checked, at)
    finally:
        stopped.set()
        stuck.set()


@pytest.mark.req("HANG.log")
def test_the_log_says_each_line_in_order_on_a_daemon_thread_of_its_own():
    said: list[tuple[str, threading.Thread]] = []
    log = cli.Log(lambda line: said.append((line, threading.current_thread())))
    for line in ("first", "second", "third"):
        log(line)
    assert within(5.0, lambda: len(said) == 3)
    assert [line for line, _ in said] == ["first", "second", "third"]
    assert all(thread.daemon and thread is not threading.current_thread() for _, thread in said)


@pytest.mark.req("HANG.log")
def test_a_line_the_blocked_log_has_no_room_for_is_dropped_and_counted_and_the_count_said_once_it_takes_lines():
    entered, held = threading.Event(), threading.Event()
    said: list[str] = []

    def sink(line: str) -> None:
        if line == "stuck":
            entered.set()
            held.wait(30)
        said.append(line)

    log = cli.Log(sink)
    log("stuck")
    assert entered.wait(5.0)
    began = time.monotonic()
    for n in range(cli.LOG_BACKLOG + 3):
        log(f"line {n}")
    assert time.monotonic() - began < 1.0
    held.set()
    assert within(5.0, lambda: len(said) == cli.LOG_BACKLOG)
    log("again")
    assert within(5.0, lambda: len(said) == cli.LOG_BACKLOG + 2)
    kept = [f"line {n}" for n in range(cli.LOG_BACKLOG - 1)]
    assert said[: cli.LOG_BACKLOG] == ["stuck", *kept]
    assert said[cli.LOG_BACKLOG :] == [said[cli.LOG_BACKLOG], "again"]
    assert said[cli.LOG_BACKLOG].startswith("4 ") and "dropped" in said[cli.LOG_BACKLOG]


@pytest.mark.req("HANG.log")
def test_settling_the_log_waits_for_its_lines_and_never_past_its_bound():
    held = threading.Event()
    log = cli.Log(lambda _line: held.wait(30))
    log("a line")
    began = time.monotonic()
    log.settle(0.2)
    assert time.monotonic() - began >= 0.2
    held.set()
    began = time.monotonic()
    log.settle(5.0)
    assert time.monotonic() - began < 1.0


def broken_pipe(_line: str) -> None:
    raise BrokenPipeError(errno.EPIPE, "Broken pipe")


@pytest.mark.req("HANG.log")
def test_a_line_the_log_fails_to_write_ends_its_settling_and_keeps_its_error_while_later_lines_raise_nothing():
    log = cli.Log(broken_pipe)
    log("first")
    began = time.monotonic()
    log.settle(5.0)
    assert time.monotonic() - began < 1.0
    log("second")
    assert isinstance(log.failed, BrokenPipeError)


@pytest.mark.req("HANG.log", "HANG.reset")
def test_a_log_that_failed_still_gets_the_reset_two_seconds_after_the_sync(tmp_path):
    clock = Clock()
    at: dict[str, float] = {}
    ticks = Ticks(2)
    ticks.clock = clock

    def sysrq(key: str) -> None:
        at[key] = clock()

    cli.hang_watch(
        put(tmp_path / "heartbeat.json", beat(at=NOW - 300.0)),
        STALE,
        EVERY,
        clock=clock,
        state_of=lambda _pid: LIVE,
        sysrq=sysrq,
        sysrq_ready=lambda: True,
        probe=Disk(),
        say=broken_pipe,
        pause=clock.pause,
        stop=ticks,
    )
    assert sorted(at) == ["b", "s"]
    assert at["s"] == NOW + EVERY and at["b"] - at["s"] == pytest.approx(2.0, abs=1e-9)


@pytest.mark.req("HANG.log", "HANG.disk")
def test_a_log_that_fails_every_line_still_resets_a_silent_disk_within_one_watch(tmp_path):
    directory = tmp_path / "history"
    disk = Disk(hangs=True)
    heartbeat = tmp_path / "heartbeat.json"
    ticks = beating(heartbeat, [driving(between, directory)] * 11)
    clock = Clock()
    ticks.clock = clock
    ticks.settle = disk.settle
    keys: list[str] = []
    try:
        cli.hang_watch(
            heartbeat,
            STALE,
            EVERY,
            clock=clock,
            state_of=lambda _pid: LIVE,
            sysrq=keys.append,
            sysrq_ready=lambda: True,
            probe=disk,
            say=broken_pipe,
            pause=clock.pause,
            stop=ticks,
        )
    finally:
        disk.released.set()
    assert keys == ["s", "b"]


@pytest.mark.req("HANG.log")
def test_a_log_that_failed_ends_the_watch_at_the_first_check_that_finds_no_session_driving(tmp_path):
    clock = Clock()
    ticks = Ticks(3)
    ticks.clock = clock
    with pytest.raises(BrokenPipeError):
        cli.hang_watch(
            put(tmp_path / "heartbeat.json", idle(NOW)),
            STALE,
            EVERY,
            clock=clock,
            state_of=lambda _pid: LIVE,
            sysrq=lambda _key: None,
            sysrq_ready=lambda: True,
            probe=Disk(),
            say=broken_pipe,
            pause=clock.pause,
            stop=ticks,
        )
    assert ticks.waits == []


@pytest.fixture
def wired(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "hang_watch", lambda *args, **kwargs: calls.append((args, kwargs)))
    return calls


@pytest.mark.req("HANG.path")
def test_the_command_under_sudo_watches_the_invoking_user_s_heartbeat(wired, monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_UID", "1234")
    assert cli.cli_main(["hang-watch"]) == cli.EXIT_COMPLETED
    [(args, _)] = wired
    assert args[0] == paths.heartbeat_path(1234)


@pytest.mark.req("HANG.watch")
def test_the_command_watches_this_user_s_heartbeat_with_the_real_clock_probe_trigger_and_log(
    wired, tmp_path, monkeypatch, capsys
):
    assert cli.cli_main(["hang-watch"]) == cli.EXIT_COMPLETED
    [(args, kwargs)] = wired
    assert args == (paths.heartbeat_path(paths.invoking_uid()), STALE, EVERY)
    before = time.clock_gettime(time.CLOCK_MONOTONIC)
    assert before <= kwargs["clock"]() <= time.clock_gettime(time.CLOCK_MONOTONIC)
    assert kwargs["state_of"](os.getpid())[0] == "R"
    paused = time.monotonic()
    kwargs["pause"](0.05)
    assert time.monotonic() - paused >= 0.05
    trigger = tmp_path / "sysrq-trigger"
    monkeypatch.setattr(cli, "SYSRQ_TRIGGER", tmp_path / "absent" / "sysrq-trigger")
    assert kwargs["sysrq_ready"]() is False
    trigger.write_text("")
    monkeypatch.setattr(cli, "SYSRQ_TRIGGER", trigger)
    assert kwargs["sysrq_ready"]() is True
    kwargs["sysrq"]("b")
    assert trigger.read_text() == "b"
    kwargs["say"]("a line")
    assert capsys.readouterr().err == "a line\n"
    assert kwargs["stop"].is_set() is False
    history = tmp_path / "history"
    history.mkdir()
    kwargs["probe"](history)
    assert list(history.iterdir()) == []


@pytest.mark.req("HANG.disk")
def test_the_disk_write_lands_and_is_synced_in_the_session_s_directory_and_leaves_nothing_behind(tmp_path, monkeypatch):
    synced: list[tuple[str, int]] = []
    sync = os.fsync

    def recording(handle: int) -> None:
        synced.append((os.readlink(f"/proc/self/fd/{handle}"), os.fstat(handle).st_size))
        sync(handle)

    monkeypatch.setattr(os, "fsync", recording)
    cli.write_probe(tmp_path)
    [(path, size)] = synced
    assert Path(path).parent == tmp_path and size > 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.req("HANG.disk")
def test_a_disk_write_that_fails_raises_and_leaves_nothing_behind(tmp_path, monkeypatch):
    def failing(_handle: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", failing)
    with pytest.raises(OSError, match="Input/output error"):
        cli.write_probe(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.req("HANG.disk")
def test_the_disk_write_never_follows_a_link_out_of_the_session_s_directory(tmp_path):
    history = tmp_path / "history"
    history.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("kept")
    (history / cli.PROBE_NAME).symlink_to(elsewhere)
    with pytest.raises(OSError):
        cli.write_probe(history)
    assert elsewhere.read_text() == "kept"


@pytest.mark.req("HANG.watch")
def test_the_command_takes_its_limit_and_period_in_seconds(wired):
    assert cli.cli_main(["hang-watch", "--stale-after", "0.5", "--every", "2"]) == cli.EXIT_COMPLETED
    assert wired[0][0][1:] == (0.5, 2.0)


@pytest.mark.req("HANG.watch")
@pytest.mark.parametrize("flag", ["--stale-after", "--every"])
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "x"])
def test_a_limit_or_period_that_is_not_a_positive_number_of_seconds_is_refused(wired, capsys, flag, value):
    assert cli.cli_main(["hang-watch", flag, value]) == cli.EXIT_REFUSED
    assert wired == []
    assert value in capsys.readouterr().err


def test_help_is_no_refusal_and_watches_nothing(wired, capsys):
    assert cli.cli_main(["hang-watch", "--help"]) == cli.EXIT_COMPLETED
    assert wired == []
    assert "--stale-after" in capsys.readouterr().out


RENAMES_ITSELF = """
import os, sys, time
open(sys.argv[1], "w").close()
while not os.path.exists(sys.argv[2]):
    time.sleep(0.01)
with open("/proc/self/comm", "wb") as comm:
    comm.write(bytes.fromhex(sys.argv[4]))
open(sys.argv[3], "w").close()
time.sleep(30)
"""


@pytest.mark.req("HANG.watch")
@pytest.mark.parametrize("name", [b"x) R 1 2 3", b"\xe2\x82x) R 1 2"], ids=["parentheses", "not utf-8"])
def test_the_process_probe_reads_the_state_and_start_time_after_the_last_parenthesis(tmp_path, name):
    named, go, renamed = (tmp_path / step for step in ("named", "go", "renamed"))
    child = subprocess.Popen([sys.executable, "-c", RENAMES_ITSELF, str(named), str(go), str(renamed), name.hex()])
    try:
        assert within(10.0, named.exists)
        plain = writer_lock.process_state(child.pid)
        assert plain is not None and plain[1].isdigit(), plain
        go.touch()
        assert within(10.0, renamed.exists)
        os.kill(child.pid, signal.SIGSTOP)
        os.waitpid(child.pid, os.WUNTRACED)
        assert Path(f"/proc/{child.pid}/comm").read_bytes() == name + b"\n"
        assert writer_lock.process_state(child.pid) == ("T", plain[1])
        os.kill(child.pid, signal.SIGKILL)
        assert within(5.0, lambda: (writer_lock.process_state(child.pid) or ("",))[0] == "Z"), (
            writer_lock.process_state(child.pid)
        )
    finally:
        child.kill()
        child.wait()
    assert writer_lock.process_state(child.pid) is None
