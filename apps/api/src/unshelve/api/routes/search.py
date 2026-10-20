"""Search over the saved items, and an overview answer built from them."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from unshelve.api import search_views
from unshelve.api.deps import CurrentUserDep, DbDep, EmbedderDep, current_user
from unshelve.api.errors import NotImplementedYetError, problems
from unshelve.api.schemas import Overview, OverviewRequest, SearchPage, SearchQuery

router = APIRouter(tags=["search"], dependencies=[Depends(current_user)])


@router.get("/search", responses=problems(401, 422))
async def search_items(
    query: Annotated[SearchQuery, Query()],
    user: CurrentUserDep,
    db: DbDep,
    embedder: EmbedderDep,
) -> SearchPage:
    """Hybrid search: keyword, trigram and vector results, fused into one ranking.

    Each hit has a snippet that shows why it was found, and its place in the list of each
    retriever. When the embedder fails, the words still answer, and vector_search says so.
    """
    return await search_views.search(db, embedder, user.id, query)


@router.post("/search/overview", responses=problems(401, 422))
async def create_overview(body: OverviewRequest) -> Overview:
    """An answer built only from saved items. Every statement points at its sources."""
    raise NotImplementedYetError
