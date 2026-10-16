"""Batch mode for the cards (UNSHELVE_CARDS=batch): the cards wait, one batch asks for them at
half the price, and the worker collects the answers. A pretend batch provider and a pretend
model: no network, and no paid call."""

import dataclasses
import hashlib
import math
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import BatchQuestion, CaptureEnvelope, LLMUsage, TextPart
from unshelve.contracts.enums import BatchState, CaptureSource, EnrichState, JobKind, JobState
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.jobs import Services
from unshelve.db.models import Item, ItemContent, Job, LLMBatch, LLMBatchItem, LLMCall
from unshelve.ingest import resolve
from unshelve.pipeline import batches
from unshelve.providers.ledger import CachedLLM, cost_usd
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services
from unshelve.testing.fakes import FakeBatches, FakeLLM

Sessions = async_sessionmaker[AsyncSession]

NOON = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
NOTES = (
    "Leases make a job queue on one Postgres table safe.",
    "A heartbeat moves the lease of a worker forward.",
    "SKIP LOCKED lets many workers share one queue.",
)


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    settings = Settings(env="test", database_url=private_database_url, cards="batch")
    return build_services(settings, committed_db)


def _provider(services: Services) -> FakeBatches:
    assert isinstance(services.batches, FakeBatches)
    return services.batches


def _card_calls(services: Services) -> int:
    """The questions that the card step asked the model now, not in a batch."""
    assert isinstance(services.llm, CachedLLM)
    assert isinstance(services.llm.inner, FakeLLM)
    return sum(call.task == "enrich_item" for call in services.llm.inner.calls)


async def _save(sessions: Sessions, text: str) -> int:
    """A note that you saved: the fetch step reads its text, then the card step runs."""
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=await resolve.next_api_message_id(db),
            posted_at=NOON,
            text=text,
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        item_id = await resolve.resolve_capture(db, capture_id)
        await db.commit()
    assert item_id is not None
    return item_id


async def _fetched(sessions: Sessions, text: str, number: int) -> int:
    """An item that the fetch step read already, as when two links give one text."""
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        item = Item(
            owner_id=owner_id,
            canonical_url=f"https://mirror-{number}.example/queues",
            first_captured_at=NOON,
            last_captured_at=NOON,
            enrich_state=EnrichState.FETCHED,
        )
        db.add(item)
        await db.flush()
        db.add(
            ItemContent(
                item_id=item.id,
                owner_id=owner_id,
                text=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
                extractor="article",
                extractor_version=1,
            )
        )
        await db.commit()
        return item.id


def _worker(services: Services) -> Worker:
    discover()
    return Worker(services, registry, WorkerConfig(worker_id="w1"))


async def _run_due(services: Services) -> None:
    """Run each job that is due now."""
    worker = _worker(services)
    while await worker.run_one():
        pass


async def _run_all(services: Services) -> None:
    """Run each job, also a job that waits: the time of a delay passes at once."""
    worker = _worker(services)
    while True:
        async with services.sessions() as db:
            await db.execute(
                sa.update(Job)
                .where(Job.state == JobState.PENDING)
                .values(run_after=sa.func.now() - sa.text("interval '1 second'"))
            )
            await db.commit()
        if not await worker.run_one():
            return
        while await worker.run_one():
            pass


async def _send(services: Services) -> int:
    waiting = await batches.waiting_cards(services.sessions, _provider(services).model)
    return await batches.send_batch(services.sessions, _provider(services), waiting)


async def _states(sessions: Sessions, items: list[int]) -> list[EnrichState]:
    async with sessions() as db:
        found = {
            item.id: item.enrich_state
            for item in (await db.scalars(sa.select(Item).where(Item.id.in_(items)))).all()
        }
    return [found[item] for item in items]


async def _batch(sessions: Sessions, batch_id: int) -> LLMBatch:
    async with sessions() as db:
        return await db.get_one(LLMBatch, batch_id)


async def _collect_job(sessions: Sessions) -> Job:
    async with sessions() as db:
        return (await db.scalars(sa.select(Job).where(Job.kind == JobKind.COLLECT_BATCH))).one()


# --- A card waits for a batch --------------------------------------------------------------------


async def test_in_batch_mode_a_card_waits_and_no_model_is_asked(services: Services) -> None:
    items = [await _save(services.sessions, note) for note in NOTES]

    await _run_all(services)

    assert await _states(services.sessions, items) == [EnrichState.FETCHED] * 3
    assert _card_calls(services) == 0
    async with services.sessions() as db:
        jobs = (await db.scalars(sa.select(Job).where(Job.kind == JobKind.ENRICH_ITEM))).all()
    # Each card step ended cleanly: no job tries again and again, the item waits without one.
    assert [(job.state, job.attempts, job.last_error) for job in jobs] == [
        (JobState.DONE, 1, None)
    ] * 3


