"""Sign-in, sign-out and the current user."""

from fastapi import APIRouter, Depends

from unshelve.api.deps import current_user
from unshelve.api.errors import NotImplementedYetError, problems
from unshelve.api.schemas import MagicConsumeRequest, Me

router = APIRouter(tags=["auth"])


@router.post("/auth/magic/consume", status_code=204, responses=problems(401, 422))
async def consume_magic_link(body: MagicConsumeRequest) -> None:
    """Trade the one-time token from a login link for a session cookie."""
    raise NotImplementedYetError


@router.post(
    "/auth/logout",
    status_code=204,
    dependencies=[Depends(current_user)],
    responses=problems(401),
)
async def logout() -> None:
    """End the current session."""
    raise NotImplementedYetError


@router.get("/me", dependencies=[Depends(current_user)], responses=problems(401))
async def get_me() -> Me:
    """The signed-in user and their settings."""
    raise NotImplementedYetError
