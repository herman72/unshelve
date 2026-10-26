"""Operations: tags, counters and the job queue."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from unshelve.api import stats_views
from unshelve.api.deps import CurrentUserDep, DbDep, SettingsDep, current_user
from unshelve.api.errors import NotImplementedYetError, problems
from unshelve.api.routes.params import JobId
from unshelve.api.schemas import JobListQuery, JobOut, JobPage, Stats, TagList

router = APIRouter(tags=["admin"], dependencies=[Depends(current_user)])


@router.get("/tags", responses=problems(401))
async def list_tags() -> TagList:
    """Every tag with the number of items that carry it."""
    raise NotImplementedYetError


@router.get("/stats", responses=problems(401))
async def get_stats(user: CurrentUserDep, db: DbDep, settings: SettingsDep) -> Stats:
    """Your items by state, the job queue with its depth and failures, and what the model calls
    cost for each task. The queue and the costs are those of the whole instance."""
    return await stats_views.stats(db, user.id, settings)


@router.get("/admin/jobs", responses=problems(401, 422))
async def list_jobs(query: Annotated[JobListQuery, Query()]) -> JobPage:
    """A page of queue jobs, filtered by state and kind."""
    raise NotImplementedYetError


@router.post("/admin/jobs/{job_id}/retry", responses=problems(401, 404, 409, 422))
async def retry_job(job_id: JobId) -> JobOut:
    """Put a dead job back in the queue. A job in any other state is a 409."""
    raise NotImplementedYetError
