"""The plan of a new scoring of old items. Each test name is one rule."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.contracts.enums import EnrichState, GoalLabel, LabelSource
from unshelve.db.models import Goal, Item, ItemGoal
from unshelve.relevance import rescore
from unshelve.testing.fakes import FakeEmbedder

NOON = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

Sessions = async_sessionmaker[AsyncSession]


async def _items(sessions: Sessions, count: int) -> tuple[int, list[int]]:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        items = [
            Item(
                owner_id=owner_id,
                enrich_state=EnrichState.READY,
                first_captured_at=NOON,
                last_captured_at=NOON,
            )
            for _ in range(count)
        ]
        db.add_all(items)
        await db.commit()
    return owner_id, [item.id for item in items]


async def test_the_plan_counts_each_item_without_a_score_of_the_current_goal_and_prompt(
    committed_db: Sessions,
) -> None:
    owner_id, (none, current, old_prompt, old_goal, labelled) = await _items(committed_db, 5)
    async with committed_db() as db:
        goal = Goal(owner_id=owner_id, slug="indexes", title="Indexes", version=2)
        db.add(goal)
        await db.flush()
        for item_id, scorer, version, label in (
            (current, "score-1:fake-llm", 2, None),
            (old_prompt, "score-0:fake-llm", 2, None),
            (old_goal, "score-1:fake-llm", 1, None),
            # A label is a verdict: its score stays, also of an older goal version.
            (labelled, "score-0:fake-llm", 1, GoalLabel.CONFIRMED),
        ):
            db.add(
                ItemGoal(
                    item_id=item_id,
                    goal_id=goal.id,
                    owner_id=owner_id,
                    score=1,
                    reason="",
                    scorer_version=scorer,
                    goal_version=version,
                    label=label,
                    label_source=LabelSource.WEB if label else None,
                )
            )
        await db.commit()

    plan = await rescore.plan(
        committed_db, FakeEmbedder(), owner_id, model="fake-llm", prefilter=-1
    )

    assert plan.items == [none, old_prompt, old_goal]
    assert (plan.goals, plan.far, plan.stored, plan.questions) == (1, 0, 0, 3)


async def test_a_second_start_adds_no_job_for_an_item_that_waits(committed_db: Sessions) -> None:
    owner_id, items = await _items(committed_db, 2)

    first = await rescore.start(committed_db, owner_id, items)
    second = await rescore.start(committed_db, owner_id, items)

    assert (first, second) == (2, 0)
