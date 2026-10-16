"""Batches of questions to Claude, against a pretend Message Batches API. No test reaches the
real API."""

import json
from typing import Any

import pytest
from pydantic import BaseModel

from tests.providers.support import (
    BATCH_ID,
    BatchLine,
    PretendAPI,
    PretendBatchAPI,
    answered,
    error,
    message_json,
)
from unshelve.contracts.dto import BatchAnswer, BatchQuestion, TextPart
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.protocols import BatchProvider
from unshelve.providers.batches import ClaudeBatches
from unshelve.providers.claude import FALLBACK_BETA, ClaudeLLM
from unshelve.testing.conformance import BatchProviderConformance
from unshelve.testing.fakes import FakeBatches


class Note(BaseModel):
    title: str
    tags: list[str]


QUESTION = BatchQuestion(
    custom_id="a" * 64,
    system="Describe the text.",
    parts=[TextPart(text="A note about queues.")],
    max_output_tokens=500,
)


class TestClaudeBatches(BatchProviderConformance):
    @pytest.fixture
    def batches(self) -> BatchProvider:
        return ClaudeBatches(model="claude-haiku-5-5", client=PretendBatchAPI().client())


class TestFakeBatches(BatchProviderConformance):
    @pytest.fixture
    def batches(self) -> BatchProvider:
        return FakeBatches()


async def _answers(
    api: PretendBatchAPI, model: str = "claude-haiku-5-5"
) -> list[BatchAnswer[Note]]:
    batches = ClaudeBatches(model=model, client=api.client())
    await batches.send([QUESTION], Note)
    return [answer async for answer in batches.answers(BATCH_ID, Note)]


# --- What goes out ----------------------------------------------------------------------------


async def test_a_question_of_a_batch_is_the_request_of_a_call_now() -> None:
    # The same question, now and in a batch: the answer is stored under one cache key, so the
    # two requests must ask exactly the same.
    now = PretendAPI()
    await ClaudeLLM(model="claude-haiku-5-5", client=now.client()).generate(
        task="test",
        prompt_version="1",
        system=QUESTION.system,
        parts=QUESTION.parts,
        schema=Note,
        max_output_tokens=QUESTION.max_output_tokens,
    )
    later = PretendBatchAPI()

    await ClaudeBatches(model="claude-haiku-5-5", client=later.client()).send([QUESTION], Note)

    [request] = later.created[0]["requests"]
    assert request["custom_id"] == QUESTION.custom_id
    assert request["params"] == now.bodies[0]


async def test_each_question_is_one_request_with_its_id() -> None:
    api = PretendBatchAPI()
    other = QUESTION.model_copy(update={"custom_id": "b" * 64})

    batch_id = await ClaudeBatches(model="claude-haiku-5-5", client=api.client()).send(
        [QUESTION, other], Note
    )

    assert batch_id == BATCH_ID
    assert [request["custom_id"] for request in api.created[0]["requests"]] == ["a" * 64, "b" * 64]
    assert "message-batches-2024-09-24" in api.headers[0]["anthropic-beta"]


async def test_a_model_with_a_fallback_asks_for_it_in_each_request() -> None:
    api = PretendBatchAPI()

    await ClaudeBatches(model="claude-opus-5-5", client=api.client()).send([QUESTION], Note)

    [request] = api.created[0]["requests"]
    assert request["params"]["fallbacks"] == "default"
    assert FALLBACK_BETA in api.headers[0]["anthropic-beta"]


# --- What comes back --------------------------------------------------------------------------


async def test_a_batch_that_runs_has_not_ended() -> None:
    api = PretendBatchAPI(ended=False)
    batches = ClaudeBatches(model="claude-haiku-5-5", client=api.client())
    await batches.send([QUESTION], Note)

    assert await batches.ended(BATCH_ID) is False
    api.ended = True
    assert await batches.ended(BATCH_ID) is True


async def test_an_answer_fits_the_schema_and_has_its_usage() -> None:
    [answer] = await _answers(PretendBatchAPI())

    assert answer.custom_id == QUESTION.custom_id
    assert answer.result is not None
    assert answer.result.value == Note(title="a value", tags=["a point"])
    assert answer.result.model == "claude-haiku-5-5"
    assert (answer.result.usage.input_tokens, answer.result.usage.output_tokens) == (1200, 150)


def _line(result: dict[str, Any]) -> Any:
    def lines(requests: list[dict[str, Any]]) -> list[BatchLine]:
        return [{"custom_id": request["custom_id"], "result": result} for request in requests]

    return lines


@pytest.mark.parametrize(
    ("result", "why"),
    [
        (
            {
                "type": "errored",
                "error": {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "busy"},
                },
            },
            "errored: overloaded_error",
        ),
        ({"type": "canceled"}, "canceled"),
        ({"type": "expired"}, "expired"),
        (
            {
                "type": "succeeded",
                "message": message_json('{"title": "no tags"}', model="claude-haiku-5-5"),
            },
            "the answer does not fit Note",
        ),
    ],
)
async def test_a_question_without_an_answer_says_why(result: dict[str, Any], why: str) -> None:
    [answer] = await _answers(PretendBatchAPI(_line(result)))

    assert (answer.result, answer.error) == (None, why)


async def test_a_refusal_is_no_answer() -> None:
    refused = message_json("", model="claude-haiku-5-5", stop_reason="refusal")
    refused["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "no"}

    [answer] = await _answers(PretendBatchAPI(_line({"type": "succeeded", "message": refused})))

    assert answer.error == "the model declined (cyber)"


async def test_the_answers_come_in_any_order_and_are_named_by_their_id() -> None:
    def backwards(requests: list[dict[str, Any]]) -> list[BatchLine]:
        return [answered(request) for request in reversed(requests)]

    api = PretendBatchAPI(backwards)
    batches = ClaudeBatches(model="claude-haiku-5-5", client=api.client())
    other = QUESTION.model_copy(update={"custom_id": "b" * 64})
    await batches.send([QUESTION, other], Note)

    names = [answer.custom_id async for answer in batches.answers(BATCH_ID, Note)]

    assert names == ["b" * 64, "a" * 64]


# --- Failures of the API ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [
        (429, "rate_limit_error", True),
        (529, "overloaded_error", True),
        (401, "authentication_error", True),  # a person must fix the key; the item waits
        (400, "invalid_request_error", False),
    ],
)
async def test_a_failure_of_the_api_says_if_a_second_try_can_help(
    status: int, kind: str, retryable: bool
) -> None:
    api = PretendBatchAPI(fail=error(status, kind))

    with pytest.raises(ProviderError) as caught:
        await ClaudeBatches(model="claude-haiku-5-5", client=api.client()).send([QUESTION], Note)

    assert caught.value.retryable is retryable


def test_the_custom_id_of_a_question_is_a_name_that_the_api_takes() -> None:
    with pytest.raises(ValueError, match="custom_id"):
        BatchQuestion(custom_id="a key with spaces", system="s", parts=[], max_output_tokens=1)
    assert json.dumps(QUESTION.custom_id)  # 64 hex digits: a cache key fits
