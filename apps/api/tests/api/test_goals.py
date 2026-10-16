"""Goals over HTTP: add, list, change, archive, focus. Each test name is one rule."""

# Persian titles: their letters look like Latin letters to the linter.
# ruff: noqa: RUF001

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.support import problem_body
from tests.conftest import ClientFactory
from unshelve.api import goal_views
from unshelve.api.deps import SESSION_COOKIE
from unshelve.api.errors import ConflictError
from unshelve.api.schemas import GoalCreate
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.db.models import Goal

GOALS = "/api/v1/goals"
OWNER_TELEGRAM_ID = 4242

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


async def _add(client: httpx.AsyncClient, title: str, **fields: Any) -> dict[str, Any]:
    response = await client.post(GOALS, json={"title": title, **fields})
    assert response.status_code == 201, response.text
    return response.json()


async def _change(client: httpx.AsyncClient, goal_id: int, **fields: Any) -> httpx.Response:
    return await client.patch(f"{GOALS}/{goal_id}", json=fields)


# --- Adding ------------------------------------------------------------------------------


async def test_a_new_goal_has_a_slug_from_its_title_and_the_first_version(
    client: httpx.AsyncClient,
) -> None:
    goal = await _add(
        client,
        "Rust & systems programming",
        description="Ownership, then data structures from scratch.",
        keywords=["rust", " borrow checker ", "rust"],
        starts_on="2026-11-01",
        ends_on="2027-01-31",
    )

    assert goal["slug"] == "rust-systems-programming"
    assert (goal["version"], goal["is_focus"], goal["archived_at"]) == (1, False, None)
    # Each keyword one time, without the spaces around it.
    assert goal["keywords"] == ["rust", "borrow checker"]
    assert (goal["starts_on"], goal["ends_on"]) == ("2026-11-01", "2027-01-31")


async def test_a_title_without_latin_letters_gets_a_slug_that_stays_the_same(
    client: httpx.AsyncClient,
) -> None:
    goal = await _add(client, "یادگیری سیستم‌ها")

    assert goal["slug"] == goal_views.slug_for("یادگیری   سیستم‌ها")
    assert goal["slug"].startswith("goal-")
    # The same title again is the same slug: a second click does not add a second goal.
    problem_body(await client.post(GOALS, json={"title": "یادگیری سیستم‌ها"}), 409)


def test_a_slug_keeps_letters_with_accents_and_stops_at_a_word() -> None:
    assert goal_views.slug_for("Café  Systems!") == "cafe-systems"
    long_slug = goal_views.slug_for("word " * 20)
    assert len(long_slug) <= 60
    assert not long_slug.endswith("-")


async def test_a_second_goal_with_the_same_title_is_a_409(client: httpx.AsyncClient) -> None:
    await _add(client, "GPU programming")

    response = await client.post(GOALS, json={"title": "GPU  Programming!"})

    detail = problem_body(response, 409)["detail"]
    assert detail == "a goal with this title exists already: 'GPU programming'"


async def test_the_title_of_an_archived_goal_is_taken_too(client: httpx.AsyncClient) -> None:
    # Restore the archived goal instead: its scores and labels stay with it.
    goal = await _add(client, "GPU programming")
    await _change(client, goal["id"], archived=True)

    detail = problem_body(await client.post(GOALS, json={"title": "GPU programming"}), 409)
    assert detail["detail"].startswith("an archived goal with this title exists already")


async def test_an_eleventh_active_goal_is_a_409_and_an_archived_goal_does_not_count(
    client: httpx.AsyncClient,
) -> None:
    goals = [await _add(client, f"Goal number {n}") for n in range(10)]

    full = await client.post(GOALS, json={"title": "One more"})
    await _change(client, goals[0]["id"], archived=True)
    after = await client.post(GOALS, json={"title": "One more"})

    assert problem_body(full, 409)["detail"] == (
        "you have 10 active goals, the most; archive one first"
    )
    assert after.status_code == 201


