"""What a digest shows: the things that were saved in the last days, and one older thing.

The older thing is chosen at random from the inbox. Without it, a digest only repeats the
week, and an item that is older than one week never comes back.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import ItemStatus
from unshelve.db.models import Item

RECENT_DAYS = 7
MOST_RECENT = 5
# The statuses of a thing that is still to read. "done" and "archived" never come back here.
OPEN = (ItemStatus.INBOX, ItemStatus.QUEUED, ItemStatus.READING)


@dataclass(frozen=True, slots=True)
class Digest:
    recent: list[Item]
    from_the_shelf: Item | None

    @property
    def empty(self) -> bool:
        return not self.recent and self.from_the_shelf is None


def choose_one(candidates: Sequence[int], seed: str) -> int | None:
    """One id, chosen at random. The same seed gives the same choice, so a second try of the
    same digest shows the same thing."""
    if not candidates:
        return None
    return random.Random(seed).choice(sorted(candidates))  # noqa: S311 - a choice, not a secret


async def pick(
    session: AsyncSession,
    owner_id: int,
    *,
    now: datetime,
    seed: str,
    days: int = RECENT_DAYS,
    limit: int = MOST_RECENT,
) -> Digest:
    """The items of a digest for ``owner_id`` at the moment ``now``."""
    since = now - timedelta(days=days)
    own = sa.and_(Item.owner_id == owner_id, Item.duplicate_of_id.is_(None), Item.status.in_(OPEN))
    recent = (
        await session.scalars(
            sa.select(Item)
            .where(own, Item.last_captured_at >= since)
            .order_by(Item.last_captured_at.desc(), Item.id.desc())
            .limit(limit)
        )
    ).all()
    older = (
        await session.scalars(
            sa.select(Item.id).where(
                own, Item.status == ItemStatus.INBOX, Item.last_captured_at < since
            )
        )
    ).all()
    chosen = choose_one(older, seed)
    shelf = await session.get(Item, chosen) if chosen is not None else None
    return Digest(recent=list(recent), from_the_shelf=shelf)
