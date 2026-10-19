"""The stale prompt: which old items of the inbox it asks about. Each test name is one rule."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.enums import Actor, EnrichState, ItemStatus
from unshelve.db.models import Digest, Event, Item
from unshelve.resurface import stale

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
# The rules in the numbers that a person sees: 30 days, three weeks, 90 days, five items.
MONTH = timedelta(days=30)
THREE_WEEKS = timedelta(days=21)
SECOND = timedelta(seconds=1)


async def _owner(db: AsyncSession, telegram_id: int = 1) -> int:
    return await auth.ensure_owner(db, telegram_id)


async def _item(db: AsyncSession, owner_id: int, saved: datetime, **values: object) -> int:
    fields: dict[str, object] = {
        "owner_id": owner_id,
        "status": ItemStatus.INBOX,
        "enrich_state": EnrichState.READY,
        "first_captured_at": saved,
        "last_captured_at": saved,
        **values,
    }
    item = Item(**fields)
    db.add(item)
    await db.flush()
    return item.id


async def _stale(db: AsyncSession, owner_id: int) -> list[int]:
    return [item.id for item in await stale.stale_items(db, owner_id, now=NOW)]


async def test_an_item_of_the_inbox_is_stale_30_days_after_its_last_save(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    month = await _item(db, owner_id, NOW - MONTH)
    almost = await _item(db, owner_id, NOW - MONTH + SECOND)
    # Saved first long ago, and again last week: not stale.
    again = await _item(
        db, owner_id, NOW - timedelta(days=7), first_captured_at=NOW - 3 * MONTH, capture_count=2
    )

    assert await _stale(db, owner_id) == [month]
    assert almost not in await _stale(db, owner_id)
    assert again not in await _stale(db, owner_id)


async def test_only_the_inbox_of_the_owner_counts(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    old = NOW - 2 * MONTH
    inbox = await _item(db, owner_id, old)
    for status in (ItemStatus.QUEUED, ItemStatus.READING, ItemStatus.DONE, ItemStatus.ARCHIVED):
        await _item(db, owner_id, old, status=status, read_at=old)
    await _item(db, owner_id, old, duplicate_of_id=inbox)
    await _item(db, await _owner(db, 2), old)

    assert await _stale(db, owner_id) == [inbox]


async def test_an_item_that_a_digest_of_the_last_three_weeks_showed_is_not_asked_about(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    shown, long_ago, planned = [await _item(db, owner_id, NOW - 2 * MONTH) for _ in range(3)]
    db.add_all(
        [
            Digest(
                owner_id=owner_id,
                period_key="a",
                item_ids=[shown],
                sent_at=NOW - THREE_WEEKS + SECOND,
            ),
            Digest(
                owner_id=owner_id,
                period_key="b",
                item_ids=[long_ago],
                sent_at=NOW - THREE_WEEKS - SECOND,
            ),
            Digest(owner_id=owner_id, period_key="c", item_ids=[planned]),  # never sent
        ]
    )
    await db.flush()

    assert await _stale(db, owner_id) == [long_ago, planned]


async def test_an_item_that_a_prompt_of_the_last_three_weeks_asked_about_is_not_asked_again(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    asked, long_ago, unsent = [await _item(db, owner_id, NOW - 2 * MONTH) for _ in range(3)]
    db.add_all(
        [
            Digest(
                owner_id=owner_id,
                period_key="a",
                stale_item_ids=[asked],
                stale_sent_at=NOW - THREE_WEEKS + SECOND,
            ),
            Digest(
                owner_id=owner_id,
                period_key="b",
                stale_item_ids=[long_ago],
                stale_sent_at=NOW - THREE_WEEKS - SECOND,
            ),
            Digest(owner_id=owner_id, period_key="c", stale_item_ids=[unsent]),
        ]
    )
    await db.flush()

    assert await _stale(db, owner_id) == [long_ago, unsent]


async def test_keep_leaves_an_item_alone_for_90_days(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    kept_lately, kept_long_ago = [await _item(db, owner_id, NOW - 6 * MONTH) for _ in range(2)]
    for item_id, days in ((kept_lately, 89), (kept_long_ago, 91)):
        db.add(
            Event(
                owner_id=owner_id,
                item_id=item_id,
                actor=Actor.USER,
                type=stale.KEPT,
                created_at=NOW - timedelta(days=days),
            )
        )
    await db.flush()

    assert await _stale(db, owner_id) == [kept_long_ago]


async def test_only_the_answer_keep_leaves_an_item_alone_other_events_do_not(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    item_id = await _item(db, owner_id, NOW - 6 * MONTH)
    for kind, days in (("surfaced", 25), ("stale_asked", 25), ("status", 2)):
        db.add(
            Event(
                owner_id=owner_id,
                item_id=item_id,
                actor=Actor.SYSTEM,
                type=kind,
                created_at=NOW - timedelta(days=days),
            )
        )
    await db.flush()

    # The digests and the prompts count by their rows (the tests above), not by these events.
    assert await _stale(db, owner_id) == [item_id]


async def test_a_prompt_asks_about_at_most_5_items_the_oldest_first(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    ids = [await _item(db, owner_id, NOW - MONTH - timedelta(days=days)) for days in range(8)]

    # The same time of saving: the lower id first.
    same = [await _item(db, owner_id, NOW - 9 * MONTH) for _ in range(2)]

    assert await _stale(db, owner_id) == [*same, ids[7], ids[6], ids[5]]


async def test_keep_writes_an_event_only_for_an_item_of_the_owner(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    mine = await _item(db, owner_id, NOW - 2 * MONTH)
    theirs = await _item(db, await _owner(db, 2), NOW - 2 * MONTH)

    assert await stale.keep(db, owner_id, mine) is True
    assert await stale.keep(db, owner_id, theirs) is False
    assert await stale.keep(db, owner_id, 999_999) is False
    await db.flush()

    rows = (
        await db.execute(sa.select(Event.item_id, Event.actor, Event.type, Event.payload))
    ).all()
    assert [tuple(row) for row in rows] == [(mine, Actor.USER, "kept", {"via": "telegram"})]
    assert (await db.get_one(Item, mine)).status is ItemStatus.INBOX
