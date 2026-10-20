"""The cache and the cost ledger in front of a model provider: the table ``llm_calls``.

Each call is kept with its cache key, its tokens and its price. The same question is never
paid two times: a job that runs again gets the stored answer. A question is the model, the
task, the prompt version, the system prompt, the parts and the schema; a different image
is a different question, but the ledger keeps only its hash. A question with citations is the
model, the task, the prompt version, the system prompt, the sources and the question.

The same table gives the cost of each day, so a daily budget can stop new calls before
they cost more. A stored answer needs no budget, and neither does the answer of a batch: you
accepted its price by hand (``pipeline.batches``).

In batch mode (``UNSHELVE_CARDS=batch``) a question of a batch task is never asked now: without
a stored answer it raises ``WaitsForBatch``, and a batch answers it later at half the price.
"""

import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa
import structlog
from pydantic import BaseModel
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.dto import (
    CitedAnswer,
    ImagePart,
    LLMResult,
    LLMUsage,
    SourceText,
    TextPart,
)
from unshelve.contracts.errors import ProviderError, WaitsForBatch
from unshelve.contracts.protocols import ModelProvider
from unshelve.db.models import LLMCall

log = structlog.get_logger()

_MILLION = Decimal(1_000_000)
# Dollars for one million tokens: input, output, input read from the cache. From the
# documentation of the Claude API (prices of October 2026).
PRICES: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "claude-opus-5-5": (Decimal("4.00"), Decimal("20.00"), Decimal("0.20")),
    "claude-sonnet-5-5": (Decimal("2.00"), Decimal("10.00"), Decimal("0.20")),
    "claude-haiku-5-5": (Decimal("0.10"), Decimal("0.50"), Decimal("0.01")),
    "fake-llm": (Decimal(0), Decimal(0), Decimal(0)),
}
# Claude Haiku 5.5 costs more for a prompt of more than 100,000 tokens.
_HAIKU_LONG_PROMPT = 100_000
_HAIKU_LONG_PRICES = (Decimal("0.50"), Decimal("2.50"), Decimal("0.05"))


def cost_usd(model: str, usage: LLMUsage, *, batch: bool = False) -> Decimal | None:
    """The price of one call in dollars, or None for a model without a known price.

    ``batch``: the answer came in a batch, which costs half of each price.
    """
    prices = PRICES.get(model)
    if prices is None:
        return None
    if model == "claude-haiku-5-5" and usage.input_tokens > _HAIKU_LONG_PROMPT:
        prices = _HAIKU_LONG_PRICES
    per_input, per_output, per_cache_read = prices
    total = (
        usage.input_tokens * per_input
        + usage.output_tokens * per_output
        + usage.cache_read_tokens * per_cache_read
    )
    if batch:
        total /= 2
    return (total / _MILLION).normalize() if total else Decimal(0)


def _part(part: TextPart | ImagePart) -> dict[str, str]:
    if isinstance(part, TextPart):
        return {"text": part.text}
    return {"image_sha256": hashlib.sha256(part.data).hexdigest(), "mime": part.mime}


