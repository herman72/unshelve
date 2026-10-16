"""Your verdicts on the scores: the write path, and the score that the chat asks about next.
Each test name is one rule."""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.enums import EnrichState, GoalLabel, LabelSource
from unshelve.db.models import Goal, Item, ItemGoal
from unshelve.relevance.labels import LabelOutcome, next_to_label, set_label

NOON = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 17, 18, 30, tzinfo=UTC)


async def _owner(db: AsyncSession, telegram_id: int = 1) -> int:
    return await auth.ensure_owner(db, telegram_id)


async def _goal(db: AsyncSession, owner_id: int, title: str, **values: object) -> Goal:
    goal = Goal(owner_id=owner_id, slug=title.lower().replace(" ", "-"), title=title, **values)
    db.add(goal)
    await db.flush()
    return goal


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


async def _score(
    db: AsyncSession, item: Item, goal: Goal, score: int, label: GoalLabel | None = None
) -> ItemGoal:
    row = ItemGoal(
        item_id=item.id,
        goal_id=goal.id,
        owner_id=item.owner_id,
        score=score,
        reason=f"Reason {score}.",
        scorer_version="score-1:fake-llm",
        goal_version=1,
        label=label,
        label_source=LabelSource.WEB if label else None,
    )
    db.add(row)
    await db.flush()
    return row


async def _scores(db: AsyncSession, owner_id: int, goal: Goal, values: Sequence[int]) -> list[Item]:
    items = [await _item(db, owner_id) for _ in values]
    for item, value in zip(items, values, strict=True):
        await _score(db, item, goal, value)
    return items


# --- The write path --------------------------------------------------------------------------


