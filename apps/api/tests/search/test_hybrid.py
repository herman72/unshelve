"""Hybrid search on the made-up corpus: three retrievers, one ranking. Each test name is one
rule."""

import time

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tests.search.corpus import TOPICS, Corpus, TopicEmbedder, seed
from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.enums import ItemStatus
from unshelve.contracts.errors import ProviderError
from unshelve.db.models import Item
from unshelve.search import hybrid
from unshelve.search.hybrid import Ranks, VectorSearch
from unshelve.search.retrievers import Filters


@pytest.fixture
async def corpus(db: AsyncSession) -> Corpus:
    return await seed(db)


def _ids(ranking: hybrid.Ranking) -> list[int]:
    return [hit.item_id for hit in ranking.hits]


async def test_a_search_fuses_the_three_lists_and_keeps_the_place_in_each(
    db: AsyncSession, corpus: Corpus
) -> None:
    # Full text: queue, pgvector, text_only. Trigram: queue. Vector ("postgres" is about
    # queues): queue, pgvector, rust.
    ranking = await hybrid.search(db, TopicEmbedder(), corpus.owner_id, "postgres")

    assert [(hit.item_id, hit.ranks) for hit in ranking.hits] == [
        (corpus["queue"], Ranks(full_text=1, trigram=1, vector=1)),
        (corpus["pgvector"], Ranks(full_text=2, trigram=None, vector=2)),
        # Equal scores, 1/63 each: the lower id first.
        (corpus["rust"], Ranks(full_text=None, trigram=None, vector=3)),
        (corpus["text_only"], Ranks(full_text=3, trigram=None, vector=None)),
    ]
    assert [hit.score for hit in ranking.hits] == pytest.approx([3 / 61, 2 / 62, 1 / 63, 1 / 63])
    assert ranking.vector_search is VectorSearch.USED
    assert ranking.set_id == corpus.set_id
    assert ranking.query_vector == pytest.approx([1.0, 0.0, 0.0])


async def test_a_failing_embedder_leaves_the_words(db: AsyncSession, corpus: Corpus) -> None:
    embedder = TopicEmbedder(error=ProviderError("voyage: 503", retryable=True))

    ranking = await hybrid.search(db, embedder, corpus.owner_id, "postgres")

    assert ranking.vector_search is VectorSearch.FAILED
    assert _ids(ranking) == [corpus["queue"], corpus["pgvector"], corpus["text_only"]]
    assert all(hit.ranks.vector is None for hit in ranking.hits)
    assert (ranking.set_id, ranking.query_vector) == (None, None)


async def test_a_slow_embedder_is_not_waited_for(db: AsyncSession, corpus: Corpus) -> None:
    started = time.monotonic()

    ranking = await hybrid.search(
        db, TopicEmbedder(delay=5.0), corpus.owner_id, "postgres", vector_wait=0.05
    )

    assert ranking.vector_search is VectorSearch.FAILED
    assert _ids(ranking) == [corpus["queue"], corpus["pgvector"], corpus["text_only"]]
    assert time.monotonic() - started < 2.0


async def test_without_vectors_of_its_model_the_embedder_is_not_asked(
    db: AsyncSession, corpus: Corpus
) -> None:
    embedder = TopicEmbedder(spec=EmbeddingSpec(provider="test", model="never-used", dim=3))

    ranking = await hybrid.search(db, embedder, corpus.owner_id, "postgres")

    assert ranking.vector_search is VectorSearch.NO_VECTORS
    assert embedder.calls == 0
    assert _ids(ranking) == [corpus["queue"], corpus["pgvector"], corpus["text_only"]]


async def test_no_transaction_is_open_while_the_embedder_works(
    db: AsyncSession, corpus: Corpus
) -> None:
    open_during_call: list[bool] = []
    embedder = TopicEmbedder(on_call=lambda: open_during_call.append(db.in_transaction()))

    await hybrid.search(db, embedder, corpus.owner_id, "postgres")

    assert open_during_call == [False]


async def test_the_filters_reach_each_retriever(db: AsyncSession, corpus: Corpus) -> None:
    # Each of these items is found by one retriever only: text_only by full text, rust by
    # vector (for "postgres"), and persian_ml by trigram (for "کتابهای").
    only_one = {corpus["text_only"], corpus["rust"], corpus["persian_ml"]}
    found_before = [
        *_ids(await hybrid.search(db, TopicEmbedder(), corpus.owner_id, "postgres")),
        *_ids(await hybrid.search(db, TopicEmbedder(), corpus.owner_id, "کتابهای")),
    ]
    await db.execute(sa.update(Item).where(Item.id.in_(only_one)).values(status="archived"))
    inbox = Filters(statuses=frozenset({ItemStatus.INBOX}))

    found = [
        *_ids(await hybrid.search(db, TopicEmbedder(), corpus.owner_id, "postgres", inbox)),
        *_ids(await hybrid.search(db, TopicEmbedder(), corpus.owner_id, "کتابهای", inbox)),
    ]

    assert only_one <= set(found_before)
    assert not only_one & set(found)


def test_the_corpus_embedder_has_the_spec_of_the_active_set() -> None:
    # The tests above rely on it: the seed makes the active set "test/topics" with 3 numbers.
    assert (TOPICS.provider, TOPICS.model, TOPICS.dim) == ("test", "topics", 3)
