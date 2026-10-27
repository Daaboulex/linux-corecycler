from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from typing import TYPE_CHECKING

import pytest

from corecycler import cli
from corecycler.engine import execution
from corecycler.engine.backends.base import LaneSignal, Outcome
from corecycler.engine.execution import SuperviseHooks
from corecycler.smu import writer_lock
from tests.test_execution import FakeBackend, _child, lane, make_supervisor, run_one

if TYPE_CHECKING:
    from pathlib import Path

SLEEPS = "import time; time.sleep(30)"
FAILS = (LaneSignal(Outcome.COMPUTATION, "fake error: FATAL"),)


def monotonic() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def read(heartbeat: Path) -> dict | None:
    return json.loads(heartbeat.read_text()) if heartbeat.exists() else None


def watched(heartbeat: Path, status, found=()):
    supervisor, _, _ = make_supervisor(
        FakeBackend(_child(SLEEPS), found=found),
        phase="search 1t",
        hooks=SuperviseHooks(on_status=status),
        heartbeat=execution.Heartbeat(heartbeat, heartbeat.parent / "history"),
    )
    return supervisor


@pytest.mark.req("HANG.heartbeat")
@pytest.mark.parametrize(
    ("found", "breaks", "outcome"), [((), False, Outcome.PASS), (FAILS, False, Outcome.COMPUTATION), ((), True, None)]
)
def test_the_heartbeat_says_running_while_a_test_runs_and_idle_once_it_ends(tmp_path, found, breaks, outcome):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    seen = []

    def status(_core, _elapsed):
        seen.append(read(heartbeat))
        if breaks:
            raise RuntimeError("the watch broke")

    supervisor = watched(heartbeat, status, found)
    began = monotonic()
    if breaks:
        with pytest.raises(RuntimeError, match="the watch broke"):
            run_one(supervisor, lane(tmp_path, 3, (3, 19)), 0.3)
    else:
        assert run_one(supervisor, lane(tmp_path, 3, (3, 19)), 0.3).outcome is outcome
    ended = monotonic()
    running = seen[0]
    assert running is not None, "no heartbeat while the test ran"
    assert running["state"] == "running" and running["pid"] == os.getpid()
    assert running["started"] == writer_lock.process_state(os.getpid())[1]
    assert running["probe_dir"] == str(heartbeat.parent / "history")
    assert "search 1t" in running["test"] and "3,19" in running["test"]
    assert began <= running["at"] <= ended
    idle = read(heartbeat)
    assert idle["state"] == "idle" and idle["pid"] == os.getpid()
    assert running["at"] <= idle["at"] <= ended


