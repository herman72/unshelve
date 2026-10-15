"""The files of a saved message, for the fetch step: downloaded once, then read from a copy.

An extractor asks for the bytes of a file (``ctx.files``). The first time, they come from the
source (the Bot API, ``net.telegram_files``), go into the blob store, and get a row in
``assets`` with the id of the file of Telegram. Each later time, for example after an edit of
the message, they come from the copy, and Telegram is not asked.

Two files of Telegram with the same bytes share one copy. Only the first of them finds it by
its id; the second is downloaded again each time, which is rare and harmless.
"""

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from unshelve.contracts.dto import MediaRef
from unshelve.contracts.errors import FetchError
from unshelve.contracts.protocols import BlobStore, FileSource
from unshelve.db.models import Asset

log = structlog.get_logger()

# The type of a file that the message does not name: bytes of an unknown kind.
UNKNOWN_TYPE = "application/octet-stream"


def label_of(media: MediaRef) -> str:
    """The name of a file in an error or a log: never its address, which holds the token."""
    return f"telegram file {media.file_unique_id or 'without an id'}"


class StoredFiles:
    def __init__(
        self,
        source: FileSource | None,
        blobs: BlobStore,
        sessions: async_sessionmaker[AsyncSession],
        owner_id: int,
    ) -> None:
        """``source`` is None without a bot token: then only copies are read."""
        self._source = source
        self._blobs = blobs
        self._sessions = sessions
        self._owner_id = owner_id

    async def read(self, media: MediaRef, *, max_bytes: int) -> bytes:
        kept = await self._kept(media)
        if kept is not None:
            if len(kept) > max_bytes:
                raise FetchError("too_large", label_of(media))
            return kept
        if self._source is None:
            raise FetchError("unsupported", label_of(media))
        # A FetchError of the source goes up unchanged: the fetch step decides what follows.
        data = await self._source.read(media, max_bytes=max_bytes)
        key = await self._blobs.put(data, media.mime or UNKNOWN_TYPE)
        async with self._sessions() as session:
            await session.execute(
                postgresql.insert(Asset)
                .values(
                    owner_id=self._owner_id,
                    sha256=key,
                    mime=media.mime or UNKNOWN_TYPE,
                    size_bytes=len(data),
                    storage_key=key,
                    file_unique_id=media.file_unique_id,
                )
                .on_conflict_do_nothing()
            )
            await session.commit()
        return data

    async def _kept(self, media: MediaRef) -> bytes | None:
        """The bytes of the copy of this file, or None when there is no copy."""
        if media.file_unique_id is None:
            return None
        async with self._sessions() as session:
            key = await session.scalar(
                sa.select(Asset.storage_key).where(
                    Asset.owner_id == self._owner_id,
                    Asset.file_unique_id == media.file_unique_id,
                )
            )
        if key is None:
            return None
        try:
            return await self._blobs.get(key)
        except KeyError:
            # The row is there, but the file is not (a new volume): download it again.
            log.warning("files.copy_missing", file=media.file_unique_id)
            return None
