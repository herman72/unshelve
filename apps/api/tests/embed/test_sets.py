"""The embedding sets: which one search reads, and how a new one gets its vectors.
Each test name is one rule."""

import dataclasses
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope, EmbeddingSpec
from unshelve.contracts.enums import CaptureSource, JobKind, JobState
from unshelve.contracts.jobs import Services
from unshelve.db.models import EmbeddingSet, Job
from unshelve.embed import sets
from unshelve.ingest import resolve
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services
from unshelve.testing.fakes import FakeEmbedder

NOON = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SENTENCE = "Postgres keeps each job in one row, and a worker locks the row. "
LONG_TEXT = "\n\n".join(f"Part {number}. " + SENTENCE * 8 for number in range(6))  # 3 pieces
SHORT_TEXT = "A short note about vector search."

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    return build_services(Settings(env="test", database_url=private_database_url), committed_db)


async def _save(sessions: Sessions, text: str) -> int:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=await resolve.next_api_message_id(db),
            posted_at=NOON,
            text=text,
            urls=[],
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        item_id = await resolve.resolve_capture(db, capture_id)
        await db.commit()
    assert item_id is not None
    return item_id


async def _run_all(services: Services) -> None:
    discover()
    worker = Worker(services, registry, WorkerConfig(worker_id="w1"))
    while await worker.run_one():
        pass


async def _two_ready_items(services: Services) -> None:
    await _save(services.sessions, LONG_TEXT)
    await _save(services.sessions, SHORT_TEXT)
    await _run_all(services)


async def _pending_embed_jobs(sessions: Sessions) -> int:
    async with sessions() as db:
        found = await db.scalar(
            sa.select(sa.func.count()).where(
                Job.kind == JobKind.EMBED_ITEM, Job.state == JobState.PENDING
            )
        )
    return found or 0


SMALL = FakeEmbedder(32)


async def _set_of(sessions: Sessions, spec: EmbeddingSpec) -> int:
    async with sessions() as db:
        set_id = await sets.ensure_set(db, spec)
        await db.commit()
    return set_id


async def test_the_first_set_is_active_and_a_later_one_starts_inactive(
    services: Services,
) -> None:
    first = await _set_of(services.sessions, FakeEmbedder(64).spec)
    again = await _set_of(services.sessions, FakeEmbedder(64).spec)
    later = await _set_of(services.sessions, SMALL.spec)

    assert again == first
    # Each embed job looks up its set. A lookup uses no number of the sequence, so the
    # numbers of the sets stay small and in order: the commands print them.
    assert later == first + 1
    async with services.sessions() as db:
        assert (await db.get_one(EmbeddingSet, first)).is_active is True
        assert (await db.get_one(EmbeddingSet, later)).is_active is False
        assert await sets.find_set(db, SMALL.spec) == later
        assert await sets.find_set(db, FakeEmbedder(16).spec) is None


async def test_a_summary_counts_the_items_and_the_pieces_in_each_set(services: Services) -> None:
    await _two_ready_items(services)

    async with services.sessions() as db:
        [summary] = await sets.summaries(db)
        ready = await sets.ready_items(db)

    assert ready == 2
    assert (summary.provider, summary.model, summary.dim, summary.is_active) == (
        "fake",
        "hashed-bow-64",
        64,
        True,
    )
    assert (summary.items, summary.pieces) == (2, 3)


async def test_a_backfill_adds_one_job_for_each_item_without_a_vector_in_the_set(
    services: Services,
) -> None:
    await _two_ready_items(services)
    await _save(services.sessions, "A third note, not read yet.")  # its pipeline waits
    new_set = await _set_of(services.sessions, SMALL.spec)

    async with services.sessions() as db:
        assert await sets.missing(db, new_set) == 2  # ready items only
        assert await sets.backfill(db, new_set) == 2
        # The jobs wait already: a second backfill adds none.
        assert await sets.backfill(db, new_set) == 0
        await db.commit()

    await _run_all(dataclasses.replace(services, embedder=SMALL))

    async with services.sessions() as db:
        assert await sets.missing(db, new_set) == 0
        summaries = {summary.model: summary for summary in await sets.summaries(db)}
    # The third item went through its pipeline with the new embedder.
    assert (summaries["hashed-bow-32"].items, summaries["hashed-bow-32"].pieces) == (3, 3)
    assert summaries["hashed-bow-64"].is_active is True  # search reads the old set until now


async def test_a_backfill_of_every_item_adds_jobs_also_for_items_that_have_vectors(
    services: Services,
) -> None:
    await _two_ready_items(services)
    async with services.sessions() as db:
        active = await sets.find_set(db, FakeEmbedder(64).spec)
        assert active is not None
        assert await sets.backfill(db, active) == 0
        assert await sets.backfill(db, active, every_item=True) == 2
        await db.commit()

    assert await _pending_embed_jobs(services.sessions) == 2


async def test_activate_makes_one_set_the_set_that_search_reads(services: Services) -> None:
    first = await _set_of(services.sessions, FakeEmbedder(64).spec)
    later = await _set_of(services.sessions, SMALL.spec)

    async with services.sessions() as db:
        await sets.activate(db, later)
        await db.commit()

    async with services.sessions() as db:
        found = await db.execute(sa.select(EmbeddingSet.id, EmbeddingSet.is_active))
        assert sorted((row[0], row[1]) for row in found) == [(first, False), (later, True)]


async def test_an_unknown_set_cannot_be_activated(services: Services) -> None:
    await _set_of(services.sessions, FakeEmbedder(64).spec)

    async with services.sessions() as db:
        with pytest.raises(LookupError):
            await sets.activate(db, 999)
