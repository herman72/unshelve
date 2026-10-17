"""Bringing items back: the reading queue and the weekly digest."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from unshelve.api import item_views
from unshelve.api.deps import CurrentUserDep, DbDep, current_user
from unshelve.api.errors import NotImplementedYetError, problems
from unshelve.api.schemas import DigestPage, DigestPreview, DigestPreviewRequest, Queue

router = APIRouter(tags=["resurface"], dependencies=[Depends(current_user)])


@router.get("/queue", responses=problems(401))
async def get_queue(user: CurrentUserDep, db: DbDep) -> Queue:
    """The items the user plans to read, with the queue limit and how much of it is used."""
    return await item_views.get_queue(db, user.id)


@router.get("/digests", responses=problems(401, 422))
async def list_digests(
    limit: Annotated[int, Query(ge=1, le=50)] = 10, cursor: str | None = None
) -> DigestPage:
    """A page of past digests."""
    raise NotImplementedYetError


@router.post("/digests/preview", responses=problems(401, 404, 422))
async def preview_digest(
    body: DigestPreviewRequest, user: CurrentUserDep, db: DbDep
) -> DigestPreview:
    """The picks a digest would contain now, for a reading time budget, and why. Nothing is
    sent, and nothing is marked as shown. A goal that is not an active goal is a 404."""
    return await item_views.preview_digest(db, user.id, body, now=datetime.now(UTC))
