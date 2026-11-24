"""The text that a step writes to ``jobs.last_error`` keeps the mark ``FIX_BY_HAND``.

A watcher finds a job that waits for a person with one search for the mark in that column.
The tests of the providers check the text of each error. These tests check that each step
keeps the mark when it writes the text. A pretend model, a pretend embedder and a pretend
batch API: no test reaches a real API."""

import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope, MediaRef
from unshelve.contracts.enums import CaptureSource, JobKind, JobState
from unshelve.contracts.errors import FIX_BY_HAND, ProviderError
from unshelve.contracts.jobs import Services
from unshelve.db.models import Goal, Job
from unshelve.enrich import card
from unshelve.extract import image
from unshelve.extract.image import ImageReading
from unshelve.ingest import resolve
from unshelve.pipeline import batches
from unshelve.providers.ledger import CachedLLM
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.relevance import prompt as relevance
from unshelve.services import build_services
from unshelve.testing.conformance import TINY_PNG
from unshelve.testing.fakes import FakeBatches, FakeEmbedder, FakeFiles, FakeLLM, FakeLLMCall

pytestmark = pytest.mark.db

Sessions = async_sessionmaker[AsyncSession]

NOON = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
PHOTO = MediaRef(kind="photo", file_id="AgACAgMadeUpPhoto", file_unique_id="AQADmadeup")
READING = ImageReading(description="A whiteboard with a diagram of a job queue.", text="")
EMBED = "embed"  # the part that refuses is the embedder, not a task of the model
GOALS = "goals"  # the embedder refuses the vectors of the goals, for the prefilter


def _refusal(part: str) -> ProviderError:
    """The error of a provider that refuses the key, as the adapters write it."""
    if part in (EMBED, GOALS):
        return ProviderError(
            f"voyage 401: {FIX_BY_HAND}: Provided API key is invalid.", retryable=True
        )
    return ProviderError(f"401 authentication_error: {FIX_BY_HAND}", retryable=True)


class _Embedder(FakeEmbedder):
    def __init__(self, refuses: str) -> None:
        super().__init__()
        self._refuses = refuses

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if self._refuses == EMBED:
            raise _refusal(EMBED)
        return await super().embed_documents(texts)

    async def embed_query(self, text: str) -> list[float]:
        if self._refuses == GOALS:
            raise _refusal(GOALS)
        return await super().embed_query(text)


def _services(sessions: Sessions, database_url: str, refuses: str) -> Services:
    """The services in which one part refuses the key: a task of the model, or the embedder."""

    def respond(call: FakeLLMCall, schema: type[BaseModel]) -> BaseModel | None:
        if call.task == refuses:
            raise _refusal(refuses)
        return READING if call.task == image.TASK else None  # None: a card from the text

    # The prefilter of the score step asks the embedder for the vectors of the goals. Only the
    # case of these vectors needs it. Without the prefilter (-1), the model scores each item.
    prefilter = 0.3 if refuses == GOALS else -1
    settings = Settings(env="test", database_url=database_url, score_prefilter=prefilter)
    return dataclasses.replace(
        build_services(settings, sessions),
        files=FakeFiles({str(PHOTO.file_id): TINY_PNG}),
        llm=CachedLLM(FakeLLM(responder=respond), sessions, model=FakeLLM.model),
        embedder=_Embedder(refuses),
    )


async def _save_photo(sessions: Sessions) -> None:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=await resolve.next_api_message_id(db),
            posted_at=NOON,
            text="The whiteboard of today",
            urls=[],
            media=[PHOTO],
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        assert await resolve.resolve_capture(db, capture_id) is not None
        await db.commit()


async def _add_goal(sessions: Sessions) -> None:
    """An active goal. The score step of a ready item asks the embedder (when the prefilter is
    on) or the model about it."""
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        db.add(Goal(owner_id=owner_id, slug="job-queues", title="Job queues", version=1))
        await db.commit()


async def _run(services: Services) -> None:
    """Run each job that is due now. A job that fails waits for a later try."""
    discover()
    worker = Worker(services, registry, WorkerConfig(worker_id="w1"))
    while await worker.run_one():
        pass


async def _job(sessions: Sessions, kind: JobKind) -> Job:
    async with sessions() as db:
        return (await db.scalars(sa.select(Job).where(Job.kind == kind))).one()


@pytest.mark.parametrize(
    ("refuses", "kind"),
    [
        (image.TASK, JobKind.FETCH_ITEM),  # the model reads the photo
        (card.TASK, JobKind.ENRICH_ITEM),  # the model writes the card
        (EMBED, JobKind.EMBED_ITEM),  # Voyage makes the vectors
        (GOALS, JobKind.SCORE_ITEM),  # Voyage makes the vectors of the goals
        (relevance.TASK, JobKind.SCORE_ITEM),  # the model scores the item
    ],
)
async def test_a_step_keeps_the_mark_in_jobs_last_error(
    committed_db: Sessions, private_database_url: str, refuses: str, kind: JobKind
) -> None:
    services = _services(committed_db, private_database_url, refuses)
    await _save_photo(committed_db)
    await _add_goal(committed_db)

    await _run(services)

    job = await _job(committed_db, kind)
    assert job.state is JobState.PENDING  # the item waits, and goes on after the fix
    assert job.last_error is not None
    assert FIX_BY_HAND in job.last_error  # the one search of the watcher


class _RefusingBatches(FakeBatches):
    """The batch API, when Claude refuses the key at a look at a batch."""

    async def ended(self, batch_id: str) -> bool:
        raise _refusal(card.TASK)


async def test_the_collect_step_of_a_batch_keeps_the_mark_in_jobs_last_error(
    committed_db: Sessions, private_database_url: str
) -> None:
    settings = Settings(env="test", database_url=private_database_url, cards="batch")
    provider = _RefusingBatches()
    services = dataclasses.replace(
        build_services(settings, committed_db),
        files=FakeFiles({str(PHOTO.file_id): TINY_PNG}),
        batches=provider,
    )
    await _save_photo(committed_db)
    await _run(services)  # in batch mode, the card waits for a batch
    waiting = await batches.waiting_cards(committed_db, provider.model)
    assert len(waiting.questions) == 1  # the card of the photo
    await batches.send_batch(committed_db, provider, waiting)
    async with committed_db() as db:  # the first look at the batch is due now
        await db.execute(
            sa.update(Job).where(Job.kind == JobKind.COLLECT_BATCH).values(run_after=sa.func.now())
        )
        await db.commit()

    await _run(services)

    job = await _job(committed_db, JobKind.COLLECT_BATCH)
    assert job.state is JobState.PENDING  # the worker looks at the batch again after the fix
    assert job.last_error is not None
    assert FIX_BY_HAND in job.last_error  # the one search of the watcher
