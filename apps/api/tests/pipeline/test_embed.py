"""The embed step: a vector for the card of an item, and one for each piece of a long text.
Each test name is one rule. A pretend embedder: no test reaches a real API."""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import (
    Actor,
    CaptureSource,
    ChunkKind,
    EnrichState,
    JobKind,
    JobState,
    Visibility,
)
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.jobs import ClaimedJob, EmbedItemPayload, JobContext, Services
from unshelve.db.models import Chunk, EmbeddingSet, Event, Item, ItemContent, Job
from unshelve.embed.chunks import card_text, pieces_of
from unshelve.ingest import resolve
from unshelve.pipeline import items as steps
from unshelve.providers.ledger import CachedLLM
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services
from unshelve.testing.fakes import FakeEmbedder, FakeLLM, FakeLLMCall

NOON = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
SHORT_TEXT = "A job queue in Postgres uses SKIP LOCKED, so that two workers never take one job."
# Six paragraphs of about 525 characters: three pieces of two paragraphs each.
SENTENCE = "Postgres keeps each job in one row, and a worker locks the row. "
LONG_TEXT = "\n\n".join(f"Part {number}. " + SENTENCE * 8 for number in range(6))

Sessions = async_sessionmaker[AsyncSession]


class RecordingEmbedder:
    """The fake embedder, with the texts of each call. It can fail, or wait for the test."""

    def __init__(self, *, dim: int = 64, error: ProviderError | None = None) -> None:
        self._inner = FakeEmbedder(dim)
        self.spec = self._inner.spec
        self.calls: list[list[str]] = []
        self.error = error
        self.asked = asyncio.Event()
        self.may_answer: asyncio.Event | None = None

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self.asked.set()
        if self.may_answer is not None:
            await self.may_answer.wait()
        if self.error is not None:
            raise self.error
        return await self._inner.embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        return await self._inner.embed_query(text)


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    return build_services(Settings(env="test", database_url=private_database_url), committed_db)


def _with(services: Services, embedder: RecordingEmbedder) -> Services:
    return dataclasses.replace(services, embedder=embedder)


def _no_card(services: Services) -> Services:
    """The services with a model that writes no card."""

    def decline(call: FakeLLMCall, schema: type[BaseModel]) -> BaseModel | None:
        raise ProviderError("the model declined", retryable=False)

    model = CachedLLM(FakeLLM(responder=decline), services.sessions, model=FakeLLM.model)
    return dataclasses.replace(services, llm=model)


def _worker(services: Services) -> Worker:
    discover()
    return Worker(services, registry, WorkerConfig(worker_id="w1"))


async def _run_all(services: Services) -> None:
    worker = _worker(services)
    while await worker.run_one():
        pass


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


async def _item(sessions: Sessions, item_id: int) -> Item:
    async with sessions() as db:
        return await db.get_one(Item, item_id)


async def _vectors(sessions: Sessions, item_id: int, model: str = "hashed-bow-64") -> list[Chunk]:
    """The vectors of the item in one set: the card first, then the pieces in order."""
    async with sessions() as db:
        found = await db.scalars(
            sa.select(Chunk)
            .join(EmbeddingSet, EmbeddingSet.id == Chunk.embedding_set_id)
            .where(Chunk.item_id == item_id, EmbeddingSet.model == model)
        )
        return sorted(found.all(), key=lambda chunk: (chunk.kind != ChunkKind.CARD, chunk.ord))


async def _events(sessions: Sessions, item_id: int) -> list[Event]:
    async with sessions() as db:
        found = await db.scalars(sa.select(Event).where(Event.item_id == item_id))
        return list(found.all())


async def _embed_job(sessions: Sessions) -> Job:
    async with sessions() as db:
        return (await db.scalars(sa.select(Job).where(Job.kind == JobKind.EMBED_ITEM))).one()


def _context(services: Services) -> JobContext:
    """What a handler receives, for a test that calls the step without the worker."""

    async def no_heartbeat() -> None:
        return None

    job = ClaimedJob(
        id=0,
        kind=JobKind.EMBED_ITEM,
        payload={},
        attempts=1,
        max_attempts=5,
        locked_by="test",
        locked_until=NOON,
    )
    return JobContext(job=job, services=services, heartbeat=no_heartbeat)


async def _embed_again(services: Services, item_id: int) -> None:
    await steps.embed_item(_context(services), EmbedItemPayload(item_id=item_id))


