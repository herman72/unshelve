"""Items as the API shows them: the queries, and the mapping to the response shapes.

Every query here is limited to one owner. An item of a different owner does not exist for the
caller: it is not in a list, and asking for it by id is a 404.
"""

import base64
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.api.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    NotImplementedYetError,
)
from unshelve.api.schemas import (
    DigestPick,
    DigestPreview,
    DigestPreviewRequest,
    EventOut,
    EventPage,
    GoalScore,
    ItemDetail,
    ItemListQuery,
    ItemPage,
    ItemSummary,
    LabelRequest,
    Queue,
    TagRef,
)
from unshelve.contracts.enums import Actor, EnrichState, ItemStatus, LabelSource
from unshelve.contracts.states import (
    STATUS_TRANSITIONS,
    IllegalTransitionError,
    check_enrich_change,
)
from unshelve.db.models import Event, Goal, Item, ItemGoal, ItemTag, Tag, User
from unshelve.ingest import resolve
from unshelve.relevance import labels
from unshelve.resurface.plan import plan_digest
from unshelve.track import status

# --- Paging ------------------------------------------------------------------------------
#
# A page ends at one item, and the next page starts after it. The cursor is the position of
# that item in the sort order: its capture time and its id. This stays correct when new items
# arrive between two requests, which "skip the first 30 rows" does not.


def _encode_cursor(sort: str, item: Item) -> str:
    position = [sort, item.last_captured_at.isoformat(), item.id]
    return base64.urlsafe_b64encode(json.dumps(position).encode()).decode()


def _decode_cursor(cursor: str, sort: str) -> tuple[datetime, int]:
    try:
        cursor_sort, captured_at, item_id = json.loads(base64.urlsafe_b64decode(cursor))
        position = datetime.fromisoformat(captured_at), int(item_id)
    except (ValueError, TypeError) as error:
        raise InvalidInputError("the cursor is not valid") from error
    if cursor_sort != sort or position[0].tzinfo is None:
        raise InvalidInputError("the cursor is not valid")
    return position


# --- Tags and goals of several items, in one query each -----------------------------------


async def _tags_of(db: AsyncSession, item_ids: Sequence[int]) -> dict[int, list[TagRef]]:
    rows = await db.execute(
        sa.select(ItemTag.item_id, Tag.slug, Tag.name, Tag.is_public)
        .join(Tag, Tag.id == ItemTag.tag_id)
        .where(ItemTag.item_id.in_(item_ids))
        .order_by(Tag.slug)
    )
    tags: dict[int, list[TagRef]] = {}
    for item_id, slug, name, is_public in rows:
        tags.setdefault(item_id, []).append(TagRef(slug=slug, name=name, is_public=is_public))
    return tags


async def _goals_of(db: AsyncSession, item_ids: Sequence[int]) -> dict[int, list[GoalScore]]:
    """The scores of each item against the active goals, the highest score first. A score of an
    archived goal stays in the database, but you do not work on that goal now."""
    rows = await db.execute(
        sa.select(
            ItemGoal.item_id,
            ItemGoal.goal_id,
            Goal.slug,
            Goal.title,
            ItemGoal.score,
            ItemGoal.reason,
            ItemGoal.label,
        )
        .join(Goal, Goal.id == ItemGoal.goal_id)
        .where(ItemGoal.item_id.in_(item_ids), Goal.archived_at.is_(None))
        .order_by(ItemGoal.score.desc(), ItemGoal.goal_id)
    )
    goals: dict[int, list[GoalScore]] = {}
    for item_id, goal_id, slug, title, score, reason, label in rows:
        goals.setdefault(item_id, []).append(
            GoalScore(
                goal_id=goal_id,
                goal_slug=slug,
                goal_title=title,
                score=score,
                reason=reason,
                label=label,
            )
        )
    return goals


def _summary_fields(item: Item, tags: list[TagRef], goals: list[GoalScore]) -> dict[str, Any]:
    return {
        "id": item.id,
        "kind": item.kind,
        "status": item.status,
        "enrich_state": item.enrich_state,
        "partial": item.partial,
        "visibility": item.visibility,
        "title": item.title,
        "url": item.canonical_url,
        "site": item.site,
        "author": item.author,
        "lang": item.lang,
        "summary": item.summary,
        "est_minutes": item.est_minutes,
        "tags": tags,
        # An item with only scores of 0 has no top goal: no goal is served by it.
        "top_goal": goals[0] if goals and goals[0].score > 0 else None,
        "capture_count": item.capture_count,
        "first_captured_at": item.first_captured_at,
        "last_captured_at": item.last_captured_at,
        "read_at": item.read_at,
        "published_at": item.published_at,
    }


