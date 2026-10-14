"""The jobs that give stored messages their items. Each test name is one rule.

The messages arrive the way the bot delivers them: stored, with a job that waits.
"""

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope, MediaRef
from unshelve.contracts.enums import CaptureSource, EnrichState, JobKind, JobState
from unshelve.contracts.jobs import IngestCapturePayload, Services
from unshelve.db.models import Capture, Item, ItemContent, Job
from unshelve.ingest import resolve
from unshelve.queue import store as queue
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
CHAT = -1001234567890
ALBUM = "14335384938541892"

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    settings = Settings(env="test", database_url=private_database_url)
    return build_services(settings, committed_db)


def _worker(services: Services) -> Worker:
    discover()
    return Worker(services, registry, WorkerConfig(worker_id="w1"))


def _post(message_id: int, **fields: object) -> CaptureEnvelope:
    values: dict[str, object] = {
        "source": CaptureSource.BOT,
        "chat_id": CHAT,
        "message_id": message_id,
        "posted_at": NOON,
    }
    return CaptureEnvelope.model_validate(values | fields)


def _photo(name: str) -> MediaRef:
    return MediaRef(kind="photo", file_id=f"id-{name}", file_unique_id=f"unique-{name}")


async def _arrive(sessions: Sessions, *envelopes: CaptureEnvelope) -> None:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        for envelope in envelopes:
            assert await resolve.accept_capture(db, owner_id, envelope) is not None
        await db.commit()


async def _the_album_wait_is_over(sessions: Sessions) -> None:
    async with sessions() as db:
        await db.execute(
            sa.update(Job).where(Job.state == JobState.PENDING).values(run_after=sa.func.now())
        )
        await db.commit()


async def _run_all(worker: Worker) -> int:
    steps = 0
    while await worker.run_one():
        steps += 1
    return steps


async def _items(sessions: Sessions) -> list[Item]:
    async with sessions() as db:
        return list((await db.execute(sa.select(Item).order_by(Item.id))).scalars().all())


def test_the_worker_knows_the_two_ingest_jobs() -> None:
    discover()

    assert {JobKind.INGEST_CAPTURE, JobKind.INGEST_ALBUM} <= set(registry.kinds())


async def test_a_stored_message_becomes_a_finished_item(services: Services) -> None:
    await _arrive(
        services.sessions,
        _post(10, text="https://example.com/a-talk", urls=["https://example.com/a-talk"]),
    )

    steps = await _run_all(_worker(services))

    assert steps == 4  # ingest, fetch, enrich, embed
    (item,) = await _items(services.sessions)
    assert item.enrich_state is EnrichState.READY
    assert item.canonical_url == "https://example.com/a-talk"
    assert item.first_captured_at == NOON


async def test_two_posts_with_the_same_link_become_one_item(services: Services) -> None:
    await _arrive(
        services.sessions,
        _post(10, text="https://example.com/a-talk", urls=["https://example.com/a-talk"]),
        _post(
            11,
            text="again: https://www.example.com/a-talk/?utm_source=telegram",
            urls=["https://www.example.com/a-talk/?utm_source=telegram"],
            posted_at=NOON + timedelta(hours=1),
        ),
    )

    steps = await _run_all(_worker(services))

    # Two ingest jobs, and then one fetch, one enrich and one embed for the one item.
    assert steps == 5
    (item,) = await _items(services.sessions)
    assert item.enrich_state is EnrichState.READY
    assert item.capture_count == 2
    assert (item.first_captured_at, item.last_captured_at) == (NOON, NOON + timedelta(hours=1))


async def test_an_album_waits_and_then_becomes_one_item(services: Services) -> None:
    await _arrive(
        services.sessions,
        _post(7, media_group_id=ALBUM, media=[_photo("a")], text="Slides from the queue talk"),
        _post(8, media_group_id=ALBUM, media=[_photo("b")]),
        _post(9, media_group_id=ALBUM, media=[_photo("c")], text="The last slide has the links"),
    )
    worker = _worker(services)

    assert await worker.run_one() is False  # the job of the album does not start yet

    await _the_album_wait_is_over(services.sessions)
    assert await _run_all(worker) == 4
    (item,) = await _items(services.sessions)
    assert item.enrich_state is EnrichState.READY
    async with services.sessions() as db:
        linked = (await db.execute(sa.select(Capture.item_id))).scalars().all()
        content = await db.get_one(ItemContent, item.id)
    assert linked == [item.id, item.id, item.id]
    # The text of the item is the text of all its messages, in the order of the album.
    assert content.text == "Slides from the queue talk\n\nThe last slide has the links"


async def test_two_posts_and_one_album_become_three_items(services: Services) -> None:
    await _arrive(
        services.sessions,
        _post(7, media_group_id=ALBUM, media=[_photo("a")]),
        _post(8, media_group_id=ALBUM, media=[_photo("b")]),
        _post(9, media_group_id=ALBUM, media=[_photo("c")]),
        _post(10, text="A link post"),
        _post(11, text="A forwarded post"),
    )
    await _the_album_wait_is_over(services.sessions)

    await _run_all(_worker(services))

    items = await _items(services.sessions)
    assert len(items) == 3
    assert {item.enrich_state for item in items} == {EnrichState.READY}


async def test_an_ingest_job_that_runs_again_makes_no_second_item(services: Services) -> None:
    await _arrive(services.sessions, _post(10, text="A link post"))
    async with services.sessions() as db:
        capture_id = (await db.execute(sa.select(Capture.id))).scalar_one()
    worker = _worker(services)
    await worker.run_one()

    async with services.sessions() as db:
        await queue.enqueue(db, JobKind.INGEST_CAPTURE, IngestCapturePayload(capture_id=capture_id))
        await db.commit()
    await _run_all(worker)

    assert len(await _items(services.sessions)) == 1


async def test_a_job_for_a_message_that_does_not_exist_is_dead_at_once(services: Services) -> None:
    async with services.sessions() as db:
        await queue.enqueue(db, JobKind.INGEST_CAPTURE, IngestCapturePayload(capture_id=999))
        await db.commit()

    await _worker(services).run_one()

    async with services.sessions() as db:
        job = (await db.execute(sa.select(Job))).scalar_one()
    assert (job.state, job.last_error) == (JobState.DEAD, "capture 999 does not exist")


async def test_the_pipeline_reads_the_latest_version_of_a_message(services: Services) -> None:
    await _arrive(services.sessions, _post(10, text="The first text"))
    async with services.sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        await resolve.accept_capture(db, owner_id, _post(10, text="The corrected text", edit_seq=5))
        item_id = await resolve.resolve_capture(
            db, (await db.execute(sa.select(Capture.id).where(Capture.edit_seq == 0))).scalar_one()
        )
        # The edit belongs to the same item. The resolver sets this link in the next change.
        await db.execute(sa.update(Capture).where(Capture.edit_seq == 5).values(item_id=item_id))
        await db.commit()

    await _run_all(_worker(services))

    async with services.sessions() as db:
        content = await db.get_one(ItemContent, item_id)
    # The latest version, one time: not "The first text" and the correction together.
    assert content.text == "The corrected text"
