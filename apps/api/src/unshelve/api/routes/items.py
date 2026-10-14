"""Items: list, create, edit, move through the reading states, publish."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from unshelve.api import item_views
from unshelve.api.deps import CurrentUserDep, DbDep, current_user
from unshelve.api.errors import NotImplementedYetError, problems
from unshelve.api.routes.params import GoalId, ItemId
from unshelve.api.schemas import (
    EventPage,
    GoalScore,
    ItemCreate,
    ItemDetail,
    ItemListQuery,
    ItemPage,
    ItemUpdate,
    LabelRequest,
    PublicItem,
    SimilarItems,
    TransitionRequest,
    VisibilityRequest,
    VisibilityResult,
)
from unshelve.contracts.dto import CaptureEnvelope
from unshelve.contracts.enums import CaptureSource
from unshelve.ingest import resolve

router = APIRouter(tags=["items"], dependencies=[Depends(current_user)])


@router.get("/items", responses=problems(401, 422))
async def list_items(
    query: Annotated[ItemListQuery, Query()], user: CurrentUserDep, db: DbDep
) -> ItemPage:
    """A page of items, filtered and sorted by the query."""
    return await item_views.list_items(db, user.id, query)


@router.post("/items", status_code=201, responses=problems(401, 422))
async def create_item(body: ItemCreate, user: CurrentUserDep, db: DbDep) -> ItemDetail:
    """Save an item by hand: a link, a note, or both."""
    # The answer comes at once, with the item as it is now: saved and not processed yet. The
    # worker does the rest. A later read of the item shows its summary.
    envelope = CaptureEnvelope(
        source=CaptureSource.API,
        chat_id=resolve.API_CHAT_ID,
        message_id=await resolve.next_api_message_id(db),
        posted_at=datetime.now(UTC),
        text=body.text or "",
        urls=[body.url] if body.url else [],
    )
    capture_id = await resolve.store_capture(db, user.id, envelope)
    if capture_id is None:  # cannot happen: the message id is new
        msg = "a capture with a new message id was refused as a repeat"
        raise RuntimeError(msg)
    item_id = await resolve.resolve_capture(db, capture_id)
    if item_id is None:  # cannot happen: a capture of the API is a first version
        msg = "a first version of a message got no item"
        raise RuntimeError(msg)
    # The capture, the item and its first job are saved together, or not at all.
    await db.commit()
    return await item_views.get_item(db, user.id, item_id)


@router.get("/items/{item_id}", responses=problems(401, 404, 422))
async def get_item(item_id: ItemId, user: CurrentUserDep, db: DbDep) -> ItemDetail:
    """One item in full."""
    return await item_views.get_item(db, user.id, item_id)


@router.patch("/items/{item_id}", responses=problems(401, 404, 422))
async def update_item(item_id: ItemId, body: ItemUpdate) -> ItemDetail:
    """Change the title, the notes or the tags. Fields that are left out stay as they are."""
    raise NotImplementedYetError


@router.post("/items/{item_id}/transition", responses=problems(401, 404, 409, 422))
async def transition_item(item_id: ItemId, body: TransitionRequest) -> ItemDetail:
    """Move the item to another reading status. A move the state machine forbids is a 409."""
    raise NotImplementedYetError


@router.get("/items/{item_id}/public-preview", responses=problems(401, 404, 422))
async def get_public_preview(item_id: ItemId) -> PublicItem:
    """Exactly what the public feed would contain for this item, shown before publishing."""
    raise NotImplementedYetError


@router.put("/items/{item_id}/visibility", responses=problems(401, 404, 422))
async def set_item_visibility(item_id: ItemId, body: VisibilityRequest) -> VisibilityResult:
    """Publish the item or make it private again."""
    raise NotImplementedYetError


@router.post("/items/{item_id}/reprocess", status_code=202, responses=problems(401, 404, 409, 422))
async def reprocess_item(item_id: ItemId) -> ItemDetail:
    """Send a failed or finished item back to the start of the pipeline.

    The answer comes at once and the work happens later, on the worker. An item that is still
    being processed cannot be sent back: 409.
    """
    raise NotImplementedYetError


@router.get("/items/{item_id}/events", responses=problems(401, 404, 422))
async def list_item_events(
    item_id: ItemId,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: str | None = None,
) -> EventPage:
    """A page of the item's history."""
    raise NotImplementedYetError


@router.get("/items/{item_id}/similar", responses=problems(401, 404, 422))
async def list_similar_items(
    item_id: ItemId, limit: Annotated[int, Query(ge=1, le=20)] = 5
) -> SimilarItems:
    """The saved items that are closest in meaning to this one."""
    raise NotImplementedYetError


@router.put("/items/{item_id}/goals/{goal_id}/label", responses=problems(401, 404, 422))
async def set_goal_label(item_id: ItemId, goal_id: GoalId, body: LabelRequest) -> GoalScore:
    """Confirm or reject the score of one goal for this item. A null label removes the verdict."""
    raise NotImplementedYetError