@pytest.mark.req("HANG.heartbeat")
def test_a_run_dir_removal_that_blocks_keeps_the_test_s_running_beat_which_goes_stale_and_resets(tmp_path, monkeypatch):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    removing, released = threading.Event(), threading.Event()
    remove = shutil.rmtree

    def stuck(path, *args, **kwargs):
        removing.set()
        released.wait(30)
        remove(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", stuck)
    supervising = threading.Thread(target=run_one, args=(watched(heartbeat, None), lane(tmp_path), 0.3), daemon=True)
    supervising.start()
    try:
        assert removing.wait(10)
        beat = read(heartbeat) or {}
        assert beat.get("state") == "running", beat
        stale = beat["at"] + cli.HANG_STALE_S + 1.0
        found = cli.hang_reason(heartbeat, cli.HANG_STALE_S, stale, writer_lock.process_state)
        assert found[1:] == (beat["at"], heartbeat.parent / "history"), found
    finally:
        released.set()
        supervising.join(10)
    assert (read(heartbeat) or {}).get("state") == "idle"


@pytest.mark.req("HANG.heartbeat")
def test_a_running_heartbeat_is_renewed_every_five_seconds_and_never_sooner(tmp_path):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    beats = execution.Heartbeat(heartbeat, tmp_path)
    renewed = []
    for now in (100.0, 104.9, 105.0, 109.9, 110.0):
        heartbeat.unlink(missing_ok=True)
        beats.running("soak watch", now)
        renewed.append(heartbeat.exists())
    assert renewed == [True, False, True, False, True]


@pytest.mark.req("HANG.path")
def test_the_heartbeat_s_directory_is_handed_back_to_the_invoking_user(tmp_path, monkeypatch):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    handed = []
    monkeypatch.setattr(execution, "hand_back", handed.append)
    run_one(watched(heartbeat, None), lane(tmp_path), 0.3)
    assert heartbeat.parent in handed


@pytest.mark.req("HANG.write")
def test_a_heartbeat_that_cannot_be_written_never_ends_the_test_and_is_logged_once(tmp_path, caplog):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    heartbeat = blocked / "heartbeat.json"

    def named() -> list[str]:
        return [record.getMessage() for record in caplog.records if str(heartbeat) in record.getMessage()]

    logged_by_the_first_tick = []
    supervisor = watched(heartbeat, lambda _core, _elapsed: logged_by_the_first_tick.append(len(named())))
    with caplog.at_level(logging.ERROR, logger=execution.__name__):
        verdict = run_one(supervisor, lane(tmp_path), 0.3)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert logged_by_the_first_tick[:1] == [1]
    assert len(named()) == 1, named()


@pytest.mark.req("HANG.write")
def test_a_write_that_fails_after_a_running_beat_leaves_no_running_heartbeat_behind(tmp_path):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    seen = []

    def status(_core, _elapsed):
        seen.append(read(heartbeat))
        heartbeat.with_name(heartbeat.name + ".tmp").mkdir(exist_ok=True)

    verdict = run_one(watched(heartbeat, status), lane(tmp_path), 0.3)
    assert verdict.outcome is Outcome.PASS, verdict.detail
    assert seen[0] is not None and seen[0]["state"] == "running", seen[:1]
    assert (read(heartbeat) or {}).get("state") != "running"


@pytest.mark.req("HANG.between")
def test_a_between_beat_names_the_driver_its_start_time_and_the_directory_the_watch_probes(tmp_path):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    began = monotonic()
    execution.Heartbeat(heartbeat, tmp_path / "history").between(100.0)
    beat = read(heartbeat)
    assert beat == {
        "state": "between",
        "probe_dir": str(tmp_path / "history"),
        "pid": os.getpid(),
        "started": writer_lock.process_state(os.getpid())[1],
        "at": beat["at"],
    }
    assert began <= beat["at"] <= monotonic()


@pytest.mark.req("HANG.heartbeat", "HANG.between")
def test_a_change_of_what_the_heartbeat_says_is_written_at_once_and_the_same_beat_every_five_seconds(tmp_path):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    beats = execution.Heartbeat(heartbeat, tmp_path)
    said = []
    for step in (
        lambda: beats.between(100.0),
        lambda: beats.between(104.0),
        lambda: beats.running("search 1t", 104.0),
        lambda: beats.running("search 1t", 108.5),
        lambda: beats.running("search 2t", 108.5),
        lambda: beats.ended(108.5),
        lambda: beats.between(108.5),
        lambda: beats.between(113.0),
        lambda: beats.between(113.5),
    ):
        heartbeat.unlink(missing_ok=True)
        step()
        beat = read(heartbeat) or {}
        said.append(" ".join(filter(None, (beat.get("state"), beat.get("test")))))
    assert said == ["between", "", "running search 1t", "", "running search 2t", "idle", "between", "", "between"]


@pytest.mark.req("HANG.write")
def test_a_heartbeat_that_fails_again_after_it_was_written_is_logged_again(tmp_path, caplog):
    blocker = tmp_path / "corecycler"
    heartbeat = blocker / "heartbeat.json"
    beats = execution.Heartbeat(heartbeat, tmp_path)

    def named() -> list[str]:
        return [record.getMessage() for record in caplog.records if str(heartbeat) in record.getMessage()]

    blocker.write_text("")
    with caplog.at_level(logging.ERROR, logger=execution.__name__):
        beats.between(100.0)
        beats.between(105.0)
        assert len(named()) == 1
        blocker.unlink()
        beats.between(110.0)
        assert read(heartbeat)["state"] == "between"
        heartbeat.unlink()
        blocker.rmdir()
        blocker.write_text("")
        beats.between(115.0)
    assert len(named()) == 2, named()
