"""The one write path of the reading status. Each test name is one rule."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.contracts.enums import Actor, ItemStatus
from unshelve.contracts.states import STATUS_TRANSITIONS
from unshelve.db.models import Event, Item, User
from unshelve.track import status as status_module
from unshelve.track.status import Outcome, change_status, in_reading_queue

S = ItemStatus
NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 18, 9, 30, tzinfo=UTC)

# Each pair of two different statuses: 20 moves, of which the table allows 13.
PAIRS = [(current, target) for current in S for target in S if current is not target]


async def _item(db: AsyncSession, owner_id: int, status: S = S.INBOX, **extra: Any) -> int:
    values = {
        "owner_id": owner_id,
        "status": status,
        "first_captured_at": NOON,
        "last_captured_at": NOON,
        **extra,
    }
    if status is S.DONE:
        values["read_at"] = NOON
    return (await db.execute(sa.insert(Item).values(**values).returning(Item.id))).scalar_one()


async def _events(db: AsyncSession) -> list[Event]:
    return list((await db.execute(sa.select(Event).order_by(Event.id))).scalars().all())


async def _move(
    db: AsyncSession, owner_id: int, item_id: int, target: S
) -> status_module.StatusChange:
    return await change_status(
        db, owner_id, item_id, target, actor=Actor.USER, via="api", now=LATER
    )


async def test_a_move_saves_the_status_and_one_event_together(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id)

    change = await _move(db, owner_id, item_id, S.QUEUED)

    assert (change.outcome, change.status) == (Outcome.CHANGED, S.QUEUED)
    assert (await db.get_one(Item, item_id)).status is S.QUEUED
    (event,) = await _events(db)
    assert (event.owner_id, event.item_id, event.actor) == (owner_id, item_id, Actor.USER)
    assert (event.type, event.from_status, event.to_status) == ("status", "inbox", "queued")
    assert event.payload == {"via": "api"}


@pytest.mark.parametrize(("current", "target"), PAIRS, ids=[f"{c}-to-{t}" for c, t in PAIRS])
async def test_each_move_follows_the_table_and_only_a_move_writes_an_event(
    db: AsyncSession, current: S, target: S
) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id, current)

    change = await _move(db, owner_id, item_id, target)

    item = await db.get_one(Item, item_id)
    if target in STATUS_TRANSITIONS[current]:
        assert (change.outcome, change.status, item.status) == (Outcome.CHANGED, target, target)
        (event,) = await _events(db)
        assert (event.from_status, event.to_status) == (current, target)
    else:
        assert (change.outcome, change.status, item.status) == (
            Outcome.NOT_ALLOWED,
            current,
            current,
        )
        assert await _events(db) == []


@pytest.mark.parametrize("current", list(S))
async def test_the_status_that_the_item_has_already_is_no_move_and_no_event(
    db: AsyncSession, current: S
) -> None:
    # A second press on a button, or two open tabs: the item is where the press wants it.
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id, current)

    change = await _move(db, owner_id, item_id, current)

    assert (change.outcome, change.status) == (Outcome.ALREADY, current)
    assert await _events(db) == []


async def test_done_saves_the_time_of_reading(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id)

    await _move(db, owner_id, item_id, S.DONE)

    assert (await db.get_one(Item, item_id)).read_at == LATER


async def test_a_second_press_on_done_keeps_the_first_time(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id)
    await _move(db, owner_id, item_id, S.DONE)

    again = await change_status(db, owner_id, item_id, S.DONE, actor=Actor.USER, via="telegram")

    assert again.outcome is Outcome.ALREADY
    assert (await db.get_one(Item, item_id)).read_at == LATER


async def test_an_item_read_again_keeps_its_last_time_until_it_is_done_again(
    db: AsyncSession,
) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _item(db, owner_id, S.DONE)  # read at noon

    await _move(db, owner_id, item_id, S.QUEUED)
    assert (await db.get_one(Item, item_id)).read_at == NOON
    await _move(db, owner_id, item_id, S.DONE)

    assert (await db.get_one(Item, item_id)).read_at == LATER


# --- The reading queue -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"), [(S.INBOX, S.QUEUED), (S.INBOX, S.READING), (S.DONE, S.QUEUED)]
)
async def test_a_move_into_a_full_queue_is_refused_with_the_count_and_the_limit(
    db: AsyncSession, current: S, target: S
) -> None:
    owner_id = await auth.ensure_owner(db, 1)  # 5 slots
    for status in (S.QUEUED, S.QUEUED, S.QUEUED, S.READING, S.READING):
        await _item(db, owner_id, status)
    item_id = await _item(db, owner_id, current)

    change = await _move(db, owner_id, item_id, target)

    assert (change.outcome, change.status) == (Outcome.QUEUE_FULL, current)
    assert (change.in_queue, change.limit) == (5, 5)
    assert (await db.get_one(Item, item_id)).status is current
    assert await _events(db) == []


async def test_a_move_inside_the_queue_takes_no_new_slot(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    queued = [await _item(db, owner_id, S.QUEUED) for _ in range(5)]

    to_reading = await _move(db, owner_id, queued[0], S.READING)
    back = await _move(db, owner_id, queued[0], S.QUEUED)

    assert (to_reading.outcome, back.outcome) == (Outcome.CHANGED, Outcome.CHANGED)


async def test_the_status_that_the_item_has_already_is_no_error_in_a_full_queue(
    db: AsyncSession,
) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    queued = [await _item(db, owner_id, S.QUEUED) for _ in range(5)]

    change = await _move(db, owner_id, queued[0], S.QUEUED)

    assert change.outcome is Outcome.ALREADY


async def test_a_move_out_of_the_queue_frees_a_slot(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    queued = [await _item(db, owner_id, S.QUEUED) for _ in range(5)]
    waiting = await _item(db, owner_id)

    assert (await _move(db, owner_id, waiting, S.QUEUED)).outcome is Outcome.QUEUE_FULL
    await _move(db, owner_id, queued[0], S.DONE)

    assert (await _move(db, owner_id, waiting, S.QUEUED)).outcome is Outcome.CHANGED
    assert await in_reading_queue(db, owner_id) == 5


async def test_a_duplicate_takes_no_slot_in_the_queue(db: AsyncSession) -> None:
    # The lists do not show a duplicate, so the owner cannot see it or move it out.
    owner_id = await auth.ensure_owner(db, 1)
    queued = [await _item(db, owner_id, S.QUEUED) for _ in range(4)]
    await _item(db, owner_id, S.QUEUED, duplicate_of_id=queued[0])
    item_id = await _item(db, owner_id)

    change = await _move(db, owner_id, item_id, S.QUEUED)

    assert change.outcome is Outcome.CHANGED


async def test_the_items_of_another_owner_take_no_slot(db: AsyncSession) -> None:
    mine = await auth.ensure_owner(db, 1)
    theirs = await auth.ensure_owner(db, 2)
    for _ in range(5):
        await _item(db, theirs, S.QUEUED)
    item_id = await _item(db, mine)

    assert (await _move(db, mine, item_id, S.QUEUED)).outcome is Outcome.CHANGED


async def test_the_owner_has_the_number_of_slots_of_their_row(db: AsyncSession) -> None:
    # The owner made the queue smaller after it was filled: 3 items, and 2 slots now.
    owner_id = await auth.ensure_owner(db, 1)
    for _ in range(3):
        await _item(db, owner_id, S.QUEUED)
    await db.execute(sa.update(User).where(User.id == owner_id).values(queue_limit=2))
    item_id = await _item(db, owner_id)

    change = await _move(db, owner_id, item_id, S.QUEUED)

    assert (change.outcome, change.in_queue, change.limit) == (Outcome.QUEUE_FULL, 3, 2)


# --- Items that cannot move ----------------------------------------------------------------


async def test_a_duplicate_is_refused_and_names_the_item_to_change(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    older = await _item(db, owner_id)
    duplicate = await _item(db, owner_id, duplicate_of_id=older)

    change = await _move(db, owner_id, duplicate, S.DONE)

    assert (change.outcome, change.status, change.duplicate_of) == (
        Outcome.DUPLICATE,
        S.INBOX,
        older,
    )
    assert (await db.get_one(Item, duplicate)).status is S.INBOX
    assert await _events(db) == []


async def test_the_item_of_a_different_owner_is_not_found(db: AsyncSession) -> None:
    mine = await auth.ensure_owner(db, 1)
    theirs = await auth.ensure_owner(db, 2)
    their_item = await _item(db, theirs)

    change = await _move(db, mine, their_item, S.DONE)

    assert (change.outcome, change.status) == (Outcome.NOT_FOUND, None)
    assert (await db.get_one(Item, their_item)).status is S.INBOX


async def test_an_item_that_does_not_exist_is_not_found(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    assert (await _move(db, owner_id, 999_999, S.QUEUED)).outcome is Outcome.NOT_FOUND


# --- Two requests at the same moment ---------------------------------------------------------


async def test_two_presses_at_the_same_moment_write_one_event(
    committed_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, 1)
        item_id = await _item(db, owner_id)
        await db.commit()
    check = status_module.can_change_status
    the_first_press_looked = asyncio.Event()

    def look_and_tell(current: S, target: S) -> bool:
        the_first_press_looked.set()
        return check(current, target)

    monkeypatch.setattr(status_module, "can_change_status", look_and_tell)

    async def press(wait: bool) -> Outcome:
        if wait:
            await the_first_press_looked.wait()
        async with committed_db() as db:
            change = await change_status(
                db, owner_id, item_id, S.DONE, actor=Actor.USER, via="telegram"
            )
            if not wait:
                # The first press is slow before it saves. Without the lock of the row, the
                # second press reads "inbox" in this time and writes a second event.
                await asyncio.sleep(0.3)
            await db.commit()
        return change.outcome

    first, second = await asyncio.gather(press(wait=False), press(wait=True))

    assert (first, second) == (Outcome.CHANGED, Outcome.ALREADY)
    async with committed_db() as db:
        assert len(await _events(db)) == 1


async def test_two_moves_into_the_last_free_slot_fill_it_once(
    committed_db: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, 1)
        for _ in range(4):
            await _item(db, owner_id, S.QUEUED)
        one, other = await _item(db, owner_id), await _item(db, owner_id)
        await db.commit()
    count = status_module.in_reading_queue
    the_first_move_counted = asyncio.Event()

    async def count_and_tell(session: AsyncSession, owner: int) -> int:
        found = await count(session, owner)
        the_first_move_counted.set()
        return found

    monkeypatch.setattr(status_module, "in_reading_queue", count_and_tell)

    async def move(item_id: int, wait: bool) -> Outcome:
        if wait:
            await the_first_move_counted.wait()
        async with committed_db() as db:
            change = await change_status(
                db, owner_id, item_id, S.QUEUED, actor=Actor.USER, via="api"
            )
            if not wait:
                # The first move counted 4 and is slow before it saves. The two items are two
                # different rows, so only the lock of the owner makes the second move wait.
                # Without it, the second move also counts 4, and the queue gets 6 items.
                await asyncio.sleep(0.3)
            await db.commit()
        return change.outcome

    first, second = await asyncio.gather(move(one, wait=False), move(other, wait=True))

    assert (first, second) == (Outcome.CHANGED, Outcome.QUEUE_FULL)
    async with committed_db() as db:
        assert await count(db, owner_id) == 5
