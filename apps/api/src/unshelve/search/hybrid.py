"""Hybrid search: the three retrievers for one query, fused into one ranking (docs/learn/38).

The vector retriever needs the vector of the query, and only the embedder can make it: a call
over the network. That call happens with no transaction open, and it has a short time limit.
When the embedder fails or is slow, the search goes on with the words alone (full text and
trigram), and the result says so.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.errors import ProviderError
from unshelve.contracts.protocols import Embedder
from unshelve.search import retrievers
from unshelve.search.fusion import reciprocal_rank_fusion
from unshelve.search.retrievers import NO_FILTERS, Filters

log = structlog.get_logger()

# The longest wait for the vector of a query, in seconds. After it, the search goes on with
# the words alone: a slow embedder must not make the whole search slow.
QUERY_VECTOR_WAIT = 3.0


class VectorSearch(StrEnum):
    """Whether the vector retriever took part in a search."""

    USED = "used"
    # The embedder has no active set of vectors: there is nothing to compare the query with.
    NO_VECTORS = "no_vectors"
    # The embedder answered with an error, or not in time.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Ranks:
    """The place of an item in the list of each retriever, from 1. None: not in that list."""

    full_text: int | None
    trigram: int | None
    vector: int | None


@dataclass(frozen=True, slots=True)
class Hit:
    item_id: int
    score: float  # the score of reciprocal rank fusion
    ranks: Ranks


@dataclass(frozen=True, slots=True)
class Ranking:
    hits: list[Hit]  # the best first
    vector_search: VectorSearch
    # The set and the vector of the query, when the vector retriever took part. The snippets
    # use them to find the closest passage of an item.
    set_id: int | None = None
    query_vector: list[float] | None = None


def _places(found: Sequence[int]) -> dict[int, int]:
    """The place of each id in a list, from 1. A retriever returns each id one time."""
    return {item_id: place for place, item_id in enumerate(found, start=1)}


async def search(
    session: AsyncSession,
    embedder: Embedder,
    owner_id: int,
    query: str,
    filters: Filters = NO_FILTERS,
    *,
    vector_wait: float = QUERY_VECTOR_WAIT,
) -> Ranking:
    """All the items of the owner that a retriever finds for ``query``, the best first.

    The function ends the transaction of ``session`` before it asks the embedder, so that no
    transaction stays open during the call: give it a session without changes that wait. It
    waits ``vector_wait`` seconds at most for the vector of the query.
    """
    set_id = await retrievers.searchable_set(session, embedder.spec)
    await session.commit()

    query_vector: list[float] | None = None
    vector_search = VectorSearch.NO_VECTORS
    if set_id is not None:
        try:
            async with asyncio.timeout(vector_wait):
                query_vector = await embedder.embed_query(query)
            vector_search = VectorSearch.USED
        except (ProviderError, TimeoutError) as error:
            # The query can be private: the log has only the kind of the error.
            log.warning("search.no_query_vector", error=type(error).__name__)
            vector_search = VectorSearch.FAILED

    full = await retrievers.full_text(session, owner_id, query, filters=filters)
    close = await retrievers.trigram(session, owner_id, query, filters=filters)
    near = (
        await retrievers.vector(session, owner_id, set_id, query_vector, filters=filters)
        if set_id is not None and query_vector is not None
        else []
    )

    places = [_places(found) for found in (full, close, near)]
    hits = [
        Hit(item_id, score, Ranks(*(place.get(item_id) for place in places)))
        for item_id, score in reciprocal_rank_fusion([full, close, near])
    ]
    if vector_search is not VectorSearch.USED:
        return Ranking(hits, vector_search)
    return Ranking(hits, vector_search, set_id=set_id, query_vector=query_vector)