async def _change(sessions: Sessions, item_id: int, **values: object) -> None:
    async with sessions() as db:
        await db.execute(sa.update(Item).where(Item.id == item_id).values(**values))
        await db.commit()


# --- The vectors of an item --------------------------------------------------------------


async def test_a_long_text_gets_a_vector_for_its_card_and_one_for_each_piece(
    services: Services,
) -> None:
    embedder = RecordingEmbedder()
    services = _with(services, embedder)
    item_id = await _save(services.sessions, LONG_TEXT)

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert item.enrich_state is EnrichState.READY
    card, *body = await _vectors(services.sessions, item_id)
    expected = pieces_of(LONG_TEXT)
    assert len(expected) == 3
    assert (card.kind, card.ord, card.text) == (
        ChunkKind.CARD,
        0,
        card_text(item.title, item.summary, item.key_points),
    )
    assert [(chunk.kind, chunk.ord) for chunk in body] == [(ChunkKind.BODY, n) for n in range(3)]
    assert [(chunk.char_start, chunk.char_end) for chunk in body] == [
        (piece.start, piece.end) for piece in expected
    ]
    assert all(LONG_TEXT[chunk.char_start : chunk.char_end] == chunk.text for chunk in body)
    assert all(chunk.dim == len(chunk.embedding) == 64 for chunk in (card, *body))
    # One request for the whole item.
    assert embedder.calls == [[card.text, *(piece.text for piece in expected)]]


async def test_a_short_text_gets_only_the_vector_of_its_card(services: Services) -> None:
    item_id = await _save(services.sessions, SHORT_TEXT)

    await _run_all(services)

    assert [chunk.kind for chunk in await _vectors(services.sessions, item_id)] == [ChunkKind.CARD]


async def test_an_item_without_a_card_gets_the_start_of_its_text_as_its_card(
    services: Services,
) -> None:
    services = _no_card(services)
    item_id = await _save(services.sessions, SHORT_TEXT)

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.title, item.summary, item.key_points) == (None, None, [])
    [card] = await _vectors(services.sessions, item_id)
    assert card.text == SHORT_TEXT


# --- A second run ------------------------------------------------------------------------


async def test_a_second_run_asks_the_embedder_nothing_and_writes_nothing(
    services: Services,
) -> None:
    embedder = RecordingEmbedder()
    services = _with(services, embedder)
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    before = await _vectors(services.sessions, item_id)
    item_before = await _item(services.sessions, item_id)

    await _embed_again(services, item_id)

    assert len(embedder.calls) == 1
    after = await _vectors(services.sessions, item_id)
    assert [(c.id, c.text, c.created_at) for c in after] == [
        (c.id, c.text, c.created_at) for c in before
    ]
    assert (await _item(services.sessions, item_id)).updated_at == item_before.updated_at


async def test_a_new_card_gets_a_new_vector_and_the_pieces_keep_theirs(
    services: Services,
) -> None:
    embedder = RecordingEmbedder()
    services = _with(services, embedder)
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    _, *body_before = await _vectors(services.sessions, item_id)
    await _change(services.sessions, item_id, summary="A new summary.")

    await _embed_again(services, item_id)

    item = await _item(services.sessions, item_id)
    new_card = card_text(item.title, item.summary, item.key_points)
    assert embedder.calls[-1] == [new_card]  # only the text that changed
    card, *body = await _vectors(services.sessions, item_id)
    assert card.text == new_card
    assert [(c.id, c.created_at) for c in body] == [(c.id, c.created_at) for c in body_before]


async def test_the_pieces_of_an_old_text_go_when_the_text_is_short_now(
    services: Services,
) -> None:
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    async with services.sessions() as db:
        await db.execute(
            sa.update(ItemContent)
            .where(ItemContent.item_id == item_id)
            .values(text=SHORT_TEXT, content_hash="new")
        )
        await db.commit()

    await _embed_again(services, item_id)

    assert [chunk.kind for chunk in await _vectors(services.sessions, item_id)] == [ChunkKind.CARD]


async def test_an_item_that_does_not_wait_for_its_vectors_is_left_alone(
    services: Services,
) -> None:
    embedder = RecordingEmbedder()
    services = _with(services, embedder)
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    vectors_before = await _vectors(services.sessions, item_id)
    # A new version of the message sends the item back to the start, and its card changes.
    await _change(
        services.sessions, item_id, enrich_state=EnrichState.PENDING, summary="A new summary."
    )

    await _embed_again(services, item_id)

    assert len(embedder.calls) == 1  # the first run only
    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.PENDING
    assert [c.id for c in await _vectors(services.sessions, item_id)] == [
        c.id for c in vectors_before
    ]


