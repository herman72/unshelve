"""The vectors of the goals, and the similarity of a card to the closest goal. Each test name is
one rule. A pretend embedder: no test reaches a real API."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.contracts.enums import ChunkKind, EnrichState
from unshelve.db.models import Chunk, Goal, GoalVector, Item
from unshelve.embed.sets import ensure_set
from unshelve.relevance.vectors import best_similarities, ensure_goal_vectors
from unshelve.testing.fakes import FakeEmbedder

NOON = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
CARD = "Leases make a job queue on one Postgres table safe"

Sessions = async_sessionmaker[AsyncSession]


async def _goal(sessions: Sessions, title: str, version: int = 1) -> Goal:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        goal = Goal(
            owner_id=owner_id, slug=title.lower().replace(" ", "-"), title=title, version=version
        )
        db.add(goal)
        await db.commit()
    return goal


async def _item_with_card(sessions: Sessions, embedder: FakeEmbedder) -> tuple[int, int]:
    """An item with a card vector of ``CARD``. Returns the item and the set."""
    (vector,) = await embedder.embed_documents([CARD])
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        set_id = await ensure_set(db, embedder.spec)
        item = Item(
            owner_id=owner_id,
            enrich_state=EnrichState.READY,
            first_captured_at=NOON,
            last_captured_at=NOON,
        )
        db.add(item)
        await db.flush()
        db.add(
            Chunk(
                owner_id=owner_id,
                item_id=item.id,
                embedding_set_id=set_id,
                kind=ChunkKind.CARD,
                ord=0,
                text=CARD,
                dim=len(vector),
                embedding=vector,
            )
        )
        await db.commit()
    return item.id, set_id


async def test_a_card_is_close_to_a_goal_in_its_own_words(committed_db: Sessions) -> None:
    embedder = FakeEmbedder()
    item_id, _ = await _item_with_card(committed_db, embedder)
    goals = [await _goal(committed_db, CARD), await _goal(committed_db, "Run a marathon")]

    set_id = await ensure_goal_vectors(committed_db, embedder, goals)
    async with committed_db() as db:
        found = await best_similarities(db, set_id, [item_id], goals)

    assert found[item_id] == pytest.approx(1.0)


async def test_a_goal_without_a_vector_of_its_current_version_stops_the_prefilter(
    committed_db: Sessions,
) -> None:
    embedder = FakeEmbedder()
    item_id, _ = await _item_with_card(committed_db, embedder)
    with_vector = await _goal(committed_db, "Run a marathon")
    set_id = await ensure_goal_vectors(committed_db, embedder, [with_vector])
    without = await _goal(committed_db, CARD)  # close to the card, and no vector yet

    async with committed_db() as db:
        both = await best_similarities(db, set_id, [item_id], [with_vector, without])
        alone = await best_similarities(db, set_id, [item_id], [with_vector])

    # The card could be close to just the goal without a vector: no answer for the item.
    assert both == {}
    assert item_id in alone


async def test_a_vector_of_an_older_version_of_a_goal_does_not_count(
    committed_db: Sessions,
) -> None:
    embedder = FakeEmbedder()
    item_id, _ = await _item_with_card(committed_db, embedder)
    goal = await _goal(committed_db, CARD)
    set_id = await ensure_goal_vectors(committed_db, embedder, [goal])
    async with committed_db() as db:
        await db.execute(sa.update(Goal).where(Goal.id == goal.id).values(version=2))
        await db.commit()
        changed = await db.get_one(Goal, goal.id)

        # The vector holds version 1. Its words could be far from the new meaning.
        found = await best_similarities(db, set_id, [item_id], [changed])

    assert found == {}


async def test_an_older_goal_text_never_replaces_a_newer_vector(committed_db: Sessions) -> None:
    embedder = FakeEmbedder()
    newer = await _goal(committed_db, "Indexes", version=2)
    await ensure_goal_vectors(committed_db, embedder, [newer])
    # A slow job that read the goal before its change still holds version 1.
    older = Goal(
        id=newer.id,
        owner_id=newer.owner_id,
        title="Database internals",
        description="",
        keywords=[],
        version=1,
    )

    await ensure_goal_vectors(committed_db, embedder, [older])

    async with committed_db() as db:
        vector = await db.scalar(sa.select(GoalVector))
    assert vector is not None
    assert (vector.goal_version, vector.text) == (2, "Indexes")
