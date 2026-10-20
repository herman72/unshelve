"""Reciprocal rank fusion: three ranked lists become one (docs/learn/37).

Each retriever (``search.retrievers``) returns item ids, the best first. Their scores cannot be
compared: a rank of full text, a strict word similarity and a cosine distance are different
things. Fusion therefore uses only the positions. An item that is high in several lists comes
first.

This module is the M4-2 exercise: the owner of the project writes it against
``tests/search/test_fusion.py``.
"""

from collections.abc import Sequence

# The constant of Cormack, Clarke and Büttcher (2009). A larger k makes the first places of a
# list count less against the later ones.
RRF_K = 60


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[int]], *, k: int = RRF_K, limit: int | None = None
) -> list[tuple[int, float]]:
    """Join ranked lists of item ids into one ranking. Returns (item id, score) pairs.

    The contract:

    * The rank of an item in a list starts at 1: the first item has the rank 1.
    * The score of an item is the sum of ``1 / (k + rank)`` over the lists that contain it. A
      list that does not contain the item adds nothing.
    * If an id is two times in one list, only its first (best) place counts. The copy keeps
      its place, so the items after it keep their ranks.
    * The result has each item of the lists one time, the highest score first. Two items with
      the same score: the lower id first.
    * ``limit``: at most this many pairs (the first ones); None returns all of them.
    * No lists, or only empty lists: an empty result.
    * ``k`` must be 0 or more, and ``limit`` 0 or more (or None): else ``ValueError``.
    """
    raise NotImplementedError("M4-2 exercise")
