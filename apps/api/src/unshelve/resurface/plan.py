"""The digest of now: the candidates from the database, then the pure choice of
``resurface.selection``.

This module reads; it writes nothing. So a preview shows a digest and changes no state, and
the choice itself stays free of the database: its rules have tests on made-up inputs.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import GoalLabel, ItemStatus
from unshelve.db.models import Digest, Goal, Item, ItemGoal
from unshelve.resurface.selection import Candidate, Pick, Reason, select

# A digest does not show an item again that a digest of the last three weeks showed.
NO_REPEAT_DAYS = 21


@dataclass(frozen=True, slots=True)
class PlannedDigest:
    goal: Goal | None  # the goal of the focus picks
    picks: list[tuple[Item, Pick]]
    goal_titles: dict[int, str]  # the title of each active goal, for the reasons
    # The scores that the digest shows, as (item, goal): (score of the model, your verdict).
    # A pick for a goal shows the score of its goal; the random pick shows its score for the
    # focus goal, when it has one. A digest asks for a verdict only on a score that has none.
    scores: dict[tuple[int, int], tuple[int, GoalLabel | None]]

    def shown_score(self, pick: Pick) -> tuple[int, int, GoalLabel | None] | None:
        """The goal, the score and your verdict that the digest shows for a pick, or None."""
        goal_id = _goal_of_score(pick, self.goal)
        if goal_id is None or (pick.item_id, goal_id) not in self.scores:
            return None
        return (goal_id, *self.scores[(pick.item_id, goal_id)])

    @property
    def total_minutes(self) -> int:
        return sum(pick.minutes for _, pick in self.picks)

    @property
    def empty(self) -> bool:
        return not self.picks


async def _goal(session: AsyncSession, owner_id: int, goal_id: int | None) -> Goal | None:
    """The goal of the focus picks: the one asked for, or else the focus goal."""
    chosen = Goal.is_focus.is_(True) if goal_id is None else Goal.id == goal_id
    goal = await session.scalar(
        sa.select(Goal).where(Goal.owner_id == owner_id, Goal.archived_at.is_(None), chosen)
    )
    if goal is None and goal_id is not None:
        msg = "no such active goal"
        raise LookupError(msg)
    return goal


async def shown_since(session: AsyncSession, owner_id: int, since: datetime) -> set[int]:
    """The items of the digests that were sent from ``since`` on. A planned digest that was
    never sent showed nothing."""
    shown: set[int] = set()
    for item_ids in await session.scalars(
        sa.select(Digest.item_ids).where(Digest.owner_id == owner_id, Digest.sent_at >= since)
    ):
        shown.update(item_ids)
    return shown


async def plan_digest(
    session: AsyncSession,
    owner_id: int,
    *,
    now: datetime,
    seed: str,
    minutes: int,
    goal_id: int | None = None,
) -> PlannedDigest:
    """The items that a digest for ``minutes`` of reading would show now, and why.

    ``goal_id`` replaces the focus goal; a goal that is not an active goal of the owner raises
    LookupError.
    """
    goal = await _goal(session, owner_id, goal_id)
    titles = {
        row.id: row.title
        for row in await session.execute(
            sa.select(Goal.id, Goal.title).where(
                Goal.owner_id == owner_id, Goal.archived_at.is_(None)
            )
        )
    }
    items = list(
        await session.scalars(
            sa.select(Item).where(
                Item.owner_id == owner_id,
                Item.status == ItemStatus.INBOX,
                Item.duplicate_of_id.is_(None),
            )
        )
    )
    scores: dict[int, dict[int, tuple[int, GoalLabel | None]]] = {}
    for row in await session.execute(
        sa.select(ItemGoal.item_id, ItemGoal.goal_id, ItemGoal.score, ItemGoal.label)
        .join(Item, Item.id == ItemGoal.item_id)
        .where(
            ItemGoal.owner_id == owner_id,
            ItemGoal.goal_id.in_(list(titles)),
            Item.status == ItemStatus.INBOX,
        )
    ):
        scores.setdefault(row.item_id, {})[row.goal_id] = (row.score, row.label)
    selection = select(
        [
            Candidate(
                item_id=item.id,
                status=item.status,
                saved_at=item.last_captured_at,
                saves=item.capture_count,
                minutes=item.est_minutes,
                scores=scores.get(item.id, {}),
            )
            for item in items
        ],
        focus_goal_id=goal.id if goal is not None else None,
        budget=minutes,
        seed=seed,
        now=now,
        shown_recently=await shown_since(session, owner_id, now - timedelta(days=NO_REPEAT_DAYS)),
    )
    by_id = {item.id: item for item in items}
    return PlannedDigest(
        goal=goal,
        picks=[(by_id[pick.item_id], pick) for pick in selection.picks],
        goal_titles=titles,
        scores={
            (pick.item_id, goal_id): scores[pick.item_id][goal_id]
            for pick in selection.picks
            if (goal_id := _goal_of_score(pick, goal)) is not None
            and goal_id in scores.get(pick.item_id, {})
        },
    )


def _goal_of_score(pick: Pick, focus: Goal | None) -> int | None:
    """The goal whose score the digest shows for a pick: the goal of a pick for a goal, or the
    focus goal for the random pick. Your verdict on the random pick tells whether a low score
    hid a useful item: the other rules never show such a score (docs/learn/34)."""
    if pick.goal_id is not None:
        return pick.goal_id
    if pick.reason is Reason.RANDOM and focus is not None:
        return focus.id
    return None
