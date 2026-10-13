"""From a saved message to an item.

A *capture* is one saved message, kept as it arrived. An *item* is the thing to read. Saving
something makes a capture first. Then the capture gets its item, and the pipeline starts for
that item: fetch, enrich, embed.

This is a simple version: a new capture becomes a new item, and the messages of one album
become one item together. Recognising a link that was saved twice arrives with the resolver of
milestone M1 (docs/adr/0003).

Every function runs in the caller's transaction and does not commit. The caller commits. So
the capture, its item and the first job exist together, or none of them does.
"""

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import FetchItemPayload, IngestAlbumPayload, IngestCapturePayload
from unshelve.db.models import Capture, Item
from unshelve.queue import store as queue

# Captures that did not come from a Telegram chat have no chat. They share this number.
API_CHAT_ID = 0
# Telegram sends an album as one message for each photo. The job that joins them starts this
# long after the first part, so that the other parts have time to arrive (docs/adr/0009).
ALBUM_WAIT = timedelta(seconds=5)


async def next_api_message_id(session: AsyncSession) -> int:
    """A message id for a capture that was made through the API, where no message exists."""
    return (await session.execute(sa.select(sa.func.nextval("capture_api_seq")))).scalar_one()


async def store_capture(
    session: AsyncSession, owner_id: int, envelope: CaptureEnvelope
) -> int | None:
    """Store a saved message. Returns its id, or None when this message is stored already.

    The bot can receive the same message twice, and an import can meet a message that the bot
    stored before. The second arrival must change nothing, so the caller starts work only for
    a capture that is new.
    """
    return await session.scalar(
        postgresql.insert(Capture)
        .values(
            owner_id=owner_id,
            source=envelope.source,
            chat_id=envelope.chat_id,
            message_id=envelope.message_id,
            edit_seq=envelope.edit_seq,
            media_group_id=envelope.media_group_id,
            forward_origin=(
                envelope.forward_origin.model_dump(mode="json") if envelope.forward_origin else None
            ),
            posted_at=envelope.posted_at,
            text=envelope.text,
            urls=envelope.urls,
            media=[media.model_dump(mode="json") for media in envelope.media],
            raw=envelope.raw,
        )
        .on_conflict_do_nothing(constraint="uq_captures_message")
        .returning(Capture.id)
    )


async def resolve_capture(session: AsyncSession, capture_id: int) -> int:
    """Give a capture its item and start the pipeline for it. Returns the id of the item.

    Safe to call again for the same capture: the second call finds the item and adds nothing.
    """
    capture = (
        await session.execute(
            # The row is locked, so two calls for one capture cannot both make an item.
            sa.select(Capture).where(Capture.id == capture_id).with_for_update()
        )
    ).scalar_one()
    if capture.item_id is not None:
        return capture.item_id

    item_id = (
        await session.execute(
            sa.insert(Item)
            .values(
                owner_id=capture.owner_id,
                canonical_url=capture.urls[0] if capture.urls else None,
                first_captured_at=capture.posted_at,
                last_captured_at=capture.posted_at,
            )
            .returning(Item.id)
        )
    ).scalar_one()
    capture.item_id = item_id
    await queue.enqueue(
        session,
        JobKind.FETCH_ITEM,
        FetchItemPayload(item_id=item_id),
        owner_id=capture.owner_id,
        dedupe_key=f"item:{item_id}",
    )
    return item_id


async def accept_capture(
    session: AsyncSession, owner_id: int, envelope: CaptureEnvelope
) -> int | None:
    """Store a saved message and add the job that gives it an item.

    This is the way in for the bot and for the importer. It returns the id of the capture, or
    None when the message is stored already. A repeat adds no job.

    An edit of a message is stored, and it gets no job yet: the item of the message exists,
    and updating an item from an edit is work of the resolver.
    """
    capture_id = await store_capture(session, owner_id, envelope)
    if capture_id is None or envelope.edit_seq > 0:
        return capture_id
    if envelope.media_group_id is None:
        await queue.enqueue(
            session,
            JobKind.INGEST_CAPTURE,
            IngestCapturePayload(capture_id=capture_id),
            owner_id=owner_id,
            dedupe_key=f"capture:{capture_id}",
        )
    else:
        # One job for the whole album. Each part asks for it, and the key keeps one.
        await queue.enqueue(
            session,
            JobKind.INGEST_ALBUM,
            IngestAlbumPayload(
                owner_id=owner_id,
                chat_id=envelope.chat_id,
                media_group_id=envelope.media_group_id,
            ),
            owner_id=owner_id,
            dedupe_key=f"album:{envelope.chat_id}:{envelope.media_group_id}",
            delay=ALBUM_WAIT,
        )
    return capture_id


async def resolve_album(
    session: AsyncSession, owner_id: int, chat_id: int, media_group_id: str
) -> int | None:
    """Give the messages of one album one item. Returns its id, or None for an unknown album.

    Safe to call again. A part that arrives late is added to the item that exists.
    """
    parts = (
        (
            await session.execute(
                sa.select(Capture)
                .where(
                    Capture.owner_id == owner_id,
                    Capture.chat_id == chat_id,
                    Capture.media_group_id == media_group_id,
                    Capture.edit_seq == 0,
                )
                .order_by(Capture.message_id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    if not parts:
        return None

    item_id = next((part.item_id for part in parts if part.item_id is not None), None)
    if item_id is None:
        urls = [url for part in parts for url in part.urls]
        item_id = (
            await session.execute(
                sa.insert(Item)
                .values(
                    owner_id=owner_id,
                    canonical_url=urls[0] if urls else None,
                    first_captured_at=min(part.posted_at for part in parts),
                    last_captured_at=max(part.posted_at for part in parts),
                )
                .returning(Item.id)
            )
        ).scalar_one()
        await queue.enqueue(
            session,
            JobKind.FETCH_ITEM,
            FetchItemPayload(item_id=item_id),
            owner_id=owner_id,
            dedupe_key=f"item:{item_id}",
        )
    for part in parts:
        if part.item_id is None:
            part.item_id = item_id
    return item_id
