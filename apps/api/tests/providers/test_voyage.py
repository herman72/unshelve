"""The Voyage embedder against a pretend Voyage API. Each test name is one rule.

The pretend API answers in the form of the real one (``POST /v1/embeddings``). Its vectors
come from the fake embedder, so texts that share words get similar vectors, and the shared
conformance suite can run on the real adapter.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from unshelve.contracts.errors import ProviderError
from unshelve.providers import voyage as voyage_module
from unshelve.providers.voyage import MAX_CHARS, MAX_TEXTS, VoyageEmbedder
from unshelve.testing.conformance import EmbedderConformance
from unshelve.testing.fakes import FakeEmbedder

KEY = "test-key"

type Answer = Callable[[dict[str, Any]], httpx.Response]


def _streamed(status: int, body: object, headers: dict[str, str] | None = None) -> httpx.Response:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, headers=headers, stream=httpx.ByteStream(data))


class PretendVoyage:
    """A pretend ``POST https://api.voyageai.com/v1/embeddings``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        # Answers for the next requests, in order. When none is left, each text gets its vector.
        self.next: list[Answer] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._answer)

    async def _answer(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = json.loads(request.content)
        self.requests.append(request)
        self.bodies.append(body)
        if self.next:
            return self.next.pop(0)(body)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return _streamed(401, {"detail": "Provided API key is invalid."})
        return _streamed(200, await vectors_for(body))


async def vectors_for(body: dict[str, Any], *, extra: int = 0, short: int = 0) -> dict[str, Any]:
    """The answer of Voyage for ``body``. The rows come in reverse order, as they may."""
    texts: list[str] = body["input"]
    dim = body["output_dimension"] - short
    vectors = await FakeEmbedder(dim).embed_documents(texts + ["extra"] * extra)
    rows = [
        {"object": "embedding", "embedding": vector, "index": index}
        for index, vector in enumerate(vectors)
    ]
    return {
        "object": "list",
        "data": rows[::-1],
        "model": body["model"],
        "usage": {"total_tokens": sum(len(text.split()) for text in texts)},
    }


@pytest.fixture
def voyage() -> PretendVoyage:
    return PretendVoyage()


def _embedder(voyage: PretendVoyage, *, key: str = KEY) -> VoyageEmbedder:
    return VoyageEmbedder(model="voyage-4-large", dim=1024, api_key=key, transport=voyage.transport)


class TestVoyageEmbedderConformance(EmbedderConformance):
    @pytest.fixture
    def embedder(self, voyage: PretendVoyage) -> VoyageEmbedder:  # pyright: ignore[reportIncompatibleMethodOverride]
        return _embedder(voyage)


# --- What the adapter sends --------------------------------------------------------------


async def test_documents_go_to_the_embeddings_endpoint_with_the_model_and_the_key(
    voyage: PretendVoyage,
) -> None:
    embedder = _embedder(voyage)

    await embedder.embed_documents(["postgres indexes", "vector search"])

    [request] = voyage.requests
    assert str(request.url) == "https://api.voyageai.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer test-key"
    assert voyage.bodies == [
        {
            "input": ["postgres indexes", "vector search"],
            "model": "voyage-4-large",
            "input_type": "document",
            "output_dimension": 1024,
        }
    ]
    assert embedder.spec.model_dump() == {
        "provider": "voyage",
        "model": "voyage-4-large",
        "dim": 1024,
        "distance": "cosine",
        "doc_prefix": "",
        "query_prefix": "",
    }


async def test_a_query_is_sent_as_a_query(voyage: PretendVoyage) -> None:
    await _embedder(voyage).embed_query("how do workers share a queue?")

    assert voyage.bodies[0]["input_type"] == "query"
    assert voyage.bodies[0]["input"] == ["how do workers share a queue?"]


async def test_no_text_is_no_request(voyage: PretendVoyage) -> None:
    assert await _embedder(voyage).embed_documents([]) == []
    assert voyage.requests == []


async def test_many_texts_go_in_several_requests_and_keep_their_order(
    voyage: PretendVoyage,
) -> None:
    texts = [f"note number {n}" for n in range(MAX_TEXTS * 2 + 44)]

    vectors = await _embedder(voyage).embed_documents(texts)

    assert [len(body["input"]) for body in voyage.bodies] == [MAX_TEXTS, MAX_TEXTS, 44]
    assert vectors == await FakeEmbedder(1024).embed_documents(texts)


async def test_long_texts_go_in_several_requests(voyage: PretendVoyage) -> None:
    piece = "x" * 4_000
    per_request = MAX_CHARS // len(piece)  # 25 pieces of 4,000 characters fill one request

    await _embedder(voyage).embed_documents([piece] * (2 * per_request + 10))

    assert [len(body["input"]) for body in voyage.bodies] == [per_request, per_request, 10]


async def test_the_log_has_the_numbers_and_no_text(
    voyage: PretendVoyage, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A new logger: one that an earlier test configured and cached would not be captured.
    monkeypatch.setattr(voyage_module, "log", structlog.get_logger())
    with capture_logs() as logs:
        await _embedder(voyage).embed_documents(["a private note about my plans"])

    [event] = [entry for entry in logs if entry["event"] == "embed.call"]
    assert (event["model"], event["texts"], event["tokens"]) == ("voyage-4-large", 1, 6)
    assert "private" not in repr(event)


# --- When Voyage fails -------------------------------------------------------------------


def _always(status: int, body: object, headers: dict[str, str] | None = None) -> Answer:
    return lambda _: _streamed(status, body, headers)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (429, True),  # too many requests: it can pass
        (500, True),
        (502, True),
        (503, True),
        (504, True),
        (401, True),  # a person must fix the key; the step waits
        (403, True),
        (404, True),
        (400, False),  # this request cannot work
        (422, False),
    ],
)
async def test_each_status_of_voyage_has_its_group(
    voyage: PretendVoyage, status: int, retryable: bool
) -> None:
    voyage.next.append(_always(status, {"detail": f"reason {status}"}))

    with pytest.raises(ProviderError) as caught:
        await _embedder(voyage).embed_documents(["a text"])

    assert caught.value.retryable is retryable
    assert str(caught.value) == f"voyage {status}: reason {status}"