async def test_a_goal_that_ends_before_it_starts_is_a_422(client: httpx.AsyncClient) -> None:
    dates = {"starts_on": "2027-01-01", "ends_on": "2026-12-31"}
    problem_body(await client.post(GOALS, json={"title": "Backwards", **dates}), 422)

    goal = await _add(client, "Forwards", starts_on="2027-01-01")
    response = await _change(client, goal["id"], ends_on="2026-12-31")
    assert problem_body(response, 422)["detail"] == "the goal ends before it starts"


async def test_a_body_that_cannot_be_a_goal_is_a_422(client: httpx.AsyncClient) -> None:
    problem_body(await client.post(GOALS, json={"title": "   "}), 422)
    problem_body(await client.post(GOALS, json={"title": "x", "slug": "chosen"}), 422)
    problem_body(await client.post(GOALS, json={"title": "x" * 201}), 422)


# --- Listing -----------------------------------------------------------------------------


async def test_the_list_shows_the_active_goals_and_the_archived_ones_on_request(
    client: httpx.AsyncClient,
) -> None:
    first = await _add(client, "First")
    second = await _add(client, "Second")
    third = await _add(client, "Third")
    await _change(client, first["id"], archived=True)

    active = (await client.get(GOALS)).json()["items"]
    every = (await client.get(GOALS, params={"include_archived": "true"})).json()["items"]

    assert [goal["id"] for goal in active] == [second["id"], third["id"]]
    # The active goals first, then the archived ones.
    assert [goal["id"] for goal in every] == [second["id"], third["id"], first["id"]]


