"""From a saved message to an item. Each test name is one rule."""

from datetime import UTC, datetime

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


async def test_api_captures_get_message_ids_that_do_not_repeat(db: AsyncSession) -> None:
    first = await resolve.next_api_message_id(db)
    second = await resolve.next_api_message_id(db)

    assert second > first
