"""The files of an export: copied into the blob store, so that the export folder can go.

A file of an export is a path inside its folder (``ExportedFile.path``). The importer copies
the bytes into the blob store, writes a row in ``assets``, and gives the capture the SHA-256 of
the bytes (``MediaRef.sha256``): the key of the copy. Then an extractor reads the copy, and the
folder of the export is not needed any more.

A file is known by its message and its place in the message, not by its path. Two exports of
one chat can give one file two paths (the app for macOS puts a number before the name), and
two files one path (``part-one.pdf`` of two different messages).

A post that an earlier import stored without the copies of its files gets them in a later run,
and its item is read again. A post that the bot stored needs no copy: the bot reads its files
from Telegram.

The path is text from the page of the export. Before a file is opened, its whole path must stay
inside the folder: ``../../.env``, an absolute path, or a link that points out of the folder is
refused and counted. A file that is too large, that the folder does not have, or that cannot be
read is counted too, and the import goes on.
"""

import asyncio
import mimetypes
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.enums import CaptureSource
from unshelve.contracts.protocols import BlobStore
from unshelve.db.models import Asset, Capture
from unshelve.ingest import resolve
from unshelve.ingest.export_html import ExportedMessage

log = structlog.get_logger()

# A larger file is not copied: no extractor reads such a video, and it would fill the disk.
MAX_FILE_BYTES = 50_000_000
UNKNOWN_TYPE = "application/octet-stream"

# The table of types of Python only, without the files of the computer (``/etc/mime.types``):
# so a name gives the same type on each computer. Telegram keeps a voice message as OGG.
_TYPES = mimetypes.MimeTypes()
_TYPES.add_type("audio/ogg", ".ogg")

# A file of an export: the id of its message, and its place among the files of the message.
FileKey = tuple[int, int]

# The posts of a chat that are stored already, by message id: the media of a post from an
# export, or None for a post from the bot (``stored_posts``).
StoredPosts = Mapping[int, Sequence[Mapping[str, Any]] | None]


@dataclass(frozen=True, slots=True)
class StoredFile:
    """The copy of one file of an export: the key in the blob store, the type, and the path."""

    sha256: str
    mime: str
    path: str


@dataclass(slots=True)
class FileCount:
    copied: int = 0  # read and stored now
    known: int = 0  # its post has the copy already: an earlier import
    not_needed: int = 0  # its post came from the bot, which reads the file from Telegram
    too_large: int = 0
    refused: int = 0  # the path leaves the folder of the export
    missing: int = 0  # the folder does not have the file
    unreadable: int = 0  # the file is there, and the system does not let us read it


def inside(folder: Path, path: str) -> Path | None:
    """The file of ``path`` in ``folder``, or None when the path leaves the folder.

    The path is resolved first, with each ``..`` and each link, so a link that points out of
    the folder is refused as well. An absolute path stays itself when it is joined to the
    folder, so it is refused too; an empty path is the folder itself.
    """
    root = folder.resolve()
    target = (root / path).resolve()
    return target if target != root and target.is_relative_to(root) else None


def mime_of(name: str) -> str:
    """The type of a file from its name, as the export gives no type."""
    return _TYPES.guess_type(name)[0] or UNKNOWN_TYPE


async def stored_posts(session: AsyncSession, owner_id: int, chat_id: int) -> StoredPosts:
    """The posts of this chat that are stored already: their first versions, by message id."""
    rows = await session.execute(
        sa.select(Capture.message_id, Capture.source, Capture.media).where(
            Capture.owner_id == owner_id,
            Capture.chat_id == chat_id,
            # The version that an import stores, or finds when the bot was first.
            Capture.edit_seq == 0,
        )
    )
    return {
        message_id: (media if source == CaptureSource.EXPORT else None)
        for message_id, source, media in rows
    }


