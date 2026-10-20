"""Reciprocal rank fusion: its specification. Read it from the top: each test name is one rule,
and the first tests use the lists of note 36."""

from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from unshelve.search.fusion import RRF_K, reciprocal_rank_fusion

# The search "postgres" of note 36, with small ids: 1 is the job queue, 2 pgvector, 3 Rust and
# 8 the weekend notes.
FULL_TEXT = [1, 2, 8]
TRIGRAM = [1]
VECTOR = [1, 2, 3]


def _ids(fused: list[tuple[int, float]]) -> list[int]:
    return [item for item, _ in fused]


# --- Examples ------------------------------------------------------------------------------


def test_the_constant_k_is_60() -> None:
    assert RRF_K == 60


def test_an_item_scores_one_over_k_plus_its_rank() -> None:
    # One list: the first item has the rank 1, so it scores 1 / (60 + 1).
    assert reciprocal_rank_fusion([[5, 7, 9]]) == [
        (5, pytest.approx(1 / 61)),
        (7, pytest.approx(1 / 62)),
        (9, pytest.approx(1 / 63)),
    ]


def test_the_scores_of_an_item_add_up_over_the_lists() -> None:
    fused = reciprocal_rank_fusion([FULL_TEXT, TRIGRAM, VECTOR])

    assert fused == [
        (1, pytest.approx(3 / 61)),  # first in each of the three lists
        (2, pytest.approx(2 / 62)),  # second in two lists
        (3, pytest.approx(1 / 63)),  # third in one list
        (8, pytest.approx(1 / 63)),  # third in one list: the same score, so the lower id first
    ]


def test_an_item_high_in_two_lists_beats_an_item_first_in_one() -> None:
    # 2: 1/62 + 1/62 = 0.0323. 1: 1/61 = 0.0164. Agreement wins.
    assert _ids(reciprocal_rank_fusion([[1, 2], [3, 2]])) == [2, 1, 3]


def test_equal_scores_put_the_lower_id_first() -> None:
    assert reciprocal_rank_fusion([[9], [4]]) == [
        (4, pytest.approx(1 / 61)),
        (9, pytest.approx(1 / 61)),
    ]


def test_an_id_two_times_in_one_list_counts_only_at_its_first_place() -> None:
    # 5 scores 1/61, not 1/61 + 1/63. The copy still takes the place 3.
    assert reciprocal_rank_fusion([[5, 6, 5, 7]]) == [
        (5, pytest.approx(1 / 61)),
        (6, pytest.approx(1 / 62)),
        (7, pytest.approx(1 / 64)),
    ]


def test_k_decides_how_much_one_first_place_counts_against_agreement() -> None:
    lists = [[1, 7, 2], [8, 9, 2]]

    # k = 0: a first place scores 1, two third places 1/3 + 1/3. One first place wins.
    assert reciprocal_rank_fusion(lists, k=0) == [
        (1, pytest.approx(1.0)),
        (8, pytest.approx(1.0)),
        (2, pytest.approx(2 / 3)),
        (7, pytest.approx(0.5)),
        (9, pytest.approx(0.5)),
    ]
    # k = 60: 2/63 = 0.0317 against 1/61 = 0.0164. Agreement wins.
    assert _ids(reciprocal_rank_fusion(lists, k=60)) == [2, 1, 8, 7, 9]


def test_limit_keeps_the_first_pairs() -> None:
    fused = reciprocal_rank_fusion([FULL_TEXT, TRIGRAM, VECTOR], limit=2)

    assert _ids(fused) == [1, 2]
    assert reciprocal_rank_fusion([FULL_TEXT], limit=0) == []
    assert len(reciprocal_rank_fusion([FULL_TEXT], limit=99)) == 3


def test_no_lists_or_empty_lists_give_nothing() -> None:
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[], []]) == []


def test_a_negative_k_or_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="k"):
        reciprocal_rank_fusion([[1]], k=-1)
    with pytest.raises(ValueError, match="limit"):
        reciprocal_rank_fusion([[1]], limit=-1)


# --- Properties: Hypothesis makes many lists and checks each rule ---------------------------

ITEMS = st.integers(min_value=1, max_value=30)
LISTS = st.lists(st.lists(ITEMS, max_size=12), max_size=4)


@given(LISTS)
def test_each_item_of_the_lists_comes_out_one_time(rankings: list[list[int]]) -> None:
    ids = _ids(reciprocal_rank_fusion(rankings))

    assert len(ids) == len(set(ids))
    assert set(ids) == {item for ranking in rankings for item in ranking}


@given(LISTS)
def test_the_scores_go_down_and_equal_scores_go_by_id(rankings: list[list[int]]) -> None:
    fused = reciprocal_rank_fusion(rankings)

    for (first, high), (second, low) in pairwise(fused):
        assert high > low or (high == low and first < second)
    assert all(score > 0 for _, score in fused)


@given(st.lists(ITEMS, unique=True, max_size=12))
def test_one_list_keeps_its_order(ranking: list[int]) -> None:
    assert _ids(reciprocal_rank_fusion([ranking])) == ranking


@given(LISTS, st.lists(ITEMS, max_size=12))
def test_one_more_list_never_lowers_a_score(rankings: list[list[int]], extra: list[int]) -> None:
    before = dict(reciprocal_rank_fusion(rankings))
    after = dict(reciprocal_rank_fusion([*rankings, extra]))

    for item, score in before.items():
        assert after[item] >= score
