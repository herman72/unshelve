"""The three retrievers, each one alone, on the made-up corpus. Each test name is one rule."""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from tests.search.corpus import QUEUES, SAVED, Corpus, seed
from unshelve.contracts.dto import EmbeddingSpec
from unshelve.contracts.enums import GoalLabel, ItemKind, ItemStatus, LabelSource
from unshelve.db.models import Goal, Item, ItemContent, ItemGoal, ItemTag, Tag
from unshelve.resurface.selection import HELPS_FROM, effective_score
from unshelve.search import retrievers
from unshelve.search.retrievers import NO_FILTERS, Filters


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


# --- Filters -------------------------------------------------------------------------------

INBOX_ONLY = Filters(statuses=frozenset({ItemStatus.INBOX}))


async def _change(db: AsyncSession, item_id: int, **values: object) -> None:
    await db.execute(sa.update(Item).where(Item.id == item_id).values(**values))


async def _postgres(db: AsyncSession, corpus: Corpus, filters: Filters) -> list[int]:
    return await retrievers.full_text(db, corpus.owner_id, "postgres", filters=filters)


async def _three_lists(db: AsyncSession, corpus: Corpus, filters: Filters) -> list[list[int]]:
    """One list of each retriever, each one with a query that finds two items or more."""
    return [
        await retrievers.full_text(db, corpus.owner_id, "postgres", filters=filters),
        await retrievers.trigram(db, corpus.owner_id, "کتابهای", filters=filters),
        await retrievers.vector(db, corpus.owner_id, corpus.set_id, QUEUES, filters=filters),
    ]


async def test_each_retriever_keeps_only_the_items_that_pass_the_filters(
    db: AsyncSession, corpus: Corpus
) -> None:
    everything = await _three_lists(db, corpus, NO_FILTERS)
    archived = {corpus["pgvector"], corpus["persian_ml"]}
    for item_id in archived:
        await _change(db, item_id, status=ItemStatus.ARCHIVED)

    filtered = await _three_lists(db, corpus, INBOX_ONLY)

    # Each list had one of the items that are archived now, and only that one goes.
    assert all(set(found) & archived for found in everything)
    assert filtered == [[item for item in found if item not in archived] for found in everything]


async def test_a_filter_acts_before_the_limit(db: AsyncSession, corpus: Corpus) -> None:
    # The best match is archived. A filter after the limit of 1 would leave an empty list.
    await _change(db, corpus["queue"], status=ItemStatus.ARCHIVED)

    found = await retrievers.full_text(db, corpus.owner_id, "postgres", filters=INBOX_ONLY, limit=1)

    assert found == [corpus["pgvector"]]


async def test_kinds_and_statuses_combine(db: AsyncSession, corpus: Corpus) -> None:
    await _change(db, corpus["queue"], kind=ItemKind.ARTICLE)
    await _change(db, corpus["pgvector"], kind=ItemKind.ARTICLE, status=ItemStatus.ARCHIVED)
    await _change(db, corpus["text_only"], kind=ItemKind.NOTE)
    articles = Filters(kinds=frozenset({ItemKind.ARTICLE}))
    notes_and_articles_of_the_inbox = Filters(
        kinds=frozenset({ItemKind.ARTICLE, ItemKind.NOTE}), statuses=frozenset({ItemStatus.INBOX})
    )

    assert await _postgres(db, corpus, articles) == [corpus["queue"], corpus["pgvector"]]
    assert await _postgres(db, corpus, notes_and_articles_of_the_inbox) == [
        corpus["queue"],
        corpus["text_only"],
    ]


READ = datetime(2026, 10, 18, 19, 0, tzinfo=UTC)


async def test_unread_only_keeps_the_items_without_a_read_time(
    db: AsyncSession, corpus: Corpus
) -> None:
    everything = await _three_lists(db, corpus, NO_FILTERS)
    read = {corpus["pgvector"], corpus["persian_ml"]}
    for item_id in read:
        await _change(db, item_id, status=ItemStatus.DONE, read_at=READ)

    unread = await _three_lists(db, corpus, Filters(unread=True))

    # Each list had one of the items that are read now, and only that one goes.
    assert all(set(found) & read for found in everything)
    assert unread == [[item for item in found if item not in read] for found in everything]