async def test_a_verdict_keeps_its_source_and_its_time(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    item = await _item(db, owner_id)
    await _score(db, item, goal, 3)

    change = await set_label(
        db, owner_id, item.id, goal.id, GoalLabel.CONFIRMED, source=LabelSource.DM, now=NOON
    )

    assert change.outcome is LabelOutcome.SET
    assert change.row is not None
    assert (change.row.label, change.row.label_source, change.row.labelled_at) == (
        GoalLabel.CONFIRMED,
        LabelSource.DM,
        NOON,
    )


async def test_a_second_verdict_replaces_the_first_with_its_own_source(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    item = await _item(db, owner_id)
    await _score(db, item, goal, 3)
    await set_label(
        db, owner_id, item.id, goal.id, GoalLabel.CONFIRMED, source=LabelSource.DM, now=NOON
    )

    change = await set_label(
        db, owner_id, item.id, goal.id, GoalLabel.REJECTED, source=LabelSource.WEB, now=LATER
    )

    assert change.row is not None
    assert (change.row.label, change.row.label_source, change.row.labelled_at) == (
        GoalLabel.REJECTED,
        LabelSource.WEB,
        LATER,
    )


async def test_no_verdict_removes_the_label_with_its_source_and_its_time(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    item = await _item(db, owner_id)
    await _score(db, item, goal, 3)
    await set_label(
        db, owner_id, item.id, goal.id, GoalLabel.CONFIRMED, source=LabelSource.DM, now=NOON
    )

    change = await set_label(db, owner_id, item.id, goal.id, None, source=LabelSource.WEB)

    assert change.row is not None
    assert (change.row.label, change.row.label_source, change.row.labelled_at) == (None, None, None)


async def test_a_verdict_on_a_score_that_changed_since_it_was_shown_is_refused(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    item = await _item(db, owner_id)
    await _score(db, item, goal, 2)

    change = await set_label(
        db, owner_id, item.id, goal.id, GoalLabel.CONFIRMED, source=LabelSource.DM, shown_score=3
    )

    assert change.outcome is LabelOutcome.SCORE_CHANGED
    assert change.row is not None
    assert (change.row.score, change.row.label) == (2, None)


async def test_the_score_of_another_owner_is_not_found(db: AsyncSession) -> None:
    mine = await _owner(db, 1)
    theirs = await _owner(db, 2)
    goal = await _goal(db, theirs, "Theirs")
    item = await _item(db, theirs)
    await _score(db, item, goal, 3)

    change = await set_label(db, mine, item.id, goal.id, GoalLabel.CONFIRMED, source=LabelSource.DM)

    assert (change.outcome, change.row) == (LabelOutcome.NOT_FOUND, None)


# --- The next score to ask about ---------------------------------------------------------------


async def test_the_next_score_has_the_value_with_the_fewest_verdicts(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    await _scores(db, owner_id, goal, [3, 3, 0, 0])
    # Two verdicts on a 3 and one on a 0 already: the 0 has fewer.
    for value in (3, 3, 0):
        await _score(db, await _item(db, owner_id), goal, value, GoalLabel.CONFIRMED)

    found = await next_to_label(db, owner_id, seed="a")

    assert found is not None
    assert found.score == 0


async def test_the_verdicts_of_another_owner_do_not_count(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    await _scores(db, owner_id, goal, [3, 0])
    theirs = await _owner(db, 2)
    their_goal = await _goal(db, theirs, "Theirs")
    for _ in range(3):
        await _score(db, await _item(db, theirs), their_goal, 3, GoalLabel.CONFIRMED)

    found = await next_to_label(db, owner_id, seed="a")

    # Their three verdicts on a 3 do not make your 3 less wanted.
    assert found is not None
    assert found.score == 3


async def test_with_equal_counts_the_higher_score_comes_first(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    await _scores(db, owner_id, goal, [0, 1, 2, 3])

    found = await next_to_label(db, owner_id, seed="a")

    assert found is not None
    assert (found.goal.id, found.score, found.reason) == (goal.id, 3, "Reason 3.")


async def _picked(db: AsyncSession, owner_id: int, seed: str) -> int:
    found = await next_to_label(db, owner_id, seed=seed)
    assert found is not None
    return found.item.id


async def test_the_seed_decides_the_order_among_equal_scores(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    items = await _scores(db, owner_id, goal, [2] * 6)

    picks = {await _picked(db, owner_id, seed) for seed in "abcdefgh"}

    assert len(picks) > 1  # not always the same item
    assert picks <= {item.id for item in items}
    assert await _picked(db, owner_id, "a") == await _picked(db, owner_id, "a")


async def test_a_skipped_score_is_not_asked_again_at_once(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    only, other = await _scores(db, owner_id, goal, [3, 0])

    found = await next_to_label(db, owner_id, seed="a", skip=(only.id, goal.id))

    assert found is not None
    assert found.item.id == other.id


async def test_a_skipped_score_does_not_come_again_among_equal_scores(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")
    await _scores(db, owner_id, goal, [3, 3])
    first = await _picked(db, owner_id, "a")  # the one that the seed gives first

    found = await next_to_label(db, owner_id, seed="a", skip=(first, goal.id))

    assert found is not None
    assert found.item.id != first


async def test_scores_that_the_lists_do_not_show_are_not_asked_about(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    active = await _goal(db, owner_id, "Rust")
    archived = await _goal(db, owner_id, "Old", archived_at=NOON)
    older = await _item(db, owner_id)
    await _score(db, await _item(db, owner_id), archived, 3)
    await _score(db, await _item(db, owner_id, duplicate_of_id=older.id), active, 3)
    await _score(db, await _item(db, owner_id, enrich_state=EnrichState.ENRICHED), active, 3)
    await _score(db, older, active, 3, GoalLabel.CONFIRMED)
    theirs = await _owner(db, 2)
    their_goal = await _goal(db, theirs, "Theirs")
    await _score(db, await _item(db, theirs), their_goal, 3)

    # An archived goal, a merged copy, an item in the pipeline, a verdict, another owner.
    assert await next_to_label(db, owner_id, seed="a") is None
    found = await db.scalar(sa.select(sa.func.count()).select_from(ItemGoal))
    assert found == 5