async def test_one_batch_asks_for_each_waiting_card(services: Services) -> None:
    for note in NOTES:
        await _save(services.sessions, note)
    await _run_all(services)
    started = datetime.now(UTC)

    batch_id = await _send(services)

    [sent] = _provider(services).sent.values()
    assert len(sent) == 3
    batch = await _batch(services.sessions, batch_id)
    assert (batch.state, batch.questions, batch.provider_batch_id) == (
        BatchState.SENT,
        3,
        "msgbatch_fake_1",
    )
    job = await _collect_job(services.sessions)
    assert (job.state, job.max_attempts) == (JobState.PENDING, batches.COLLECT_ATTEMPTS)
    assert job.run_after >= started + batches.COLLECT_WAIT - timedelta(seconds=5)


async def test_after_the_batch_each_card_is_made_and_no_model_is_asked(
    services: Services,
) -> None:
    items = [await _save(services.sessions, note) for note in NOTES]
    await _run_all(services)
    batch_id = await _send(services)

    await _run_all(services)

    assert await _states(services.sessions, items) == [EnrichState.READY] * 3
    assert _card_calls(services) == 0
    async with services.sessions() as db:
        kept = (await db.scalars(sa.select(LLMCall).where(LLMCall.task == "enrich_item"))).all()
    assert [row.batch_id for row in kept] == [batch_id] * 3
    batch = await _batch(services.sessions, batch_id)
    assert (batch.state, batch.answered, batch.not_answered) == (BatchState.COLLECTED, 3, 0)


async def test_two_items_with_one_text_share_one_question(services: Services) -> None:
    # Two links that give the same text: one question in the batch, and both get the card.
    items = [
        await _fetched(services.sessions, NOTES[0], 1),
        await _fetched(services.sessions, NOTES[0], 2),
        await _fetched(services.sessions, NOTES[1], 3),
    ]

    waiting = await batches.waiting_cards(services.sessions, _provider(services).model)
    await batches.send_batch(services.sessions, _provider(services), waiting)
    await _run_all(services)

    assert (len(waiting.items), len(waiting.questions)) == (3, 2)
    assert await _states(services.sessions, items) == [EnrichState.READY] * 3
    # The estimate counts each question one time.
    texts = [
        part.text
        for question in waiting.questions
        for part in question.parts
        if isinstance(part, TextPart)
    ]
    assert waiting.input_tokens == sum(900 + math.ceil(len(text) / 2.5) for text in texts)
    assert waiting.output_tokens == 2 * 400


async def test_a_second_send_asks_for_no_card_that_has_its_answer(services: Services) -> None:
    for note in NOTES:
        await _save(services.sessions, note)
    await _run_all(services)
    await _send(services)
    while_it_runs = await batches.waiting_cards(services.sessions, _provider(services).model)
    async with services.sessions() as db:  # the batch has ended: the worker collects it now
        await db.execute(
            sa.update(Job).where(Job.kind == JobKind.COLLECT_BATCH).values(run_after=sa.func.now())
        )
        await db.commit()
    await _worker(services).run_one()  # only the collection: the card steps have not run yet

    answered = await batches.waiting_cards(services.sessions, _provider(services).model)

    assert (
        await _states(services.sessions, await _all_items(services.sessions))
        == [EnrichState.FETCHED] * 3
    )
    assert (while_it_runs.questions, answered.questions) == ([], [])


async def _all_items(sessions: Sessions) -> list[int]:
    async with sessions() as db:
        return list((await db.scalars(sa.select(Item.id).order_by(Item.id))).all())


async def test_an_item_without_text_has_no_question(services: Services) -> None:
    await _fetched(services.sessions, "  ", 1)

    waiting = await batches.waiting_cards(services.sessions, _provider(services).model)

    assert (waiting.items, waiting.questions) == ([], [])


async def test_a_batch_that_was_never_sent_holds_its_questions_ten_minutes(
    services: Services,
) -> None:
    # A command that stopped after it stored the batch, before the provider answered.
    await _fetched(services.sessions, NOTES[0], 1)
    waiting = await batches.waiting_cards(services.sessions, _provider(services).model)
    async with services.sessions() as db:
        batch = LLMBatch(
            model="fake-llm",
            task="enrich_item",
            prompt_version="enrich-2",
            questions=1,
            estimated_cost_usd=0,
        )
        db.add(batch)
        await db.flush()
        [(item_id, key, request)] = waiting.items
        db.add(LLMBatchItem(batch_id=batch.id, item_id=item_id, custom_id=key, request=request))
        await db.commit()
        stored_at = batch.created_at

    soon = await batches.waiting_cards(
        services.sessions, "fake-llm", now=stored_at + timedelta(minutes=9)
    )
    later = await batches.waiting_cards(
        services.sessions, "fake-llm", now=stored_at + timedelta(minutes=11)
    )

    assert (len(soon.questions), len(later.questions)) == (0, 1)


