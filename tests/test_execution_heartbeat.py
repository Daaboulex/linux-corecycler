from __future__ import annotations

import json
import logging
import os
import time
from typing import TYPE_CHECKING

import pytest

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
        FakeBackend(_child(SLEEPS), found=found), phase="search 1t", hooks=SuperviseHooks(on_status=status)
    )
    supervisor.heartbeat = heartbeat
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
    assert "search 1t" in running["test"] and "3,19" in running["test"]
    assert began <= running["at"] <= ended
    idle = read(heartbeat)
    assert idle["state"] == "idle" and idle["pid"] == os.getpid()
    assert running["at"] <= idle["at"] <= ended


@pytest.mark.req("HANG.heartbeat")
def test_a_running_heartbeat_is_renewed_every_five_seconds_and_never_sooner(tmp_path):
    heartbeat = tmp_path / "corecycler" / "heartbeat.json"
    beats = execution.Heartbeat(heartbeat)
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