async def test_a_wrong_key_is_a_401_that_can_be_fixed(voyage: PretendVoyage) -> None:
    with pytest.raises(ProviderError) as caught:
        await _embedder(voyage, key="").embed_documents(["a text"])

    assert caught.value.retryable is True
    assert str(caught.value) == "voyage 401: Provided API key is invalid."


async def test_a_429_keeps_the_wait_that_voyage_asks_for(voyage: PretendVoyage) -> None:
    voyage.next.append(_always(429, {"detail": "slow down"}, {"retry-after": "30"}))

    with pytest.raises(ProviderError) as caught:
        await _embedder(voyage).embed_documents(["a text"])

    assert caught.value.retry_after == 30.0


async def test_an_error_without_a_reason_still_has_its_status(voyage: PretendVoyage) -> None:
    voyage.next.append(_always(503, b"<html>busy</html>"))

    with pytest.raises(ProviderError, match=r"^voyage 503: no reason$"):
        await _embedder(voyage).embed_documents(["a text"])


async def test_no_connection_can_pass() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    embedder = VoyageEmbedder(api_key=KEY, transport=httpx.MockTransport(refuse))
    with pytest.raises(ProviderError) as caught:
        await embedder.embed_documents(["a text"])

    assert (str(caught.value), caught.value.retryable) == ("voyage: network", True)


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(b"not json", id="not json"),
        pytest.param({"data": [], "usage": {"total_tokens": 0}}, id="no vector"),
        pytest.param({"vectors": []}, id="other fields"),
    ],
)
async def test_an_answer_that_does_not_fit_can_pass(voyage: PretendVoyage, answer: object) -> None:
    voyage.next.append(_always(200, answer))

    with pytest.raises(ProviderError, match="does not fit") as caught:
        await _embedder(voyage).embed_documents(["a text"])

    assert caught.value.retryable is True


@pytest.mark.parametrize(("extra", "short"), [(1, 0), (0, 1)], ids=["one vector more", "short"])
async def test_vectors_of_the_wrong_number_or_length_do_not_fit(
    voyage: PretendVoyage, extra: int, short: int
) -> None:
    async def wrong(body: dict[str, Any]) -> httpx.Response:
        return _streamed(200, await vectors_for(body, extra=extra, short=short))

    embedder = VoyageEmbedder(api_key=KEY, transport=httpx.MockTransport(_with_body(wrong)))
    with pytest.raises(ProviderError, match="does not fit"):
        await embedder.embed_documents(["a text", "another text"])


def _with_body(
    answer: Callable[[dict[str, Any]], Any],
) -> Callable[[httpx.Request], Any]:
    async def handle(request: httpx.Request) -> httpx.Response:
        return await answer(json.loads(request.content))

    return handle