def cache_key(
    model: str,
    task: str,
    prompt_version: str,
    system: str,
    parts: Sequence[TextPart | ImagePart],
    schema: type[BaseModel],
) -> str:
    question = {
        "model": model,
        "task": task,
        "prompt_version": prompt_version,
        "system": system,
        "parts": [_part(part) for part in parts],
        "schema": schema.model_json_schema(),
    }
    encoded = json.dumps(question, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def cited_key(
    model: str,
    task: str,
    prompt_version: str,
    system: str,
    sources: Sequence[SourceText],
    question: str,
) -> str:
    asked = {
        "model": model,
        "task": task,
        "prompt_version": prompt_version,
        "system": system,
        "sources": [source.model_dump() for source in sources],
        "question": question,
        # A question with citations never meets a question with a schema under one key.
        "answer": "cited",
    }
    encoded = json.dumps(asked, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def cited_request_of(
    system: str, sources: Sequence[SourceText], question: str, max_output_tokens: int
) -> dict[str, Any]:
    """A question with citations as the ledger keeps it."""
    return {
        "system": system,
        "sources": [source.model_dump() for source in sources],
        "question": question,
        "schema": CitedAnswer.__name__,
        "max_output_tokens": max_output_tokens,
    }


def request_of(
    system: str,
    parts: Sequence[TextPart | ImagePart],
    schema: type[BaseModel],
    max_output_tokens: int,
) -> dict[str, Any]:
    """The question as the ledger keeps it: an image only by its hash."""
    return {
        "system": system,
        "parts": [_part(part) for part in parts],
        "schema": schema.__name__,
        "max_output_tokens": max_output_tokens,
    }


async def store_answer(
    session: AsyncSession,
    *,
    key: str,
    task: str,
    prompt_version: str,
    request: dict[str, Any],
    result: LLMResult[Any],
    cost: Decimal,
    duration_ms: int | None,
    batch_id: int | None = None,
) -> None:
    """Keep an answer under the key of its question. The caller commits."""
    await session.execute(
        postgresql.insert(LLMCall)
        .values(
            cache_key=key,
            task=task,
            model=result.model,
            prompt_version=prompt_version,
            request=request,
            response={"value": result.value.model_dump(mode="json")},
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            cost_usd=cost,
            duration_ms=duration_ms,
            batch_id=batch_id,
        )
        # Two workers can ask the same question at the same moment. One row is enough.
        .on_conflict_do_nothing(index_elements=[LLMCall.cache_key])
    )


class CachedLLM:
    """An ``LLMProvider`` that asks ``inner`` only for a question that it has not stored."""

    def __init__(
        self,
        inner: ModelProvider,
        sessions: async_sessionmaker[AsyncSession],
        *,
        model: str,
        budget_usd: Decimal | None = None,
        timezone: str = "UTC",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        batch_tasks: Collection[str] = (),
    ) -> None:
        """``batch_tasks``: the tasks whose questions wait for a batch (batch mode)."""
        self.inner = inner
        self._sessions = sessions
        self.model = model
        self.batch_tasks = frozenset(batch_tasks)
        self.budget_usd = budget_usd
        self._zone = ZoneInfo(timezone)
        self._clock = clock

    async def aclose(self) -> None:
        close: Any = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()

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
        return await self._answer(
            cache_key(self.model, task, prompt_version, system, parts, schema),
            task=task,
            prompt_version=prompt_version,
            schema=schema,
            request=request_of(system, parts, schema, max_output_tokens),
            ask=lambda: self.inner.generate(
                task=task,
                prompt_version=prompt_version,
                system=system,
                parts=parts,
                schema=schema,
                max_output_tokens=max_output_tokens,
            ),
        )

    async def cite(
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        sources: Sequence[SourceText],
        question: str,
        max_output_tokens: int,
    ) -> LLMResult[CitedAnswer]:
        return await self._answer(
            cited_key(self.model, task, prompt_version, system, sources, question),
            task=task,
            prompt_version=prompt_version,
            schema=CitedAnswer,
            request=cited_request_of(system, sources, question, max_output_tokens),
            ask=lambda: self.inner.cite(
                task=task,
                prompt_version=prompt_version,
                system=system,
                sources=sources,
                question=question,
                max_output_tokens=max_output_tokens,
            ),
        )

    async def _answer[T: BaseModel](
        self,
        key: str,
        *,
        task: str,
        prompt_version: str,
        schema: type[T],
        request: dict[str, Any],
        ask: Callable[[], Awaitable[LLMResult[T]]],
    ) -> LLMResult[T]:
        """The stored answer of ``key``, or a new answer of ``ask``, which is then stored."""
        async with self._sessions() as session:
            stored = await session.scalar(sa.select(LLMCall).where(LLMCall.cache_key == key))
            if stored is None and task in self.batch_tasks:
                raise WaitsForBatch(key)
            if stored is None:
                await self._check_budget(session)
        if stored is not None:
            return LLMResult[schema](
                value=schema.model_validate(stored.response["value"]),
                model=stored.model,
                usage=LLMUsage(
                    input_tokens=stored.input_tokens,
                    output_tokens=stored.output_tokens,
                    cache_read_tokens=stored.cache_read_tokens,
                ),
                cached=True,
            )

        started = time.monotonic()
        result = await ask()
        milliseconds = round((time.monotonic() - started) * 1000)
        cost = cost_usd(result.model, result.usage)
        if cost is None:
            log.warning("llm.no_price", model=result.model)
        async with self._sessions() as session:
            await store_answer(
                session,
                key=key,
                task=task,
                prompt_version=prompt_version,
                request=request,
                result=result,
                cost=cost or Decimal(0),
                duration_ms=milliseconds,
            )
            await session.commit()
        log.info(
            "llm.call",
            task=task,
            model=result.model,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cost_usd=str(cost),
            seconds=round(milliseconds / 1000, 2),
        )
        return result

    async def _check_budget(self, session: AsyncSession) -> None:
        if self.budget_usd is None:
            return
        now = self._clock().astimezone(self._zone)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = await session.scalar(
            sa.select(sa.func.coalesce(sa.func.sum(LLMCall.cost_usd), 0)).where(
                LLMCall.created_at >= today,
                # A batch was accepted by hand, with its price: it does not use the budget.
                LLMCall.batch_id.is_(None),
            )
        )
        if Decimal(spent or 0) >= self.budget_usd:
            # Midnight on the wall clock of the next day. The difference is taken in UTC:
            # Python ignores a change of the clocks between two times of one time zone.
            tomorrow = today + timedelta(days=1)
            wait = tomorrow.astimezone(UTC) - now.astimezone(UTC)
            raise ProviderError(
                f"the daily budget of ${self.budget_usd} is used",
                retryable=True,
                retry_after=wait.total_seconds(),
            )
