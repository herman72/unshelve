"""Deterministic stand-ins for every external dependency. No network, no clock, no randomness."""

import hashlib
import math
import re
import types
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from unshelve.contracts.dto import (
    EmbeddingSpec,
    Extracted,
    FetchResult,
    ImagePart,
    LLMResult,
    LLMUsage,
    MediaRef,
    SourceRef,
    TextPart,
)
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import ExtractContext, accepts

_WORD = re.compile(r"\w+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


# The tags of a prompt, such as <saved_text>. A fake card is made from the words of the text.
_PROMPT_TAG = re.compile(r"</?[A-Za-z_][\w-]*>")


# --- Fetcher -----------------------------------------------------------------------------


class FakeFetcher:
    """Serves pages from a dictionary. An unknown URL is a 404."""

    def __init__(self, pages: Mapping[str, FetchResult | FetchError] | None = None) -> None:
        self.pages: dict[str, FetchResult | FetchError] = dict(pages or {})
        self.requested: list[str] = []

    def add(self, url: str, body: bytes | str, content_type: str = "text/html") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.pages[url] = FetchResult(
            url=url, final_url=url, status=200, content_type=content_type, body=data
        )

    async def get(self, url: str, *, max_bytes: int, accept: Sequence[str]) -> FetchResult:
        self.requested.append(url)
        page = self.pages.get(url)
        if page is None:
            raise FetchError("http_error", url, status=404)
        if isinstance(page, FetchError):
            raise page
        # The type first, as the real fetcher: it knows the type from the headers.
        if not accepts(page.content_type, accept):
            raise FetchError("unsupported", url, content_type=page.content_type)
        if len(page.body) > max_bytes:
            raise FetchError("too_large", url)
        return page


# --- Blob store --------------------------------------------------------------------------


class InMemoryBlobStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, data: bytes, mime: str) -> str:
        key = hashlib.sha256(data).hexdigest()
        self._blobs[key] = data
        return key

    async def get(self, key: str) -> bytes:
        return self._blobs[key]


# --- Files of messages -------------------------------------------------------------------


class FakeFiles:
    """Serves the bytes of files by their ``file_id``. An unknown file is unsupported."""

    def __init__(self, files: Mapping[str, bytes | FetchError] | None = None) -> None:
        self.files: dict[str, bytes | FetchError] = dict(files or {})
        self.read_ids: list[str] = []

    async def read(self, media: MediaRef, *, max_bytes: int) -> bytes:
        label = f"telegram file {media.file_unique_id or 'without an id'}"
        found = self.files.get(media.file_id) if media.file_id is not None else None
        if found is None:
            raise FetchError("unsupported", label)
        self.read_ids.append(str(media.file_id))
        if isinstance(found, FetchError):
            raise found
        if len(found) > max_bytes:
            raise FetchError("too_large", label)
        return found


# --- Embedder ----------------------------------------------------------------------------


class FakeEmbedder:
    """A hashed bag of words. Texts that share words get similar vectors, which is enough
    to test retrieval logic without a model."""

    def __init__(self, dim: int = 64) -> None:
        self.spec = EmbeddingSpec(provider="fake", model=f"hashed-bow-{dim}", dim=dim)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.spec.dim
        for word in _words(text):
            digest = hashlib.blake2b(word.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4]) % self.spec.dim
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vector))
        if norm == 0.0:  # no words: a fixed unit vector, so the result is always usable
            vector[0] = norm = 1.0
        return [x / norm for x in vector]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(self.spec.doc_prefix + text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(self.spec.query_prefix + text)


# --- LLM ---------------------------------------------------------------------------------


def synthesize[T: BaseModel](schema: type[T], text: str) -> T:
    """Build a valid instance of ``schema`` from ``text``, deterministically.

    Strings are made from the first words of the input, so a fake summary still looks
    related to its source. Constrained fields that this cannot satisfy raise a validation
    error; give ``FakeLLM`` a ``responder`` for those schemas.
    """
    return schema.model_validate(_model_value(schema, _words(text)))


def _model_value(model: type[BaseModel], words: list[str]) -> dict[str, Any]:
    return {name: _value(info.annotation, name, words) for name, info in model.model_fields.items()}


def _value(annotation: Any, name: str, words: list[str]) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        return _value(next(a for a in args if a is not type(None)), name, words)
    if origin is Literal:
        return args[0]
    if origin in (list, Sequence):
        distinct = list(dict.fromkeys(w for w in words if len(w) > 3))[:3] or [name]
        if args and args[0] is str:
            return distinct
        return [_value(args[0], name, words)] if args else []
    if origin in (dict, Mapping):
        return {}
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return _model_value(annotation, words)
        if issubclass(annotation, Enum):
            return next(iter(annotation))
        if annotation is bool:
            return False
        if annotation is int:
            return 1
        if annotation is float:
            return 0.5
        if annotation is str:
            return " ".join(words[:12]) or name
        if annotation is datetime:
            return datetime(2026, 1, 1, tzinfo=UTC)
    msg = f"FakeLLM cannot synthesize a value for {name!r}: {annotation!r}; pass a responder"
    raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class FakeLLMCall:
    task: str
    prompt_version: str
    system: str
    text: str
    images: int


Responder = Callable[[FakeLLMCall, type[BaseModel]], BaseModel | None]


@dataclass
class FakeLLM:
    """Returns schema-valid output for any schema. ``responder`` can script specific tasks;
    when it returns None, the output is synthesized from the input."""

    responder: Responder | None = None
    calls: list[FakeLLMCall] = field(default_factory=list[FakeLLMCall])
    model: ClassVar[str] = "fake-llm"

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
        text = "\n".join(p.text for p in parts if isinstance(p, TextPart))
        images = sum(isinstance(p, ImagePart) for p in parts)
        call = FakeLLMCall(task, prompt_version, system, text, images)
        self.calls.append(call)

        scripted = self.responder(call, schema) if self.responder else None
        content = _PROMPT_TAG.sub(" ", text)
        value = (
            schema.model_validate(scripted.model_dump())
            if scripted
            else synthesize(schema, content)
        )
        usage = LLMUsage(
            input_tokens=len(_words(system)) + len(_words(text)),
            output_tokens=min(len(_words(value.model_dump_json())), max_output_tokens),
        )
        return LLMResult[schema](value=value, model=self.model, usage=usage)


# --- Extractor ---------------------------------------------------------------------------


class EchoExtractor:
    """Matches anything, weakly, and returns the saved text itself. The last resort when
    no real extractor fits, and the extractor used by the walking skeleton."""

    name: ClassVar[str] = "echo"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return 1 if (ref.text or ref.url) else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        return Extracted(kind=ItemKind.NOTE, text=ref.text or ref.url or "", final_url=ref.url)
