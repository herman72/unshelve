"""Search over the saved items, and an overview answer built from them."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from unshelve.api.deps import current_user
from unshelve.api.errors import NotImplementedYetError, problems
from unshelve.api.schemas import Overview, OverviewRequest, SearchPage, SearchQuery

router = APIRouter(tags=["search"], dependencies=[Depends(current_user)])


@router.get("/search", responses=problems(401, 422))
async def search_items(query: Annotated[SearchQuery, Query()]) -> SearchPage:
    """Hybrid search: keyword, trigram and vector results, fused into one ranking."""
    raise NotImplementedYetError


@router.post("/search/overview", responses=problems(401, 422))
async def create_overview(body: OverviewRequest) -> Overview:
    """An answer built only from saved items. Every statement points at its sources."""
    raise NotImplementedYetError