# --- A new embedder ----------------------------------------------------------------------


async def test_a_new_embedder_fills_its_own_set_and_the_old_set_stays_active(
    services: Services,
) -> None:
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    old = await _vectors(services.sessions, item_id)
    new_embedder = RecordingEmbedder(dim=32)

    await _embed_again(_with(services, new_embedder), item_id)

    async with services.sessions() as db:
        found = (await db.execute(sa.select(EmbeddingSet).order_by(EmbeddingSet.id))).scalars()
        assert [(s.model, s.is_active) for s in found] == [
            ("hashed-bow-64", True),
            ("hashed-bow-32", False),
        ]
    new = await _vectors(services.sessions, item_id, model="hashed-bow-32")
    assert [(c.kind, c.ord, c.dim) for c in new] == [(c.kind, c.ord, 32) for c in old]
    assert [c.id for c in await _vectors(services.sessions, item_id)] == [c.id for c in old]
    # The item was ready before: its scores stay, and no score job starts (a paid question).
    async with services.sessions() as db:
        scores = await db.scalar(
            sa.select(sa.func.count()).select_from(Job).where(Job.kind == JobKind.SCORE_ITEM)
        )
    assert scores == 1  # the one of the first pipeline run


async def _new_card(sessions: Sessions, item_id: int) -> None:
    await _change(sessions, item_id, summary="A newer summary.")


async def _new_text(sessions: Sessions, item_id: int) -> None:
    async with sessions() as db:
        await db.execute(
            sa.update(ItemContent)
            .where(ItemContent.item_id == item_id)
            .values(text=LONG_TEXT + "\n\nA new paragraph.", content_hash="newer")
        )
        await db.commit()


async def _back_to_the_start(sessions: Sessions, item_id: int) -> None:
    await _change(sessions, item_id, enrich_state=EnrichState.PENDING)


@pytest.mark.parametrize(
    "change", [_new_card, _new_text, _back_to_the_start], ids=["card", "text", "state"]
)
async def test_an_item_that_changed_during_the_call_gets_no_vectors_of_its_old_text(
    services: Services, change: Callable[[Sessions, int], Awaitable[None]]
) -> None:
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    slow = RecordingEmbedder(dim=32)
    slow.may_answer = asyncio.Event()
    step = asyncio.ensure_future(_embed_again(_with(services, slow), item_id))
    await slow.asked.wait()

    # While the embedder works, the item changes: a new version of the message, for example.
    await change(services.sessions, item_id)
    slow.may_answer.set()
    await step

    assert await _vectors(services.sessions, item_id, model="hashed-bow-32") == []


async def test_an_item_whose_card_changed_during_the_call_does_not_become_ready(
    services: Services,
) -> None:
    slow = RecordingEmbedder()
    slow.may_answer = asyncio.Event()
    services = _with(services, slow)
    item_id = await _save(services.sessions, LONG_TEXT)
    worker = _worker(services)
    await worker.run_one()  # fetch
    await worker.run_one()  # enrich
    step = asyncio.ensure_future(_embed_again(services, item_id))
    await slow.asked.wait()

    await _change(services.sessions, item_id, title="A title that you wrote")
    slow.may_answer.set()
    await step

    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.ENRICHED
    assert await _vectors(services.sessions, item_id) == []


# --- When the embedder fails -------------------------------------------------------------


async def test_a_failure_that_can_pass_is_tried_again_later(services: Services) -> None:
    error = ProviderError("voyage 429: slow down", retryable=True, retry_after=30)
    services = _with(services, RecordingEmbedder(error=error))
    item_id = await _save(services.sessions, LONG_TEXT)
    worker = _worker(services)
    await worker.run_one()  # fetch
    await worker.run_one()  # enrich
    before = datetime.now(UTC)

    await worker.run_one()  # embed

    job = await _embed_job(services.sessions)
    assert (job.state, job.attempts) == (JobState.PENDING, 1)
    assert before + timedelta(seconds=25) <= job.run_after <= before + timedelta(seconds=35)
    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.ENRICHED


