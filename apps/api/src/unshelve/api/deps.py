"""Request-scoped dependencies."""

from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncEngine

from unshelve.api.errors import NotImplementedYetError, UnauthorizedError

SESSION_COOKIE = "unshelve_session"

_session_cookie = APIKeyCookie(
    name=SESSION_COOKIE,
    auto_error=False,
    description="Set by POST /api/v1/auth/magic/consume.",
)


def get_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    id: int
    telegram_user_id: int


async def current_user(token: Annotated[str | None, Depends(_session_cookie)]) -> CurrentUser:
    """The signed-in user.

    Deny by default: until sessions exist (work package M0-6), nobody is signed in. A request
    without the cookie is 401. A request with a cookie cannot be checked yet, so it is 501
    and never treated as valid.
    """
    if token is None:
        raise UnauthorizedError("sign in first")
    raise NotImplementedYetError("sessions arrive with the login work package")


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]
