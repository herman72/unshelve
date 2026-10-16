"""Saving, listing and reading items over HTTP. Each test name is one rule."""

import asyncio
import base64
import json
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.api.support import problem_body
from tests.conftest import ClientFactory
from unshelve.api.deps import SESSION_COOKIE
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import Actor, CaptureSource, EnrichState, JobKind, JobState
from unshelve.db.models import Capture, Event, Goal, Item, ItemGoal, ItemTag, Job, Tag
from unshelve.ingest import resolve

ITEMS = "/api/v1/items"
URL = "https://example.com/how-pgvector-works"
TEXT = "A short guide to vector search in Postgres"
NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
OWNER_TELEGRAM_ID = 4242

Sessions = async_sessionmaker[AsyncSession]


async def _owner(sessions: Sessions, telegram_user_id: int = OWNER_TELEGRAM_ID) -> int:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, telegram_user_id)
        await db.commit()
    return owner_id


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


async def _saved(sessions: Sessions, owner_id: int, minutes: int = 0, **item_values: Any) -> int:
    """An item of ``owner_id`` that was saved ``minutes`` after noon. Returns its id."""
    async with sessions() as db:
        message_id = await resolve.next_api_message_id(db)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=message_id,
            posted_at=NOON + timedelta(minutes=minutes),
            # Each note has a text of its own. Two saves of the same note are one item.
            text=f"{TEXT} Note {message_id}.",
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        item_id = await resolve.resolve_capture(db, capture_id)
        if item_values:
            await db.execute(sa.update(Item).where(Item.id == item_id).values(**item_values))
        await db.commit()
    assert item_id is not None
    return item_id


def _ids(response: httpx.Response) -> list[int]:
    assert response.status_code == 200
    return [item["id"] for item in response.json()["items"]]


# --- Saving ------------------------------------------------------------------------------


async def test_saving_a_link_answers_with_the_new_item(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    response = await client.post(ITEMS, json={"url": URL, "text": TEXT})

    assert response.status_code == 201
    item = response.json()
    assert item["id"] == 1
    assert item["url"] == URL
    assert (item["status"], item["enrich_state"]) == ("inbox", "pending")
    assert (item["title"], item["summary"], item["est_minutes"]) == (None, None, None)
    assert (item["kind"], item["visibility"], item["partial"]) == ("unknown", "private", False)
    assert (item["tags"], item["goals"], item["top_goal"]) == ([], [], None)
    assert (item["key_points"], item["extra_links"]) == ([], [])
    assert item["capture_count"] == 1
    assert item["first_captured_at"] == item["last_captured_at"]
    assert item["allowed_transitions"] == ["queued", "reading", "done", "archived"]
    async with committed_db() as db:
        capture = (await db.execute(sa.select(Capture))).scalar_one()
        job = (await db.execute(sa.select(Job))).scalar_one()
    assert (capture.source, capture.chat_id, capture.item_id) == (CaptureSource.API, 0, 1)
    assert (capture.text, capture.urls) == (TEXT, [URL])
    assert (job.kind, job.state, job.payload) == (
        JobKind.FETCH_ITEM,
        JobState.PENDING,
        {"item_id": 1},
    )


async def test_saving_the_same_link_again_answers_with_the_item_that_exists(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    first = (await client.post(ITEMS, json={"url": URL})).json()

    # The same page: with "www.", with http, and with a parameter of a share button.
    again = URL.replace("https://", "http://www.") + "?utm_source=telegram"
    response = await client.post(ITEMS, json={"url": again, "text": "read this before Friday"})

    assert response.status_code == 201
    second = response.json()
    assert second["id"] == first["id"]
    assert second["url"] == URL
    assert second["capture_count"] == 2
    assert _ids(await client.get(ITEMS)) == [first["id"]]
    async with committed_db() as db:
        captures = (await db.execute(sa.select(Capture.urls, Capture.item_id))).all()
        jobs = (await db.execute(sa.select(Job.kind))).scalars().all()
    # The second message is kept as it arrived, with its own address.
    assert [(urls, item_id) for urls, item_id in captures] == [
        ([URL], first["id"]),
        ([again], first["id"]),
    ]
    assert jobs == [JobKind.FETCH_ITEM]


async def test_a_note_can_be_saved_without_a_link(client: httpx.AsyncClient) -> None:
    response = await client.post(ITEMS, json={"text": "Idea: compare RRF with a weighted sum"})

    assert response.status_code == 201
    assert response.json()["url"] is None


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="nothing"),
        pytest.param({"text": "   "}, id="only spaces"),
        pytest.param({"url": "ftp://example.com/file"}, id="not a web address"),
        pytest.param({"url": URL, "colour": "red"}, id="an unknown field"),
    ],
)
async def test_a_body_that_cannot_be_an_item_is_refused(
    client: httpx.AsyncClient, committed_db: Sessions, body: dict[str, str]
) -> None:
    response = await client.post(ITEMS, json=body)

    problem_body(response, 422)
    async with committed_db() as db:
        assert (await db.execute(sa.select(sa.func.count()).select_from(Capture))).scalar_one() == 0


