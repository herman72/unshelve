"""Publishing an item over HTTP: the preview of its public record, then the record itself
(docs/learn/45). Each test name is one rule."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta, timezone
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
from unshelve.contracts.enums import (
    Actor,
    CaptureSource,
    EnrichState,
    ItemKind,
    JobKind,
    JobState,
    Visibility,
)
from unshelve.db.models import Event, Item, ItemTag, Job, Tag
from unshelve.ingest import resolve
from unshelve.publish import feed
from unshelve.publish.visibility import PUBLISH_DELAY

ITEMS = "/api/v1/items"
OWNER_TELEGRAM_ID = 4242
TITLE = "Leases for Job Queues in Postgres"
SLUG = "leases-for-job-queues-in-postgres"
PRIVATE_NOTE = "LEAK-private-note"

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
async def owner_id(committed_db: Sessions) -> int:
    async with committed_db() as db:
        owner = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        await db.commit()
    return owner


@pytest.fixture
async def client(
    client_for: ClientFactory, private_database_url: str, committed_db: Sessions, owner_id: int
) -> AsyncGenerator[httpx.AsyncClient]:
    """A client that is signed in as the owner."""
    async with committed_db() as db:
        secret = await auth.open_session(db, owner_id)
        await db.commit()
    async with client_for(Settings(env="test", database_url=private_database_url)) as client:
        client.cookies.set(SESSION_COOKIE, secret)
        yield client


async def _item(sessions: Sessions, owner_id: int, **values: Any) -> int:
    """A ready item of ``owner_id``, with a public tag and a private tag. Returns its id."""
    async with sessions() as db:
        message_id = await resolve.next_api_message_id(db)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=message_id,
            posted_at=datetime(2026, 10, 25, 9, 0, tzinfo=UTC),
            # A text of its own: two saves of the same text are one item.
            text=f"A made-up page about leases. Note {message_id}.",
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        item_id = await resolve.resolve_capture(db, capture_id)
        assert item_id is not None
        fields: dict[str, Any] = {
            "enrich_state": EnrichState.READY,
            "kind": ItemKind.ARTICLE,
            "title": TITLE,
            "canonical_url": "https://example.com/leases",
            "author": "A. Made-up",
            "site": "example.com",
            "lang": "en",
            "summary": "A job queue on one Postgres table, made safe with leases.",
            "key_points": ["A lease ends when its worker stops"],
            "est_minutes": 14,
            "public_note": "Worth a read before the design review.",
            "note": PRIVATE_NOTE,
            "extra_links": ["https://leak.example/extra-link"],
        } | values
        await db.execute(sa.update(Item).where(Item.id == item_id).values(**fields))
        for slug, public in (("databases", True), ("leak-private-tag", False)):
            tag = await db.scalar(sa.select(Tag).where(Tag.owner_id == owner_id, Tag.slug == slug))
            if tag is None:
                tag = Tag(owner_id=owner_id, slug=slug, name=slug, is_public=public)
                db.add(tag)
                await db.flush()
            db.add(ItemTag(item_id=item_id, tag_id=tag.id, owner_id=owner_id))
        await db.commit()
    return item_id


async def _preview(client: httpx.AsyncClient, item_id: int) -> dict[str, Any]:
    response = await client.get(f"{ITEMS}/{item_id}/public-preview")
    assert response.status_code == 200, response.text
    record: dict[str, Any] = response.json()
    return record


async def _publish(client: httpx.AsyncClient, item_id: int, record: Any) -> httpx.Response:
    return await client.put(
        f"{ITEMS}/{item_id}/visibility", json={"visibility": "public", "record": record}
    )


async def _make_private(client: httpx.AsyncClient, item_id: int) -> httpx.Response:
    return await client.put(f"{ITEMS}/{item_id}/visibility", json={"visibility": "private"})


async def _row(sessions: Sessions, item_id: int) -> Item:
    async with sessions() as db:
        item = await db.get(Item, item_id)
        assert item is not None
        return item


async def _events(sessions: Sessions, item_id: int) -> list[Event]:
    async with sessions() as db:
        found = await db.scalars(
            sa.select(Event)
            .where(Event.item_id == item_id, Event.type == "visibility")
            .order_by(Event.id)
        )
        return list(found.all())


async def _publish_jobs(sessions: Sessions) -> list[Job]:
    async with sessions() as db:
        found = await db.scalars(
            sa.select(Job).where(Job.kind == JobKind.PUBLISH_FEED, Job.state == JobState.PENDING)
        )
        return list(found.all())


# --- The acceptance test of #37 ----------------------------------------------------------


async def test_the_preview_is_the_published_record_byte_for_byte(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)

    preview = await _preview(client, item_id)
    response = await _publish(client, item_id, preview)

    assert response.status_code == 200, response.text
    # The feed that the publish job will write holds exactly the record of the preview.
    built = await feed.build_feed(committed_db, owner_id)
    assert built.data == feed.feed_bytes([{name: preview[name] for name in feed.FIELDS}])


# --- The preview -------------------------------------------------------------------------


async def test_a_preview_shows_the_public_fields_and_nothing_private(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)

    preview = await _preview(client, item_id)

    assert list(preview) == list(feed.FIELDS)
    assert preview["id"] == SLUG
    assert preview["note"] == "Worth a read before the design review."
    assert preview["tags"] == ["databases"]
    text = str(preview)
    assert PRIVATE_NOTE not in text
    assert "leak" not in text


async def test_a_preview_is_the_record_of_the_item_also_among_other_public_items(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    # The other item comes first in the feed: its time is later.
    later = datetime.now(UTC) + timedelta(days=1)
    await _item(
        committed_db,
        owner_id,
        title="Retries without duplicates",
        visibility=Visibility.PUBLIC,
        public_slug="retries-without-duplicates",
        published_at=later,
    )
    item_id = await _item(committed_db, owner_id)

    assert (await _preview(client, item_id))["id"] == SLUG


async def test_a_preview_saves_nothing(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)

    await _preview(client, item_id)

    item = await _row(committed_db, item_id)
    assert (item.visibility, item.public_slug, item.published_at) == (
        Visibility.PRIVATE,
        None,
        None,
    )
    assert await _events(committed_db, item_id) == []
    assert await _publish_jobs(committed_db) == []
    assert (await feed.build_feed(committed_db, owner_id)).items == 0


async def test_a_new_publication_is_dated_now_to_the_second(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    before = datetime.now(UTC).replace(microsecond=0)

    preview = await _preview(client, item_id)

    published = datetime.strptime(preview["published_at"], "%Y-%m-%dT%H:%M:%S%z")
    assert before <= published <= datetime.now(UTC)
    assert preview["published_at"].endswith("Z")


async def test_a_name_that_another_item_has_gets_a_number(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    await _item(committed_db, owner_id, public_slug=SLUG)
    item_id = await _item(committed_db, owner_id)

    assert (await _preview(client, item_id))["id"] == f"{SLUG}-2"


async def test_a_persian_title_gives_a_persian_name(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id, title="صف کارها در پستگرس", lang="fa")

    assert (await _preview(client, item_id))["id"] == "صف-کارها-در-پستگرس"


# --- Publishing ----------------------------------------------------------------------------


async def test_publishing_writes_the_item_an_event_and_one_publish_job(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)

    response = await _publish(client, item_id, preview)

    assert response.json() == {"visibility": "public", "public": preview}
    item = await _row(committed_db, item_id)
    assert (item.visibility, item.public_slug) == (Visibility.PUBLIC, SLUG)
    assert item.published_at is not None
    assert feed.time_text(item.published_at) == preview["published_at"]
    [event] = await _events(committed_db, item_id)
    assert (event.actor, event.payload) == (Actor.USER, {"visibility": "public", "slug": SLUG})
    [job] = await _publish_jobs(committed_db)
    assert (job.payload, job.dedupe_key) == ({"owner_id": owner_id}, f"feed:{owner_id}")
    # The job waits, so that the changes of the next half minute go out with it.
    assert job.run_after >= item.published_at + PUBLISH_DELAY


async def test_a_record_that_changed_after_the_preview_is_a_409_and_nothing_changes(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)
    # The worker writes a new summary between the preview and the click.
    async with committed_db() as db:
        await db.execute(sa.update(Item).where(Item.id == item_id).values(summary="A new summary."))
        await db.commit()

    response = await _publish(client, item_id, preview)

    detail = problem_body(response, 409)["detail"]
    assert detail == "the public record changed after the preview: look at the new preview"
    assert (await _row(committed_db, item_id)).visibility == Visibility.PRIVATE
    assert await _events(committed_db, item_id) == []
    assert await _publish_jobs(committed_db) == []


@pytest.mark.parametrize("shift", [timedelta(hours=-2), timedelta(hours=2)])
async def test_a_preview_older_than_an_hour_or_from_the_future_is_a_409(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int, shift: timedelta
) -> None:
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)
    preview["published_at"] = feed.time_text(datetime.now(UTC) + shift)

    response = await _publish(client, item_id, preview)

    assert problem_body(response, 409)["detail"] == (
        "the preview is more than an hour old: look at a new preview"
    )
    assert (await _row(committed_db, item_id)).visibility == Visibility.PRIVATE


async def test_the_same_time_in_another_time_zone_is_the_same_record(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)
    utc = datetime.strptime(preview["published_at"], "%Y-%m-%dT%H:%M:%S%z")
    tehran = utc.astimezone(timezone(timedelta(hours=3, minutes=30)))

    response = await _publish(client, item_id, preview | {"published_at": tehran.isoformat()})

    assert response.status_code == 200, response.text
    assert response.json()["public"] == preview


async def test_publishing_without_the_record_of_the_preview_is_a_422(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)

    response = await client.put(f"{ITEMS}/{item_id}/visibility", json={"visibility": "public"})

    assert response.status_code == 422
    assert (await _row(committed_db, item_id)).visibility == Visibility.PRIVATE


async def test_a_name_that_is_not_a_name_is_a_409(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)

    response = await _publish(client, item_id, preview | {"id": "../feed"})

    assert problem_body(response, 409)["detail"] == (
        "the public name of the preview is not a name of letters, digits and hyphens"
    )


async def test_a_second_click_on_publish_changes_nothing(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)

    first = await _publish(client, item_id, preview)
    second = await _publish(client, item_id, preview)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert len(await _events(committed_db, item_id)) == 1


async def test_two_clicks_at_the_same_moment_write_one_event(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    # The second request waits for the lock on the row, then finds the item public.
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)

    answers = await asyncio.gather(
        _publish(client, item_id, preview), _publish(client, item_id, preview)
    )

    assert [answer.status_code for answer in answers] == [200, 200]
    assert len(await _events(committed_db, item_id)) == 1


async def test_changes_in_a_row_wait_as_one_publish_job(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    first = await _item(committed_db, owner_id)
    second = await _item(committed_db, owner_id, title="Retries without duplicates")

    await _publish(client, first, await _preview(client, first))
    await _publish(client, second, await _preview(client, second))
    await _make_private(client, first)

    assert len(await _publish_jobs(committed_db)) == 1


async def test_a_copy_cannot_be_published_and_the_answer_names_the_original(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    original = await _item(committed_db, owner_id)
    copy = await _item(committed_db, owner_id, duplicate_of_id=original)
    preview = await _preview(client, original)

    answers = [
        await client.get(f"{ITEMS}/{copy}/public-preview"),
        await _publish(client, copy, preview),
    ]

    for answer in answers:
        assert problem_body(answer, 409)["detail"] == (
            f"the item is a copy of item {original}: publish that one"
        )
    assert (await _row(committed_db, copy)).visibility == Visibility.PRIVATE


async def test_an_item_of_a_different_owner_does_not_exist_for_you(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    async with committed_db() as db:
        someone = await auth.ensure_owner(db, 999)
        await db.commit()
    theirs = await _item(committed_db, someone)
    preview = await _preview(client, await _item(committed_db, owner_id))

    answers = [
        await client.get(f"{ITEMS}/{theirs}/public-preview"),
        await _publish(client, theirs, preview),
        await _make_private(client, theirs),
    ]

    assert [answer.status_code for answer in answers] == [404, 404, 404]
    assert (await _row(committed_db, theirs)).visibility == Visibility.PRIVATE


# --- Making an item private again ----------------------------------------------------------


async def test_a_private_item_leaves_the_feed_and_keeps_its_name_and_time(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    preview = await _preview(client, item_id)
    await _publish(client, item_id, preview)
    async with committed_db() as db:
        await db.execute(sa.delete(Job))  # the job of the publication ran
        await db.commit()

    response = await _make_private(client, item_id)

    assert response.json() == {"visibility": "private", "public": None}
    assert len(await _publish_jobs(committed_db)) == 1
    item = await _row(committed_db, item_id)
    assert (item.visibility, item.public_slug) == (Visibility.PRIVATE, SLUG)
    assert (await feed.build_feed(committed_db, owner_id)).items == 0
    assert [event.payload for event in await _events(committed_db, item_id)] == [
        {"visibility": "public", "slug": SLUG},
        {"visibility": "private"},
    ]


async def test_published_again_an_item_comes_back_with_its_first_name_and_time(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)
    first = await _preview(client, item_id)
    await _publish(client, item_id, first)
    await _make_private(client, item_id)
    # A different item takes nothing from it: its name stays its own.
    await _item(committed_db, owner_id)

    again = await _preview(client, item_id)

    assert (again["id"], again["published_at"]) == (first["id"], first["published_at"])
    assert (await _publish(client, item_id, again)).status_code == 200


async def test_a_second_click_on_private_changes_nothing(
    client: httpx.AsyncClient, committed_db: Sessions, owner_id: int
) -> None:
    item_id = await _item(committed_db, owner_id)

    response = await _make_private(client, item_id)

    assert response.json() == {"visibility": "private", "public": None}
    assert await _events(committed_db, item_id) == []
    assert await _publish_jobs(committed_db) == []
