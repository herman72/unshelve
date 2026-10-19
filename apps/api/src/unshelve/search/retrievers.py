"""The three retrievers of search. Each one is one SQL query, and it returns the ids of the items
of one owner, the best first. Reciprocal rank fusion (M4-2) joins their lists into one.

* **Full text**: the words of the query against the card (title, summary, author and site) and
  the fetched text of each item. English words are stemmed: "queues" finds "queue".
* **Trigram**: the query against the titles, by pieces of three letters. It finds a title with
  a typo, a part of a word, or a Persian word that has a zero-width non-joiner in one spelling
  and not in the other. Full text misses each of these.
* **Vector**: the vector of the query against the vectors of the card and of the pieces of the
  text, in the active embedding set. It finds the meaning, also in other words.

``u_norm()`` makes the query and the stored text equal for the forms that Arabic and Persian
keyboards type differently (docs/learn/03). Each retriever sees only the items of the owner, and
no merged copy: the lists show the older item instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import EmbeddingSpec
from unshelve.db.models import Chunk, EmbeddingSet, Item, ItemContent
from unshelve.embed.sets import find_set

# Each retriever returns at most this many ids. Fusion reads the top of each list.
LIMIT = 50
# The trigram retriever uses the operator <<% (strict word similarity, pg_trgm), which the index
# ix_items_title_trgm serves. Its threshold is the default of pg_trgm, 0.5: measured in
# docs/learn/36, it keeps a typo (0.73), a prefix (0.5) and a Persian word with or without a
# zero-width non-joiner (0.55), and it drops "search" for "research" (0.45).


def _visible(owner_id: int) -> sa.ColumnElement[bool]:
    return sa.and_(Item.owner_id == owner_id, Item.duplicate_of_id.is_(None))


async def full_text(
    session: AsyncSession, owner_id: int, query: str, *, limit: int = LIMIT
) -> list[int]:
    """The items whose card or text has the words of the query, the closest first.

    The card decides first: a word in the title or the summary is a stronger sign than the
    same word in a long page. The text then orders the items whose cards rank the same, for
    example the items found only in their text. The query has the syntax of a web search: "a
    phrase" in quotes, ``or`` and ``-word``. A query of stop words only ("the") finds nothing.
    """
    words = sa.func.websearch_to_tsquery("english", sa.func.u_norm(query))
    card_rank = sa.func.ts_rank(Item.fts, words).label("card_rank")
    # Normalisation 1: the rank of a long text is divided by the logarithm of its length.
    text_rank = sa.func.coalesce(sa.func.ts_rank(ItemContent.fts, words, 1), 0).label("text_rank")
    rows = await session.execute(
        sa.select(Item.id, card_rank, text_rank)
        .outerjoin(ItemContent, ItemContent.item_id == Item.id)
        .where(
            _visible(owner_id),
            sa.or_(Item.fts.op("@@")(words), ItemContent.fts.op("@@")(words)),
        )
        .order_by(card_rank.desc(), text_rank.desc(), Item.id)
        .limit(limit)
    )
    return [row.id for row in rows]


async def trigram(
    session: AsyncSession, owner_id: int, query: str, *, limit: int = LIMIT
) -> list[int]:
    """The items whose title has a word close to the words of the query, the closest first."""
    normalised = sa.func.u_norm(query)
    title = sa.func.u_norm(Item.title)
    closeness = sa.func.strict_word_similarity(normalised, title).label("closeness")
    rows = await session.execute(
        sa.select(Item.id, closeness)
        .where(_visible(owner_id), normalised.op("<<%")(title))
        .order_by(closeness.desc(), Item.id)
        .limit(limit)
    )
    return [row.id for row in rows]


async def searchable_set(session: AsyncSession, spec: EmbeddingSpec) -> int | None:
    """The id of the set of ``spec`` when it is the active set: search compares a query vector
    only with vectors of the same model. None for another model, or before the first vector."""
    set_id = await find_set(session, spec)
    if set_id is None:
        return None
    active = await session.scalar(
        sa.select(EmbeddingSet.is_active).where(EmbeddingSet.id == set_id)
    )
    return set_id if active else None


async def vector(
    session: AsyncSession,
    owner_id: int,
    set_id: int,
    query_vector: Sequence[float],
    *,
    limit: int = LIMIT,
) -> list[int]:
    """The items with a vector close to the query vector, the closest first.

    The distance of an item is the distance of its closest vector: the card, or one piece of its
    text. The caller makes the query vector with the embedder of ``set_id`` (``searchable_set``),
    with no transaction open. This is the exact nearest search: each vector of the owner in the
    set is compared. Also when no item is near, the nearest ones come back; fusion and the
    evaluation decide what is too far.
    """
    distance = sa.func.min(Chunk.embedding.cosine_distance(list(query_vector))).label("distance")
    rows = await session.execute(
        sa.select(Chunk.item_id, distance)
        .join(Item, Item.id == Chunk.item_id)
        .where(
            _visible(owner_id),
            Chunk.owner_id == owner_id,
            Chunk.embedding_set_id == set_id,
            # A vector of another length cannot be compared (pgvector refuses it).
            Chunk.dim == len(query_vector),
        )
        .group_by(Chunk.item_id)
        .order_by(distance, Chunk.item_id)
        .limit(limit)
    )
    return [row.item_id for row in rows]
