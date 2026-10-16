"""Batch mode for the cards (``UNSHELVE_CARDS=batch``): one batch for many cards, at half the price.

In batch mode the card step asks only the ledger. A card without a stored answer waits: its item
stays ``fetched``, and its job ends. ``unshelve llm batch send`` then sends the waiting
questions as one batch (``send_batch``). When the batch has ended, the job ``collect_batch``
keeps each answer in the ledger, under the key of its question, and starts the card step of
each item again. That step finds the answer, and asks no model.

A question that got no answer (an error, a refusal) leaves its item waiting for the next batch.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.dto import BatchQuestion, LLMUsage
from unshelve.contracts.enums import BatchState, EnrichState, JobKind
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.jobs import (
    CollectBatchPayload,
    EnrichItemPayload,
    JobContext,
    Permanent,
    Retry,
)
from unshelve.contracts.protocols import BatchProvider
from unshelve.db.models import Item, ItemContent, LLMBatch, LLMBatchItem, LLMCall
from unshelve.enrich.card import (
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    TASK,
    Card,
    card_parts,
)
from unshelve.providers.ledger import cache_key, cost_usd, request_of, store_answer
from unshelve.queue import store as queue
from unshelve.queue.registry import registry

log = structlog.get_logger()

# The worker looks at a batch this often. Most batches end within one hour, each within 24.
COLLECT_WAIT = timedelta(minutes=5)
# 300 looks, 5 minutes apart: 25 hours, more than the longest batch.
COLLECT_ATTEMPTS = 300
# A batch that a command stored, but never sent, holds its questions only this long.
SENDING_HOLD = timedelta(minutes=10)
# The estimate of one card, from the live checks of October: the system prompt, the schema and
# the request take about 900 input tokens; a text adds one token for each 2.5 characters
# (English about 3.5, Persian about 1.5); the answer, with a little thinking, about 400.
_FIXED_INPUT = 900
_CHARACTERS_PER_TOKEN = 2.5
_OUTPUT = 400


@dataclass(frozen=True, slots=True)
class Waiting:
    """The cards that wait for a batch, and what one batch of them would cost."""

    items: list[tuple[int, str, dict[str, Any]]]  # item, its question (custom_id), the request
    questions: list[BatchQuestion]  # one for each question: two items can share one
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: Decimal


async def waiting_cards(
    sessions: async_sessionmaker[AsyncSession], model: str, *, now: datetime | None = None
) -> Waiting:
    """The items in state ``fetched`` whose card question has no stored answer, and is in no
    batch that runs. ``model``: the model of the batch, the same as of the card step."""
    moment = now or datetime.now(UTC)
    async with sessions() as session:
        rows = (
            await session.execute(
                sa.select(Item.id, ItemContent.text)
                .join(ItemContent, ItemContent.item_id == Item.id)
                .where(Item.enrich_state == EnrichState.FETCHED)
                .order_by(Item.id)
            )
        ).all()
        asked: list[tuple[int, str, list[Any]]] = []
        for item_id, text in rows:
            if not text.strip():
                continue  # no question: the card step writes an empty card by itself
            parts = card_parts(text)
            asked.append(
                (item_id, cache_key(model, TASK, PROMPT_VERSION, SYSTEM_PROMPT, parts, Card), parts)
            )
        done = await _answered_or_open(session, {key for _, key, _ in asked}, moment)

    items: list[tuple[int, str, dict[str, Any]]] = []
    questions: dict[str, BatchQuestion] = {}
    input_tokens = 0
    for item_id, key, parts in asked:
        if key in done:
            continue
        items.append((item_id, key, request_of(SYSTEM_PROMPT, parts, Card, MAX_OUTPUT_TOKENS)))
        if key not in questions:
            questions[key] = BatchQuestion(
                custom_id=key,
                system=SYSTEM_PROMPT,
                parts=parts,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            input_tokens += _FIXED_INPUT + math.ceil(len(parts[0].text) / _CHARACTERS_PER_TOKEN)
    output_tokens = _OUTPUT * len(questions)
    usage = LLMUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    return Waiting(
        items=items,
        questions=list(questions.values()),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost_usd=cost_usd(model, usage, batch=True) or Decimal(0),
    )


async def _answered_or_open(session: AsyncSession, keys: set[str], now: datetime) -> set[str]:
    """The questions that have a stored answer, or that a batch asks now."""
    if not keys:
        return set()
    answered = await session.scalars(
        sa.select(LLMCall.cache_key).where(LLMCall.cache_key.in_(keys))
    )
    asked = await session.scalars(
        sa.select(LLMBatchItem.custom_id)
        .join(LLMBatch, LLMBatch.id == LLMBatchItem.batch_id)
        .where(
            LLMBatchItem.custom_id.in_(keys),
            sa.or_(
                LLMBatch.state == BatchState.SENT,
                # A command that stopped before the provider answered holds them a short time.
                sa.and_(
                    LLMBatch.state == BatchState.SENDING,
                    LLMBatch.created_at > now - SENDING_HOLD,
                ),
            ),
        )
    )
    return set(answered) | set(asked)


async def send_batch(
    sessions: async_sessionmaker[AsyncSession], provider: BatchProvider, waiting: Waiting
) -> int:
    """Keep the batch and its items, send it, and add the job that collects it. Returns the id
    of the batch. A failure of the provider goes up, and the batch is kept as failed."""
    async with sessions() as session:
        batch = LLMBatch(
            model=provider.model,
            task=TASK,
            prompt_version=PROMPT_VERSION,
            questions=len(waiting.questions),
            estimated_cost_usd=waiting.estimated_cost_usd,
        )
        session.add(batch)
        await session.flush()
        batch_id = batch.id
        session.add_all(
            LLMBatchItem(batch_id=batch_id, item_id=item_id, custom_id=key, request=request)
            for item_id, key, request in waiting.items
        )
        await session.commit()
    try:
        provider_id = await provider.send(waiting.questions, Card)
    except ProviderError as error:
        async with sessions() as session:
            await session.execute(
                sa.update(LLMBatch)
                .where(LLMBatch.id == batch_id)
                .values(state=BatchState.FAILED, error=str(error)[:300])
            )
            await session.commit()
        raise
    async with sessions() as session:
        await session.execute(
            sa.update(LLMBatch)
            .where(LLMBatch.id == batch_id)
            .values(state=BatchState.SENT, provider_batch_id=provider_id)
        )
        await queue.enqueue(
            session,
            JobKind.COLLECT_BATCH,
            CollectBatchPayload(batch_id=batch_id),
            dedupe_key=f"batch:{batch_id}",
            delay=COLLECT_WAIT,
            max_attempts=COLLECT_ATTEMPTS,
        )
        await session.commit()
    log.info("batch.sent", batch_id=batch_id, questions=len(waiting.questions))
    return batch_id


@registry.handler(JobKind.COLLECT_BATCH)
async def collect_batch(ctx: JobContext, payload: CollectBatchPayload) -> None:
    """Keep the answers of an ended batch in the ledger, and start the card step of its items
    again: each one finds its answer there. A batch that runs is looked at again later."""
    services = ctx.services
    if services.batches is None:
        raise Permanent("no batch provider")
    async with services.sessions() as session:
        batch = await session.get(LLMBatch, payload.batch_id)
    if batch is None:
        raise Permanent(f"batch {payload.batch_id} does not exist")
    if batch.state is not BatchState.SENT or batch.provider_batch_id is None:
        return  # collected already, or never sent
    last_try = ctx.job.attempts >= ctx.job.max_attempts
    try:
        ended = await services.batches.ended(batch.provider_batch_id)
        if not ended and not last_try:
            raise Retry("the batch runs", after=COLLECT_WAIT)
        if not ended:
            await _give_up(services.sessions, batch.id, "the batch did not end in 25 hours")
            return
        await _collect(services.sessions, services.batches, batch, batch.provider_batch_id)
    except ProviderError as error:
        if error.retryable and not last_try:
            raise Retry(f"batch: {error}", after=COLLECT_WAIT) from error
        await _give_up(services.sessions, batch.id, str(error)[:300])


async def _collect(
    sessions: async_sessionmaker[AsyncSession],
    provider: BatchProvider,
    batch: LLMBatch,
    provider_id: str,
) -> None:
    """Keep each answer, write what each item got, and start the card step of the items."""
    async with sessions() as session:
        items = (
            await session.scalars(sa.select(LLMBatchItem).where(LLMBatchItem.batch_id == batch.id))
        ).all()
        requests = {item.custom_id: item.request for item in items}
        outcomes: dict[str, str] = {}
        cost = Decimal(0)
        async for answer in provider.answers(provider_id, Card):
            if answer.custom_id not in requests:
                continue
            if answer.result is None:
                outcomes[answer.custom_id] = answer.error or "no answer"
                continue
            price = cost_usd(answer.result.model, answer.result.usage, batch=True) or Decimal(0)
            await store_answer(
                session,
                key=answer.custom_id,
                task=batch.task,
                prompt_version=batch.prompt_version,
                request=requests[answer.custom_id],
                result=answer.result,
                cost=price,
                duration_ms=None,
                batch_id=batch.id,
            )
            outcomes[answer.custom_id] = "answered"
            cost += price
        for item in items:
            item.outcome = outcomes.get(item.custom_id, "no answer")
        answered = sum(outcome == "answered" for outcome in outcomes.values())
        await session.execute(
            sa.update(LLMBatch)
            .where(LLMBatch.id == batch.id)
            .values(
                state=BatchState.COLLECTED,
                answered=answered,
                not_answered=len(requests) - answered,
                cost_usd=cost,
                collected_at=sa.func.now(),
            )
        )
        rows = await session.execute(
            sa.select(Item.id, Item.owner_id).where(Item.id.in_([i.item_id for i in items]))
        )
        # Not dict(rows): a result has keys(), the names of its columns, so dict() would read it
        # as a mapping, and fail.
        owners = {row.id: row.owner_id for row in rows}
        for item_id, owner_id in sorted(owners.items()):
            # The card step finds the answer now. An item without an answer waits again.
            await queue.enqueue(
                session,
                JobKind.ENRICH_ITEM,
                EnrichItemPayload(item_id=item_id),
                owner_id=owner_id,
                dedupe_key=f"item:{item_id}",
            )
        await session.commit()
    log.info("batch.collected", batch_id=batch.id, answered=answered, cost_usd=str(cost))


async def _give_up(sessions: async_sessionmaker[AsyncSession], batch_id: int, why: str) -> None:
    """The batch failed: its items wait for the next batch."""
    async with sessions() as session:
        await session.execute(
            sa.update(LLMBatch)
            .where(LLMBatch.id == batch_id)
            .values(state=BatchState.FAILED, error=why)
        )
        await session.commit()
    log.warning("batch.failed", batch_id=batch_id, why=why)
