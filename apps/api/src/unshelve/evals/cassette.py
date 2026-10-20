"""Recorded vectors: a replay of the calls of an embedder, so that the evaluation runs in CI
without the network and without a key (docs/learn/40).

A cassette holds the spec of the embedder that made the vectors, and one vector for each text
that it embedded, as a document or as a query. ``Replay`` answers from the cassette, and it
refuses a text that the cassette does not hold: then the golden set changed, and ``make
eval-live`` must record it again. ``Recorder`` asks a live embedder and keeps each answer.

The vectors are kept as half-precision floats (16 bits) in base64: a vector of 1,024 numbers
takes about 2.7 KB, not about 9 KB of decimal text. The recorder rounds each vector in the same
way before it gives it back, so a live run and its replay rank the items the same.
"""

import base64
import hashlib
import json
import struct
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.protocols import Embedder

Kind = Literal["document", "query"]


class MissingVectorError(LookupError):
    """The cassette holds no vector for a text: the golden set changed since the recording."""


def _key(kind: Kind, text: str) -> str:
    return hashlib.sha256(f"{kind}\n{text}".encode()).hexdigest()


def _pack(vector: Sequence[float]) -> str:
    # "e": an IEEE 754 half-precision float, little-endian ("<").
    return base64.b64encode(struct.pack(f"<{len(vector)}e", *vector)).decode()


def _unpack(packed: str) -> list[float]:
    raw = base64.b64decode(packed)
    return list(struct.unpack(f"<{len(raw) // 2}e", raw))


@dataclass
class Cassette:
    spec: EmbeddingSpec
    # The key of each text (its kind and its words, hashed), and its packed vector.
    vectors: dict[str, str] = field(default_factory=dict[str, str])

    @classmethod
    def load(cls, path: Path) -> "Cassette":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(spec=EmbeddingSpec.model_validate(data["spec"]), vectors=data["vectors"])

    def save(self, path: Path) -> None:
        data = {"spec": self.spec.model_dump(mode="json"), "vectors": self.vectors}
        path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


class Replay:
    """An ``Embedder`` that answers from a cassette, with no network."""

    def __init__(self, cassette: Cassette) -> None:
        self.spec = cassette.spec
        self._vectors = cassette.vectors

    def _vector(self, kind: Kind, text: str) -> list[float]:
        packed = self._vectors.get(_key(kind, text))
        if packed is None:
            msg = f"no recorded vector for this {kind}: record the golden set with make eval-live"
            raise MissingVectorError(msg)
        return _unpack(packed)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector("document", text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector("query", text)


class Recorder:
    """An ``Embedder`` that asks ``inner`` and keeps each vector in ``cassette``."""

    def __init__(self, inner: Embedder) -> None:
        self.spec = inner.spec
        self._inner = inner
        self.cassette = Cassette(spec=inner.spec)

    def _keep(self, kind: Kind, text: str, vector: Sequence[float]) -> list[float]:
        packed = _pack(vector)
        self.cassette.vectors[_key(kind, text)] = packed
        return _unpack(packed)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = await self._inner.embed_documents(texts)
        return [
            self._keep("document", text, vector)
            for text, vector in zip(texts, vectors, strict=True)
        ]

    async def embed_query(self, text: str) -> list[float]:
        return self._keep("query", text, await self._inner.embed_query(text))
