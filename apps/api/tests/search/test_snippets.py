"""Snippets: why search found an item. Each test name is one rule."""

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tests.search.corpus import SAVED, Corpus, TopicEmbedder, seed
from unshelve.contracts.enums import ChunkKind
from unshelve.db.models import Chunk, Item, ItemContent
from unshelve.search import hybrid
from unshelve.search.snippets import (
    HIT_END,
    HIT_START,
    Part,
    Snippet,
    Source,
    parts_of_headline,
    passage_parts,
    snippets,
)

# --- The parts of a headline, without a database --------------------------------------------


def test_the_words_between_the_marks_are_hits() -> None:
    headline = f"A worker takes a {HIT_START}lease{HIT_END} on a row."

    assert parts_of_headline(headline) == (
        Part("A worker takes a ", hit=False),
        Part("lease", hit=True),
        Part(" on a row.", hit=False),
    )


def test_a_hit_can_open_the_snippet() -> None:
    assert parts_of_headline(f"{HIT_START}Postgres{HIT_END} is a database") == (
        Part("Postgres", hit=True),
        Part(" is a database", hit=False),
    )


def test_line_breaks_and_runs_of_spaces_become_one_space() -> None:
    assert parts_of_headline("  one\n\n two\t three  ") == (Part("one two three", hit=False),)


def test_an_empty_headline_has_no_parts() -> None:
    assert parts_of_headline("") == ()


def test_a_passage_shows_its_first_words_and_says_when_it_goes_on() -> None:
    words = [f"w{n}" for n in range(1, 36)]

    assert passage_parts(" ".join(words)) == (Part(" ".join(words[:30]) + " …", hit=False),)
    assert passage_parts("A short piece.") == (Part("A short piece.", hit=False),)
    assert passage_parts("  ") == ()


# --- Snippets of the corpus -----------------------------------------------------------------


@pytest.fixture
async def corpus(db: AsyncSession) -> Corpus:
    return await seed(db)


async def _snippets(db: AsyncSession, corpus: Corpus, query: str) -> dict[int, Snippet]:
    ranking = await hybrid.search(db, TopicEmbedder(), corpus.owner_id, query)
    return await snippets(
        db,
        corpus.owner_id,
        query,
        ranking.hits,
        set_id=ranking.set_id,
        query_vector=ranking.query_vector,
    )


def _hits(snippet: Snippet) -> list[str]:
    return [part.text for part in snippet.parts if part.hit]


async def test_the_text_shows_the_words_of_the_query(db: AsyncSession, corpus: Corpus) -> None:
    found = await _snippets(db, corpus, "concurrency")

    assert found[corpus["text_only"]] == Snippet(
        Source.TEXT,
        (
            Part("Saturday: read about multiversion ", hit=False),
            Part("concurrency", hit=True),
            # The whole text has 18 words: it fits into one piece of at most 20 words, which
            # ends with a word, not with the full stop.
            Part(
                " control in Postgres. Each row keeps old versions until no transaction needs them",
                hit=False,
            ),
        ),
    )


async def test_another_form_of_the_word_is_marked(db: AsyncSession, corpus: Corpus) -> None:
    # "keep" and "keeps" have one stem: full text found the item, and the mark agrees.
    found = await _snippets(db, corpus, "keep")

    assert _hits(found[corpus["text_only"]]) == ["keeps"]


async def test_the_summary_speaks_when_the_text_has_no_word_of_the_query(
    db: AsyncSession, corpus: Corpus
) -> None:
    # The text of the queue item says "lease", not "skip locked"; its summary says it.
    found = await _snippets(db, corpus, "skip locked")

    assert found[corpus["queue"]].source is Source.SUMMARY
    assert _hits(found[corpus["queue"]]) == ["SKIP", "LOCKED"]


async def test_the_text_comes_before_the_summary(db: AsyncSession, corpus: Corpus) -> None:
    # "worker": in the text of the queue item ("A worker takes"), and in its summary ("workers").
    found = await _snippets(db, corpus, "worker")

    assert found[corpus["queue"]].source is Source.TEXT
    assert _hits(found[corpus["queue"]]) == ["worker", "worker"]


