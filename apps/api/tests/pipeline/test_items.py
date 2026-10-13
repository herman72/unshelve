"""The pipeline of one item: fetch, enrich, embed. Each test name is one rule.

The steps run on the real worker loop, with the fake providers and a database of their own.
"""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope, ImagePart, LLMResult, TextPart
from unshelve.contracts.enums import (
    CaptureSource,
    ChunkKind,
    EnrichState,
    ItemKind,
    JobKind,
    JobState,
)
from unshelve.contracts.jobs import (
    ClaimedJob,
    EmbedItemPayload,
    EnrichItemPayload,
    FetchItemPayload,
    JobContext,
    JobPayload,
    Services,
)
from unshelve.db.models import Chunk, EmbeddingSet, Item, ItemContent, Job
from unshelve.ingest import resolve
from unshelve.pipeline import items as steps
from unshelve.queue import store as queue
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services
from unshelve.testing.fakes import FakeLLM, FakeLLMCall

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
URL = "https://example.com/how-pgvector-works"
TEXT = "A short guide to vector search in Postgres with pgvector"

Sessions = async_sessionmaker[AsyncSession]
Handler = Callable[[JobContext, Any], Awaitable[None]]


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    settings = Settings(env="test", database_url=private_database_url)
    return build_services(settings, committed_db)


def _worker(services: Services, name: str = "w1") -> Worker:
    discover()
    return Worker(services, registry, WorkerConfig(worker_id=name))


async def _save(sessions: Sessions, *, text: str = TEXT, url: str | None = URL) -> int:
    """Save one item the way POST /items does. Returns the id of the item."""
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=await resolve.next_api_message_id(db),
            posted_at=NOON,
            text=text,
            urls=[url] if url else [],
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        item_id = await resolve.resolve_capture(db, capture_id)
        await db.commit()
    return item_id


async def _item(sessions: Sessions, item_id: int) -> Item:
    async with sessions() as db:
        return await db.get_one(Item, item_id)


async def _jobs(sessions: Sessions) -> dict[JobKind, list[Job]]:
    async with sessions() as db:
        jobs = (await db.execute(sa.select(Job).order_by(Job.id))).scalars().all()
    by_kind: dict[JobKind, list[Job]] = {}
    for job in jobs:
        by_kind.setdefault(job.kind, []).append(job)
    return by_kind