# --- The list ----------------------------------------------------------------------------


async def test_a_saved_item_is_in_the_inbox(client: httpx.AsyncClient) -> None:
    created = (await client.post(ITEMS, json={"url": URL})).json()

    response = await client.get(ITEMS, params={"status": "inbox"})

    assert response.status_code == 200
    page = response.json()
    assert page["next_cursor"] is None
    assert [item["id"] for item in page["items"]] == [created["id"]]
    assert page["items"][0]["url"] == URL


async def test_the_list_is_newest_first_and_can_be_turned_around(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    first, second, third = [await _saved(committed_db, owner_id, minutes) for minutes in (0, 1, 2)]

    assert _ids(await client.get(ITEMS)) == [third, second, first]
    assert _ids(await client.get(ITEMS, params={"sort": "captured_asc"})) == [first, second, third]


async def test_the_list_comes_in_pages(client: httpx.AsyncClient, committed_db: Sessions) -> None:
    owner_id = await _owner(committed_db)
    # Three of the five items were saved in the same minute: the id decides their order.
    saved = [await _saved(committed_db, owner_id, minutes) for minutes in (0, 1, 1, 1, 2)]

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(3):
        params: dict[str, str | int] = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        page = (await client.get(ITEMS, params=params)).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]

    assert seen == sorted(saved, reverse=True)
    assert cursor is None


