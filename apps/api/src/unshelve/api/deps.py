"""Request-scoped dependencies."""

from typing import Annotated, cast

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine


def get_engine(request: Request) -> AsyncEngine:
    return cast(AsyncEngine, request.app.state.engine)


EngineDep = Annotated[AsyncEngine, Depends(get_engine)]
