"""The jobs that give stored messages their items. They start the pipeline of the item.

The bot and the importer only store a message and add one of these jobs. So a slow database
or a busy worker never makes the bot miss a message.
"""

from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import IngestAlbumPayload, IngestCapturePayload, JobContext, Permanent
from unshelve.db.models import Capture
from unshelve.ingest import resolve
from unshelve.queue.registry import registry


@registry.handler(JobKind.INGEST_CAPTURE)
async def ingest_capture(ctx: JobContext, payload: IngestCapturePayload) -> None:
    """Give one stored message its item."""
    async with ctx.services.sessions() as session:
        if await session.get(Capture, payload.capture_id) is None:
            raise Permanent(f"capture {payload.capture_id} does not exist")
        await resolve.resolve_capture(session, payload.capture_id)
        await session.commit()


@registry.handler(JobKind.INGEST_ALBUM)
async def ingest_album(ctx: JobContext, payload: IngestAlbumPayload) -> None:
    """Give the messages of one album one item."""
    async with ctx.services.sessions() as session:
        item_id = await resolve.resolve_album(
            session, payload.owner_id, payload.chat_id, payload.media_group_id
        )
        if item_id is None:
            raise Permanent(f"album {payload.media_group_id} has no stored message")
        await session.commit()
