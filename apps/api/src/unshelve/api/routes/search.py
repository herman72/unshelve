"""Search over the saved items, and an overview answer built from them."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from unshelve.api import search_views
from unshelve.api.deps import CitingLLMDep, CurrentUserDep, DbDep, EmbedderDep, current_user
from unshelve.api.errors import problems
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


@router.post("/search/overview", responses=problems(401, 422, 503))
async def create_overview(
    body: OverviewRequest,
    user: CurrentUserDep,
    db: DbDep,
    embedder: EmbedderDep,
    llm: CitingLLMDep,
) -> Overview:
    """An answer built only from saved items. Every statement points at its sources.

    The model reads the passages of the best items, and each citation names one retrieved
    piece of one item. The same question on the same items is answered from the ledger, at no
    cost. When the model cannot answer (no key, the daily budget is used), the answer is a 503.
    """
    return await search_views.answer(db, embedder, llm, user.id, body)
