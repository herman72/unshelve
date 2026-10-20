"""The Claude adapter, against a pretend Anthropic API. No test reaches the real API."""

import base64
import json
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

from tests.providers.support import (
    PretendAPI,
    cited_answer,
    error,
    message,
    message_json,
    refusal,
)
from unshelve.contracts.dto import CitedSegment, ImagePart, Quote, SourceText, TextPart
from unshelve.contracts.errors import ProviderError
from unshelve.contracts.protocols import CitingLLM, LLMProvider
from unshelve.providers.claude import THINKING_ROOM, ClaudeLLM
from unshelve.testing.conformance import TINY_PNG, CitingLLMConformance, LLMProviderConformance


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


class TestClaudeLLM(LLMProviderConformance, CitingLLMConformance):
    @pytest.fixture
    def llm(self) -> LLMProvider:
        return ClaudeLLM(model="claude-opus-5-5", client=PretendAPI().client())

    @pytest.fixture
    def citing_llm(self) -> CitingLLM:
        return ClaudeLLM(model="claude-opus-5-5", client=PretendAPI(cited_answer).client())


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


# --- An answer with citations -----------------------------------------------------------------

SOURCES = [
    SourceText(
        title="Postgres as a job queue",
        context="Item 1, piece 1 of 2",
        text="Postgres can be a job queue. SKIP LOCKED lets workers share a table.",
    ),
    SourceText(title="Sourdough", text="Sourdough needs time."),
]


async def _cite(llm: ClaudeLLM, sources: list[SourceText] = SOURCES) -> list[CitedSegment]:
    result = await llm.cite(
        task="test",
        prompt_version="1",
        system="Answer from the sources.",
        sources=sources,
        question="How can Postgres share jobs?",
        max_output_tokens=800,
    )
    return result.value.segments


async def test_each_source_goes_out_as_a_plain_text_document_with_citations() -> None:
    api = PretendAPI(cited_answer)

    await _cite(ClaudeLLM(model="claude-opus-5-5", client=api.client()))

    (body,) = api.bodies
    assert body["system"] == "Answer from the sources."
    assert body["messages"][0]["content"] == [
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": SOURCES[0].text},
            "citations": {"enabled": True},
            "title": "Postgres as a job queue",
            "context": "Item 1, piece 1 of 2",
        },
        {
            "type": "document",
            "source": {"type": "text", "media_type": "text/plain", "data": "Sourdough needs time."},
            "citations": {"enabled": True},
            "title": "Sourdough",
        },
        {"type": "text", "text": "How can Postgres share jobs?"},
    ]
    # Citations and structured output cannot go together: no output format.
    assert "format" not in body["output_config"]
    assert body["output_config"]["effort"] == "low"
    assert body["max_tokens"] == 800 + THINKING_ROOM
    assert "tools" not in body
    assert body["fallbacks"] == "default"


async def test_a_title_has_at_most_500_characters_and_an_empty_one_is_left_out() -> None:
    api = PretendAPI(cited_answer)
    sources = [SourceText(title="x" * 600, text="One."), SourceText(title="  ", text="Two.")]

    await _cite(ClaudeLLM(model="claude-opus-5-5", client=api.client()), sources)

    first, second, _ = api.bodies[0]["messages"][0]["content"]
    assert first["title"] == "x" * 500
    assert "title" not in second
    assert "context" not in second


async def test_the_citations_become_quotes_of_the_sources() -> None:
    llm = ClaudeLLM(model="claude-opus-5-5", client=PretendAPI(cited_answer).client())

    segments = await _cite(llm)

    # The thinking block carries no text of the answer.
    assert segments == [
        CitedSegment(text="The first source says that "),
        CitedSegment(
            text="postgres can be a job queue.",
            quotes=[Quote(source=0, start=0, end=28, text="Postgres can be a job queue.")],
        ),
    ]


async def test_a_cited_answer_that_was_cut_is_no_answer() -> None:
    def cut(body: dict[str, Any]) -> httpx2.Response:
        return httpx2.Response(200, json=message_json("Postgres can", stop_reason="max_tokens"))

    llm = ClaudeLLM(model="claude-opus-5-5", client=PretendAPI(cut).client())

    with pytest.raises(ProviderError, match="cut") as caught:
        await _cite(llm)
    assert not caught.value.retryable


async def test_a_refused_cited_answer_is_no_answer() -> None:
    declines = PretendAPI(lambda body: refusal("cyber"))
    llm = ClaudeLLM(model="claude-opus-5-5", client=declines.client())

    with pytest.raises(ProviderError, match="declined"):
        await _cite(llm)


async def test_an_error_of_the_api_for_a_cited_answer_can_pass() -> None:
    busy = PretendAPI(lambda body: error(529, "overloaded_error"))

    with pytest.raises(ProviderError) as caught:
        await _cite(ClaudeLLM(model="claude-opus-5-5", client=busy.client()))
    assert caught.value.retryable