async def test_the_last_attempt_leaves_the_item_ready_without_vectors(
    services: Services,
) -> None:
    services = _with(services, RecordingEmbedder(error=ProviderError("busy", retryable=True)))
    item_id = await _save(services.sessions, LONG_TEXT)
    worker = _worker(services)
    await worker.run_one()  # fetch
    await worker.run_one()  # enrich
    async with services.sessions() as db:  # the next attempt is the last one
        await db.execute(sa.update(Job).values(max_attempts=1))
        await db.commit()

    await _run_all(services)

    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.READY
    assert await _vectors(services.sessions, item_id) == []
    (event,) = await _events(services.sessions, item_id)
    assert (event.actor, event.type, event.payload) == (
        Actor.WORKER,
        "embed_failed",
        {"reason": "busy", "attempt": 1},
    )


async def test_a_failure_that_cannot_pass_leaves_the_item_ready_at_once(
    services: Services,
) -> None:
    error = ProviderError("voyage 400: the model is unknown", retryable=False)
    services = _with(services, RecordingEmbedder(error=error))
    item_id = await _save(services.sessions, LONG_TEXT)

    await _run_all(services)

    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.READY
    assert (await _embed_job(services.sessions)).state is JobState.DONE
    (event,) = await _events(services.sessions, item_id)
    assert event.payload["reason"] == "voyage 400: the model is unknown"


async def test_a_failed_new_card_removes_the_vector_of_the_old_card_only(
    services: Services,
) -> None:
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    _, *body_before = await _vectors(services.sessions, item_id)
    await _change(services.sessions, item_id, summary="A new summary.")
    failing = RecordingEmbedder(error=ProviderError("voyage 400: too long", retryable=False))

    await _embed_again(_with(services, failing), item_id)

    # The vector of the old card is gone: a search must not find the item for its old words.
    # The pieces did not change, so their vectors stay. A backfill gives the card its vector.
    body = await _vectors(services.sessions, item_id)
    assert [(c.id, c.kind) for c in body] == [(c.id, c.kind) for c in body_before]
    assert [event.type for event in await _events(services.sessions, item_id)] == ["embed_failed"]


# --- A public item -----------------------------------------------------------------------


async def _publish_jobs(sessions: Sessions) -> list[Job]:
    async with sessions() as db:
        found = await db.scalars(
            sa.select(Job).where(Job.kind == JobKind.PUBLISH_FEED, Job.state == JobState.PENDING)
        )
        return list(found.all())


async def _make_public(sessions: Sessions, item_id: int) -> None:
    await _change(
        sessions,
        item_id,
        visibility=Visibility.PUBLIC,
        public_slug=f"item-{item_id}",
        published_at=NOON,
    )


async def _process_again(services: Services, item_id: int) -> None:
    """Send the item back to the start, as "Process again" does, and run each job."""
    async with services.sessions() as db:
        item = await db.get_one(Item, item_id)
        await resolve.restart_item(db, item.owner_id, item_id)
        await db.commit()
    await _run_all(services)


async def test_a_public_item_that_is_ready_again_queues_the_publish_job(
    services: Services,
) -> None:
    # A new run writes a new card: the public record of the item changes.
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    await _make_public(services.sessions, item_id)

    await _process_again(services, item_id)

    owner_id = (await _item(services.sessions, item_id)).owner_id
    [job] = await _publish_jobs(services.sessions)
    assert (job.payload, job.dedupe_key) == ({"owner_id": owner_id}, f"feed:{owner_id}")


async def test_a_public_item_that_is_ready_without_vectors_queues_the_publish_job_too(
    services: Services,
) -> None:
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    await _make_public(services.sessions, item_id)
    # Without stored vectors, the step must ask the embedder, and the embedder fails.
    async with services.sessions() as db:
        await db.execute(sa.delete(Chunk).where(Chunk.item_id == item_id))
        await db.commit()
    failing = RecordingEmbedder(error=ProviderError("the embedder is gone", retryable=False))

    await _process_again(_with(services, failing), item_id)

    assert failing.calls
    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.READY
    assert len(await _publish_jobs(services.sessions)) == 1


async def test_a_private_item_that_is_ready_again_queues_no_publish_job(
    services: Services,
) -> None:
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)

    await _process_again(services, item_id)

    assert await _publish_jobs(services.sessions) == []


async def test_new_vectors_of_a_ready_public_item_queue_no_publish_job(
    services: Services,
) -> None:
    # Vectors are not in the public record, so the feed does not change.
    item_id = await _save(services.sessions, LONG_TEXT)
    await _run_all(services)
    await _make_public(services.sessions, item_id)

    await _embed_again(_with(services, RecordingEmbedder(dim=32)), item_id)

    assert await _publish_jobs(services.sessions) == []
