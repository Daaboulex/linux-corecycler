from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from corecycler.tuner.orders import Method, RoundCursor, default_method, next_core, order_problem, round_sequence

TWO_CCD_16 = {core: core // 8 for core in range(16)}
SEVEN_9 = {**{core: 0 for core in range(6)}, **{core: 1 for core in range(8, 14)}}


def test_alternate_on_two_equal_ccds_is_the_owner_s_corecycler_order():
    assert round_sequence(Method.ALTERNATE, range(16), TWO_CCD_16, 0, "1t", 0)[:6] == (0, 8, 1, 9, 2, 10)


@pytest.mark.req("ORDER.domain")
def test_alternate_on_non_contiguous_ids_pairs_cores_by_rank():
    sequence = round_sequence(Method.ALTERNATE, sorted(SEVEN_9), SEVEN_9, 0, "1t", 0)
    assert sequence == (0, 8, 1, 9, 2, 10, 3, 11, 4, 12, 5, 13)


@pytest.mark.req("ORDER.alternate")
def test_alternate_on_uneven_domains_drops_the_exhausted_domain():
    domain_of = {0: 0, 1: 0, 2: 0, 8: 1}
    assert round_sequence(Method.ALTERNATE, [0, 1, 2, 8], domain_of, 0, "1t", 0) == (0, 8, 1, 2)


def test_alternate_on_three_domains_visits_every_domain_before_any_second_core():
    domain_of = {core: core // 4 for core in range(12)}
    sequence = round_sequence(Method.ALTERNATE, range(12), domain_of, 0, "1t", 0)
    assert sequence[:3] == (0, 4, 8)
    assert sorted(domain_of[core] for core in sequence[:3]) == [0, 1, 2]


@pytest.mark.req("D45")
def test_alternate_is_refused_on_one_domain_and_on_an_unknown_domain():
    assert "one" in order_problem(Method.ALTERNATE, [0, 1, 2], {0: 0, 1: 0, 2: 0})
    assert "[2]" in order_problem(Method.ALTERNATE, [0, 1, 2], {0: 0, 1: 1, 2: None})
    with pytest.raises(ValueError):
        round_sequence(Method.ALTERNATE, [0, 1], {0: 0, 1: 0}, 0, "1t", 0)


@pytest.mark.req("ORDER.sequential")
def test_sequential_and_random_run_on_any_layout_but_never_on_nothing():
    for method in (Method.SEQUENTIAL, Method.RANDOM):
        assert order_problem(method, [3], {3: None}) is None
        assert order_problem(method, [], {}) is not None
        assert order_problem(method, [1, 1], {1: 0}) is not None


def test_the_default_is_alternate_across_ccds_and_sequential_on_one():
    assert default_method(range(16), TWO_CCD_16) is Method.ALTERNATE
    assert default_method(range(8), TWO_CCD_16) is Method.SEQUENTIAL
    assert default_method([0, 1], {0: 0, 1: None}) is Method.SEQUENTIAL


@pytest.mark.req("INV.L26", "L07.h", "L26.d.next", "ORDER.random")
def test_random_repeats_for_the_same_seed_class_and_round_and_changes_between_rounds():
    first = round_sequence(Method.RANDOM, range(16), {}, 7, "1t", 3)
    assert first == round_sequence(Method.RANDOM, range(16), {}, 7, "1t", 3)
    rounds = {round_sequence(Method.RANDOM, range(16), {}, 7, "1t", r) for r in range(10)}
    assert len(rounds) > 1


layouts = st.integers(min_value=1, max_value=16).flatmap(
    lambda n: st.tuples(
        st.lists(st.integers(min_value=0, max_value=63), min_size=n, max_size=n, unique=True),
        st.lists(st.integers(min_value=0, max_value=3), min_size=n, max_size=n),
    )
)


@pytest.mark.req("D55")
@given(layouts, st.sampled_from(list(Method)), st.integers(min_value=0, max_value=2**32), st.integers(0, 50))
def test_every_round_of_every_method_is_a_permutation_of_the_cores(layout, method, seed, round_no):
    cores, domains = layout
    domain_of = dict(zip(cores, domains, strict=True))
    if order_problem(method, cores, domain_of) is not None:
        with pytest.raises(ValueError):
            round_sequence(method, cores, domain_of, seed, "1t", round_no)
        return
    sequence = round_sequence(method, cores, domain_of, seed, "1t", round_no)
    assert sorted(sequence) == sorted(cores)


@pytest.mark.req("L26.a")
@given(layouts, st.sampled_from(list(Method)), st.integers(min_value=0, max_value=2**32))
def test_two_rounds_visit_every_core_twice_and_never_the_same_core_back_to_back_within_a_round(layout, method, seed):
    cores, domains = layout
    domain_of = dict(zip(cores, domains, strict=True))
    if order_problem(method, cores, domain_of) is not None:
        return
    cursor = RoundCursor()
    picks = []
    for _ in range(2 * len(cores)):
        core, cursor = next_core(method, cores, domain_of, seed, "1t", cursor, None)
        picks.append(core)
    assert cursor == RoundCursor(2, 0)
    assert all(picks.count(core) == 2 for core in cores)
    assert all(a != b for a, b in zip(picks[: len(cores)], picks[1 : len(cores)], strict=False))


def test_an_owed_core_is_tested_without_moving_the_round():
    cursor = RoundCursor(0, 3)
    core, after = next_core(Method.SEQUENTIAL, [0, 1, 2, 3, 4], {}, 0, "1t", cursor, owed=1)
    assert (core, after) == (1, cursor)
    core, after = next_core(Method.SEQUENTIAL, [0, 1, 2, 3, 4], {}, 0, "1t", after, owed=None)
    assert (core, after) == (3, RoundCursor(0, 4))


def test_an_owed_core_outside_the_class_and_a_position_outside_the_round_are_refused():
    with pytest.raises(ValueError):
        next_core(Method.SEQUENTIAL, [0, 1], {}, 0, "1t", RoundCursor(), owed=5)
    with pytest.raises(ValueError):
        next_core(Method.SEQUENTIAL, [0, 1], {}, 0, "1t", RoundCursor(0, 2), owed=None)
