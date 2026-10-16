"""Goals: what the user is working towards. Items are scored against them."""

from fastapi import APIRouter, Depends

from unshelve.api import goal_views
from unshelve.api.deps import CurrentUserDep, DbDep, current_user
from unshelve.api.errors import problems
from unshelve.api.routes.params import GoalId
from unshelve.api.schemas import GoalCreate, GoalList, GoalOut, GoalUpdate

router = APIRouter(tags=["goals"], dependencies=[Depends(current_user)])


@router.get("/goals", responses=problems(401, 422))
async def list_goals(user: CurrentUserDep, db: DbDep, include_archived: bool = False) -> GoalList:
    """All goals, the active ones first. Archived goals are left out unless the query asks for
    them."""
    return await goal_views.list_goals(db, user.id, include_archived=include_archived)


@router.post("/goals", status_code=201, responses=problems(401, 409, 422))
async def create_goal(body: GoalCreate, user: CurrentUserDep, db: DbDep) -> GoalOut:
    """Add a goal. A title that gives the slug of an existing goal is a 409, and so is an
    eleventh active goal."""
    goal = await goal_views.create_goal(db, user.id, body)
    await db.commit()
    return goal


@router.patch("/goals/{goal_id}", responses=problems(401, 404, 409, 422))
async def update_goal(
    goal_id: GoalId, body: GoalUpdate, user: CurrentUserDep, db: DbDep
) -> GoalOut:
    """Change a goal, archive it or restore it. Fields that are left out stay as they are. A
    new title, description or keywords give the goal a new version."""
    goal = await goal_views.update_goal(db, user.id, goal_id, body)
    await db.commit()
    return goal


@router.post("/goals/{goal_id}/focus", responses=problems(401, 404, 409, 422))
async def focus_goal(goal_id: GoalId, user: CurrentUserDep, db: DbDep) -> GoalOut:
    """Make this the focus goal. There is at most one focus goal. An archived goal cannot be
    the focus: 409."""
    goal = await goal_views.focus_goal(db, user.id, goal_id)
    await db.commit()
    return goal
