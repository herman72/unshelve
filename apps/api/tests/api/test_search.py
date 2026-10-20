"""GET /search over HTTP, on the made-up corpus of tests/search. Each test name is one rule.

The first test is the contract: the exact shape that the web app reads.
"""

from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.support import ClientWith, problem_body
from tests.search.corpus import Corpus, TopicEmbedder
from unshelve.api.schemas import ItemSummary
from unshelve.contracts.enums import ItemStatus
from unshelve.contracts.errors import ProviderError
from unshelve.db.models import Item, ItemTag, Tag

SEARCH = "/api/v1/search"

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def client(client_with: ClientWith) -> AsyncGenerator[httpx.AsyncClient]:
    async with client_with(TopicEmbedder()) as client:
        yield client


async def _search(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get(SEARCH, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _ids(body: dict[str, Any]) -> list[int]:
    return [hit["item"]["id"] for hit in body["items"]]


# --- The contract ------------------------------------------------------------------------


async def test_a_search_answers_with_the_shape_of_the_contract(
    client: httpx.AsyncClient, corpus: Corpus
) -> None:
    body = await _search(client, q="concurrency")

    assert set(body) == {"items", "next_cursor", "vector_search"}
    assert body["vector_search"] == "used"
    hit = next(hit for hit in body["items"] if hit["item"]["id"] == corpus["text_only"])
    assert set(hit) == {"item", "score", "snippet", "ranks"}
    assert set(hit["item"]) == set(ItemSummary.model_fields)
    assert isinstance(hit["score"], float)
    assert set(hit["ranks"]) == {"fts", "trigram", "vector"}
    assert hit["ranks"]["fts"] == 1
    assert set(hit["snippet"]) == {"source", "parts"}
    assert hit["snippet"]["source"] == "text"
    assert {"text": "concurrency", "hit": True} in hit["snippet"]["parts"]
    assert all(set(part) == {"text", "hit"} for part in hit["snippet"]["parts"])


# --- The ranking -------------------------------------------------------------------------


async def test_the_hits_come_in_the_fused_order_with_their_place_in_each_list(
    client: httpx.AsyncClient, corpus: Corpus
) -> None:
    body = await _search(client, q="postgres")

    assert [(hit["item"]["id"], hit["ranks"]) for hit in body["items"]] == [
        (corpus["queue"], {"fts": 1, "trigram": 1, "vector": 1}),
        (corpus["pgvector"], {"fts": 2, "trigram": None, "vector": 2}),
        (corpus["rust"], {"fts": None, "trigram": None, "vector": 3}),
        (corpus["text_only"], {"fts": 3, "trigram": None, "vector": None}),
    ]
    assert [hit["score"] for hit in body["items"]] == pytest.approx(
        [3 / 61, 2 / 62, 1 / 63, 1 / 63]
    )
    # No merged copy, and no item of another owner.
    assert corpus["copy"] not in _ids(body)
    assert corpus["stranger"] not in _ids(body)


async def test_each_hit_says_why_it_was_found(client: httpx.AsyncClient, corpus: Corpus) -> None:
    body = await _search(client, q="worker")
    snippets = {hit["item"]["id"]: hit["snippet"] for hit in body["items"]}

    assert snippets[corpus["queue"]]["source"] == "text"  # "A worker takes a lease"
    assert snippets[corpus["pgvector"]]["source"] == "passage"  # a piece about queues
    assert snippets[corpus["rust"]] is None  # its card is its closest vector


async def test_when_the_embedder_fails_the_words_still_answer(
    client_with: ClientWith, corpus: Corpus
) -> None:
    down = TopicEmbedder(error=ProviderError("voyage: 503", retryable=True))

    async with client_with(down) as client:
        body = await _search(client, q="postgres")

    assert body["vector_search"] == "failed"
    assert _ids(body) == [corpus["queue"], corpus["pgvector"], corpus["text_only"]]


# --- Filters -----------------------------------------------------------------------------


async def test_filters_narrow_the_hits(
    client: httpx.AsyncClient, corpus: Corpus, committed_db: Sessions
) -> None:
    async with committed_db() as db:
        await db.execute(
            sa.update(Item).where(Item.id == corpus["pgvector"]).values(status="archived")
        )
        tag = Tag(owner_id=corpus.owner_id, slug="databases", name="Databases")
        db.add(tag)
        await db.flush()
        db.add_all(
            ItemTag(item_id=corpus[name], tag_id=tag.id, owner_id=corpus.owner_id)
            for name in ("queue", "text_only")
        )
        await db.commit()

    in_the_inbox = await _search(client, q="postgres", status=[ItemStatus.INBOX.value])
    two_statuses = await _search(client, q="postgres", status=["inbox", "archived"])
    tagged = await _search(client, q="postgres", tag="databases")

    assert corpus["pgvector"] not in _ids(in_the_inbox)
    assert corpus["pgvector"] in _ids(two_statuses)
    assert _ids(tagged) == [corpus["queue"], corpus["text_only"]]


async def test_an_unknown_goal_or_tag_finds_nothing(client: httpx.AsyncClient) -> None:
    assert (await _search(client, q="postgres", goal_id=999_999))["items"] == []
    assert (await _search(client, q="postgres", tag="no-such-tag"))["items"] == []


# --- Pages -------------------------------------------------------------------------------


async def test_the_pages_follow_each_other_without_a_gap(
    client: httpx.AsyncClient, corpus: Corpus
) -> None:
    everything = _ids(await _search(client, q="postgres"))

    first = await _search(client, q="postgres", limit=3)
    second = await _search(client, q="postgres", limit=3, cursor=first["next_cursor"])

    assert _ids(first) + _ids(second) == everything
    assert len(_ids(first)) == 3
    assert second["next_cursor"] is None


async def test_a_cursor_belongs_to_its_search(client: httpx.AsyncClient) -> None:
    first = await _search(client, q="postgres", limit=1)

    other_words = await client.get(SEARCH, params={"q": "rust", "cursor": first["next_cursor"]})
    other_filter = await client.get(
        SEARCH, params={"q": "postgres", "status": "inbox", "cursor": first["next_cursor"]}
    )
    broken = await client.get(SEARCH, params={"q": "postgres", "cursor": "not-a-cursor"})

    assert "another search" in problem_body(other_words, 422)["detail"]
    assert "another search" in problem_body(other_filter, 422)["detail"]
    assert problem_body(broken, 422)["detail"] == "the cursor is not valid"


@pytest.mark.parametrize("q", ["", "   "])
async def test_a_search_needs_words(client: httpx.AsyncClient, q: str) -> None:
    problem_body(await client.get(SEARCH, params={"q": q}), 422)
