"""The fetch step: the best extractor reads the source. A link that cannot be read keeps the
saved text, and a passing failure is tried again later. A pretend web: no network."""

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope, MediaRef
from unshelve.contracts.enums import Actor, CaptureSource, EnrichState, ItemKind, JobKind, JobState
from unshelve.contracts.errors import FetchError, ProviderError
from unshelve.contracts.jobs import Services
from unshelve.db.models import Asset, Event, Item, ItemContent, ItemKey, Job
from unshelve.extract import pdf
from unshelve.extract.image import ImageReading
from unshelve.ingest import resolve, versions
from unshelve.pipeline import items as steps
from unshelve.providers.ledger import CachedLLM
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.reading import reading_minutes
from unshelve.services import build_services
from unshelve.testing.conformance import TINY_PNG
from unshelve.testing.fakes import FakeFetcher, FakeFiles, FakeLLM, FakeLLMCall

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


async def _save(
    sessions: Sessions,
    *,
    text: str = TEXT,
    url: str | None = URL,
    media: list[MediaRef] | None = None,
) -> int:
    async with sessions() as db:
        owner_id = await auth.ensure_owner(db, 1)
        envelope = CaptureEnvelope(
            source=CaptureSource.API,
            chat_id=resolve.API_CHAT_ID,
            message_id=await resolve.next_api_message_id(db),
            posted_at=NOON,
            text=text,
            urls=[url] if url else [],
            media=media or [],
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


async def test_a_video_gets_its_length_as_its_minutes(services: Services) -> None:
    video = "https://youtu.be/dEmOvIdEo01?si=made-up"
    watch = "https://www.youtube.com/watch?v=dEmOvIdEo01&hl=en"
    page = (PAGES / "youtube" / "watch_en.html").read_bytes()
    _web(services).add(watch, page, content_type="text/html; charset=utf-8")
    item_id = await _save(services.sessions, text=video, url=video)

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.kind, item.partial) == (
        EnrichState.READY,
        ItemKind.VIDEO,
        False,
    )
    assert (item.title, item.author, item.site) == (
        "Job queues in Postgres, from scratch",
        "Made-up Channel",
        "youtube.com",
    )
    # The length of the video (30.5 minutes), not the time to read its description.
    assert item.est_minutes == 31
    assert item.extra_links == [
        "https://github.com/example/queue-demo",
        "https://example.com/queue-slides.pdf",
        "https://example.com/notes",  # without the full stop after it, and each link one time
    ]
    content = await _content(services.sessions, item_id)
    assert content is not None
    assert content.extractor == "youtube"


PDF_LINK = "https://example.com/papers/leases.pdf"


async def test_a_pdf_gives_its_text_and_its_facts_to_the_pipeline(services: Services) -> None:
    paper = (PAGES / "pdf" / "paper.pdf").read_bytes()
    _web(services).add(PDF_LINK, paper, content_type="application/pdf")
    item_id = await _save(services.sessions, text=PDF_LINK, url=PDF_LINK)

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.kind, item.partial) == (
        EnrichState.READY,
        ItemKind.PDF,
        False,
    )
    assert (item.title, item.site) == ("Leases for Job Queues in Postgres", "example.com")
    assert item.extra_links == ["https://example.org/queue-data", "https://example.com/queue-notes"]
    content = await _content(services.sessions, item_id)
    assert content is not None
    assert content.extractor == "pdf"
    assert content.text.startswith("Leases for Job Queues in Postgres\nA PDF document, 3 pages")
    # The whole file was read, so the pipeline counts the minutes of its text.
    assert item.est_minutes == reading_minutes(content.text)


async def test_a_pdf_read_in_part_gets_the_minutes_of_all_its_pages(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf, "MAX_PAGES", 1)
    paper = (PAGES / "pdf" / "paper.pdf").read_bytes()
    _web(services).add(PDF_LINK, paper, content_type="application/pdf")
    item_id = await _save(services.sessions, text=PDF_LINK, url=PDF_LINK)

    await _run_all(services)

    content = await _content(services.sessions, item_id)
    assert content is not None
    first_page = content.text.split("\n\n", 1)[1]
    # 1 of 3 pages was read: the minutes of that page, three times.
    assert (await _item(services.sessions, item_id)).est_minutes == 3 * reading_minutes(first_page)


