"""The files of an export: copied into the blob store, one time, and only from inside the folder
of the export. Each test name is one rule. A real database, a blob store in memory."""

import mimetypes
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.bot.support import CHANNEL, OWNER_TELEGRAM_ID
from tests.ingest.support import (
    PDF,
    PHOTO_ONE,
    PHOTO_TWO,
    made_up_with_files,
    rewrite,
    write,
)
from unshelve.auth import store as auth
from unshelve.config import Settings
from unshelve.contracts.dto import CaptureEnvelope, MediaRef
from unshelve.contracts.enums import CaptureSource, EnrichState, ItemKind, JobKind, JobState
from unshelve.contracts.jobs import Services
from unshelve.contracts.protocols import BlobStore
from unshelve.db.models import Asset, Capture, Item, Job, User
from unshelve.ingest import export_files, importer, resolve
from unshelve.ingest.export_files import FileCount, StoredFile, inside, mime_of, with_copies
from unshelve.ingest.export_html import ExportedFile, ExportedMessage, read_export
from unshelve.queue.registry import discover, registry
from unshelve.queue.worker import Worker, WorkerConfig
from unshelve.services import build_services
from unshelve.testing.fakes import InMemoryBlobStore

Sessions = async_sessionmaker[AsyncSession]

HELSINKI = ZoneInfo("Europe/Helsinki")
SECRET = b"%PDF-1.7 a file outside the export"


class CountingBlobs(InMemoryBlobStore):
    """A blob store in memory that counts its writes."""

    def __init__(self) -> None:
        super().__init__()
        self.puts = 0

    async def put(self, data: bytes, mime: str) -> str:
        self.puts += 1
        return await super().put(data, mime)


def _key(data: bytes) -> str:
    return sha256(data).hexdigest()


async def _import(
    sessions: Sessions,
    blobs: BlobStore,
    folder: Path,
    messages: Sequence[ExportedMessage] | None = None,
    *,
    dry_run: bool = False,
) -> importer.Imported:
    """What ``unshelve import`` does with the export in ``folder``, or with these messages."""
    chosen = read_export(folder).messages if messages is None else messages
    return await importer.import_folder(
        sessions,
        blobs,
        OWNER_TELEGRAM_ID,
        folder,
        chosen,
        chat_id=CHANNEL,
        zone=HELSINKI,
        albums=importer.album_groups(chosen),
        dry_run=dry_run,
    )


def _message(message_id: int, *files: ExportedFile) -> ExportedMessage:
    return ExportedMessage(
        message_id=message_id,
        sent_at=datetime(2026, 10, 14, 0, 10, message_id),  # noqa: DTZ001 - a clock time
        text="",
        urls=(),
        files=files,
        forwarded_from=None,
        forwarded_at=None,
    )


async def _media(sessions: Sessions) -> dict[int, list[dict[str, Any]]]:
    """The media of each stored post, by message id."""
    async with sessions() as db:
        rows = await db.execute(
            sa.select(Capture.message_id, Capture.media).order_by(Capture.message_id)
        )
        return {row.message_id: row.media for row in rows}


async def _assets(sessions: Sessions) -> set[tuple[str, str, int]]:
    async with sessions() as db:
        rows = await db.execute(sa.select(Asset.sha256, Asset.mime, Asset.size_bytes))
        return {(key, mime, size) for key, mime, size in rows}


# --- A path must stay inside the folder of the export ----------------------------------------


def test_a_path_inside_the_folder_gives_its_file(tmp_path: Path) -> None:
    file = write(tmp_path / "files" / "part-one.pdf", PDF)

    assert inside(tmp_path, "files/part-one.pdf") == file.resolve()
    assert inside(tmp_path, "photos/../files/part-one.pdf") == file.resolve()


