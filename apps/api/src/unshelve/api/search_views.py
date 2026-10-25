"""Search as the API shows it: one page of hits, each with its item, its snippet and its place in
the list of each retriever (docs/learn/38); and an overview with citations (docs/learn/39).

Every query here is limited to one owner, as in ``item_views``.
"""

import base64
import hashlib
import json

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.api import item_views
from unshelve.api.errors import InvalidInputError, ServiceUnavailableError
from unshelve.api.schemas import (
    Citation,
    Overview,
    OverviewRequest,
    OverviewSegment,
    RetrieverRanks,
    SearchHit,
    SearchPage,
    SearchQuery,
    Snippet,
    SnippetPart,
)
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.protocols import CitingLLM, Embedder
from unshelve.db.models import Item
from unshelve.search import hybrid, overview, snippets
from unshelve.search.retrievers import Filters

# --- Paging ------------------------------------------------------------------------------
#
# A search runs again for each page, and the cursor says where the next page starts in its
# ranking. The ranking has at most 150 hits (three lists of 50), so a position is enough. It
# belongs to one query: a cursor with other words or other filters would point into a
# different ranking, so it is refused.


def _fingerprint(query: SearchQuery) -> str:
    """The parts of the query that decide the ranking."""
    parts = [
        query.q,
        sorted(query.status or []),
        sorted(query.kind or []),
        sorted(query.tag or []),
        query.goal_id,
        query.unread,
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


def _encode_cursor(query: SearchQuery, start: int) -> str:
    position = ["search", _fingerprint(query), start]
    return base64.urlsafe_b64encode(json.dumps(position).encode()).decode()


def _decode_cursor(cursor: str, query: SearchQuery) -> int:
    try:
        kind, fingerprint, start = json.loads(base64.urlsafe_b64decode(cursor))
    except (ValueError, TypeError) as error:
        raise InvalidInputError("the cursor is not valid") from error
    if kind != "search" or fingerprint != _fingerprint(query):
        raise InvalidInputError("the cursor belongs to another search: start again without it")
    if not isinstance(start, int) or start < 0:
        raise InvalidInputError("the cursor is not valid")
    return start


# --- The page ----------------------------------------------------------------------------


def filters_of(query: SearchQuery) -> Filters:
    return Filters(
        statuses=frozenset(query.status or ()),
        kinds=frozenset(query.kind or ()),
        tags=frozenset(query.tag or ()),
        goal_id=query.goal_id,
        unread=query.unread,
    )


def _snippet_out(snippet: snippets.Snippet | None) -> Snippet | None:
    if snippet is None:
        return None
    return Snippet(
        source=snippet.source.value,
        parts=[SnippetPart(text=part.text, hit=part.hit) for part in snippet.parts],
    )


async def search(
    db: AsyncSession, embedder: Embedder, owner_id: int, query: SearchQuery
) -> SearchPage:
    start = _decode_cursor(query.cursor, query) if query.cursor else 0
    ranking = await hybrid.search(db, embedder, owner_id, query.q, filters_of(query))
    page = ranking.hits[start : start + query.limit]

    ids = [hit.item_id for hit in page]
    found = await db.scalars(sa.select(Item).where(Item.id.in_(ids), Item.owner_id == owner_id))
    items = {item.id: item for item in found}
    shown = await item_views.summaries(db, [items[item_id] for item_id in ids])
    # Only the hits of this page: each snippet reads the text of its item.
    passages = await snippets.snippets(
        db,
        owner_id,
        query.q,
        page,
        set_id=ranking.set_id,
        query_vector=ranking.query_vector,
    )

    end = start + len(page)
    return SearchPage(
        items=[
            SearchHit(
                item=summary,
                score=hit.score,
                snippet=_snippet_out(passages.get(hit.item_id)),
                ranks=RetrieverRanks(
                    fts=hit.ranks.full_text, trigram=hit.ranks.trigram, vector=hit.ranks.vector
                ),
            )
            for hit, summary in zip(page, shown, strict=True)
        ],
        next_cursor=_encode_cursor(query, end) if end < len(ranking.hits) else None,
        vector_search=ranking.vector_search.value,
    )


# --- The overview ------------------------------------------------------------------------


async def answer(
    db: AsyncSession, embedder: Embedder, llm: CitingLLM, owner_id: int, body: OverviewRequest
) -> Overview:
    filters = Filters(statuses=frozenset(body.status or ()), unread=body.unread)
    try:
        found = await overview.overview(
            db, embedder, llm, owner_id, body.q, filters, limit=body.limit
        )
    except ProviderError as error:
        # The model, its key or the daily budget: the search itself works, the answer does not.
        raise ServiceUnavailableError(f"the model gave no answer: {error}") from error
    items = await db.scalars(
        sa.select(Item).where(Item.id.in_(found.item_ids), Item.owner_id == owner_id)
    )
    read = {item.id: item for item in items}
    return Overview(
        segments=[
            OverviewSegment(
                text=segment.text,
                citations=[
                    Citation(item_id=cited.item_id, chunk_id=cited.chunk_id, quote=cited.quote)
                    for cited in segment.citations
                ],
            )
            for segment in found.segments
        ],
        sources=await item_views.summaries(db, [read[item_id] for item_id in found.item_ids]),
        vector_search=found.vector_search.value,
    )
