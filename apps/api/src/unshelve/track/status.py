"""The one way to change the reading status of an item.

The status and the event that records the change are written in one transaction. So the log
(the table ``events``) never misses a change, and never shows a change that did not happen.

The moves are the table of ``contracts.states``. A move into the reading queue (``queued`` or
``reading``) needs a free slot in it: an owner has ``queue_limit`` slots, 5 at the start, so
that the queue stays short enough to read. A move inside the queue, from ``queued`` to
``reading``, takes no new slot.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import Actor, ItemStatus
from unshelve.contracts.states import QUEUE_SLOT_STATUSES, can_change_status
from unshelve.db.models import Event, Item, User


class Outcome(StrEnum):
    CHANGED = "changed"
    ALREADY = "already"  # the item has this status already: a second press, not an error
    NOT_ALLOWED = "not_allowed"  # the table of moves does not have this one
    QUEUE_FULL = "queue_full"  # the move needs a slot in the reading queue, and none is free
    DUPLICATE = "duplicate"  # the item is the same thing as an older item: change that one
    NOT_FOUND = "not_found"  # no such item, or the item of a different owner


@dataclass(frozen=True, slots=True)
class StatusChange:
    outcome: Outcome
    status: ItemStatus | None  # the status after the call; None when there is no such item
    in_queue: int | None = None  # QUEUE_FULL: the items in the reading queue
    limit: int | None = None  # QUEUE_FULL: the slots of the reading queue
    duplicate_of: int | None = None  # DUPLICATE: the item to change instead


async def change_status(
    session: AsyncSession,
    owner_id: int,
    item_id: int,
    target: ItemStatus,
    *,
    actor: Actor,
    via: str,
    now: datetime | None = None,
) -> StatusChange:
    """Move an item to ``target``. Runs in the caller's transaction and does not commit."""
    limit = None
    if target in QUEUE_SLOT_STATUSES:
        # The owner first, then the item. Two moves into the queue at the same moment then
        # count the queue one after the other, and the limit holds. Each move that locks both
        # takes them in this order, so two moves cannot wait for each other.
        limit = await session.scalar(
            sa.select(User.queue_limit).where(User.id == owner_id).with_for_update()
        )
    item = await session.scalar(
        # The row is locked: two presses at the same moment cannot both write an event.
        sa.select(Item).where(Item.id == item_id, Item.owner_id == owner_id).with_for_update()
    )
    if item is None:
        return StatusChange(Outcome.NOT_FOUND, None)
    if item.duplicate_of_id is not None:
        # The lists do not show a duplicate, and its status would count in the queue unseen.
        return StatusChange(Outcome.DUPLICATE, item.status, duplicate_of=item.duplicate_of_id)
    current = item.status
    if current is target:
        return StatusChange(Outcome.ALREADY, current)
    if not can_change_status(current, target):
        return StatusChange(Outcome.NOT_ALLOWED, current)
    if limit is not None and current not in QUEUE_SLOT_STATUSES:
        in_queue = await in_reading_queue(session, owner_id)
        if in_queue >= limit:
            return StatusChange(Outcome.QUEUE_FULL, current, in_queue=in_queue, limit=limit)
    item.status = target
    if target is ItemStatus.DONE:
        item.read_at = now or datetime.now(UTC)  # the database refuses "done" without it
    session.add(
        Event(
            owner_id=owner_id,
            item_id=item_id,
            actor=actor,
            type="status",
            from_status=current,
            to_status=target,
            payload={"via": via},
        )
    )
    await session.flush()
    return StatusChange(Outcome.CHANGED, target)


def in_queue(owner_id: int) -> sa.ColumnElement[bool]:
    """The items of the owner that take a slot in the reading queue. A duplicate takes none.

    The guard counts these rows, and the page of the queue lists them: one rule for both.
    """
    return sa.and_(
        Item.owner_id == owner_id,
        Item.status.in_(QUEUE_SLOT_STATUSES),
        Item.duplicate_of_id.is_(None),
    )


async def in_reading_queue(session: AsyncSession, owner_id: int) -> int:
    """The number of slots of the reading queue that are used."""
    count = await session.scalar(
        sa.select(sa.func.count()).select_from(Item).where(in_queue(owner_id))
    )
    return count or 0
