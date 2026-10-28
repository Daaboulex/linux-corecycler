from __future__ import annotations

import json
from dataclasses import replace

import pytest

from corecycler.tuner.live import stored_from_json, stored_to_json
from corecycler.tuner.machine import bisect
from corecycler.tuner.machine.types import FailContext, Stage
from tests._tuner_world import Close, Group, Report, fresh, run_to_rest, start
from tests.test_tuner_slice_c import FAST, failing_once, slot, stepped, until_test

BEGIN_STAGES = (Stage.LIVE1, Stage.MEMORY, Stage.STEADY, Stage.TRANSIENT, Stage.ENDURANCE, Stage.SOAK)


def written(st):
    return json.loads(json.dumps(stored_to_json(st)))


def begun():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    until_test(fake, Stage.TRANSIENT)
    fake.deliver(Report(Group.SILICON, 1.0, context=FailContext(steady=False, working=(0, 1, 2, 3), paused=False)))
    run = fake.db.flow.bisect
    assert (run.rerun, run.queue[0]) == (None, ())
    return fake.db


def replaying():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = until_test(fake, Stage.TRANSIENT)
    failing_once(fake, held, slot(held, (0, 1)), slot(held, (2, 3)))
    until_test(fake, Stage.BISECT, slot(stepped(held, (0, 1)), (0, 1)))
    fake.post(Close())
    run = fake.db.flow.bisect
    assert (fake.db.status, run.queue, run.rerun, bisect.after(run)) == ("paused", (), (0, 1), ((2, 3),))
    return fake.db


def retesting():
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = until_test(fake, Stage.TRANSIENT)
    failing_once(fake, held)
    while fake.db.flow.bisect is None or not bisect.retesting(fake.db.flow.bisect):
        assert fake.progress()
    run = fake.db.flow.bisect
    assert run.results[0] == (run.universe, False)
    return fake.db


def outside():
    fake = fresh(3, [0, -6, -6], 2, max_offset=-8, k_1t=1, k_2t=1, wait_between_cores_s=0.0)
    start(fake)
    until_test(fake, Stage.STEADY)
    fake.deliver(Report(Group.SILICON, 1.0, context=FailContext(steady=True, working=(0, 1, 2), paused=False)))
    run = fake.db.flow.bisect
    assert (fake.db.config.selected, run.lane, run.universe) == ((0, 1, 2), 0, (1, 2))
    return fake.db


def one_core():
    fake = fresh(2, [-4, 0], 2, max_offset=-8, **FAST)
    start(fake)
    until_test(fake, Stage.TRANSIENT)
    fake.deliver(Report(Group.SILICON, 1.0, context=FailContext(steady=False, working=(0, 1), paused=False)))
    run = fake.db.flow.bisect
    assert (run.universe, run.values) == ((0,), ((0, -1),))
    return fake.db


def entered(stage):
    def state():
        st = retesting()
        return replace(st, flow=replace(st.flow, bisect=replace(st.flow.bisect, entry=stage)))

    return state


WRITTEN = {
    "rerun null with the empty group queued": begun,
    "the D116.stored Q6 replay": replaying,
    "the full-set retest": retesting,
    "a lane outside the universe": outside,
    "a one-core universe at -1": one_core,
    **{f"the full-set retest begun in {stage}": entered(stage) for stage in BEGIN_STAGES},
}


@pytest.mark.req("D116.stored.read")
@pytest.mark.parametrize("state", list(WRITTEN.values()), ids=list(WRITTEN))
def test_a_bisection_a_build_wrote_reads_back_from_the_session_file_unchanged(state):
    st = state()
    assert stored_from_json(written(st), continued=False) == st


@pytest.mark.req("D116.stored.read")
@pytest.mark.parametrize(
    ("failing", "replayed"),
    [
        pytest.param(((0, 1, 2, 3), (0, 1), (2, 3)), {(0, 1), (2, 3)}, id="two groups replayed in turn"),
        pytest.param(((0, 1, 2, 3), (0, 1, 2, 3)), {(0, 1, 2, 3)}, id="the full set replayed after its retest"),
    ],
)
def test_every_replay_a_run_stores_reads_back_from_the_session_file_unchanged(failing, replayed):
    fake = fresh(4, [-6] * 4, 2, max_offset=-8, **FAST)
    start(fake)
    held = until_test(fake, Stage.TRANSIENT)
    failing_once(fake, *(slot(held, group) for group in failing))
    progress = fake.progress
    seen = set()

    def watched():
        moved = progress()
        st = fake.db
        if st.flow is not None and st.flow.bisect is not None and st.flow.bisect.rerun is not None:
            assert stored_from_json(written(st), continued=False) == st
            seen.add(st.flow.bisect.rerun)
        return moved

    fake.progress = watched
    run_to_rest(fake, 20_000)
    assert seen == replayed


