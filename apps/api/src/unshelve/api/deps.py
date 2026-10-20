"""Request-scoped dependencies."""

from collections.abc import AsyncGenerator
from typing import Annotated, cast

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from unshelve.api.errors import UnauthorizedError
from unshelve.auth import store
from unshelve.auth.store import SessionUser
from unshelve.config import Settings
from unshelve.contracts.protocols import Embedder

SESSION_COOKIE = "unshelve_session"

_session_cookie = APIKeyCookie(
    name=SESSION_COOKIE,
    auto_error=False,
    description="Set by POST /api/v1/auth/magic/consume.",
)

# The value of the session cookie, or None when the request has no such cookie.
SessionSecretDep = Annotated[str | None, Depends(_session_cookie)]


def get_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]


def app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


SettingsDep = Annotated[Settings, Depends(app_settings)]


async def get_db(request: Request) -> AsyncGenerator[AsyncSession]:
    """One database session for one request. It opens no connection until it is used.

    Nothing is committed on the handler's behalf. A handler that writes calls ``commit``
    itself, at the point where its work is complete.
    """
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        yield session


DbDep = Annotated[AsyncSession, Depends(get_db)]


def get_embedder(request: Request) -> Embedder:
    return cast(Embedder, request.app.state.embedder)


EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


async def current_user(secret: SessionSecretDep, db: DbDep) -> SessionUser:
    """The signed-in user.

    Deny by default: a request is anonymous until its cookie names a live session. Without
    the cookie, the answer is 401 and the database is not asked.
    """
    if secret is None:
        raise UnauthorizedError("sign in first")
    user = await store.find_session(db, secret)
    # Ends the read. It also keeps the "last seen" mark, when find_session wrote one.
    await db.commit()
    if user is None:
        raise UnauthorizedError("sign in first")
    return user


CurrentUserDep = Annotated[SessionUser, Depends(current_user)]