async def test_an_answer_of_a_batch_costs_half_in_the_ledger(services: Services) -> None:
    services = dataclasses.replace(services, batches=FakeBatches(model="claude-haiku-5-5"))
    await _fetched(services.sessions, NOTES[0], 1)
    await _send(services)

    await _run_all(services)

    async with services.sessions() as db:
        [row] = (await db.scalars(sa.select(LLMCall))).all()
    usage = LLMUsage(input_tokens=row.input_tokens, output_tokens=row.output_tokens)
    half = cost_usd("claude-haiku-5-5", usage, batch=True)
    assert half is not None
    assert half > 0
    # The ledger keeps 6 decimals, and Postgres rounds a half away from zero.
    assert row.cost_usd == half.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


# --- A batch that runs, a question without an answer, a failure -----------------------------------


async def test_a_batch_that_runs_is_looked_at_again_later(services: Services) -> None:
    services = dataclasses.replace(services, batches=FakeBatches(polls=3))
    for note in NOTES:
        await _save(services.sessions, note)
    await _run_all(services)
    batch_id = await _send(services)
    async with services.sessions() as db:  # the first look is due now
        await db.execute(
            sa.update(Job).where(Job.kind == JobKind.COLLECT_BATCH).values(run_after=sa.func.now())
        )
        await db.commit()
    looked = datetime.now(UTC)

    await _run_due(services)

    job = await _collect_job(services.sessions)
    assert (job.state, job.attempts) == (JobState.PENDING, 1)
    assert job.run_after >= looked + batches.COLLECT_WAIT - timedelta(seconds=5)
    assert (await _batch(services.sessions, batch_id)).state == BatchState.SENT


async def test_a_card_without_an_answer_waits_for_the_next_batch(services: Services) -> None:
    items = [await _save(services.sessions, note) for note in NOTES[:2]]
    await _run_all(services)
    waiting = await batches.waiting_cards(services.sessions, _provider(services).model)
    failing = waiting.items[1][1]  # the question of the second item
    services = dataclasses.replace(
        services, batches=FakeBatches(errors={failing: "errored: overloaded_error"})
    )
    batch_id = await batches.send_batch(services.sessions, _provider(services), waiting)

    await _run_all(services)

    assert await _states(services.sessions, items) == [EnrichState.READY, EnrichState.FETCHED]
    batch = await _batch(services.sessions, batch_id)
    assert (batch.answered, batch.not_answered) == (1, 1)
    async with services.sessions() as db:
        outcome = await db.scalar(
            sa.select(LLMBatchItem.outcome).where(LLMBatchItem.item_id == items[1])
        )
    assert outcome == "errored: overloaded_error"
    again = await batches.waiting_cards(services.sessions, _provider(services).model)
    assert [question.custom_id for question in again.questions] == [failing]


class Refusing(FakeBatches):
    async def send(self, questions: object, schema: object) -> str:
        raise ProviderError("400 invalid_request_error", retryable=False)


async def test_a_batch_that_the_provider_refuses_is_kept_as_failed(services: Services) -> None:
    for note in NOTES[:2]:
        await _save(services.sessions, note)
    await _run_all(services)
    waiting = await batches.waiting_cards(services.sessions, "fake-llm")

    with pytest.raises(ProviderError):
        await batches.send_batch(services.sessions, Refusing(), waiting)

    async with services.sessions() as db:
        [batch] = (await db.scalars(sa.select(LLMBatch))).all()
    assert (batch.state, batch.error) == (BatchState.FAILED, "400 invalid_request_error")
    again = await batches.waiting_cards(services.sessions, "fake-llm")
    assert len(again.questions) == 2


async def test_a_batch_that_does_not_end_in_25_hours_fails(services: Services) -> None:
    services = dataclasses.replace(services, batches=FakeBatches(polls=10))
    for note in NOTES[:2]:
        await _save(services.sessions, note)
    await _run_all(services)
    batch_id = await _send(services)
    async with services.sessions() as db:  # the last look
        await db.execute(
            sa.update(Job)
            .where(Job.kind == JobKind.COLLECT_BATCH)
            .values(run_after=sa.func.now(), max_attempts=1)
        )
        await db.commit()

    await _run_due(services)

    batch = await _batch(services.sessions, batch_id)
    assert (batch.state, batch.error) == (BatchState.FAILED, "the batch did not end in 25 hours")
    again = await batches.waiting_cards(services.sessions, _provider(services).model)
    assert len(again.questions) == 2


async def test_a_question_of_a_batch_is_the_question_of_the_card_step(services: Services) -> None:
    # The key of a question in a batch is the key under which the card step looks.
    await _save(services.sessions, NOTES[0])
    await _run_all(services)

    waiting = await batches.waiting_cards(services.sessions, _provider(services).model)

    [question] = waiting.questions
    assert isinstance(question, BatchQuestion)
    assert isinstance(services.llm, CachedLLM)
    async with services.sessions() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(LLMCall)) == 0
    # The card step raised WaitsForBatch with this key; the batch asks with the same.
    assert question.custom_id == waiting.items[0][1]
