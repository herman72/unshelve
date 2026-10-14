"""From a saved message to an item. Each test name is one rule."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.auth import store as auth
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import CaptureSource, EnrichState, ItemStatus, JobKind, JobState
from unshelve.db.models import Capture, Item, ItemKey, Job
from unshelve.ingest import resolve
from unshelve.ingest.keys import text_key

NOON = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
URL = "https://example.com/how-pgvector-works"


def _envelope(message_id: int = 7, **fields: object) -> CaptureEnvelope:
    values: dict[str, object] = {
        "source": CaptureSource.BOT,
        "chat_id": -1001234567890,
        "message_id": message_id,
        "posted_at": NOON,
        "text": f"Worth a read {URL}",
        "urls": [URL],
    }
    return CaptureEnvelope.model_validate(values | fields)


async def _count(db: AsyncSession, table: type[object]) -> int:
    return (await db.execute(sa.select(sa.func.count()).select_from(table))).scalar_one()


async def test_a_new_message_is_stored_as_it_arrived(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    capture_id = await resolve.store_capture(db, owner_id, _envelope())

    capture = await db.get_one(Capture, capture_id)
    assert capture.source is CaptureSource.BOT
    assert (capture.chat_id, capture.message_id, capture.edit_seq) == (-1001234567890, 7, 0)
    assert capture.text == f"Worth a read {URL}"
    assert capture.urls == [URL]
    assert capture.posted_at == NOON
    assert capture.item_id is None


async def test_the_same_message_is_stored_once(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await resolve.store_capture(db, owner_id, _envelope())

    again = await resolve.store_capture(db, owner_id, _envelope(text="the text changed"))

    assert first is not None
    assert again is None
    assert await _count(db, Capture) == 1


async def test_an_edit_of_a_message_is_a_capture_of_its_own(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await resolve.store_capture(db, owner_id, _envelope())

    edited = await resolve.store_capture(db, owner_id, _envelope(edit_seq=1))

    assert edited is not None
    assert await _count(db, Capture) == 2


async def test_a_capture_gets_an_item_and_the_first_job(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    capture_id = await resolve.store_capture(db, owner_id, _envelope())
    assert capture_id is not None

    item_id = await resolve.resolve_capture(db, capture_id)

    item = await db.get_one(Item, item_id)
    assert item.owner_id == owner_id
    assert item.canonical_url == URL
    assert item.status is ItemStatus.INBOX
    assert item.enrich_state is EnrichState.PENDING
    assert (item.first_captured_at, item.last_captured_at) == (NOON, NOON)
    assert (await db.get_one(Capture, capture_id)).item_id == item_id
    job = (await db.execute(sa.select(Job))).scalar_one()
    assert (job.kind, job.state, job.owner_id) == (JobKind.FETCH_ITEM, JobState.PENDING, owner_id)
    assert job.payload == {"item_id": item_id}


async def test_a_note_without_a_link_gets_an_item_without_an_address(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    capture_id = await resolve.store_capture(
        db, owner_id, _envelope(text="Idea: compare RRF with a weighted sum", urls=[])
    )
    assert capture_id is not None

    item = await db.get_one(Item, await resolve.resolve_capture(db, capture_id))

    assert item.canonical_url is None


async def test_resolving_a_capture_again_changes_nothing(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    capture_id = await resolve.store_capture(db, owner_id, _envelope())
    assert capture_id is not None
    first = await resolve.resolve_capture(db, capture_id)

    second = await resolve.resolve_capture(db, capture_id)

    assert second == first
    assert await _count(db, Item) == 1
    assert await _count(db, Job) == 1
    # A job that runs two times must not count the same save two times.
    assert (await db.get_one(Item, first)).capture_count == 1


async def test_an_item_gets_the_canonical_form_of_its_address_and_its_key(
    db: AsyncSession,
) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    shared = "http://www.example.com/how-pgvector-works/?utm_source=telegram#top"

    item_id = await _saved(db, owner_id, 7, urls=[shared])

    assert (await db.get_one(Item, item_id)).canonical_url == URL
    assert await _keys(db) == {f"url:{URL}": item_id}


# --- The same thing, saved again -------------------------------------------------------------


async def _saved(db: AsyncSession, owner_id: int, message_id: int, **fields: object) -> int:
    """Store one message and give it its item. Returns the id of the item."""
    capture_id = await resolve.store_capture(db, owner_id, _envelope(message_id, **fields))
    assert capture_id is not None
    return await resolve.resolve_capture(db, capture_id)


async def _keys(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(sa.select(ItemKey.key, ItemKey.item_id))
    return {row.key: row.item_id for row in rows}


async def _item(db: AsyncSession, item_id: int) -> Item:
    item = await db.get_one(Item, item_id)
    await db.refresh(item)  # the resolver changes items with statements, not through this object
    return item


async def test_a_link_that_is_saved_again_joins_its_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await _saved(db, owner_id, 7)

    second = await _saved(db, owner_id, 8, posted_at=NOON + timedelta(days=30))

    assert second == first
    assert await _count(db, Item) == 1
    item = await _item(db, first)
    assert item.capture_count == 2
    assert (item.first_captured_at, item.last_captured_at) == (NOON, NOON + timedelta(days=30))
    linked = (await db.execute(sa.select(Capture.item_id))).scalars()
    assert list(linked) == [first, first]
    # The item exists and its pipeline ran or will run: no second job.
    assert await _count(db, Job) == 1


async def test_two_forms_of_one_address_are_one_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await _saved(db, owner_id, 7, urls=["https://example.com/how-pgvector-works"])

    second = await _saved(
        db, owner_id, 8, urls=["http://www.example.com/how-pgvector-works/?utm_source=telegram"]
    )

    assert second == first
    assert (await _item(db, first)).capture_count == 2


async def test_two_different_links_are_two_items(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await _saved(db, owner_id, 7)

    second = await _saved(db, owner_id, 8, urls=["https://example.com/how-pgvector-works?page=2"])

    assert second != first
    assert (await _item(db, first)).capture_count == 1
    assert (await _item(db, second)).capture_count == 1


async def test_an_older_post_that_comes_in_later_moves_the_first_date_back(
    db: AsyncSession,
) -> None:
    # The bot stored a post of today. Then an import brings a post of last year with the link.
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _saved(db, owner_id, 500)

    await _saved(db, owner_id, 12, posted_at=NOON - timedelta(days=365))

    item = await _item(db, item_id)
    assert (item.first_captured_at, item.last_captured_at) == (NOON - timedelta(days=365), NOON)
    assert item.capture_count == 2


async def test_a_note_that_is_saved_again_joins_its_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await _saved(db, owner_id, 7, text="Idea: compare RRF with a weighted sum", urls=[])

    second = await _saved(db, owner_id, 8, text="idea:  compare RRF with a weighted sum\n", urls=[])
    third = await _saved(db, owner_id, 9, text="Idea: compare RRF with a product", urls=[])

    assert second == first
    assert third != first
    assert (await _item(db, first)).capture_count == 2


async def test_a_note_and_a_link_with_the_same_words_are_two_items(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    note = await _saved(db, owner_id, 7, text="Read this", urls=[])

    link = await _saved(db, owner_id, 8, text="Read this", urls=[URL])

    assert link != note


async def test_a_post_that_is_forwarded_again_joins_its_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    source = {"type": "channel", "chat_id": -1001111111111, "message_id": 590}
    first = await _saved(db, owner_id, 7, text="A post", urls=[], forward_origin=source)

    # The channel changed the text of its post, and then you forwarded it again.
    second = await _saved(db, owner_id, 8, text="A post, corrected", urls=[], forward_origin=source)

    assert second == first
    assert set(await _keys(db)) == {
        "tg_fwd:-1001111111111:590",
        text_key("A post"),
        text_key("A post, corrected"),
    }


async def test_a_forwarded_post_and_its_link_saved_alone_are_one_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    source = {"type": "channel", "chat_id": -1001111111111, "message_id": 590}
    forwarded = await _saved(db, owner_id, 7, forward_origin=source)

    alone = await _saved(db, owner_id, 8, text=URL)

    assert alone == forwarded
    assert (await _item(db, forwarded)).capture_count == 2


async def test_a_file_that_is_saved_again_joins_its_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    photo = {"kind": "photo", "file_id": "id-a", "file_unique_id": "unique-a"}
    first = await _saved(db, owner_id, 7, text="The whiteboard", urls=[], media=[photo])

    second = await _saved(db, owner_id, 8, text="", urls=[], media=[photo])

    assert second == first
    assert (await _item(db, first)).capture_count == 2


async def test_the_items_of_two_owners_do_not_join(db: AsyncSession) -> None:
    mine = await _saved(db, await auth.ensure_owner(db, 1), 7)

    theirs = await _saved(db, await auth.ensure_owner(db, 2), 7)

    assert theirs != mine
    assert (await _item(db, mine)).capture_count == 1


async def test_a_very_long_address_can_be_saved_and_found_again(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    long_address = "https://example.com/search?q=" + "a" * 3000
    first = await _saved(db, owner_id, 7, urls=[long_address])

    second = await _saved(db, owner_id, 8, urls=[long_address])

    assert second == first
    (key,) = await _keys(db)
    assert key.startswith("url:sha256:")
    assert (await _item(db, first)).canonical_url == long_address


async def test_a_save_that_shares_keys_with_two_items_joins_the_two(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    source = {"type": "channel", "chat_id": -1001111111111, "message_id": 590}
    link = await _saved(db, owner_id, 7)  # the link alone
    post = await _saved(db, owner_id, 8, text="A post", urls=[], forward_origin=source)
    assert post != link

    # The channel added the link to its post, and you forwarded the post again.
    both = await _saved(db, owner_id, 9, forward_origin=source, posted_at=NOON + timedelta(hours=1))

    # The older item stays. The newer item points to it, and nothing is deleted.
    assert both == link
    assert (await _item(db, post)).duplicate_of_id == link
    assert (await _item(db, link)).duplicate_of_id is None
    # Three saves of one thing: the link, the post, and the post with the link.
    assert (await _item(db, link)).capture_count == 3
    assert (await _item(db, link)).last_captured_at == NOON + timedelta(hours=1)
    assert await _count(db, Item) == 2
    linked = (await db.execute(sa.select(Capture.item_id).order_by(Capture.message_id))).scalars()
    assert list(linked) == [link, post, link]


async def test_two_joined_items_keep_the_first_and_the_last_date_of_the_two(
    db: AsyncSession,
) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    source = {"type": "channel", "chat_id": -1001111111111, "message_id": 590}
    link = await _saved(db, owner_id, 50)
    post = await _saved(
        db,
        owner_id,
        60,
        text="A post",
        urls=[],
        forward_origin=source,
        posted_at=NOON + timedelta(hours=2),
    )

    # An import brings an old post that has the link and the same source.
    await _saved(db, owner_id, 9, forward_origin=source, posted_at=NOON - timedelta(days=1))

    older = await _item(db, link)
    assert (await _item(db, post)).duplicate_of_id == link
    assert older.first_captured_at == NOON - timedelta(days=1)
    # The newer item was saved last. Its date is not lost when it points to the older item.
    assert older.last_captured_at == NOON + timedelta(hours=2)


async def test_a_save_joins_the_item_at_the_end_of_the_duplicate_links(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    source = {"type": "channel", "chat_id": -1001111111111, "message_id": 590}
    link = await _saved(db, owner_id, 7)
    post = await _saved(db, owner_id, 8, text="A post", urls=[], forward_origin=source)
    await _saved(db, owner_id, 9, forward_origin=source)  # joins the two

    # This save has only the key of the newer item, which is a duplicate now.
    again = await _saved(db, owner_id, 10, text="A post", urls=[], forward_origin=source)

    assert again == link
    assert (await _item(db, link)).capture_count == 4
    assert (await _item(db, post)).capture_count == 1


# --- The way in for the bot and the importer -------------------------------------------------


async def _jobs(db: AsyncSession) -> list[Job]:
    return list((await db.execute(sa.select(Job).order_by(Job.id))).scalars().all())


async def test_a_new_message_gets_a_job_and_a_repeat_gets_none(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    capture_id = await resolve.accept_capture(db, owner_id, _envelope())
    again = await resolve.accept_capture(db, owner_id, _envelope())

    assert capture_id is not None
    assert again is None
    (job,) = await _jobs(db)
    assert (job.kind, job.state) == (JobKind.INGEST_CAPTURE, JobState.PENDING)
    assert job.payload == {"capture_id": capture_id}
    assert job.run_after == job.created_at  # it can start at once


async def test_the_parts_of_an_album_share_one_job_that_starts_later(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    for message_id in (7, 8, 9):
        await resolve.accept_capture(
            db,
            owner_id,
            _envelope(message_id, text="", urls=[], media_group_id="14335384938541892"),
        )

    assert await _count(db, Capture) == 3
    (job,) = await _jobs(db)
    assert job.kind is JobKind.INGEST_ALBUM
    assert job.payload == {
        "owner_id": owner_id,
        "chat_id": -1001234567890,
        "media_group_id": "14335384938541892",
    }
    assert job.run_after - job.created_at == resolve.ALBUM_WAIT


async def test_an_edit_is_stored_and_gets_no_job(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await resolve.accept_capture(db, owner_id, _envelope())

    edit = await resolve.accept_capture(db, owner_id, _envelope(edit_seq=1791923347))

    assert edit is not None
    assert await _count(db, Capture) == 2
    assert [job.kind for job in await _jobs(db)] == [JobKind.INGEST_CAPTURE]


# --- An album ----------------------------------------------------------------------------------

ALBUM = "14335384938541892"
CHAT = -1001234567890


async def _album_part(db: AsyncSession, owner_id: int, message_id: int, **fields: object) -> None:
    values: dict[str, object] = {"text": "", "urls": list[str](), "media_group_id": ALBUM}
    values.update(fields)
    assert await resolve.store_capture(db, owner_id, _envelope(message_id, **values)) is not None


async def test_the_messages_of_an_album_become_one_item(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7, text=f"Slides from the talk {URL}", urls=[URL])
    await _album_part(db, owner_id, 8, posted_at=NOON + timedelta(seconds=1))
    await _album_part(db, owner_id, 9, posted_at=NOON + timedelta(seconds=2))

    item_id = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    assert item_id is not None
    assert await _count(db, Item) == 1
    item = await db.get_one(Item, item_id)
    assert item.canonical_url == URL
    assert item.first_captured_at == NOON
    assert item.last_captured_at == NOON + timedelta(seconds=2)
    # Three messages, and one save.
    assert item.capture_count == 1
    linked = (await db.execute(sa.select(Capture.item_id).order_by(Capture.message_id))).scalars()
    assert list(linked) == [item_id, item_id, item_id]
    (job,) = await _jobs(db)
    assert (job.kind, job.payload) == (JobKind.FETCH_ITEM, {"item_id": item_id})


async def test_a_late_part_of_an_album_joins_the_item_that_exists(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7)
    await _album_part(db, owner_id, 8)
    first = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    await _album_part(db, owner_id, 9)  # it arrived after the job ran
    second = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    assert second == first
    assert await _count(db, Item) == 1
    assert await _count(db, Job) == 1
    linked = (await db.execute(sa.select(Capture.item_id))).scalars()
    assert set(linked) == {first}
    # The late part belongs to the save that is counted already.
    assert first is not None
    assert (await _item(db, first)).capture_count == 1


def _photo(name: str) -> dict[str, str]:
    return {"kind": "photo", "file_id": f"id-{name}", "file_unique_id": f"unique-{name}"}


async def test_an_album_that_is_forwarded_again_counts_one_more_save(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    for message_id, name in ((7, "a"), (8, "b"), (9, "c")):
        await _album_part(db, owner_id, message_id, media=[_photo(name)])
    first = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)
    assert first is not None

    # The same three photos arrive again. Telegram gives the new album a new number.
    for message_id, name in ((20, "a"), (21, "b"), (22, "c")):
        await _album_part(db, owner_id, message_id, media=[_photo(name)], media_group_id="999")
    second = await resolve.resolve_album(db, owner_id, CHAT, "999")

    assert second == first
    assert await _count(db, Item) == 1
    # Six messages, and two saves.
    assert (await _item(db, first)).capture_count == 2
    linked = (await db.execute(sa.select(Capture.item_id))).scalars()
    assert list(linked) == [first] * 6


async def test_one_photo_of_an_album_that_is_saved_alone_joins_the_album(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7, media=[_photo("a")])
    await _album_part(db, owner_id, 8, media=[_photo("b")])
    album = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)
    assert album is not None

    alone = await _saved(db, owner_id, 30, text="", urls=[], media=[_photo("b")])

    assert alone == album
    assert (await _item(db, album)).capture_count == 2


async def test_an_album_job_that_runs_again_does_not_count_the_album_again(
    db: AsyncSession,
) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7, media=[_photo("a")])
    await _album_part(db, owner_id, 8, media=[_photo("b")])
    first = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)
    assert first is not None

    second = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    assert second == first
    assert (await _item(db, first)).capture_count == 1
    assert await _count(db, Job) == 1


async def test_an_album_of_a_different_chat_is_a_different_album(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    await _album_part(db, owner_id, 7)
    await _album_part(db, owner_id, 7, chat_id=-1009999999999)

    await resolve.resolve_album(db, owner_id, CHAT, ALBUM)

    assert await _count(db, Item) == 1
    rows = await db.execute(sa.select(Capture.chat_id, Capture.item_id))
    assert {chat_id: item_id is not None for chat_id, item_id in rows} == {
        CHAT: True,
        -1009999999999: False,
    }


async def test_an_album_that_nobody_stored_is_nothing(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)

    assert await resolve.resolve_album(db, owner_id, CHAT, "no-such-album") is None


async def test_api_captures_get_message_ids_that_do_not_repeat(db: AsyncSession) -> None:
    first = await resolve.next_api_message_id(db)
    second = await resolve.next_api_message_id(db)

    assert second > first


# --- Keys for the items that exist already ---------------------------------------------------


async def _saved_before_the_keys_existed(
    db: AsyncSession, owner_id: int, message_id: int, **fields: object
) -> int:
    item_id = await _saved(db, owner_id, message_id, **fields)
    await db.execute(sa.delete(ItemKey).where(ItemKey.item_id == item_id))
    return item_id


async def test_an_old_item_gets_its_keys(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _saved_before_the_keys_existed(db, owner_id, 7)

    added = await resolve.rekey_item(db, item_id)

    assert added == 1
    assert await _keys(db) == {f"url:{URL}": item_id}
    # Now a new save of the link finds the item.
    assert await _saved(db, owner_id, 8) == item_id


async def test_two_old_items_that_are_the_same_thing_are_joined(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await _saved_before_the_keys_existed(db, owner_id, 7)
    second = await _saved_before_the_keys_existed(
        db, owner_id, 8, urls=[URL + "?utm_source=telegram"], posted_at=NOON + timedelta(days=3)
    )
    assert second != first

    assert await resolve.rekey_item(db, first) == 1
    assert await resolve.rekey_item(db, second) == 0  # the older item has this key

    older, newer = await _item(db, first), await _item(db, second)
    assert newer.duplicate_of_id == first
    assert older.duplicate_of_id is None
    assert older.capture_count == 2
    assert older.last_captured_at == NOON + timedelta(days=3)


async def test_the_keys_of_an_item_can_be_made_again_and_again(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    first = await _saved_before_the_keys_existed(db, owner_id, 7)
    second = await _saved_before_the_keys_existed(db, owner_id, 8)
    for item_id in (first, second):
        await resolve.rekey_item(db, item_id)

    again = [await resolve.rekey_item(db, item_id) for item_id in (first, second)]

    assert again == [0, 0]
    assert (await _item(db, first)).capture_count == 2
    assert await _keys(db) == {f"url:{URL}": first}


async def test_an_old_album_gets_the_keys_of_the_album_and_of_its_files(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    other_link = "https://example.com/the-second-link"
    await _album_part(db, owner_id, 7, media=[_photo("a")], urls=[URL])
    await _album_part(db, owner_id, 8, media=[_photo("b")], urls=[other_link])
    album = await resolve.resolve_album(db, owner_id, CHAT, ALBUM)
    assert album is not None
    before = await _keys(db)
    await db.execute(sa.delete(ItemKey))

    added = await resolve.rekey_item(db, album)

    assert added == 4
    # An album is one save, so only its first link is a key: the same keys as before.
    assert await _keys(db) == before
    assert set(before) == {
        f"tg_album:{CHAT}:{ALBUM}",
        "tg_file:unique-a",
        "tg_file:unique-b",
        f"url:{URL}",
    }


async def test_an_item_with_two_saves_gets_the_keys_of_each_save(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    source = {"type": "channel", "chat_id": -1001111111111, "message_id": 590}
    # The first save is a post without a link. The second is the same post, with a link now.
    item_id = await _saved(db, owner_id, 7, text="A post", urls=[], forward_origin=source)
    assert await _saved(db, owner_id, 8, forward_origin=source) == item_id
    before = await _keys(db)
    await db.execute(sa.delete(ItemKey))

    await resolve.rekey_item(db, item_id)

    # The text is a key of the first save only: the second save has a link.
    assert await _keys(db) == before
    assert set(before) == {f"url:{URL}", "tg_fwd:-1001111111111:590", text_key("A post")}


async def test_an_edit_of_a_message_gives_no_key(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = await _saved_before_the_keys_existed(db, owner_id, 7)
    edit = await resolve.store_capture(
        db, owner_id, _envelope(7, edit_seq=1791930240, urls=["https://example.com/after-the-edit"])
    )
    assert edit is not None
    await db.execute(sa.update(Capture).where(Capture.id == edit).values(item_id=item_id))

    await resolve.rekey_item(db, item_id)

    assert set(await _keys(db)) == {f"url:{URL}"}


async def test_an_item_without_a_stored_message_has_no_keys(db: AsyncSession) -> None:
    owner_id = await auth.ensure_owner(db, 1)
    item_id = (
        await db.execute(
            sa.insert(Item)
            .values(owner_id=owner_id, first_captured_at=NOON, last_captured_at=NOON)
            .returning(Item.id)
        )
    ).scalar_one()

    assert await resolve.rekey_item(db, item_id) == 0
