"""The metrics of search: their specification. Read it from the top: each test name is one rule,
with the numbers computed by hand in note 37."""

from math import log2

import pytest
from hypothesis import given
from hypothesis import strategies as st

from unshelve.search.metrics import mean_reciprocal_rank, ndcg_at_k, recall_at_k, reciprocal_rank

# A ranking of one query, and the relevant items that a person checked.
RANKING = [3, 1, 4, 5, 9]
RELEVANT = {1, 5, 7}  # 7 is relevant, but the ranking does not have it

# --- recall@k ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("k", "expected"),
    [
        (1, 0.0),  # [3]: no relevant item
        (2, 1 / 3),  # [3, 1]: one of the three
        (4, 2 / 3),  # [3, 1, 4, 5]: two of the three
        (10, 2 / 3),  # the ranking has only 5 places, and 7 is not in it
    ],
)
def test_recall_is_the_share_of_the_relevant_items_in_the_first_k_places(
    k: int, expected: float
) -> None:
    assert recall_at_k(RANKING, RELEVANT, k) == pytest.approx(expected)


def test_recall_divides_by_the_relevant_items_not_by_k() -> None:
    assert recall_at_k([3, 1, 4], {1}, k=3) == pytest.approx(1.0)


def test_recall_counts_a_copy_one_time() -> None:
    # The first two places hold 1 and its copy: one of the two relevant items, not two.
    assert recall_at_k([1, 1, 2], {1, 2}, k=2) == pytest.approx(0.5)


# --- reciprocal rank and MRR ---------------------------------------------------------------


def test_reciprocal_rank_is_one_over_the_place_of_the_first_relevant_item() -> None:
    assert reciprocal_rank(RANKING, RELEVANT) == pytest.approx(1 / 2)  # 1 is at the place 2
    assert reciprocal_rank(RANKING, {3}) == pytest.approx(1.0)
    assert reciprocal_rank(RANKING, {7}) == 0.0  # not in the ranking


def test_a_copy_keeps_its_place() -> None:
    # The copy of 5 is not relevant, but it takes the place 2: item 2 is at the place 3.
    assert reciprocal_rank([5, 5, 2], {2}) == pytest.approx(1 / 3)


def test_mrr_is_the_mean_over_the_queries() -> None:
    queries = [
        ([3, 1, 4], {4}),  # 1/3
        ([2], {2}),  # 1
        ([9, 8], {7}),  # 0
    ]

    assert mean_reciprocal_rank(queries) == pytest.approx((1 / 3 + 1 + 0) / 3)


# --- nDCG@k --------------------------------------------------------------------------------

# Grades from 0 (not relevant) to 3 (very relevant), and a ranking with each kind of place.
GRADES = {1: 3, 2: 2, 3: 1, 4: 0}
GRADED_RANKING = [2, 4, 1, 9, 3]  # 9 has no grade: it counts as 0


def test_dcg_divides_each_grade_by_the_log_of_its_place_plus_one() -> None:
    dcg = 2 / log2(2) + 0 / log2(3) + 3 / log2(4) + 0 / log2(5) + 1 / log2(6)  # 3.887
    ideal = 3 / log2(2) + 2 / log2(3) + 1 / log2(4)  # 4.762: the grades in their best order

    assert ndcg_at_k(GRADED_RANKING, GRADES) == pytest.approx(dcg / ideal)  # 0.816


@pytest.mark.parametrize(
    ("k", "expected"),
    [
        (3, (2 + 3 / 2) / (3 + 2 / log2(3) + 1 / 2)),  # 0.735: the first three places
        (1, 2 / 3),  # place 1: grade 2, and the best first grade is 3
    ],
)
def test_ndcg_reads_only_the_first_k_places(k: int, expected: float) -> None:
    assert ndcg_at_k(GRADED_RANKING, GRADES, k) == pytest.approx(expected)


def test_the_best_order_scores_1() -> None:
    assert ndcg_at_k([1, 2, 3], GRADES) == pytest.approx(1.0)


def test_grades_of_1_make_a_binary_ndcg() -> None:
    # Found: 1 and 2 at the places 2 and 3. Best: at the places 1 and 2.
    expected = (1 / log2(3) + 1 / log2(4)) / (1 / log2(2) + 1 / log2(3))  # 0.693

    assert ndcg_at_k([5, 1, 2], {1: 1, 2: 1}) == pytest.approx(expected)


