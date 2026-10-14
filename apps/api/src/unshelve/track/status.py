"""The one way to change the reading status of an item.

The status and the event that records the change are written in one transaction. So the log
(the table ``events``) never misses a change, and never shows a change that did not happen.

This version has the move to "done", for the button of the bot. The other moves, the queue
limit and the API arrive with the package M3-1 (issue #25). They use the same function.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import Actor, ItemStatus
from unshelve.contracts.states import can_change_status
from unshelve.db.models import Event, Item


class Outcome(StrEnum):
    CHANGED = "changed"
    ALREADY = "already"  # a second press of the same button: not an error
    NOT_ALLOWED = "not_allowed"  # the table of moves does not have this one
    NOT_FOUND = "not_found"  # no such item, or the item of a different owner


@dataclass(frozen=True, slots=True)
class StatusChange:
    outcome: Outcome
    status: ItemStatus | None  # the status after the call; None when there is no such item


async def mark_done(
    session: AsyncSession,
    owner_id: int,
    item_id: int,
    *,
    actor: Actor,
    via: str,
    now: datetime | None = None,
) -> StatusChange:
    """Move an item to "done". Runs in the caller's transaction and does not commit."""
    item = await session.scalar(
        # The row is locked: two presses at the same moment cannot both write an event.
        sa.select(Item).where(Item.id == item_id, Item.owner_id == owner_id).with_for_update()
    )
    if item is None:
        return StatusChange(Outcome.NOT_FOUND, None)
    current = item.status
    if current is ItemStatus.DONE:
        return StatusChange(Outcome.ALREADY, current)
    if not can_change_status(current, ItemStatus.DONE):
        return StatusChange(Outcome.NOT_ALLOWED, current)
    at = now or datetime.now(UTC)
    item.status = ItemStatus.DONE
    item.read_at = at  # the database refuses "done" without a time of reading
    session.add(
        Event(
            owner_id=owner_id,
            item_id=item_id,
            actor=actor,
            type="status",
            from_status=current,
            to_status=ItemStatus.DONE,
            payload={"via": via},
        )
    )
    await session.flush()
    return StatusChange(Outcome.CHANGED, ItemStatus.DONE)