SENT = MediaRef(
    kind="document",
    file_id="BQACAgMadeUpFileId",
    file_unique_id="AgADmadeup",
    mime="application/pdf",
    size_bytes=96_235,
    file_name="leases.pdf",
)


async def test_a_pdf_sent_as_a_file_is_read_and_its_copy_is_kept(services: Services) -> None:
    files = FakeFiles({str(SENT.file_id): (PAGES / "pdf" / "paper.pdf").read_bytes()})
    services = dataclasses.replace(services, files=files)
    item_id = await _save(services.sessions, text="A paper to read", url=None, media=[SENT])

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.kind, item.partial) == (
        EnrichState.READY,
        ItemKind.PDF,
        False,
    )
    assert item.title == "Leases for Job Queues in Postgres"
    content = await _content(services.sessions, item_id)
    assert content is not None
    assert (content.extractor, content.final_url) == ("pdf", None)
    assert files.read_ids == [SENT.file_id]
    async with services.sessions() as db:
        [asset] = (await db.scalars(sa.select(Asset))).all()
    assert (asset.file_unique_id, asset.mime) == ("AgADmadeup", "application/pdf")
    assert await services.blobs.get(asset.storage_key) == (PAGES / "pdf" / "paper.pdf").read_bytes()


async def test_a_pdf_sent_as_a_file_without_a_bot_token_keeps_its_caption(
    services: Services,
) -> None:
    # Without a token the worker has no source of files: the item keeps what you wrote.
    item_id = await _save(services.sessions, text="A paper to read", url=None, media=[SENT])

    await _run_all(services)

    content = await _content(services.sessions, item_id)
    assert content is not None
    assert (content.extractor, content.text) == ("saved_text", "A paper to read")
    [event] = await _events(services.sessions, item_id)
    assert (event.payload["extractor"], event.payload["reason"]) == ("pdf", "unsupported")


async def test_a_pdf_of_an_export_is_read_from_its_copy(services: Services) -> None:
    # The importer copied the file of the export, and the media names the copy. No bot token.
    data = (PAGES / "pdf" / "paper.pdf").read_bytes()
    exported = MediaRef(
        kind="document",
        local_path="files/paper.pdf",
        mime="application/pdf",
        file_name="paper.pdf",
        sha256=await services.blobs.put(data, "application/pdf"),
    )
    item_id = await _save(services.sessions, text="A paper to read", url=None, media=[exported])

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.kind, item.title) == (
        EnrichState.READY,
        ItemKind.PDF,
        "Leases for Job Queues in Postgres",
    )


# --- The address where a link ended is a key of the item (#68) ------------------------------

SHORT = "https://t.co/AbCdEf1234"
OTHER_SHORT = "https://bit.ly/3madeUp"
HTML = "text/html; charset=utf-8"


def _redirect(services: Services, link: str, final: str = URL) -> None:
    """A link whose redirects end at ``final``, with the article there."""
    _web(services).add(link, ARTICLE, content_type=HTML, final_url=final)


async def _keys(sessions: Sessions, item_id: int) -> list[str]:
    async with sessions() as db:
        found = await db.scalars(
            sa.select(ItemKey.key).where(ItemKey.item_id == item_id).order_by(ItemKey.key)
        )
        return list(found.all())


async def test_a_short_link_that_ends_at_a_saved_page_joins_its_item(services: Services) -> None:
    _web(services).add(URL, ARTICLE, content_type=HTML)
    page = await _save(services.sessions, text=URL, url=URL)
    await _run_all(services)
    _redirect(services, SHORT)
    short = await _save(services.sessions, text=SHORT, url=SHORT)

    await _run_all(services)

    assert (await _item(services.sessions, short)).duplicate_of_id == page
    assert (await _item(services.sessions, page)).capture_count == 2


async def test_a_page_saved_before_its_short_link_was_read_joins_the_older_item(
    services: Services,
) -> None:
    # The short link was saved first, and its fetch had not run yet: it is the item that stays.
    _redirect(services, SHORT)
    _web(services).add(URL, ARTICLE, content_type=HTML)
    short = await _save(services.sessions, text=SHORT, url=SHORT)
    page = await _save(services.sessions, text=URL, url=URL)

    await _run_all(services)

    assert (await _item(services.sessions, page)).duplicate_of_id == short
    assert (await _item(services.sessions, short)).capture_count == 2


