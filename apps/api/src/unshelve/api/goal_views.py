"""Goals as the API shows them, and the one place that writes them.

A goal has a version. It goes up when the text that the scorer reads changes: the title, the
description or the keywords. A score keeps the version that it read (``item_goals``), so a
score of an older version is known as old. A change of the dates, an archive and the focus
do not change what the goal means, so they keep the version.

Each write locks the row of the owner first. So two requests at the same moment count the
active goals, look for a clash and move the focus one after the other.
"""

import hashlib
import re
import unicodedata
from datetime import UTC, date, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from unshelve.api.errors import ConflictError, InvalidInputError, NotFoundError
from unshelve.api.schemas import GoalCreate, GoalList, GoalOut, GoalUpdate
from unshelve.db.models import Goal, User

# Each goal adds words to each question to the model that scores an item. Ten goals is more
# than a year of plans needs, and it keeps the question short.
MAX_ACTIVE_GOALS = 10
_MAX_SLUG_LENGTH = 60
_NOT_A_SLUG = re.compile(r"[^a-z0-9]+")


def slug_for(title: str) -> str:
    """The slug of a new goal: the Latin letters and digits of its title.

    "Rust & systems programming" gives "rust-systems-programming", and "Café" gives "cafe". A
    title without them, such as a Persian title, gives "goal-" and a short hash of the title.
    The same title gives the same slug, so a second click on "Add" finds a clash.
    """
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    slug = _NOT_A_SLUG.sub("-", plain).strip("-")
    if len(slug) > _MAX_SLUG_LENGTH:
        slug = slug[:_MAX_SLUG_LENGTH].rsplit("-", 1)[0]
    if slug:
        return slug
    digest = hashlib.sha256(" ".join(title.casefold().split()).encode()).hexdigest()
    return f"goal-{digest[:8]}"


def _keywords(values: list[str]) -> list[str]:
    """Each keyword one time, in its first place. The schema refuses an empty keyword, and it
    removes the spaces around each one."""
    return list(dict.fromkeys(values))


def goal_out(goal: Goal) -> GoalOut:
    return GoalOut.model_validate(goal)


async def _lock_owner(db: AsyncSession, owner_id: int) -> None:
    await db.execute(sa.select(User.id).where(User.id == owner_id).with_for_update())


async def _active_goals(db: AsyncSession, owner_id: int) -> int:
    count = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Goal)
        .where(Goal.owner_id == owner_id, Goal.archived_at.is_(None))
    )
    return count or 0


async def _goal(db: AsyncSession, owner_id: int, goal_id: int) -> Goal:
    goal = await db.scalar(
        sa.select(Goal).where(Goal.id == goal_id, Goal.owner_id == owner_id).with_for_update()
    )
    if goal is None:
        raise NotFoundError("no such goal")
    return goal


def _check_dates(starts_on: date | None, ends_on: date | None) -> None:
    if starts_on is not None and ends_on is not None and ends_on < starts_on:
        raise InvalidInputError("the goal ends before it starts")


async def list_goals(db: AsyncSession, owner_id: int, *, include_archived: bool) -> GoalList:
    """The goals, the active ones first, each group in the order of creation."""
    statement = sa.select(Goal).where(Goal.owner_id == owner_id)
    if not include_archived:
        statement = statement.where(Goal.archived_at.is_(None))
    goals = await db.scalars(statement.order_by(Goal.archived_at.is_not(None), Goal.id))
    return GoalList(items=[goal_out(goal) for goal in goals])


async def create_goal(db: AsyncSession, owner_id: int, body: GoalCreate) -> GoalOut:
    """Add a goal. The caller commits."""
    _check_dates(body.starts_on, body.ends_on)
    await _lock_owner(db, owner_id)
    slug = slug_for(body.title)
    clash = await db.scalar(sa.select(Goal).where(Goal.owner_id == owner_id, Goal.slug == slug))
    if clash is not None:
        where = "an archived goal" if clash.archived_at else "a goal"
        raise ConflictError(f"{where} with this title exists already: {clash.title!r}")
    if await _active_goals(db, owner_id) >= MAX_ACTIVE_GOALS:
        raise ConflictError(
            f"you have {MAX_ACTIVE_GOALS} active goals, the most; archive one first"
        )
    goal = Goal(
        owner_id=owner_id,
        slug=slug,
        title=body.title,
        description=body.description.strip(),
        keywords=_keywords(body.keywords),
        starts_on=body.starts_on,
        ends_on=body.ends_on,
        version=1,
        is_focus=False,
    )
    db.add(goal)
    await db.flush()
    return goal_out(goal)


async def update_goal(
    db: AsyncSession, owner_id: int, goal_id: int, body: GoalUpdate, *, now: datetime | None = None
) -> GoalOut:
    """Change a goal. Only the fields of the request change. The caller commits."""
    sent = body.model_fields_set
    for name in ("title", "description", "keywords", "archived"):
        if name in sent and getattr(body, name) is None:
            raise InvalidInputError(f"{name} cannot be null; leave it out to keep it")
    await _lock_owner(db, owner_id)
    goal = await _goal(db, owner_id, goal_id)

    meaning = (goal.title, goal.description, goal.keywords)
    if body.title is not None:
        goal.title = body.title
    if body.description is not None:
        goal.description = body.description.strip()
    if body.keywords is not None:
        goal.keywords = _keywords(body.keywords)
    if "starts_on" in sent:
        goal.starts_on = body.starts_on
    if "ends_on" in sent:
        goal.ends_on = body.ends_on
    _check_dates(goal.starts_on, goal.ends_on)
    if (goal.title, goal.description, goal.keywords) != meaning:
        goal.version += 1

    if body.archived is True and goal.archived_at is None:
        goal.archived_at = now or datetime.now(UTC)
        goal.is_focus = False  # an archived goal is not worked on
    elif body.archived is False and goal.archived_at is not None:
        if await _active_goals(db, owner_id) >= MAX_ACTIVE_GOALS:
            raise ConflictError(
                f"you have {MAX_ACTIVE_GOALS} active goals, the most; archive one first"
            )
        goal.archived_at = None
    await db.flush()
    return goal_out(goal)


async def focus_goal(db: AsyncSession, owner_id: int, goal_id: int) -> GoalOut:
    """Make the goal the focus, and the focus of before a goal like the others."""
    await _lock_owner(db, owner_id)
    goal = await _goal(db, owner_id, goal_id)
    if goal.archived_at is not None:
        raise ConflictError("an archived goal cannot be the focus; restore it first")
    if not goal.is_focus:
        # The old focus first: the database allows one focus for each owner at any moment.
        await db.execute(
            sa.update(Goal)
            .where(Goal.owner_id == owner_id, Goal.is_focus, Goal.id != goal.id)
            .values(is_focus=False)
        )
        goal.is_focus = True
        await db.flush()
    return goal_out(goal)
