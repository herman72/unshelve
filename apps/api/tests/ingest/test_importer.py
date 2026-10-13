"""The importer: from the messages of an export to stored captures. Each test name is one rule."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.bot.support import CHANNEL, OWNER_TELEGRAM_ID, post
from tests.ingest.support import DESKTOP, MACOS, made_up
from unshelve.auth import store as auth_store
from unshelve.bot.envelope import envelope_from_post
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope, ForwardOrigin, MediaKind, MediaRef
from unshelve.contracts.enums import CaptureSource, EnrichState, JobKind, JobState
from unshelve.db.models import Capture, Item, Job
from unshelve.ingest import importer, resolve
from unshelve.ingest.export_html import ExportedChat, ExportedFile, ExportedMessage, read_export
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services

Sessions = async_sessionmaker[AsyncSession]

HELSINKI = ZoneInfo("Europe/Helsinki")
NIGHT = datetime(2026, 10, 13, 23, 25, 17)  # noqa: DTZ001 - a clock time of an export
LATER = datetime(2026, 10, 13, 23, 25, 18)  # noqa: DTZ001
MADE_UP_IDS = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 20, 21, 23, 24, 26]


def _message(
    message_id: int,
    at: datetime = NIGHT,
    *,
    files: tuple[MediaKind, ...] = ("photo",),
    text: str = "",
    source: str | None = None,
    first_sent: datetime | None = None,
) -> ExportedMessage:
    return ExportedMessage(
        message_id=message_id,
        sent_at=at,
        text=text,
        urls=(),
        files=tuple(ExportedFile(kind, None, None) for kind in files),
        forwarded_from=source,
        forwarded_at=first_sent,
    )


def _envelopes(chat: ExportedChat, zone: ZoneInfo = HELSINKI) -> list[CaptureEnvelope]:
    albums = importer.album_groups(chat.messages)
    return importer.to_envelopes(chat.messages, chat_id=CHANNEL, zone=zone, albums=albums)


# --- Albums: the export does not mark them, so the importer finds them ----------------------


def test_photos_of_the_same_second_with_ids_that_follow_each_other_are_one_album() -> None:
    album = importer.album_groups([_message(7), _message(8), _message(9)])

    assert album == {7: "export:7", 8: "export:7", 9: "export:7"}


def test_one_photo_is_not_an_album() -> None:
    assert importer.album_groups([_message(7)]) == {}


def test_photos_of_two_different_seconds_are_not_an_album() -> None:
    assert importer.album_groups([_message(7), _message(8, LATER)]) == {}


def test_photos_with_ids_that_do_not_follow_each_other_are_not_an_album() -> None:
    assert importer.album_groups([_message(7), _message(9)]) == {}


def test_an_album_can_have_one_caption() -> None:
    album = importer.album_groups([_message(7, text="The whiteboard"), _message(8)])

    assert set(album) == {7, 8}


def test_two_posts_with_a_text_each_are_two_posts() -> None:
    # Two photo posts that were forwarded together look like an album. Each has its own text.
    assert importer.album_groups([_message(7, text="First"), _message(8, text="Second")]) == {}


@pytest.mark.parametrize(
    ("kinds", "is_album"),
    [
        (("photo", "video"), True),
        (("document", "document"), True),
        (("audio", "audio"), True),
        # Telegram does not put these together in one album.
        (("photo", "document"), False),
        (("document", "audio"), False),
        # These kinds are never in an album.
        (("sticker", "sticker"), False),
        (("voice", "voice"), False),
        (("animation", "animation"), False),
    ],
)
def test_only_files_that_telegram_sends_together_are_an_album(
    kinds: tuple[MediaKind, MediaKind], is_album: bool
) -> None:
    album = importer.album_groups([_message(7, files=kinds[:1]), _message(8, files=kinds[1:])])

    assert bool(album) is is_album


def test_posts_without_a_file_are_not_an_album() -> None:
    assert importer.album_groups([_message(7, files=(), text="a"), _message(8, files=())]) == {}


def test_a_post_without_a_file_ends_an_album() -> None:
    album = importer.album_groups([_message(7), _message(8), _message(9, files=(), text="note")])

    assert set(album) == {7, 8}


def test_two_albums_that_follow_each_other_stay_two_albums() -> None:
    messages = [_message(7), _message(8), _message(9, LATER), _message(10, LATER)]

    assert importer.album_groups(messages) == {
        7: "export:7",
        8: "export:7",
        9: "export:9",
        10: "export:9",
    }


def test_photos_from_two_sources_are_not_an_album() -> None:
    assert importer.album_groups([_message(7, source="A"), _message(8, source="B")]) == {}
    assert importer.album_groups([_message(7, source="A"), _message(8)]) == {}


def test_photos_that_were_first_sent_at_two_moments_are_not_an_album() -> None:
    # A person selected two photo posts of a channel and forwarded them together.
    monday = datetime(2026, 10, 12, 9, 0, 0)  # noqa: DTZ001
    tuesday = datetime(2026, 10, 13, 9, 0, 0)  # noqa: DTZ001
    messages = [
        _message(7, source="A", first_sent=monday),
        _message(8, source="A", first_sent=tuesday),
    ]

    assert importer.album_groups(messages) == {}


def test_a_forwarded_album_is_one_album() -> None:
    monday = datetime(2026, 10, 12, 9, 0, 0)  # noqa: DTZ001
    messages = [
        _message(7, source="A", first_sent=monday),
        _message(8, source="A", first_sent=monday),
        # Telegram Desktop gives no first date for this part (see the reader).
        _message(9, source="A"),
    ]

    assert set(importer.album_groups(messages)) == {7, 8, 9}


def test_the_made_up_export_has_two_albums() -> None:
    assert importer.album_groups(made_up().messages) == {
        7: "export:7",
        8: "export:7",
        9: "export:7",
        23: "export:23",
        24: "export:23",
    }


# --- Envelopes ------------------------------------------------------------------------------


def test_a_post_becomes_an_envelope() -> None:
    document = _envelopes(read_export(DESKTOP))[-1]

    assert document == CaptureEnvelope(
        source=CaptureSource.EXPORT,
        chat_id=CHANNEL,
        message_id=6,
        posted_at=datetime(2026, 10, 13, 19, 44, 19, tzinfo=UTC),
        text="",
        urls=[],
        media=[
            MediaRef(
                kind="document",
                local_path="files/a-guide-to-job-queues.pdf",
                file_name="a-guide-to-job-queues.pdf",
            )
        ],
        raw={
            "export": "telegram-html",
            "clock_time": "2026-10-13T22:44:19",
            "time_zone": "Europe/Helsinki",
        },
    )


@pytest.mark.parametrize(
    ("clock", "zone", "moment"),
    [
        # Summer time in Helsinki: 3 hours before UTC.
        (datetime(2026, 10, 13, 22, 40, 37), "Europe/Helsinki", datetime(2026, 10, 13, 19, 40, 37)),  # noqa: DTZ001
        # Winter time in Helsinki: 2 hours. One zone name gives the two.
        (datetime(2026, 1, 15, 10, 0, 0), "Europe/Helsinki", datetime(2026, 1, 15, 8, 0, 0)),  # noqa: DTZ001
        (datetime(2026, 10, 13, 22, 40, 37), "Asia/Tehran", datetime(2026, 10, 13, 19, 10, 37)),  # noqa: DTZ001
        (datetime(2026, 10, 13, 22, 40, 37), "UTC", datetime(2026, 10, 13, 22, 40, 37)),  # noqa: DTZ001
    ],
)
def test_a_clock_time_and_a_time_zone_give_the_exact_moment(
    clock: datetime, zone: str, moment: datetime
) -> None:
    (envelope,) = importer.to_envelopes(
        [_message(7, clock)], chat_id=CHANNEL, zone=ZoneInfo(zone), albums={}
    )

    assert envelope.posted_at == moment.replace(tzinfo=UTC)
    assert envelope.raw["clock_time"] == clock.isoformat()
    assert envelope.raw["time_zone"] == zone


def test_a_forwarded_post_names_its_source_and_has_no_ids() -> None:
    forwarded = _envelopes(read_export(DESKTOP))[2]

    assert forwarded.forward_origin == ForwardOrigin(
        type="unknown",
        name="Systems Notes | Queues & Agents 🚀",
        date=datetime(2026, 6, 20, 12, 56, 2, tzinfo=UTC),
    )


def test_a_forwarded_post_without_a_first_date_has_no_date() -> None:
    forwarded = _envelopes(read_export(MACOS))[2]

    assert forwarded.forward_origin == ForwardOrigin(
        type="unknown", name="Systems Notes | Queues & Agents 🚀"
    )


def test_the_parts_of_an_album_get_the_key_of_the_album() -> None:
    keys = {envelope.message_id: envelope.media_group_id for envelope in _envelopes(made_up())}

    assert [keys[7], keys[8], keys[9]] == ["export:7"] * 3
    assert keys[10] is None


@pytest.mark.parametrize("message_id", [10, 11])
def test_the_importer_and_the_bot_agree_on_a_post_that_both_have(message_id: int) -> None:
    from_bot = envelope_from_post(post("seen", message_id))
    from_export = next(e for e in _envelopes(made_up()) if e.message_id == message_id)

    # These fields are the identity of a capture: with them, the post is stored one time.
    assert (from_export.chat_id, from_export.message_id, from_export.edit_seq) == (
        from_bot.chat_id,
        from_bot.message_id,
        from_bot.edit_seq,
    )
    assert from_export.posted_at == from_bot.posted_at
    assert from_export.text == from_bot.text
    assert from_export.urls == from_bot.urls


def test_the_importer_and_the_bot_agree_on_the_source_of_a_forwarded_post() -> None:
    from_bot = envelope_from_post(post("seen", 11)).forward_origin
    from_export = next(e for e in _envelopes(made_up()) if e.message_id == 11).forward_origin

    assert from_bot is not None
    assert from_export is not None
    assert (from_export.name, from_export.date) == (from_bot.name, from_bot.date)
    # The bot knows the ids of the source. An export has only its name.
    assert (from_bot.type, from_bot.chat_id) == ("channel", -1001111111111)
    assert (from_export.type, from_export.chat_id) == ("unknown", None)


# --- A part of an export --------------------------------------------------------------------


def test_since_keeps_the_posts_of_that_clock_time_and_later() -> None:
    chosen = importer.in_range(made_up().messages, since=datetime(2026, 10, 14), limit=None)  # noqa: DTZ001

    assert [message.message_id for message in chosen] == [23, 24, 26]


def test_limit_keeps_the_first_posts() -> None:
    chosen = importer.in_range(made_up().messages, since=None, limit=3)

    assert [message.message_id for message in chosen] == [7, 8, 9]


def test_since_and_limit_work_together() -> None:
    chosen = importer.in_range(made_up().messages, since=datetime(2026, 10, 14), limit=1)  # noqa: DTZ001

    assert [message.message_id for message in chosen] == [23]


def test_without_since_and_limit_each_post_is_kept() -> None:
    chosen = importer.in_range(made_up().messages, since=None, limit=None)

    assert [message.message_id for message in chosen] == MADE_UP_IDS


# --- The database ---------------------------------------------------------------------------


async def _captures(sessions: Sessions) -> list[Capture]:
    async with sessions() as db:
        found = await db.execute(sa.select(Capture).order_by(Capture.message_id, Capture.edit_seq))
        return list(found.scalars().all())


async def _job_kinds(sessions: Sessions) -> list[JobKind]:
    async with sessions() as db:
        return list((await db.execute(sa.select(Job.kind).order_by(Job.id))).scalars().all())


async def _the_bot_stores(sessions: Sessions, *message_ids: int) -> None:
    async with sessions() as db:
        owner_id = await auth_store.ensure_owner(db, OWNER_TELEGRAM_ID)
        for message_id in message_ids:
            envelope = envelope_from_post(post("seen", message_id))
            assert await resolve.accept_capture(db, owner_id, envelope) is not None
        await db.commit()


async def _work_until_done(sessions: Sessions, database_url: str) -> None:
    async with sessions() as db:  # the wait for the other parts of an album is over
        await db.execute(
            sa.update(Job).where(Job.state == JobState.PENDING).values(run_after=sa.func.now())
        )
        await db.commit()
    discover()
    settings = Settings(env="test", database_url=database_url)
    worker = Worker(build_services(settings, sessions), registry, WorkerConfig(worker_id="w1"))
    while await worker.run_one():
        pass


async def test_an_import_stores_each_post_and_adds_its_job(committed_db: Sessions) -> None:
    count = await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, _envelopes(made_up()))

    assert count == importer.ImportCount(stored=16, repeats=0)
    captures = await _captures(committed_db)
    assert [capture.message_id for capture in captures] == MADE_UP_IDS
    assert {capture.source for capture in captures} == {CaptureSource.EXPORT}
    assert {capture.chat_id for capture in captures} == {CHANNEL}
    kinds = await _job_kinds(committed_db)
    # Five posts are in two albums. Each album has one job, and each other post has its job.
    assert kinds.count(JobKind.INGEST_ALBUM) == 2
    assert kinds.count(JobKind.INGEST_CAPTURE) == 11
    assert len(kinds) == 13


async def test_a_stored_post_keeps_what_the_export_said(committed_db: Sessions) -> None:
    await importer.import_envelopes(
        committed_db, OWNER_TELEGRAM_ID, _envelopes(read_export(DESKTOP))
    )

    stored = {capture.message_id: capture for capture in await _captures(committed_db)}
    assert stored[2].posted_at == datetime(2026, 10, 13, 19, 40, 37, tzinfo=UTC)
    assert stored[2].urls == ["https://example.com/blog/scaling-storage/"]
    assert stored[4].forward_origin is not None
    assert stored[4].forward_origin["name"] == "Systems Notes | Queues & Agents 🚀"
    assert stored[4].forward_origin["type"] == "unknown"
    assert stored[6].media[0]["local_path"] == "files/a-guide-to-job-queues.pdf"
    assert stored[6].raw["clock_time"] == "2026-10-13T22:44:19"


async def test_a_second_import_stores_nothing(committed_db: Sessions) -> None:
    envelopes = _envelopes(made_up())
    await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, envelopes)

    count = await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, envelopes)

    assert count == importer.ImportCount(stored=0, repeats=16)
    assert len(await _captures(committed_db)) == 16
    assert len(await _job_kinds(committed_db)) == 13


async def test_the_export_of_the_other_app_stores_nothing_new(committed_db: Sessions) -> None:
    await importer.import_envelopes(
        committed_db, OWNER_TELEGRAM_ID, _envelopes(read_export(DESKTOP))
    )

    count = await importer.import_envelopes(
        committed_db, OWNER_TELEGRAM_ID, _envelopes(read_export(MACOS))
    )

    assert count == importer.ImportCount(stored=0, repeats=5)


async def test_a_post_that_the_bot_stored_is_not_stored_again(committed_db: Sessions) -> None:
    await _the_bot_stores(committed_db, 7, 8, 9, 10, 11)

    count = await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, _envelopes(made_up()))

    assert count == importer.ImportCount(stored=11, repeats=5)
    captures = await _captures(committed_db)
    assert [capture.message_id for capture in captures] == MADE_UP_IDS
    # The capture of the bot stays as it was: the export does not replace it.
    by_bot = [capture for capture in captures if capture.source is CaptureSource.BOT]
    assert [capture.message_id for capture in by_bot] == [7, 8, 9, 10, 11]
    assert {capture.media_group_id for capture in by_bot[:3]} == {"14335384938541892"}
    # One album job from the bot, one from the export for the two documents.
    assert (await _job_kinds(committed_db)).count(JobKind.INGEST_ALBUM) == 2


async def test_a_dry_run_counts_and_changes_nothing(committed_db: Sessions) -> None:
    envelopes = _envelopes(made_up())

    before = await importer.count_new(committed_db, OWNER_TELEGRAM_ID, envelopes)

    assert before == importer.ImportCount(stored=16, repeats=0)
    assert await _captures(committed_db) == []
    assert await _job_kinds(committed_db) == []

    await _the_bot_stores(committed_db, 7, 8, 9, 10, 11)
    async with committed_db() as db:  # and an edit of post 10: a second row for one post
        owner_id = await auth_store.ensure_owner(db, OWNER_TELEGRAM_ID)
        edit = envelope_from_post(post("made_up", 10))
        assert edit.edit_seq > 0
        assert await resolve.accept_capture(db, owner_id, edit) is not None
        await db.commit()
    assert await importer.count_new(
        committed_db, OWNER_TELEGRAM_ID, envelopes
    ) == importer.ImportCount(stored=11, repeats=5)

    await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, envelopes)
    assert await importer.count_new(
        committed_db, OWNER_TELEGRAM_ID, envelopes
    ) == importer.ImportCount(stored=0, repeats=16)


async def test_a_dry_run_does_not_count_the_posts_of_another_chat(committed_db: Sessions) -> None:
    await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, _envelopes(made_up()))
    albums = importer.album_groups(made_up().messages)
    elsewhere = importer.to_envelopes(
        made_up().messages, chat_id=-1009999999999, zone=HELSINKI, albums=albums
    )

    count = await importer.count_new(committed_db, OWNER_TELEGRAM_ID, elsewhere)

    assert count == importer.ImportCount(stored=16, repeats=0)


async def test_a_dry_run_does_not_count_the_posts_of_another_owner(committed_db: Sessions) -> None:
    envelopes = _envelopes(made_up())
    await importer.import_envelopes(committed_db, 999, envelopes)

    count = await importer.count_new(committed_db, OWNER_TELEGRAM_ID, envelopes)

    assert count == importer.ImportCount(stored=16, repeats=0)


async def test_an_import_of_nothing_does_nothing(committed_db: Sessions) -> None:
    nothing = importer.ImportCount(stored=0, repeats=0)

    assert await importer.count_new(committed_db, OWNER_TELEGRAM_ID, []) == nothing
    assert await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, []) == nothing
    assert await _captures(committed_db) == []


async def test_the_posts_of_an_import_become_finished_items(
    committed_db: Sessions, private_database_url: str
) -> None:
    await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, _envelopes(made_up()))

    await _work_until_done(committed_db, private_database_url)

    async with committed_db() as db:
        items = (await db.execute(sa.select(Item))).scalars().all()
    item_of = {capture.message_id: capture.item_id for capture in await _captures(committed_db)}
    # 16 posts: an album of three, an album of two, and 11 other posts.
    assert len(items) == 13
    assert {item.enrich_state for item in items} == {EnrichState.READY}
    assert item_of[7] == item_of[8] == item_of[9]
    assert item_of[23] == item_of[24]
    assert len(set(item_of.values())) == 13


async def test_an_album_that_a_limit_cut_is_one_item_in_the_end(
    committed_db: Sessions, private_database_url: str
) -> None:
    chat = made_up()
    albums = importer.album_groups(chat.messages)  # found in the whole export

    def part(limit: int | None) -> list[CaptureEnvelope]:
        chosen = importer.in_range(chat.messages, since=None, limit=limit)
        return importer.to_envelopes(chosen, chat_id=CHANNEL, zone=HELSINKI, albums=albums)

    # The first import stops after one photo of the album of three.
    await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, part(limit=1))
    await _work_until_done(committed_db, private_database_url)
    count = await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, part(limit=None))
    await _work_until_done(committed_db, private_database_url)

    assert count == importer.ImportCount(stored=15, repeats=1)
    item_of = {capture.message_id: capture.item_id for capture in await _captures(committed_db)}
    assert item_of[7] is not None
    assert item_of[7] == item_of[8] == item_of[9]


async def test_an_import_that_stops_keeps_the_batches_that_it_finished(
    committed_db: Sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelopes = _envelopes(made_up())
    monkeypatch.setattr(importer, "BATCH_SIZE", 4)
    accept = resolve.accept_capture
    calls = 0

    async def fails_at_the_seventh_post(
        session: AsyncSession, owner_id: int, envelope: CaptureEnvelope
    ) -> int | None:
        nonlocal calls
        calls += 1
        if calls == 7:
            msg = "the database went away"
            raise ConnectionError(msg)
        return await accept(session, owner_id, envelope)

    monkeypatch.setattr(resolve, "accept_capture", fails_at_the_seventh_post)
    with pytest.raises(ConnectionError):
        await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, envelopes)

    # The first batch of four is saved. The second batch failed, so nothing of it is saved:
    # no post exists without its job.
    assert [capture.message_id for capture in await _captures(committed_db)] == [7, 8, 9, 10]
    assert len(await _job_kinds(committed_db)) == 2

    monkeypatch.setattr(resolve, "accept_capture", accept)
    again = await importer.import_envelopes(committed_db, OWNER_TELEGRAM_ID, envelopes)

    assert again == importer.ImportCount(stored=12, repeats=4)
    assert [capture.message_id for capture in await _captures(committed_db)] == MADE_UP_IDS