def test_the_ideal_order_reads_the_judgements_not_only_the_ranking() -> None:
    # The ranking found 1, not 2. The best ranking has both: 3 + 3 / log2(3).
    assert ndcg_at_k([1], {1: 3, 2: 3}) == pytest.approx(3 / (3 + 3 / log2(3)))  # 0.613


def test_ndcg_counts_a_copy_one_time() -> None:
    # The copy of 1 gains nothing: the same as the ranking [1] above.
    assert ndcg_at_k([1, 1], {1: 3, 2: 3}) == pytest.approx(3 / (3 + 3 / log2(3)))


def test_ndcg_at_10_is_the_default_so_place_10_counts_and_place_11_does_not() -> None:
    # Nine unjudged items, then item 1 at the place 10 and item 2 at the place 11.
    ranking = [*range(100, 109), 1, 2]
    grades = {1: 3, 2: 3}
    ideal = 3 / log2(2) + 3 / log2(3)

    assert ndcg_at_k(ranking, grades) == pytest.approx((3 / log2(11)) / ideal)
    assert ndcg_at_k(ranking, grades, k=11) == pytest.approx((3 / log2(11) + 3 / log2(12)) / ideal)


# --- A query that cannot be measured -------------------------------------------------------


def test_a_query_without_relevant_items_is_refused() -> None:
    with pytest.raises(ValueError, match="relevant"):
        recall_at_k(RANKING, set(), k=5)
    with pytest.raises(ValueError, match="relevant"):
        reciprocal_rank(RANKING, set())
    with pytest.raises(ValueError, match="relevant"):
        mean_reciprocal_rank([(RANKING, RELEVANT), (RANKING, set())])
    with pytest.raises(ValueError, match="relevant"):
        ndcg_at_k(RANKING, {3: 0, 1: 0})  # judged, but each grade is 0


def test_mrr_needs_a_query() -> None:
    with pytest.raises(ValueError, match="quer"):
        mean_reciprocal_rank([])


def test_k_must_be_1_or_more() -> None:
    with pytest.raises(ValueError, match="k"):
        recall_at_k(RANKING, RELEVANT, k=0)
    with pytest.raises(ValueError, match="k"):
        ndcg_at_k(RANKING, GRADES, k=0)


# --- Properties: Hypothesis makes many rankings and checks each rule -----------------------

ITEMS = st.integers(min_value=1, max_value=20)
RANKINGS = st.lists(ITEMS, max_size=15)
SOME_RELEVANT = st.sets(ITEMS, min_size=1, max_size=8)
SOME_GRADES = st.dictionaries(ITEMS, st.integers(0, 3), min_size=1, max_size=8).filter(
    lambda grades: any(grades.values())
)


@given(RANKINGS, SOME_RELEVANT, st.integers(1, 20))
def test_recall_is_between_0_and_1_and_grows_with_k(
    ranking: list[int], relevant: set[int], k: int
) -> None:
    now, later = recall_at_k(ranking, relevant, k), recall_at_k(ranking, relevant, k + 1)

    assert 0.0 <= now <= later <= 1.0


@given(RANKINGS, SOME_RELEVANT)
def test_reciprocal_rank_is_0_or_one_over_a_place(ranking: list[int], relevant: set[int]) -> None:
    value = reciprocal_rank(ranking, relevant)

    assert value == 0.0 or any(value == pytest.approx(1 / place) for place in range(1, 16))
    assert (value == 1.0) is (bool(ranking) and ranking[0] in relevant)


@given(RANKINGS, SOME_GRADES, st.integers(1, 20))
def test_ndcg_is_between_0_and_1(ranking: list[int], grades: dict[int, int], k: int) -> None:
    assert 0.0 <= ndcg_at_k(ranking, grades, k) <= 1.0 + 1e-9


@given(SOME_GRADES, st.integers(1, 20))
def test_the_judged_items_in_their_best_order_score_1(grades: dict[int, int], k: int) -> None:
    best = sorted(grades, key=lambda item: -grades[item])

    assert ndcg_at_k(best, grades, k) == pytest.approx(1.0)
