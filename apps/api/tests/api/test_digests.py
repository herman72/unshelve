"""The preview of a digest over HTTP. Each test name is one rule."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.support import problem_body
from tests.conftest import ClientFactory
from unshelve.api import item_views
from unshelve.api.deps import SESSION_COOKIE
from unshelve.api.schemas import DigestPreviewRequest
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.enums import EnrichState, ItemStatus
from unshelve.db.models import Digest, Goal, Item, ItemGoal

PREVIEW = "/api/v1/digests/preview"
OWNER_TELEGRAM_ID = 4242
# Long before each run of the tests: no item of these tests is a recent save.
LONG_AGO = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def client(
    client_for: ClientFactory, private_database_url: str, committed_db: Sessions
) -> AsyncGenerator[httpx.AsyncClient]:
    """A client that is signed in as the owner."""
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        secret = await auth.open_session(db, owner_id)
        await db.commit()
    async with client_for(Settings(env="test", database_url=private_database_url)) as client:
        client.cookies.set(SESSION_COOKIE, secret)
        yield client


async def _owner(sessions: Sessions, telegram_user_id: int = OWNER_TELEGRAM_ID) -> int:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, telegram_user_id)
        await db.commit()
    return owner_id


async def _goal(sessions: Sessions, owner_id: int, title: str, **values: Any) -> int:
    async with sessions() as db:
        goal = Goal(owner_id=owner_id, slug=title.lower(), title=title, **values)
        db.add(goal)
        await db.commit()
    return goal.id


async def _item(
    sessions: Sessions, owner_id: int, minutes: int, scores: dict[int, int] | None = None
) -> int:
    """An item of the inbox with a score for each goal of ``scores``. Returns its id."""
    async with sessions() as db:
        item = Item(
            owner_id=owner_id,
            status=ItemStatus.INBOX,
            enrich_state=EnrichState.READY,
            title=f"A read of {minutes} minutes",
            est_minutes=minutes,
            first_captured_at=LONG_AGO,
            last_captured_at=LONG_AGO,
        )
        db.add(item)
        await db.flush()
        for goal_id, score in (scores or {}).items():
            db.add(
                ItemGoal(
                    item_id=item.id,
                    goal_id=goal_id,
                    owner_id=owner_id,
                    score=score,
                    reason="Close.",
                    scorer_version="score-1:fake-llm",
                    goal_version=1,
                )
            )
        await db.commit()
    return item.id


def _picks(response: httpx.Response) -> list[tuple[int, str, str | None]]:
    assert response.status_code == 200, response.text
    return [
        (pick["item"]["id"], pick["reason"], pick["goal_title"])
        for pick in response.json()["picks"]
    ]


async def test_a_preview_shows_each_pick_with_its_reason_and_its_goal(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    rust = await _goal(committed_db, owner_id, "Rust", is_focus=True)
    gpu = await _goal(committed_db, owner_id, "GPU")
    for_rust = await _item(committed_db, owner_id, 30, {rust: 3})
    for_gpu = await _item(committed_db, owner_id, 20, {gpu: 2})
    short = await _item(committed_db, owner_id, 5)  # only this one fits the random slot
    theirs = await _owner(committed_db, 99)
    await _item(committed_db, theirs, 5, {await _goal(committed_db, theirs, "Theirs"): 3})

    response = await client.post(PREVIEW, json={"minutes": 60})

    assert _picks(response) == [
        (for_rust, "focus_goal", "Rust"),
        (for_gpu, "other_goal", "GPU"),
        (short, "random", None),
    ]
    preview = response.json()
    assert (preview["total_minutes"], preview["goal_title"]) == (55, "Rust")
    first = preview["picks"][0]["item"]
    assert (first["title"], first["est_minutes"], first["status"]) == (
        "A read of 30 minutes",
        30,
        "inbox",
    )
    assert first["top_goal"]["goal_title"] == "Rust"


async def test_a_preview_sends_nothing_and_marks_nothing(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    rust = await _goal(committed_db, owner_id, "Rust", is_focus=True)
    item_id = await _item(committed_db, owner_id, 10, {rust: 3})

    assert _picks(await client.post(PREVIEW, json={})) == [(item_id, "focus_goal", "Rust")]

    async with committed_db() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(Digest)) == 0
        assert (await db.get_one(Item, item_id)).last_surfaced_at is None


async def test_the_budget_of_the_request_limits_the_minutes(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    rust = await _goal(committed_db, owner_id, "Rust", is_focus=True)
    for minutes in (40, 25, 25):
        await _item(committed_db, owner_id, minutes, {rust: 3})

    small = (await client.post(PREVIEW, json={"minutes": 30})).json()
    large = (await client.post(PREVIEW, json={"minutes": 90})).json()

    assert small["total_minutes"] == 25
    assert large["total_minutes"] == 90


async def test_a_goal_of_the_request_replaces_the_focus_goal(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    await _goal(committed_db, owner_id, "Rust", is_focus=True)
    gpu = await _goal(committed_db, owner_id, "GPU")
    item_id = await _item(committed_db, owner_id, 10, {gpu: 3})

    response = await client.post(PREVIEW, json={"goal_id": gpu})

    assert _picks(response) == [(item_id, "focus_goal", "GPU")]
    assert response.json()["goal_title"] == "GPU"


async def test_a_goal_that_is_not_an_active_goal_of_yours_is_not_found(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    archived = await _goal(committed_db, owner_id, "Old", archived_at=LONG_AGO)
    theirs = await _goal(committed_db, await _owner(committed_db, 99), "Theirs")

    for goal_id in (archived, theirs, 123_456):
        problem_body(await client.post(PREVIEW, json={"goal_id": goal_id}), 404)


async def test_without_goals_and_items_a_preview_is_empty(client: httpx.AsyncClient) -> None:
    response = await client.post(PREVIEW, json={})

    assert response.status_code == 200
    assert response.json() == {"picks": [], "total_minutes": 0, "goal_title": None}


@pytest.mark.parametrize("minutes", [4, 601, "an hour", None])
async def test_a_budget_is_from_5_to_600_minutes(client: httpx.AsyncClient, minutes: Any) -> None:
    problem_body(await client.post(PREVIEW, json={"minutes": minutes}), 422)


async def test_the_day_is_the_seed_of_the_random_slot(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
    for _ in range(30):
        db.add(
            Item(
                owner_id=owner_id,
                status=ItemStatus.INBOX,
                enrich_state=EnrichState.READY,
                est_minutes=5,
                first_captured_at=LONG_AGO,
                last_captured_at=LONG_AGO,
            )
        )
    await db.flush()
    body = DigestPreviewRequest(minutes=5)  # room for the random slot only

    async def random_pick(now: datetime) -> int:
        preview = await item_views.preview_digest(db, owner_id, body, now=now)
        [pick] = preview.picks
        return pick.item.id

    one_day = {
        await random_pick(datetime(2026, 10, 18, hour, minute, tzinfo=UTC))
        for hour, minute in ((0, 0), (7, 0), (12, 30), (23, 59))
    }
    other_days = {
        await random_pick(datetime(2026, 11, day, 9, 0, tzinfo=UTC)) for day in range(1, 11)
    }

    assert len(one_day) == 1
    assert len(other_days) > 1
