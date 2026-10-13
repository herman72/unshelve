"""Import an export of a Telegram chat: each old post becomes a capture, like a post from the bot.

The bot gets only the posts that arrive after it became an administrator (docs/adr/0009).
The history of a channel is in an export. The importer turns each exported message into the
same envelope that the bot makes, and gives it to the same function, ``accept_capture``.

So a post that the bot stored and that is also in the export is stored one time: the two
have the same chat and the same message id. And an import that runs again stores nothing.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.auth import store as auth_store
from unshelve.contracts.dto import CaptureEnvelope, ForwardOrigin, MediaRef
from unshelve.contracts.enums import CaptureSource
from unshelve.db.models import Capture, User
from unshelve.ingest import resolve
from unshelve.ingest.export_html import ExportedMessage

# Telegram lets a person send some files together as one album: photos with videos, documents
# with documents, audio files with audio files. The value is the sort of album.
_ALBUM_SORT = {"photo": "visual", "video": "visual", "document": "document", "audio": "audio"}
BATCH_SIZE = 200


def _album_sort(message: ExportedMessage) -> str | None:
    return next(
        (_ALBUM_SORT[file.kind] for file in message.files if file.kind in _ALBUM_SORT), None
    )


def _same_album(earlier: ExportedMessage, later: ExportedMessage) -> bool:
    """Can two messages that follow each other be parts of one album?"""
    if later.message_id != earlier.message_id + 1 or later.sent_at != earlier.sent_at:
        return False
    if _album_sort(later) != _album_sort(earlier):
        return False
    if later.forwarded_from != earlier.forwarded_from:
        return False
    # The parts of a forwarded album were first sent in the same second, too. The export
    # does not give this date for each message, so it is compared only when it is known.
    known = earlier.forwarded_at is not None and later.forwarded_at is not None
    return not known or earlier.forwarded_at == later.forwarded_at


def album_groups(messages: Sequence[ExportedMessage]) -> dict[int, str]:
    """Find the albums of an export. Returns the album key of each message that is in one.

    The export does not mark an album. Telegram stores an album as one message for each file.
    These messages have ids that follow each other, and the same second as their time
    (docs/adr/0009). So that is the sign. More conditions keep apart the posts that are not
    an album and were forwarded together: see ``_same_album``, and one message of an album
    at most has a text.

    Give this function all the messages of the export, not a part of them. Then the key of
    an album stays the same in each import.
    """
    groups: dict[int, str] = {}
    run: list[ExportedMessage] = []

    def close() -> None:
        if len(run) >= 2 and sum(1 for message in run if message.text) <= 1:
            key = f"export:{run[0].message_id}"
            groups.update({message.message_id: key for message in run})
        run.clear()

    for message in messages:
        if run and not _same_album(run[-1], message):
            close()
        if _album_sort(message) is not None:
            run.append(message)
    close()
    return groups


def to_envelopes(
    messages: Sequence[ExportedMessage],
    *,
    chat_id: int,
    zone: ZoneInfo,
    albums: Mapping[int, str],
) -> list[CaptureEnvelope]:
    """The envelopes of exported messages. Pure: no database.

    ``zone`` is the time zone of the computer that made the export. The export has clock times
    only, and the zone turns them into exact moments. ``albums`` comes from ``album_groups``.
    """
    envelopes: list[CaptureEnvelope] = []
    for message in messages:
        forwarded = None
        if message.forwarded_from:
            when = message.forwarded_at
            forwarded = ForwardOrigin(
                type="unknown",
                date=when.replace(tzinfo=zone) if when else None,
                name=message.forwarded_from,
            )
        envelopes.append(
            CaptureEnvelope(
                source=CaptureSource.EXPORT,
                chat_id=chat_id,
                message_id=message.message_id,
                posted_at=message.sent_at.replace(tzinfo=zone),
                text=message.text,
                urls=list(message.urls),
                media=[
                    MediaRef(kind=file.kind, local_path=file.path, file_name=file.name)
                    for file in message.files
                ],
                media_group_id=albums.get(message.message_id),
                forward_origin=forwarded,
                raw={
                    "export": "telegram-html",
                    "clock_time": message.sent_at.isoformat(),
                    "time_zone": zone.key,
                },
            )
        )
    return envelopes


@dataclass(frozen=True, slots=True)
class ImportCount:
    stored: int  # new captures
    repeats: int  # messages that were stored before, by the bot or by an earlier import


async def count_new(
    sessions: async_sessionmaker[AsyncSession],
    owner_telegram_id: int,
    envelopes: Sequence[CaptureEnvelope],
) -> ImportCount:
    """What an import would do, without doing it."""
    if not envelopes:
        return ImportCount(stored=0, repeats=0)
    async with sessions() as session:
        known = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Capture)
            .join(User, User.id == Capture.owner_id)
            .where(
                User.telegram_user_id == owner_telegram_id,
                Capture.chat_id == envelopes[0].chat_id,
                Capture.edit_seq == 0,
                Capture.message_id.in_([envelope.message_id for envelope in envelopes]),
            )
        )
    repeats = known or 0
    return ImportCount(stored=len(envelopes) - repeats, repeats=repeats)


async def import_envelopes(
    sessions: async_sessionmaker[AsyncSession],
    owner_telegram_id: int,
    envelopes: Sequence[CaptureEnvelope],
) -> ImportCount:
    """Store the envelopes. Each new one gets the job that gives it an item.

    The work is saved in batches. If the import stops in the middle, run it again: the
    messages that are stored already are skipped.
    """
    stored = 0
    for start in range(0, len(envelopes), BATCH_SIZE):
        async with sessions() as session:
            owner_id = await auth_store.ensure_owner(session, owner_telegram_id)
            for envelope in envelopes[start : start + BATCH_SIZE]:
                if await resolve.accept_capture(session, owner_id, envelope) is not None:
                    stored += 1
            await session.commit()
    return ImportCount(stored=stored, repeats=len(envelopes) - stored)


def in_range(
    messages: Sequence[ExportedMessage], *, since: datetime | None, limit: int | None
) -> list[ExportedMessage]:
    """The messages of ``since`` and later (clock time of the export), and ``limit`` at most."""
    chosen = [message for message in messages if since is None or message.sent_at >= since]
    return chosen if limit is None else chosen[:limit]
