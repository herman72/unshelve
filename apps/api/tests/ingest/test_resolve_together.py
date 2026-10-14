"""Two workers that resolve the same thing at the same moment. Real commits, real connections."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import CaptureSource
from unshelve.db.models import Capture, Item, ItemKey, Job
from unshelve.ingest import resolve

Sessions = async_sessionmaker[AsyncSession]

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
URL = "https://example.com/how-pgvector-works"


async def _stored(sessions: Sessions, *addresses: str) -> list[int]:
    """Store one message for each address. Returns the ids of the captures."""
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        ids: list[int] = []
        for number, address in enumerate(addresses, start=1):
            envelope = CaptureEnvelope(
                source=CaptureSource.BOT,
                chat_id=-1001234567890,
                message_id=number,
                posted_at=NOON,
                text=address,
                urls=[address],
            )
            capture_id = await resolve.store_capture(db, owner_id, envelope)
            assert capture_id is not None
            ids.append(capture_id)
        await db.commit()
    return ids


async def _resolved(sessions: Sessions, capture_id: int) -> int:
    """What one worker does: a transaction of its own, on a connection of its own."""
    async with sessions() as db:
        item_id = await resolve.resolve_capture(db, capture_id)
        await db.commit()
    assert item_id is not None
    return item_id


async def _items(sessions: Sessions) -> list[Item]:
    async with sessions() as db:
        return list((await db.execute(sa.select(Item).order_by(Item.id))).scalars().all())


async def _count(sessions: Sessions, table: type[object]) -> int:
    async with sessions() as db:
        return (await db.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()


async def test_two_workers_with_the_same_link_at_the_same_moment_make_one_item(
    committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = await _stored(committed_db, URL, URL + "?utm_source=telegram")
    look = resolve._items_with  # pyright: ignore[reportPrivateUsage]
    the_first_worker_looked = asyncio.Event()
    looks = 0

    async def look_and_then_be_slow(
        session: AsyncSession, owner_id: int, keys: Sequence[str]
    ) -> set[int]:
        nonlocal looks
        looks += 1
        slow = looks == 1
        found = await look(session, owner_id, keys)
        if slow:
            # The first worker found no item. Now it is slow, before it makes one. The second
            # worker gets all this time to look too, if nothing makes it wait.
            the_first_worker_looked.set()
            await asyncio.sleep(0.3)
        return found

    monkeypatch.setattr(resolve, "_items_with", look_and_then_be_slow)

    async def the_second_worker() -> int:
        await the_first_worker_looked.wait()
        return await _resolved(committed_db, second)

    one, other = await asyncio.gather(_resolved(committed_db, first), the_second_worker())

    assert one == other
    (item,) = await _items(committed_db)
    assert item.capture_count == 2
    assert await _count(committed_db, ItemKey) == 1
    assert await _count(committed_db, Job) == 1


async def test_twenty_saves_of_one_link_at_the_same_moment_are_one_item(
    committed_db: Sessions,
) -> None:
    forms = [
        URL,
        URL + "/",
        URL + "#top",
        URL + "?utm_source=telegram",
        URL.replace("https://", "http://www."),
    ]
    captures = await _stored(committed_db, *(forms * 4))

    item_ids = await asyncio.gather(*(_resolved(committed_db, c) for c in captures))

    assert len(set(item_ids)) == 1
    (item,) = await _items(committed_db)
    assert item.capture_count == 20
    assert item.canonical_url == URL
    assert await _count(committed_db, Job) == 1
    async with committed_db() as db:
        linked = (await db.execute(sa.select(Capture.item_id))).scalars().all()
    assert set(linked) == {item.id}


async def test_two_workers_with_two_different_links_do_not_wait_for_each_other(
    committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = await _stored(committed_db, URL, "https://example.com/another-page")
    look = resolve._items_with  # pyright: ignore[reportPrivateUsage]
    the_first_worker_looked = asyncio.Event()
    the_second_worker_is_done = asyncio.Event()
    looks = 0

    async def look_and_then_wait_for_the_other(
        session: AsyncSession, owner_id: int, keys: Sequence[str]
    ) -> set[int]:
        nonlocal looks
        looks += 1
        waits = looks == 1
        found = await look(session, owner_id, keys)
        if waits:
            # The first worker holds the lock of its link, and it does not go on before the
            # second worker has finished. If one lock stopped all workers, this would never end.
            the_first_worker_looked.set()
            await asyncio.wait_for(the_second_worker_is_done.wait(), timeout=5)
        return found

    monkeypatch.setattr(resolve, "_items_with", look_and_then_wait_for_the_other)

    async def the_second_worker() -> int:
        await the_first_worker_looked.wait()
        item_id = await _resolved(committed_db, second)
        the_second_worker_is_done.set()
        return item_id

    one, other = await asyncio.gather(_resolved(committed_db, first), the_second_worker())

    assert one != other
    assert len(await _items(committed_db)) == 2


async def test_rekey_waits_for_a_worker_that_works_on_the_same_key(committed_db: Sessions) -> None:
    (capture_id,) = await _stored(committed_db, URL)
    item_id = await _resolved(committed_db, capture_id)
    async with committed_db() as db:
        await db.execute(sa.delete(ItemKey))
        await db.commit()

    async def rekey() -> int:
        async with committed_db() as db:
            added = await resolve.rekey_item(db, item_id)
            await db.commit()
        return added

    async with committed_db() as worker:
        # A worker holds the lock of the key: it is in the middle of a save of this link.
        await resolve._wait_for_keys(worker, 1, [f"url:{URL}"])  # pyright: ignore[reportPrivateUsage]
        waiting = asyncio.create_task(rekey())
        await asyncio.sleep(0.3)
        assert not waiting.done()
        await worker.commit()

    assert await waiting == 1