REFUSED = [
    pytest.param("results", outside, {"results": [[[1, 2], 0]]}, id="a verdict 0"),
    pytest.param("results", outside, {"results": [[[1, 2], 1]]}, id="a verdict 1"),
    pytest.param("results", outside, {"results": [[[1, 2], "true"]]}, id="a verdict as the text true"),
    pytest.param("rerun", replaying, {"rerun": "01"}, id="a rerun as the text 01"),
    pytest.param("queue", outside, {"queue": [[], [True], [2]]}, id="a bool core in a group"),
    pytest.param("queue", outside, {"queue": [[], [1], [2], [0]]}, id="a core outside the universe in the queue"),
    pytest.param(
        "results", outside, {"results": [[[1, 2], False], [[0], True]]}, id="a core outside the universe in a result"
    ),
    pytest.param(
        "parents",
        outside,
        {"parents": [[[1], [1, 2]], [[2], [1, 2]], [[0], [1, 2]]]},
        id="a core outside the universe in a parents child",
    ),
    pytest.param(
        "parents",
        outside,
        {"parents": [[[True], [1, 2]], [[2], [1, 2]]]},
        id="a bool core in a parents child",
    ),
    pytest.param(
        "parents",
        outside,
        {"parents": [[[1], [0, 1]], [[2], [1, 2]]]},
        id="a core outside the universe in a parents parent",
    ),
    pytest.param("rerun", outside, {"rerun": [0]}, id="a core outside the universe in the rerun"),
    pytest.param("values", outside, {"values": [[0, -3], [2, -3]]}, id="a core outside the universe in the values"),
    pytest.param(
        "universe",
        outside,
        {"universe": [1, 2, 3], "values": [[1, -3], [2, -3], [3, -3]]},
        id="a universe core the session does not own",
    ),
    pytest.param(
        "universe", outside, {"universe": [2, 1], "values": [[2, -3], [1, -3]]}, id="the universe out of order"
    ),
    pytest.param(
        "universe",
        outside,
        {"universe": [], "values": [], "queue": [[]], "results": [], "parents": []},
        id="an empty universe",
    ),
    pytest.param("values", outside, {"values": [[2, -3]]}, id="a universe core missing from the values"),
    pytest.param("values", outside, {"values": [[1, 0], [2, -3]]}, id="a value 0"),
    pytest.param("values", outside, {"values": [[1, "-3"], [2, -3]]}, id="a value as the text -3"),
    pytest.param("lane", outside, {"lane": True}, id="a lane true"),
    pytest.param("lane", outside, {"lane": 3}, id="a lane naming a core the session does not own"),
    pytest.param("entry", outside, {"entry": "bisect"}, id="the entry bisect"),
    pytest.param("parents", outside, {"queue": [[], [1]]}, id="a parents child neither recorded nor queued"),
    pytest.param("rerun", replaying, {"rerun": None}, id="a rerun null with an empty queue"),
    pytest.param("queue", outside, {"queue": [[], [1], [2], [2, 2]]}, id="a core twice in a group"),
    pytest.param("results", outside, {"results": [[[1, 2], False], [[1, 2], True]]}, id="a group recorded twice"),
    pytest.param(
        "parents",
        outside,
        {"parents": [[[1], [1, 2]], [[2], [1, 2]], [[1, 2], [1, 2]]]},
        id="a parents child its own parent",
    ),
    pytest.param(
        "parents",
        begun,
        {"parents": [[[0, 1], [0, 1, 2, 3]], [[2, 3], [0, 1, 2, 3]], [[0, 1], [1, 2, 3]]]},
        id="a parents child outside its parent",
    ),
    pytest.param(
        "rerun", replaying, {"rerun": [0, 1, 2, 3]}, id="a rerun that is no failed leaf while a group is still owed"
    ),
    pytest.param("rerun", replaying, {"rerun": [0, True]}, id="a bool core in the rerun"),
]


@pytest.mark.req("D116.stored.read")
@pytest.mark.parametrize(("field", "state", "stored"), REFUSED)
def test_a_bisection_no_build_writes_refuses_the_session_naming_the_field(field, state, stored):
    raw = written(state())
    raw["flow"]["bisect"].update(stored)
    with pytest.raises(ValueError, match=f"the stored bisect's {field} is "):
        stored_from_json(raw, continued=False)