async def copy_files(
    folder: Path,
    messages: Iterable[ExportedMessage],
    *,
    blobs: BlobStore,
    sessions: async_sessionmaker[AsyncSession],
    owner_id: int,
    posts: StoredPosts,
    dry_run: bool = False,
) -> tuple[dict[FileKey, StoredFile], FileCount]:
    """Copy into the blob store each file of ``messages`` that a post needs.

    A file of a new post is copied. A file of a stored post is copied only when the post came
    from an earlier import and has no copy of it yet. With ``dry_run``, each file is checked
    and counted, and nothing is read or stored.
    """
    stored: dict[FileKey, StoredFile] = {}
    count = FileCount()
    for message in messages:
        for index, file in enumerate(message.files):
            # A file without a path: the export has only its name. Nothing to copy.
            if file.path is None:
                continue
            if message.message_id in posts:
                media = posts[message.message_id]
                entry = media[index] if media is not None and index < len(media) else None
                if entry is not None and entry.get("sha256"):
                    count.known += 1
                    continue
                if entry is None or entry.get("kind") != file.kind:
                    # The bot stored the post, with the file of Telegram. (Or the stored post
                    # has another file at this place: then the copy would be wrong.)
                    count.not_needed += 1
                    continue
            target = inside(folder, file.path)
            if target is None:
                count.refused += 1
                log.warning("import.file_refused", message_id=message.message_id)
                continue
            try:
                if not target.is_file():
                    count.missing += 1
                    continue
                if target.stat().st_size > MAX_FILE_BYTES:
                    count.too_large += 1
                    continue
                data = b"" if dry_run else await asyncio.to_thread(target.read_bytes)
            except OSError as error:
                # For example, no right to read it. One such file does not stop the import.
                count.unreadable += 1
                log.warning(
                    "import.file_unreadable",
                    message_id=message.message_id,
                    error=type(error).__name__,
                )
                continue
            count.copied += 1
            if dry_run:
                continue
            mime = mime_of(file.name or target.name)
            key = await blobs.put(data, mime)
            async with sessions() as session:
                # One row for each content of an owner: the same bytes twice are one copy.
                await session.execute(
                    postgresql.insert(Asset)
                    .values(
                        owner_id=owner_id,
                        sha256=key,
                        mime=mime,
                        size_bytes=len(data),
                        storage_key=key,
                    )
                    .on_conflict_do_nothing()
                )
                await session.commit()
            stored[message.message_id, index] = StoredFile(sha256=key, mime=mime, path=file.path)
    return stored, count


def with_copies(
    entries: Sequence[Mapping[str, Any]], copies: Mapping[int, StoredFile]
) -> list[dict[str, Any]] | None:
    """The media of a post with the keys of their copies, by place, or None when nothing changes.

    ``copies``: the copies of the files of this post, by their place in the post.
    """
    changed = False
    result: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        copy = copies.get(index)
        if copy is not None and not entry.get("sha256"):
            result.append(
                {
                    **entry,
                    "sha256": copy.sha256,
                    "mime": entry.get("mime") or copy.mime,
                    # An export without files gave the post no path. This one has it.
                    "local_path": entry.get("local_path") or copy.path,
                }
            )
            changed = True
        else:
            result.append(dict(entry))
    return result if changed else None


async def fill_captures(
    sessions: async_sessionmaker[AsyncSession],
    owner_id: int,
    chat_id: int,
    stored: Mapping[FileKey, StoredFile],
) -> int:
    """Give the posts of an earlier import the keys of their new copies, and start their items
    again, so that an extractor reads the files. Returns the number of posts."""
    by_message: dict[int, dict[int, StoredFile]] = {}
    for (message_id, index), copy in stored.items():
        by_message.setdefault(message_id, {})[index] = copy
    if not by_message:
        return 0
    filled = 0
    async with sessions() as session:
        captures = (
            await session.scalars(
                sa.select(Capture)
                .where(
                    Capture.owner_id == owner_id,
                    Capture.chat_id == chat_id,
                    # Only a post of an export: the bot reads its files from Telegram.
                    Capture.source == CaptureSource.EXPORT,
                    Capture.message_id.in_(by_message),
                )
                .order_by(Capture.message_id)
            )
        ).all()
        restart: set[int] = set()
        for capture in captures:
            media = with_copies(capture.media, by_message[capture.message_id])
            if media is None:
                # A new post of this run: it has its copies from its envelope.
                continue
            capture.media = media
            filled += 1
            if capture.item_id is not None:
                restart.add(capture.item_id)
        for item_id in sorted(restart):
            await resolve.restart_item(session, owner_id, item_id)
        await session.commit()
    return filled
