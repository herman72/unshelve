"""The golden set: your verdicts as an evaluation set. Each test name is one rule."""

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.enums import EnrichState, GoalLabel, LabelSource
from unshelve.db.models import Goal, Item, ItemContent, ItemGoal, ItemTag, Tag
from unshelve.relevance.golden import golden_set, relevant, to_jsonl

NOON = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


async def _owner(db: AsyncSession, telegram_id: int = 1) -> int:
    return await auth.ensure_owner(db, telegram_id)


async def _item(db: AsyncSession, owner_id: int, **values: object) -> Item:
    fields: dict[str, object] = {
        "owner_id": owner_id,
        "enrich_state": EnrichState.READY,
        "first_captured_at": NOON,
        "last_captured_at": NOON,
        **values,
    }
    item = Item(**fields)
    db.add(item)
    await db.flush()
    return item


async def _verdict(
    db: AsyncSession,
    item: Item,
    goal: Goal,
    score: int,
    label: GoalLabel | None,
    *,
    source: LabelSource = LabelSource.DM,
    at: datetime = NOON,
) -> None:
    db.add(
        ItemGoal(
            item_id=item.id,
            goal_id=goal.id,
            owner_id=item.owner_id,
            score=score,
            reason=f"Reason {score}.",
            scorer_version="score-1:claude-haiku-5-5",
            goal_version=1,
            label=label,
            label_source=source if label else None,
            labelled_at=at if label else None,
        )
    )
    await db.flush()


@pytest.mark.parametrize(
    ("score", "label", "expected"),
    [
        (3, GoalLabel.CONFIRMED, True),
        (2, GoalLabel.CONFIRMED, True),
        (1, GoalLabel.CONFIRMED, False),
        (0, GoalLabel.CONFIRMED, False),
        (3, GoalLabel.REJECTED, False),
        (2, GoalLabel.REJECTED, False),
        (1, GoalLabel.REJECTED, True),
        (0, GoalLabel.REJECTED, True),  # the prefilter hid a useful item
    ],
)
def test_relevant_follows_the_score_and_turns_with_a_rejection(
    score: int, label: GoalLabel, expected: bool
) -> None:
    assert relevant(score, label) is expected


async def test_a_line_holds_the_goal_the_card_the_score_and_the_verdict(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = Goal(
        owner_id=owner_id,
        slug="database-internals",
        title="Database internals",
        description="How Postgres stores rows.",
        keywords=["postgres"],
        version=2,
    )
    db.add(goal)
    item = await _item(
        db, owner_id, title="Leases in Postgres", summary="A job queue on one table."
    )
    tag = Tag(owner_id=owner_id, slug="postgres", name="postgres")
    db.add(tag)
    await db.flush()
    db.add(ItemTag(item_id=item.id, tag_id=tag.id, owner_id=owner_id))
    await _verdict(db, item, goal, 3, GoalLabel.CONFIRMED)

    (line,) = await golden_set(db, owner_id)

    assert line == {
        "item_id": item.id,
        "goal": "database-internals",
        "goal_version": 1,
        "goal_version_now": 2,  # the goal changed after its score
        "goal_text": "Database internals\nHow Postgres stores rows.\nKeywords: postgres",
        "card": "Title: Leases in Postgres\nSummary: A job queue on one table.\nTags: postgres",
        "score": 3,
        "reason": "Reason 3.",
        "scorer": "score-1:claude-haiku-5-5",
        "label": "confirmed",
        "label_source": "dm",
        "labelled_at": "2026-10-17T12:00:00+00:00",
        "relevant": True,
    }


async def test_only_scores_with_a_verdict_of_the_owner_are_in_the_set(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = Goal(owner_id=owner_id, slug="rust", title="Rust", version=1)
    theirs = await _owner(db, 2)
    their_goal = Goal(owner_id=theirs, slug="theirs", title="Theirs", version=1)
    db.add_all([goal, their_goal])
    await db.flush()
    await _verdict(db, await _item(db, owner_id), goal, 2, None)
    await _verdict(db, await _item(db, owner_id), goal, 2, GoalLabel.REJECTED)
    await _verdict(db, await _item(db, theirs), their_goal, 2, GoalLabel.CONFIRMED)

    lines = await golden_set(db, owner_id)

    assert [(line["label"], line["relevant"]) for line in lines] == [("rejected", False)]


async def test_the_same_database_gives_the_same_bytes_whatever_the_order_of_the_verdicts(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    goals = [Goal(owner_id=owner_id, slug=f"g{n}", title=f"Goal {n}", version=1) for n in (1, 2)]
    db.add_all(goals)
    items = [await _item(db, owner_id, title=f"Item {n}") for n in (1, 2)]
    # The verdicts come in another order than the items and the goals.
    await _verdict(db, items[1], goals[1], 1, GoalLabel.REJECTED, source=LabelSource.WEB)
    await _verdict(db, items[0], goals[1], 3, GoalLabel.CONFIRMED)
    await _verdict(db, items[1], goals[0], 0, GoalLabel.CONFIRMED)
    await _verdict(db, items[0], goals[0], 2, GoalLabel.REJECTED)

    first = to_jsonl(await golden_set(db, owner_id))
    second = to_jsonl(await golden_set(db, owner_id))

    assert first == second
    lines = [json.loads(line) for line in first.splitlines()]
    assert [(line["item_id"], line["goal"]) for line in lines] == [
        (items[0].id, "g1"),
        (items[0].id, "g2"),
        (items[1].id, "g1"),
        (items[1].id, "g2"),
    ]
    assert all(list(line) == sorted(line) for line in lines)  # the keys too


async def test_the_time_of_a_verdict_is_in_utc_and_persian_text_stays_readable(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    goal = Goal(owner_id=owner_id, slug="farsi", title="فارسی", version=1)
    db.add(goal)
    helsinki = timezone(timedelta(hours=3))
    await _verdict(
        db,
        await _item(db, owner_id, title="صف کارها"),
        goal,
        2,
        GoalLabel.CONFIRMED,
        at=datetime(2026, 10, 17, 15, 0, tzinfo=helsinki),
    )

    # The connection speaks the time of Helsinki: the file must not.
    await db.execute(sa.text("SET TIME ZONE 'Europe/Helsinki'"))
    text = to_jsonl(await golden_set(db, owner_id))

    assert '"labelled_at": "2026-10-17T12:00:00+00:00"' in text
    assert "صف کارها" in text  # not "\\u0635..."


async def test_a_card_without_a_title_shows_the_start_of_its_text(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = Goal(owner_id=owner_id, slug="rust", title="Rust", version=1)
    db.add(goal)
    item = await _item(db, owner_id)
    db.add(
        ItemContent(
            item_id=item.id,
            owner_id=owner_id,
            text="A photo of a whiteboard about lifetimes.",
            content_hash="0" * 64,
            extractor="image",
            extractor_version=1,
        )
    )
    await _verdict(db, item, goal, 1, GoalLabel.REJECTED)

    (line,) = await golden_set(db, owner_id)

    assert line["card"] == "Text: A photo of a whiteboard about lifetimes."
