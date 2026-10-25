"""POST /search/overview over HTTP, on the made-up corpus of tests/search. Each test name is one
rule. The model is the fake: it quotes the first sentence of the first three sources.

The first test is the contract: the exact shape that the web app reads.
"""

from collections.abc import AsyncGenerator, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.support import ClientWith, problem_body
from tests.search.corpus import Corpus, TopicEmbedder
from unshelve.api.schemas import ItemSummary
from unshelve.contracts.dto import CitedAnswer, SourceText
from unshelve.contracts.errors import ProviderError
from unshelve.db.models import Chunk, Item, LLMCall
from unshelve.testing.fakes import FakeLLM, FakeLLMCall

OVERVIEW = "/api/v1/search/overview"

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def client(client_with: ClientWith, llm: FakeLLM) -> AsyncGenerator[httpx.AsyncClient]:
    async with client_with(TopicEmbedder(), llm) as client:
        yield client


async def _overview(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await client.post(OVERVIEW, json=body)
    assert response.status_code == 200, response.text
    answer: dict[str, Any] = response.json()
    return answer


async def test_an_overview_answers_with_the_shape_of_the_contract(
    client: httpx.AsyncClient, corpus: Corpus
) -> None:
    answer = await _overview(client, q="postgres")

    assert set(answer) == {"segments", "sources", "vector_search"}
    assert answer["vector_search"] == "used"
    assert [source["id"] for source in answer["sources"]] == [
        corpus["queue"],
        corpus["pgvector"],
        corpus["rust"],
    ]
    assert all(set(source) == set(ItemSummary.model_fields) for source in answer["sources"])
    assert all(set(segment) == {"text", "citations"} for segment in answer["segments"])
    cited = next(segment for segment in answer["segments"] if segment["citations"])
    assert set(cited["citations"][0]) == {"item_id", "chunk_id", "quote"}


async def test_the_sources_come_in_the_order_of_search(
    client: httpx.AsyncClient, corpus: Corpus
) -> None:
    # "rust": the Rust item first, then by meaning the job queue and pgvector. Not the order of
    # their ids.
    answer = await _overview(client, q="rust")

    assert [source["id"] for source in answer["sources"]] == [
        corpus["rust"],
        corpus["queue"],
        corpus["pgvector"],
    ]


async def test_every_citation_of_the_answer_maps_to_a_retrieved_chunk(
    client: httpx.AsyncClient, corpus: Corpus, committed_db: Sessions
) -> None:
    answer = await _overview(client, q="postgres")

    citations = [citation for segment in answer["segments"] for citation in segment["citations"]]
    assert len(citations) == 3
    sources = {source["id"] for source in answer["sources"]}
    async with committed_db() as db:
        for citation in citations:
            chunk = await db.get(Chunk, citation["chunk_id"])
            assert chunk is not None
            assert chunk.item_id == citation["item_id"]
            assert chunk.embedding_set_id == corpus.set_id
            assert citation["quote"] in chunk.text
            assert citation["item_id"] in sources


async def test_the_same_question_is_answered_from_the_ledger(
    client_with: ClientWith, committed_db: Sessions
) -> None:
    # Without a model of the test, the app builds its own: the ledger in front of the fake.
    async with client_with(TopicEmbedder()) as client:
        first = await _overview(client, q="postgres")
        second = await _overview(client, q="postgres")

    assert second == first
    async with committed_db() as db:
        calls = (await db.scalars(sa.select(LLMCall.task))).all()
    assert calls == ["overview"]


async def test_the_limit_and_the_status_choose_the_items_that_are_read(
    client: httpx.AsyncClient, corpus: Corpus
) -> None:
    one = await _overview(client, q="postgres", limit=1)
    done = await _overview(client, q="postgres", status=["done"])

    assert [source["id"] for source in one["sources"]] == [corpus["queue"]]
    # No item of the corpus is done: nothing to read, so no answer and no cost.
    assert (done["segments"], done["sources"]) == ([], [])


async def test_unread_only_leaves_out_the_items_that_you_read(
    client: httpx.AsyncClient, corpus: Corpus, committed_db: Sessions
) -> None:
    async with committed_db() as db:
        read_at = datetime(2026, 10, 18, 19, 0, tzinfo=UTC)
        await db.execute(sa.update(Item).where(Item.id == corpus["queue"]).values(read_at=read_at))
        await db.commit()

    everything = await _overview(client, q="postgres")
    unread = await _overview(client, q="postgres", unread=True)

    queue, pgvector, rust = corpus["queue"], corpus["pgvector"], corpus["rust"]
    assert [source["id"] for source in everything["sources"]] == [queue, pgvector, rust]
    assert [source["id"] for source in unread["sources"]] == [pgvector, rust]


async def test_when_the_model_cannot_answer_the_answer_is_503(
    client_with: ClientWith,
) -> None:
    def budget_used(call: FakeLLMCall, sources: Sequence[SourceText]) -> CitedAnswer:
        raise ProviderError("the daily budget of $1.00 is used", retryable=True)

    async with client_with(TopicEmbedder(), FakeLLM(cited_responder=budget_used)) as client:
        response = await client.post(OVERVIEW, json={"q": "postgres"})

    problem = problem_body(response, 503)
    assert problem["detail"] == "the model gave no answer: the daily budget of $1.00 is used"


@pytest.mark.parametrize("body", [{"q": ""}, {"q": "postgres", "limit": 31}, {"question": "x"}])
async def test_an_overview_needs_a_question_and_a_limit_of_30_or_less(
    client: httpx.AsyncClient, body: dict[str, Any]
) -> None:
    problem_body(await client.post(OVERVIEW, json=body), 422)
