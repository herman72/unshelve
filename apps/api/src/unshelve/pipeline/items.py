"""The pipeline of one item: fetch, enrich, embed. After the last step the item is ready.

Each step is one job. A step does three things, in this order:

1. It reads the item. If the item is not waiting for this step, the step ran before: stop.
2. It calls a service (an extractor, the model, the embedder). No transaction is open then.
3. In one transaction, it writes its result, moves the item to the next state and adds the
   job of the next step.

Point 3 is what makes a step safe to run twice. The move to the next state happens only if
the item is still in the state that the step started from. A second run finds that the item
has moved, and writes nothing.

The fetch step reads the source with the best extractor (``unshelve.extract.registry``), the
enrich step asks the model for the card (``unshelve.enrich.card``), and the embed step stores
the vectors of the card and of the pieces of the text (``unshelve.embed``). The states are in
``unshelve.contracts.states``.
"""

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.contracts.dto import (
    Extracted,
    ForwardOrigin,
    MediaRef,
    SourceRef,
    TextPart,
)
from unshelve.contracts.enums import Actor, ChunkKind, EnrichState, JobKind, TagSource
from unshelve.contracts.errors import ExtractError, FetchError, ProviderError
from unshelve.contracts.jobs import (
    EmbedItemPayload,
    EnrichItemPayload,
    FetchItemPayload,
    JobContext,
    Permanent,
    Retry,
)
from unshelve.contracts.protocols import ExtractContext, Extractor
from unshelve.contracts.states import check_enrich_change
from unshelve.db.models import Capture, Chunk, Event, Item, ItemContent, ItemTag, Tag
from unshelve.embed.chunks import card_text, pieces_of
from unshelve.embed.sets import ensure_set
from unshelve.enrich.card import (
    MAX_OUTPUT_TOKENS,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    TASK,
    Card,
    key_points_of,
    language_of,
    prompt_text,
    tags_of,
)
from unshelve.extract.registry import FALLBACK, choose
from unshelve.ingest import versions
from unshelve.pipeline.files import StoredFiles
from unshelve.queue import store as queue
from unshelve.queue.registry import registry
from unshelve.reading import reading_minutes

log = structlog.get_logger()

# The card of milestone M2: title, summary, key points, tags and language.
ENRICH_VERSION = 2
MAX_TITLE_LENGTH = 200
# A passing failure of the fetch (a timeout, a busy server) is tried again after 1, 2, 4 and
# 8 minutes, or after the wait that the server asks for, but never later than one hour. The
# last attempt keeps the saved text, so that no item waits for ever.
FIRST_WAIT = timedelta(minutes=1)
LONGEST_WAIT = timedelta(hours=1)
# A failure of the model that can pass is tried again after 2, 8, 32 and 60 minutes, or
# after the wait that the provider gives (a 429, or the next day of the budget), at most one
# day. The last attempt goes on without a card.
MODEL_FIRST_WAIT = timedelta(minutes=2)
MODEL_LONGEST_WAIT = timedelta(hours=1)
PROVIDER_LONGEST_WAIT = timedelta(days=1)


async def _item_waiting_for(session: AsyncSession, item_id: int, state: EnrichState) -> Item | None:
    """The item, if it waits for the step that starts from ``state``. None if that step is done."""
    item = await session.get(Item, item_id)
    if item is None:
        raise Permanent(f"item {item_id} does not exist")
    return item if item.enrich_state is state else None


async def _advance(
    session: AsyncSession,
    item_id: int,
    *,
    start: EnrichState,
    end: EnrichState,
    unchanged: sa.ColumnElement[bool] | None = None,
    **values: Any,
) -> bool:
    """Move the item from ``start`` to ``end`` and set ``values``, in one statement.

    Returns False when the item is not in ``start`` any more. Then another run of the same
    step was faster, or the item was started again, and the caller must write nothing.
    ``unchanged`` is one more condition of the same statement.
    """
    check_enrich_change(start, end)
    where = [Item.id == item_id, Item.enrich_state == start]
    if unchanged is not None:
        where.append(unchanged)
    moved = await session.scalar(
        sa.update(Item).where(*where).values(enrich_state=end, **values).returning(Item.id)
    )
    return moved is not None


# --- Step 1: fetch -----------------------------------------------------------------------


