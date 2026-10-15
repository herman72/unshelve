"""The cache and the cost ledger in front of a model: the table llm_calls."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.dto import ImagePart, LLMResult, LLMUsage, TextPart
from unshelve.contracts.errors import ProviderError
from unshelve.db.models import LLMCall
from unshelve.providers.ledger import CachedLLM, cost_usd
from unshelve.testing.conformance import TINY_PNG
from unshelve.testing.fakes import synthesize

Sessions = async_sessionmaker[AsyncSession]
# 23:00 in Helsinki on 15 October 2026 (summer time, UTC+3).
LATE_EVENING = datetime(2026, 10, 15, 20, 0, tzinfo=UTC)


class Note(BaseModel):
    title: str
    tags: list[str]


@dataclass
class Counting:
    """A provider that counts its calls. Each call costs $6 with the prices of Opus 5.5."""

    model: str = "claude-opus-5-5"
    calls: int = 0
    fail_with: ProviderError | None = None
    asked: list[str] = field(default_factory=list[str])

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
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        text = " ".join(part.text for part in parts if isinstance(part, TextPart))
        self.asked.append(text)
        return LLMResult[schema](
            value=synthesize(schema, text),
            model=self.model,
            usage=LLMUsage(input_tokens=1_000_000, output_tokens=100_000),
        )


def _ledger(sessions: Sessions, inner: Counting, **options: object) -> CachedLLM:
    settings: dict[str, object] = {"model": inner.model, "clock": lambda: LATE_EVENING}
    return CachedLLM(inner, sessions, **(settings | options))  # type: ignore[arg-type]


async def _ask(
    llm: CachedLLM, text: str = "A note about queues.", *, version: str = "v1"
) -> LLMResult[Note]:
    return await llm.generate(
        task="test",
        prompt_version=version,
        system="Describe the text.",
        parts=[TextPart(text=text)],
        schema=Note,
        max_output_tokens=500,
    )


async def _rows(sessions: Sessions) -> list[LLMCall]:
    async with sessions() as db:
        return list((await db.scalars(sa.select(LLMCall).order_by(LLMCall.id))).all())


async def test_the_same_question_is_paid_one_time(committed_db: Sessions) -> None:
    inner = Counting()
    llm = _ledger(committed_db, inner)

    first = await _ask(llm)
    second = await _ask(llm)

    assert inner.calls == 1
    assert (first.cached, second.cached) == (False, True)
    assert second.value == first.value
    (row,) = await _rows(committed_db)
    assert (row.task, row.model, row.prompt_version) == ("test", "claude-opus-5-5", "v1")
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (
        1_000_000,
        100_000,
        Decimal("6.000000"),
    )
    assert row.request["parts"] == [{"text": "A note about queues."}]
    assert row.response["value"] == first.value.model_dump(mode="json")


async def test_a_new_text_a_new_prompt_or_a_new_model_is_a_new_question(
    committed_db: Sessions,
) -> None:
    inner = Counting()

    await _ask(_ledger(committed_db, inner))
    await _ask(_ledger(committed_db, inner), "A different note.")
    await _ask(_ledger(committed_db, inner), version="v2")
    await _ask(_ledger(committed_db, inner, model="claude-haiku-5-5"))

    assert inner.calls == 4


async def test_an_image_is_kept_as_its_hash(committed_db: Sessions) -> None:
    inner = Counting()
    llm = _ledger(committed_db, inner)
    other_png = TINY_PNG + b"\x00"

    for image in (TINY_PNG, other_png):
        await llm.generate(
            task="test",
            prompt_version="v1",
            system="Describe the image.",
            parts=[TextPart(text="A screenshot."), ImagePart(data=image, mime="image/png")],
            schema=Note,
            max_output_tokens=500,
        )

    assert inner.calls == 2  # a different image is a different question
    image_part = (await _rows(committed_db))[0].request["parts"][1]
    assert set(image_part) == {"image_sha256", "mime"}  # no bytes in the ledger


async def test_a_failed_call_is_not_kept(committed_db: Sessions) -> None:
    inner = Counting(fail_with=ProviderError("529 overloaded_error", retryable=True))
    llm = _ledger(committed_db, inner)

    for _ in range(2):
        with pytest.raises(ProviderError):
            await _ask(llm)

    assert inner.calls == 2  # the second try asks again
    assert await _rows(committed_db) == []


# --- The daily budget ------------------------------------------------------------------------


async def _spent(sessions: Sessions, cost: str, at: datetime) -> None:
    async with sessions() as db:
        db.add(
            LLMCall(
                cache_key=f"old-{at.isoformat()}-{cost}",
                task="test",
                model="claude-opus-5-5",
                prompt_version="v1",
                request={},
                response={},
                cost_usd=Decimal(cost),
                created_at=at,
            )
        )
        await db.commit()


async def test_a_used_budget_stops_new_calls_until_the_next_day(committed_db: Sessions) -> None:
    await _spent(committed_db, "1.00", LATE_EVENING - timedelta(hours=1))
    inner = Counting()
    llm = _ledger(committed_db, inner, budget_usd=Decimal("1.00"), timezone="Europe/Helsinki")

    with pytest.raises(ProviderError) as caught:
        await _ask(llm)

    assert inner.calls == 0
    assert caught.value.retryable is True
    # The day ends at midnight in Helsinki: one hour after 23:00.
    assert caught.value.retry_after == 3600.0


async def test_the_costs_of_yesterday_do_not_count(committed_db: Sessions) -> None:
    # 23:00 UTC on 14 October is 02:00 on 15 October in Helsinki: today. 20:00 UTC is not.
    await _spent(committed_db, "5.00", datetime(2026, 10, 14, 20, 0, tzinfo=UTC))
    await _spent(committed_db, "0.40", datetime(2026, 10, 14, 23, 0, tzinfo=UTC))
    inner = Counting()
    llm = _ledger(committed_db, inner, budget_usd=Decimal("1.00"), timezone="Europe/Helsinki")

    await _ask(llm)

    assert inner.calls == 1


async def test_the_day_of_the_budget_is_the_day_in_your_time_zone(
    committed_db: Sessions,
) -> None:
    # 22:30 UTC on 14 October is 01:30 on 15 October in Helsinki: the same day as the call.
    await _spent(committed_db, "1.00", datetime(2026, 10, 14, 22, 30, tzinfo=UTC))
    inner = Counting()
    llm = _ledger(committed_db, inner, budget_usd=Decimal("1.00"), timezone="Europe/Helsinki")

    with pytest.raises(ProviderError):
        await _ask(llm)

    assert inner.calls == 0


async def test_the_wait_is_right_on_the_night_when_the_clocks_go_back(
    committed_db: Sessions,
) -> None:
    # 00:30 on 25 October 2026 in Helsinki. At 04:00 the clocks go back to 03:00, so this day
    # has 25 hours, and the next midnight is 24.5 hours away, not 23.5.
    night = datetime(2026, 10, 24, 21, 30, tzinfo=UTC)
    await _spent(committed_db, "1.00", night)
    llm = _ledger(
        committed_db,
        Counting(),
        budget_usd=Decimal("1.00"),
        timezone="Europe/Helsinki",
        clock=lambda: night,
    )

    with pytest.raises(ProviderError) as caught:
        await _ask(llm)

    assert caught.value.retry_after == 24.5 * 3600


async def test_a_stored_answer_needs_no_budget(committed_db: Sessions) -> None:
    inner = Counting()
    await _ask(_ledger(committed_db, inner))  # costs $6, more than the budget
    llm = _ledger(committed_db, inner, budget_usd=Decimal("1.00"))

    assert (await _ask(llm)).cached is True
    assert inner.calls == 1


# --- Prices ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "usage", "dollars"),
    [
        ("claude-opus-5-5", LLMUsage(input_tokens=1_000_000, output_tokens=100_000), "6.00"),
        ("claude-sonnet-5-5", LLMUsage(input_tokens=1_000_000, output_tokens=100_000), "3.00"),
        ("claude-haiku-5-5", LLMUsage(input_tokens=100_000, output_tokens=100_000), "0.06"),
        # Claude Haiku 5.5 costs more for a prompt of more than 100,000 tokens.
        ("claude-haiku-5-5", LLMUsage(input_tokens=200_000, output_tokens=100_000), "0.35"),
        ("claude-opus-5-5", LLMUsage(cache_read_tokens=1_000_000), "0.20"),
        ("fake-llm", LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000), "0"),
    ],
)
def test_the_cost_comes_from_the_price_of_the_model(
    model: str, usage: LLMUsage, dollars: str
) -> None:
    assert cost_usd(model, usage) == Decimal(dollars)


def test_a_model_without_a_price_has_no_cost() -> None:
    assert cost_usd("claude-unknown-9", LLMUsage(input_tokens=10)) is None
