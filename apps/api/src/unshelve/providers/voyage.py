"""Voyage AI as the embedder, through its REST API (ADR 0012).

Voyage AI is part of MongoDB. A key of the Voyage platform (``pa-...``) works at
api.voyageai.com, and a model key of MongoDB Atlas (``al-...``) at ai.mongodb.com. The two
addresses take the same request. The official client chooses the address by the start of the
key, and so does this adapter.

One request embeds many texts. ``input_type`` tells Voyage whether a text is a document or
a query. Voyage puts its own short instruction in front of the text for each type, so the
embedding set has no prefixes of its own.

The errors fall into the same three groups as the errors of the model (ADR 0011):

- It can pass: 429, 5xx, no connection, a timeout, an answer that does not fit.
- A person must fix it: 401 (the key), 403, 404. The step waits in the same way, so that the
  item gets its vectors after the fix.
- This request cannot work: 400, such as a model name that Voyage does not know.
"""

import time
from collections.abc import Iterator, Sequence
from typing import Literal

import structlog
from pydantic import BaseModel, ValidationError

from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.enums import Distance
from unshelve.contracts.errors import ProviderError
from unshelve.net.service import (
    ServiceAnswer,
    ServiceClient,
    ServiceUnreachableError,
    Transport,
)

log = structlog.get_logger()

VOYAGE_API = "https://api.voyageai.com"
ATLAS_API = "https://ai.mongodb.com"
EMBEDDINGS = "/v1/embeddings"
# One request holds at most this many texts and characters. Voyage allows 1,000 texts, and
# 120,000 tokens for voyage-4-large. 100,000 characters are fewer tokens, also in Persian.
MAX_TEXTS = 128
MAX_CHARS = 100_000
# 128 vectors of 2,048 numbers, as JSON text, are about 6 MB.
MAX_ANSWER_BYTES = 32_000_000
_FIX_BY_HAND = frozenset({401, 403, 404})


class _Vector(BaseModel):
    embedding: list[float]
    index: int


class _Usage(BaseModel):
    total_tokens: int


class _Answer(BaseModel):
    data: list[_Vector]
    usage: _Usage


class _Problem(BaseModel):
    """The body of a 4xx answer."""

    detail: str


def api_for(api_key: str) -> str:
    """The address for the key: MongoDB Atlas for a model key of Atlas, else Voyage."""
    return ATLAS_API if api_key.startswith("al-") else VOYAGE_API


def voyage_spec(model: str, dim: int) -> EmbeddingSpec:
    """The embedding set of the vectors of ``model`` with ``dim`` numbers."""
    return EmbeddingSpec(provider="voyage", model=model, dim=dim, distance=Distance.COSINE)


class VoyageEmbedder:
    """The ``Embedder`` of Voyage AI. It needs ``VOYAGE_API_KEY``."""

    def __init__(
        self,
        *,
        model: str = "voyage-4-large",
        dim: int = 1024,
        api_key: str = "",
        timeout: float = 60.0,
        transport: Transport | None = None,
    ) -> None:
        """``transport`` is for the tests: a pretend Voyage in the same process."""
        self.spec = voyage_spec(model, dim)
        self._client = ServiceClient(
            api_for(api_key),
            headers={"authorization": f"Bearer {api_key}"},
            timeout=timeout,
            max_bytes=MAX_ANSWER_BYTES,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for batch in _batches(texts):
            vectors += await self._embed(batch, "document")
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await self._embed([text], "query")
        return vector

    async def _embed(
        self, texts: list[str], input_type: Literal["document", "query"]
    ) -> list[list[float]]:
        body = {
            "input": texts,
            "model": self.spec.model,
            "input_type": input_type,
            "output_dimension": self.spec.dim,
        }
        started = time.monotonic()
        try:
            answer = await self._client.post_json(EMBEDDINGS, body)
        except ServiceUnreachableError as error:
            raise ProviderError(f"voyage: {error.reason}", retryable=True) from error
        if answer.status != 200:
            raise _error(answer)
        vectors, tokens = _read(answer, count=len(texts), dim=self.spec.dim)
        # The numbers only: a text can be private.
        log.info(
            "embed.call",
            model=self.spec.model,
            texts=len(texts),
            tokens=tokens,
            seconds=round(time.monotonic() - started, 2),
        )
        return vectors


def _batches(texts: Sequence[str]) -> Iterator[list[str]]:
    """The texts in order, in requests of at most ``MAX_TEXTS`` texts and ``MAX_CHARS``."""
    batch: list[str] = []
    characters = 0
    for text in texts:
        if batch and (len(batch) == MAX_TEXTS or characters + len(text) > MAX_CHARS):
            yield batch
            batch, characters = [], 0
        batch.append(text)
        characters += len(text)
    if batch:
        yield batch


def _read(answer: ServiceAnswer, *, count: int, dim: int) -> tuple[list[list[float]], int]:
    """The vectors in the order of the texts, and the tokens that Voyage counted."""
    try:
        found = _Answer.model_validate_json(answer.body)
    except ValidationError as error:
        raise ProviderError("voyage: the answer does not fit", retryable=True) from error
    rows = sorted(found.data, key=lambda row: row.index)
    if [row.index for row in rows] != list(range(count)) or any(
        len(row.embedding) != dim for row in rows
    ):
        raise ProviderError(
            f"voyage: the answer does not fit: {len(rows)} vectors for {count} texts",
            retryable=True,
        )
    return [row.embedding for row in rows], found.usage.total_tokens


def _error(answer: ServiceAnswer) -> ProviderError:
    message = f"voyage {answer.status}: {_detail(answer)}"
    if answer.status == 429 or answer.status >= 500:
        return ProviderError(message, retryable=True, retry_after=answer.retry_after)
    return ProviderError(message, retryable=answer.status in _FIX_BY_HAND)


def _detail(answer: ServiceAnswer) -> str:
    """The reason that Voyage gives in a 4xx answer: ``{"detail": "..."}``."""
    try:
        return _Problem.model_validate_json(answer.body).detail[:200]
    except ValidationError:
        return "no reason"
