"""Helpers shared by the API tests."""

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts

from unshelve.api.app import create_app
from unshelve.config import Settings

# Nothing listens on port 1. These tests never need a database, so if one of them reaches for
# it, the test fails at once instead of quietly using the development database.
OFFLINE = Settings(
    env="test",
    database_url="postgresql+psycopg://nobody:nothing@127.0.0.1:1/none?connect_timeout=1",
)

# The only operations that answer without a session. Everything else is protected: a new route
# is protected until someone adds its operation id here on purpose.
PUBLIC_OPERATIONS = frozenset({"healthz", "readyz", "consume_magic_link"})


@dataclass(frozen=True)
class Operation:
    """One HTTP method of one route, as the application will serve it."""

    method: str
    path: str
    operation_id: str
    tags: tuple[str, ...]
    in_schema: bool


def build_app() -> FastAPI:
    return create_app(OFFLINE)


def api_operations(app: FastAPI) -> list[Operation]:
    """Every operation of the API, read from the application's route table.

    FastAPI keeps an included router as one entry and resolves its prefix, tags and
    dependencies later. ``iter_route_contexts`` is what FastAPI's own OpenAPI code uses to
    get the resolved routes. The documentation routes (/openapi.json, /docs) are left out.
    """
    return [
        Operation(
            method=method,
            path=context.path or "",
            operation_id=context.unique_id,
            tags=tuple(str(tag) for tag in context.tags),
            in_schema=context.include_in_schema,
        )
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
        for method in sorted(context.methods or ())
    ]


def problem_body(response: httpx.Response, status: int) -> dict[str, Any]:
    """The body of a problem response, after the checks that every problem must pass."""
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    assert isinstance(body["title"], str)
    assert body["title"]
    return body
