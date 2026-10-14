"""From a saved message to an item.

A *capture* is one saved message, kept as it arrived. An *item* is the thing to read. Saving
something makes a capture first. Then the capture gets its item, and the pipeline starts for
that item: fetch, enrich, embed.

A *save* is one message, or the messages of one album. A save that is the same thing as an
item that exists does not make a second item. It joins that item, and the item counts one
more save. "The same thing" means: the two share a key (``keys.py``, docs/adr/0003).

Every function runs in the caller's transaction and does not commit. The caller commits. So
the capture, its item and the first job exist together, or none of them does.
"""

from collections.abc import Sequence
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import JobKind
from unshelve.contracts.jobs import FetchItemPayload, IngestAlbumPayload, IngestCapturePayload
from unshelve.db.models import Capture, Item, ItemKey
from unshelve.ingest.keys import keys_of, primary_url
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


async def _wait_for_keys(session: AsyncSession, owner_id: int, keys: Sequence[str]) -> None:
    """Take a lock for each key. It is held until the transaction of the caller ends.

    Two workers can get two saves of the same link at the same moment. Without a lock, the
    two look for the key, the two find nothing, and the two make an item. With the lock, the
    second worker waits here until the first is done, and then it finds the item.

    The keys are sorted. So two transactions that need the same keys ask for them in the same
    order, and they cannot wait for each other for ever.
    """
    for key in keys:
        name = sa.cast(f"unshelve:item_key:{owner_id}:{key}", sa.Text)
        number = sa.func.hashtextextended(name, sa.cast(0, sa.BigInteger))
        await session.execute(sa.select(sa.func.pg_advisory_xact_lock(number)))


async def _root(session: AsyncSession, item_id: int) -> int:
    """The item at the end of the "is a duplicate of" links. For most items: the item itself."""
    while True:
        parent = await session.scalar(sa.select(Item.duplicate_of_id).where(Item.id == item_id))
        if parent is None:
            return item_id
        item_id = parent  # a link goes to an older item only, so this loop ends


async def _items_with(session: AsyncSession, owner_id: int, keys: Sequence[str]) -> set[int]:
    """The items that have one of these keys already."""
    if not keys:
        return set()
    owners = await session.scalars(
        sa.select(ItemKey.item_id)
        .where(ItemKey.owner_id == owner_id, ItemKey.key.in_(keys))
        .distinct()
    )
    return {await _root(session, item_id) for item_id in owners}


async def _join_as_one(session: AsyncSession, same: set[int]) -> None:
    """Record that these items are one thing. The oldest stays, and the others point to it.

    Nothing is moved and nothing is deleted, so a wrong match can be undone (docs/adr/0003).
    """
    winner = min(same)
    losers = sorted(same - {winner})
    if not losers:
        return
    gone = (
        await session.execute(
            sa.select(
                sa.func.sum(Item.capture_count),
                sa.func.min(Item.first_captured_at),
                sa.func.max(Item.last_captured_at),
            ).where(Item.id.in_(losers))
        )
    ).one()
    await session.execute(
        sa.update(Item)
        .where(Item.id == winner)
        .values(
            capture_count=Item.capture_count + gone[0],
            first_captured_at=sa.func.least(Item.first_captured_at, gone[1]),
            last_captured_at=sa.func.greatest(Item.last_captured_at, gone[2]),
        )
    )
    await session.execute(
        sa.update(Item)
        .where(sa.or_(Item.id.in_(losers), Item.duplicate_of_id.in_(losers)))
        .values(duplicate_of_id=winner)
    )


async def _give_item(session: AsyncSession, owner_id: int, captures: Sequence[Capture]) -> int:
    """Give the captures of one save their item. Returns the id of the item."""
    keys = keys_of(captures)
    await _wait_for_keys(session, owner_id, keys)
    found = await _items_with(session, owner_id, keys)
    homes = {capture.item_id for capture in captures if capture.item_id is not None}
    first = min(capture.posted_at for capture in captures)
    last = max(capture.posted_at for capture in captures)

    if homes:
        # The save has its item: a part of an album arrived late, or a job ran a second time.
        # It is no new save.
        home, again = min(homes), False
    elif found:
        # The thing was saved before. The oldest item is the one that stays.
        home, again = min(found), True
    else:
        home, again = None, False

    if home is None:
        home = (
            await session.execute(
                sa.insert(Item)
                .values(
                    owner_id=owner_id,
                    canonical_url=primary_url(captures),
                    first_captured_at=first,
                    last_captured_at=last,
                )
                .returning(Item.id)
            )
        ).scalar_one()
        await queue.enqueue(
            session,
            JobKind.FETCH_ITEM,
            FetchItemPayload(item_id=home),
            owner_id=owner_id,
            dedupe_key=f"item:{home}",
        )
    else:
        await session.execute(
            sa.update(Item)
            .where(Item.id == home)
            .values(
                # A repeated save is a sign of interest. The ranking uses this number.
                capture_count=Item.capture_count + (1 if again else 0),
                first_captured_at=sa.func.least(Item.first_captured_at, first),
                last_captured_at=sa.func.greatest(Item.last_captured_at, last),
            )
        )

    for capture in captures:
        if capture.item_id is None:
            capture.item_id = home
    if keys:
        await session.execute(
            postgresql.insert(ItemKey)
            .values([{"owner_id": owner_id, "key": key, "item_id": home} for key in keys])
            # A key that an item has already stays with that item.
            .on_conflict_do_nothing()
        )
    # This save can share one key with one item and another key with a second item.
    await _join_as_one(session, found | {await _root(session, home)})
    return home


async def resolve_capture(session: AsyncSession, capture_id: int) -> int:
    """Give a capture its item and start the pipeline for it. Returns the id of the item.

    Safe to call again for the same capture: the second call finds the item and adds nothing.
    """
    capture = (
        await session.execute(
            # The row is locked, so two calls for one capture cannot both give it an item.
            sa.select(Capture).where(Capture.id == capture_id).with_for_update()
        )
    ).scalar_one()
    return await _give_item(session, capture.owner_id, [capture])


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
    return await _give_item(session, owner_id, parts)


async def rekey_item(session: AsyncSession, item_id: int) -> int:
    """Give an item the keys of its saves again. Returns the number of keys that are new.

    For an item that was made before the keys existed, and for all items after a change of
    the rules for an address (``canonical.VERSION``). The stored messages are the source: the
    keys can always be made again from them.

    When a key belongs to a different item already, the two items are one thing, and they
    are joined. Safe to call again.
    """
    captures = (
        (
            await session.execute(
                sa.select(Capture)
                .where(Capture.item_id == item_id, Capture.edit_seq == 0)
                .order_by(Capture.chat_id, Capture.message_id)
            )
        )
        .scalars()
        .all()
    )
    if not captures:
        return 0
    owner_id = captures[0].owner_id
    # The messages of one album are one save. Each other message is a save of its own.
    saves: dict[tuple[int, object], list[Capture]] = {}
    for capture in captures:
        album = capture.media_group_id
        saves.setdefault((capture.chat_id, album or capture.id), []).append(capture)
    keys = sorted({key for save in saves.values() for key in keys_of(save)})
    if not keys:
        return 0

    await _wait_for_keys(session, owner_id, keys)
    found = await _items_with(session, owner_id, keys)
    new = await session.scalars(
        postgresql.insert(ItemKey)
        .values([{"owner_id": owner_id, "key": key, "item_id": item_id} for key in keys])
        .on_conflict_do_nothing()
        .returning(ItemKey.key)
    )
    added = len(new.all())
    await _join_as_one(session, found | {await _root(session, item_id)})
    return added
