"""The public feed, read as the publisher role (docs/adr/0004). Each test name is one rule."""

import hashlib
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tests.publish.support import (
    LEASES_NOTE,
    LEASES_RECORD,
    LEASES_SLUG,
    PERSIAN_SLUG,
    PRIVATE,
    seed_world,
)
from unshelve.contracts.enums import Visibility
from unshelve.db.engine import create_session_factory
from unshelve.db.models import Item
from unshelve.publish.feed import FIELDS, PUBLISHER_ROLE, build_feed, publish, read_records
from unshelve.testing.fakes import InMemoryFeedTarget

pytestmark = pytest.mark.db

Sessions = async_sessionmaker[AsyncSession]


async def test_no_private_text_reaches_the_feed(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)

    feed = (await build_feed(committed_db, world.owner_id)).data.decode().lower()

    leaks = [place for place, marker in PRIVATE.items() if marker.lower() in feed]
    assert leaks == []
    # The feed is not empty by mistake: the public text of the owner is in it.
    assert LEASES_NOTE.lower() in feed
    assert "صف کارها در پستگرس" in feed


async def test_the_feed_has_the_public_items_of_its_owner_the_newest_first(
    committed_db: Sessions,
) -> None:
    world = await seed_world(committed_db)

    feed = await build_feed(committed_db, world.owner_id)

    assert [record["id"] for record in json.loads(feed.data)["items"]] == [
        PERSIAN_SLUG,
        LEASES_SLUG,
    ]
    assert feed.items == 2


async def test_a_record_has_the_agreed_fields_in_their_order(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)

    records = json.loads((await build_feed(committed_db, world.owner_id)).data)["items"]

    assert records[1] == LEASES_RECORD
    assert list(records[1]) == list(FIELDS)


async def test_the_feed_is_utf8_json_with_one_field_on_each_line(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)

    data = (await build_feed(committed_db, world.owner_id)).data

    assert data.startswith(b'{\n  "version": 1,\n  "items": [\n    {\n      "id": "queues-fa",')
    assert data.endswith(b"\n}\n")
    # Persian stays readable, with no \u escapes.
    assert "صف کارها در پستگرس".encode() in data
    assert b"\\u" not in data


async def test_the_same_items_give_the_same_bytes(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)

    first = await build_feed(committed_db, world.owner_id)
    second = await build_feed(committed_db, world.owner_id)

    assert first.data == second.data


async def test_an_owner_with_no_public_item_has_an_empty_feed(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)

    feed = await build_feed(committed_db, world.owner_id + world.other_owner_id + 1)

    assert feed.data == b'{\n  "version": 1,\n  "items": []\n}\n'
    assert feed.items == 0


async def test_an_item_that_becomes_private_leaves_the_next_feed(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)
    async with committed_db() as db:
        await db.execute(
            sa.update(Item).where(Item.id == world.leases_id).values(visibility=Visibility.PRIVATE)
        )
        await db.commit()

    feed = await build_feed(committed_db, world.owner_id)

    assert [record["id"] for record in json.loads(feed.data)["items"]] == [PERSIAN_SLUG]


async def test_the_records_are_read_as_the_publisher_role(committed_db: Sessions) -> None:
    world = await seed_world(committed_db)

    async with committed_db() as db, db.begin():
        await read_records(db, world.owner_id)

        # The role holds until the end of the transaction: a private table is out of reach.
        assert await db.scalar(sa.text("SELECT current_user")) == PUBLISHER_ROLE
        with pytest.raises(ProgrammingError, match="permission denied"):
            await db.execute(sa.text("SELECT title FROM items LIMIT 1"))


async def _fail_after_the_read(sessions: Sessions, owner_id: int) -> None:
    async with sessions() as db, db.begin():
        await read_records(db, owner_id)
        raise RuntimeError("a failure after the read")


async def test_the_role_ends_with_its_transaction(private_database_url: str) -> None:
    # One connection only: each session below runs on the connection of the one before.
    engine = create_async_engine(private_database_url, pool_size=1, max_overflow=0)
    sessions = create_session_factory(engine)
    try:
        world = await seed_world(sessions)
        await build_feed(sessions, world.owner_id)
        with pytest.raises(RuntimeError):
            await _fail_after_the_read(sessions, world.owner_id)

        async with sessions() as db:
            assert await db.scalar(sa.text("SELECT current_user")) != PUBLISHER_ROLE
            assert await db.scalar(sa.select(sa.func.count()).select_from(Item)) == 6
    finally:
        await engine.dispose()


async def test_publish_gives_the_feed_to_the_target_and_reports_it(
    committed_db: Sessions,
) -> None:
    world = await seed_world(committed_db)
    target = InMemoryFeedTarget()

    report = await publish(committed_db, world.owner_id, target)
    again = await publish(committed_db, world.owner_id, target)

    [data] = target.feeds
    assert (report.items, report.size, report.published.changed) == (2, len(data), True)
    assert report.sha256 == hashlib.sha256(data).hexdigest()
    assert again.published.changed is False