async def summaries(db: AsyncSession, items: Sequence[Item]) -> list[ItemSummary]:
    """The list shape of ``items``, in their order, with their tags and goal scores: two
    queries for all of them, not two for each one."""
    ids = [item.id for item in items]
    tags, goals = await _tags_of(db, ids), await _goals_of(db, ids)
    return [
        ItemSummary(**_summary_fields(item, tags.get(item.id, []), goals.get(item.id, [])))
        for item in items
    ]


# --- The two reads -----------------------------------------------------------------------


async def list_items(db: AsyncSession, owner_id: int, query: ItemListQuery) -> ItemPage:
    for name in ("tag", "goal_id", "min_score"):
        if getattr(query, name) is not None:
            # An ignored filter would give a wrong list. Saying "not built" is honest.
            raise NotImplementedYetError(f"the filter {name!r} is not built yet")
    if query.sort == "relevance":
        raise NotImplementedYetError("the sort 'relevance' is not built yet")

    # A repeated save is merged into the first item. The merged copy is not shown in lists.
    statement = sa.select(Item).where(Item.owner_id == owner_id, Item.duplicate_of_id.is_(None))
    if query.status:
        statement = statement.where(Item.status.in_(query.status))
    if query.kind:
        statement = statement.where(Item.kind.in_(query.kind))
    if query.visibility:
        statement = statement.where(Item.visibility == query.visibility)

    newest_first = query.sort == "captured_desc"
    position = sa.tuple_(Item.last_captured_at, Item.id)
    if query.cursor:
        after = sa.tuple_(*_decode_cursor(query.cursor, query.sort))
        statement = statement.where(position < after if newest_first else position > after)
    order = (
        (Item.last_captured_at.desc(), Item.id.desc())
        if newest_first
        else (Item.last_captured_at.asc(), Item.id.asc())
    )
    # One row more than the page: if it exists, there is a next page.
    found = (await db.execute(statement.order_by(*order).limit(query.limit + 1))).scalars().all()
    items = found[: query.limit]
    return ItemPage(
        items=await summaries(db, items),
        next_cursor=_encode_cursor(query.sort, items[-1]) if len(found) > query.limit else None,
    )


async def get_item(db: AsyncSession, owner_id: int, item_id: int) -> ItemDetail:
    item = await db.scalar(sa.select(Item).where(Item.id == item_id, Item.owner_id == owner_id))
    if item is None:
        raise NotFoundError("no such item")
    tags = (await _tags_of(db, [item.id])).get(item.id, [])
    goals = (await _goals_of(db, [item.id])).get(item.id, [])
    # A merged copy does not move: its moves belong to the item that it is a copy of.
    allowed = (
        STATUS_TRANSITIONS[item.status] if item.duplicate_of_id is None else frozenset[ItemStatus]()
    )
    return ItemDetail(
        **_summary_fields(item, tags, goals),
        key_points=item.key_points,
        extra_links=item.extra_links,
        note=item.note,
        public_note=item.public_note,
        public_slug=item.public_slug,
        duplicate_of_id=item.duplicate_of_id,
        goals=goals,
        allowed_transitions=[status for status in ItemStatus if status in allowed],
    )


# --- The reading status -----------------------------------------------------------------


async def transition_item(
    db: AsyncSession, owner_id: int, item_id: int, target: ItemStatus
) -> None:
    """Move an item to ``target`` through the one write path. The caller commits.

    The same status again is no error: a second click finds the item moved already.
    """
    change = await status.change_status(db, owner_id, item_id, target, actor=Actor.USER, via="api")
    match change.outcome:
        case status.Outcome.NOT_FOUND:
            raise NotFoundError("no such item")
        case status.Outcome.NOT_ALLOWED:
            raise IllegalTransitionError(str(change.status), target)
        case status.Outcome.QUEUE_FULL:
            raise ConflictError(
                f"the reading queue is full: all {change.limit} slots are used; move an item out "
                "of it first"
            )
        case status.Outcome.DUPLICATE:
            raise ConflictError(
                f"the item is the same thing as item {change.duplicate_of}: change that one"
            )
        case status.Outcome.CHANGED | status.Outcome.ALREADY:
            return


async def get_queue(db: AsyncSession, owner_id: int) -> Queue:
    """The reading queue: the items that are being read first, then the items that wait.

    In each group, the item that took its slot first comes first. That moment is the newest
    status event of the item: the log knows when each item moved. An item without a move in the
    log (its status was written before the log) counts as the oldest.
    """
    limit = (await db.execute(sa.select(User.queue_limit).where(User.id == owner_id))).scalar_one()
    moved = (
        sa.select(sa.func.max(Event.id))
        .where(Event.item_id == Item.id, Event.type == "status")
        .scalar_subquery()
    )
    items = (
        await db.scalars(
            sa.select(Item)
            .where(status.in_queue(owner_id))
            .order_by(
                (Item.status == ItemStatus.READING).desc(), moved.asc().nulls_first(), Item.id
            )
        )
    ).all()
    return Queue(limit=limit, used=len(items), items=await summaries(db, items))


