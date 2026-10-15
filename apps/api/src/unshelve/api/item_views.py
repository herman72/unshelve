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
    GoalScore,
    ItemDetail,
    ItemListQuery,
    ItemPage,
    ItemSummary,
    TagRef,
)
from unshelve.contracts.enums import Actor, EnrichState, ItemStatus
from unshelve.contracts.states import STATUS_TRANSITIONS, check_enrich_change
from unshelve.db.models import Event, Goal, Item, ItemGoal, ItemTag, Tag
from unshelve.ingest import resolve

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
    """The scores of each item, the highest score first."""
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
        .where(ItemGoal.item_id.in_(item_ids))
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
        "top_goal": goals[0] if goals else None,
        "capture_count": item.capture_count,
        "first_captured_at": item.first_captured_at,
        "last_captured_at": item.last_captured_at,
        "read_at": item.read_at,
        "published_at": item.published_at,
    }


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

    ids = [item.id for item in items]
    tags, goals = await _tags_of(db, ids), await _goals_of(db, ids)
    return ItemPage(
        items=[
            ItemSummary(**_summary_fields(item, tags.get(item.id, []), goals.get(item.id, [])))
            for item in items
        ],
        next_cursor=_encode_cursor(query.sort, items[-1]) if len(found) > query.limit else None,
    )


async def get_item(db: AsyncSession, owner_id: int, item_id: int) -> ItemDetail:
    item = await db.scalar(sa.select(Item).where(Item.id == item_id, Item.owner_id == owner_id))
    if item is None:
        raise NotFoundError("no such item")
    tags = (await _tags_of(db, [item.id])).get(item.id, [])
    goals = (await _goals_of(db, [item.id])).get(item.id, [])
    allowed = STATUS_TRANSITIONS[item.status]
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