async def test_a_second_fetch_adds_no_second_key(services: Services) -> None:
    _redirect(services, SHORT)
    short = await _save(services.sessions, text=SHORT, url=SHORT)
    await _run_all(services)
    async with services.sessions() as db:
        await resolve.restart_item(db, (await db.get_one(Item, short)).owner_id, short)
        await db.commit()

    await _run_all(services)

    assert await _keys(services.sessions, short) == [f"url:{URL}", f"url:{SHORT}"]


async def test_two_short_links_of_one_page_read_at_the_same_moment_give_one_item(
    services: Services,
) -> None:
    # No item has the address of the page yet, and two workers read the two links at once. The
    # lock for each key is proved in tests/ingest/test_resolve_together.py: there the first
    # worker is made slow, so that the second one surely looks at the same moment.
    _redirect(services, SHORT)
    _redirect(services, OTHER_SHORT)
    first = await _save(services.sessions, text=SHORT, url=SHORT)
    second = await _save(services.sessions, text=OTHER_SHORT, url=OTHER_SHORT)
    discover()
    one = Worker(services, registry, WorkerConfig(worker_id="w1"))
    two = Worker(services, registry, WorkerConfig(worker_id="w2"))

    await asyncio.gather(one.run_one(), two.run_one())  # the two fetch jobs at the same moment

    assert (await _item(services.sessions, second)).duplicate_of_id == first


async def test_two_dead_links_that_end_at_the_front_page_stay_two_items(
    services: Services,
) -> None:
    dead = ["https://blog.example.com/2019/old-one", "https://blog.example.com/2019/old-two"]
    for link in dead:
        _redirect(services, link, final="https://blog.example.com/")
    items = [await _save(services.sessions, text=link, url=link) for link in dead]

    await _run_all(services)
    async with services.sessions() as db:  # and the same rule for ``unshelve rekey``
        for item in items:
            await resolve.rekey_item(db, item)
        await db.commit()

    assert [(await _item(services.sessions, item)).duplicate_of_id for item in items] == [
        None,
        None,
    ]


async def test_a_saved_front_page_gives_the_front_page_where_it_ends(services: Services) -> None:
    # A site that moved: its old name sends each visitor to the front page of the new name.
    old, new = "https://queue-notes.example/", "https://queues.example/"
    _redirect(services, old, final=new)
    first = await _save(services.sessions, text=old, url=old)
    await _run_all(services)

    again = await _save(services.sessions, text=new, url=new)

    assert again == first
    assert (await _item(services.sessions, first)).capture_count == 2


async def test_rekey_joins_a_short_link_read_before_the_keys_of_final_addresses(
    services: Services,
) -> None:
    # A short link read before #68 has no key for the address where it ended. Its page, saved
    # later, became a second item. ``unshelve rekey`` joins them.
    _redirect(services, SHORT)
    short = await _save(services.sessions, text=SHORT, url=SHORT)
    await _run_all(services)
    async with services.sessions() as db:
        await db.execute(sa.delete(ItemKey).where(ItemKey.key == f"url:{URL}"))
        await db.commit()
    _web(services).add(URL, ARTICLE, content_type=HTML)
    page = await _save(services.sessions, text=URL, url=URL)
    assert (await _item(services.sessions, page)).duplicate_of_id is None

    async with services.sessions() as db:
        await resolve.rekey_item(db, short)
        await db.commit()

    assert (await _item(services.sessions, page)).duplicate_of_id == short


# --- A photo or a screenshot: the model reads it ---------------------------------------------

PHOTO = MediaRef(kind="photo", file_id="AgACAgMadeUpPhoto", file_unique_id="AQADmadeup")
READING = ImageReading(
    description="A whiteboard with a diagram of a job queue and its workers.",
    text="SKIP LOCKED\nlease: 30 s",
)


def _reads_images(
    services: Services, error: ProviderError | None = None, files: FakeFiles | None = None
) -> tuple[Services, FakeLLM]:
    """The services with a source of files and a pretend model that reads each image the same
    way, behind the same cache as in the worker. The card is made up from the text."""

    def respond(call: FakeLLMCall, schema: type[BaseModel]) -> BaseModel | None:
        if call.task != "read_image":
            return None
        if error is not None:
            raise error
        return READING

    model = FakeLLM(responder=respond)
    services = dataclasses.replace(
        services,
        files=files or FakeFiles({str(PHOTO.file_id): TINY_PNG}),
        llm=CachedLLM(model, services.sessions, model=FakeLLM.model),
    )
    return services, model


