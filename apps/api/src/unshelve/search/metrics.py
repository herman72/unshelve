"""The metrics of search: how good a ranking is for one query, measured against the answers
that a person checked (docs/learn/37).

* **recall@k**: the share of the relevant items that are in the first k places.
* **reciprocal rank**: 1 / the place of the first relevant item. The mean over many queries is
  the MRR (mean reciprocal rank).
* **nDCG@k**: how close the first k places are to the best possible order, when some items are
  more relevant than others. 1 is the best order.

This module is the M4-2 exercise: the owner of the project writes it against
``tests/search/test_metrics.py``.

Rules for each function:

* A ranking is a list of item ids, the best first. If an id is two times in a ranking, only its
  first place counts: a later copy is not relevant, but it keeps its place.
* A query without any relevant item cannot be measured: ``ValueError``. The evaluation leaves
  such a query out.
* ``k`` must be 1 or more: else ``ValueError``.
"""

from collections.abc import Collection, Mapping, Sequence


def recall_at_k(ranking: Sequence[int], relevant: Collection[int], k: int) -> float:
    """The share of the relevant items that are in the first ``k`` places of the ranking.

    The number of relevant items in the first ``k`` places, divided by the number of all
    relevant items (not by ``k``). From 0 to 1. A ranking shorter than ``k`` is read as it is.
    """
    raise NotImplementedError("M4-2 exercise")


def reciprocal_rank(ranking: Sequence[int], relevant: Collection[int]) -> float:
    """1 / the place of the first relevant item, with the first place 1. 0 when the ranking
    has no relevant item."""
    raise NotImplementedError("M4-2 exercise")


def mean_reciprocal_rank(queries: Sequence[tuple[Sequence[int], Collection[int]]]) -> float:
    """The mean of ``reciprocal_rank`` over queries: each one is (ranking, relevant items).

    No queries: ``ValueError``. A query without relevant items: ``ValueError``, as above.
    """
    raise NotImplementedError("M4-2 exercise")


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
    raise NotImplementedError("M4-2 exercise")