async def test_an_item_found_by_meaning_shows_its_closest_piece_of_text(
    db: AsyncSession, corpus: Corpus
) -> None:
    # "worker" is about queues. No word of it is in pgvector, but a piece of its text is
    # about queues: that piece is closer than its card.
    found = await _snippets(db, corpus, "worker")

    assert found[corpus["pgvector"]] == Snippet(
        Source.PASSAGE, (Part(f"piece 1 of item {corpus['pgvector']}", hit=False),)
    )


async def test_a_passage_comes_from_the_active_set_of_vectors(
    db: AsyncSession, corpus: Corpus
) -> None:
    # After a change of model, an item has pieces in two sets. This piece of the old set points
    # exactly at queues, so it is closer to "worker" than each piece of the active set.
    db.add(
        Chunk(
            owner_id=corpus.owner_id,
            item_id=corpus["pgvector"],
            embedding_set_id=corpus.other_set_id,
            kind=ChunkKind.BODY,
            ord=1,
            text="A piece of the old model.",
            dim=3,
            embedding=[1.0, 0.0, 0.0],
        )
    )
    await db.flush()

    found = await _snippets(db, corpus, "worker")

    assert found[corpus["pgvector"]].parts == (
        Part(f"piece 1 of item {corpus['pgvector']}", hit=False),
    )


async def test_no_snippet_when_the_title_or_the_card_already_says_why(
    db: AsyncSession, corpus: Corpus
) -> None:
    # Rust: found by meaning, and its closest vector is its card. The Persian item: found
    # only by trigram, in its title.
    assert corpus["rust"] not in await _snippets(db, corpus, "worker")
    assert corpus["persian_ml"] not in await _snippets(db, corpus, "کتابهای")


async def test_marks_in_the_saved_text_cannot_make_a_hit(db: AsyncSession, corpus: Corpus) -> None:
    item = Item(
        owner_id=corpus.owner_id,
        title="A trap",
        first_captured_at=SAVED,
        last_captured_at=SAVED,
    )
    db.add(item)
    await db.flush()
    db.add(
        ItemContent(
            item_id=item.id,
            owner_id=corpus.owner_id,
            text=f"This {HIT_START}fake{HIT_END} mark is near the word sourdough.",
            content_hash="trap",
            extractor="test",
            extractor_version=1,
        )
    )
    await db.flush()

    found = await _snippets(db, corpus, "sourdough")

    assert _hits(found[item.id]) == ["sourdough"]
    assert "fake" in "".join(part.text for part in found[item.id].parts)


async def test_a_snippet_needs_an_item_of_the_owner(db: AsyncSession, corpus: Corpus) -> None:
    # The item of a stranger: the word in its summary, and a piece of text that is the closest
    # of its vectors to the query.
    stranger = corpus["stranger"]
    await db.execute(sa.update(Item).where(Item.id == stranger).values(summary="Postgres, always."))
    their_id = await db.scalar(sa.select(Item.owner_id).where(Item.id == stranger))
    assert their_id is not None
    db.add(
        Chunk(
            owner_id=their_id,
            item_id=stranger,
            embedding_set_id=corpus.set_id,
            kind=ChunkKind.BODY,
            ord=1,
            text="A piece about vectors.",
            dim=3,
            # Closer to the query than its card, which points to queues (corpus).
            embedding=[0.0, 1.0, 0.0],
        )
    )
    await db.flush()
    by_words = hybrid.Hit(stranger, 1.0, hybrid.Ranks(full_text=1, trigram=None, vector=None))
    by_meaning = hybrid.Hit(stranger, 1.0, hybrid.Ranks(full_text=None, trigram=None, vector=1))

    async def snippets_for(owner_id: int, hit: hybrid.Hit) -> dict[int, Snippet]:
        return await snippets(
            db, owner_id, "postgres", [hit], set_id=corpus.set_id, query_vector=[0.0, 1.0, 0.0]
        )

    assert await snippets_for(corpus.owner_id, by_words) == {}
    assert await snippets_for(corpus.owner_id, by_meaning) == {}
    # The control: for the stranger, the same calls find both snippets.
    assert (await snippets_for(their_id, by_words))[stranger].source is Source.SUMMARY
    assert (await snippets_for(their_id, by_meaning))[stranger].source is Source.PASSAGE
