"""A small made-up corpus for the tests of search. Each item is a case for one retriever.

The vectors have three dimensions, one for each topic: queues, vectors and Rust. A real
embedder makes vectors of hundreds of dimensions; the SQL is the same.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.enums import ChunkKind, EnrichState
from unshelve.db.models import Chunk, EmbeddingSet, Item, ItemContent

SAVED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
QUEUES, VECTORS = (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)
# Rust is a little closer to queues than vectors are: the card of pgvector is the farthest
# vector, so a search that took the farthest piece of an item would rank pgvector last.
RUST = (0.2, 0.0, 1.0)


@dataclass(frozen=True)
class Corpus:
    owner_id: int
    set_id: int  # the active embedding set
    other_set_id: int  # an inactive set of another model
    ids: dict[str, int]

    def __getitem__(self, name: str) -> int:
        return self.ids[name]


async def _item(
    db: AsyncSession,
    owner_id: int,
    title: str | None,
    summary: str | None = None,
    *,
    text: str | None = None,
    **values: object,
) -> int:
    item = Item(
        owner_id=owner_id,
        title=title,
        summary=summary,
        enrich_state=EnrichState.READY,
        first_captured_at=SAVED,
        last_captured_at=SAVED,
        **values,
    )
    db.add(item)
    await db.flush()
    if text is not None:
        db.add(
            ItemContent(
                item_id=item.id,
                owner_id=owner_id,
                text=text,
                content_hash=f"hash-{item.id}",
                extractor="test",
                extractor_version=1,
            )
        )
    return item.id


def _chunk(
    owner_id: int, item_id: int, set_id: int, vector: tuple[float, ...], ord_: int = 0
) -> Chunk:
    return Chunk(
        owner_id=owner_id,
        item_id=item_id,
        embedding_set_id=set_id,
        kind=ChunkKind.CARD if ord_ == 0 else ChunkKind.BODY,
        ord=ord_,
        text=f"piece {ord_} of item {item_id}",
        dim=len(vector),
        embedding=list(vector),
    )


async def seed(db: AsyncSession) -> Corpus:
    owner_id = await auth.ensure_owner(db, 1)
    stranger = await auth.ensure_owner(db, 2)
    ids = {
        "queue": await _item(
            db,
            owner_id,
            "Postgres as a job queue",
            "How SKIP LOCKED lets workers share one table of jobs.",
            text="A worker takes a lease on a row. When the worker stops, the lease ends.",
            site="example.com",
        ),
        "pgvector": await _item(
            db,
            owner_id,
            "How pgvector stores vectors",
            "Vector search inside Postgres, with an index for the nearest neighbours.",
        ),
        "rust": await _item(
            db, owner_id, "Ownership in Rust explained", "Borrowing and lifetimes, step by step."
        ),
        # Typed on a keyboard with Arabic letters, and with zero-width non-joiners.
        "persian_ml": await _item(
            db, owner_id, "معرفي كتاب‌هاي يادگيري ماشين", "فهرستی از کتاب‌های خوب برای شروع."
        ),
        # The same plural without the zero-width non-joiner.
        "persian_db": await _item(db, owner_id, "کتابهای پایگاه داده"),
        "research": await _item(db, owner_id, "A research agenda for retrieval"),
        "bread": await _item(db, owner_id, "Sourdough at home", "Flour, water, salt and time."),
        # Only the fetched text says what it is about.
        "text_only": await _item(
            db,
            owner_id,
            "Weekend notes",
            text=(
                "Saturday: read about multiversion concurrency control in Postgres. Each row "
                "keeps old versions until no transaction needs them."
            ),
        ),
    }
    ids["copy"] = await _item(
        db, owner_id, "Postgres as a job queue (saved again)", duplicate_of_id=ids["queue"]
    )
    ids["stranger"] = await _item(db, stranger, "Postgres as a job queue")

    active = EmbeddingSet(provider="test", model="topics", dim=3, is_active=True)
    other = EmbeddingSet(provider="test", model="old-topics", dim=3, is_active=False)
    db.add_all([active, other])
    await db.flush()
    db.add_all(
        [
            _chunk(owner_id, ids["queue"], active.id, QUEUES),
            _chunk(owner_id, ids["pgvector"], active.id, VECTORS),
            # A piece of the text of pgvector is about queues too.
            _chunk(owner_id, ids["pgvector"], active.id, (0.9, 0.1, 0.0), ord_=1),
            _chunk(owner_id, ids["rust"], active.id, RUST),
            _chunk(owner_id, ids["copy"], active.id, QUEUES),
            _chunk(stranger, ids["stranger"], active.id, QUEUES),
            # The bread has a vector only in the old set: search does not read it.
            _chunk(owner_id, ids["bread"], other.id, QUEUES),
        ]
    )
    await db.flush()
    return Corpus(owner_id=owner_id, set_id=active.id, other_set_id=other.id, ids=ids)
