"""The seams of the system. Each has a fake in ``unshelve.testing.fakes`` and a conformance
suite in ``unshelve.testing.conformance`` that every implementation must pass."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Protocol

from pydantic import BaseModel

from unshelve.contracts.dto import (
    EmbeddingSpec,
    Extracted,
    FetchResult,
    ImagePart,
    LLMResult,
    MediaRef,
    SourceRef,
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


class Embedder(Protocol):
    spec: EmbeddingSpec

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


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
