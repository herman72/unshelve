"""The rules that the database itself enforces, whatever the application code does."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import (
    Actor,
    CaptureSource,
    ChunkKind,
    ItemStatus,
    JobKind,
    JobState,
    Visibility,
)
from unshelve.db.models import Capture, Chunk, EmbeddingSet, Event, Item, Job, User
from unshelve.testing.fakes import FakeEmbedder

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


async def _owner(db: AsyncSession) -> User:
    user = User(telegram_user_id=1)
    db.add(user)
    await db.flush()
    return user


async def _item(db: AsyncSession, owner: User, **fields: object) -> Item:
    item = Item(owner_id=owner.id, first_captured_at=NOW, last_captured_at=NOW, **fields)
    db.add(item)
    await db.flush()
    return item


async def _insert(db: AsyncSession, rows: tuple[object, ...]) -> None:
    # A savepoint, so a refused insert does not end the test's transaction.
    async with db.begin_nested():
        db.add_all(rows)
        await db.flush()


async def _rejects(db: AsyncSession, *rows: object) -> None:
    """Assert that the database refuses ``rows``."""
    with pytest.raises(IntegrityError):
        await _insert(db, rows)


# --- Text normalisation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "normalised"),
    [
        ("Hello World", "hello world"),
        ("كتاب", "کتاب"),  # Arabic kaf -> Persian kaf
        ("يادگيري", "یادگیری"),  # Arabic yeh -> Persian yeh
        ("می‌خواهم", "می خواهم"),  # zero-width non-joiner -> space
        ("١٢٣ ۴۵۶", "123 456"),  # Arabic-Indic and Persian digits
        ("مُحَمَّد", "محمد"),  # diacritics removed
        ("سلـــام", "سلام"),  # tatweel removed
    ],
)
async def test_u_norm(db: AsyncSession, raw: str, normalised: str) -> None:
    assert await db.scalar(sa.select(sa.func.u_norm(raw))) == normalised


async def test_full_text_search_matches_across_persian_spellings(db: AsyncSession) -> None:
    owner = await _owner(db)
    # Saved with Arabic yeh and kaf and a zero-width non-joiner, as many keyboards type it.
    persian = await _item(db, owner, title="معرفي كتاب‌هاي يادگيري ماشين")
    english = await _item(db, owner, title="Postgres as a job queue")

    async def search(query: str) -> list[int]:
        match = Item.fts.op("@@")(sa.func.websearch_to_tsquery("english", sa.func.u_norm(query)))
        return list(await db.scalars(sa.select(Item.id).where(match)))

    assert await search("یادگیری ماشین") == [persian.id]  # Persian yeh in the query
    assert await search("کتاب های") == [persian.id]
    assert await search("queues") == [english.id]  # English is stemmed


async def test_trigram_similarity_works_on_persian_titles(db: AsyncSession) -> None:
    owner = await _owner(db)
    item = await _item(db, owner, title="یادگیری ماشین برای مهندسان نرم‌افزار")

    # Needs a UTF-8 locale: with lc_ctype=C, pg_trgm sees no letters in Persian text.
    similarity = sa.func.similarity(sa.func.u_norm(Item.title), sa.func.u_norm("يادگيري ماشين"))
    score = await db.scalar(sa.select(similarity).where(Item.id == item.id))

    assert score is not None
    assert score > 0.3


# --- Captures ----------------------------------------------------------------------------


def _capture(owner: User, **fields: object) -> Capture:
    defaults: dict[str, object] = {
        "owner_id": owner.id,
        "source": CaptureSource.BOT,
        "chat_id": -100123,
        "message_id": 7,
        "posted_at": NOW,
    }
    return Capture(**(defaults | fields))


async def test_a_message_is_captured_once(db: AsyncSession) -> None:
    owner = await _owner(db)
    db.add(_capture(owner))
    await db.flush()

    await _rejects(db, _capture(owner, source=CaptureSource.EXPORT))  # same message, other source
    db.add(_capture(owner, edit_seq=1))  # an edit of the message is a new capture
    await db.flush()


# --- Items -------------------------------------------------------------------------------


async def test_an_item_starts_private_in_the_inbox_and_is_read_back_as_enums(
    db: AsyncSession,
) -> None:
    item = await _item(db, await _owner(db))
    await db.refresh(item)

    assert item.status is ItemStatus.INBOX
    assert item.visibility is Visibility.PRIVATE
    assert item.capture_count == 1


async def test_a_public_item_needs_a_slug_and_a_publication_time(db: AsyncSession) -> None:
    owner = await _owner(db)
    await _rejects(
        db,
        Item(
            owner_id=owner.id,
            first_captured_at=NOW,
            last_captured_at=NOW,
            visibility=Visibility.PUBLIC,
        ),
    )
    await _item(db, owner, visibility=Visibility.PUBLIC, public_slug="a1", published_at=NOW)


async def test_a_done_item_needs_a_read_time(db: AsyncSession) -> None:
    owner = await _owner(db)
    await _rejects(
        db,
        Item(
            owner_id=owner.id, first_captured_at=NOW, last_captured_at=NOW, status=ItemStatus.DONE
        ),
    )
    await _item(db, owner, status=ItemStatus.DONE, read_at=NOW)


async def test_an_unknown_status_is_refused_by_the_database(db: AsyncSession) -> None:
    item = await _item(db, await _owner(db))
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                sa.text("UPDATE items SET status = 'snoozed' WHERE id = :id"), {"id": item.id}
            )


# --- Events ------------------------------------------------------------------------------


async def test_events_are_append_only(db: AsyncSession) -> None:
    owner = await _owner(db)
    item = await _item(db, owner)
    db.add(Event(owner_id=owner.id, item_id=item.id, actor=Actor.USER, type="status_changed"))
    await db.flush()

    for statement in ("UPDATE events SET type = 'rewritten'", "DELETE FROM events"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with db.begin_nested():
                await db.execute(sa.text(statement))

    assert await db.scalar(sa.select(sa.func.count()).select_from(Event)) == 1


# --- Vectors -----------------------------------------------------------------------------


async def test_nearest_neighbour_search_and_the_dimension_check(db: AsyncSession) -> None:
    owner = await _owner(db)
    embedder = FakeEmbedder()
    spec = embedder.spec
    embedding_set = EmbeddingSet(
        provider=spec.provider, model=spec.model, dim=spec.dim, is_active=True
    )
    db.add(embedding_set)
    await db.flush()

    texts = ["postgres vector index tuning", "banana bread with walnuts", "telegram bot polling"]
    vectors = await embedder.embed_documents(texts)
    items = [await _item(db, owner, title=text) for text in texts]

    def chunk(item: Item, text: str, vector: list[float], dim: int) -> Chunk:
        return Chunk(
            owner_id=owner.id,
            item_id=item.id,
            embedding_set_id=embedding_set.id,
            kind=ChunkKind.CARD,
            text=text,
            dim=dim,
            embedding=vector,
        )

    db.add_all(chunk(i, t, v, spec.dim) for i, t, v in zip(items, texts, vectors, strict=True))
    await db.flush()

    query = await embedder.embed_query("tuning a vector index in postgres")
    nearest = await db.scalar(
        sa.select(Chunk.item_id).order_by(Chunk.embedding.cosine_distance(query)).limit(1)
    )
    assert nearest == items[0].id

    # The stored dimension must be the real dimension of the vector.
    await _rejects(db, chunk(items[0], "wrong", vectors[0][:8], spec.dim))


async def test_only_one_embedding_set_is_active(db: AsyncSession) -> None:
    db.add(EmbeddingSet(provider="p", model="a", dim=8, is_active=True))
    await db.flush()
    await _rejects(db, EmbeddingSet(provider="p", model="b", dim=8, is_active=True))


# --- Jobs --------------------------------------------------------------------------------


def _job(**fields: object) -> Job:
    return Job(**({"kind": JobKind.FETCH_ITEM, "payload": {"item_id": 1}} | fields))


async def test_a_running_job_has_a_lease_and_no_other_job_does(db: AsyncSession) -> None:
    lease = {"locked_by": "worker-1", "locked_until": NOW + timedelta(minutes=1)}

    await _rejects(db, _job(state=JobState.RUNNING))  # running without a lease
    await _rejects(db, _job(state=JobState.PENDING, **lease))  # a lease without running
    await _rejects(db, _job(state=JobState.RUNNING, locked_by="worker-1"))  # half a lease

    db.add(_job(state=JobState.RUNNING, **lease))
    await db.flush()


async def test_pending_jobs_with_the_same_dedupe_key_are_one_job(db: AsyncSession) -> None:
    first = _job(dedupe_key="item:1")
    db.add(first)
    await db.flush()

    await _rejects(db, _job(dedupe_key="item:1"))

    db.add(_job(dedupe_key="item:2"))  # another key is another job
    db.add(_job(kind=JobKind.ENRICH_ITEM, dedupe_key="item:1"))  # so is another kind
    first.state = JobState.DONE  # once the first has left the queue, the key is free again
    await db.flush()
    db.add(_job(dedupe_key="item:1"))
    await db.flush()
