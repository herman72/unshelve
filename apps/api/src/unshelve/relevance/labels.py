"""Your verdicts on the scores: one write path for the web app and for the private chat.

A label says whether the score that you saw is right (confirmed) or wrong (rejected). It keeps
where you gave it (``label_source``: web or dm) and when. The score step never writes over a
score with a label (``pipeline.scores``), so a label and its score stay together: the pair is one
line of the golden set (``relevance.golden``).

A verdict is about the score that you saw. When the score changed after it was shown, the
verdict is refused: a "right" for a score that you never saw would put a false line into the
golden set.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import EnrichState, GoalLabel, LabelSource
from unshelve.db.models import Goal, Item, ItemGoal


class LabelOutcome(StrEnum):
    SET = "set"
    NOT_FOUND = "not_found"  # no score of this goal for this item, or not of this owner
    SCORE_CHANGED = "score_changed"  # the score is not the one that was shown


@dataclass(frozen=True, slots=True)
class LabelChange:
    outcome: LabelOutcome
    row: ItemGoal | None = None  # the score as it is now; None when there is none


async def set_label(
    session: AsyncSession,
    owner_id: int,
    item_id: int,
    goal_id: int,
    label: GoalLabel | None,
    *,
    source: LabelSource,
    shown_score: int | None = None,
    now: datetime | None = None,
) -> LabelChange:
    """Give the score of one goal for one item your verdict, or remove it (``label`` None).

    Runs in the caller's transaction and does not commit. The latest verdict wins: a second
    verdict replaces the first, with its own source and time.

    The check of the shown score and the write are one statement: the write happens only while
    the score is still the shown one. With two statements, the score step could write a new
    score between them, and the verdict would confirm a score that nobody saw.
    """
    same = (
        ItemGoal.item_id == item_id,
        ItemGoal.goal_id == goal_id,
        ItemGoal.owner_id == owner_id,
    )
    shown = () if shown_score is None else (ItemGoal.score == shown_score,)
    values: dict[str, object] = (
        {"label": None, "label_source": None, "labelled_at": None}
        if label is None
        else {"label": label, "label_source": source, "labelled_at": now or datetime.now(UTC)}
    )
    written = await session.scalar(
        sa.update(ItemGoal)
        .where(*same, *shown)
        .values(**values)
        .returning(ItemGoal)
        .execution_options(populate_existing=True)
    )
    if written is not None:
        return LabelChange(LabelOutcome.SET, written)
    current = await session.scalar(sa.select(ItemGoal).where(*same))
    if current is None:
        return LabelChange(LabelOutcome.NOT_FOUND)
    return LabelChange(LabelOutcome.SCORE_CHANGED, current)


@dataclass(frozen=True, slots=True)
class ToLabel:
    """One score that waits for your verdict."""

    item: Item
    goal: Goal
    score: int
    reason: str


def _waiting(owner_id: int) -> sa.ColumnElement[bool]:
    """A score without a label, of an active goal, of an item that the lists show."""
    return sa.and_(
        ItemGoal.owner_id == owner_id,
        ItemGoal.label.is_(None),
        Goal.archived_at.is_(None),
        Item.enrich_state == EnrichState.READY,
        Item.duplicate_of_id.is_(None),
    )


async def next_to_label(
    session: AsyncSession, owner_id: int, *, seed: str, skip: tuple[int, int] | None = None
) -> ToLabel | None:
    """The next score to ask you about, or None when each score has your verdict.

    It comes from the score value (0 to 3) with the fewest verdicts so far: so the golden set
    gets each value, also the 0 of the prefilter, which can hide a useful item. Among the scores
    of that value, ``seed`` gives the order. ``skip`` is the pair (item, goal) that you skipped.
    """
    joined = (
        sa.select(ItemGoal.score)
        .join(Goal, Goal.id == ItemGoal.goal_id)
        .join(Item, Item.id == ItemGoal.item_id)
        .where(_waiting(owner_id))
    )
    if skip is not None:
        joined = joined.where(sa.tuple_(ItemGoal.item_id, ItemGoal.goal_id) != sa.tuple_(*skip))
    waiting = set(await session.scalars(joined.distinct()))
    if not waiting:
        return None
    rows = await session.execute(
        sa.select(ItemGoal.score, sa.func.count().label("verdicts"))
        .where(ItemGoal.owner_id == owner_id, ItemGoal.label.is_not(None))
        .group_by(ItemGoal.score)
    )
    labelled = {row.score: row.verdicts for row in rows}
    # The fewest verdicts first; between equal counts, the higher score first.
    score = min(waiting, key=lambda value: (labelled.get(value, 0), -value))
    order = sa.func.md5(sa.func.concat(ItemGoal.item_id, ":", ItemGoal.goal_id, ":", seed))
    statement = (
        sa.select(Item, Goal, ItemGoal.score, ItemGoal.reason)
        .join(ItemGoal, ItemGoal.item_id == Item.id)
        .join(Goal, Goal.id == ItemGoal.goal_id)
        .where(_waiting(owner_id), ItemGoal.score == score)
    )
    if skip is not None:
        statement = statement.where(
            sa.tuple_(ItemGoal.item_id, ItemGoal.goal_id) != sa.tuple_(*skip)
        )
    found = (await session.execute(statement.order_by(order).limit(1))).one()
    return ToLabel(item=found.Item, goal=found.Goal, score=found.score, reason=found.reason)