async def test_a_new_item_does_not_shift_the_next_page(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    one, two, three, four = [await _saved(committed_db, owner_id, m) for m in (0, 1, 2, 3)]
    first_page = (await client.get(ITEMS, params={"limit": 2})).json()
    assert [item["id"] for item in first_page["items"]] == [four, three]

    await _saved(committed_db, owner_id, minutes=4)  # arrives between the two requests
    second_page = await client.get(ITEMS, params={"limit": 2, "cursor": first_page["next_cursor"]})

    # "Skip the first two rows" would show item three a second time.
    assert _ids(second_page) == [two, one]


async def test_a_cursor_that_is_broken_or_from_a_different_sort_is_refused(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    for minutes in (0, 1):
        await _saved(committed_db, owner_id, minutes)
    cursor = (await client.get(ITEMS, params={"limit": 1})).json()["next_cursor"]

    broken = await client.get(ITEMS, params={"cursor": "not-a-cursor"})
    other_sort = await client.get(ITEMS, params={"cursor": cursor, "sort": "captured_asc"})

    assert problem_body(broken, 422)["detail"] == "the cursor is not valid"
    assert problem_body(other_sort, 422)["detail"] == "the cursor is not valid"


async def test_the_list_can_be_filtered(client: httpx.AsyncClient, committed_db: Sessions) -> None:
    owner_id = await _owner(committed_db)
    plain = await _saved(committed_db, owner_id, 0)
    queued = await _saved(committed_db, owner_id, 1, status="queued")
    video = await _saved(committed_db, owner_id, 2, kind="video")
    public = await _saved(
        committed_db, owner_id, 3, visibility="public", published_at=NOON, public_slug="pub-1"
    )

    assert _ids(await client.get(ITEMS, params={"status": "queued"})) == [queued]
    assert _ids(await client.get(ITEMS, params={"status": ["inbox", "queued"]})) == [
        public,
        video,
        queued,
        plain,
    ]
    assert _ids(await client.get(ITEMS, params={"kind": "video"})) == [video]
    assert _ids(await client.get(ITEMS, params={"visibility": "public"})) == [public]
    assert _ids(await client.get(ITEMS, params={"status": "queued", "kind": "video"})) == []


@pytest.mark.parametrize(
    "params",
    [{"tag": "databases"}, {"goal_id": 1}, {"min_score": 2}, {"sort": "relevance"}],
    ids=["tag", "goal", "score", "relevance"],
)
async def test_a_filter_that_is_not_built_says_so(
    client: httpx.AsyncClient, params: dict[str, str | int]
) -> None:
    response = await client.get(ITEMS, params=params)

    # Not a list that silently ignores the filter.
    problem_body(response, 501)


async def test_a_merged_copy_is_not_in_the_list(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    original = await _saved(committed_db, owner_id, 0)
    copy = await _saved(committed_db, owner_id, 1, duplicate_of_id=original)

    assert _ids(await client.get(ITEMS)) == [original]
    assert (await client.get(f"{ITEMS}/{copy}")).json()["duplicate_of_id"] == original


# --- One item ----------------------------------------------------------------------------


async def test_one_item_can_be_read_in_full(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    item_id = await _saved(
        committed_db,
        owner_id,
        title="Vector search in Postgres",
        summary="How pgvector works.",
        key_points=["Vectors are a column type"],
        note="read before the interview",
        status="reading",
    )

    response = await client.get(f"{ITEMS}/{item_id}")

    assert response.status_code == 200
    item = response.json()
    assert (item["id"], item["title"]) == (item_id, "Vector search in Postgres")
    assert (item["summary"], item["key_points"]) == (
        "How pgvector works.",
        ["Vectors are a column type"],
    )
    assert item["note"] == "read before the interview"
    assert item["allowed_transitions"] == ["queued", "done", "archived"]


async def test_an_item_that_does_not_exist_is_a_404(client: httpx.AsyncClient) -> None:
    missing = await client.get(f"{ITEMS}/999")
    not_an_id = await client.get(f"{ITEMS}/0")

    assert problem_body(missing, 404)["detail"] == "no such item"
    problem_body(not_an_id, 422)


async def test_the_items_of_a_different_owner_do_not_exist_for_you(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    mine = await _saved(committed_db, await _owner(committed_db))
    theirs = await _saved(committed_db, await _owner(committed_db, telegram_user_id=999), 1)

    assert _ids(await client.get(ITEMS)) == [mine]
    problem_body(await client.get(f"{ITEMS}/{theirs}"), 404)


async def test_tags_and_goal_scores_are_part_of_an_item(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    item_id = await _saved(committed_db, owner_id)
    async with committed_db() as db:
        tags = [
            Tag(owner_id=owner_id, slug="postgres", name="Postgres", is_public=True),
            Tag(owner_id=owner_id, slug="career", name="Career", is_public=False),
        ]
        goals = [
            Goal(owner_id=owner_id, slug="rag", title="Build a RAG system"),
            Goal(owner_id=owner_id, slug="interviews", title="Prepare for interviews"),
        ]
        db.add_all([*tags, *goals])
        await db.flush()
        db.add_all(ItemTag(item_id=item_id, tag_id=tag.id, owner_id=owner_id) for tag in tags)
        for goal, score in zip(goals, (3, 1), strict=True):
            db.add(
                ItemGoal(
                    item_id=item_id,
                    goal_id=goal.id,
                    owner_id=owner_id,
                    score=score,
                    reason=f"score {score}",
                    scorer_version="1",
                    goal_version=1,
                )
            )
        await db.commit()

    listed = (await client.get(ITEMS)).json()["items"][0]
    detail = (await client.get(f"{ITEMS}/{item_id}")).json()

    assert [tag["slug"] for tag in listed["tags"]] == ["career", "postgres"]
    assert listed["tags"][1] == {"slug": "postgres", "name": "Postgres", "is_public": True}
    assert listed["top_goal"]["goal_slug"] == "rag"
    assert listed["top_goal"]["score"] == 3
    assert [(goal["goal_slug"], goal["score"]) for goal in detail["goals"]] == [
        ("rag", 3),
        ("interviews", 1),
    ]
    assert detail["top_goal"] == detail["goals"][0]
    assert detail["goals"][0]["label"] is None


# --- Starting the pipeline again ---------------------------------------------------------


async def _at(sessions: Sessions, item_id: int, state: EnrichState) -> None:
    """The item with its pipeline at ``state``, and no job waiting for it."""
    async with sessions() as db:
        await db.execute(sa.update(Item).where(Item.id == item_id).values(enrich_state=state))
        await db.execute(sa.delete(Job))
        await db.commit()


async def _waiting_fetch_jobs(sessions: Sessions, item_id: int) -> int:
    async with sessions() as db:
        found = await db.scalar(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(
                Job.kind == JobKind.FETCH_ITEM,
                Job.state == JobState.PENDING,
                Job.dedupe_key == f"item:{item_id}",
            )
        )
    return found or 0


async def _events(sessions: Sessions, item_id: int) -> list[Event]:
    async with sessions() as db:
        found = await db.scalars(sa.select(Event).where(Event.item_id == item_id))
        return list(found.all())


@pytest.mark.parametrize("state", [EnrichState.READY, EnrichState.FAILED])
async def test_a_finished_or_failed_item_goes_back_to_the_start(
    client: httpx.AsyncClient, committed_db: Sessions, state: EnrichState
) -> None:
    item_id = await _saved(committed_db, await _owner(committed_db))
    await _at(committed_db, item_id, state)

    response = await client.post(f"{ITEMS}/{item_id}/reprocess")

    assert response.status_code == 202
    assert (response.json()["id"], response.json()["enrich_state"]) == (item_id, "pending")
    # The first step waits for the worker; the answer does not wait for the work.
    assert await _waiting_fetch_jobs(committed_db, item_id) == 1
    [event] = await _events(committed_db, item_id)
    assert (event.actor, event.type, event.payload) == (
        Actor.USER,
        "reprocess",
        {"enrich_state": state.value},
    )


@pytest.mark.parametrize("state", [EnrichState.PENDING, EnrichState.FETCHED, EnrichState.ENRICHED])
async def test_an_item_in_the_pipeline_cannot_go_back_to_the_start(
    client: httpx.AsyncClient, committed_db: Sessions, state: EnrichState
) -> None:
    item_id = await _saved(committed_db, await _owner(committed_db))
    await _at(committed_db, item_id, state)

    response = await client.post(f"{ITEMS}/{item_id}/reprocess")

    detail = problem_body(response, 409)["detail"]
    assert detail == f"the item is still in the pipeline ({state.value})"
    assert await _waiting_fetch_jobs(committed_db, item_id) == 0
    assert await _events(committed_db, item_id) == []


async def test_a_second_request_finds_the_item_in_the_pipeline(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    # A double click: one start, one job.
    item_id = await _saved(committed_db, await _owner(committed_db))
    await _at(committed_db, item_id, EnrichState.READY)

    first = await client.post(f"{ITEMS}/{item_id}/reprocess")
    second = await client.post(f"{ITEMS}/{item_id}/reprocess")

    assert (first.status_code, second.status_code) == (202, 409)
    assert await _waiting_fetch_jobs(committed_db, item_id) == 1


async def test_two_requests_at_the_same_moment_start_the_item_once(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    # The second request waits for the lock on the row of the item, then finds it pending.
    item_id = await _saved(committed_db, await _owner(committed_db))
    await _at(committed_db, item_id, EnrichState.READY)

    answers = await asyncio.gather(
        client.post(f"{ITEMS}/{item_id}/reprocess"),
        client.post(f"{ITEMS}/{item_id}/reprocess"),
    )

    assert sorted(answer.status_code for answer in answers) == [202, 409]
    assert len(await _events(committed_db, item_id)) == 1


async def test_an_item_that_is_not_yours_cannot_be_sent_back(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    theirs = await _saved(committed_db, await _owner(committed_db, telegram_user_id=999))
    await _at(committed_db, theirs, EnrichState.READY)

    problem_body(await client.post(f"{ITEMS}/{theirs}/reprocess"), 404)
    problem_body(await client.post(f"{ITEMS}/999/reprocess"), 404)
    assert await _waiting_fetch_jobs(committed_db, theirs) == 0


# --- The reading status ------------------------------------------------------------------


async def _move(client: httpx.AsyncClient, item_id: int, target: str) -> httpx.Response:
    return await client.post(f"{ITEMS}/{item_id}/transition", json={"to": target})


async def test_a_move_answers_with_the_item_in_its_new_status(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    item_id = await _saved(committed_db, await _owner(committed_db))

    response = await _move(client, item_id, "queued")

    assert response.status_code == 200
    item = response.json()
    assert (item["id"], item["status"]) == (item_id, "queued")
    # The buttons of the page come from here: the moves of the table, in the order of the enum.
    assert item["allowed_transitions"] == ["inbox", "reading", "done", "archived"]
    [event] = await _events(committed_db, item_id)
    assert (event.actor, event.type, event.from_status, event.to_status, event.payload) == (
        Actor.USER,
        "status",
        "inbox",
        "queued",
        {"via": "api"},
    )


async def test_the_status_that_the_item_has_already_is_no_error(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    # A double click, or a second tab: the item is where the request wants it.
    item_id = await _saved(committed_db, await _owner(committed_db))

    first = await _move(client, item_id, "done")
    second = await _move(client, item_id, "done")

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["status"] == "done"
    assert len(await _events(committed_db, item_id)) == 1


async def test_two_moves_at_the_same_moment_write_one_event(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    # The second request waits for the lock on the row of the item, then finds it done.
    item_id = await _saved(committed_db, await _owner(committed_db))

    answers = await asyncio.gather(_move(client, item_id, "done"), _move(client, item_id, "done"))

    assert [answer.status_code for answer in answers] == [200, 200]
    assert len(await _events(committed_db, item_id)) == 1


async def test_a_move_that_the_table_does_not_have_is_a_409_with_both_statuses(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    item_id = await _saved(committed_db, await _owner(committed_db), status="archived")

    response = await _move(client, item_id, "done")

    assert problem_body(response, 409)["detail"] == "cannot move from 'archived' to 'done'"
    assert await _events(committed_db, item_id) == []


async def test_a_move_into_a_full_queue_is_a_409_that_says_how_full_it_is(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    for minutes in range(5):
        await _saved(committed_db, owner_id, minutes, status="queued")
    item_id = await _saved(committed_db, owner_id, 10)

    response = await _move(client, item_id, "reading")

    assert problem_body(response, 409)["detail"] == (
        "the reading queue is full: all 5 slots are used; move an item out of it first"
    )
    assert (await client.get(f"{ITEMS}/{item_id}")).json()["status"] == "inbox"


async def test_a_merged_copy_cannot_move_and_the_answer_names_the_item_to_move(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    older = await _saved(committed_db, owner_id)
    copy = await _saved(committed_db, owner_id, 1, duplicate_of_id=older)

    response = await _move(client, copy, "queued")

    assert problem_body(response, 409)["detail"] == (
        f"the item is the same thing as item {older}: change that one"
    )


async def test_a_merged_copy_offers_no_moves(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    # The page shows a button for each allowed move, so a merged copy shows none.
    owner_id = await _owner(committed_db)
    older = await _saved(committed_db, owner_id)
    copy = await _saved(committed_db, owner_id, 1, duplicate_of_id=older)

    assert (await client.get(f"{ITEMS}/{copy}")).json()["allowed_transitions"] == []
    assert (await client.get(f"{ITEMS}/{older}")).json()["allowed_transitions"] == [
        "queued",
        "reading",
        "done",
        "archived",
    ]


async def test_a_status_that_does_not_exist_is_a_422(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    item_id = await _saved(committed_db, await _owner(committed_db))

    problem_body(await _move(client, item_id, "later"), 422)
    problem_body(await client.post(f"{ITEMS}/{item_id}/transition", json={}), 422)


async def test_an_item_that_is_not_yours_cannot_move(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    theirs = await _saved(committed_db, await _owner(committed_db, telegram_user_id=999))

    problem_body(await _move(client, theirs, "done"), 404)
    problem_body(await _move(client, 999_999, "done"), 404)
    assert await _events(committed_db, theirs) == []


# --- The history of an item --------------------------------------------------------------


async def test_the_history_of_an_item_is_newest_first(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    item_id = await _saved(committed_db, await _owner(committed_db))
    for target in ("queued", "reading", "done"):
        assert (await _move(client, item_id, target)).status_code == 200

    response = await client.get(f"{ITEMS}/{item_id}/events")

    assert response.status_code == 200
    page = response.json()
    assert page["next_cursor"] is None
    assert [(event["from_status"], event["to_status"]) for event in page["items"]] == [
        ("reading", "done"),
        ("queued", "reading"),
        ("inbox", "queued"),
    ]
    newest = page["items"][0]
    assert (newest["actor"], newest["type"], newest["payload"]) == (
        "user",
        "status",
        {"via": "api"},
    )
    assert datetime.fromisoformat(newest["created_at"]).tzinfo is not None


async def test_the_history_comes_in_pages(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    item_id = await _saved(committed_db, await _owner(committed_db))
    for target in ("queued", "reading", "done"):
        await _move(client, item_id, target)

    first = (await client.get(f"{ITEMS}/{item_id}/events", params={"limit": 2})).json()
    rest = (
        await client.get(
            f"{ITEMS}/{item_id}/events", params={"limit": 2, "cursor": first["next_cursor"]}
        )
    ).json()

    assert [event["to_status"] for event in first["items"]] == ["done", "reading"]
    assert [event["to_status"] for event in rest["items"]] == ["queued"]
    assert rest["next_cursor"] is None


async def test_a_cursor_of_the_history_that_is_broken_or_from_the_list_is_refused(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    item_id = await _saved(committed_db, owner_id)
    await _saved(committed_db, owner_id, 1)
    list_cursor = (await client.get(ITEMS, params={"limit": 1})).json()["next_cursor"]
    # The form of a cursor of the history, with another kind: a cursor of a later page type.
    other_kind = base64.urlsafe_b64encode(json.dumps(["digests", 1]).encode()).decode()

    for cursor in ("not-a-cursor", list_cursor, other_kind):
        response = await client.get(f"{ITEMS}/{item_id}/events", params={"cursor": cursor})
        assert problem_body(response, 422)["detail"] == "the cursor is not valid"


async def test_the_history_of_an_item_that_is_not_yours_is_a_404(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    theirs = await _saved(committed_db, await _owner(committed_db, telegram_user_id=999))
    async with committed_db() as db:
        db.add(
            Event(
                owner_id=await _owner(committed_db, 999),
                item_id=theirs,
                actor=Actor.USER,
                type="status",
                from_status="inbox",
                to_status="done",
                payload={},
            )
        )
        await db.commit()

    problem_body(await client.get(f"{ITEMS}/{theirs}/events"), 404)
    problem_body(await client.get(f"{ITEMS}/999999/events"), 404)


# --- The reading queue -------------------------------------------------------------------

QUEUE = "/api/v1/queue"


async def test_an_empty_queue_has_all_its_slots_free(client: httpx.AsyncClient) -> None:
    response = await client.get(QUEUE)

    assert response.status_code == 200
    assert response.json() == {"limit": 5, "used": 0, "items": []}


async def test_the_queue_lists_the_items_being_read_then_the_items_that_wait_longest(
    client: httpx.AsyncClient, committed_db: Sessions
) -> None:
    owner_id = await _owner(committed_db)
    first, second, third, finished = [await _saved(committed_db, owner_id, m) for m in range(4)]
    # The order of the moves, not the order of saving, is the order of the queue.
    for item_id, target in ((second, "queued"), (first, "queued"), (third, "reading")):
        assert (await _move(client, item_id, target)).status_code == 200
    assert (await _move(client, finished, "done")).status_code == 200
    # Queued before the log existed: no move in the log, so it counts as the oldest.
    old = await _saved(committed_db, owner_id, 5, status="queued")
    await _saved(committed_db, owner_id, 9, status="queued", duplicate_of_id=first)
    await _saved(committed_db, await _owner(committed_db, telegram_user_id=999), status="queued")

    queue = (await client.get(QUEUE)).json()

    assert (queue["limit"], queue["used"]) == (5, 4)
    assert [(item["id"], item["status"]) for item in queue["items"]] == [
        (third, "reading"),
        (old, "queued"),
        (second, "queued"),
        (first, "queued"),
    ]
