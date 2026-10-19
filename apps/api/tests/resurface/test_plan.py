"""The digest of now, from the database. Each test name is one rule."""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.enums import EnrichState, ItemStatus
from unshelve.db.models import Digest, Goal, Item, ItemGoal
from unshelve.resurface.plan import plan_digest
from unshelve.resurface.selection import Reason

NOW = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=60)
THREE_WEEKS = timedelta(days=21)


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
        "status": ItemStatus.INBOX,
        "enrich_state": EnrichState.READY,
        "est_minutes": 20,  # more than a quarter of 60: never the random slot
        "first_captured_at": OLD,
        "last_captured_at": OLD,
        **values,
    }
    item = Item(**fields)
    db.add(item)
    await db.flush()
    return item


async def _score(db: AsyncSession, item: Item, goal: Goal, score: int) -> None:
    db.add(
        ItemGoal(
            item_id=item.id,
            goal_id=goal.id,
            owner_id=item.owner_id,
            score=score,
            reason="",
            scorer_version="score-1:fake-llm",
            goal_version=1,
        )
    )
    await db.flush()


async def _plan(db: AsyncSession, owner_id: int, **values: object) -> list[tuple[int, Reason]]:
    planned = await plan_digest(db, owner_id, now=NOW, seed="2026-W42", minutes=60, **values)  # type: ignore[arg-type]
    return [(item.id, pick.reason) for item, pick in planned.picks]


async def test_the_focus_goal_of_the_owner_gives_the_focus_picks(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    focus = await _goal(db, owner_id, "Rust", is_focus=True)
    other = await _goal(db, owner_id, "GPU")
    for_focus, for_other = await _item(db, owner_id), await _item(db, owner_id)
    await _score(db, for_focus, focus, 3)
    await _score(db, for_other, other, 3)

    planned = await plan_digest(db, owner_id, now=NOW, seed="2026-W42", minutes=60)

    assert [(item.id, pick.reason) for item, pick in planned.picks] == [
        (for_focus.id, Reason.FOCUS_GOAL),
        (for_other.id, Reason.OTHER_GOAL),
    ]
    assert planned.goal is not None
    assert planned.goal.title == "Rust"
    assert planned.goal_titles == {focus.id: "Rust", other.id: "GPU"}
    assert planned.total_minutes == 40


async def test_the_plan_keeps_the_scores_that_the_digest_shows(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    focus = await _goal(db, owner_id, "Rust", is_focus=True)
    other = await _goal(db, owner_id, "GPU")
    for_other = await _item(db, owner_id)
    # 5 minutes and no rule: the random pick. Its score of 0 for the focus goal is shown.
    short = await _item(db, owner_id, est_minutes=5)
    await _score(db, for_other, other, 3)
    await _score(db, for_other, focus, 0)
    await _score(db, short, focus, 0)

    planned = await plan_digest(db, owner_id, now=NOW, seed="2026-W42", minutes=60)

    assert [(item.id, pick.reason) for item, pick in planned.picks] == [
        (for_other.id, Reason.OTHER_GOAL),
        (short.id, Reason.RANDOM),
    ]
    # Only the score that each pick shows: not the 0 of the first item for the focus goal.
    assert planned.scores == {(for_other.id, other.id): (3, None), (short.id, focus.id): (0, None)}
    assert [planned.shown_score(pick) for _, pick in planned.picks] == [
        (other.id, 3, None),
        (focus.id, 0, None),
    ]


async def test_a_goal_of_the_request_replaces_the_focus_goal(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    await _goal(db, owner_id, "Rust", is_focus=True)
    asked = await _goal(db, owner_id, "GPU")
    item = await _item(db, owner_id)
    await _score(db, item, asked, 2)

    assert await _plan(db, owner_id, goal_id=asked.id) == [(item.id, Reason.FOCUS_GOAL)]


async def test_a_goal_that_is_not_active_is_refused(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    archived = await _goal(db, owner_id, "Old", archived_at=OLD)
    theirs = await _goal(db, await _owner(db, 2), "Theirs")

    for goal_id in (archived.id, theirs.id, 999_999):
        with pytest.raises(LookupError):
            await _plan(db, owner_id, goal_id=goal_id)


async def test_without_a_focus_goal_the_other_rules_still_fill_the_digest(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust")  # not the focus
    item = await _item(db, owner_id)
    await _score(db, item, goal, 3)

    assert await _plan(db, owner_id) == [(item.id, Reason.OTHER_GOAL)]


async def test_an_item_of_a_digest_of_the_last_three_weeks_does_not_come_again(
    db: AsyncSession,
) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust", is_focus=True)
    recent, long_ago, planned_only = [await _item(db, owner_id) for _ in range(3)]
    for item in (recent, long_ago, planned_only):
        await _score(db, item, goal, 3)
    db.add_all(
        [
            Digest(
                owner_id=owner_id,
                period_key="a",
                item_ids=[recent.id],
                sent_at=NOW - THREE_WEEKS + timedelta(seconds=1),
            ),
            Digest(
                owner_id=owner_id,
                period_key="b",
                item_ids=[long_ago.id],
                sent_at=NOW - THREE_WEEKS - timedelta(seconds=1),
            ),
            # A digest that was never sent showed nothing.
            Digest(owner_id=owner_id, period_key="c", item_ids=[planned_only.id]),
        ]
    )
    await db.flush()

    picked = {item_id for item_id, _ in await _plan(db, owner_id)}

    assert picked == {long_ago.id, planned_only.id}


async def test_only_the_inbox_of_the_owner_counts(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust", is_focus=True)
    inbox = await _item(db, owner_id)
    queued = await _item(db, owner_id, status=ItemStatus.QUEUED)
    copy = await _item(db, owner_id, duplicate_of_id=inbox.id)
    theirs = await _owner(db, 2)
    their_goal = await _goal(db, theirs, "Theirs", is_focus=True)
    their_item = await _item(db, theirs)
    for item in (inbox, queued, copy):
        await _score(db, item, goal, 3)
    await _score(db, their_item, their_goal, 3)

    assert await _plan(db, owner_id) == [(inbox.id, Reason.FOCUS_GOAL)]


async def test_an_item_that_you_saved_again_counts_by_its_last_save(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    # Both were saved two times. The first save of "lately" is older, its last save is newer.
    lately = await _item(
        db,
        owner_id,
        capture_count=2,
        first_captured_at=NOW - timedelta(days=90),
        last_captured_at=NOW - timedelta(days=9),
    )
    earlier = await _item(
        db,
        owner_id,
        capture_count=2,
        first_captured_at=NOW - timedelta(days=30),
        last_captured_at=NOW - timedelta(days=20),
    )

    assert await _plan(db, owner_id) == [
        (lately.id, Reason.SAVED_AGAIN),
        (earlier.id, Reason.SAVED_AGAIN),
    ]


async def test_the_score_of_an_archived_goal_does_not_count(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    old = await _goal(db, owner_id, "Old", archived_at=OLD)
    item = await _item(db, owner_id)
    await _score(db, item, old, 3)

    # No rule takes it: an old item, saved one time, with a score of a goal that you left.
    assert await _plan(db, owner_id) == []


async def test_a_planned_digest_writes_nothing(db: AsyncSession) -> None:
    owner_id = await _owner(db)
    goal = await _goal(db, owner_id, "Rust", is_focus=True)
    item = await _item(db, owner_id)
    await _score(db, item, goal, 3)

    await _plan(db, owner_id)

    assert await db.scalar(sa.select(sa.func.count()).select_from(Digest)) == 0
    assert (await db.get_one(Item, item.id)).last_surfaced_at is None
