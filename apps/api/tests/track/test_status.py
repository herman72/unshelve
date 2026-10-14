"""The move to "done". Each test name is one rule."""

import asyncio
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.contracts.enums import Actor, ItemStatus
from unshelve.db.models import Event, Item
from unshelve.track import status as status_module
from unshelve.track.status import Outcome, mark_done

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)


async def _item(db: AsyncSession, owner_id: int, status: ItemStatus = ItemStatus.INBOX) -> int:
    values = {
        "owner_id": owner_id,
        "status": status,
        "first_captured_at": NOON,
        "last_captured_at": NOON,
    }
    if status is ItemStatus.DONE:
        values["read_at"] = NOON
    return (await db.execute(sa.insert(Item).values(**values).returning(Item.id))).scalar_one()


async def _events(db: AsyncSession) -> list[Event]:
    return list((await db.execute(sa.select(Event).order_by(Event.id))).scalars().all())


async def test_an_item_in_the_inbox_becomes_done_and_the_change_is_in_the_log(
    db: AsyncSession,
) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id)

    change = await mark_done(db, owner_id, item_id, actor=Actor.USER, via="telegram", now=LATER)

    assert (change.outcome, change.status) == (Outcome.CHANGED, ItemStatus.DONE)
    item = await db.get_one(Item, item_id)
    assert (item.status, item.read_at) == (ItemStatus.DONE, LATER)
    (event,) = await _events(db)
    assert (event.item_id, event.actor, event.type) == (item_id, Actor.USER, "status")
    assert (event.from_status, event.to_status) == ("inbox", "done")
    assert event.payload == {"via": "telegram"}


async def test_a_second_press_changes_nothing_and_writes_no_second_event(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id)
    await mark_done(db, owner_id, item_id, actor=Actor.USER, via="telegram", now=LATER)

    again = await mark_done(db, owner_id, item_id, actor=Actor.USER, via="telegram")

    assert (again.outcome, again.status) == (Outcome.ALREADY, ItemStatus.DONE)
    assert len(await _events(db)) == 1
    assert (await db.get_one(Item, item_id)).read_at == LATER


async def test_queued_and_reading_items_can_become_done(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    queued, reading = (
        await _item(db, owner_id, ItemStatus.QUEUED),
        await _item(db, owner_id, ItemStatus.READING),
    )

    for item_id in (queued, reading):
        change = await mark_done(db, owner_id, item_id, actor=Actor.USER, via="telegram")
        assert change.outcome is Outcome.CHANGED


async def test_an_archived_item_cannot_become_done(db: AsyncSession) -> None:
    # The table of moves: an archived item goes back to the inbox first.
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id, ItemStatus.ARCHIVED)

    change = await mark_done(db, owner_id, item_id, actor=Actor.USER, via="telegram")

    assert (change.outcome, change.status) == (Outcome.NOT_ALLOWED, ItemStatus.ARCHIVED)
    assert (await db.get_one(Item, item_id)).status is ItemStatus.ARCHIVED
    assert await _events(db) == []


async def test_the_item_of_a_different_owner_is_not_found(db: AsyncSession) -> None:
    mine = await auth.ensure_owner(db, 1)
    theirs = await auth.ensure_owner(db, 2)
    their_item = await _item(db, theirs)

    change = await mark_done(db, mine, their_item, actor=Actor.USER, via="telegram")

    assert (change.outcome, change.status) == (Outcome.NOT_FOUND, None)
    assert (await db.get_one(Item, their_item)).status is ItemStatus.INBOX


async def test_an_item_that_does_not_exist_is_not_found(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    change = await mark_done(db, owner_id, 999_999, actor=Actor.USER, via="telegram")

    assert change.outcome is Outcome.NOT_FOUND


async def test_two_presses_at_the_same_moment_write_one_event(
    committed_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, 1)
        item_id = await _item(db, owner_id)
        await db.commit()
    check = status_module.can_change_status
    the_first_press_looked = asyncio.Event()
    looks = 0

    def look_and_remember(current: ItemStatus, target: ItemStatus) -> bool:
        nonlocal looks
        looks += 1
        if looks == 1:
            the_first_press_looked.set()
        return check(current, target)

    monkeypatch.setattr(status_module, "can_change_status", look_and_remember)

    async def press(wait: bool) -> Outcome:
        if wait:
            await the_first_press_looked.wait()
        async with committed_db() as db:
            change = await mark_done(db, owner_id, item_id, actor=Actor.USER, via="telegram")
            if not wait:
                # The first press is slow before it saves. Without the lock of the row, the
                # second press reads "inbox" in this time and writes a second event.
                await asyncio.sleep(0.3)
            await db.commit()
        return change.outcome

    first, second = await asyncio.gather(press(wait=False), press(wait=True))

    assert (first, second) == (Outcome.CHANGED, Outcome.ALREADY)
    async with committed_db() as db:
        assert len((await db.execute(sa.select(Event))).scalars().all()) == 1
