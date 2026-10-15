"""The Voyage embedder against the real Voyage API. It runs only when it is asked for:

    VOYAGE_API_KEY=... uv run pytest -m live -o addopts="" tests/providers/test_voyage_live.py

``-o addopts=""`` lifts the rule that no test may leave this computer. The tests send about 20
short texts: a few hundred tokens, far below the free tokens of a Voyage account.
"""

import math
import os
from collections.abc import AsyncIterator, Sequence

import pytest

from unshelve.providers.voyage import VoyageEmbedder
from unshelve.testing.conformance import EmbedderConformance

pytestmark = pytest.mark.live


@pytest.fixture
async def voyage() -> AsyncIterator[VoyageEmbedder]:
    key = os.environ.get("VOYAGE_API_KEY", "")
    if not key:
        pytest.skip("needs VOYAGE_API_KEY")
    embedder = VoyageEmbedder(api_key=key)
    yield embedder
    await embedder.aclose()


class TestVoyageLive(EmbedderConformance):
    @pytest.fixture
    def embedder(self, voyage: VoyageEmbedder) -> VoyageEmbedder:  # pyright: ignore[reportIncompatibleMethodOverride]
        return voyage


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


async def test_a_persian_query_finds_the_english_text_on_its_topic(voyage: VoyageEmbedder) -> None:
    query = await voyage.embed_query("صف کارها در پستگرس")  # "a job queue in Postgres"
    related, unrelated = await voyage.embed_documents(
        [
            "Postgres can be a job queue: SKIP LOCKED lets many workers share one table.",
            "A recipe for banana bread with walnuts and brown sugar.",
        ]
    )

    assert _cosine(query, related) > _cosine(query, unrelated)


async def test_an_english_query_finds_the_persian_text_on_its_topic(voyage: VoyageEmbedder) -> None:
    query = await voyage.embed_query("how a neural network learns its weights")
    related, unrelated = await voyage.embed_documents(
        [
            # "A neural network learns by changing the weights of its connections."
            "شبکه عصبی با تغییر وزن اتصال‌هایش یاد می‌گیرد.",
            # "Tehran has a cold winter and a hot summer."
            "تهران زمستان سرد و تابستان گرمی دارد.",
        ]
    )

    assert _cosine(query, related) > _cosine(query, unrelated)