def _image_calls(model: FakeLLM) -> int:
    return sum(call.task == "read_image" for call in model.calls)


async def test_a_photo_sent_to_the_channel_is_read_by_the_model(services: Services) -> None:
    services, model = _reads_images(services)
    item_id = await _save(
        services.sessions, text="The whiteboard of today", url=None, media=[PHOTO]
    )

    await _run_all(services)

    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.kind, item.partial) == (
        EnrichState.READY,
        ItemKind.IMAGE,
        False,
    )
    content = await _content(services.sessions, item_id)
    assert content is not None
    assert content.extractor == "image"
    assert content.text == (
        "The whiteboard of today\n\n"
        "An image: A whiteboard with a diagram of a job queue and its workers.\n\n"
        "Text in the image:\nSKIP LOCKED\nlease: 30 s"
    )
    assert _image_calls(model) == 1
    # The card is written from that text: the model got the words of the whiteboard.
    [card_call] = [call for call in model.calls if call.task != "read_image"]
    assert "SKIP LOCKED" in card_call.text


async def test_a_photo_is_paid_one_time_also_when_it_is_read_again(services: Services) -> None:
    services, model = _reads_images(services)
    item_id = await _save(services.sessions, text="", url=None, media=[PHOTO])
    await _run_all(services)

    async with services.sessions() as db:
        await resolve.restart_item(db, (await db.get_one(Item, item_id)).owner_id, item_id)
        await db.commit()
    await _run_all(services)

    assert (await _item(services.sessions, item_id)).enrich_state == EnrichState.READY
    assert _image_calls(model) == 1  # the second answer came from the ledger


async def test_a_busy_model_reads_the_photo_later(services: Services) -> None:
    busy = ProviderError("529 overloaded_error", retryable=True, retry_after=30)
    services, _ = _reads_images(services, busy)
    item_id = await _save(services.sessions, text="", url=None, media=[PHOTO])
    started = datetime.now(UTC)

    await _run_all(services)

    assert (await _item(services.sessions, item_id)).enrich_state == EnrichState.PENDING
    job = await _fetch_job(services.sessions)
    assert (job.state, job.attempts) == (JobState.PENDING, 1)
    assert job.run_after >= started + timedelta(seconds=29)


async def test_a_photo_that_the_model_cannot_read_keeps_its_caption(services: Services) -> None:
    declined = ProviderError("the model declined (cyber)", retryable=False)
    services, _ = _reads_images(services, declined)
    item_id = await _save(services.sessions, text="My caption", url=None, media=[PHOTO])

    await _run_all(services)

    # Not partial: the item has no link that could not be read, as for a sent PDF.
    item = await _item(services.sessions, item_id)
    assert (item.enrich_state, item.partial) == (EnrichState.READY, False)
    content = await _content(services.sessions, item_id)
    assert content is not None
    assert (content.extractor, content.text) == ("saved_text", "My caption")
    [event, *_] = await _events(services.sessions, item_id)
    assert (event.payload["extractor"], event.payload["reason"], event.payload["detail"]) == (
        "image",
        "model",
        "the model declined (cyber)",
    )


async def test_a_photo_of_an_export_is_read_from_its_copy(services: Services) -> None:
    # The importer copied the photo, and the media names the copy. No bot token.
    exported = MediaRef(
        kind="photo",
        local_path="photos/photo_1@13-10-2026_23-25-17.jpg",
        file_name="photo_1@13-10-2026_23-25-17.jpg",
        sha256=await services.blobs.put(TINY_PNG, "image/png"),
    )
    services, model = _reads_images(services, files=FakeFiles({}))
    services = dataclasses.replace(services, files=None)
    item_id = await _save(services.sessions, text="", url=None, media=[exported])

    await _run_all(services)

    assert (await _item(services.sessions, item_id)).kind == ItemKind.IMAGE
    assert _image_calls(model) == 1


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


async def test_a_link_to_a_file_that_no_extractor_reads_is_not_opened(services: Services) -> None:
    item_id = await _save(services.sessions, url="https://example.com/queue-data.zip")

    await _worker(services).run_one()

    assert _web(services).requested == []  # no extractor reads a zip file
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
