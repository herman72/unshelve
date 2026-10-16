"""The prefilter of the scores: how close the card of an item is to each goal.

Each goal gets a vector of its words (``goal_text``), in the embedding set of the embedder, as
a query. The card vector of an item is a document vector (the embed step). Their similarity is
the cosine: 1 is the same direction, 0 is nothing in common. Postgres computes it (pgvector's
``<=>`` is the cosine distance, 1 - similarity), for the score step and for the command that
scores items again, so both use one definition.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.enums import ChunkKind
from unshelve.contracts.protocols import Embedder
from unshelve.db.models import Chunk, Goal, GoalVector
from unshelve.embed.sets import ensure_set
from unshelve.relevance.prompt import goal_text


async def ensure_goal_vectors(
    sessions: async_sessionmaker[AsyncSession], embedder: Embedder, goals: Sequence[Goal]
) -> int:
    """Give each goal a vector of its current version in the set of ``embedder``. Returns the
    id of the set.

    A goal whose vector holds its current version asks the embedder nothing. The embedder is
    asked with no transaction open. Its errors go to the caller.
    """
    async with sessions() as session:
        set_id = await ensure_set(session, embedder.spec)
        rows = await session.execute(
            sa.select(GoalVector.goal_id, GoalVector.goal_version).where(
                GoalVector.embedding_set_id == set_id,
                GoalVector.goal_id.in_([goal.id for goal in goals]),
            )
        )
        # Not dict(rows): a result has keys(), so dict() would read it as a mapping.
        stored = {row.goal_id: row.goal_version for row in rows}
        await session.commit()
    missing = [goal for goal in goals if stored.get(goal.id) != goal.version]
    if not missing:
        return set_id
    vectors = [await embedder.embed_query(goal_text(goal)) for goal in missing]
    async with sessions() as session:
        insert = postgresql.insert(GoalVector).values(
            [
                {
                    "goal_id": goal.id,
                    "embedding_set_id": set_id,
                    "owner_id": goal.owner_id,
                    "goal_version": goal.version,
                    "text": goal_text(goal),
                    "dim": len(vector),
                    "embedding": vector,
                }
                for goal, vector in zip(missing, vectors, strict=True)
            ]
        )
        columns = ("goal_version", "text", "dim", "embedding")
        await session.execute(
            insert.on_conflict_do_update(
                constraint="pk_goal_vectors",
                set_={column: insert.excluded[column] for column in columns},
                # A slower run with an older text never replaces a newer vector.
                where=GoalVector.goal_version <= insert.excluded.goal_version,
            )
        )
        await session.commit()
    return set_id


async def best_similarities(
    session: AsyncSession, set_id: int, item_ids: Sequence[int], goals: Sequence[Goal]
) -> dict[int, float]:
    """For each item: the similarity of its card to the closest goal.

    An item without a card vector in the set is not in the result: nothing says that it is far
    from the goals, so the model must read it. The result is empty when a goal has no vector of
    its current version: an item could be close to just that goal.
    """
    if not item_ids or not goals:
        return {}
    current = sa.and_(
        GoalVector.embedding_set_id == set_id,
        sa.or_(
            *(
                sa.and_(GoalVector.goal_id == goal.id, GoalVector.goal_version == goal.version)
                for goal in goals
            )
        ),
    )
    with_vector = await session.scalar(
        sa.select(sa.func.count()).select_from(GoalVector).where(current)
    )
    if with_vector != len(goals):
        return {}
    similarity = sa.func.max(1 - Chunk.embedding.cosine_distance(GoalVector.embedding)).label(
        "best"
    )
    rows = await session.execute(
        sa.select(Chunk.item_id, similarity)
        .join(GoalVector, GoalVector.embedding_set_id == Chunk.embedding_set_id)
        .where(
            Chunk.embedding_set_id == set_id,
            Chunk.kind == ChunkKind.CARD,
            Chunk.ord == 0,
            Chunk.item_id.in_(item_ids),
            current,
        )
        .group_by(Chunk.item_id)
    )
    return {row.item_id: float(row.best) for row in rows}
