"""Items: list, create, edit, move through the reading states, publish."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from unshelve.api.deps import current_user
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

router = APIRouter(tags=["items"], dependencies=[Depends(current_user)])


@router.get("/items", responses=problems(401, 422))
async def list_items(query: Annotated[ItemListQuery, Query()]) -> ItemPage:
    """A page of items, filtered and sorted by the query."""
    raise NotImplementedYetError


@router.post("/items", status_code=201, responses=problems(401, 422))
async def create_item(body: ItemCreate) -> ItemDetail:
    """Save an item by hand: a link, a note, or both."""
    raise NotImplementedYetError


@router.get("/items/{item_id}", responses=problems(401, 404, 422))
async def get_item(item_id: ItemId) -> ItemDetail:
    """One item in full."""
    raise NotImplementedYetError


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
