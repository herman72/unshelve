"""The fetch step: the best extractor reads the source. A link that cannot be read keeps the
saved text, and a passing failure is tried again later. A pretend web: no network."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import Actor, CaptureSource, EnrichState, ItemKind, JobKind, JobState
from unshelve.contracts.errors import FetchError
from unshelve.contracts.jobs import Services
from unshelve.db.models import Event, Item, ItemContent, Job
from unshelve.ingest import resolve, versions
from unshelve.pipeline import items as steps
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services
from unshelve.testing.fakes import FakeFetcher

PAGES = Path(__file__).parents[1] / "extract" / "fixtures"
ARTICLE = (PAGES / "article_en.html").read_text(encoding="utf-8")
APP_SHELL = (PAGES / "app_shell.html").read_text(encoding="utf-8")
NOON = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
URL = "https://blog.example.com/2026/queues"
TEXT = f"Read this one later: {URL}"

Sessions = async_sessionmaker[AsyncSession]


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    return build_services(Settings(env="test", database_url=private_database_url), committed_db)


def _web(services: Services) -> FakeFetcher:
    """The pretend web of the tests: a page exists only when a test adds it."""
    assert isinstance(services.fetcher, FakeFetcher)
    return services.fetcher


def _worker(services: Services) -> Worker:
    discover()
    return Worker(services, registry, WorkerConfig(worker_id="w1"))


async def _run_all(services: Services) -> None:
    worker = _worker(services)
    while await worker.run_one():
        pass


async def _save(sessions: Sessions, *, text: str = TEXT, url: str | None = URL) -> int:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=await resolve.next_api_message_id(db),
            posted_at=NOON,
            text=text,
            urls=[url] if url else [],
        )
        capture_id = await resolve.store_capture(db, owner_id, envelope)
        assert capture_id is not None
        item_id = await resolve.resolve_capture(db, capture_id)
        await db.commit()
    assert item_id is not None
    return item_id


async def _item(sessions: Sessions, item_id: int) -> Item:
    async with sessions() as db:
        return await db.get_one(Item, item_id)


async def _content(sessions: Sessions, item_id: int) -> ItemContent | None:
    async with sessions() as db:
        return await db.get(ItemContent, item_id)


async def _events(sessions: Sessions, item_id: int) -> list[Event]:
    async with sessions() as db:
        found = await db.scalars(
            sa.select(Event).where(Event.item_id == item_id).order_by(Event.id)
        )
        return list(found.all())


async def _fetch_job(sessions: Sessions) -> Job:
    async with sessions() as db:
        return (await db.scalars(sa.select(Job).where(Job.kind == JobKind.FETCH_ITEM))).one()


# --- A link that can be read ------------------------------------------------------------------


async def test_a_link_to_an_article_gives_the_text_of_the_article(services: Services) -> None:
    _web(services).add(URL, ARTICLE, content_type="text/html; charset=utf-8")
    item_id = await _save(services.sessions)

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.kind, item.partial) == (
        EnrichState.READY,
        ItemKind.ARTICLE,
        False,
    )
    # The page gave the title, and the model does not replace it.
    assert (item.title, item.author, item.site, item.lang) == (
        "Queues in Postgres",
        "Ada Example",
        "blog.example.com",
        "en",
    )
    content = await _content(services.sessions, item_id)
    assert content is not None
    assert (content.extractor, content.final_url) == ("article", URL)
    assert "the second worker takes the next free row" in content.text
    assert await _events(services.sessions, item_id) == []


async def test_an_article_after_a_video_counts_its_minutes_again(services: Services) -> None:
    # The fetch step writes the minutes on each run, also None. So after an edit that changes
    # the link, the old length of a video does not stay on the item.
    item_id = await _save(services.sessions)
    _web(services).add(URL, ARTICLE, content_type="text/html; charset=utf-8")
    async with services.sessions() as db:
        await db.execute(sa.update(Item).where(Item.id == item_id).values(est_minutes=99))
        await db.commit()

    await _run_all(services)

    assert (await _item(services.sessions, item_id)).est_minutes == 1


async def test_the_link_is_opened_as_it_was_saved(services: Services) -> None:
    # The canonical address drops "www.", the tracking and the http. It is for identity only.
    saved = "http://www.blog.example.com/2026/queues?utm_source=telegram"
    _web(services).add(saved, ARTICLE, content_type="text/html; charset=utf-8")
    item_id = await _save(services.sessions, text="", url=saved)

    await _run_all(services)

    assert _web(services).requested == [saved]
    assert (await _item(services.sessions, item_id)).canonical_url == URL


@pytest.mark.parametrize(
    ("saved", "canonical", "opened"),
    [
        (
            ["https://blog.example.com/a"],
            "https://blog.example.com/a",
            "https://blog.example.com/a",
        ),
        (
            ["http://www.example.com/a?id=3"],
            "https://example.com/a?id=3",
            "http://www.example.com/a?id=3",
        ),
        # A link without a scheme cannot be opened as it is. The canonical form can.
        (["example.com/a"], "https://example.com/a", "https://example.com/a"),
        ([], None, None),  # a note
    ],
)
def test_the_address_to_open_is_the_saved_one_when_it_can_be_opened(
    saved: list[str], canonical: str | None, opened: str | None
) -> None:
    read = versions.Source(texts=(), urls=tuple(saved), media=(), forward_origin=None)

    assert steps._address_to_open(read, canonical) == opened  # pyright: ignore[reportPrivateUsage]


# --- A link that cannot be read ---------------------------------------------------------------


async def test_a_dead_link_gives_a_partial_item_that_is_still_enriched(services: Services) -> None:
    item_id = await _save(services.sessions)  # the pretend web has no page here: 404

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.kind, item.partial) == (
        EnrichState.READY,
        ItemKind.UNKNOWN,
        True,
    )
    assert item.summary is not None  # the model got the saved text
    content = await _content(services.sessions, item_id)
    assert content is not None
    assert (content.extractor, content.text, content.final_url) == ("saved_text", TEXT, None)
    (event,) = await _events(services.sessions, item_id)
    assert (event.actor, event.type) == (Actor.WORKER, "fetch_failed")
    assert event.payload == {
        "extractor": "article",
        "reason": "http_error",
        "status": 404,
        "attempt": 1,
    }


@pytest.mark.parametrize(
    "error",
    [
        FetchError("blocked_address", URL),
        FetchError("no_such_host", URL),
        FetchError("too_large", URL),
        FetchError("http_error", URL, status=410),
    ],
    ids=["blocked", "no such host", "too large", "gone"],
)
async def test_a_failure_that_will_not_pass_keeps_the_saved_text_at_once(
    services: Services, error: FetchError
) -> None:
    _web(services).pages[URL] = error
    item_id = await _save(services.sessions)

    await _worker(services).run_one()

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.partial) == (EnrichState.FETCHED, True)
    assert (await _fetch_job(services.sessions)).state is JobState.DONE
    (event,) = await _events(services.sessions, item_id)
    assert event.payload["reason"] == error.reason


async def test_a_page_that_is_no_article_keeps_the_saved_text(services: Services) -> None:
    _web(services).add(URL, APP_SHELL, content_type="text/html; charset=utf-8")
    item_id = await _save(services.sessions)

    await _worker(services).run_one()

    assert (await _item(services.sessions, item_id)).partial is True
    (event,) = await _events(services.sessions, item_id)
    assert event.payload["reason"] == "no_text"
    assert event.payload["detail"].startswith("the page has only ")


async def test_a_link_to_a_file_is_not_opened_yet(services: Services) -> None:
    item_id = await _save(services.sessions, url="https://example.com/paper.pdf")

    await _worker(services).run_one()

    assert _web(services).requested == []  # no extractor reads a PDF file yet
    assert (await _item(services.sessions, item_id)).partial is True
    assert await _events(services.sessions, item_id) == []


async def test_a_note_without_a_link_is_read_from_its_text(services: Services) -> None:
    item_id = await _save(services.sessions, text="Buy oat milk, and call the dentist.", url=None)

    await _worker(services).run_one()

    item = await _item(services.sessions, item_id)
    assert (item.kind, item.partial) == (ItemKind.NOTE, False)
    assert _web(services).requested == []


# --- A failure that can pass ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "wait"),
    [
        (FetchError("timeout", URL), timedelta(minutes=1)),
        (FetchError("network", URL), timedelta(minutes=1)),
        (FetchError("http_error", URL, status=429, retry_after=300.0), timedelta(minutes=5)),
        (FetchError("http_error", URL, status=503, retry_after=86_400.0), timedelta(hours=1)),
    ],
    ids=["timeout", "network", "429 with its wait", "a wait of one day becomes one hour"],
)
async def test_a_passing_failure_is_tried_again_later(
    services: Services, error: FetchError, wait: timedelta
) -> None:
    _web(services).pages[URL] = error
    item_id = await _save(services.sessions)
    before = datetime.now(UTC)

    await _worker(services).run_one()

    job = await _fetch_job(services.sessions)
    assert (job.state, job.attempts) == (JobState.PENDING, 1)
    assert job.last_error is not None
    assert job.last_error.startswith(f"fetch: {error.reason}")
    assert (
        before + wait - timedelta(seconds=5)
        <= job.run_after
        <= before + wait + timedelta(seconds=5)
    )
    # Nothing is written: the item waits for the next try.
    assert (await _item(services.sessions, item_id)).enrich_state is EnrichState.PENDING
    assert await _content(services.sessions, item_id) is None
    assert await _events(services.sessions, item_id) == []


async def test_the_wait_doubles_with_each_try(services: Services) -> None:
    _web(services).pages[URL] = FetchError("timeout", URL)
    await _save(services.sessions)
    worker = _worker(services)
    waits: list[timedelta] = []

    for _ in range(4):
        before = datetime.now(UTC)
        await worker.run_one()
        job = await _fetch_job(services.sessions)
        waits.append(job.run_after - before)
        async with services.sessions() as db:  # the test does not wait for real
            await db.execute(sa.update(Job).values(run_after=sa.func.now()))
            await db.commit()

    assert [round(wait.total_seconds() / 60) for wait in waits] == [1, 2, 4, 8]


async def test_the_last_attempt_keeps_the_saved_text(services: Services) -> None:
    _web(services).pages[URL] = FetchError("timeout", URL)
    item_id = await _save(services.sessions)
    async with services.sessions() as db:  # the next attempt is the last one
        await db.execute(sa.update(Job).values(max_attempts=1))
        await db.commit()

    await _worker(services).run_one()

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.partial) == (EnrichState.FETCHED, True)
    assert (await _fetch_job(services.sessions)).state is JobState.DONE
    (event,) = await _events(services.sessions, item_id)
    assert (event.payload["reason"], event.payload["attempt"]) == ("timeout", 1)