@pytest.mark.parametrize(
    "path",
    ["../.env", "files/../../.env", "/etc/hosts", "", ".", "files/.."],
)
def test_a_path_that_leaves_the_folder_is_refused(tmp_path: Path, path: str) -> None:
    folder = tmp_path / "export"
    write(folder / "files" / "part-one.pdf", PDF)
    write(tmp_path / ".env", b"A_SECRET=1")

    assert inside(folder, path) is None


def test_a_link_that_points_out_of_the_folder_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "export"
    secret = write(tmp_path / "outside" / "secret.pdf", SECRET)
    (folder / "files").mkdir(parents=True)
    (folder / "files" / "paper.pdf").symlink_to(secret)
    (folder / "photos").symlink_to(tmp_path / "outside", target_is_directory=True)

    assert inside(folder, "files/paper.pdf") is None
    assert inside(folder, "photos/secret.pdf") is None


def test_a_folder_given_through_a_link_keeps_its_files(tmp_path: Path) -> None:
    # On a Mac, /var is a link to /private/var: a folder can have two names.
    file = write(tmp_path / "export" / "files" / "part-one.pdf", PDF)
    (tmp_path / "link").symlink_to(tmp_path / "export", target_is_directory=True)

    assert inside(tmp_path / "link", "files/part-one.pdf") == file.resolve()


def test_a_link_to_a_file_inside_the_folder_is_followed(tmp_path: Path) -> None:
    file = write(tmp_path / "files" / "part-one.pdf", PDF)
    (tmp_path / "files" / "again.pdf").symlink_to(file)

    assert inside(tmp_path, "files/again.pdf") == file.resolve()


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("part-one.pdf", "application/pdf"),
        ("photo_1@13-10-2026_23-25-17.jpg", "image/jpeg"),
        ("talk.mp4", "video/mp4"),
        ("audio_1@13-10-2026_23-37-00.ogg", "audio/ogg"),
        ("notes", "application/octet-stream"),
    ],
)
def test_the_type_of_a_file_comes_from_its_name(name: str, mime: str) -> None:
    assert mime_of(name) == mime


def test_the_type_does_not_come_from_the_files_of_the_computer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # mimetypes.guess_type also reads files such as /etc/mime.types, which differ from one
    # computer to the next. Here the table of the system gives each name a wrong type.
    def wrong(url: str, strict: bool = True) -> tuple[str, None]:
        return "text/x-wrong", None

    monkeypatch.setattr(mimetypes, "guess_type", wrong)

    assert mime_of("part-one.pdf") == "application/pdf"


# --- The copies go to the files of a stored post, by place -----------------------------------

COPY = StoredFile(sha256="a" * 64, mime="application/pdf", path="files/part-one.pdf")


def test_a_copy_goes_to_the_file_at_its_place() -> None:
    entries = [
        {"kind": "photo", "local_path": "photos/photo_1.jpg", "sha256": None},
        {"kind": "document", "local_path": "files/part-one.pdf", "sha256": None, "mime": None},
    ]

    assert with_copies(entries, {1: COPY}) == [
        {"kind": "photo", "local_path": "photos/photo_1.jpg", "sha256": None},
        {
            "kind": "document",
            "local_path": "files/part-one.pdf",
            "sha256": "a" * 64,
            "mime": "application/pdf",
        },
    ]


def test_a_file_with_a_copy_keeps_its_copy() -> None:
    entries = [{"kind": "document", "local_path": "files/part-one.pdf", "sha256": "b" * 64}]

    assert with_copies(entries, {0: COPY}) is None


def test_a_post_of_an_export_without_files_gets_the_path_with_the_copy() -> None:
    # An export can leave the files out: then the post has only the name of its file.
    entries = [{"kind": "document", "local_path": None, "file_name": "part-one.pdf"}]

    [entry] = with_copies(entries, {0: COPY}) or []

    assert (entry["local_path"], entry["sha256"]) == ("files/part-one.pdf", "a" * 64)


# --- An import copies the files ---------------------------------------------------------------


