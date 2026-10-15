"""The Claude adapter, against a pretend Anthropic API. No test reaches the real API."""

import base64
import json
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from tests.providers.support import PretendAPI, error, message, refusal
from unshelve.contracts.dto import ImagePart, TextPart
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.protocols import LLMProvider
from unshelve.providers.claude import THINKING_ROOM, ClaudeLLM
from unshelve.testing.conformance import TINY_PNG, LLMProviderConformance


class Note(BaseModel):
    title: str
    tags: list[str]


async def _ask(llm: ClaudeLLM, *parts: TextPart | ImagePart) -> Note:
    result = await llm.generate(
        task="test",
        prompt_version="1",
        system="Describe the text.",
        parts=list(parts) or [TextPart(text="A note about queues.")],
        schema=Note,
        max_output_tokens=500,
    )
    return result.value


class TestClaudeLLM(LLMProviderConformance):
    @pytest.fixture
    def llm(self) -> LLMProvider:
        return ClaudeLLM(model="claude-opus-5-5", client=PretendAPI().client())


# --- What goes out ----------------------------------------------------------------------------


async def test_the_request_asks_for_the_schema_with_little_thinking() -> None:
    api = PretendAPI()

    await _ask(ClaudeLLM(model="claude-opus-5-5", client=api.client()))

    (body,) = api.bodies
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == "Describe the text."
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "A note about queues."}]}
    ]
    # The answer must fit the schema. The model thinks a little, and the thinking needs room.
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["format"]["schema"]["required"] == ["title", "tags"]
    assert body["output_config"]["effort"] == "low"
    assert body["max_tokens"] == 500 + THINKING_ROOM
    # No tools: a page that asks the model to do something has nothing to do it with.
    assert "tools" not in body


async def test_an_image_is_sent_as_base64() -> None:
    api = PretendAPI()

    await _ask(
        ClaudeLLM(model="claude-opus-5-5", client=api.client()),
        TextPart(text="A screenshot."),
        ImagePart(data=TINY_PNG, mime="image/png"),
    )

    image = api.bodies[0]["messages"][0]["content"][1]
    assert image == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(TINY_PNG).decode(),
        },
    }


@pytest.mark.parametrize(
    ("model", "fallback"),
    [("claude-opus-5-5", True), ("claude-sonnet-5-5", True), ("claude-haiku-5-5", False)],
)
async def test_a_refused_request_can_go_to_a_second_model(model: str, fallback: bool) -> None:
    # With "fallbacks", the API itself runs a refused request again on a different model.
    # Claude Haiku 5.5 has no such fallback.
    api = PretendAPI()

    await _ask(ClaudeLLM(model=model, client=api.client()))

    assert (api.bodies[0].get("fallbacks") == "default") is fallback
    beta = api.headers[0].get("anthropic-beta", "")
    assert ("server-side-fallback-2026-07-01" in beta) is fallback


# --- What comes back -------------------------------------------------------------------------


async def test_the_model_that_answered_and_the_tokens_come_back() -> None:
    usage = {
        "input_tokens": 1000,
        "output_tokens": 150,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 200,
    }
    answer = json.dumps({"title": "Queues", "tags": ["postgres"]})
    # A refusal of Claude Opus 5.5 can be answered by the fallback model.
    api = PretendAPI(lambda body: message(answer, model="claude-opus-4-8", usage=usage))

    result = await ClaudeLLM(model="claude-opus-5-5", client=api.client()).generate(
        task="test",
        prompt_version="1",
        system="Describe the text.",
        parts=[TextPart(text="A note.")],
        schema=Note,
        max_output_tokens=500,
    )

    assert result.value == Note(title="Queues", tags=["postgres"])
    assert result.model == "claude-opus-4-8"
    # Tokens written to the cache are input tokens, too.
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1200, 150)
    assert result.usage.cache_read_tokens == 300


# --- What goes wrong --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "retryable", "retry_after"),
    [
        (error(429, "rate_limit_error", {"retry-after": "30"}), True, 30.0),
        (error(529, "overloaded_error"), True, None),
        (error(500, "api_error"), True, None),
        (error(503, "api_error"), True, None),
        # A person must fix these, so the item waits: the key, the account, the model name.
        (error(401, "authentication_error"), True, None),
        (error(402, "billing_error"), True, None),
        (error(403, "permission_error"), True, None),
        (error(404, "not_found_error"), True, None),
        # This request cannot work, and a second try sends the same request.
        (error(400, "invalid_request_error"), False, None),
        (error(413, "request_too_large"), False, None),
    ],
    ids=["429", "529", "500", "503", "401", "402", "403", "404", "400", "413"],
)
async def test_an_error_of_the_api_says_if_a_second_try_can_help(
    answer: httpx2.Response, retryable: bool, retry_after: float | None
) -> None:
    llm = ClaudeLLM(model="claude-opus-5-5", client=PretendAPI(lambda body: answer).client())

    with pytest.raises(ProviderError) as caught:
        await _ask(llm)

    assert (caught.value.retryable, caught.value.retry_after) == (retryable, retry_after)


async def test_an_image_type_that_the_model_cannot_read_gives_no_card() -> None:
    llm = ClaudeLLM(model="claude-opus-5-5", client=PretendAPI().client())

    with pytest.raises(ProviderError, match="image/heic") as caught:
        await _ask(llm, ImagePart(data=b"heic", mime="image/heic"))

    assert caught.value.retryable is False


async def test_a_network_error_can_pass() -> None:
    def no_network(body: dict[str, Any]) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    with pytest.raises(ProviderError) as caught:
        await _ask(ClaudeLLM(model="claude-opus-5-5", client=PretendAPI(no_network).client()))

    assert caught.value.retryable is True


async def test_a_refusal_gives_no_card() -> None:
    llm = ClaudeLLM(
        model="claude-opus-5-5", client=PretendAPI(lambda body: refusal("cyber")).client()
    )

    with pytest.raises(ProviderError, match="cyber") as caught:
        await _ask(llm)

    assert caught.value.retryable is False


@pytest.mark.parametrize(
    ("text", "stop_reason"),
    [
        ("this is not json", "end_turn"),
        (json.dumps({"title": "Only a title"}), "end_turn"),
        ('{"title": "Cut in the mid', "max_tokens"),
    ],
    ids=["no json", "a field is missing", "cut at max_tokens"],
)
async def test_an_answer_that_does_not_fit_gives_no_card(text: str, stop_reason: str) -> None:
    api = PretendAPI(lambda body: message(text, stop_reason=stop_reason))

    with pytest.raises(ProviderError) as caught:
        await _ask(ClaudeLLM(model="claude-opus-5-5", client=api.client()))

    assert caught.value.retryable is False
