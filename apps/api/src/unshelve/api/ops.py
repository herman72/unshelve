"""Liveness and readiness probes."""

from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from unshelve import __version__
from unshelve.api.deps import EngineDep

router = APIRouter(tags=["ops"])


class Health(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = __version__


@router.get("/healthz")
async def healthz() -> Health:
    """The process is up. Checks nothing else, so a database outage does not restart the API."""
    return Health()


@router.get("/readyz", responses={status.HTTP_503_SERVICE_UNAVAILABLE: {}})
async def readyz(engine: EngineDep) -> Health:
    """The process can serve requests: the database answers."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="database is not reachable"
        ) from exc
    return Health()
