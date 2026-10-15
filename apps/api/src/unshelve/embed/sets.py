"""The embedding sets: one for each embedding model that made vectors (ADR 0007).

Exactly one set is active, and search reads only its vectors. A new model gets a new set,
which starts inactive. New items get vectors in it from the embed step. ``unshelve embeddings
backfill`` gives vectors in it to the items that were ready before. Then ``unshelve
embeddings activate`` makes it the set that search reads. An old set keeps its vectors.
"""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.enums import ChunkKind, EnrichState, JobKind
from unshelve.contracts.jobs import EmbedItemPayload
from unshelve.db.models import Chunk, EmbeddingSet, Item
from unshelve.queue import store as queue


@dataclass(frozen=True)
class SetSummary:
    id: int
    provider: str
    model: str
    dim: int
    is_active: bool
    created_at: datetime
    # Items with a card vector in the set, and the vectors of the pieces of their texts.
    items: int
    pieces: int


def _same_spec(spec: EmbeddingSpec) -> sa.ColumnElement[bool]:
    return sa.and_(
        EmbeddingSet.provider == spec.provider,
        EmbeddingSet.model == spec.model,
        EmbeddingSet.dim == spec.dim,
    )


async def ensure_set(session: AsyncSession, spec: EmbeddingSpec) -> int:
    """The id of the embedding set of ``spec``. The row is made the first time.

    The first set of an instance becomes the active one. A later set starts inactive.
    """
    found = await find_set(session, spec)
    if found is not None:
        return found
    no_active_set = ~sa.exists().where(EmbeddingSet.is_active)
    await session.execute(
        postgresql.insert(EmbeddingSet)
        .values(
            provider=spec.provider,
            model=spec.model,
            dim=spec.dim,
            distance=spec.distance,
            doc_prefix=spec.doc_prefix,
            query_prefix=spec.query_prefix,
            is_active=no_active_set,
        )
        # No index is named here, on purpose. Two workers can do this at the same moment on a
        # new database. Then the second one can meet the row of the first one in two indexes:
        # "one set for each model" and "one active set". Each of the two must mean: do nothing.
        .on_conflict_do_nothing()
    )
    found = await session.execute(sa.select(EmbeddingSet.id).where(_same_spec(spec)))
    return found.scalar_one()


async def find_set(session: AsyncSession, spec: EmbeddingSpec) -> int | None:
    """The id of the embedding set of ``spec``, or None if no vector was made with it yet."""
    return await session.scalar(sa.select(EmbeddingSet.id).where(_same_spec(spec)))


async def summaries(session: AsyncSession) -> list[SetSummary]:
    """Each embedding set, the oldest first, with the number of its vectors."""
    count_of = sa.func.count(Chunk.id)
    rows = await session.execute(
        sa.select(
            EmbeddingSet,
            count_of.filter(Chunk.kind == ChunkKind.CARD),
            count_of.filter(Chunk.kind == ChunkKind.BODY),
        )
        .outerjoin(Chunk, Chunk.embedding_set_id == EmbeddingSet.id)
        .group_by(EmbeddingSet.id)
        .order_by(EmbeddingSet.id)
    )
    return [
        SetSummary(
            id=found.id,
            provider=found.provider,
            model=found.model,
            dim=found.dim,
            is_active=found.is_active,
            created_at=found.created_at,
            items=cards,
            pieces=pieces,
        )
        for found, cards, pieces in rows
    ]


async def ready_items(session: AsyncSession) -> int:
    """The number of items that went through the whole pipeline."""
    found = await session.scalar(
        sa.select(sa.func.count()).where(Item.enrich_state == EnrichState.READY)
    )
    return found or 0


def _without_card_vector(set_id: int) -> sa.ColumnElement[bool]:
    return ~sa.exists().where(
        Chunk.item_id == Item.id,
        Chunk.embedding_set_id == set_id,
        Chunk.kind == ChunkKind.CARD,
    )


async def missing(session: AsyncSession, set_id: int) -> int:
    """The number of ready items without a card vector in the set."""
    found = await session.scalar(
        sa.select(sa.func.count()).where(
            Item.enrich_state == EnrichState.READY, _without_card_vector(set_id)
        )
    )
    return found or 0


async def backfill(session: AsyncSession, set_id: int, *, every_item: bool = False) -> int:
    """Add an embed job for each ready item without a card vector in the set.

    With ``every_item``, for each ready item: the step then makes a vector only for a text
    that changed, so the items that are complete cost nothing. Returns the number of jobs.
    """
    where = [Item.enrich_state == EnrichState.READY]
    if not every_item:
        where.append(_without_card_vector(set_id))
    rows = await session.execute(sa.select(Item.id, Item.owner_id).where(*where).order_by(Item.id))
    added = 0
    for item_id, owner_id in rows:
        job = await queue.enqueue(
            session,
            JobKind.EMBED_ITEM,
            EmbedItemPayload(item_id=item_id),
            owner_id=owner_id,
            dedupe_key=f"item:{item_id}",
        )
        if job is not None:
            added += 1
    return added


async def activate(session: AsyncSession, set_id: int) -> None:
    """Make the set the one that search reads. Raises ``LookupError`` for an unknown set."""
    if await session.get(EmbeddingSet, set_id) is None:
        raise LookupError(f"no embedding set {set_id}")
    # Two statements, in this order: the database allows one active set at each moment.
    await session.execute(
        sa.update(EmbeddingSet)
        .where(EmbeddingSet.is_active, EmbeddingSet.id != set_id)
        .values(is_active=False)
    )
    await session.execute(
        sa.update(EmbeddingSet).where(EmbeddingSet.id == set_id).values(is_active=True)
    )