def _linked_now(item_id: int) -> tuple[sa.ScalarSelect[int], sa.ScalarSelect[int]]:
    """The number of the captures of the item, and the highest id among them. A new version
    or a late part of an album changes the pair: captures are linked, never unlinked."""
    of_item = sa.select(Capture).where(Capture.item_id == item_id).subquery()
    return (
        sa.select(sa.func.count()).select_from(of_item).scalar_subquery(),
        sa.select(sa.func.coalesce(sa.func.max(of_item.c.id), 0)).scalar_subquery(),
    )


def _address_to_open(read: versions.Source, canonical_url: str | None) -> str | None:
    """The link as you saved it.

    The canonical address is made for identity: it drops "www." and turns http into https. A
    site can answer only on the form that you saved, so the fetch opens that form.
    """
    if canonical_url is None:
        return None
    saved = read.urls[0] if read.urls else None
    return saved if saved and saved.lower().startswith(("http://", "https://")) else canonical_url


def _wait(error: FetchError, attempts: int) -> timedelta:
    """The pause before the next try of a passing failure."""
    if error.retry_after is not None:
        return min(timedelta(seconds=error.retry_after), LONGEST_WAIT)
    return min(FIRST_WAIT * 2 ** (attempts - 1), LONGEST_WAIT)


def _why_not_read(
    extractor: Extractor, error: FetchError | ExtractError, attempt: int
) -> dict[str, Any]:
    """What the event log keeps about a source that could not be read."""
    if isinstance(error, FetchError):
        return {
            "extractor": extractor.name,
            "reason": error.reason,
            "status": error.status,
            "attempt": attempt,
        }
    return {"extractor": extractor.name, "reason": "no_text", "detail": str(error)[:300]}


@registry.handler(JobKind.FETCH_ITEM)
async def fetch_item(ctx: JobContext, payload: FetchItemPayload) -> None:
    """Read the source of the item with the best extractor, and store its text.

    A dead link, an address that the guard refuses, or a page without an article gives the
    saved text instead, and the item is partial. A passing failure is tried again later, and
    the last attempt also gives the saved text.
    """
    services = ctx.services
    async with services.sessions() as session:
        item = await _item_waiting_for(session, payload.item_id, EnrichState.PENDING)
        if item is None:
            return
        # An item of an album has one capture for each photo, and an edit of a message is a
        # capture too. The item reads the latest version of each message, one time.
        linked = (await session.scalars(sa.select(Capture).where(Capture.item_id == item.id))).all()
        read = versions.source(linked)
        # What was read. If a new version arrives during this step, the result is old.
        seen = (len(linked), max(capture.id for capture in linked) if linked else 0)

    source = SourceRef(
        url=_address_to_open(read, item.canonical_url),
        text=read.text,
        media=[MediaRef.model_validate(media) for media in read.media],
        forward_origin=ForwardOrigin.model_validate(read.forward_origin)
        if read.forward_origin
        else None,
    )
    context = ExtractContext(
        fetcher=services.fetcher,
        blobs=services.blobs,
        llm=services.llm,
        # A file of the message is downloaded once; later runs read its copy.
        files=StoredFiles(services.files, services.blobs, services.sessions, item.owner_id),
    )
    extractor = tried = choose(source)
    extracted: Extracted | None = None
    failure: FetchError | ExtractError | None = None
    try:
        extracted = await extractor.extract(source, context)
    except FetchError as error:
        if error.retryable and ctx.job.attempts < ctx.job.max_attempts:
            detail = f"{error.reason} {error.status}" if error.status else error.reason
            raise Retry(f"fetch: {detail}", after=_wait(error, ctx.job.attempts)) from error
        failure = error
    except ExtractError as error:
        failure = error
    why = _why_not_read(tried, failure, ctx.job.attempts) if failure is not None else None
    if extracted is None:
        extractor = FALLBACK
        extracted = await FALLBACK.extract(source, context)
    found = {
        "title": extracted.title[:MAX_TITLE_LENGTH] if extracted.title else None,
        "author": extracted.author,
        "site": extracted.site,
        "lang": extracted.lang,
        # The length of a video. None for a text: the enrich step counts its reading time.
        "est_minutes": extracted.minutes,
    }

    async with services.sessions() as session:
        moved = await _advance(
            session,
            item.id,
            start=EnrichState.PENDING,
            end=EnrichState.FETCHED,
            unchanged=sa.tuple_(*_linked_now(item.id)) == sa.tuple_(*seen),
            kind=extracted.kind,
            # The item has a link, but its text is only the saved text: the page was not read.
            partial=source.url is not None and extractor is FALLBACK,
            extra_links=extracted.extra_links,
            # Each field is written on each run, also an empty one. After an edit, a field
            # of the old version must not stay.
            **found,
        )
        if moved:
            content = {
                "text": extracted.text,
                "content_hash": hashlib.sha256(extracted.text.encode()).hexdigest(),
                "extractor": extractor.name,
                "extractor_version": extractor.version,
                "final_url": extracted.final_url,
            }
            await session.execute(
                postgresql.insert(ItemContent)
                .values(item_id=item.id, owner_id=item.owner_id, **content)
                .on_conflict_do_update(
                    index_elements=[ItemContent.item_id],
                    set_={**content, "fetched_at": sa.func.now()},
                )
            )
            if why is not None:
                # The log of the item says why it has only the saved text.
                session.add(
                    Event(
                        owner_id=item.owner_id,
                        item_id=item.id,
                        actor=Actor.WORKER,
                        type="fetch_failed",
                        payload=why,
                    )
                )
            await queue.enqueue(
                session,
                JobKind.ENRICH_ITEM,
                EnrichItemPayload(item_id=item.id),
                owner_id=item.owner_id,
                dedupe_key=f"item:{item.id}",
            )
        await session.commit()
    if moved and why is not None:
        # The reason and the code only: a link can hold private words in its path or query.
        log.info("fetch.saved_text", item_id=item.id, **why)


