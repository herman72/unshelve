"""Publishing an item and making it private again, with the exact public record shown first
(docs/adr/0004, docs/learn/45).

The preview takes the path of the feed itself. In a savepoint, the item becomes public; its
record is read from the view ``pub.items`` as the publisher role (``feed.read_records``); then
the savepoint is rolled back, with the role. So the preview is the record that the feed will
hold, by construction: there is no second copy of the rules of the view that could drift.

To publish, the owner sends back the record that they saw. The item becomes public only when
the record that it would have now is the same, byte for byte. If the worker wrote a new summary
in between, the owner gets a conflict and asks for a new preview: they never publish a record
that they did not see.

A public item keeps its name (``public_slug``) and its time of publication for good. Made
private and public again, it comes back under the same name and with the first time.

Each change queues one publish job for the owner. The job waits a little, so a quick series of
changes gives one publish, and one commit with the git target.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.enums import Actor, JobKind, Visibility
from unshelve.contracts.jobs import PublishFeedPayload
from unshelve.db.models import Event, Item
from unshelve.publish import feed
from unshelve.queue import store as queue

# The time in a preview is the time of the publication. An older preview would publish a wrong
# time, so it must be made again.
PREVIEW_MAX_AGE = timedelta(hours=1)
# A clock of a browser may be a little ahead of the clock of the server.
CLOCK_SKEW = timedelta(minutes=1)
# The publish job waits this long: the changes of the next half minute go out with it.
PUBLISH_DELAY = timedelta(seconds=30)
SLUG_MAX = 60

# Words of letters and digits of any script, joined by single hyphens.
_SLUG = re.compile(r"[^\W_]+(?:-[^\W_]+)*")


class NoSuchItemError(Exception):
    pass


class NotPublishableError(Exception):
    """The item is a copy of another item: the view leaves copies out."""


class StalePreviewError(Exception):
    """The record of the preview is not the record that publishing would write now."""


@dataclass(frozen=True, slots=True)
class _State:
    title: str | None
    visibility: Visibility
    public_slug: str | None
    published_at: datetime | None
    duplicate_of_id: int | None


def slug_for(title: str | None) -> str:
    """The public name that a title gives: lowercase words joined by hyphens, at most 60
    characters, cut after a whole word. The letters of each script stay, so a Persian title
    gives a Persian name. A title with no letters or digits gives "item"."""
    text = unicodedata.normalize("NFKC", title or "").casefold()
    # Marks that sit on a letter (vowel signs, accents that NFKC did not join) are no word.
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    words = [word for word in re.split(r"[\W_]+", text) if word]
    if not words:
        return "item"
    slug = words[0][:SLUG_MAX]
    for word in words[1:]:
        if len(slug) + 1 + len(word) > SLUG_MAX:
            break
        slug = f"{slug}-{word}"
    return slug


async def free_slug(session: AsyncSession, base: str) -> str:
    """``base``, else ``base-2``, ``base-3`` and so on: the first that no item uses. The name is
    unique among all items, public or not, so a link never moves to a different item."""
    taken = set(
        (
            await session.scalars(
                sa.select(Item.public_slug).where(
                    sa.or_(Item.public_slug == base, Item.public_slug.like(f"{base}-%"))
                )
            )
        ).all()
    )
    if base not in taken:
        return base
    number = 2
    while f"{base}-{number}" in taken:
        number += 1
    return f"{base}-{number}"


async def preview(
    session: AsyncSession, owner_id: int, item_id: int, *, now: datetime
) -> feed.Record:
    """The record that the feed would hold for the item, if it were published now. Nothing is
    saved. Raises ``NoSuchItemError`` and ``NotPublishableError``."""
    state = await _state(session, owner_id, item_id)
    _check_not_a_copy(state)
    slug = state.public_slug or await free_slug(session, slug_for(state.title))
    # A time of the feed has no fraction of a second, so the preview has none either.
    published_at = state.published_at or now.replace(microsecond=0)
    return await _record_if_public(session, owner_id, item_id, slug, published_at)


async def publish(
    session: AsyncSession, owner_id: int, item_id: int, seen: feed.Record, *, now: datetime
) -> feed.Record:
    """Make the item public with ``seen``, the record of its preview. The caller commits.

    The item takes the name and the time of ``seen`` when it has none of its own yet. Raises
    ``NoSuchItemError``, ``NotPublishableError`` and ``StalePreviewError``; then nothing is written.
    """
    state = await _state(session, owner_id, item_id, lock=True)
    _check_not_a_copy(state)
    slug = state.public_slug or _seen_slug(seen)
    published_at = state.published_at or _seen_time(seen, now)
    record = await _record_if_public(session, owner_id, item_id, slug, published_at)
    if feed.feed_bytes([record]) != feed.feed_bytes([seen]):
        msg = "the public record changed after the preview: look at the new preview"
        raise StalePreviewError(msg)
    if state.visibility == Visibility.PUBLIC:
        return record  # public already, with this record: a second click
    await session.execute(
        sa.update(Item)
        .where(Item.id == item_id)
        .values(visibility=Visibility.PUBLIC, public_slug=slug, published_at=published_at)
        .execution_options(synchronize_session=False)
    )
    session.add(
        Event(
            owner_id=owner_id,
            item_id=item_id,
            actor=Actor.USER,
            type="visibility",
            payload={"visibility": Visibility.PUBLIC.value, "slug": slug},
        )
    )
    await queue_publish(session, owner_id)
    return record


async def unpublish(session: AsyncSession, owner_id: int, item_id: int) -> None:
    """Make the item private. Its name and its time stay, for the day it is public again. The
    caller commits. Raises ``NoSuchItemError``."""
    state = await _state(session, owner_id, item_id, lock=True)
    if state.visibility == Visibility.PRIVATE:
        return  # private already: a second click
    await session.execute(
        sa.update(Item)
        .where(Item.id == item_id)
        .values(visibility=Visibility.PRIVATE)
        .execution_options(synchronize_session=False)
    )
    session.add(
        Event(
            owner_id=owner_id,
            item_id=item_id,
            actor=Actor.USER,
            type="visibility",
            payload={"visibility": Visibility.PRIVATE.value},
        )
    )
    await queue_publish(session, owner_id)


async def _state(
    session: AsyncSession, owner_id: int, item_id: int, *, lock: bool = False
) -> _State:
    query = sa.select(
        Item.title, Item.visibility, Item.public_slug, Item.published_at, Item.duplicate_of_id
    ).where(Item.id == item_id, Item.owner_id == owner_id)
    # Of two clicks at the same moment, the second waits, and then finds the first one done.
    row = (await session.execute(query.with_for_update() if lock else query)).one_or_none()
    if row is None:
        msg = "no such item"
        raise NoSuchItemError(msg)
    return _State(*row)


def _check_not_a_copy(state: _State) -> None:
    if state.duplicate_of_id is not None:
        msg = f"the item is a copy of item {state.duplicate_of_id}: publish that one"
        raise NotPublishableError(msg)


async def _record_if_public(
    session: AsyncSession, owner_id: int, item_id: int, slug: str, published_at: datetime
) -> feed.Record:
    """The record of the item, read by the path of the feed, as if the item were public with
    this name and time. In a savepoint, which is always rolled back."""
    savepoint = await session.begin_nested()
    try:
        await session.execute(
            sa.update(Item)
            .where(Item.id == item_id)
            .values(visibility=Visibility.PUBLIC, public_slug=slug, published_at=published_at)
            .execution_options(synchronize_session=False)
        )
        records = await feed.read_records(session, owner_id, slug=slug)
    except IntegrityError as error:
        msg = f"the public name {slug!r} is taken now: look at the new preview"
        raise StalePreviewError(msg) from error
    finally:
        await savepoint.rollback()
    if not records:  # cannot happen: the item is no copy, so the view shows it
        msg = f"the view does not show item {item_id} as a public item"
        raise RuntimeError(msg)
    return records[0]


def _seen_slug(seen: feed.Record) -> str:
    slug = str(seen.get("id"))
    if len(slug) > SLUG_MAX + 10 or not _SLUG.fullmatch(slug):
        msg = "the public name of the preview is not a name of letters, digits and hyphens"
        raise StalePreviewError(msg)
    return slug


def _seen_time(seen: feed.Record, now: datetime) -> datetime:
    try:
        moment = datetime.strptime(str(seen.get("published_at")), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError as error:
        msg = "the time of the preview is not a time of the feed"
        raise StalePreviewError(msg) from error
    if not now - PREVIEW_MAX_AGE <= moment <= now + CLOCK_SKEW:
        msg = "the preview is more than an hour old: look at a new preview"
        raise StalePreviewError(msg)
    return moment


async def republish_if_public(session: AsyncSession, owner_id: int, item_id: int) -> bool:
    """Queue the publish job when the item is public now. The pipeline calls it when it makes
    an item ready: a new summary or new tags change its public record. True: it queued one."""
    found = await session.scalar(sa.select(Item.visibility).where(Item.id == item_id))
    if found != Visibility.PUBLIC:
        return False
    await queue_publish(session, owner_id)
    return True


async def queue_publish(session: AsyncSession, owner_id: int) -> None:
    """One publish job for the owner. While one waits, a new change needs no second one."""
    await queue.enqueue(
        session,
        JobKind.PUBLISH_FEED,
        PublishFeedPayload(owner_id=owner_id),
        owner_id=owner_id,
        dedupe_key=f"feed:{owner_id}",
        delay=PUBLISH_DELAY,
    )
