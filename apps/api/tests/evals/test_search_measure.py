"""The evaluation of search, on a small golden set whose numbers can be counted by hand. Each
test name is one rule."""

from math import log2
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.enums import ChunkKind
from unshelve.db.models import Chunk, EmbeddingSet, ItemContent
from unshelve.embed.chunks import pieces_of
from unshelve.evals import search
from unshelve.evals.cassette import Cassette, MissingVectorError, Replay
from unshelve.evals.golden import GoldenSet
from unshelve.evals.search import FUSED, QueryResult, Report, Scores
from unshelve.testing.fakes import FakeEmbedder

SMALL = GoldenSet.model_validate(
    {
        "items": [
            {
                "key": "queue",
                "title": "Postgres as a job queue",
                "summary": "How workers share one table of jobs.",
            },
            {
                "key": "vectors",
                "title": "How pgvector stores vectors",
                "summary": "An index for the nearest neighbours.",
            },
            {"key": "bread", "title": "Sourdough at home", "summary": "Flour, water and time."},
        ],
        "queries": [
            # The bread is a judged miss: a grade of 0 is not relevant.
            {
                "q": "postgres",
                "what": "a word of a title",
                "grades": {"queue": 3, "vectors": 1, "bread": 0},
            },
            {"q": "sourdough", "what": "a word of a title", "grades": {"bread": 3}},
            {"q": "postgress", "what": "a typo", "grades": {"queue": 3}},
        ],
    }
)


async def _owner(db: AsyncSession) -> int:
    return await auth.ensure_owner(db, 9000)


async def test_each_list_is_measured_against_the_grades(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    embedder = FakeEmbedder()
    ids = await search.load_items(db, owner_id, SMALL, embedder)

    report = await search.measure(db, embedder, owner_id, SMALL, ids)

    # "postgres": the queue (grade 3) at the place 1, pgvector (grade 1) not at all.
    # "sourdough": the bread at the place 1. "postgress": trigram finds the queue, full text
    # finds nothing.
    postgres = (1 / 2, 3 / (3 + 1 / log2(3)), 1.0)
    found_all, found_nothing = (1.0, 1.0, 1.0), (0.0, 0.0, 0.0)
    by_hand = {
        "full text": [postgres, found_all, found_nothing],
        "trigram": [postgres, found_all, found_all],
    }
    assert list(report.lists) == ["full text", "trigram", "vector", FUSED]
    for name, per_query in by_hand.items():
        found = report.lists[name]
        means = tuple(sum(scores[n] for scores in per_query) / 3 for n in range(3))
        assert (found.recall, found.ndcg, found.mrr) == pytest.approx(means), name
    # The vector list has each item, the nearest first: the relevant ones are in the first 10.
    assert report.lists["vector"].recall == pytest.approx(1.0)
    assert [result.q for result in report.queries] == ["postgres", "sourdough", "postgress"]


async def test_each_item_gets_its_card_and_its_pieces_as_in_the_pipeline(
    db: AsyncSession,
) -> None:
    text = "A long text about queues. " * 80  # about 2,000 characters: more than one piece
    golden = GoldenSet.model_validate(
        {
            "items": [
                {"key": "long", "title": "A long read", "summary": "On queues.", "text": text}
            ],
            "queries": [{"q": "queues", "what": "a word", "grades": {"long": 3}}],
        }
    )
    owner_id = await _owner(db)

    ids = await search.load_items(db, owner_id, golden, FakeEmbedder())

    chunks = (
        await db.scalars(sa.select(Chunk).where(Chunk.item_id == ids["long"]).order_by(Chunk.id))
    ).all()
    pieces = pieces_of(text)
    assert len(pieces) > 1
    assert [(chunk.kind, chunk.ord) for chunk in chunks] == [
        (ChunkKind.CARD, 0),
        *((ChunkKind.BODY, number) for number in range(len(pieces))),
    ]
    assert [(chunk.char_start, chunk.char_end) for chunk in chunks[1:]] == [
        (piece.start, piece.end) for piece in pieces
    ]
    assert chunks[0].text == "A long read\nOn queues."
    assert await db.get(ItemContent, ids["long"]) is not None
    active = await db.scalar(sa.select(EmbeddingSet.id).where(EmbeddingSet.is_active))
    assert {chunk.embedding_set_id for chunk in chunks} == {active}


async def test_the_vectors_of_the_evaluation_are_the_set_that_search_reads(
    db: AsyncSession,
) -> None:
    # Another set is active already, as in a database that was used before.
    other = EmbeddingSet(provider="test", model="older", dim=3, is_active=True)
    db.add(other)
    await db.flush()
    embedder = FakeEmbedder()
    owner_id = await _owner(db)
    ids = await search.load_items(db, owner_id, SMALL, embedder)

    report = await search.measure(db, embedder, owner_id, SMALL, ids)

    active = await db.scalar(sa.select(EmbeddingSet.model).where(EmbeddingSet.is_active))
    assert active == embedder.spec.model
    assert report.lists["vector"].recall == pytest.approx(1.0)


async def test_a_text_that_was_not_recorded_fails_the_evaluation(db: AsyncSession) -> None:
    empty = Replay(Cassette(spec=FakeEmbedder().spec))

    with pytest.raises(MissingVectorError):
        await search.load_items(db, await _owner(db), SMALL, empty)


# --- The baseline, without a database ------------------------------------------------------

SPEC = EmbeddingSpec(provider="test", model="topics", dim=3)


def _report(recall: float, ndcg: float, spec: EmbeddingSpec = SPEC) -> Report:
    scores = Scores(recall=recall, ndcg=ndcg, mrr=0.5)
    result = QueryResult(q="postgres", what="a word", fused=scores)
    return Report(spec=spec, items=3, queries=(result,), lists={FUSED: scores})


def test_the_same_scores_hold_the_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    search.save_baseline(_report(0.8, 0.7), path)

    assert search.compare(_report(0.8, 0.7), path) == []


def test_a_lower_score_fails_the_gate(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    search.save_baseline(_report(0.8, 0.7), path)

    assert search.compare(_report(0.75, 0.7), path) == [
        "recall@10 dropped from 0.800000 to 0.750000"
    ]


def test_a_higher_score_must_be_written_into_the_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    search.save_baseline(_report(0.8, 0.7), path)

    (problem,) = search.compare(_report(0.8, 0.75), path)

    assert problem.startswith("nDCG@10 rose from 0.700000 to 0.750000")
    assert "make eval-baseline" in problem


def test_a_baseline_of_other_vectors_cannot_be_compared(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    search.save_baseline(_report(0.8, 0.7), path)
    other = EmbeddingSpec(provider="voyage", model="voyage-4-large", dim=1024)

    (problem,) = search.compare(_report(0.8, 0.7, other), path)

    assert "test/topics/3" in problem
    assert "voyage/voyage-4-large/1024" in problem


def test_the_table_shows_each_list_and_the_weakest_queries() -> None:
    lines = _report(0.8, 0.7).table().splitlines()

    assert lines[0] == (
        "search on the golden set: 3 items, 1 queries; vectors of test/topics (3 numbers)"
    )
    assert lines[2] == "fused           0.800    0.700  0.500"
    assert lines[-1] == "  0.700  'postgres'  (a word)"
