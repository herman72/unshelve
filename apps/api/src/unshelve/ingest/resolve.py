"""From a saved message to an item.

A *capture* is one saved message, kept as it arrived. An *item* is the thing to read. Saving
something makes a capture first. Then the capture gets its item, and the pipeline starts for
that item: fetch, enrich, embed.

This is the first, simple version: every new capture becomes a new item. Recognising that two
captures are the same thing (a link saved twice, the photos of one album) arrives with the
resolver of milestone M1 (docs/adr/0003).

Every function runs in the caller's transaction and does not commit. The caller commits. So
the capture, its item and the first job exist together, or none of them does.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import FetchItemPayload
from unshelve.db.models import Capture, Item
from unshelve.queue import store as queue

# Captures that did not come from a Telegram chat have no chat. They share this number.
API_CHAT_ID = 0


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
