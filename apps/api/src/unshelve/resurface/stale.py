"""The stale prompt: old items of the inbox, and the question "archive it, or keep it?".

An item is stale when it waits in the inbox since its last save 30 days ago or more, and
nothing brought it back since: no digest of the last 3 weeks showed it, no stale prompt of the
last 3 weeks asked about it, and you did not keep it in the last 90 days. A prompt asks about
the oldest 5 of them. The weekly digest sends the prompt after its own message
(resurface.weekly).
"""

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import Actor, ItemStatus
from unshelve.db.models import Digest, Event, Item
from unshelve.resurface.plan import NO_REPEAT_DAYS, shown_since

STALE_AFTER_DAYS = 30
# "Keep" says more than no answer: the prompt does not ask about the item again for this long.
KEEP_DAYS = 90
AT_MOST = 5
# The types of the events of the prompt: it asked about the item, and you kept it.
ASKED = "stale_asked"
KEPT = "kept"


async def asked_since(session: AsyncSession, owner_id: int, since: datetime) -> set[int]:
    """The items of the stale prompts that were sent from ``since`` on."""
    asked: set[int] = set()
    for item_ids in await session.scalars(
        sa.select(Digest.stale_item_ids).where(
            Digest.owner_id == owner_id, Digest.stale_sent_at >= since
        )
    ):
        asked.update(item_ids)
    return asked


async def stale_items(session: AsyncSession, owner_id: int, *, now: datetime) -> list[Item]:
    """The items that a stale prompt asks about now, the oldest first. The digest of the same
    week counts as shown: it is marked as sent before the prompt is chosen, so the prompt does
    not ask to archive a thing that the message before it brought back."""
    since = now - timedelta(days=NO_REPEAT_DAYS)
    left_out = await shown_since(session, owner_id, since) | await asked_since(
        session, owner_id, since
    )
    kept = sa.select(Event.item_id).where(
        Event.owner_id == owner_id,
        Event.type == KEPT,
        Event.created_at >= now - timedelta(days=KEEP_DAYS),
    )
    query = sa.select(Item).where(
        Item.owner_id == owner_id,
        Item.status == ItemStatus.INBOX,
        Item.duplicate_of_id.is_(None),
        Item.last_captured_at <= now - timedelta(days=STALE_AFTER_DAYS),
        Item.id.not_in(kept),
    )
    if left_out:
        query = query.where(Item.id.not_in(left_out))
    return list(
        await session.scalars(query.order_by(Item.last_captured_at, Item.id).limit(AT_MOST))
    )


async def keep(session: AsyncSession, owner_id: int, item_id: int) -> bool:
    """The button "Keep": the item stays where it is, and the prompt leaves it alone for 90
    days. The caller commits. False when the owner has no such item."""
    found = await session.scalar(
        sa.select(Item.id).where(Item.id == item_id, Item.owner_id == owner_id)
    )
    if found is None:
        return False
    session.add(
        Event(
            owner_id=owner_id,
            item_id=item_id,
            actor=Actor.USER,
            type=KEPT,
            payload={"via": "telegram"},
        )
    )
    return True
