"""From a saved message to an item. Each test name is one rule."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import CaptureSource, EnrichState, ItemStatus, JobKind, JobState
from unshelve.db.models import Capture, Item, Job
from unshelve.ingest import resolve

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
URL = "https://example.com/how-pgvector-works"


def _envelope(message_id: int = 7, **fields: object) -> CaptureEnvelope:
    values: dict[str, object] = {
        "source": CaptureSource.BOT,
        "chat_id": -1001234567890,
        "message_id": message_id,
        "posted_at": NOON,
        "text": f"Worth a read {URL}",
        "urls": [URL],
    }
    return CaptureEnvelope.model_validate(values | fields)


async def _count(db: AsyncSession, table: type[object]) -> int:
    return (await db.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()


async def test_a_new_message_is_stored_as_it_arrived(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    capture_id = await resolve.store_capture(db, owner_id, _envelope())

    capture = await db.get_one(Capture, capture_id)
    assert capture.source is CaptureSource.BOT
    assert (capture.chat_id, capture.message_id, capture.edit_seq) == (-1001234567890, 7, 0)
    assert capture.text == f"Worth a read {URL}"
    assert capture.urls == [URL]
    assert capture.posted_at == NOON
    assert capture.item_id is None


async def test_the_same_message_is_stored_once(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await resolve.store_capture(db, owner_id, _envelope())

    again = await resolve.store_capture(db, owner_id, _envelope(text="the text changed"))

    assert first is not None
    assert again is None
    assert await _count(db, Capture) == 1


async def test_an_edit_of_a_message_is_a_capture_of_its_own(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await resolve.store_capture(db, owner_id, _envelope())

    edited = await resolve.store_capture(db, owner_id, _envelope(edit_seq=1))

    assert edited is not None
    assert await _count(db, Capture) == 2


async def test_a_capture_gets_an_item_and_the_first_job(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    capture_id = await resolve.store_capture(db, owner_id, _envelope())
    assert capture_id is not None

    item_id = await resolve.resolve_capture(db, capture_id)

    item = await db.get_one(Item, item_id)
    assert item.owner_id == owner_id
    assert item.canonical_url == URL
    assert item.status is ItemStatus.INBOX
    assert item.enrich_state is EnrichState.PENDING
    assert (item.first_captured_at, item.last_captured_at) == (NOON, NOON)
    assert (await db.get_one(Capture, capture_id)).item_id == item_id
    job = (await db.execute(sa.select(Job))).scalar_one()
    assert (job.kind, job.state, job.owner_id) == (JobKind.FETCH_ITEM, JobState.PENDING, owner_id)
    assert job.payload == {"item_id": item_id}


async def test_a_note_without_a_link_gets_an_item_without_an_address(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    capture_id = await resolve.store_capture(
        db, owner_id, _envelope(text="Idea: compare RRF with a weighted sum", urls=[])
    )
    assert capture_id is not None

    item = await db.get_one(Item, await resolve.resolve_capture(db, capture_id))

    assert item.canonical_url is None


async def test_resolving_a_capture_again_changes_nothing(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    capture_id = await resolve.store_capture(db, owner_id, _envelope())
    assert capture_id is not None
    first = await resolve.resolve_capture(db, capture_id)

    second = await resolve.resolve_capture(db, capture_id)

    assert second == first
    assert await _count(db, Item) == 1
    assert await _count(db, Job) == 1


# --- The way in for the bot and the importer -------------------------------------------------


async def _jobs(db: AsyncSession) -> list[Job]:
    return list((await db.execute(sa.select(Job).order_by(Job.id))).scalars().all())


async def test_a_new_message_gets_a_job_and_a_repeat_gets_none(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    capture_id = await resolve.accept_capture(db, owner_id, _envelope())
    again = await resolve.accept_capture(db, owner_id, _envelope())

    assert capture_id is not None
    assert again is None
    (job,) = await _jobs(db)
    assert (job.kind, job.state) == (JobKind.INGEST_CAPTURE, JobState.PENDING)
    assert job.payload == {"capture_id": capture_id}
    assert job.run_after == job.created_at  # it can start at once


async def test_the_parts_of_an_album_share_one_job_that_starts_later(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    for message_id in (7, 8, 9):
        await resolve.accept_capture(
            db,
            owner_id,
            _envelope(message_id, text="", urls=[], media_group_id="14335384938541892"),
        )

    assert await _count(db, Capture) == 3
    (job,) = await _jobs(db)
    assert job.kind is JobKind.INGEST_ALBUM
    assert job.payload == {
        "owner_id": owner_id,
        "chat_id": -1001234567890,
        "media_group_id": "14335384938541892",
    }
    assert job.run_after - job.created_at == resolve.ALBUM_WAIT


async def test_an_edit_is_stored_and_gets_no_job(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await resolve.accept_capture(db, owner_id, _envelope())

    edit = await resolve.accept_capture(db, owner_id, _envelope(edit_seq=1791923347))

    assert edit is not None
    assert await _count(db, Capture) == 2
    assert [job.kind for job in await _jobs(db)] == [JobKind.INGEST_CAPTURE]


# --- An album ----------------------------------------------------------------------------------

ALBUM = "14335384938541892"
CHAT = -1001234567890


async def _album_part(db: AsyncSession, owner_id: int, message_id: int, **fields: object) -> None:
    values: dict[str, object] = {"text": "", "urls": list[str](), "media_group_id": ALBUM}
    values.update(fields)
    assert await resolve.store_capture(db, owner_id, _envelope(message_id, **values)) is not None


async def test_the_messages_of_an_album_become_one_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7, text=f"Slides from the talk {URL}", urls=[URL])
    await _album_part(db, owner_id, 8, posted_at=NOON + timedelta(seconds=1))
    await _album_part(db, owner_id, 9, posted_at=NOON + timedelta(seconds=2))

    item_id = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    assert item_id is not None
    assert await _count(db, Item) == 1
    item = await db.get_one(Item, item_id)
    assert item.canonical_url == URL
    assert item.first_captured_at == NOON
    assert item.last_captured_at == NOON + timedelta(seconds=2)
    linked = (await db.execute(sa.select(Capture.item_id).order_by(Capture.message_id))).scalars()
    assert list(linked) == [item_id, item_id, item_id]
    (job,) = await _jobs(db)
    assert (job.kind, job.payload) == (JobKind.FETCH_ITEM, {"item_id": item_id})


async def test_a_late_part_of_an_album_joins_the_item_that_exists(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7)
    await _album_part(db, owner_id, 8)
    first = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    await _album_part(db, owner_id, 9)  # it arrived after the job ran
    second = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    assert second == first
    assert await _count(db, Item) == 1
    assert await _count(db, Job) == 1
    linked = (await db.execute(sa.select(Capture.item_id))).scalars()
    assert set(linked) == {first}


async def test_an_album_of_a_different_chat_is_a_different_album(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7)
    await _album_part(db, owner_id, 7, chat_id=-1009999999999)

    await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    assert await _count(db, Item) == 1
    rows = await db.execute(sa.select(Capture.chat_id, Capture.item_id))
    assert {chat_id: item_id is not None for chat_id, item_id in rows} == {
        CHAT: True,
        -1009999999999: False,
    }


async def test_an_album_that_nobody_stored_is_nothing(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    assert await resolve.resolve_album(db, owner_id, CHAT, "no-such-album") is None


async def test_api_captures_get_message_ids_that_do_not_repeat(db: AsyncSession) -> None:
    first = await resolve.next_api_message_id(db)
    second = await resolve.next_api_message_id(db)

    assert second > first
