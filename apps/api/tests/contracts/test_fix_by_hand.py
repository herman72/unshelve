"""The mark of an error that a person must fix: ``contracts.errors.FIX_BY_HAND``.

A watcher finds these errors with one search in ``jobs.last_error``. So each provider error
that a person must fix holds the mark, and no other provider error holds it. Each error that
a person must fix can be tried again, and so can each error that can pass. So ``retryable``
alone cannot tell these two groups apart.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Any, Literal

import httpx2
import pytest
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.providers.support import BATCH_ID, PretendAPI, PretendBatchAPI, error
from unshelve.contracts.dto import BatchQuestion, TextPart
from unshelve.contracts.errors import FIX_BY_HAND, ProviderError
from unshelve.providers.batches import ClaudeBatches
from unshelve.providers.claude import ClaudeLLM
from unshelve.providers.ledger import CachedLLM
from unshelve.testing.fakes import FakeLLM

Answer = Callable[[dict[str, Any]], httpx2.Response]
Method = Literal["send", "ended", "answers"]  # a call of the batch API


class Note(BaseModel):
    title: str


def test_the_mark_is_one_fixed_text() -> None:
    # The one place where a test writes the mark out, so a change of the constant fails here.
    # A watcher searches for this text. The old rows of a Claude 401, 403 or 404 hold it too.
    # The old rows of a Voyage 401, 403 or 404 and of a Claude 402 do not hold it.
    assert FIX_BY_HAND == "a person must fix it"


async def _ask(llm: ClaudeLLM | CachedLLM) -> None:
    await llm.generate(
        task="test",
        prompt_version="1",
        system="Name the text.",
        parts=[TextPart(text="A note about queues.")],
        schema=Note,
        max_output_tokens=100,
    )


async def _claude_error(answer: Answer) -> ProviderError:
    llm = ClaudeLLM(model="claude-opus-5-5", client=PretendAPI(answer).client())
    with pytest.raises(ProviderError) as caught:
        await _ask(llm)
    return caught.value


def _status(status: int, kind: str) -> Answer:
    """An answer of the API with an error of ``status``."""

    def answer(body: dict[str, Any]) -> httpx2.Response:
        return error(status, kind)

    return answer


def _no_network(body: dict[str, Any]) -> httpx2.Response:
    raise httpx2.ConnectError("refused")


# --- Claude ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, "authentication_error"),  # the key
        (402, "billing_error"),  # the credit of the account
        (403, "permission_error"),
        (404, "not_found_error"),  # the name of the model
    ],
)
async def test_a_claude_error_that_a_person_must_fix_holds_the_mark(status: int, kind: str) -> None:
    found = await _claude_error(_status(status, kind))

    assert found.retryable is True  # the item waits, and gets its card after the fix
    assert FIX_BY_HAND in str(found)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(_status(429, "rate_limit_error"), id="429"),
        pytest.param(_status(500, "api_error"), id="500"),
        pytest.param(_status(529, "overloaded_error"), id="529"),
        pytest.param(_no_network, id="no connection"),
    ],
)
async def test_a_claude_error_that_can_pass_has_no_mark(answer: Answer) -> None:
    found = await _claude_error(answer)

    assert found.retryable is True
    assert FIX_BY_HAND not in str(found)


@pytest.mark.parametrize(
    ("status", "kind"), [(400, "invalid_request_error"), (413, "request_too_large")]
)
async def test_a_claude_error_that_cannot_work_has_no_mark(status: int, kind: str) -> None:
    found = await _claude_error(_status(status, kind))

    assert found.retryable is False  # a second try of this request gives the same answer
    assert FIX_BY_HAND not in str(found)


async def test_a_402_says_that_the_account_has_no_credit() -> None:
    found = await _claude_error(_status(402, "billing_error"))

    assert str(found) == f"402 billing_error: {FIX_BY_HAND}: the account has no credit"


async def _call(batches: ClaudeBatches, method: Method) -> None:
    """One call of the batch API: it sends a batch, looks at a batch, or reads its answers."""
    match method:
        case "send":
            question = BatchQuestion(custom_id="a" * 64, system="s", parts=[], max_output_tokens=1)
            await batches.send([question], Note)
        case "ended":
            await batches.ended(BATCH_ID)
        case "answers":
            async for _ in batches.answers(BATCH_ID, Note):
                pass


@pytest.mark.parametrize("method", ["send", "ended", "answers"])
@pytest.mark.parametrize(
    ("status", "kind"), [(401, "authentication_error"), (402, "billing_error")]
)
async def test_an_error_of_the_batch_api_that_a_person_must_fix_holds_the_mark(
    status: int, kind: str, method: Method
) -> None:
    batches = ClaudeBatches(
        model="claude-haiku-5-5", client=PretendBatchAPI(fail=error(status, kind)).client()
    )

    with pytest.raises(ProviderError) as caught:
        await _call(batches, method)

    assert caught.value.retryable is True
    assert FIX_BY_HAND in str(caught.value)


# --- The ledger --------------------------------------------------------------------------


@pytest.mark.db
async def test_the_error_of_the_daily_budget_has_no_mark(
    committed_db: async_sessionmaker[AsyncSession],
) -> None:
    # A budget of $0 is used before the first call. The wait ends by itself at midnight: no
    # person needs to act.
    llm = CachedLLM(FakeLLM(), committed_db, model=FakeLLM.model, budget_usd=Decimal(0))

    with pytest.raises(ProviderError, match="budget") as caught:
        await _ask(llm)

    assert caught.value.retryable is True
    assert FIX_BY_HAND not in str(caught.value)
