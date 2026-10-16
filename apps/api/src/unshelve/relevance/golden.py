"""The golden set: your verdicts as an evaluation set for the scorer.

Each line is one score that has your verdict: the goal, the card of the item, the score and its
reason, the scorer that gave it, and your verdict with its source and time. The export is
reproducible: the same database gives the same bytes. The lines are in the order of the item
and the goal, the keys of each line are sorted, and the file holds no time of the export.

The truth of a line is ``relevant``: does the item help the goal? A score of 2 or 3 says yes,
and a score of 0 or 1 says no. A confirmed score keeps its answer; a rejected score turns it.
The goal text and the card are as they are now: ``goal_version`` and ``goal_version_now`` show
a goal that changed after its score.
"""

import json
from collections.abc import Sequence
from datetime import UTC
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import GoalLabel
from unshelve.db.models import Goal, Item, ItemContent, ItemGoal, ItemTag, Tag
from unshelve.relevance.prompt import goal_text, item_text

# A score from this value up says that the item helps the goal (relevance.prompt: 2 helps, 3 is
# directly useful).
RELEVANT_FROM = 2


def relevant(score: int, label: GoalLabel) -> bool:
    """Does the item help the goal, after your verdict on its score?"""
    return (score >= RELEVANT_FROM) == (label is GoalLabel.CONFIRMED)


async def _tags_and_texts(
    session: AsyncSession, item_ids: Sequence[int]
) -> tuple[dict[int, list[str]], dict[int, str]]:
    tags: dict[int, list[str]] = {}
    for row in await session.execute(
        sa.select(ItemTag.item_id, Tag.name)
        .join(Tag, Tag.id == ItemTag.tag_id)
        .where(ItemTag.item_id.in_(item_ids))
        .order_by(ItemTag.item_id, Tag.name)
    ):
        tags.setdefault(row.item_id, []).append(row.name)
    texts = {
        row.item_id: row.text
        for row in await session.execute(
            sa.select(ItemContent.item_id, ItemContent.text).where(
                ItemContent.item_id.in_(item_ids)
            )
        )
    }
    return tags, texts


async def golden_set(session: AsyncSession, owner_id: int) -> list[dict[str, Any]]:
    """Each score of the owner with a verdict, as one line of the golden set."""
    found = list(
        await session.execute(
            sa.select(ItemGoal, Goal, Item)
            .join(Goal, Goal.id == ItemGoal.goal_id)
            .join(Item, Item.id == ItemGoal.item_id)
            .where(ItemGoal.owner_id == owner_id, ItemGoal.label.is_not(None))
            .order_by(ItemGoal.item_id, ItemGoal.goal_id)
        )
    )
    tags, texts = await _tags_and_texts(session, sorted({row.Item.id for row in found}))
    lines: list[dict[str, Any]] = []
    for row in found:
        score, goal, item = row.ItemGoal, row.Goal, row.Item
        if score.label is None or score.label_source is None:
            continue  # the query leaves it out, and a check of the table needs both together
        labelled_at = score.labelled_at
        lines.append(
            {
                "item_id": item.id,
                "goal": goal.slug,
                "goal_version": score.goal_version,
                "goal_version_now": goal.version,
                "goal_text": goal_text(goal),
                "card": item_text(item, tags.get(item.id, []), texts.get(item.id, "")),
                "score": score.score,
                "reason": score.reason,
                "scorer": score.scorer_version,
                "label": score.label.value,
                "label_source": score.label_source.value,
                # In UTC, so that the file does not depend on the time zone of the connection.
                "labelled_at": labelled_at.astimezone(UTC).isoformat() if labelled_at else None,
                "relevant": relevant(score.score, score.label),
            }
        )
    return lines


def to_jsonl(lines: Sequence[dict[str, Any]]) -> str:
    """One JSON object on each line, with sorted keys. Persian text stays readable."""
    return "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines)