# A page of events ends at one event, and the next page starts after it: newest first, by id.


def _encode_event_cursor(event_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps(["events", event_id]).encode()).decode()


def _decode_event_cursor(cursor: str) -> int:
    try:
        kind, event_id = json.loads(base64.urlsafe_b64decode(cursor))
        position = int(event_id)
    except (ValueError, TypeError) as error:
        raise InvalidInputError("the cursor is not valid") from error
    if kind != "events":
        raise InvalidInputError("the cursor is not valid")
    return position


async def list_events(
    db: AsyncSession, owner_id: int, item_id: int, *, limit: int, cursor: str | None
) -> EventPage:
    """The history of an item, newest first: its moves, and what the workers did with it."""
    exists = await db.scalar(
        sa.select(Item.id).where(Item.id == item_id, Item.owner_id == owner_id)
    )
    if exists is None:
        raise NotFoundError("no such item")
    statement = sa.select(Event).where(Event.item_id == item_id, Event.owner_id == owner_id)
    if cursor:
        statement = statement.where(Event.id < _decode_event_cursor(cursor))
    found = (await db.scalars(statement.order_by(Event.id.desc()).limit(limit + 1))).all()
    events = found[:limit]
    return EventPage(
        items=[
            EventOut(
                id=event.id,
                actor=event.actor,
                type=event.type,
                from_status=event.from_status,
                to_status=event.to_status,
                payload=event.payload,
                created_at=event.created_at,
            )
            for event in events
        ],
        next_cursor=_encode_event_cursor(events[-1].id) if len(found) > limit else None,
    )


# --- Your verdicts on the scores ---------------------------------------------------------


async def set_goal_label(
    db: AsyncSession, owner_id: int, item_id: int, goal_id: int, body: LabelRequest
) -> GoalScore:
    """Give the score of one goal for one item your verdict from the web app. The caller commits."""
    change = await labels.set_label(
        db,
        owner_id,
        item_id,
        goal_id,
        body.label,
        source=LabelSource.WEB,
        shown_score=body.score,
    )
    if change.row is None:
        raise NotFoundError("the item has no score for this goal")
    if change.outcome is labels.LabelOutcome.SCORE_CHANGED:
        raise ConflictError(
            f"the score changed since you saw it: it is {change.row.score} now; look at it again"
        )
    goal = await db.get_one(Goal, goal_id)
    return GoalScore(
        goal_id=goal.id,
        goal_slug=goal.slug,
        goal_title=goal.title,
        score=change.row.score,
        reason=change.row.reason,
        label=change.row.label,
    )


# --- A preview of the digest ------------------------------------------------------------


async def preview_digest(
    db: AsyncSession, owner_id: int, body: DigestPreviewRequest, *, now: datetime
) -> DigestPreview:
    """The digest of now, for the reading time of the request. Nothing is sent or marked.

    The day is the seed: a second preview of the same day shows the same random pick.
    """
    try:
        planned = await plan_digest(
            db,
            owner_id,
            now=now,
            seed=f"{owner_id}:{now:%Y-%m-%d}",
            minutes=body.minutes,
            goal_id=body.goal_id,
        )
    except LookupError as error:
        raise NotFoundError(str(error)) from error
    shown = await summaries(db, [item for item, _ in planned.picks])
    return DigestPreview(
        picks=[
            DigestPick(
                item=summary,
                reason=pick.reason.value,
                goal_title=None if pick.goal_id is None else planned.goal_titles[pick.goal_id],
            )
            for summary, (_, pick) in zip(shown, planned.picks, strict=True)
        ],
        total_minutes=planned.total_minutes,
        goal_title=planned.goal.title if planned.goal is not None else None,
    )


# --- Processing again ----------------------------------------------------------------------

# An item in these states has no step running, so it can start again. In the other states a
# step is at work, and a second start would only race with it.
REPROCESSABLE = frozenset({EnrichState.READY, EnrichState.FAILED})


async def reprocess_item(db: AsyncSession, owner_id: int, item_id: int) -> None:
    """Send a finished or failed item back to the start of its pipeline. The caller commits.

    The row is locked: of two requests at the same moment (a double click), the second one
    sees the item in the pipeline already, and gets a 409.
    """
    item = await db.scalar(
        sa.select(Item).where(Item.id == item_id, Item.owner_id == owner_id).with_for_update()
    )
    if item is None:
        raise NotFoundError("no such item")
    if item.enrich_state not in REPROCESSABLE:
        raise ConflictError(f"the item is still in the pipeline ({item.enrich_state})")
    check_enrich_change(item.enrich_state, EnrichState.PENDING)
    db.add(
        Event(
            owner_id=owner_id,
            item_id=item_id,
            actor=Actor.USER,
            type="reprocess",
            payload={"enrich_state": item.enrich_state.value},
        )
    )
    await resolve.restart_item(db, owner_id, item_id)
