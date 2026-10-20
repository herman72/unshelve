"""The metrics of search: how good a ranking is for one query, measured against the answers
that a person checked (docs/learn/37).

* **recall@k**: the share of the relevant items that are in the first k places.
* **reciprocal rank**: 1 / the place of the first relevant item. The mean over many queries is
  the MRR (mean reciprocal rank).
* **nDCG@k**: how close the first k places are to the best possible order, when some items are
  more relevant than others. 1 is the best order.

``tests/search/test_metrics.py`` is their specification.

Rules for each function:

* A ranking is a list of item ids, the best first. If an id is two times in a ranking, only its
  first place counts: a later copy is not relevant, but it keeps its place.
* A query without any relevant item cannot be measured: ``ValueError``. The evaluation leaves
  such a query out.
* ``k`` must be 1 or more: else ``ValueError``.
"""

from collections.abc import Collection, Mapping, Sequence
from math import log2


def _first_places(ranking: Sequence[int]) -> list[int | None]:
    """The ranking with each later copy of an id replaced by None. The copy keeps its place, so
    the places of the items after it do not change, but it can be neither found nor relevant."""
    seen: set[int] = set()
    places: list[int | None] = []
    for item in ranking:
        places.append(None if item in seen else item)
        seen.add(item)
    return places


def _check_k(k: int) -> None:
    if k < 1:
        raise ValueError(f"k must be 1 or more, not {k}")


def _check_relevant(relevant: Collection[int]) -> None:
    if not relevant:
        raise ValueError("a query without relevant items cannot be measured")


def recall_at_k(ranking: Sequence[int], relevant: Collection[int], k: int) -> float:
    """The share of the relevant items that are in the first ``k`` places of the ranking.

    The number of relevant items in the first ``k`` places, divided by the number of all
    relevant items (not by ``k``). From 0 to 1. A ranking shorter than ``k`` is read as it is.
    """
    _check_k(k)
    _check_relevant(relevant)
    found = {item for item in _first_places(ranking)[:k] if item is not None and item in relevant}
    return len(found) / len(set(relevant))


def reciprocal_rank(ranking: Sequence[int], relevant: Collection[int]) -> float:
    """1 / the place of the first relevant item, with the first place 1. 0 when the ranking
    has no relevant item."""
    _check_relevant(relevant)
    for place, item in enumerate(_first_places(ranking), start=1):
        if item is not None and item in relevant:
            return 1 / place
    return 0.0


def mean_reciprocal_rank(queries: Sequence[tuple[Sequence[int], Collection[int]]]) -> float:
    """The mean of ``reciprocal_rank`` over queries: each one is (ranking, relevant items).

    No queries: ``ValueError``. A query without relevant items: ``ValueError``, as above.
    """
    if not queries:
        raise ValueError("no queries to measure")
    return sum(reciprocal_rank(ranking, relevant) for ranking, relevant in queries) / len(queries)


def ndcg_at_k(ranking: Sequence[int], relevance: Mapping[int, int], k: int = 10) -> float:
    """The normalised discounted cumulative gain of the first ``k`` places.

    ``relevance`` gives the grade of each judged item: 0 (not relevant) up to 3 (very
    relevant). An item that is not in ``relevance`` has the grade 0.

    * The gain of an item is its grade.
    * The discount of place ``i`` (the first place is 1) is ``log2(i + 1)``.
    * DCG@k: the sum of gain / discount over the first ``k`` places of the ranking.
    * IDCG@k: the DCG@k of the best possible ranking: all judged items, the highest grade
      first. It reads the judgements, not only the items that the ranking found.
    * nDCG@k = DCG@k / IDCG@k, from 0 to 1. IDCG@k = 0 (no grade above 0): ``ValueError``.
    """
    _check_k(k)
    # The best order first: it reads all the judgements, and it decides whether the query can
    # be measured at all, before anything is divided by it.
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(grade / log2(place + 1) for place, grade in enumerate(ideal, start=1))
    if idcg == 0:
        raise ValueError("a query without relevant items cannot be measured")
    dcg = sum(
        relevance.get(item, 0) / log2(place + 1)
        for place, item in enumerate(_first_places(ranking)[:k], start=1)
        if item is not None
    )
    return dcg / idcg
