"""The seams of the system. Each has a fake in ``unshelve.testing.fakes`` and a conformance
suite in ``unshelve.testing.conformance`` that every implementation must pass."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from pydantic import BaseModel

from unshelve.contracts.dto import (
    BatchAnswer,
    BatchQuestion,
    CitedAnswer,
    EmbeddingSpec,
    Extracted,
    FetchResult,
    ImagePart,
    LLMResult,
    MediaRef,
    Published,
    SourceRef,
    SourceText,
    TextPart,
)


def accepts(content_type: str | None, accept: Sequence[str]) -> bool:
    """Whether ``content_type`` satisfies ``accept``.

    Patterns are ``type/subtype``, ``type/*`` or ``*/*``. An empty ``accept`` allows anything.
    Parameters such as ``; charset=utf-8`` are ignored. A missing content type matches only
    an empty ``accept`` or ``*/*``.
    """
    if not accept or "*/*" in accept:
        return True
    if content_type is None:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    main = media.split("/", 1)[0]
    return any(pattern.lower() in (media, f"{main}/*") for pattern in accept)


class Fetcher(Protocol):
    """The only component allowed to open arbitrary URLs.

    Raises ``FetchError``: ``too_large`` when the body exceeds ``max_bytes`` (never a silent
    truncation), ``unsupported`` when the content type does not satisfy ``accept``.
    """

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult: ...


class BlobStore(Protocol):
    """Content-addressed storage for media. The key is the SHA-256 of the bytes, in hex."""

    async def put(self, data: bytes, mime: str) -> str: ...

    async def get(self, key: str) -> bytes:
        """Raises ``KeyError`` when the key is unknown."""
        ...


class LLMProvider(Protocol):
    """Structured generation. ``task`` and ``prompt_version`` identify the call for caching."""

    async def generate[T: BaseModel](
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        parts: Sequence[TextPart | ImagePart],
        schema: type[T],
        max_output_tokens: int,
    ) -> LLMResult[T]: ...


class CitingLLM(Protocol):
    """An answer to ``question`` from the given ``sources`` alone, in which each statement can
    point at the parts of the sources that support it (the native citations of the provider).

    The answer is free text, not a schema: the model writes the citations between its words,
    which structured output does not allow. ``task`` and ``prompt_version`` identify the call
    for caching, as in ``LLMProvider``. Each quote points inside ``sources``.
    """

    async def cite(
        self,
        *,
        task: str,
        prompt_version: str,
        system: str,
        sources: Sequence[SourceText],
        question: str,
        max_output_tokens: int,
    ) -> LLMResult[CitedAnswer]: ...


class ModelProvider(LLMProvider, CitingLLM, Protocol):
    """A model that gives both kinds of answer: in a schema, and in text with citations."""


class BatchProvider(Protocol):
    """Many questions to a model in one batch, at a lower price, answered later.

    Each question has its own ``custom_id``, and its answer comes back with it, in any order.
    All questions of a batch share one schema. The batch ends within hours: ``ended`` says when.
    """

    model: str

    async def send(self, questions: Sequence[BatchQuestion], schema: type[BaseModel]) -> str:
        """Send the questions. Returns the id of the batch at the provider."""
        ...

    async def ended(self, batch_id: str) -> bool: ...

    def answers[T: BaseModel](
        self, batch_id: str, schema: type[T]
    ) -> AsyncIterator[BatchAnswer[T]]:
        """The answer to each question of an ended batch."""
        ...


class Embedder(Protocol):
    spec: EmbeddingSpec

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class FeedTarget(Protocol):
    """Where the public feed goes (docs/adr/0004): a file, a git repository or object storage.

    ``publish`` stores the bytes as the feed, and says whether the target held other bytes
    before. ``read`` returns the feed that the target holds now, or None before the first one.
    Both raise ``PublishError``.
    """

    async def publish(self, data: bytes) -> Published: ...

    async def read(self) -> bytes | None: ...


class FileSource(Protocol):
    """The bytes of a file of a saved message: a document or a photo that you sent.

    Raises ``FetchError``: ``unsupported`` for a file that it cannot get, ``too_large`` over
    ``max_bytes``, and the reasons of a fetch for a failure of the network.
    """

    async def read(self, media: MediaRef, *, max_bytes: int) -> bytes: ...


@dataclass(frozen=True, slots=True)
class ExtractContext:
    """What an extractor may use. It never opens a socket itself."""

    fetcher: Fetcher
    blobs: BlobStore
    llm: LLMProvider
    # The files of the message. None where no file can be read: the CLI reads links only.
    files: FileSource | None = None


class Extractor(Protocol):
    name: ClassVar[str]
    version: ClassVar[int]  # bump when the output for the same input changes

    def match(self, ref: SourceRef) -> int:
        """How well this extractor fits ``ref``. 0 means no; the highest score wins."""
        ...

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted: ...