async def test_unread_only_reads_the_read_time_and_not_the_status(
    db: AsyncSession, corpus: Corpus
) -> None:
    # You read the job queue, then queued it to read it again: it keeps its read time. And you
    # archived pgvector without reading it.
    await _change(db, corpus["queue"], status=ItemStatus.QUEUED, read_at=READ)
    await _change(db, corpus["pgvector"], status=ItemStatus.ARCHIVED)

    everything = await _postgres(db, corpus, NO_FILTERS)
    unread = await _postgres(db, corpus, Filters(unread=True))

    assert everything == [corpus["queue"], corpus["pgvector"], corpus["text_only"]]
    assert unread == [corpus["pgvector"], corpus["text_only"]]


async def _tag(db: AsyncSession, owner_id: int, slug: str, *item_ids: int) -> None:
    tag = Tag(owner_id=owner_id, slug=slug, name=slug)
    db.add(tag)
    await db.flush()
    db.add_all(ItemTag(item_id=item_id, tag_id=tag.id, owner_id=owner_id) for item_id in item_ids)
    await db.flush()


async def test_two_tags_find_the_items_that_have_both(db: AsyncSession, corpus: Corpus) -> None:
    queue, pgvector, text_only = corpus["queue"], corpus["pgvector"], corpus["text_only"]
    await _tag(db, corpus.owner_id, "databases", queue, pgvector, text_only)
    await _tag(db, corpus.owner_id, "queues", queue, text_only)

    def tags(*slugs: str) -> Filters:
        return Filters(tags=frozenset(slugs))

    assert await _postgres(db, corpus, tags("databases")) == [queue, pgvector, text_only]
    assert await _postgres(db, corpus, tags("databases", "queues")) == [queue, text_only]
    assert await _postgres(db, corpus, tags("databases", "no-such-tag")) == []


async def _goal(db: AsyncSession, owner_id: int, slug: str) -> int:
    goal = Goal(owner_id=owner_id, slug=slug, title=slug)
    db.add(goal)
    await db.flush()
    return goal.id


def _score(
    corpus: Corpus, item_id: int, goal_id: int, score: int, label: GoalLabel | None = None
) -> ItemGoal:
    return ItemGoal(
        item_id=item_id,
        goal_id=goal_id,
        owner_id=corpus.owner_id,
        score=score,
        label=label,
        label_source=None if label is None else LabelSource.WEB,
        scorer_version="test",
        goal_version=1,
    )


@pytest.mark.parametrize(
    ("score", "label", "helps"),
    [
        (3, None, True),
        (2, None, True),
        (1, None, False),
        (0, None, False),
        (3, GoalLabel.CONFIRMED, True),
        (2, GoalLabel.CONFIRMED, True),
        (1, GoalLabel.CONFIRMED, False),
        (0, GoalLabel.CONFIRMED, False),
        # A rejected score turns: "it helps" becomes "it does not", and the other way round.
        (3, GoalLabel.REJECTED, False),
        (2, GoalLabel.REJECTED, False),
        (1, GoalLabel.REJECTED, True),
        (0, GoalLabel.REJECTED, True),
    ],
)
async def test_the_goal_filter_keeps_the_items_that_help_the_goal_after_your_verdict(
    db: AsyncSession, corpus: Corpus, score: int, label: GoalLabel | None, helps: bool
) -> None:
    goal_id = await _goal(db, corpus.owner_id, "learn-postgres")
    db.add(_score(corpus, corpus["queue"], goal_id, score, label))
    await db.flush()

    found = await _postgres(db, corpus, Filters(goal_id=goal_id))

    assert found == ([corpus["queue"]] if helps else [])
    # The same rule as the digest.
    assert helps == (effective_score(score, label) >= HELPS_FROM)


async def test_the_goal_filter_reads_only_the_score_of_that_goal(
    db: AsyncSession, corpus: Corpus
) -> None:
    learn = await _goal(db, corpus.owner_id, "learn-postgres")
    cook = await _goal(db, corpus.owner_id, "bake-bread")
    db.add_all(
        [
            _score(corpus, corpus["queue"], learn, 3),
            _score(corpus, corpus["queue"], cook, 0),
            _score(corpus, corpus["pgvector"], learn, 0),
            _score(corpus, corpus["pgvector"], cook, 3),
        ]
    )
    await db.flush()

    assert await _postgres(db, corpus, Filters(goal_id=learn)) == [corpus["queue"]]
    assert await _postgres(db, corpus, Filters(goal_id=cook)) == [corpus["pgvector"]]