async def _count(sessions: Sessions, table: type[object]) -> int:
    async with sessions() as db:
        return (await db.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()


def _context(services: Services) -> JobContext:
    """What a handler receives, for a test that calls a handler without the worker."""

    async def no_heartbeat() -> None:
        return None

    job = ClaimedJob(
        id=0,
        kind=JobKind.FETCH_ITEM,
        payload={},
        attempts=1,
        max_attempts=5,
        locked_by="test",
        locked_until=NOON,
    )
    return JobContext(job=job, services=services, heartbeat=no_heartbeat)


# --- The three steps ---------------------------------------------------------------------


def test_the_worker_knows_the_three_steps() -> None:
    discover()

    assert {JobKind.FETCH_ITEM, JobKind.ENRICH_ITEM, JobKind.EMBED_ITEM} <= set(registry.kinds())


async def test_fetch_stores_the_text_and_hands_over_to_enrich(services: Services) -> None:
    item_id = await _save(services.sessions)

    assert await _worker(services).run_one() is True

    item = await _item(services.sessions, item_id)
    assert item.enrich_state is EnrichState.FETCHED
    assert item.kind is ItemKind.NOTE
    assert item.partial is True  # it has a link, and the link was not opened
    async with services.sessions() as db:
        content = await db.get_one(ItemContent, item_id)
    assert content.text == TEXT
    assert (content.extractor, content.extractor_version) == ("echo", 1)
    assert content.final_url == URL
    assert len(content.content_hash) == 64
    jobs = await _jobs(services.sessions)
    assert [job.state for job in jobs[JobKind.FETCH_ITEM]] == [JobState.DONE]
    assert [job.state for job in jobs[JobKind.ENRICH_ITEM]] == [JobState.PENDING]
    assert jobs[JobKind.ENRICH_ITEM][0].payload == {"item_id": item_id}


async def test_a_note_without_a_link_is_not_partial(services: Services) -> None:
    item_id = await _save(services.sessions, url=None)

    await _worker(services).run_one()

    assert (await _item(services.sessions, item_id)).partial is False


async def test_enrich_writes_the_card_and_hands_over_to_embed(services: Services) -> None:
    def scripted(call: FakeLLMCall, schema: type[BaseModel]) -> BaseModel:
        assert call.task == "enrich_item"
        assert call.text == TEXT
        return steps.Card(
            title="Vector search in Postgres",
            summary="How pgvector stores and compares vectors.",
            key_points=["Vectors are a column type", "Exact search comes first"],
        )

    services = dataclasses.replace(services, llm=FakeLLM(responder=scripted))
    item_id = await _save(services.sessions)
    worker = _worker(services)

    await worker.run_one()
    await worker.run_one()

    item = await _item(services.sessions, item_id)
    assert item.enrich_state is EnrichState.ENRICHED
    assert item.title == "Vector search in Postgres"
    assert item.summary == "How pgvector stores and compares vectors."
    assert item.key_points == ["Vectors are a column type", "Exact search comes first"]
    assert item.est_minutes == 1
    assert item.enrich_version == steps.ENRICH_VERSION
    jobs = await _jobs(services.sessions)
    assert [job.state for job in jobs[JobKind.ENRICH_ITEM]] == [JobState.DONE]
    assert [job.state for job in jobs[JobKind.EMBED_ITEM]] == [JobState.PENDING]


async def test_enrich_keeps_a_title_that_the_source_gave(services: Services) -> None:
    item_id = await _save(services.sessions)
    worker = _worker(services)
    await worker.run_one()
    async with services.sessions() as db:
        await db.execute(sa.update(Item).where(Item.id == item_id).values(title="From the page"))
        await db.commit()

    await worker.run_one()

    assert (await _item(services.sessions, item_id)).title == "From the page"


async def test_the_model_gets_at_most_the_start_of_a_long_text(services: Services) -> None:
    llm = FakeLLM()
    services = dataclasses.replace(services, llm=llm)
    await _save(services.sessions, text="word " * 5000)
    worker = _worker(services)

    await worker.run_one()
    await worker.run_one()

    assert len(llm.calls) == 1
    assert len(llm.calls[0].text) <= steps.MAX_MODEL_INPUT_CHARS


async def test_an_item_without_text_is_not_sent_to_the_model(services: Services) -> None:
    llm = FakeLLM()
    services = dataclasses.replace(services, llm=llm)
    item_id = await _save(services.sessions, text="", url=None)
    worker = _worker(services)

    while await worker.run_one():
        pass

    item = await _item(services.sessions, item_id)
    assert llm.calls == []
    assert item.enrich_state is EnrichState.READY
    assert (item.title, item.summary, item.key_points, item.est_minutes) == (None, None, [], None)


async def test_embed_stores_one_vector_and_the_item_is_ready(services: Services) -> None:
    item_id = await _save(services.sessions)
    worker = _worker(services)

    for _ in range(3):
        assert await worker.run_one() is True
    assert await worker.run_one() is False  # nothing is left to do

    item = await _item(services.sessions, item_id)
    assert item.enrich_state is EnrichState.READY
    async with services.sessions() as db:
        chunk = (await db.execute(sa.select(Chunk))).scalar_one()
        embedding_set = (await db.execute(sa.select(EmbeddingSet))).scalar_one()
    assert (chunk.item_id, chunk.kind, chunk.ord) == (item_id, ChunkKind.CARD, 0)
    assert chunk.dim == 64
    assert len(chunk.embedding) == 64
    assert item.title is not None
    assert chunk.text.startswith(item.title)
    assert chunk.embedding_set_id == embedding_set.id
    assert (embedding_set.provider, embedding_set.model, embedding_set.dim) == (
        "fake",
        "hashed-bow-64",
        64,
    )
    assert embedding_set.is_active is True
    jobs = await _jobs(services.sessions)
    assert {kind: [job.state for job in found] for kind, found in jobs.items()} == {
        JobKind.FETCH_ITEM: [JobState.DONE],
        JobKind.ENRICH_ITEM: [JobState.DONE],
        JobKind.EMBED_ITEM: [JobState.DONE],
    }


async def test_two_items_share_one_embedding_set(services: Services) -> None:
    await _save(services.sessions)
    await _save(services.sessions, text="A second note", url=None)
    worker = _worker(services)

    while await worker.run_one():
        pass

    assert await _count(services.sessions, Chunk) == 2
    assert await _count(services.sessions, EmbeddingSet) == 1


def test_the_reading_time_is_at_least_one_minute() -> None:
    assert steps.reading_minutes("") == 1
    assert steps.reading_minutes("word " * 220) == 1
    assert steps.reading_minutes("word " * 221) == 2


# --- A step that runs more than once -----------------------------------------------------

STEPS: list[tuple[int, Handler, type[JobPayload], JobKind | None]] = [
    (0, steps.fetch_item, FetchItemPayload, JobKind.ENRICH_ITEM),
    (1, steps.enrich_item, EnrichItemPayload, JobKind.EMBED_ITEM),
    (2, steps.embed_item, EmbedItemPayload, None),
]


@pytest.mark.parametrize(
    ("steps_before", "handler", "payload_type", "next_kind"),
    STEPS,
    ids=["fetch", "enrich", "embed"],
)
async def test_a_step_that_runs_again_changes_nothing(
    services: Services,
    steps_before: int,
    handler: Handler,
    payload_type: type[JobPayload],
    next_kind: JobKind | None,
) -> None:
    item_id = await _save(services.sessions)
    worker = _worker(services)
    for _ in range(steps_before):
        await worker.run_one()
    payload = payload_type.model_validate({"item_id": item_id})

    await handler(_context(services), payload)
    after_first = await _item(services.sessions, item_id)
    await handler(_context(services), payload)

    after_second = await _item(services.sessions, item_id)
    assert after_second.enrich_state is after_first.enrich_state
    assert after_second.updated_at == after_first.updated_at  # the row was not written again
    assert await _count(services.sessions, ItemContent) == 1
    if next_kind is None:
        assert await _count(services.sessions, Chunk) == 1
    else:
        assert len((await _jobs(services.sessions))[next_kind]) == 1


async def test_a_step_that_ran_before_does_not_ask_the_model_again(services: Services) -> None:
    llm = FakeLLM()
    services = dataclasses.replace(services, llm=llm)
    item_id = await _save(services.sessions)
    worker = _worker(services)
    await worker.run_one()
    await worker.run_one()
    assert len(llm.calls) == 1

    await steps.enrich_item(_context(services), EnrichItemPayload(item_id=item_id))

    assert len(llm.calls) == 1  # a model call costs money; the second run must not make one


async def test_two_workers_on_the_same_step_give_one_result(services: Services) -> None:
    item_id = await _save(services.sessions)
    payload = FetchItemPayload(item_id=item_id)

    await asyncio.gather(
        steps.fetch_item(_context(services), payload),
        steps.fetch_item(_context(services), payload),
    )

    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.FETCHED
    assert await _count(services.sessions, ItemContent) == 1
    assert len((await _jobs(services.sessions))[JobKind.ENRICH_ITEM]) == 1


class SlowModel:
    """A model that answers only when the test allows it."""

    def __init__(self) -> None:
        self.asked = asyncio.Event()
        self.may_answer = asyncio.Event()

    async def generate[T: BaseModel](
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        parts: Sequence[TextPart | ImagePart],
        schema: type[T],
        max_output_tokens: int,
    ) -> LLMResult[T]:
        self.asked.set()
        await self.may_answer.wait()
        late = {"title": "late title", "summary": "late summary", "key_points": ["late"]}
        return LLMResult[schema](value=schema.model_validate(late), model="slow")


async def test_a_slow_worker_that_finishes_late_does_not_move_the_item_back(
    services: Services,
) -> None:
    item_id = await _save(services.sessions)
    worker = _worker(services)
    await worker.run_one()  # fetch
    # A slow worker starts the enrich step and waits for its model.
    slow_model = SlowModel()
    slow_services = dataclasses.replace(services, llm=slow_model)
    slow_step = asyncio.ensure_future(
        steps.enrich_item(_context(slow_services), EnrichItemPayload(item_id=item_id))
    )
    await slow_model.asked.wait()
    # In that time, a different worker does the same step and the rest of the pipeline.
    while await worker.run_one():
        pass
    finished = await _item(services.sessions, item_id)
    assert finished.enrich_state is EnrichState.READY

    slow_model.may_answer.set()
    await slow_step

    item = await _item(services.sessions, item_id)
    assert item.enrich_state is EnrichState.READY  # not back to "enriched"
    assert item.summary == finished.summary  # not "late summary"
    assert item.updated_at == finished.updated_at
    assert len((await _jobs(services.sessions))[JobKind.EMBED_ITEM]) == 1
    assert await _count(services.sessions, Chunk) == 1


async def test_a_job_for_an_item_that_does_not_exist_is_dead_at_once(services: Services) -> None:
    async with services.sessions() as db:
        await queue.enqueue(db, JobKind.FETCH_ITEM, FetchItemPayload(item_id=999))
        await db.commit()

    await _worker(services).run_one()

    job = (await _jobs(services.sessions))[JobKind.FETCH_ITEM][0]
    assert job.state is JobState.DEAD
    assert job.attempts == 1
    assert job.last_error == "item 999 does not exist"


# --- A worker that dies ------------------------------------------------------------------


async def _die_holding_the_next_job(services: Services, name: str) -> ClaimedJob:
    """A worker takes the next job and dies: the job stays "running", and nobody works on it."""
    async with services.sessions() as db:
        job = await queue.claim(db, worker_id=name, lease=timedelta(seconds=60))
        await db.commit()
    assert job is not None
    return job


async def _the_lease_runs_out(services: Services) -> None:
    async with services.sessions() as db:
        reaped = await queue.reap_expired(db, now=datetime.now(UTC) + timedelta(minutes=2))
        await db.commit()
    assert reaped == 1


async def test_a_worker_that_dies_before_it_saves_leaves_the_step_to_the_next_worker(
    services: Services,
) -> None:
    item_id = await _save(services.sessions)
    await _die_holding_the_next_job(services, "dead-worker")
    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.PENDING

    await _the_lease_runs_out(services)
    worker = _worker(services, "second-worker")
    while await worker.run_one():
        pass

    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.READY
    fetch = (await _jobs(services.sessions))[JobKind.FETCH_ITEM]
    assert [(job.state, job.attempts) for job in fetch] == [(JobState.DONE, 2)]
    assert await _count(services.sessions, ItemContent) == 1
    assert await _count(services.sessions, Chunk) == 1


async def test_a_worker_that_dies_after_it_saved_does_not_repeat_the_step(
    services: Services,
) -> None:
    item_id = await _save(services.sessions)
    held = await _die_holding_the_next_job(services, "dead-worker")
    # The handler finished and saved its result. The worker died before it could report that.
    await steps.fetch_item(_context(services), FetchItemPayload.model_validate(held.payload))
    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.FETCHED

    await _the_lease_runs_out(services)
    worker = _worker(services, "second-worker")
    while await worker.run_one():
        pass

    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.READY
    jobs = await _jobs(services.sessions)
    assert [(job.state, job.attempts) for job in jobs[JobKind.FETCH_ITEM]] == [(JobState.DONE, 2)]
    assert [(job.state, job.attempts) for job in jobs[JobKind.ENRICH_ITEM]] == [(JobState.DONE, 1)]
    assert [(job.state, job.attempts) for job in jobs[JobKind.EMBED_ITEM]] == [(JobState.DONE, 1)]
    assert await _count(services.sessions, ItemContent) == 1
    assert await _count(services.sessions, Chunk) == 1