# --- Step 2: enrich ----------------------------------------------------------------------


def _model_wait(error: ProviderError, attempts: int) -> timedelta:
    """The pause before the next question to the model."""
    if error.retry_after is not None:
        return min(timedelta(seconds=error.retry_after), PROVIDER_LONGEST_WAIT)
    return min(MODEL_FIRST_WAIT * 4 ** (attempts - 1), MODEL_LONGEST_WAIT)


async def _replace_model_tags(
    session: AsyncSession, item: Item, tags: list[tuple[str, str]]
) -> None:
    """The tags of the model replace the tags that it gave before. A tag that you set stays."""
    await session.execute(
        sa.delete(ItemTag).where(ItemTag.item_id == item.id, ItemTag.source == TagSource.LLM)
    )
    if not tags:
        return
    await session.execute(
        postgresql.insert(Tag)
        .values([{"owner_id": item.owner_id, "slug": slug, "name": name} for slug, name in tags])
        .on_conflict_do_nothing(constraint="uq_tags_slug")
    )
    tag_ids = await session.scalars(
        sa.select(Tag.id).where(
            Tag.owner_id == item.owner_id, Tag.slug.in_([slug for slug, _ in tags])
        )
    )
    await session.execute(
        postgresql.insert(ItemTag)
        .values(
            [
                {"item_id": item.id, "tag_id": tag_id, "owner_id": item.owner_id}
                for tag_id in tag_ids.all()
            ]
        )
        # The item has the tag already, from you: it stays yours.
        .on_conflict_do_nothing()
    )


