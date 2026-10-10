"""Goals: what the user is working towards. Items are scored against them."""

from fastapi import APIRouter, Depends

from unshelve.api.deps import current_user
from unshelve.api.errors import NotImplementedYetError, problems
from unshelve.api.routes.params import GoalId
from unshelve.api.schemas import GoalCreate, GoalList, GoalOut, GoalUpdate

router = APIRouter(tags=["goals"], dependencies=[Depends(current_user)])


@router.get("/goals", responses=problems(401, 422))
async def list_goals(include_archived: bool = False) -> GoalList:
    """All goals. Archived goals are left out unless the query asks for them."""
    raise NotImplementedYetError


@router.post("/goals", status_code=201, responses=problems(401, 409, 422))
async def create_goal(body: GoalCreate) -> GoalOut:
    """Add a goal. A goal that clashes with an existing one is a 409."""
    raise NotImplementedYetError


@router.patch("/goals/{goal_id}", responses=problems(401, 404, 422))
async def update_goal(goal_id: GoalId, body: GoalUpdate) -> GoalOut:
    """Change a goal or archive it. Fields that are left out stay as they are."""
    raise NotImplementedYetError


@router.post("/goals/{goal_id}/focus", responses=problems(401, 404, 422))
async def focus_goal(goal_id: GoalId) -> GoalOut:
    """Make this the focus goal. There is at most one focus goal."""
    raise NotImplementedYetError
