"""The public feed of an owner, read as the publisher role (docs/adr/0004, docs/learn/42).

The role ``unshelve_publisher`` can read the view ``pub.items`` and nothing else. The feed is
read inside one transaction that has switched to that role, so a query here cannot reach a
private row or a private column, also by mistake: the database refuses it.

Two lists decide what is public. The view decides what CAN be public: only public items that
are no copy, only their public columns, only their public tags. ``FIELDS`` here decides what
IS published. A new column of the view stays out of the feed until it is added here too.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.dto import Published
from unshelve.contracts.protocols import FeedTarget

PUBLISHER_ROLE = "unshelve_publisher"
# The shape of the feed. A site that reads it can refuse a version that it does not know.
FEED_VERSION = 1

# The fields of a public record, in their order in the feed. ``owner_id`` is in the view for
# the filter below; it is an internal number, so it is not in the feed.
FIELDS = (
    "id",
    "kind",
    "title",
    "url",
    "author",
    "site",
    "lang",
    "summary",
    "key_points",
    "est_minutes",
    "note",
    "published_at",
    "tags",
)

# The view as the query sees it (migration 0001). tests/db/test_public_boundary.py pins its columns.
_PUB_ITEMS = sa.table(
    "items",
    sa.column("owner_id", sa.BigInteger),
    sa.column("id", sa.Text),
    sa.column("kind", sa.Text),
    sa.column("title", sa.Text),
    sa.column("url", sa.Text),
    sa.column("author", sa.Text),
    sa.column("site", sa.Text),
    sa.column("lang", sa.Text),
    sa.column("summary", sa.Text),
    sa.column("key_points", JSONB),
    sa.column("est_minutes", sa.Integer),
    sa.column("note", sa.Text),
    sa.column("published_at", sa.DateTime(timezone=True)),
    sa.column("tags", JSONB),
    schema="pub",
)

type Record = dict[str, object]


async def read_records(session: AsyncSession, owner_id: int) -> list[Record]:
    """The public records of ``owner_id``, the newest first.

    It switches the session to the publisher role until its transaction ends (``SET LOCAL
    ROLE``). So call it last in a transaction, and end the transaction right after it:
    ``build_feed`` does both.
    """
    await session.execute(sa.text(f"SET LOCAL ROLE {PUBLISHER_ROLE}"))
    # The view holds the public items of every owner: this filter keeps the ones of one.
    query = (
        sa.select(*(_PUB_ITEMS.c[name] for name in FIELDS))
        .where(_PUB_ITEMS.c.owner_id == owner_id)
        .order_by(_PUB_ITEMS.c.published_at.desc(), _PUB_ITEMS.c.id)
    )
    rows = await session.execute(query)
    return [_record(row) for row in rows.mappings()]


def _record(row: RowMapping) -> Record:
    record: Record = {name: row[name] for name in FIELDS}
    published: datetime = row["published_at"]  # never NULL: a public item has one
    # One form for each time: UTC, to the second. The same item always gives the same text.
    record["published_at"] = published.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return record


def feed_bytes(records: Sequence[Record]) -> bytes:
    """The feed as UTF-8 JSON, one field on each line, so that a change is a small diff.

    The same records always give the same bytes, so a target can see that nothing changed.
    Persian text stays readable: no ``\\u`` escapes.
    """
    feed = {"version": FEED_VERSION, "items": list(records)}
    return (json.dumps(feed, ensure_ascii=False, indent=2) + "\n").encode()


@dataclass(frozen=True, slots=True)
class Feed:
    data: bytes
    items: int


async def build_feed(sessions: async_sessionmaker[AsyncSession], owner_id: int) -> Feed:
    """The feed of ``owner_id``, read in a transaction of its own: the role ends with it."""
    async with sessions() as session, session.begin():
        records = await read_records(session, owner_id)
    return Feed(feed_bytes(records), len(records))


@dataclass(frozen=True, slots=True)
class Report:
    items: int
    size: int  # bytes
    sha256: str
    published: Published


async def publish(
    sessions: async_sessionmaker[AsyncSession], owner_id: int, target: FeedTarget
) -> Report:
    """Build the feed of ``owner_id`` and give it to ``target``.

    The transaction ends before the target is called: a slow disk or network never holds a
    connection to the database. Raises ``PublishError``.
    """
    feed = await build_feed(sessions, owner_id)
    published = await target.publish(feed.data)
    return Report(
        items=feed.items,
        size=len(feed.data),
        sha256=hashlib.sha256(feed.data).hexdigest(),
        published=published,
    )