@registry.handler(JobKind.ENRICH_ITEM)
async def enrich_item(ctx: JobContext, payload: EnrichItemPayload) -> None:
    """Ask the model for the card of the item: title, summary, key points, tags, language.

    A failure that can pass is tried again later. A failure that cannot pass, or the last
    attempt, gives an empty card, and an event says why: the item goes on, and no item
    waits for ever.
    """
    services = ctx.services
    async with services.sessions() as session:
        item = await _item_waiting_for(session, payload.item_id, EnrichState.FETCHED)
        if item is None:
            return
        text = await session.scalar(
            sa.select(ItemContent.text).where(ItemContent.item_id == item.id)
        )
    if text is None:
        raise Permanent(f"item {item.id} has no text to enrich")

    card: Card | None = None
    failure: ProviderError | None = None
    # Photos without a caption have no text. A model that gets no text invents some, so it
    # is not asked: the item gets an empty card until a later step reads the photos.
    if text.strip():
        try:
            result = await services.llm.generate(
                task=TASK,
                prompt_version=PROMPT_VERSION,
                system=SYSTEM_PROMPT,
                parts=[TextPart(text=prompt_text(text))],
                schema=Card,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
            card = result.value
        except ProviderError as error:
            if error.retryable and ctx.job.attempts < ctx.job.max_attempts:
                raise Retry(
                    f"model: {error}", after=_model_wait(error, ctx.job.attempts)
                ) from error
            failure = error

    written: dict[str, Any] = {
        "summary": None,
        "key_points": [],
        # The minutes that the source gave (the length of a video) stay.
        "est_minutes": sa.func.coalesce(
            Item.est_minutes, reading_minutes(text) if text.strip() else None
        ),
    }
    tags: list[tuple[str, str]] = []
    if card is not None:
        written |= {
            # A title or a language that the page gave is kept. The card fills an empty one.
            "title": sa.func.coalesce(Item.title, card.title.strip()[:MAX_TITLE_LENGTH] or None),
            "summary": card.summary.strip() or None,
            "key_points": key_points_of(card),
        }
        language = language_of(card)
        if language is not None:
            written["lang"] = sa.func.coalesce(Item.lang, language)
        tags = tags_of(card)

    async with services.sessions() as session:
        moved = await _advance(
            session,
            item.id,
            start=EnrichState.FETCHED,
            end=EnrichState.ENRICHED,
            enrich_version=ENRICH_VERSION,
            **written,
        )
        if moved:
            await _replace_model_tags(session, item, tags)
            if failure is not None:
                session.add(
                    Event(
                        owner_id=item.owner_id,
                        item_id=item.id,
                        actor=Actor.WORKER,
                        type="enrich_failed",
                        payload={"reason": str(failure)[:300], "attempt": ctx.job.attempts},
                    )
                )
            await queue.enqueue(
                session,
                JobKind.EMBED_ITEM,
                EmbedItemPayload(item_id=item.id),
                owner_id=item.owner_id,
                dedupe_key=f"item:{item.id}",
            )
        await session.commit()
    if moved and failure is not None:
        log.info("enrich.no_card", item_id=item.id, reason=str(failure)[:300])


# --- Step 3: embed -----------------------------------------------------------------------

# An item gets its vectors after its card. A ready item gets them again when the embedder
# changes: ``unshelve embeddings backfill`` fills a new embedding set.
_EMBEDDABLE = frozenset({EnrichState.ENRICHED, EnrichState.READY})
# When the model wrote no card, the start of the text stands in for it.
CARD_STAND_IN_CHARS = 1_000


@dataclass(frozen=True)
class _Vector:
    """One vector that the item must have, at its place: (kind, ord)."""

    kind: ChunkKind
    ord: int
    text: str
    start: int | None = None
    end: int | None = None


def _wanted(item: Item, text: str) -> list[_Vector]:
    """The vectors of the item: one for its card, and one for each piece of a long text."""
    card = card_text(item.title, item.summary, item.key_points)
    card = card or text[:CARD_STAND_IN_CHARS].strip()
    wanted = [_Vector(ChunkKind.CARD, 0, card)] if card else []
    wanted += [
        _Vector(ChunkKind.BODY, number, piece.text, piece.start, piece.end)
        for number, piece in enumerate(pieces_of(text))
    ]
    return wanted


type _Place = tuple[ChunkKind, int]


async def _stored(session: AsyncSession, item_id: int, set_id: int) -> dict[_Place, _Vector]:
    """The vectors that the item has in the set, without the numbers of the vectors."""
    rows = await session.execute(
        sa.select(Chunk.kind, Chunk.ord, Chunk.text, Chunk.char_start, Chunk.char_end).where(
            Chunk.item_id == item_id, Chunk.embedding_set_id == set_id
        )
    )
    return {
        (kind, number): _Vector(kind, number, text, start, end)
        for kind, number, text, start, end in rows
    }


def _unchanged(item: Item, content_hash: str | None) -> sa.ColumnElement[bool]:
    """The card and the text of the item are still the ones that the step read."""
    hash_now = (
        sa.select(ItemContent.content_hash).where(ItemContent.item_id == item.id).scalar_subquery()
    )
    return sa.and_(
        Item.title.is_not_distinct_from(item.title),
        Item.summary.is_not_distinct_from(item.summary),
        Item.key_points == item.key_points,
        hash_now.is_not_distinct_from(content_hash),
    )


async def _still_ready(
    session: AsyncSession, item_id: int, unchanged: sa.ColumnElement[bool]
) -> bool:
    """Lock the item while it is ready and unchanged. False when it moved or changed."""
    found = await session.scalar(
        sa.select(Item.id)
        .where(Item.id == item_id, Item.enrich_state == EnrichState.READY, unchanged)
        .with_for_update()
    )
    return found is not None


async def _write_vectors(
    session: AsyncSession,
    item: Item,
    set_id: int,
    *,
    changed: list[_Vector],
    gone: list[_Place],
    vectors: dict[str, list[float]],
) -> None:
    in_set = (Chunk.item_id == item.id, Chunk.embedding_set_id == set_id)
    if gone:
        await session.execute(
            sa.delete(Chunk).where(*in_set, sa.tuple_(Chunk.kind, Chunk.ord).in_(gone))
        )
    if not changed:
        return
    insert = postgresql.insert(Chunk).values(
        [
            {
                "owner_id": item.owner_id,
                "item_id": item.id,
                "embedding_set_id": set_id,
                "kind": vector.kind,
                "ord": vector.ord,
                "text": vector.text,
                "char_start": vector.start,
                "char_end": vector.end,
                "dim": len(vectors[vector.text]),
                "embedding": vectors[vector.text],
            }
            for vector in changed
        ]
    )
    columns = ("text", "char_start", "char_end", "dim", "embedding")
    await session.execute(
        insert.on_conflict_do_update(
            constraint="uq_chunks_position",
            set_={column: insert.excluded[column] for column in columns},
        )
    )


@registry.handler(JobKind.EMBED_ITEM)
async def embed_item(ctx: JobContext, payload: EmbedItemPayload) -> None:
    """Store the vectors of the item in the embedding set of the embedder: one for its card,
    and one for each piece of a long text (``unshelve.embed.chunks``). After this step, the
    item is ready.

    A vector whose text did not change stays, so a second run asks the embedder nothing. A
    failure that can pass is tried again later. The last attempt, and a failure that cannot
    pass, leave the item ready without vectors in the set, and an event says why.
    """
    services = ctx.services
    async with services.sessions() as session:
        item = await session.get(Item, payload.item_id)
        if item is None:
            raise Permanent(f"item {payload.item_id} does not exist")
        if item.enrich_state not in _EMBEDDABLE:
            return
        content = await session.get(ItemContent, item.id)
        set_id = await ensure_set(session, services.embedder.spec)
        stored = await _stored(session, item.id, set_id)
        await session.commit()

    text = content.text if content is not None else ""
    content_hash = content.content_hash if content is not None else None
    wanted = _wanted(item, text)
    changed = [vector for vector in wanted if stored.get((vector.kind, vector.ord)) != vector]
    gone = sorted(set(stored) - {(vector.kind, vector.ord) for vector in wanted})
    if item.enrich_state is EnrichState.READY and not changed and not gone:
        return  # each vector is there already

    new_texts = list(dict.fromkeys(vector.text for vector in changed))
    vectors: dict[str, list[float]] = {}
    failure: ProviderError | None = None
    if new_texts:
        try:
            found = await services.embedder.embed_documents(new_texts)
        except ProviderError as error:
            if error.retryable and ctx.job.attempts < ctx.job.max_attempts:
                raise Retry(
                    f"embed: {error}", after=_model_wait(error, ctx.job.attempts)
                ) from error
            failure = error
        else:
            vectors.update(zip(new_texts, found, strict=True))

    async with services.sessions() as session:
        unchanged = _unchanged(item, content_hash)
        if item.enrich_state is EnrichState.ENRICHED:
            moved = await _advance(
                session,
                item.id,
                start=EnrichState.ENRICHED,
                end=EnrichState.READY,
                unchanged=unchanged,
            )
        else:
            moved = await _still_ready(session, item.id, unchanged)
        if moved and failure is None:
            await _write_vectors(session, item, set_id, changed=changed, gone=gone, vectors=vectors)
        elif moved and failure is not None:
            # The vector of an old text is worse than none: a search would find the item for
            # words that it does not have now. A vector of a text that did not change stays.
            # A backfill gives the item its missing vectors later.
            stale = {(vector.kind, vector.ord) for vector in changed} & stored.keys()
            await _write_vectors(
                session, item, set_id, changed=[], gone=sorted(stale | set(gone)), vectors={}
            )
            session.add(
                Event(
                    owner_id=item.owner_id,
                    item_id=item.id,
                    actor=Actor.WORKER,
                    type="embed_failed",
                    payload={"reason": str(failure)[:300], "attempt": ctx.job.attempts},
                )
            )
        await session.commit()
    if moved and failure is not None:
        log.info("embed.no_vectors", item_id=item.id, reason=str(failure)[:300])
    elif moved:
        log.info(
            "embed.done", item_id=item.id, set_id=set_id, vectors=len(wanted), new=len(new_texts)
        )
