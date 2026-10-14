"""What a digest shows. Each test name is one rule."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.enums import ItemStatus
from unshelve.db.models import Item
from unshelve.resurface.digest import choose_one, pick

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)


async def _item(
    db: AsyncSession,
    owner_id: int,
    days_ago: float,
    status: ItemStatus = ItemStatus.INBOX,
    **values: object,
) -> int:
    at = NOW - timedelta(days=days_ago)
    fields: dict[str, object] = {
        "owner_id": owner_id,
        "status": status,
        "first_captured_at": at,
        "last_captured_at": at,
    }
    if status is ItemStatus.DONE:
        fields["read_at"] = at
    return (
        await db.execute(sa.insert(Item).values(**(fields | values)).returning(Item.id))
    ).scalar_one()


def _ids(items: list[Item]) -> list[int]:
    return [item.id for item in items]


async def test_the_things_of_the_last_seven_days_come_newest_first(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    old = await _item(db, owner_id, 6.9)
    new = await _item(db, owner_id, 0.5)
    middle = await _item(db, owner_id, 3)
    await _item(db, owner_id, 7.1)  # a little too old for "recent"

    found = await pick(db, owner_id, now=NOW, seed="a")

    assert _ids(found.recent) == [new, middle, old]


async def test_at_most_five_recent_things_are_shown(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    ids = [await _item(db, owner_id, days) for days in (1, 2, 3, 4, 5, 6)]

    found = await pick(db, owner_id, now=NOW, seed="a")

    assert _ids(found.recent) == ids[:5]


async def test_done_archived_and_duplicate_items_are_not_shown(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    shown = await _item(db, owner_id, 1)
    await _item(db, owner_id, 1, ItemStatus.DONE)
    await _item(db, owner_id, 1, ItemStatus.ARCHIVED)
    await _item(db, owner_id, 1, duplicate_of_id=shown)
    await _item(db, owner_id, 30, ItemStatus.DONE)
    await _item(db, owner_id, 30, ItemStatus.ARCHIVED)

    found = await pick(db, owner_id, now=NOW, seed="a")

    assert _ids(found.recent) == [shown]
    assert found.from_the_shelf is None


async def test_queued_and_reading_items_count_as_recent(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    queued = await _item(db, owner_id, 1, ItemStatus.QUEUED)
    reading = await _item(db, owner_id, 2, ItemStatus.READING)

    found = await pick(db, owner_id, now=NOW, seed="a")

    assert _ids(found.recent) == [queued, reading]


async def test_one_older_thing_of_the_inbox_comes_from_the_shelf(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    shelf = {await _item(db, owner_id, days) for days in (10, 40, 200)}
    await _item(db, owner_id, 50, ItemStatus.QUEUED)  # in the queue already: not from the shelf

    found = await pick(db, owner_id, now=NOW, seed="2026-W42")

    assert found.recent == []
    assert found.from_the_shelf is not None
    assert found.from_the_shelf.id in shelf


async def test_the_same_seed_gives_the_same_thing_from_the_shelf(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    for days in range(10, 60):
        await _item(db, owner_id, days)

    picks = {(await pick(db, owner_id, now=NOW, seed="2026-W42")).from_the_shelf for _ in range(3)}
    other = {(await pick(db, owner_id, now=NOW, seed=f"week {n}")).from_the_shelf for n in range(8)}

    assert len({item.id for item in picks if item}) == 1
    # Different seeds give different things: the choice is not always the same item.
    assert len({item.id for item in other if item}) > 1


async def test_the_items_of_a_different_owner_are_not_shown(db: AsyncSession) -> None:
    mine = await auth.ensure_owner(db, 1)
    theirs = await auth.ensure_owner(db, 2)
    await _item(db, theirs, 1)
    await _item(db, theirs, 30)

    found = await pick(db, mine, now=NOW, seed="a")

    assert found.empty


def test_nothing_to_choose_gives_nothing() -> None:
    assert choose_one([], "a") is None


def test_the_choice_does_not_depend_on_the_order_of_the_candidates() -> None:
    assert choose_one([3, 1, 2], "seed") == choose_one([2, 3, 1], "seed")


async def test_an_older_thing_in_the_queue_or_in_reading_is_not_taken_from_the_shelf(
    db: AsyncSession,
) -> None:
    # The owner chose these already. The shelf is for things that wait in the inbox.
    owner_id = await auth.ensure_owner(db, 1)
    await _item(db, owner_id, 50, ItemStatus.QUEUED)
    await _item(db, owner_id, 60, ItemStatus.READING)

    found = await pick(db, owner_id, now=NOW, seed="2026-W42")

    assert found.from_the_shelf is None
    assert found.empty
