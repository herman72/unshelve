"""The three retrievers, each one alone, on the made-up corpus. Each test name is one rule."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.search.corpus import QUEUES, SAVED, Corpus, seed
from unshelve.contracts.dto import EmbeddingSpec
from unshelve.db.models import Item, ItemContent
from unshelve.search import retrievers


@pytest.fixture
async def corpus(db: AsyncSession) -> Corpus:
    return await seed(db)


async def _full_text(db: AsyncSession, corpus: Corpus, query: str) -> list[int]:
    return await retrievers.full_text(db, corpus.owner_id, query)


async def _trigram(db: AsyncSession, corpus: Corpus, query: str) -> list[int]:
    return await retrievers.trigram(db, corpus.owner_id, query)


# --- Full text ---------------------------------------------------------------------------


async def test_full_text_finds_another_form_of_an_english_word(
    db: AsyncSession, corpus: Corpus
) -> None:
    # "queues" and "queue" have one stem. No merged copy and no item of another owner.
    assert await _full_text(db, corpus, "queues") == [corpus["queue"]]


async def test_full_text_ranks_the_title_above_the_summary_and_the_summary_above_the_text(
    db: AsyncSession, corpus: Corpus
) -> None:
    # "Postgres": in the title of one item, in the summary of another, in the text of a third.
    assert await _full_text(db, corpus, "postgres") == [
        corpus["queue"],
        corpus["pgvector"],
        corpus["text_only"],
    ]


async def test_full_text_reads_the_fetched_text(db: AsyncSession, corpus: Corpus) -> None:
    assert await _full_text(db, corpus, "multiversion concurrency") == [corpus["text_only"]]


async def test_full_text_meets_the_persian_spellings_of_another_keyboard(
    db: AsyncSession, corpus: Corpus
) -> None:
    # The title has Arabic yeh and kaf; the query has the Persian letters.
    assert await _full_text(db, corpus, "یادگیری ماشین") == [corpus["persian_ml"]]
    # The title has zero-width non-joiners; the query has spaces.
    assert await _full_text(db, corpus, "کتاب های") == [corpus["persian_ml"]]
    # The other way round: the query has the Arabic yeh, the title the Persian one.
    assert await _full_text(db, corpus, "پايگاه داده") == [corpus["persian_db"]]


async def test_full_text_orders_equal_cards_by_their_texts(
    db: AsyncSession, corpus: Corpus
) -> None:
    async def note(text: str, title: str = "A note") -> int:
        item = Item(
            owner_id=corpus.owner_id,
            title=title,
            first_captured_at=SAVED,
            last_captured_at=SAVED,
        )
        db.add(item)
        await db.flush()
        db.add(
            ItemContent(
                item_id=item.id,
                owner_id=corpus.owner_id,
                text=text,
                content_hash=f"note-{item.id}",
                extractor="test",
                extractor_version=1,
            )
        )
        await db.flush()
        return item.id

    # Found only in the text: the text that says more comes first.
    once = await note("Replication is mentioned here one time, among many other words.")
    often = await note("Replication: logical replication, physical replication, replication slots.")
    # The same title: the text decides between them, but a title beats any text.
    plain = await note("No more words here.", title="Replication in practice")
    rich = await note(
        "Replication slots and replication lag, explained.", title="Replication in practice"
    )

    assert await _full_text(db, corpus, "replication") == [rich, plain, often, once]


async def test_full_text_understands_quotes_and_a_minus(db: AsyncSession, corpus: Corpus) -> None:
    assert await _full_text(db, corpus, '"job queue"') == [corpus["queue"]]
    assert await _full_text(db, corpus, "postgres -vector") == [
        corpus["queue"],
        corpus["text_only"],
    ]


async def test_full_text_finds_nothing_for_stop_words_or_unknown_words(
    db: AsyncSession, corpus: Corpus
) -> None:
    assert await _full_text(db, corpus, "the of and") == []
    assert await _full_text(db, corpus, "kubernetes") == []


# --- Trigram -----------------------------------------------------------------------------


async def test_trigram_finds_a_title_with_a_typo(db: AsyncSession, corpus: Corpus) -> None:
    # Full text knows no "postgress". No merged copy and no item of another owner.
    assert await _full_text(db, corpus, "postgress") == []
    assert await _trigram(db, corpus, "postgress") == [corpus["queue"]]


async def test_trigram_finds_a_title_by_a_part_of_a_word(db: AsyncSession, corpus: Corpus) -> None:
    assert await _trigram(db, corpus, "pgvec") == [corpus["pgvector"]]


async def test_trigram_meets_a_persian_word_with_and_without_the_non_joiner(
    db: AsyncSession, corpus: Corpus
) -> None:
    # Joined in the query: the same word in one title, the split word in the other.
    assert await _trigram(db, corpus, "کتابهای") == [corpus["persian_db"], corpus["persian_ml"]]
    # With a zero-width non-joiner in the query: the other way round.
    assert await _trigram(db, corpus, "کتاب‌های") == [corpus["persian_ml"], corpus["persian_db"]]
    # Full text sees two different words, and finds only the title with the same form.
    assert await _full_text(db, corpus, "کتابهای") == [corpus["persian_db"]]


async def test_trigram_meets_the_persian_spellings_of_another_keyboard(
    db: AsyncSession, corpus: Corpus
) -> None:
    # The query has the Arabic yeh, the title the Persian one.
    assert await _trigram(db, corpus, "پايگاه") == [corpus["persian_db"]]


async def test_trigram_does_not_take_a_part_of_a_longer_word(
    db: AsyncSession, corpus: Corpus
) -> None:
    # "search" is a part of "research", and the strict word similarity is 0.45: no match.
    assert await _trigram(db, corpus, "search") == []


async def test_trigram_reads_only_the_titles(db: AsyncSession, corpus: Corpus) -> None:
    assert await _trigram(db, corpus, "lifetimes") == []  # a word of a summary


# --- Vector ------------------------------------------------------------------------------


async def test_vector_ranks_the_items_by_their_closest_vector(
    db: AsyncSession, corpus: Corpus
) -> None:
    found = await retrievers.vector(db, corpus.owner_id, corpus.set_id, QUEUES)

    # pgvector comes second: its card is the farthest vector, but one piece of its text is
    # close, and the closest piece counts. Then Rust, far away: the nearest come back also
    # when they are not near.
    assert found == [corpus["queue"], corpus["pgvector"], corpus["rust"]]


async def test_vector_reads_only_the_given_set_and_the_items_of_the_owner(
    db: AsyncSession, corpus: Corpus
) -> None:
    active = await retrievers.vector(db, corpus.owner_id, corpus.set_id, QUEUES)
    old = await retrievers.vector(db, corpus.owner_id, corpus.other_set_id, QUEUES)

    for excluded in ("copy", "stranger", "bread"):
        assert corpus[excluded] not in active
    assert old == [corpus["bread"]]


async def test_vector_ignores_vectors_of_another_length(db: AsyncSession, corpus: Corpus) -> None:
    # pgvector refuses to compare vectors of different lengths; no vector of length 2 exists.
    assert await retrievers.vector(db, corpus.owner_id, corpus.set_id, (1.0, 0.0)) == []


async def test_each_retriever_returns_at_most_its_limit(db: AsyncSession, corpus: Corpus) -> None:
    owner_id = corpus.owner_id

    assert len(await retrievers.vector(db, owner_id, corpus.set_id, QUEUES, limit=2)) == 2
    assert len(await retrievers.full_text(db, owner_id, "postgres", limit=2)) == 2
    assert len(await retrievers.trigram(db, owner_id, "کتابهای", limit=1)) == 1


async def test_only_the_active_set_of_the_model_is_searchable(
    db: AsyncSession, corpus: Corpus
) -> None:
    active = EmbeddingSpec(provider="test", model="topics", dim=3)
    old = EmbeddingSpec(provider="test", model="old-topics", dim=3)
    unknown = EmbeddingSpec(provider="test", model="never-used", dim=3)

    assert await retrievers.searchable_set(db, active) == corpus.set_id
    assert await retrievers.searchable_set(db, old) is None
    assert await retrievers.searchable_set(db, unknown) is None
