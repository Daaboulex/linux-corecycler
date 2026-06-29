"""Property-based fuzzing of the data layers: config JSON parsing and the
per-core state DB round-trip.

- TunerConfig.from_json must fail closed on any string (a corrupted DB row must not
  crash resume/abort).
- A CoreState saved to the DB must load back identically -- a field that is added
  to the dataclass but not threaded through the upsert/loader silently loses state
  across resume. This property catches that automatically.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from history.db import HistoryDB  # noqa: E402
from tuner.config import TunerConfig  # noqa: E402
from tuner.state import CoreState, TunerPhase  # noqa: E402


class TestConfigJsonFailsClosed:
    @settings(max_examples=400, deadline=None)
    @given(s=st.text(max_size=200))
    def test_from_json_never_raises(self, s):
        cfg = TunerConfig.from_json(s)
        assert isinstance(cfg, TunerConfig)

    @given(blob=st.one_of(st.lists(st.integers()), st.integers(), st.floats(allow_nan=False),
                          st.booleans(), st.none()))
    def test_non_object_json_yields_defaults(self, blob):
        import json
        cfg = TunerConfig.from_json(json.dumps(blob))
        assert cfg == TunerConfig()

    @settings(max_examples=200, deadline=None)
    @given(
        coarse=st.integers(1, 15), fine=st.integers(1, 5), direction=st.sampled_from([-1, 1]),
        start=st.integers(-60, 30), max_off=st.integers(-60, 30),
        inherit=st.booleans(), auto_validate=st.booleans(),
        order=st.sampled_from(["sequential", "round_robin", "weakest_first",
                               "ccd_alternating", "ccd_round_robin"]),
        stretch=st.floats(0.0, 20.0, allow_nan=False, allow_infinity=False),
    )
    def test_to_from_json_round_trips(self, coarse, fine, direction, start, max_off,
                                      inherit, auto_validate, order, stretch):
        cfg = TunerConfig(
            coarse_step=coarse, fine_step=min(fine, coarse), direction=direction,
            start_offset=start, max_offset=max_off, inherit_current=inherit,
            auto_validate=auto_validate, test_order=order, stretch_threshold_pct=stretch,
            cores_to_test=[0, 1, 2],
        )
        assert TunerConfig.from_json(cfg.to_json()) == cfg


def _core_state(data) -> CoreState:
    opt_int = st.one_of(st.none(), st.integers(-70, 30))
    return CoreState(
        core_id=data.draw(st.integers(0, 63)),
        phase=data.draw(st.sampled_from(list(TunerPhase))),
        current_offset=data.draw(st.integers(-70, 30)),
        best_offset=data.draw(opt_int),
        coarse_fail_offset=data.draw(opt_int),
        confirm_attempts=data.draw(st.integers(0, 9)),
        baseline_offset=data.draw(st.integers(-70, 30)),
        backoff_mode=data.draw(st.booleans()),
        consecutive_backoff_fails=data.draw(st.integers(0, 9)),
        backoff_fail_bound=data.draw(opt_int),
        backoff_pass_bound=data.draw(opt_int),
        in_test=data.draw(st.booleans()),
        crash_count=data.draw(st.integers(0, 20)),
        crash_cooldown=data.draw(st.integers(0, 5)),
        thermal_aborts=data.draw(st.integers(0, 5)),
        cumulative_test_time=data.draw(st.floats(0.0, 1e6, allow_nan=False,
                                                 allow_infinity=False)),
        hardening_tier_index=data.draw(st.integers(0, 4)),
    )


class TestCoreStateRoundTrip:
    @settings(max_examples=300, deadline=None)
    @given(data=st.data())
    def test_every_field_round_trips_through_the_db(self, data):
        cs = _core_state(data)
        db = HistoryDB(":memory:")
        try:
            sid = db.create_tuner_session("{}", "", "TestCPU")
            db.upsert_tuner_core_state(sid, cs)
            loaded = db.get_tuner_core_states(sid)[cs.core_id]
            assert loaded == cs, f"round-trip lost a field: {cs} -> {loaded}"
            # Upsert again (the conflict path) and confirm it still matches.
            db.upsert_tuner_core_state(sid, cs)
            assert db.get_tuner_core_states(sid)[cs.core_id] == cs
        finally:
            db.close()