async def test_an_import_copies_each_file_and_each_post_names_its_copy(
    committed_db: Sessions, tmp_path: Path
) -> None:
    blobs = CountingBlobs()

    result = await _import(committed_db, blobs, made_up_with_files(tmp_path / "export"))

    media = await _media(committed_db)
    assert [media[message][0]["sha256"] for message in (7, 8, 23)] == [
        _key(PHOTO_ONE),
        _key(PHOTO_TWO),
        _key(PDF),
    ]
    assert (media[23][0]["local_path"], media[23][0]["mime"]) == (
        "files/part-one.pdf",
        "application/pdf",
    )
    assert await blobs.get(_key(PDF)) == PDF
    assert await _assets(committed_db) == {
        (_key(PHOTO_ONE), "image/jpeg", len(PHOTO_ONE)),
        (_key(PHOTO_TWO), "image/jpeg", len(PHOTO_TWO)),
        (_key(PDF), "application/pdf", len(PDF)),
    }
    # The video, the voice message and the audio file are not in the folder.
    assert result.files == FileCount(copied=3, missing=3)
    assert (result.count.stored, result.filled) == (16, 0)
    assert [media[message][0]["sha256"] for message in (13, 20, 21)] == [None, None, None]


async def test_a_file_that_the_export_did_not_include_gives_no_error(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # Message 24 has the name of its PDF only: the export has no link to the file.
    await _import(committed_db, CountingBlobs(), made_up_with_files(tmp_path / "export"))

    [entry] = (await _media(committed_db))[24]
    assert (entry["file_name"], entry["local_path"], entry["sha256"]) == (
        "part-two.pdf",
        None,
        None,
    )


async def test_a_second_run_copies_no_file_again(committed_db: Sessions, tmp_path: Path) -> None:
    blobs = CountingBlobs()
    folder = made_up_with_files(tmp_path / "export")
    await _import(committed_db, blobs, folder)

    again = await _import(committed_db, blobs, folder)

    assert blobs.puts == 3
    assert again.files == FileCount(known=3, missing=3)
    assert (again.count.stored, again.filled) == (0, 0)
    assert len(await _assets(committed_db)) == 3


async def test_a_file_is_known_by_its_post_not_by_its_path(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # The app for macOS puts a number before the name of a file: one file, two paths.
    blobs = CountingBlobs()
    await _import(committed_db, blobs, made_up_with_files(tmp_path / "desktop"))
    macos = made_up_with_files(tmp_path / "macos")
    (macos / "files" / "part-one.pdf").rename(macos / "files" / "6_part-one.pdf")
    rewrite(macos / "messages2.html", "files/part-one.pdf", "files/6_part-one.pdf")

    again = await _import(committed_db, blobs, macos)

    assert (again.files.copied, again.files.known, blobs.puts) == (0, 3, 3)


async def test_a_dry_run_counts_the_files_and_stores_nothing(
    committed_db: Sessions, tmp_path: Path
) -> None:
    blobs = CountingBlobs()

    result = await _import(
        committed_db, blobs, made_up_with_files(tmp_path / "export"), dry_run=True
    )

    assert result.files == FileCount(copied=3, missing=3)  # "3 to copy"
    assert blobs.puts == 0
    assert await _assets(committed_db) == set()
    assert await _media(committed_db) == {}
    async with committed_db() as db:
        assert await db.scalar(sa.select(sa.func.count()).select_from(User)) == 0


@pytest.mark.parametrize(("limit", "copied"), [(len(PDF), True), (len(PDF) - 1, False)])
async def test_a_file_over_the_limit_is_counted_and_not_copied(
    committed_db: Sessions,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    copied: bool,
) -> None:
    monkeypatch.setattr(export_files, "MAX_FILE_BYTES", limit)

    result = await _import(committed_db, CountingBlobs(), made_up_with_files(tmp_path / "export"))

    assert (result.files.copied, result.files.too_large) == ((3, 0) if copied else (2, 1))
    assert (await _media(committed_db))[23][0]["sha256"] == (_key(PDF) if copied else None)


async def test_a_path_out_of_the_folder_is_refused_and_its_file_is_not_read(
    committed_db: Sessions, tmp_path: Path
) -> None:
    folder = made_up_with_files(tmp_path / "export")
    secret = write(tmp_path / "secret.pdf", SECRET)
    rewrite(folder / "messages2.html", 'href="files/part-one.pdf"', 'href="../secret.pdf"')
    rewrite(
        folder / "messages.html",
        'href="photos/photo_1@13-10-2026_23-25-17.jpg"',
        f'href="{secret}"',
    )
    blobs = CountingBlobs()

    result = await _import(committed_db, blobs, folder)

    assert (result.files.refused, result.files.copied) == (2, 1)
    with pytest.raises(KeyError):
        await blobs.get(_key(SECRET))
    media = await _media(committed_db)
    assert (media[7][0]["sha256"], media[23][0]["sha256"]) == (None, None)


@pytest.mark.skipif(os.geteuid() == 0, reason="the root user can read each file")
async def test_a_file_that_cannot_be_read_is_counted_and_the_import_goes_on(
    committed_db: Sessions, tmp_path: Path
) -> None:
    folder = made_up_with_files(tmp_path / "export")
    (folder / "files" / "part-one.pdf").chmod(0)

    try:
        result = await _import(committed_db, CountingBlobs(), folder)
    finally:
        (folder / "files" / "part-one.pdf").chmod(0o600)

    assert (result.files.unreadable, result.files.copied) == (1, 2)
    assert len(await _media(committed_db)) == 16


async def test_a_post_that_the_bot_stored_needs_no_copy(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # The bot stored message 23 first. Its item reads the PDF from Telegram.
    from_bot = MediaRef(kind="document", file_id="BQACAgMadeUp", file_unique_id="AgADmadeup")
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        envelope = CaptureEnvelope(
            source=CaptureSource.BOT,
            chat_id=CHANNEL,
            message_id=23,
            posted_at=datetime(2026, 10, 13, 21, 10, 5, tzinfo=UTC),
            text="The two parts of the paper",
            media=[from_bot],
        )
        assert await resolve.accept_capture(db, owner_id, envelope) is not None
        await db.commit()
    blobs = CountingBlobs()

    result = await _import(committed_db, blobs, made_up_with_files(tmp_path / "export"))

    assert (result.files.copied, result.files.not_needed) == (2, 1)
    [entry] = (await _media(committed_db))[23]
    assert (entry["file_unique_id"], entry["sha256"]) == ("AgADmadeup", None)
    with pytest.raises(KeyError):
        await blobs.get(_key(PDF))


async def test_one_path_of_two_exports_gives_each_post_its_own_file(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # Two posts sent a file with the same name. The export of the whole chat calls the second
    # one "part-one (1).pdf"; a later export of the second post alone calls it "part-one.pdf".
    whole = [
        _message(23, ExportedFile("document", "files/part-one.pdf", "part-one.pdf")),
        _message(24, ExportedFile("document", "files/part-one (1).pdf", "part-one.pdf")),
    ]
    alone = [_message(24, ExportedFile("document", "files/part-one.pdf", "part-one.pdf"))]
    # The posts of the whole chat were imported before the copies: no file was copied.
    await importer.import_envelopes(
        committed_db,
        OWNER_TELEGRAM_ID,
        importer.to_envelopes(whole, chat_id=CHANNEL, zone=HELSINKI, albums={}),
    )
    folder = tmp_path / "alone"
    write(folder / "files" / "part-one.pdf", SECRET)

    await _import(committed_db, CountingBlobs(), folder, alone)

    media = await _media(committed_db)
    assert (media[23][0]["sha256"], media[24][0]["sha256"]) == (None, _key(SECRET))


async def test_each_file_of_a_post_is_known_by_its_place(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # A stored post of two files: the first has its copy, the second not yet.
    write(tmp_path / "files" / "one.pdf", PDF)
    write(tmp_path / "files" / "two.pdf", SECRET)
    message = _message(
        23,
        ExportedFile("document", "files/one.pdf", "one.pdf"),
        ExportedFile("document", "files/two.pdf", "two.pdf"),
    )
    posts = {23: [{"kind": "document", "sha256": _key(PDF)}, {"kind": "document", "sha256": None}]}
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        await db.commit()

    stored, count = await export_files.copy_files(
        tmp_path,
        [message],
        blobs=CountingBlobs(),
        sessions=committed_db,
        owner_id=owner_id,
        posts=posts,
    )

    assert (count.known, count.copied) == (1, 1)
    assert list(stored) == [(23, 1)]


async def test_a_stored_post_with_another_file_at_that_place_gets_no_copy(
    committed_db: Sessions, tmp_path: Path
) -> None:
    write(tmp_path / "files" / "part-one.pdf", PDF)
    message = _message(23, ExportedFile("document", "files/part-one.pdf", "part-one.pdf"))
    posts = {23: [{"kind": "photo", "sha256": None}]}

    stored, count = await export_files.copy_files(
        tmp_path, [message], blobs=CountingBlobs(), sessions=committed_db, owner_id=1, posts=posts
    )

    assert (stored, count) == ({}, FileCount(not_needed=1))


async def test_the_posts_of_another_chat_do_not_count(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # Another chat has a message 23 with its copy. Message 23 of this chat is new.
    folder = made_up_with_files(tmp_path / "export")
    chat = read_export(folder)
    await importer.import_folder(
        committed_db,
        CountingBlobs(),
        OWNER_TELEGRAM_ID,
        folder,
        chat.messages,
        chat_id=CHANNEL - 1,
        zone=HELSINKI,
        albums={},
    )

    result = await _import(committed_db, CountingBlobs(), folder)

    assert result.files == FileCount(copied=3, missing=3)


async def test_the_files_of_a_chat_go_to_the_posts_of_that_chat(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # Two chats, each with a message 23 of its own, both imported before the copies.
    other_chat = CHANNEL - 1
    chat = read_export(made_up_with_files(tmp_path / "export"))
    for chat_id in (other_chat, CHANNEL):
        await importer.import_envelopes(
            committed_db,
            OWNER_TELEGRAM_ID,
            importer.to_envelopes(chat.messages, chat_id=chat_id, zone=HELSINKI, albums={}),
        )

    result = await _import(committed_db, CountingBlobs(), tmp_path / "export")

    assert result.files == FileCount(copied=3, missing=3)
    async with committed_db() as db:
        rows = await db.execute(
            sa.select(Capture.chat_id, Capture.media).where(Capture.message_id == 23)
        )
        keys = {row.chat_id: row.media[0]["sha256"] for row in rows}
    assert keys == {CHANNEL: _key(PDF), other_chat: None}


async def test_a_post_that_the_bot_saw_edited_later_still_gets_its_file(
    committed_db: Sessions, tmp_path: Path
) -> None:
    # An import before the copies stored message 23. Then the bot saw an edit of it.
    folder = made_up_with_files(tmp_path / "export")
    chat = read_export(folder)
    await importer.import_envelopes(
        committed_db,
        OWNER_TELEGRAM_ID,
        importer.to_envelopes(chat.messages, chat_id=CHANNEL, zone=HELSINKI, albums={}),
    )
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        edit = CaptureEnvelope(
            source=CaptureSource.BOT,
            chat_id=CHANNEL,
            message_id=23,
            edit_seq=1_760_000_000,
            posted_at=datetime(2026, 10, 13, 21, 10, 5, tzinfo=UTC),
            text="The two parts of the paper, edited",
            media=[MediaRef(kind="document", file_id="BQACAgMadeUp", file_unique_id="AgADm")],
        )
        assert await resolve.accept_capture(db, owner_id, edit) is not None
        await db.commit()

    result = await _import(committed_db, CountingBlobs(), folder)

    assert (result.files.copied, result.files.not_needed) == (3, 0)
    async with committed_db() as db:
        first = await db.scalar(
            sa.select(Capture.media).where(Capture.message_id == 23, Capture.edit_seq == 0)
        )
    assert first is not None
    assert first[0]["sha256"] == _key(PDF)


async def test_a_post_of_the_bot_never_gets_the_copy_of_an_export(
    committed_db: Sessions,
) -> None:
    from_bot = MediaRef(kind="document", file_id="BQACAgMadeUp", file_unique_id="AgADmadeup")
    async with committed_db() as db:
        owner_id = await auth.ensure_owner(db, OWNER_TELEGRAM_ID)
        envelope = CaptureEnvelope(
            source=CaptureSource.BOT,
            chat_id=CHANNEL,
            message_id=23,
            posted_at=datetime(2026, 10, 13, 21, 10, 5, tzinfo=UTC),
            media=[from_bot],
        )
        assert await resolve.accept_capture(db, owner_id, envelope) is not None
        await db.commit()

    filled = await export_files.fill_captures(committed_db, owner_id, CHANNEL, {(23, 0): COPY})

    assert filled == 0
    assert (await _media(committed_db))[23][0]["sha256"] is None


# --- The posts of an earlier import get their files -------------------------------------------


@pytest.fixture
def services(committed_db: Sessions, private_database_url: str) -> Services:
    return build_services(Settings(env="test", database_url=private_database_url), committed_db)


async def _run_all(services: Services) -> None:
    """Run each job, also the job of an album, which waits some seconds for its last part."""
    discover()
    worker = Worker(services, registry, WorkerConfig(worker_id="w1"))
    while True:
        async with services.sessions() as db:
            await db.execute(
                sa.update(Job)
                .where(Job.state == JobState.PENDING)
                .values(run_after=sa.func.now() - sa.text("interval '1 second'"))
            )
            await db.commit()
        if not await worker.run_one():
            return
        while await worker.run_one():
            pass


async def _item_of(sessions: Sessions, message_id: int) -> Item:
    async with sessions() as db:
        capture = (
            await db.scalars(sa.select(Capture).where(Capture.message_id == message_id))
        ).one()
        assert capture.item_id is not None
        return await db.get_one(Item, capture.item_id)


async def test_the_posts_of_an_earlier_import_get_their_files_and_are_read_again(
    services: Services, tmp_path: Path
) -> None:
    folder = made_up_with_files(tmp_path / "export")
    chat = read_export(folder)
    # An import before the copies: the posts name their paths, and nothing is copied.
    older = importer.to_envelopes(
        chat.messages, chat_id=CHANNEL, zone=HELSINKI, albums=importer.album_groups(chat.messages)
    )
    await importer.import_envelopes(services.sessions, OWNER_TELEGRAM_ID, older)
    await _run_all(services)
    before = await _item_of(services.sessions, 23)
    assert (before.enrich_state, before.kind) == (EnrichState.READY, ItemKind.NOTE)

    result = await _import(services.sessions, services.blobs, folder)

    # The album of two photos and the PDF: three posts, two items.
    assert (result.count.stored, result.files.copied, result.filled) == (0, 3, 3)
    assert (await _item_of(services.sessions, 23)).enrich_state == EnrichState.PENDING
    async with services.sessions() as db:
        queued = await db.scalars(
            sa.select(Job).where(Job.kind == JobKind.FETCH_ITEM, Job.state == JobState.PENDING)
        )
        assert len(queued.all()) == 2
    await _run_all(services)
    after = await _item_of(services.sessions, 23)
    assert (after.enrich_state, after.kind, after.title) == (
        EnrichState.READY,
        ItemKind.PDF,
        "Leases for Job Queues in Postgres",
    )