async def test_the_goals_of_another_owner_do_not_exist_for_you(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    async with committed_db() as db:
        theirs = await auth.ensure_owner(db, 999)
        goal = Goal(owner_id=theirs, slug="theirs", title="Theirs", version=1)
        db.add(goal)
        await db.commit()

    assert (await client.get(GOALS, params={"include_archived": "true"})).json()["items"] == []
    problem_body(await _change(client, goal.id, title="Mine now"), 404)
    problem_body(await client.post(f"{GOALS}/{goal.id}/focus"), 404)
    problem_body(await client.post(f"{GOALS}/999999/focus"), 404)


# --- Changing ----------------------------------------------------------------------------


async def test_a_new_meaning_gives_a_new_version_and_the_same_text_keeps_it(
    client: httpx.AsyncClient,
) -> None:
    goal = await _add(client, "Inference", keywords=["kv cache"])

    same = await _change(client, goal["id"], title="Inference", keywords=["kv cache", "kv cache"])
    dates = await _change(client, goal["id"], starts_on="2026-12-01")
    archived = await _change(client, goal["id"], archived=True)
    described = await _change(client, goal["id"], description="Serving LLMs fast.")
    keywords = await _change(client, goal["id"], keywords=["kv cache", "batching"])
    renamed = await _change(client, goal["id"], title="LLM inference")

    versions = [r.json()["version"] for r in (same, dates, archived, described, keywords, renamed)]
    # The scorer reads the title, the description and the keywords. The rest is no new meaning.
    assert versions == [1, 1, 1, 2, 3, 4]
    assert renamed.json()["slug"] == "inference"  # the slug never changes


async def test_fields_that_are_left_out_stay_and_a_null_date_is_removed(
    client: httpx.AsyncClient,
) -> None:
    goal = await _add(
        client, "Rust", description="Ownership.", starts_on="2026-11-01", ends_on="2027-01-31"
    )

    response = await _change(client, goal["id"], ends_on=None)

    changed = response.json()
    assert (changed["title"], changed["description"]) == ("Rust", "Ownership.")
    assert (changed["starts_on"], changed["ends_on"]) == ("2026-11-01", None)


@pytest.mark.parametrize("field", ["title", "description", "keywords", "archived"])
async def test_a_field_that_a_goal_must_have_cannot_be_null(
    client: httpx.AsyncClient, field: str
) -> None:
    goal = await _add(client, "Rust")

    response = await _change(client, goal["id"], **{field: None})

    assert problem_body(response, 422)["detail"] == (
        f"{field} cannot be null; leave it out to keep it"
    )


async def test_a_restored_goal_counts_again(client: httpx.AsyncClient) -> None:
    goals = [await _add(client, f"Goal number {n}") for n in range(10)]
    await _change(client, goals[0]["id"], archived=True)
    await _add(client, "The tenth active goal")

    response = await _change(client, goals[0]["id"], archived=False)

    assert problem_body(response, 409)["detail"] == (
        "you have 10 active goals, the most; archive one first"
    )


# --- The focus ---------------------------------------------------------------------------


async def test_the_focus_moves_from_one_goal_to_the_other(client: httpx.AsyncClient) -> None:
    first = await _add(client, "First")
    second = await _add(client, "Second")

    await client.post(f"{GOALS}/{first['id']}/focus")
    again = await client.post(f"{GOALS}/{first['id']}/focus")  # a second click is no error
    moved = await client.post(f"{GOALS}/{second['id']}/focus")

    assert (again.status_code, again.json()["is_focus"]) == (200, True)
    assert moved.json()["is_focus"] is True
    focus = [goal["id"] for goal in (await client.get(GOALS)).json()["items"] if goal["is_focus"]]
    assert focus == [second["id"]]


async def test_an_archived_goal_loses_the_focus_and_cannot_get_it(
    client: httpx.AsyncClient,
) -> None:
    goal = await _add(client, "First")
    await client.post(f"{GOALS}/{goal['id']}/focus")

    archived = await _change(client, goal["id"], archived=True)
    focus = await client.post(f"{GOALS}/{goal['id']}/focus")

    assert archived.json()["is_focus"] is False
    assert problem_body(focus, 409)["detail"] == (
        "an archived goal cannot be the focus; restore it first"
    )


# --- Two requests at the same moment -------------------------------------------------------


async def test_two_goals_added_into_the_last_place_fill_it_once(
    committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, 1)
        for n in range(9):
            db.add(Goal(owner_id=owner_id, slug=f"goal-number-{n}", title=f"Goal {n}", version=1))
        await db.commit()
    count = goal_views._active_goals  # pyright: ignore[reportPrivateUsage]
    the_first_add_counted = asyncio.Event()

    async def count_and_tell(session: AsyncSession, owner: int) -> int:
        found = await count(session, owner)
        the_first_add_counted.set()
        return found

    monkeypatch.setattr(goal_views, "_active_goals", count_and_tell)

    async def add(title: str, wait: bool) -> str:
        if wait:
            await the_first_add_counted.wait()
        async with committed_db() as db:
            try:
                await goal_views.create_goal(db, owner_id, GoalCreate(title=title))
            except ConflictError:
                return "409"
            if not wait:
                # The first add counted 9 and is slow before it saves. Without the lock of the
                # owner, the second add counts 9 too, and the owner gets 11 goals.
                await asyncio.sleep(0.3)
            await db.commit()
        return "201"

    answers = await asyncio.gather(add("Tenth", wait=False), add("Eleventh", wait=True))

    assert answers == ["201", "409"]
    async with committed_db() as db:
        assert await count(db, owner_id) == 10


async def test_two_focus_requests_at_the_same_moment_leave_one_focus(
    committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, 1)
        goals = [Goal(owner_id=owner_id, slug=name, title=name, version=1) for name in "ab"]
        db.add_all(goals)
        await db.commit()
    first, second = (goal.id for goal in goals)
    find = goal_views._goal  # pyright: ignore[reportPrivateUsage]
    the_first_request_found_its_goal = asyncio.Event()

    async def find_and_tell(session: AsyncSession, owner: int, goal_id: int) -> Goal:
        found = await find(session, owner, goal_id)
        the_first_request_found_its_goal.set()
        return found

    monkeypatch.setattr(goal_views, "_goal", find_and_tell)

    async def focus(goal_id: int, wait: bool) -> None:
        if wait:
            await the_first_request_found_its_goal.wait()
        async with committed_db() as db:
            await goal_views.focus_goal(db, owner_id, goal_id)
            if not wait:
                # Without the lock of the owner, the second request does not see this focus
                # yet. It sets its own, and the database refuses the second focus: an error.
                await asyncio.sleep(0.3)
            await db.commit()

    await asyncio.gather(focus(first, wait=False), focus(second, wait=True))

    async with committed_db() as db:
        focused = await db.scalars(sa.select(Goal.id).where(Goal.is_focus))
        assert list(focused) == [second]
